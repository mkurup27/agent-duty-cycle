"""Ops-triage reference agent.

Event types (all carry "id" and optional "work"):
  tick      periodic feed check. payload: feed=[{service, severity, msg}]
  incident  webhook. payload: incident={id, service, severity, summary}
            -> drafts a fix plan, then WAITS for a human (status awaiting_approval)
  approval  human decision. payload: incident_id, decision approve|reject
            -> on approve, "executes" the plan
  seed      experiment setup: write state_bytes of workspace data, set history
  probe     minimal wake: verify state is intact, one model call by default

work = {cpu_s, io_wait_s, model_calls}
  cpu_s      CPU-seconds burned (process CPU time, so it bills identically
             on every arm that bills by CPU)
  io_wait_s  wall-time waiting on a slow tool call (low CPU)
  model_calls number of chat completions

The work spec comes from the schedule, so every arm does identical work.
"""

import hashlib
import os
import subprocess
import sys
import time
from pathlib import Path

from .state import AgentState
from .timing import PhaseTimer, process_info

MAX_HISTORY = 40          # messages kept in the transcript
MAX_PROCESSED_IDS = 500   # dedupe window

SYSTEM_PROMPT = (
    "You are an on-call ops triage agent. Be terse. For incidents, give a "
    "3-step remediation plan. For feed checks, flag anything that needs a human."
)


def burn_cpu(cpu_s):
    """Burn cpu_s seconds of this process's CPU time."""
    if cpu_s <= 0:
        return 0
    start = time.process_time()
    h = hashlib.sha256()
    buf = b"x" * 4096
    n = 0
    while time.process_time() - start < cpu_s:
        for _ in range(200):
            h.update(buf)
        n += 1
    return n


