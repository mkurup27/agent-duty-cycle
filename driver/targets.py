"""Delivery adapters: one per arm. Each returns a flat record the runner logs.

Every record carries:
  driver_total_s   send -> result, measured on the driver (perf_counter)
  result           the agent's JSON response (or None)
  error            transport-level failure (None when delivered)
plus arm-specific phases. Agent-internal time is result.timing.total_s, so
  platform_overhead_s = driver_total_s - result.timing.total_s
is skew-free (both are durations measured on one clock each).
"""

import base64
import json
import os
import time
import urllib.error
import urllib.request

from .hr_api import HarnessRuntime, HRError


def _now():
    return time.time()


class Target:
    kind = "base"

    def __init__(self, name, cfg):
        self.name, self.cfg = name, cfg

    def describe(self):
        """Non-secret metadata for the run manifest."""
        return {"kind": self.kind}

    def deliver(self, ev, next_due_in_s):
        raise NotImplementedError

    def close(self):
        pass


# ---------------------------------------------------------------- arm (a)
class DropletTarget(Target):
    """cfg: {url: http://10.x.x.x:8080, token_env: AGENT_TOKEN_A2}"""
    kind = "droplet"

    def describe(self):
        return {"kind": self.kind, "url": self.cfg["url"]}

    def deliver(self, ev, next_due_in_s):
        token = os.environ.get(self.cfg.get("token_env", "AGENT_TOKEN"), "")
        req = urllib.request.Request(
            self.cfg["url"].rstrip("/") + "/event?wait=900",
            data=json.dumps(ev).encode(),
            headers={"Content-Type": "application/json", "X-Agent-Token": token})
        rec = {"sent_wall": _now()}
        t = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=960) as r:
                rec["result"] = json.loads(r.read())
                rec["http_status"] = r.status
            rec["error"] = None
        except Exception as e:  # noqa: BLE001
            rec.update(result=None, error=f"{type(e).__name__}: {e}")
        rec["driver_total_s"] = round(time.perf_counter() - t, 4)
        return rec


