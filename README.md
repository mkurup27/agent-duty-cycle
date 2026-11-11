# Serverless vs. Persistent Compute for Agents: The Duty-Cycle Math Nobody Shows

**Week-long cost run of one ops-triage agent on Droplet, Functions+Postgres+Spaces, and Harness pause/resume, under identical 2% and 30% schedules.**

- **Agent:** one ops-triage process (`core/agent.py`) with ticks, incidents, approvals and probes; fixed work specs so every arm burns the same CPU and makes the same model calls
- **Arms:** (a) always-on Droplet `s5-2vcpu-4gb-30gb` (RIC1); (b) DO Functions + Managed Postgres + Spaces (NYC1/NYC3); (c) Managed Agents / Harness Runtime session `mars-2vcpu-4gb` (RIC1)
- **Model:** `llama-4-maverick` on Serverless Inference, temperature 0, 256 max tokens (same rates on every arm)
- **Volume:** 6,844 events across 23 targets over one week (29 October – 5 November 2026); 6,842 succeeded
- **Profiles:** 234 events/week at ~2% duty, 1,311 at ~30%; 603 wake probes per arm (1 min–24 h idle); 200 state-sweep wakes per serverless arm (10 KB–100 MB)
- **In this repo:** the three arm deployers, the real-time driver, the idle-billing experiment, the TAX-marked state store, the cost model, the charts, and the bug log

Companion to *"Serverless vs. Persistent Compute for Agents: The Duty-Cycle Math Nobody Shows"* (DigitalOcean Community, forthcoming).

---

## Abstract

Agents work in bursts. They handle an event, then wait on a model, a tool, or a human. The fraction of wall-clock time spent inside handled events is the agent's **duty cycle**, and for agent hosting it matters more than the size of the workload. An agent at 2% duty spends about 15 hours a month inside events and sits idle for the other 715.

The usual answer is "go serverless": pay per invocation, or pause and pay nothing while waiting. Close, but the gaps cost money. A stateless agent still pays for the databases that hold its state. A paused session is cheap, but an awake-and-idle session still pays for its memory. Neither serverless arm's measured platform overhead lands much under a second.

This run held the agent code fixed and changed only the hosting: a long-lived Droplet process, a Functions action that rehydrates state from Postgres and Spaces on every wake, and a Managed Agents session that freezes and thaws. The same schedules ran on all three for a week. List prices were verified on 9 November 2026.

Duty cycle sets the bill. At 2% the always-on Droplet cost **$37.90**/month whether the agent worked or not; the stateless arm's bill was mostly a **$22.15** backend floor; pause/resume tracked work at **$0.69**. Stateless crosses always-on at about **64.8%** duty on the table/model path. For a light agent (~11% CPU while awake), pause/resume undercuts the Droplet at every duty cycle under full memory allocation; a fully busy session crosses near **41%**.

Measure your duty cycle, count the backends, pause when work stops, and treat every network connection as suspect after a long pause.

---

## Key findings

### 1. Always-on bills every hour; serverless bills the awake fraction

| Profile | Arm | Busy | Billed awake | List price / 730 h |
| --- | --- | --- | --- | --- |
| 2% | (a) Always-on | 2.04% | 100% | **$37.90** |
| 2% | (b) Stateless | 1.96% | 1.96% | **$22.63** |
| 2% | (c) Pause/resume | 1.93% | 1.98% | **$0.69** |
| 30% | (a) Always-on | 32.33% | 100% | **$37.90** |
| 30% | (b) Stateless | 33.04% | 33.04% | **$30.18** |
| 30% | (c) Pause/resume | 31.97% | 32.25% | **$11.30** |

