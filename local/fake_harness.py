"""Local stand-in for the DO Harness Runtime API, for tests only.

A "sandbox" is a temp HOME directory; every process started through exec
carries FAKE_SANDBOX=<id> in its environment. pause = SIGSTOP every such
process (a real freeze: memory, sockets and child processes are kept),
resume = SIGCONT. Idle sessions auto-pause after idle_timeout with
pause_reason "idle", like the real service.

Implements: create (YAML), list?name=, get, pause, resume, exec, delete.
Only parses the flat YAML subset bootstrap.py writes.
"""

import argparse
import base64
import json
import os
import re
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SESS = {}
LOCK = threading.Lock()
CFG = {"resume_delay": 0.2, "boot_delay": 0.3}


def parse_yaml_subset(text):
    out, cur = {}, None
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = re.match(r"^(\s*)([A-Za-z_][\w-]*):\s*(.*)$", line)
        if not m:
            continue
        ind, k, v = m.groups()
        val = json.loads(v) if v.startswith(("\"", "[", "{")) else (v or None)
        if ind:
            out[cur][k] = val
        elif val is None:
            cur = k
            out[k] = {}
        else:
            out[k] = val
    return out


def dur(s):
    m = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", s or "15m")
    h, mi, se = (int(x or 0) for x in m.groups())
    return h * 3600 + mi * 60 + se


def sandbox_pids(s):
    """PIDs belonging to a fake sandbox.

    Prefer `$HOME/.fake_pids` (written by start_daemon.py). On Linux also
    scan /proc for FAKE_SANDBOX=<sid> so short-lived exec children are covered.
    """
    found = set()
    pid_path = os.path.join(s["home"], ".fake_pids")
    try:
        with open(pid_path) as f:
            for line in f:
                line = line.strip()
                if line.isdigit():
                    found.add(int(line))
    except OSError:
        pass
    sid = s["session_id"]
    tag = f"FAKE_SANDBOX={sid}".encode()
    if os.path.isdir("/proc"):
        for p in os.listdir("/proc"):
            if not p.isdigit():
                continue
            try:
                with open(f"/proc/{p}/environ", "rb") as f:
                    if tag in f.read():
                        found.add(int(p))
            except OSError:
                pass
    # Drop dead pids so pause/resume stay cheap.
    live = set()
    for pid in found:
        try:
            os.kill(pid, 0)
            live.add(pid)
        except OSError:
            pass
    try:
        with open(pid_path, "w") as f:
            f.write("".join(f"{p}\n" for p in sorted(live)))
    except OSError:
        pass
    return sorted(live)


def signal_all(s, sig):
    for pid in sandbox_pids(s):
        try:
            os.kill(pid, sig)
        except OSError:
            pass


def do_pause(s, reason):
    signal_all(s, signal.SIGSTOP)
    s["status"], s["pause_reason"] = "SESSION_STATUS_PAUSED", reason