class OpsTriageAgent:
    def __init__(self, model, workspace, state=None, arm="unknown"):
        self.model = model
        self.ws = Path(workspace)
        self.ws.mkdir(parents=True, exist_ok=True)
        self.state = state or AgentState()
        self.arm = arm
        self.helper = None          # long-running subprocess ("log tailer")
        self.helper_starts = 0

    # -- live resources that cannot be serialized ---------------------------
    def ensure_helper(self):
        """Returns True if the helper had to be (re)started."""
        if self.helper is not None and self.helper.poll() is None:
            return False
        self.helper = subprocess.Popen(
            [sys.executable, "-c", "import time\nwhile True: time.sleep(3600)"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)
        self.helper_starts += 1
        return True

    def liveness(self, helper_restarted):
        return {"helper_pid": self.helper.pid if self.helper else None,
                "helper_restarted": helper_restarted,
                "helper_starts_in_process": self.helper_starts,
                **self.model.liveness()}

    # -- helpers ----------------------------------------------------------
    def _remember(self, role, content):
        self.state.history.append({"role": role, "content": content[:2000]})
        if len(self.state.history) > MAX_HISTORY:
            del self.state.history[: len(self.state.history) - MAX_HISTORY]

    def _ask(self, prompt, timer, calls):
        self._remember("user", prompt)
        timer.mark("first_model_call_start")
        msgs = [{"role": "system", "content": SYSTEM_PROMPT}] + self.state.history
        text, rec = self.model.chat(msgs)
        calls.append(rec)
        self._remember("assistant", text or "(empty)")
        return text

    def _do_work(self, work, timer, calls, prompt):
        work = work or {}
        n_calls = int(work.get("model_calls", 1))
        for i in range(n_calls):
            self._ask(prompt if i == 0 else "Continue. Anything else to check?",
                      timer, calls)
        burn_cpu(float(work.get("cpu_s", 0)))
        io = float(work.get("io_wait_s", 0))
        if io > 0:
            time.sleep(io)

    # -- event handlers -----------------------------------------------------
    def _tick(self, ev, timer, calls):
        feed = ev.get("feed") or []
        self.state.feed_cursor += len(feed)
        flagged = [f for f in feed if f.get("severity") in ("high", "critical")]
        lines = "; ".join(f"{f.get('service')}:{f.get('severity')}:{f.get('msg')}"
                          for f in feed[:10]) or "no new items"
        self._do_work(ev.get("work"), timer, calls,
                      f"Feed check #{self.state.feed_cursor}: {lines}")
        return {"flagged": len(flagged)}

    def _incident(self, ev, timer, calls):
        inc = ev["incident"]
        iid = inc["id"]
        if iid in self.state.incidents:
            return {"incident_id": iid, "duplicate": True}
        self._do_work(ev.get("work"), timer, calls,
                      f"Incident {iid} on {inc.get('service')} "
                      f"({inc.get('severity')}): {inc.get('summary')}. Draft a plan.")
        plan = self.state.history[-1]["content"] if self.state.history else ""
        self.state.incidents[iid] = {
            "status": "awaiting_approval", "service": inc.get("service"),
            "severity": inc.get("severity"), "summary": inc.get("summary"),
            "plan": plan[:1000], "opened_at": time.time(),
        }
        rpt = self.ws / "reports" / f"{iid}.md"
        rpt.parent.mkdir(parents=True, exist_ok=True)
        rpt.write_text(f"# {iid}\n\n{inc.get('summary')}\n\n## Plan\n\n{plan}\n")
        return {"incident_id": iid, "status": "awaiting_approval"}

    def _approval(self, ev, timer, calls):
        iid = ev["incident_id"]
        inc = self.state.incidents.get(iid)
        if inc is None:
            # The approval arrived but the agent has no memory of the incident.
            # On a correct arm this never happens; it is the canonical
            # externalized-state bug.
            raise StateLost(f"unknown incident {iid}")
        if inc["status"] != "awaiting_approval":
            return {"incident_id": iid, "duplicate": True, "status": inc["status"]}
        rpt = self.ws / "reports" / f"{iid}.md"
        if not rpt.exists():
            raise StateLost(f"report file missing for {iid}")
        decision = ev.get("decision", "approve")
        if decision == "approve":
            self._do_work(ev.get("work"), timer, calls,
                          f"Approved: execute the plan for {iid}. Report outcome.")
            inc["status"] = "resolved"
        else:
            self._remember("user", f"Rejected plan for {iid}; close it.")
            burn_cpu(float((ev.get("work") or {}).get("cpu_s", 0)) / 4)
            inc["status"] = "rejected"
        inc["decision"], inc["decided_at"] = decision, time.time()
        with rpt.open("a") as f:
            f.write(f"\n## Decision\n\n{decision} at {time.time():.0f}\n")
        return {"incident_id": iid, "status": inc["status"]}

    def _seed(self, ev, timer, calls):
        n = int(ev.get("state_bytes", 0))
        p = self.ws / "seed" / "blob.bin"
        p.parent.mkdir(parents=True, exist_ok=True)
        h = hashlib.sha256()
        with p.open("wb") as f:
            left = n
            while left > 0:
                chunk = os.urandom(min(left, 1 << 20))
                h.update(chunk)
                f.write(chunk)
                left -= len(chunk)
        self.state.seed = {"bytes": n, "sha256": h.hexdigest()}
        k = int(ev.get("history_msgs", 0))
        self.state.history = [
            {"role": "user" if i % 2 == 0 else "assistant",
             "content": f"Earlier note {i}: " + ("lorem ipsum " * 40)}
            for i in range(min(k, MAX_HISTORY))
        ]
        return {"seeded_bytes": n, "history_msgs": len(self.state.history)}

    def _probe(self, ev, timer, calls):
        seed_ok = None
        if self.state.seed:
            p = self.ws / "seed" / "blob.bin"
            seed_ok = p.exists() and p.stat().st_size == self.state.seed["bytes"]
            if not seed_ok:
                raise StateLost("seed file missing or wrong size")
        work = ev.get("work") or {"model_calls": 1}
        self._do_work(work, timer, calls, "Health probe: reply OK.")
        return {"seed_ok": seed_ok}

    HANDLERS = {"tick": _tick, "incident": _incident, "approval": _approval,
                "seed": _seed, "probe": _probe}

    # -- entry point ----------------------------------------------------------
    def handle(self, ev, timer=None):
        """Process one event. timer may be started earlier by the arm so that
        state loading is inside the measured window."""
        timer = timer or PhaseTimer()
        timer.mark("handler_start")
        pinfo = process_info()
        helper_restarted = self.ensure_helper()
        calls = []
        out = {"event_id": ev.get("id"), "type": ev.get("type"), "arm": self.arm,
               "agent_id": self.state.agent_id}
        try:
            if ev.get("id") and ev["id"] in self.state.processed_ids:
                out.update(ok=True, duplicate_delivery=True)
            else:
                fn = self.HANDLERS[ev["type"]]
                out["result"] = fn(self, ev, timer, calls)
                out["ok"] = True
                st = self.state
                st.events_processed += 1
                st.counts[ev["type"]] = st.counts.get(ev["type"], 0) + 1
                if ev.get("id"):
                    st.processed_ids.append(ev["id"])
                    del st.processed_ids[:-MAX_PROCESSED_IDS]
        except StateLost as e:
            out.update(ok=False, error="state_lost", detail=str(e))
        except Exception as e:  # noqa: BLE001 - report, never crash the arm
            out.update(ok=False, error=type(e).__name__, detail=str(e)[:500])
        timer.mark("work_done")
        out.update(
            events_processed=self.state.events_processed,
            counts=dict(self.state.counts),
            open_incidents=sum(1 for i in self.state.incidents.values()
                               if i["status"] == "awaiting_approval"),
            history_msgs=len(self.state.history),
            model_calls=calls,
            tokens={k: sum(c.get(k, 0) for c in calls) for k in
                    ("input_tokens", "output_tokens", "cached_input_tokens", "reasoning_tokens")},
            process=pinfo,
            liveness=self.liveness(helper_restarted),
        )
        return out


class StateLost(Exception):
    pass
