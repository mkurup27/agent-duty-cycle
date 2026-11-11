"""Summarize a run directory into numbers the article can cite.

  python3 -m analysis.analyze runs/week1 [--fn-mem-gib 0.5] [--idle-timeout 900]

Writes <run>/summary.json, <run>/summary.md and <run>/events.csv.

Per target:
  correctness   ok / errors by kind / state_lost / conflicts / divergences
  duty cycle    busy fraction (agent compute time / wall) and, per arm, the
                fraction it was *billed as awake*:
                  droplet        1.0 (always on)
                  functions      sum(activation duration, 100 ms min) / wall
                  managed_agents union of resume->pause intervals / wall
  latency       p50/p95/p99 of driver round trip, platform overhead
                (round trip minus agent-internal time), and arm-specific
                phases (resume, Functions wait/init, state load/save)
  state size    load/save/overhead by seeded state size (experiments 4/5)
  liveness      how often the helper subprocess / model connection survived
  tokens        measured totals (should match across arms for one schedule)
  cost          list-price estimate from analysis/cost_model.RATES
"""

import argparse
import csv
import json
import math
from collections import defaultdict
from pathlib import Path

from . import cost_model as cm


def pct(xs, p):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    k = (len(xs) - 1) * p / 100
    f, c = math.floor(k), math.ceil(k)
    return round(xs[f] + (xs[c] - xs[f]) * (k - f), 4)


def dist(xs):
    xs = [x for x in xs if x is not None]
    return {"n": len(xs), "p50": pct(xs, 50), "p95": pct(xs, 95), "p99": pct(xs, 99),
            "max": round(max(xs), 4) if xs else None}


def g(d, *path):
    for p in path:
        if not isinstance(d, dict):
            return None
        d = d.get(p)
    return d


def union_len(intervals):
    total, cur_s, cur_e = 0.0, None, None
    for s, e in sorted(intervals):
        if cur_e is None or s > cur_e:
            if cur_e is not None:
                total += cur_e - cur_s
            cur_s, cur_e = s, e
        else:
            cur_e = max(cur_e, e)
    if cur_e is not None:
        total += cur_e - cur_s
    return total


def overhead_of(r, x, kind):
    """Platform overhead for one event = time not spent inside the agent.

    Functions: measured from the activation record (waitTime + initTime +
    the part of the billed duration outside the agent handler), because the
    driver only learns of completion by polling, and the activation record
    appears ~3 s after the function ends (P-004). Using the driver round trip
    would charge that polling lag to the platform's wake latency.
    Other arms: driver round trip minus agent-internal time.
    """
    agent = g(x, "timing", "total_s") or 0
    if kind == "functions" and r.get("fn_duration_ms") is not None:
        return ((r.get("fn_wait_ms") or 0) + (r.get("fn_init_ms") or 0)) / 1000 \
            + max(0.0, r["fn_duration_ms"] / 1000 - agent)
    return r["driver_total_s"] - agent


