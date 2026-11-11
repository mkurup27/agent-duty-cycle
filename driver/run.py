"""Replay schedules against targets in real time and log everything.

  python3 -m driver.run --targets targets.json --run-dir runs/week1 \
      sched/a-2pct.jsonl sched/b-2pct.jsonl sched/c-2pct.jsonl ...

  --speed 60         compress wall time 60x (tests only)
  --work-scale 0.01  scale cpu_s / io_wait_s (tests only)
  --resume           continue a crashed run: same start time, skip logged ids

Each target gets one worker thread and processes its events strictly in
order (one at a time), so a busy agent queues work exactly like a
single-worker agent would. Queue delay is logged.

Output: <run-dir>/manifest.json and <run-dir>/<target>.jsonl, one line per
event: due/start/finish wall times, queue delay, the target's delivery
record (latency phases, platform metadata) and the agent's response.
Run the driver on a separate small Droplet in the same region/VPC; its own
cost is not part of any arm.
"""

import argparse
import faulthandler
import hashlib
import json
import os
import platform
import queue
import signal
import sys
import threading
import time
from pathlib import Path

from .targets import build_targets

STOP = threading.Event()


def load_events(paths):
    evs = []
    for p in paths:
        with open(p) as f:
            evs += [json.loads(line) for line in f if line.strip()]
    evs.sort(key=lambda e: e["offset_s"])
    return evs


def scale_work(ev, k):
    if k == 1:
        return ev
    ev = json.loads(json.dumps(ev))
    w = ev.get("work") or {}
    for key in ("cpu_s", "io_wait_s"):
        if key in w:
            w[key] = round(w[key] * k, 4)
    return ev


def code_hash():
    root = Path(__file__).resolve().parents[1]
    h = hashlib.sha256()
    for p in sorted((root / "core").glob("*.py")) + sorted(
            (root / "arms").rglob("*.py")):
        h.update(p.read_bytes())
    return h.hexdigest()[:16]


class Worker(threading.Thread):
    def __init__(self, name, target, run_dir, start_wall, speed, work_scale, due_list):
        super().__init__(daemon=True, name=f"w-{name}")
        self.tname, self.target = name, target
        self.q = queue.Queue()
        self.start_wall, self.speed, self.work_scale = start_wall, speed, work_scale
        self.due = due_list              # sorted offsets for this target
        self.log = open(Path(run_dir) / f"{name}.jsonl", "a", buffering=1)
        self.expected_processed = None   # learned from first response
        self.n_ok = 0

    def next_due_in(self, offset):
        for o in self.due:
            if o > offset:
                return (o - offset) / self.speed
        return float("inf")

    def run(self):
        while True:
            item = self.q.get()
            if item is None:
                return
            ev = item
            due_wall = self.start_wall + ev["offset_s"] / self.speed
            start = time.time()
            payload = {k: v for k, v in scale_work(ev, self.work_scale).items()
                       if k not in ("offset_s", "target")}
            rec = self.target.deliver(payload, self.next_due_in(ev["offset_s"]))
            res = rec.get("result") or {}
            # State-integrity check: the agent's own counter must advance by
            # exactly one per accepted event. A lost update shows up here.
            divergence = None
            if res.get("ok") and not res.get("duplicate_delivery"):
                ep = res.get("events_processed")
                if self.expected_processed is None:
                    self.expected_processed = ep
                else:
                    self.expected_processed += 1
                    if ep != self.expected_processed:
                        divergence = {"expected": self.expected_processed, "got": ep}
                        self.expected_processed = ep
                self.n_ok += 1
            line = {"event_id": ev["id"], "type": ev["type"], "target": self.tname,
                    "kind": self.target.kind, "offset_s": ev["offset_s"],
                    "due_wall": round(due_wall, 3), "start_wall": round(start, 3),
                    "finish_wall": round(time.time(), 3),
                    "queue_delay_s": round(max(0.0, start - due_wall), 3),
                    "work": payload.get("work"), "state_divergence": divergence,
                    "incident_id": (payload.get("incident") or {}).get("id")
                    or payload.get("incident_id"),
                    **rec}
            self.log.write(json.dumps(line, default=str) + "\n")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("schedules", nargs="+")
    ap.add_argument("--targets", required=True)
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--speed", type=float, default=1.0)
    ap.add_argument("--work-scale", type=float, default=1.0)
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--delay-start", type=float, default=5.0)
    a = ap.parse_args(argv)

    run_dir = Path(a.run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    tcfg = json.loads(Path(a.targets).read_text())
    evs = load_events(a.schedules)
    used = sorted({e["target"] for e in evs})
    missing = [t for t in used if t not in tcfg]
    if missing:
        sys.exit(f"targets not in {a.targets}: {missing}")
    targets = build_targets({t: tcfg[t] for t in used})

    man_path = run_dir / "manifest.json"
    done = set()
    if a.resume and man_path.exists():
        man = json.loads(man_path.read_text())
        start_wall = man["start_wall"]
        for t in used:
            p = run_dir / f"{t}.jsonl"
            if p.exists():
                done |= {json.loads(x)["event_id"] for x in p.open() if x.strip()}
        man.setdefault("resumes", []).append(time.time())
    else:
        if man_path.exists():
            sys.exit(f"{man_path} exists; use --resume or a new --run-dir")
        start_wall = time.time() + a.delay_start
        man = {"start_wall": start_wall,
               "start_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(start_wall)),
               "speed": a.speed, "work_scale": a.work_scale,
               "schedules": {p: hashlib.sha256(Path(p).read_bytes()).hexdigest()[:16]
                             for p in a.schedules},
               "events": len(evs), "code_hash": code_hash(),
               "driver_host": platform.node()}
    man["targets"] = {t: targets[t].describe() for t in used}
    man_path.write_text(json.dumps(man, indent=2))

    workers = {}
    for t in used:
        due = sorted(e["offset_s"] for e in evs if e["target"] == t)
        workers[t] = Worker(t, targets[t], run_dir, start_wall, a.speed,
                            a.work_scale, due)
        workers[t].start()

    # `kill -USR1 <pid>` dumps every thread's stack to stderr (stuck-run triage)
    faulthandler.register(signal.SIGUSR1, all_threads=True)

    def stop(*_):
        STOP.set()
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    todo = [e for e in evs if e["id"] not in done]
    print(json.dumps({"run_dir": str(run_dir), "start_utc": man["start_utc"],
                      "events": len(evs), "remaining": len(todo)}), flush=True)
    for ev in todo:
        due = start_wall + ev["offset_s"] / a.speed
        while not STOP.is_set() and time.time() < due:
            STOP.wait(min(1.0, max(0.0, due - time.time())))
        if STOP.is_set():
            break
        workers[ev["target"]].q.put(ev)
    for w in workers.values():
        w.q.put(None)
    for w in workers.values():
        while w.is_alive():
            w.join(timeout=1)
            if STOP.is_set() and w.q.qsize() > 1:
                break   # interrupted: don't drain a long backlog
    man = json.loads(man_path.read_text())
    man["end_wall"] = time.time()
    man["end_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    man["interrupted"] = STOP.is_set()
    man_path.write_text(json.dumps(man, indent=2))
    print(json.dumps({"finished": True, "interrupted": STOP.is_set()}))


if __name__ == "__main__":
    main()