# ---------------------------------------------------------------- arm (b)
class FunctionsTarget(Target):
    """cfg: {apihost: https://faas-nyc1-xxxx.doserverless.co, namespace: fn-...,
             action: agent/handle_2pct, key_id_env: FN_KEY_ID, key_secret_env: FN_KEY_SECRET}

    Always invokes NON-blocking (blocking=false) and polls the activation
    record. Measured on DO (2026-10-28, BUGLOG B-006 / P-004): a blocking call
    that runs past the 30 s window returns 202 + an activationId whose record
    is never retrievable (blocking activations are not retained), so the
    result is lost. Non-blocking activations are retained and carry the
    billing-relevant fields: duration (GiB-s = memory x duration, 100 ms
    minimum), waitTime and initTime.

    Polling backs off 0.2 s -> 2 s, so short events add little driver-side
    latency. Latency analysis uses the activation's own timestamps, not the
    driver round trip.
    """
    kind = "functions"
    BACKOFF = (0.2, 0.2, 0.3, 0.5, 0.5, 1.0, 1.0)
    POLL_MAX_S = 2.0
    DEADLINE_S = 1000          # Functions max timeout is 900 s

    def describe(self):
        return {"kind": self.kind, "apihost": self.cfg["apihost"],
                "namespace": self.cfg["namespace"], "action": self.cfg["action"],
                "invoke": "non-blocking + poll"}

    def _auth(self):
        kid = os.environ.get(self.cfg.get("key_id_env", "FN_KEY_ID"), "")
        sec = os.environ.get(self.cfg.get("key_secret_env", "FN_KEY_SECRET"), "")
        return "Basic " + base64.b64encode(f"{kid}:{sec}".encode()).decode()

    def _http(self, method, path, body=None, timeout=60):
        req = urllib.request.Request(
            self.cfg["apihost"].rstrip("/") + path, method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json", "Authorization": self._auth()})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw)
            except ValueError:
                return e.code, {"raw": raw[:300].decode(errors="replace")}

    def deliver(self, ev, next_due_in_s):
        ns, action = self.cfg["namespace"], self.cfg["action"]
        rec = {"sent_wall": _now(), "polls": 0, "invoke": "async"}
        t = time.perf_counter()
        try:
            st, r = self._http(
                "POST", f"/api/v1/namespaces/{ns}/actions/{action}?blocking=false",
                ev, timeout=60)
            rec["http_status"] = st
            aid = r.get("activationId")
            if not aid:
                raise RuntimeError(f"invoke returned {st}: {str(r)[:200]}")
            rec["activation_id"] = aid
            deadline = time.time() + self.DEADLINE_S
            act, i = None, 0
            while time.time() < deadline:
                time.sleep(self.BACKOFF[i] if i < len(self.BACKOFF) else self.POLL_MAX_S)
                i += 1
                rec["polls"] += 1
                try:
                    st, got = self._http("GET", f"/api/v1/namespaces/{ns}/activations/{aid}")
                except OSError as e:          # transient network error: keep polling
                    rec["poll_errors"] = rec.get("poll_errors", 0) + 1
                    rec["last_poll_error"] = str(e)[:200]
                    continue
                if st == 200 and "response" in got:
                    act = got
                    break
            if act is None:
                raise TimeoutError(f"activation {aid} not found after {self.DEADLINE_S}s")
            rec["result_seen_s"] = round(time.perf_counter() - t, 4)
            resp = act.get("response") or {}
            result = (resp.get("result") or {}).get("body", resp.get("result"))
            ann = {a["key"]: a["value"] for a in act.get("annotations", [])
                   if isinstance(a, dict) and "key" in a}
            rec.update(
                result=result, error=None if resp.get("success") else
                f"activation status {resp.get('status')}",
                fn_duration_ms=act.get("duration"),
                fn_start_ms=act.get("start"), fn_end_ms=act.get("end"),
                fn_wait_ms=ann.get("waitTime"), fn_init_ms=ann.get("initTime"),
                fn_limits=ann.get("limits"))
        except Exception as e:  # noqa: BLE001
            rec.update(result=None, error=f"{type(e).__name__}: {e}")
        rec["driver_total_s"] = round(time.perf_counter() - t, 4)
        return rec


# ---------------------------------------------------------------- arm (c)
class ManagedAgentsTarget(Target):
    """cfg: {session: dc-c-2pct, install_dir: optional, pause_policy:
             explicit|auto, min_gap_to_pause_s: 60, wait_s: 200}

    explicit: pause right after each event unless the next one is due within
              min_gap_to_pause_s (the cost-optimal use of pause/resume).
    auto:     never pause; rely on the platform idle auto-pause (idle_timeout).
    """
    kind = "managed_agents"
    POLL_S = 5.0

    def __init__(self, name, cfg):
        super().__init__(name, cfg)
        self.hr = HarnessRuntime()
        self.sid = None
        self.dir = cfg.get("install_dir")

    def describe(self):
        return {"kind": self.kind, "session": self.cfg["session"],
                "session_id": self.sid,
                "pause_policy": self.cfg.get("pause_policy", "explicit")}

    def _ensure_ids(self):
        if not self.sid:
            s = self.hr.find_by_name(self.cfg["session"])
            if not s:
                raise HRError(404, f"no session {self.cfg['session']}")
            self.sid = s["session_id"]
        if not self.dir:
            r = self.hr.exec(self.sid, ["python3", "-c",
                             "import os;print(os.path.expanduser('~')+'/dcagent')"],
                             timeout_s=30)
            self.dir = r["stdout"].strip()

    def deliver(self, ev, next_due_in_s):
        rec = {"sent_wall": _now(), "polls": 0}
        t = time.perf_counter()
        try:
            if not self.sid:
                s = self.hr.find_by_name(self.cfg["session"])
                if not s:
                    raise HRError(404, f"no session {self.cfg['session']}")
                self.sid = s["session_id"]
            t1 = time.perf_counter()
            sess = self.hr.get(self.sid)
            rec["status_before"] = sess.get("status")
            rec["pause_reason"] = sess.get("pause_reason")
            rec["status_s"] = round(time.perf_counter() - t1, 4)
            if rec["status_before"] in ("SESSION_STATUS_DESTROYED", "SESSION_STATUS_FAILED"):
                raise HRError(410, f"session_lost: {rec['status_before']}")
            if rec["status_before"] == "SESSION_STATUS_PAUSED":
                t1 = time.perf_counter()
                rec["resume_start_wall"] = _now()
                self.hr.resume(self.sid)
                rec["resume_s"] = round(time.perf_counter() - t1, 4)
            self._ensure_ids()
            wait = float(self.cfg.get("wait_s", 200))
            t1 = time.perf_counter()
            r = self.hr.exec(self.sid, ["python3", f"{self.dir}/core/deliver.py",
                                        "submit", str(wait)],
                             stdin=json.dumps(ev).encode(), timeout_s=int(wait) + 30)
            rec["first_exec_s"] = round(time.perf_counter() - t1, 4)
            out = json.loads(r.get("stdout") or "{}")
            while out.get("pending"):
                time.sleep(self.POLL_S)
                rec["polls"] += 1
                r = self.hr.exec(self.sid, ["python3", f"{self.dir}/core/deliver.py",
                                            "result", ev["id"]], timeout_s=60)
                out = json.loads(r.get("stdout") or "{}")
            rec["result"], rec["error"] = out, None
            if out.get("error") == "daemon_unreachable":
                rec["error"] = "daemon_unreachable"
        except Exception as e:  # noqa: BLE001
            rec.update(result=None, error=f"{type(e).__name__}: {e}")
        rec["driver_total_s"] = round(time.perf_counter() - t, 4)
        # Pause decision is outside the event's latency but inside its cost.
        policy = self.cfg.get("pause_policy", "explicit")
        if (policy == "explicit" and self.sid and rec.get("error") is None
                and next_due_in_s > float(self.cfg.get("min_gap_to_pause_s", 60))):
            t1 = time.perf_counter()
            try:
                self.hr.pause(self.sid)
                rec["paused"] = True
            except Exception as e:  # noqa: BLE001
                rec["pause_error"] = str(e)
            rec["pause_s"] = round(time.perf_counter() - t1, 4)
        rec["awake_end_wall"] = _now()
        return rec


KINDS = {"droplet": DropletTarget, "functions": FunctionsTarget,
         "managed_agents": ManagedAgentsTarget}


def build_targets(cfg):
    return {name: KINDS[c["kind"]](name, c) for name, c in cfg.items()}
