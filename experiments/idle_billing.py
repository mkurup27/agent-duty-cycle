"""Experiment 2: what does a Harness Runtime session cost while idle?

Runs four windows back to back, separated by quiet gaps so each window is a
distinct block in the hourly usage/billing export:

  W1 active        keep_warm session, one vCPU burned at 100% for --hours
  W2 awake-idle    same session, nothing running, for --hours
                   (keep_warm is the only way to stay awake but idle: plain
                   sessions auto-pause after 15 min, and keep_warm sessions
                   reject pause, so W1/W2 use their own session)
  W3 paused        normal session, paused right after boot, for --hours
  W4 auto-pause    normal session left idle; poll until the platform pauses
                   it and record how long that took and the pause_reason

Writes a JSON report with exact UTC boundaries plus in-sandbox CPU counters
(/proc/stat) so each window's billed CPU and memory lines can be matched to
the invoice. The question the bill answers: in W2 and W3, which of CPU,
memory and storage keep accruing?

  python3 -m experiments.idle_billing --hours 6 --gap-hours 1 --out runs/exp2.json
  python3 -m experiments.idle_billing --hours 0.01 --gap-hours 0 --idle-timeout 5s  # rehearsal
"""

import argparse
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "arms/managed_agents"))
import bootstrap  # noqa: E402
from driver.hr_api import HarnessRuntime  # noqa: E402

PROC = ("import json,time;l=open('/proc/stat').readline().split();"
        "m={k:v for k,v in (x.split(':') for x in open('/proc/meminfo'))};"
        "print(json.dumps({'t':time.time(),'cpu_jiffies':[int(x) for x in l[1:8]],"
        "'mem_total_kb':m['MemTotal'].split()[0],'mem_avail_kb':m['MemAvailable'].split()[0]}))")
# The burner must not inherit the exec call's stdout/stderr: the exec API
# returns only when every holder of those pipes has closed them, so an
# inherited pipe makes the exec block for the whole burn and time out (B-005).
BURN = ("import subprocess,sys;D=subprocess.DEVNULL;subprocess.Popen([sys.executable,'-c',"
        "'import time\\nend=time.time()+%f\\nwhile time.time()<end: pass'],"
        "start_new_session=True,stdin=D,stdout=D,stderr=D);print('burning')")


def utc(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def sample(hr, sid):
    r = hr.exec(sid, ["python3", "-c", PROC], timeout_s=30)
    try:
        return json.loads(r.get("stdout") or "{}")
    except ValueError:
        return {"error": r}


def mk(name, keep_warm, a):
    return SimpleNamespace(name=name, mode="host", adapter=a.adapter, image=None,
                           size=a.size, idle_timeout=a.idle_timeout, keep_warm=keep_warm)


CREATED = []   # sessions still alive; deleted on exit, even after an error


def create(hr, ns):
    s = hr.create_from_yaml(bootstrap.spec_yaml(ns))
    CREATED.append(s["session_id"])
    hr.wait_status(s["session_id"])
    return s["session_id"]


def destroy(hr, sid):
    hr.delete(sid)
    if sid in CREATED:
        CREATED.remove(sid)


def main(argv=None):
    a = parse(argv)
    hr = HarnessRuntime()
    try:
        run(hr, a)
    finally:
        if not a.keep:
            for sid in list(CREATED):
                try:
                    destroy(hr, sid)
                    print(f"cleanup: destroyed {sid}", file=sys.stderr)
                except Exception as e:  # noqa: BLE001
                    print(f"cleanup FAILED for {sid}: {e} - delete it by hand",
                          file=sys.stderr)


def parse(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=6)
    ap.add_argument("--gap-hours", type=float, default=1)
    ap.add_argument("--size", default="mars-2vcpu-4gb")
    ap.add_argument("--adapter", default="opencode")
    ap.add_argument("--idle-timeout", default="15m")
    ap.add_argument("--prefix", default="dc-exp2")
    ap.add_argument("--out", required=True)
    ap.add_argument("--keep", action="store_true", help="don't destroy sessions")
    return ap.parse_args(argv)


def run(hr, a):
    W, gap = a.hours * 3600, a.gap_hours * 3600
    rep = {"size": a.size, "hours_per_window": a.hours, "windows": {}}

    def save():
        rep["api_retries"] = hr.retries
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(json.dumps(rep, indent=2))

    # W1 + W2 on a keep_warm session
    sid = create(hr, mk(f"{a.prefix}-warm", True, a))
    rep["warm_session_id"] = sid
    s0 = sample(hr, sid)
    hr.exec(sid, ["python3", "-c", BURN % W], timeout_s=30)
    t0 = time.time()
    time.sleep(W)
    s1 = sample(hr, sid)
    rep["windows"]["W1_active"] = {"start": utc(t0), "end": utc(time.time()),
                                   "proc_start": s0, "proc_end": s1}
    save()
    t0 = time.time()
    time.sleep(W)
    s2 = sample(hr, sid)
    rep["windows"]["W2_awake_idle"] = {"start": utc(t0), "end": utc(time.time()),
                                       "proc_start": s1, "proc_end": s2,
                                       "status_end": hr.status(sid)}
    save()
    if not a.keep:
        destroy(hr, sid)
    time.sleep(gap)

    # W3 paused
    sid = create(hr, mk(f"{a.prefix}-paused", False, a))
    rep["paused_session_id"] = sid
    hr.pause(sid)
    hr.wait_status(sid, ("SESSION_STATUS_PAUSED",), timeout=300, every=2)
    t0 = time.time()
    time.sleep(W)
    sess = hr.get(sid)
    rep["windows"]["W3_paused"] = {"start": utc(t0), "end": utc(time.time()),
                                   "status_end": sess.get("status"),
                                   "pause_reason": sess.get("pause_reason")}
    save()
    t = time.perf_counter()
    hr.resume(sid)
    rep["windows"]["W3_paused"]["resume_after_window_s"] = round(time.perf_counter() - t, 3)
    if not a.keep:
        destroy(hr, sid)
    time.sleep(gap)

    # W4 auto-pause observation
    sid = create(hr, mk(f"{a.prefix}-idle", False, a))
    t0 = time.time()
    polls = []
    while time.time() - t0 < 3 * 3600:
        s = hr.get(sid)
        polls.append((round(time.time() - t0, 1), s.get("status")))
        if s.get("status") == "SESSION_STATUS_PAUSED":
            rep["windows"]["W4_auto_pause"] = {
                "start": utc(t0), "paused_after_s": round(time.time() - t0, 1),
                "pause_reason": s.get("pause_reason"), "configured": a.idle_timeout}
            break
        time.sleep(min(30, max(1, W / 20)))
    else:
        rep["windows"]["W4_auto_pause"] = {"start": utc(t0), "never_paused": True}
    rep["windows"]["W4_auto_pause"]["status_polls"] = polls[-5:]
    if not a.keep:
        destroy(hr, sid)
    save()
    print(json.dumps(rep, indent=2))


if __name__ == "__main__":
    main()