def analyze_target(name, rows, man, a):
    kind = rows[0]["kind"]
    res = [r.get("result") or {} for r in rows]
    start, end = man["start_wall"], man.get("end_wall") or max(r["finish_wall"] for r in rows)
    wall = max(1.0, end - start) * man.get("speed", 1.0)   # schedule-time seconds
    real_wall = max(1.0, end - start)
    out = {"kind": kind, "events": len(rows), "wall_s": round(wall)}

    # An approval whose incident event itself failed (e.g. the model call was
    # rejected) finds no incident: that is a cascade of the earlier failure,
    # not lost state, and is counted separately.
    failed_incidents = {r.get("incident_id") for r, x in zip(rows, res)
                        if r["type"] == "incident" and (r.get("error") or not x.get("ok"))}
    errs = defaultdict(int)
    for r, x in zip(rows, res):
        if r.get("error"):
            errs["transport:" + r["error"].split(":")[0]] += 1
        elif not x.get("ok"):
            err_kind = x.get("error", "unknown")
            if err_kind == "state_lost" and r["type"] == "approval" and \
                    r.get("incident_id") in failed_incidents:
                err_kind = "cascade_after_failed_incident"
            errs[err_kind] += 1
    out["correctness"] = {
        "ok": sum(1 for r, x in zip(rows, res) if not r.get("error") and x.get("ok")),
        "errors": dict(errs),
        "duplicates": sum(1 for x in res if x.get("duplicate_delivery")),
        "state_divergences": sum(1 for r in rows if r.get("state_divergence")),
        "approvals_ok": sum(1 for r, x in zip(rows, res)
                            if r["type"] == "approval" and x.get("ok")),
        "approvals_total": sum(1 for r in rows if r["type"] == "approval"),
    }

    agent_s = [g(x, "timing", "total_s") or 0 for x in res]
    busy = sum(agent_s)
    out["duty_cycle"] = {"busy_fraction": round(busy / real_wall, 5)}
    if kind == "droplet":
        out["duty_cycle"]["billed_awake_fraction"] = 1.0
    elif kind == "functions":
        billed = sum(max(cm.RATES["fn_min_ms"], r.get("fn_duration_ms") or 0) / 1000
                     for r in rows if r.get("fn_duration_ms") is not None)
        out["duty_cycle"]["billed_awake_fraction"] = round(billed / real_wall, 5)
        out["functions"] = {
            "billed_s": round(billed, 2), "gib_s": round(billed * a.fn_mem_gib, 2),
            "polls_total": sum(r.get("polls", 0) for r in rows),
            "result_lag_s": dist([(r.get("result_seen_s") or 0) - (r.get("fn_duration_ms") or 0) / 1000
                                  for r in rows if r.get("result_seen_s") is not None]),
            "cold": sum(1 for x in res if g(x, "process", "cold_process")),
            "wait_ms": dist([r.get("fn_wait_ms") for r in rows]),
            "init_ms": dist([r.get("fn_init_ms") for r in rows]),
        }
    elif kind == "managed_agents":
        iv = []
        for i, r in enumerate(rows):
            s = r.get("resume_start_wall") or r.get("sent_wall")
            e = r.get("awake_end_wall") or r["finish_wall"]
            if not r.get("paused"):
                nxt = rows[i + 1]["sent_wall"] if i + 1 < len(rows) else end
                e = min(max(e, nxt), e + a.idle_timeout / man.get("speed", 1.0))
            iv.append((s, e))
        awake = union_len(iv)
        out["duty_cycle"]["billed_awake_fraction"] = round(awake / real_wall, 5)
        out["managed_agents"] = {
            "awake_s": round(awake * man.get("speed", 1.0), 1),
            "resumes": sum(1 for r in rows if r.get("resume_s") is not None),
            "resume_s": dist([r.get("resume_s") for r in rows]),
            "pause_s": dist([r.get("pause_s") for r in rows]),
            "first_exec_s": dist([r.get("first_exec_s") for r in rows]),
            "status_before": dict(_count(r.get("status_before") for r in rows)),
            "pause_reasons": dict(_count(r.get("pause_reason") for r in rows
                                         if r.get("pause_reason"))),
            "polls": sum(r.get("polls", 0) for r in rows),
        }

    overhead = [overhead_of(r, x, kind) for r, x in zip(rows, res) if x]
    out["latency_s"] = {
        "driver_total": dist([r["driver_total_s"] for r in rows]),
        "platform_overhead": dist(overhead),
        "client_observed_overhead": dist([r["driver_total_s"] - (g(x, "timing", "total_s") or 0)
                                          for r, x in zip(rows, res) if x]),
        "queue_delay": dist([r.get("queue_delay_s") for r in rows]),
        "state_loaded_mark": dist([g(x, "timing", "marks_s", "state_loaded") for x in res]),
        "wake_to_first_model_call": dist(
            [overhead_of(r, x, kind)
             + (g(x, "timing", "marks_s", "first_model_call_start") or 0)
             for r, x in zip(rows, res)
             if g(x, "timing", "marks_s", "first_model_call_start") is not None]),
    }
    io_keys = ("db_read_s", "blob_get_s", "extract_s", "deserialize_s", "serialize_s",
               "pack_s", "blob_put_s", "db_write_s", "resources_s")
    if kind == "functions":
        out["state_io_s"] = {k: dist([g(x, "state_io", k) for x in res]) for k in io_keys}

    # state-size sweep: group probes by the seed size in effect
    by_size, size = defaultdict(list), None
    for r, x in zip(rows, res):
        if r["type"] == "seed":
            size = g(x, "result", "seeded_bytes")
        elif r["type"] == "probe" and size is not None:
            by_size[size].append((r, x))
    if by_size:
        out["by_state_size"] = {}
        for sz, items in sorted(by_size.items()):
            e = {"wakes": len(items),
                 "ok": sum(1 for _, x in items if x.get("ok")),
                 "driver_total_s": dist([r["driver_total_s"] for r, _ in items]),
                 "platform_overhead_s": dist([overhead_of(r, x, kind) for r, x in items]),
                 "state_loaded_s": dist([g(x, "timing", "marks_s", "state_loaded")
                                         for _, x in items])}
            if kind == "functions":
                e["blob_get_s"] = dist([g(x, "state_io", "blob_get_s") for _, x in items])
                e["extract_s"] = dist([g(x, "state_io", "extract_s") for _, x in items])
                e["bytes_down"] = dist([g(x, "state_io", "bytes_down") for _, x in items])
            if kind == "managed_agents":
                e["resume_s"] = dist([r.get("resume_s") for r, _ in items])
            out["by_state_size"][str(sz)] = e

    lv = [x.get("liveness") or {} for x in res if x]
    first_calls = [(x.get("model_calls") or [{}])[0] for x in res if x.get("model_calls")]
    calls_all = [c for x in res for c in (x.get("model_calls") or [])]
    out["model_client"] = {
        "calls": len(calls_all),
        "retried_calls": sum(1 for c in calls_all if c.get("retries")),
        "retry_wait_s": round(sum(c.get("retry_wait_s") or 0 for c in calls_all), 1),
        "idle_evictions": sum(1 for c in calls_all if c.get("conn_evicted_idle")),
        "transport_errors": sum(c.get("transport_errors") or 0 for c in calls_all),
        "latency_s": dist([c.get("latency_s") for c in calls_all]),
    }
    out["liveness"] = {
        "helper_restarted_frac": _frac(l.get("helper_restarted") for l in lv),
        "model_conn_reused_frac": _frac(c.get("conn_reused") for c in first_calls),
        "distinct_pids": len({g(x, "process", "pid") for x in res if x.get("process")}),
    }
    out["tokens"] = {k: sum(g(x, "tokens", k) or 0 for x in res)
                     for k in ("input_tokens", "output_tokens", "cached_input_tokens", "reasoning_tokens")}
    out["model_calls"] = sum(len(x.get("model_calls") or []) for x in res)
    out["cpu_s_agent"] = round(sum(g(x, "timing", "cpu_s") or 0 for x in res), 2)

    # list-price estimate, scaled to a 730 h month
    scale = cm.HOURS_PER_MONTH * 3600 / wall
    if kind == "droplet":
        est = cm.droplet_month()
    elif kind == "functions":
        est = cm.functions_month(out["functions"]["billed_s"] * man.get("speed", 1.0) * scale,
                                 a.fn_mem_gib)
    else:
        awake_h = out["managed_agents"]["awake_s"] * scale / 3600
        cpu_util = min(1.0, out["cpu_s_agent"] * man.get("speed", 1.0) /
                       max(1.0, out["managed_agents"]["awake_s"]) / a.ma_vcpu)
        est = cm.harness_month(awake_h, cpu_util, vcpu=a.ma_vcpu, mem_gb=a.ma_mem_gb)
    out["list_price_estimate_usd_per_month"] = round(est, 2)
    return out


