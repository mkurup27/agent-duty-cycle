"""Generate every schedule for the full experiment plan into one directory.

  python3 -m experiments.make_schedules --out sched/
  python3 -m experiments.make_schedules --out sched/ --only main,probe

Target names it emits (targets.json must define each one you run):
  Exp 1  {a,b,c}-2pct  {a,b,c}-30pct           identical event content per profile
  Exp 3  {a,b,c}-g1m  -g15m  -g1h  -g6h  -g24h  probes at a fixed idle gap
  Exp 4  b-state                                 Functions, sizes 10 KB..100 MB
  Exp 5  c-state                                 Managed Agents, same sizes
"""

import argparse
import json
from pathlib import Path

from driver import schedule

BUCKETS = {"g1m": (60, 200), "g15m": (900, 200), "g1h": (3600, 168),
           "g6h": (21600, 28), "g24h": (86400, 7)}
SIZES = "10240,1048576,10485760,104857600"


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--only", default="main,probe,state")
    ap.add_argument("--arms", default="a,b,c")
    ap.add_argument("--days", type=float, default=7)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--model-latency", type=float, default=4.5,
                    help="measured seconds per model call (llama-4-maverick: ~4.4 s)")
    a = ap.parse_args(argv)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    only, arms = set(a.only.split(",")), a.arms.split(",")
    meta = {}

    def gen(args, name):
        meta[name] = schedule.main(args + ["--out", str(out / f"{name}.jsonl"),
                                           "--seed", str(a.seed),
                                           "--model-latency", str(a.model_latency)])

    if "main" in only:
        for arm in arms:
            for p in ("2pct", "30pct"):
                gen(["main", "--profile", p, "--days", str(a.days),
                     "--target", f"{arm}-{p}"], f"{arm}-{p}")
    if "probe" in only:
        for arm in arms:
            for b, (gap, n) in BUCKETS.items():
                gen(["probe", "--gap", str(gap), "--count", str(n),
                     "--target", f"{arm}-{b}"], f"{arm}-{b}")
    if "state" in only:
        for arm in ("b", "c"):
            if arm in arms:
                gen(["state", "--sizes", SIZES, "--wakes", "50", "--gap", "120",
                     "--target", f"{arm}-state"], f"{arm}-state")
    (out / "plan.json").write_text(json.dumps(meta, indent=2))
    print(json.dumps({k: {"events": v["events"], "span_h": round(v["span_s"] / 3600, 1)}
                      for k, v in meta.items()}, indent=1))


if __name__ == "__main__":
    main()
