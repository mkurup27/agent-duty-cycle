"""Generate event schedules (JSONL, one event per line, sorted by offset_s).

  # Experiment 1: one-week main run at a target duty cycle
  python3 -m driver.schedule main --profile 2pct  --target a-2pct --out sched/a-2pct.jsonl
  python3 -m driver.schedule main --profile 30pct --target b-30pct --out ...

  # Experiment 3: wake-latency probes at a fixed idle gap
  python3 -m driver.schedule probe --gap 3600 --count 168 --target c-g1h --out ...

  # Experiments 4/5: state size sweep
  python3 -m driver.schedule state --sizes 10240,1048576,10485760,104857600 \
      --wakes 50 --gap 120 --target b-state --out ...

The SAME --seed gives byte-identical event content for every target, so
all three arms receive identical work. Only "target" and "id" differ.

Duty cycle = sum of estimated event work time / wall time, where
  work = cpu_s + io_wait_s + model_calls * --model-latency
The generator rescales io_wait_s so the estimate hits the profile target.
The *achieved* duty cycle is measured from the logs, not assumed.
"""

import argparse
import json
import math
import random
import sys

SERVICES = ["api", "auth", "billing", "search", "db-primary", "queue", "cdn", "web"]
SEVERITIES = ["low", "low", "low", "medium", "medium", "high", "critical"]
SUMMARIES = ["p99 latency over SLO", "5xx rate elevated", "disk 92% full",
             "replication lag 40s", "cert expires in 3 days", "OOM kills in pod",
             "queue depth growing", "error budget burn 4x"]

# Per-event work templates. io_wait_s is the knob the generator rescales.
PROFILES = {
    "2pct": {"target": 0.02, "tick_every_s": 3600, "incidents_per_day": 4,
             "tick": {"cpu_s": 5, "io_wait_s": 10, "model_calls": 1},
             "incident": {"cpu_s": 20, "io_wait_s": 120, "model_calls": 2},
             "approval": {"cpu_s": 10, "io_wait_s": 150, "model_calls": 1}},
    "30pct": {"target": 0.30, "tick_every_s": 600, "incidents_per_day": 24,
              "tick": {"cpu_s": 30, "io_wait_s": 60, "model_calls": 2},
              "incident": {"cpu_s": 60, "io_wait_s": 240, "model_calls": 3},
              "approval": {"cpu_s": 30, "io_wait_s": 150, "model_calls": 1}},
}
MAX_EVENT_S = 600   # stays well inside the 15-min Functions limit


def est(work, model_latency):
    return work["cpu_s"] + work["io_wait_s"] + work["model_calls"] * model_latency


def gen_main(a):
    p = PROFILES[a.profile]
    rng = random.Random(a.seed)
    wall = a.days * 86400
    evs = []
    # periodic feed checks
    t = rng.uniform(0, p["tick_every_s"])
    while t < wall:
        feed = [{"service": rng.choice(SERVICES), "severity": rng.choice(SEVERITIES),
                 "msg": rng.choice(SUMMARIES)} for _ in range(rng.randint(0, 6))]
        evs.append({"offset_s": t, "type": "tick", "feed": feed,
                    "work": dict(p["tick"])})
        t += p["tick_every_s"]
    # webhook incidents (Poisson) and their human approvals (lognormal delay)
    rate = p["incidents_per_day"] / 86400
    t, n = rng.expovariate(rate), 0
    while t < wall:
        n += 1
        iid = f"inc-{n:04d}"
        evs.append({"offset_s": t, "type": "incident", "work": dict(p["incident"]),
                    "incident": {"id": iid, "service": rng.choice(SERVICES),
                                 "severity": rng.choice(SEVERITIES[3:]),
                                 "summary": rng.choice(SUMMARIES)}})
        delay = min(max(rng.lognormvariate(math.log(2700), 1.0), 300), 8 * 3600)
        delay += est(p["incident"], a.model_latency) + 60  # after the plan exists
        if t + delay < wall:
            evs.append({"offset_s": t + delay, "type": "approval", "incident_id": iid,
                        "decision": "approve" if rng.random() < 0.85 else "reject",
                        "work": dict(p["approval"])})
        t += rng.expovariate(rate)
    # rescale io_wait so estimated duty cycle == target
    fixed = sum(e["work"]["cpu_s"] + e["work"]["model_calls"] * a.model_latency for e in evs)
    io = sum(e["work"]["io_wait_s"] for e in evs)
    k = max(0.0, (p["target"] * wall - fixed) / io) if io else 0
    for e in evs:
        w = e["work"]
        w["io_wait_s"] = round(w["io_wait_s"] * k, 2)
        over = est(w, a.model_latency) - MAX_EVENT_S
        if over > 0:
            w["io_wait_s"] = round(max(0, w["io_wait_s"] - over), 2)
    return evs, {"profile": a.profile, "target_duty": p["target"], "io_scale": round(k, 4)}