![Monthly harness cost at 2% and 30% duty cycle](https://doimages.nyc3.cdn.digitaloceanspaces.com/007BlogBanners2024/2026/MKTG-3841/idle-cost-breakdown.png)

*Measured usage × DO list prices, scaled to a 730-hour month. Stateless Functions compute is gross (pre-allowance). Pause/resume excludes `$0.05`/GiB-month `SESSION_STORAGE`. Model tokens excluded.*

### 2. Stateless "compute" is mostly the databases that never sleep

At 2%, Functions compute is about **$0.48**/month at gross list price (~25.7k GiB-s), under the 90,000 GiB-s free allowance if it applies. The other **$22.15** is fixed: a $15 Postgres node, 10 GiB additional storage, and a $5 Spaces subscription. Stateless saves on compute and spends on the backends that hold its state.

### 3. "Pays nothing while waiting" still leaves memory on the meter

An awake but idle Harness Runtime session costs about **$0.039**/hour under full 4 GB allocation, mostly memory. CPU drops near zero as advertised; memory does not. Pausing is the cost decision: a 15-minute auto-pause tail on every wake of a 2% profile can add up to about **$10**/month against $0.69 with explicit pauses.

### 4. Both serverless arms land near one second of platform overhead

Idle gap barely mattered from 1 minute to 24 hours. Always-on wakes in **3–4 ms**. Stateless activation-record overhead is ~1 s (excluding in-handler state reload). Pause/resume measured ~1 s including resume + exec + post-result pause; resume alone was **0.24–0.42 s** at p50.

### 5. State size hits latency on stateless, not on resume

A cold stateless wake has a **0.46 s** floor (mostly DB/client setup) even at 10 KB; at 100 MB, download + unpack alone take **0.71 s**. A resumed session's state is ready in ~0.1 ms at every size. In dollars, 100 MB of state is a fraction of a cent; the cost is code and wake time.

### 6. Externalized state is an engineering tax

The stateless arm's handler + state store is **326 lines**, almost as long as the shared agent core (**347**). Of those, **112** are persistence, serialization, and consistency: a lower bound for agents that hold framework objects and open clients. Bugs caught along the way (salted `hash()` signatures, dead HTTPS sockets after a 15-minute pause, Functions blocking invokes past 30 s) are in `BUGLOG.md`.

### 7. Where the cost curves cross

![Monthly harness cost versus duty cycle](https://doimages.nyc3.cdn.digitaloceanspaces.com/007BlogBanners2024/2026/MKTG-3841/duty-cycle-cost-curves.png)

*List-price model (`article/duty_cost.py`). Stateless crosses always-on at **64.8%**. Pause/resume at 100% CPU while awake crosses at **41.2%**; at the measured ~11% CPU it stays below the Droplet even at 100% duty.*

---

## Methodology

### Fixed variables

- One shared agent codebase; only the hosting arm changes
- Identical event schedules (seeded) at 2% and 30% duty for main runs
- Same model, temperature, and max tokens on every arm
- Driver on a separate Droplet; its cost is not attributed to any arm
- Platform overhead = driver round trip − agent-internal time (no clock skew)

### Three arms

| | (a) Always-on | (b) Stateless | (c) Pause/resume |
| --- | --- | --- | --- |
| Platform | Droplet, RIC1 | Functions, NYC1 | Harness Runtime session, RIC1 |
| State between events | Process memory + local disk | Postgres rows + Spaces objects | Frozen sandbox memory + disk |
| Wake path | Function call | Cold start, then state reload | Resume API call |
| Extra code | None | Serialization, persistence, CAS | Pause and resume calls |

### What each experiment measures

1. **Main week** — cost and correctness at 2% and 30% duty
2. **Idle billing** — busy / awake-idle / paused / auto-pause windows on one session
3. **Wake latency** — probes after 1 min, 15 min, 1 h, 6 h, 24 h idle
4. **State size** — 10 KB → 100 MB workspace sweeps on both serverless arms
5. **Engineering tax** — TAX-marked line counts + `BUGLOG.md`

### Infrastructure

| Resource | Spec | Region |
| --- | --- | --- |
| Always-on Droplets | `s5-2vcpu-4gb-30gb` | RIC1 |
| Functions + Postgres + Spaces | Python 3.12, 512 MB (state action 1 GB); 1 GB PG node | NYC1 / NYC3 |
| Managed Agents sessions | `mars-2vcpu-4gb` | RIC1 |
| Driver Droplet | small always-on | same VPC as arms |

### Limitations

- Pause/resume dollars are measured usage × published Harness Runtime rates, not a per-session invoice (Managed Agents ran on a shared team).
- Table figures for pause/resume exclude `SESSION_STORAGE`; the cost model includes $0.05/GiB-month.
- Stateless Functions compute in the tables is gross list-price (pre-allowance). Invoiced Functions usage over the run was about **1.6×** the table's billed-awake assumption; treat table-only compute conclusions with that caveat.
- Memory billing assumes full 4 GB allocation (the more expensive reading); measured idle RSS was ~635 MB.
- Regions are split because Harness Runtime is RIC1-only and Functions is not offered there.
- The 112-line state-tax count is a lower bound for agents with non-JSON-serializable live objects.
- If prose and the analyzer disagree, trust `analysis.analyze` output from a real run directory.

---

## Repository layout

```bash
core/                    Shared agent, model client, timing, long-lived HTTP server
arms/droplet/            systemd installer for the always-on arm
arms/functions/          DO Functions project: handler + TAX-marked state store
arms/managed_agents/     Session bootstrap/smoke/reset, daemon starter, custom image
driver/                  Real-time schedule runner, per-arm delivery, Harness Runtime client
experiments/             Idle-billing windows; schedule generator
analysis/                Run analyzer; list-price cost model + crossover curves
article/                 Scripts and charts published in the article
  duty_cost.py           Monthly harness cost vs. duty cycle
  duty_cycle.py          Busy fraction from an event log (window-clipped)
  charts.py              Redraws article/images/*.png
  images/                Lifecycle, idle-cost, and cost-curve figures
tools/                   Engineering-tax line counter
local/                   Stand-ins for inference, Functions, Harness Runtime + e2e
tests/                   Unit + integration tests (stdlib unittest)
BUGLOG.md                Every bug found during the build and week run
RUNBOOK.md               Provision → measure → tear down
```

## Reproducing

Against the article scripts (no cloud account):

```bash
python3 -m unittest discover -s tests
bash local/e2e.sh

python3 article/duty_cost.py
python3 article/duty_cycle.py article/sample_events.jsonl 1730000000 1730007200
# → events: 4  duty cycle: 2.03%

python3 -m venv .venv && source .venv/bin/activate
pip install -r article/requirements-charts.txt
python3 article/charts.py
```

Against live infrastructure:

```bash
# Read RUNBOOK.md first. Budget roughly $75–$125 for a full week.
# Needs doctl with harness-runtime + serverless, and Managed Agents access.

python3 -m experiments.make_schedules --out sched/
python3 -m experiments.idle_billing --hours 6 --gap-hours 1 --out runs/exp2.json
python3 -m driver.run --targets targets.json --run-dir runs/week1 sched/*.jsonl

python3 -m analysis.analyze runs/week1
python3 tools/count_tax.py
python3 -m analysis.cost_model curves --out curves.csv
```

A unit test keeps `article/duty_cost.py` in step with `analysis/cost_model.py`.

---

## Citation

If you use this code or method, please cite:

```text
Serverless vs. Persistent Compute for Agents: The Duty-Cycle Math Nobody Shows (2026).
https://github.com/mkurup27/agent-duty-cycle
```

## License

MIT. See [LICENSE](LICENSE).