def idle_watch():
    while True:
        time.sleep(0.25)
        with LOCK:
            for s in list(SESS.values()):
                if (s["status"] == "SESSION_STATUS_READY" and not s["busy"] and
                        time.time() - s["last_activity"] > s["idle_timeout"]):
                    try:
                        do_pause(s, "idle")
                    except OSError:
                        pass


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, obj=None):
        b = json.dumps(obj).encode() if obj is not None else b""
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _body(self):
        n = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(n)

    def _sess(self, sid):
        s = SESS.get(sid)
        if not s:
            self._send(404, {"id": "not_found", "message": "no session"})
        return s

    @staticmethod
    def _public(s):
        return {k: s[k] for k in ("session_id", "name", "status", "pause_reason",
                                  "created_at") if s.get(k) is not None}

    def do_GET(self):
        path, _, qs = self.path.partition("?")
        parts = path.strip("/").split("/")
        if parts == ["v2", "agents", "sessions"]:
            name = dict(x.split("=", 1) for x in qs.split("&") if "=" in x).get("name")
            return self._send(200, {"sessions": [self._public(s) for s in SESS.values()
                                                 if not name or s["name"] == name]})
        s = self._sess(parts[3])
        if s:
            self._send(200, {"session": self._public(s)})

    def do_DELETE(self):
        s = self._sess(self.path.strip("/").split("/")[3])
        if s:
            signal_all(s, signal.SIGCONT)
            signal_all(s, signal.SIGKILL)
            s["status"] = "SESSION_STATUS_DESTROYED"
            self._send(204)

    def do_POST(self):
        parts = self.path.split("?")[0].strip("/").split("/")
        if parts == ["v2", "agents", "sessions"]:
            spec = parse_yaml_subset(self._body().decode())
            sid = str(uuid.uuid4())
            home = tempfile.mkdtemp(prefix="fake-sbx-")
            env = dict(spec.get("env") or {}, **(spec.get("secrets") or {}))
            # Real sandboxes have their own network namespace; locally they
            # share one, so give each fake sandbox its own daemon port.
            env["PORT"] = str(18000 + len(SESS))
            s = {"session_id": sid, "name": spec.get("name") or f"session-{sid[:8]}",
                 "status": "SESSION_STATUS_PROVISIONING", "home": home, "env": env,
                 "idle_timeout": (float("inf") if spec.get("keep_warm") == "true"
                                  else dur(spec.get("idle_timeout"))),
                 "keep_warm": spec.get("keep_warm") == "true", "busy": 0,
                 "last_activity": time.time(), "created_at": time.time(),
                 "pause_reason": None}
            SESS[sid] = s

            def boot():
                time.sleep(CFG["boot_delay"])
                s["status"] = "SESSION_STATUS_READY"
                s["last_activity"] = time.time()
            threading.Thread(target=boot, daemon=True).start()
            return self._send(200, {"session": self._public(s)})
        s = self._sess(parts[3])
        if not s:
            return
        op = parts[4] if len(parts) > 4 else ""
        if op == "pause":
            if s.get("keep_warm"):
                return self._send(409, {"id": "conflict",
                                        "message": "keep_warm sessions cannot pause"})
            with LOCK:
                do_pause(s, "manual")
            return self._send(204)
        if op == "resume":
            time.sleep(CFG["resume_delay"])
            with LOCK:
                signal_all(s, signal.SIGCONT)
                s["status"], s["pause_reason"] = "SESSION_STATUS_READY", None
                s["last_activity"] = time.time()
            return self._send(204)
        if op == "sandbox":
            if s["status"] != "SESSION_STATUS_READY":
                return self._send(409, {"id": "conflict",
                                        "message": f"session is {s['status']}"})
            req = json.loads(self._body())
            stdin = base64.b64decode(req.get("stdin") or "")
            env = dict(os.environ, HOME=s["home"], FAKE_SANDBOX=s["session_id"],
                       **s["env"])
            with LOCK:
                s["busy"] += 1
            t = time.perf_counter()
            try:
                p = subprocess.run(req["argv"], input=stdin, capture_output=True,
                                   env=env, cwd=req.get("workdir") or s["home"],
                                   timeout=min(int(req.get("timeout_seconds", 60)), 240))
                out = {"exit_code": p.returncode,
                       "stdout": p.stdout.decode(errors="replace"),
                       "stderr": p.stderr.decode(errors="replace")}
            except subprocess.TimeoutExpired:
                out = {"exit_code": 124, "stdout": "", "stderr": "timeout"}
            finally:
                with LOCK:
                    s["busy"] -= 1
                    s["last_activity"] = time.time()
            out["duration_ms"] = int((time.perf_counter() - t) * 1000)
            return self._send(200, out)
        self._send(404, {"id": "not_found"})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9300)
    ap.add_argument("--resume-delay", type=float, default=0.2)
    a = ap.parse_args()
    CFG["resume_delay"] = a.resume_delay
    threading.Thread(target=idle_watch, daemon=True).start()
    ThreadingHTTPServer(("127.0.0.1", a.port), H).serve_forever()


if __name__ == "__main__":
    main()