def gen_probe(a):
    rng = random.Random(a.seed)
    evs, t = [], a.gap
    for _ in range(a.count):
        evs.append({"offset_s": t, "type": "probe", "work": {"model_calls": 1}})
        t += a.gap * rng.uniform(1 - a.jitter, 1 + a.jitter)
    return evs, {"gap_s": a.gap, "count": a.count}


def gen_state(a):
    evs, t = [], 0.0
    for size in [int(s) for s in a.sizes.split(",")]:
        evs.append({"offset_s": t, "type": "seed", "state_bytes": size,
                    "history_msgs": a.history, "work": {"model_calls": 0}})
        t += a.seed_gap
        for _ in range(a.wakes):
            evs.append({"offset_s": t, "type": "probe", "state_bytes_expected": size,
                        "work": {"model_calls": 1}})
            t += a.gap
    return evs, {"sizes": a.sizes, "wakes": a.wakes, "gap_s": a.gap}


def main(argv=None):
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="kind", required=True)
    for k in ("main", "probe", "state"):
        s = sub.add_parser(k)
        s.add_argument("--target", required=True)
        s.add_argument("--out", required=True)
        s.add_argument("--seed", type=int, default=7)
        s.add_argument("--model-latency", type=float, default=2.5)
        s.add_argument("--start-offset", type=float, default=0,
                       help="shift all events (stagger targets)")
    m = sub.choices["main"]
    m.add_argument("--profile", choices=sorted(PROFILES), required=True)
    m.add_argument("--days", type=float, default=7)
    p = sub.choices["probe"]
    p.add_argument("--gap", type=float, required=True)
    p.add_argument("--count", type=int, required=True)
    p.add_argument("--jitter", type=float, default=0.05)
    s = sub.choices["state"]
    s.add_argument("--sizes", default="10240,1048576,10485760,104857600")
    s.add_argument("--wakes", type=int, default=50)
    s.add_argument("--gap", type=float, default=120)
    s.add_argument("--seed-gap", type=float, default=300)
    s.add_argument("--history", type=int, default=20)
    a = ap.parse_args(argv)

    evs, meta = {"main": gen_main, "probe": gen_probe, "state": gen_state}[a.kind](a)
    evs.sort(key=lambda e: e["offset_s"])
    for i, e in enumerate(evs):
        e["offset_s"] = round(e["offset_s"] + a.start_offset, 3)
        e["target"] = a.target
        e["id"] = f"{a.target}-{i:05d}-{e['type']}"
    span = (evs[-1]["offset_s"] - evs[0]["offset_s"]) if evs else 0
    work = sum(est(e["work"] | {"cpu_s": e["work"].get("cpu_s", 0),
                                "io_wait_s": e["work"].get("io_wait_s", 0)},
                   a.model_latency) for e in evs)
    wall = a.days * 86400 if a.kind == "main" else max(span, 1)
    meta.update(events=len(evs), span_s=round(span), est_work_s=round(work),
                est_duty=round(work / wall, 4),
                by_type={t: sum(1 for e in evs if e["type"] == t)
                         for t in sorted({e["type"] for e in evs})})
    with open(a.out, "w") as f:
        for e in evs:
            f.write(json.dumps(e) + "\n")
    print(json.dumps(meta), file=sys.stderr)
    return meta


if __name__ == "__main__":
    main()