def _count(xs):
    d = defaultdict(int)
    for x in xs:
        d[str(x)] += 1
    return d


def _frac(xs):
    xs = [bool(x) for x in xs if x is not None]
    return round(sum(xs) / len(xs), 4) if xs else None


def to_md(summary):
    L = [f"# Run summary: {summary['run_dir']}", "",
         f"Start {summary['manifest'].get('start_utc')}, end {summary['manifest'].get('end_utc')}, "
         f"speed x{summary['manifest'].get('speed')}, code {summary['manifest'].get('code_hash')}", "",
         "| target | kind | events | ok | errors | divergences | busy | billed awake | "
         "p50 overhead s | p95 overhead s | helper restarted | est $/mo |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for t, s in summary["targets"].items():
        c, d, lat = s["correctness"], s["duty_cycle"], s["latency_s"]["platform_overhead"]
        L.append(f"| {t} | {s['kind']} | {s['events']} | {c['ok']} | "
                 f"{sum(c['errors'].values())} | {c['state_divergences']} | "
                 f"{d['busy_fraction']:.2%} | {d.get('billed_awake_fraction', 0):.2%} | "
                 f"{lat['p50']} | {lat['p95']} | {s['liveness']['helper_restarted_frac']} | "
                 f"{s['list_price_estimate_usd_per_month']} |")
    L += ["", "Errors are listed per target in summary.json. 'est $/mo' is a list-price",
          "estimate scaled to 730 h. It is NOT the bill; compare it against the invoice."]
    return "\n".join(L) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir")
    ap.add_argument("--fn-mem-gib", type=float, default=0.5)
    ap.add_argument("--idle-timeout", type=float, default=900)
    ap.add_argument("--ma-vcpu", type=int, default=2)
    ap.add_argument("--ma-mem-gb", type=float, default=4)
    a = ap.parse_args(argv)
    rd = Path(a.run_dir)
    man = json.loads((rd / "manifest.json").read_text())
    summary = {"run_dir": str(rd), "manifest": man, "targets": {}}
    flat = []
    for p in sorted(rd.glob("*.jsonl")):
        rows = [json.loads(x) for x in p.open() if x.strip()]
        if not rows:
            continue
        rows.sort(key=lambda r: r["sent_wall"] if r.get("sent_wall") else r["start_wall"])
        summary["targets"][p.stem] = analyze_target(p.stem, rows, man, a)
        for r in rows:
            x = r.get("result") or {}
            flat.append({"target": p.stem, "event_id": r["event_id"], "type": r["type"],
                         "kind": r["kind"], "ok": bool(x.get("ok")) and not r.get("error"),
                         "error": r.get("error") or x.get("error"),
                         "driver_total_s": r["driver_total_s"],
                         "agent_total_s": g(x, "timing", "total_s"),
                         "queue_delay_s": r.get("queue_delay_s"),
                         "resume_s": r.get("resume_s"), "fn_duration_ms": r.get("fn_duration_ms"),
                         "fn_init_ms": r.get("fn_init_ms"),
                         "input_tokens": g(x, "tokens", "input_tokens"),
                         "output_tokens": g(x, "tokens", "output_tokens")})
    (rd / "summary.json").write_text(json.dumps(summary, indent=2))
    (rd / "summary.md").write_text(to_md(summary))
    if flat:
        with open(rd / "events.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(flat[0]))
            w.writeheader()
            w.writerows(flat)
    print(to_md(summary))
    return summary


if __name__ == "__main__":
    main()
