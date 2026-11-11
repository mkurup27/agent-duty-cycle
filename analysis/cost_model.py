"""List-price cost model for the three arms (harness compute only).

Rates are DO public list prices, copied from the pages below and dated.
Re-verify before publishing; update RATES and VERIFIED together.

  python3 -m analysis.cost_model curves --out curves.csv     # Section 2 chart data
  python3 -m analysis.cost_model point --duty 0.02           # one duty cycle

Model tokens are excluded on purpose: they are billed identically under all
three arms (the article's boundary with the inference-cost pieces). The
analyzer reports measured token totals separately so that claim is checked,
not assumed.
"""

import argparse
import csv
import json
import sys

VERIFIED = "2026-11-09"
SOURCES = {
    "harness_runtime": "https://docs.digitalocean.com/products/managed-agents/agent-harness-runtime/details/pricing/",
    "functions": "https://docs.digitalocean.com/products/functions/details/pricing/",
    "droplets_v5": "https://docs.digitalocean.com/products/droplets/details/pricing/",
    "spaces": "https://docs.digitalocean.com/products/spaces/details/pricing/",
    "postgres": "https://docs.digitalocean.com/products/databases/postgresql/details/pricing/",
}
RATES = {
    # Harness Runtime (public preview pricing)
    "hr_cpu_per_vcpu_h": 0.044,        # actual CPU consumed
    "hr_mem_per_gb_h": 0.0095,         # memory allocated over time (awake)
    "hr_storage_per_gib_mo": 0.05,     # session storage volumes
    "hr_snapshot_per_gib_mo": 0.05,    # snapshots/checkpoints, accrues while paused
    # Functions
    "fn_per_gib_s": 0.0000185,
    "fn_free_gib_s_mo": 90000,         # team-wide monthly allowance
    "fn_min_ms": 100,
    # Droplets, v5 per-resource rates (billed per second, no monthly cap)
    "dr_shared_vcpu_h": 0.0158219,
    "dr_mem_gib_h": 0.0040411,
    "dr_disk_gib_h": 0.000137,
    # State backends for the stateless arm (fixed monthly)
    "spaces_base_mo": 5.00,            # includes 250 GiB + 1 TiB egress
    "pg_single_node_mo": 15.00,        # smallest single-node cluster
    "pg_storage_gib": 10,              # storage billed as its own line (seen on the bill)
    "pg_storage_per_gib_mo": 0.215,    # Standard Edition storage
}
HOURS_PER_MONTH = 730


def droplet_month(vcpu=2, mem_gib=4, disk_gib=30, hours=HOURS_PER_MONTH, r=RATES):
    return hours * (vcpu * r["dr_shared_vcpu_h"] + mem_gib * r["dr_mem_gib_h"]
                    + disk_gib * r["dr_disk_gib_h"])


def state_backends_month(r=RATES):
    """Postgres node + its storage + Spaces: billed whether or not the agent runs."""
    return (r["pg_single_node_mo"] + r["pg_storage_gib"] * r["pg_storage_per_gib_mo"]
            + r["spaces_base_mo"])


def functions_month(busy_s, mem_gib=0.5, include_free=False,
                    include_backends=True, r=RATES):
    """busy_s: billed execution seconds per month (each invocation already
    rounded up to the 100 ms minimum by the caller)."""
    gib_s = mem_gib * busy_s
    if include_free:
        gib_s = max(0.0, gib_s - r["fn_free_gib_s_mo"])
    cost = gib_s * r["fn_per_gib_s"]
    if include_backends:
        cost += state_backends_month(r)
    return cost


def harness_month(awake_h, cpu_util=0.3, vcpu=2, mem_gb=4, storage_gib=1.0,
                  snapshot_gib=0.0, r=RATES):
    """awake_h: hours not paused. cpu_util: mean fraction of vCPUs busy while awake."""
    return (awake_h * vcpu * cpu_util * r["hr_cpu_per_vcpu_h"]
            + awake_h * mem_gb * r["hr_mem_per_gb_h"]
            + storage_gib * r["hr_storage_per_gib_mo"]
            + snapshot_gib * r["hr_snapshot_per_gib_mo"])


def point(duty, resume_overhead_s=1.0, wakes_per_month=1000, fn_mem_gib=0.5,
          fn_rehydrate_s=0.5, cpu_util=0.3, storage_gib=1.0, include_free=False):
    busy_h = duty * HOURS_PER_MONTH
    ma_awake_h = min(HOURS_PER_MONTH, busy_h + wakes_per_month * resume_overhead_s / 3600)
    fn_busy_s = busy_h * 3600 + wakes_per_month * fn_rehydrate_s
    return {
        "duty": duty,
        "always_on": round(droplet_month(), 2),
        "stateless": round(functions_month(fn_busy_s, fn_mem_gib,
                                           include_free=include_free), 2),
        "pause_resume": round(harness_month(ma_awake_h, cpu_util,
                                            storage_gib=storage_gib), 2),
    }


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["curves", "point", "rates"])
    ap.add_argument("--duty", type=float, default=0.02)
    ap.add_argument("--out")
    ap.add_argument("--wakes", type=int, default=1000)
    ap.add_argument("--cpu-util", type=float, default=0.3)
    a = ap.parse_args(argv)
    if a.cmd == "rates":
        print(json.dumps({"verified": VERIFIED, "rates": RATES, "sources": SOURCES},
                         indent=2))
    elif a.cmd == "point":
        print(json.dumps(point(a.duty, wakes_per_month=a.wakes, cpu_util=a.cpu_util)))
    else:
        rows = [point(d / 1000, wakes_per_month=a.wakes, cpu_util=a.cpu_util)
                for d in range(0, 1001, 5)]
        out = open(a.out, "w", newline="") if a.out else sys.stdout
        w = csv.DictWriter(out, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
        # crossovers
        def cross(key):
            for r0, r1 in zip(rows, rows[1:]):
                if (r0[key] - r0["always_on"]) * (r1[key] - r1["always_on"]) <= 0 \
                        and r0[key] != r0["always_on"]:
                    return r1["duty"]
            return None
        print(json.dumps({"crossover_vs_always_on": {
            "stateless": cross("stateless"), "pause_resume": cross("pause_resume")},
            "assumptions": {"wakes_per_month": a.wakes, "cpu_util": a.cpu_util,
                            "rates_verified": VERIFIED}}), file=sys.stderr)


if __name__ == "__main__":
    main()
