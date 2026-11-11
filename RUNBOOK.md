# Runbook: one agent, three ways

This runbook covers everything needed to reproduce the week-long measurements behind the article's Tradeoffs 1–5 (idle cost, idle billing, wake latency, state size, and engineering tax). Do the steps in order. Every command runs from the repo root unless a step says otherwise.

**Pricing used in this repo was verified on 2026-11-09** (same stamp as `analysis/cost_model.py` and the article). Re-check it before you publish (`python3 -m analysis.cost_model rates` lists every rate with its source URL).

## 0. What you need

- A DO team with **Managed Agents / Harness Runtime** access, plus a positive balance (Serverless Inference is prepaid).
- `doctl` with the **serverless** plugin (`doctl serverless …`). Harness session create/pause/resume goes through the HTTP API with `DIGITALOCEAN_TOKEN`; you do **not** need a `doctl harness-runtime` subcommand.
- **RIC1 plus New York (NYC1 / NYC3).** Harness Runtime (and therefore Managed Agents) is offered only in **RIC1**. Functions is **not** offered in RIC1, so the stateless arm and its backends live in New York:

  | Resource | Spec | Region |
  | --- | --- | --- |
  | Always-on Droplets | `s5-2vcpu-4gb-30gb` | **RIC1** |
  | Managed Agents sessions | `mars-2vcpu-4gb` | **RIC1** |
  | Functions | Python **3.12**, 512 MB (state action **1 GB**) — pin before deploy | **NYC1** |
  | Managed Postgres (1 GB node) + Spaces | backends for arm (b) | **NYC3** |
  | Driver Droplet | small always-on (`s-1vcpu-1gb` is fine) | **RIC1**, same VPC as the Droplet arm |

  Before you deploy arm (b), set every action in `arms/functions/project.yml` to `runtime: python:3.12` so the reproduce matches the article's Python 3.12 claim (DigitalOcean supports that pin). The shipped file may still say `python:default` until you edit it.

  The driver talks to Droplets over the private VPC, and to Functions / Harness over their public APIs. Activation-record wake overhead for Functions excludes the driver→NYC1 hop; Spaces/Postgres reload times in Tradeoff 4 include the NYC1↔NYC3 hop.

- A **model access key** for Serverless Inference. Pick the model slug once and use it everywhere. The article used `llama-4-maverick` at temperature **0** and **256** max tokens (hard-coded default in `core/model.py` via `MODEL_MAX_TOKENS` or `256`). Any slug is fine for the harness comparison if you keep the same slug, temperature and max tokens on every arm (token *rates* are identical across arms; measured counts can still differ):

  ```bash
  curl -s -H "Authorization: Bearer $MODEL_ACCESS_KEY" https://inference.do-ai.run/v1/models \
    | python3 -m json.tool | grep '"id"'
  export MODEL_ID=llama-4-maverick
  ```

  If you pick a different slug, measure one call's wall time and pass `--model-latency <seconds>` to `experiments.make_schedules` (default **4.5**, tuned for llama-4-maverick ~4.4 s). After the week, check `runs/week1/summary.json` → each target's `duty_cycle.busy_fraction` against Tradeoff 1 (~2% / ~32%).

  **`MODEL_MAX_TOKENS` / `MODEL_IDLE_EVICT_S`:** the Droplet `setup.sh`, Functions `project.yml` / `.env`, and Managed Agents bootstrap only forward `MODEL_ID` / `MODEL_ACCESS_KEY` (and `MODEL_BASE_URL` where applicable). Exporting those knobs on the **driver alone does nothing** for the agents. To change them, edit `core/model.py` defaults or add the vars to each deployer (Droplet env file, Functions project env, host-mode sandbox env / custom image) **before** you deploy or `install`.

- **Budget, rough estimate: $75 to $125** for everything below. That covers ~3 Droplets for about 9 days, Postgres and Spaces for one month, sessions and tokens. Tokens are the uncertain part, so set a spend alert first. The **driver Droplet's cost is not attributed to any arm**.

## 1. Rehearse locally (about 2 minutes, no cloud needed)

```bash
python3 -m unittest discover -s tests       # 24 tests across tests/ (units + integration + model client)
bash local/e2e.sh                           # compressed half-day of main + state schedules (not probes) on local stand-ins
```

`local/e2e.sh` writes analysis under `/tmp/dcstack/run/` (or the state dir you pass). Expect 0 errors and 0 divergences in `/tmp/dcstack/run/summary.json`, and matching `tokens` blocks across a/b/c for the same profile. If that isn't what you see, stop and investigate before spending money.

## 2. Provision

**Tag** every resource `agent-duty-cycle` (for bill filters). **Name** the Droplets exactly `a-2pct`, `a-30pct`, `a-probe`, and `driver` so the teardown `doctl compute droplet delete …` lines work. Managed Agents sessions use the `dc-c-*` names below. Tag is not the Droplet hostname.

### Driver (not part of any arm's cost)

Create a small Droplet named **`driver`** (`s-1vcpu-1gb`) in **RIC1**, in the same VPC as the always-on arm. Copy the repo to it and run everything below from there inside `tmux`. The driver process itself is stdlib-only Python 3.9+; Option B for arm (b) cleanup needs a small venv (see below).

### targets.json and driver environment (do this first)

```bash
cp targets.example.json targets.json
# Edit private Droplet URLs, Functions apihost/namespace, and session names.
```

Driver env vars that must be set for the week run **and** for any `--resume` (the
driver re-reads them; they are not stored in `targets.json`):

| Var                                               | Used by                                        |
|---------------------------------------------------|------------------------------------------------|
| `MODEL_ID`, `MODEL_ACCESS_KEY`                    | all arms (inference)                           |
| `TOKEN_A_2PCT`, `TOKEN_A_30PCT`, `TOKEN_A_G1M`, … | arm (a); names match each target's `token_env` |
| `FN_KEY_ID`, `FN_KEY_SECRET`                      | arm (b) namespace access key                   |
| `DIGITALOCEAN_TOKEN`                              | arm (c) Harness HTTP API                       |

Optional on the driver only where the *driver* talks to inference for local tools: `MODEL_BASE_URL` (default `https://inference.do-ai.run/v1`). See §0 for why `MODEL_MAX_TOKENS` / `MODEL_IDLE_EVICT_S` on the driver are not enough for the arms.

For **host-mode** Managed Agents, leave `install_dir` unset (the driver resolves `~/dcagent`). For **custom-mode**, set `"install_dir": "/opt/dcagent"` on every `managed_agents` target.

### Arm (a): always-on Droplets (RIC1)

Create three Droplets of size **`s5-2vcpu-4gb-30gb`** (Ubuntu) in RIC1, named **`a-2pct`**, **`a-30pct`**, **`a-probe`**, matching the article's always-on shape and the sandbox default `mars-2vcpu-4gb`:

| Droplet name | Agents (NAME / PORT)                                                   |
|--------------|------------------------------------------------------------------------|
| a-2pct       | `a-2pct` / 8080                                                        |
| a-30pct      | `a-30pct` / 8080                                                       |
| a-probe      | `a-g1m` 8081, `a-g15m` 8082, `a-g1h` 8083, `a-g6h` 8084, `a-g24h` 8085 |

Multiple agents on `a-probe` is intentional (one Droplet, five probe ports). The `setup.sh` header says "one Droplet per agent" for billing clarity on the main profiles; the probe Droplet is the documented exception.

From the **repo root** on the driver (or workstation), copy the tree once per Droplet, then install each agent. Prefer a short-lived env file so secrets are not on the remote `ssh` argv. **Regenerate `AGENT_TOKEN` and the env file for every NAME/PORT:**

```bash
# From repo root — lands at /root/agent-duty-cycle on the Droplet:
scp -r . root@<droplet>:/root/agent-duty-cycle

# On the driver — expand MODEL_* locally, never inside single-quoted ssh:
umask 077
AGENT_TOKEN=$(openssl rand -hex 16)
cat > /tmp/dc-a-2pct.env <<EOF
MODEL_ID=$MODEL_ID
MODEL_ACCESS_KEY=$MODEL_ACCESS_KEY
AGENT_TOKEN=$AGENT_TOKEN
EOF
scp /tmp/dc-a-2pct.env root@<droplet>:/tmp/dc-a-2pct.env
rm /tmp/dc-a-2pct.env
ssh root@<droplet> 'set -a; . /tmp/dc-a-2pct.env; set +a; rm -f /tmp/dc-a-2pct.env; \
  cd /root/agent-duty-cycle && NAME=a-2pct PORT=8080 DRIVER_IP=<driver private IP> \
  bash arms/droplet/setup.sh'
```

Repeat for each `NAME` / `PORT` (new token + new `/tmp/dc-<NAME>.env` each time). Copy each token from `/etc/dcagent/<NAME>.env` into the driver's environment as `TOKEN_A_2PCT`, `TOKEN_A_G1M`, and so on.

Use the Droplets' **private** IPs in `targets.json`. Agent traffic is plain HTTP plus a token, so keep it on the VPC. `setup.sh` enables ufw: **OpenSSH is allowed from anywhere**; only the **agent port** is restricted to `DRIVER_IP`.

### Arm (b): Functions (NYC1) + Postgres + Spaces (NYC3)

1. Create a Managed Postgres single-node cluster (smallest / 1 GB plan) in **NYC3** and a Spaces bucket in **NYC3** (for example `dc-agent-state`) plus a Spaces access key. For the article's **$22.15** backend floor, provision about **10 GiB additional** Postgres storage ($0.215/GiB-month) on top of the $15 node and $5 Spaces base; without that extra storage the floor is about **$20**.

2. **Trusted Sources:** Functions namespaces do **not** publish fixed egress IPs you can whitelist. For this experiment, leave the Postgres trusted-sources list **open** (or allow `0.0.0.0/0`) for the run window so actions can reach the DB; tighten again at teardown. If you only need driver-side `psql` / `reset_b.py`, you could lock to the driver IP — but then Functions state I/O will fail.

3. In `arms/functions/`, run `cp .env.example .env` and fill it in. Paste the cluster CA certificate into `PG_CA_CERT` (newlines as `\n`). **Region note:** `.env.example` comments say "same region" for Postgres/Spaces and the Functions namespace. The article put Functions in **NYC1** and Postgres/Spaces in **NYC3** (`SPACES_ENDPOINT=https://nyc3.digitaloceanspaces.com`, `SPACES_REGION=nyc3`).

4. Connect a namespace in **NYC1** and deploy (return to the repo root afterward):

   ```bash
   doctl serverless namespaces create --label dc-agent --region nyc1   # if needed
   doctl serverless connect dc-agent
   ( cd arms/functions && ./deploy.sh )
   ```

   Main and probe actions run at **512 MB**; the state-sweep action runs at **1 GB** (Functions maximum). Confirm `runtime: python:3.12` on every action in `arms/functions/project.yml` before this deploy (shipped file may still say `python:default`).

5. Create a **namespace access key**, then export it on the driver as `FN_KEY_ID` and `FN_KEY_SECRET`. Get the `apihost` and namespace from `doctl serverless status` and put them in `targets.json` (see `targets.example.json` for the `faas-nyc1-…` shape).

6. Smoke-test it from the driver:

   ```bash
   python3 -c "from driver.targets import FunctionsTarget as F; import json; \
   print(json.dumps(F('t', json.load(open('targets.json'))['b-state']).deliver({'id':'smoke-1','type':'probe'}, 999), indent=1)[:1500])"
   ```

   You want `result.ok: true`, a `fn_init_ms` value (cold start), and `state_io` timings.

7. **Clean up the smoke row** so it does not leak into the state-size sweep. Pick one:

   ```bash
   # Option A — delete only b-state (needs postgresql-client + DATABASE_URL on the driver)
   sudo apt-get install -y postgresql-client
   # DATABASE_URL from arms/functions/.env; may need ?sslmode=require
   psql "$DATABASE_URL" -c "DELETE FROM agent_state WHERE agent_id='b-state';"
   ```

   ```bash
   # Option B — wipe ALL arm-(b) DB rows + Spaces blobs (tools/reset_b.py)
   # Needs pg8000/scramp (not stdlib). It reads ~/fn.env only (not arms/functions/.env).
   python3 -m venv .venv-fn && source .venv-fn/bin/activate
   pip install -r arms/functions/packages/agent/handle/requirements.txt   # pg8000==1.31.2, scramp==1.4.5
   cp arms/functions/.env ~/fn.env
   python3 tools/reset_b.py          # dry run: lists rows
   python3 tools/reset_b.py --yes    # deletes
   deactivate
   ```

   Spaces objects named `ws-*.tar` can remain after a partial cleanup (see BUGLOG K-1);
   list and delete orphans before you trust a state-sweep bill.

**Free allowance:** Functions includes 90,000 GiB-s a month, *team-wide*. If other
functions in the team use part of it, note that when you compare the bill. The article's
tables report **gross** list-price Functions compute (pre-allowance).

### Arm (c): Managed Agents sessions (RIC1)

`DIGITALOCEAN_TOKEN`, `MODEL_ID` and `MODEL_ACCESS_KEY` must already be exported (see above). In `targets.json`, keep `pause_policy: "explicit"`. For the main 2% / 30% sessions use **`min_gap_to_pause_s: 60`**: the driver pauses after each event unless the next event is due within 60 seconds (the pause policy the article used; it skipped 72 pauses on the 30% run). Probe and state-sweep sessions use `min_gap_to_pause_s: 0` so every wake is followed by a pause — otherwise an explicit skip would leave the session awake across the deliberate idle gap and contaminate wake-latency / state-size numbers.

**Do a throwaway smoke session first.** It decides which mode works on your team:

```bash
python3 arms/managed_agents/bootstrap.py create  --name dc-c-smoke          # host mode, adapter opencode
python3 arms/managed_agents/bootstrap.py install --name dc-c-smoke
# install JSON includes model_key_present when the daemon starts
python3 arms/managed_agents/bootstrap.py smoke   --name dc-c-smoke --pause-seconds 60
python3 arms/managed_agents/bootstrap.py destroy --name dc-c-smoke
```

Read the smoke report (and the install line above it).

| Field | Must be | If not |
| --- | --- | --- |
| `probe_ok` | `true` | Check install output for `model_key_present` / `model_id`, and smoke's `probe_error` / `health_before`. Smoke itself does **not** emit `model_key_present` |
| `process_survived_pause` | `true` | **Stop.** Do not start the week run until you know why the process did not survive. Continuing would invalidate pause/resume results |
| `helper_survived_pause` | Record it | Finding, not pass/fail |
| `model_conn_reused_after_pause` | Expect `false` with the shipped client | `core/model.py` evicts connections idle longer than `MODEL_IDLE_EVICT_S` (default **30 s**). A 60 s smoke therefore closes the socket on purpose (post-fix). Pre-fix, a 60 s pause could still look open. Changing the threshold means editing `core/model.py` or the sandbox env **before** install (see §0); driver-only export is not enough. Then optionally smoke with `--pause-seconds 900` |
| `resume_call_s`, `resume_to_first_result_s` | Record them | |

If `create` fails:

- **Adapter error:** retry with `--adapter claude-code` or `--adapter codex`.
- **Python missing in the sandbox:** use custom mode (below).
- **The adapter needs its own model credential to boot:** you'll get an error naming the secret. Add it,
  or switch adapter.

**Custom mode** (needs `agent: custom` enabled for your team). You need a **DigitalOcean Container Registry**, `doctl registry login`, and an amd64 image. Custom creates must put the model key in the session spec (`--secret-mode spec`); the default file-copy path is host-install only. Smoke must pass `--mode custom` so paths resolve to `/opt/dcagent`. Do not use `up --mode custom`: `up` always smokes, custom mode has no `reset`, and those 2 probe events stay in the daemon transcript (BUGLOG B-002).

```bash
doctl registry login
docker build --platform linux/amd64 -f arms/managed_agents/Dockerfile \
  -t registry.digitalocean.com/<reg>/dcagent:1 .
docker push registry.digitalocean.com/<reg>/dcagent:1     # note the sha256 digest
IMAGE=registry.digitalocean.com/<reg>/dcagent@sha256:<digest>

# Throwaway smoke only:
python3 arms/managed_agents/bootstrap.py create --mode custom --secret-mode spec \
  --image "$IMAGE" --name dc-c-smoke
python3 arms/managed_agents/bootstrap.py smoke --mode custom --name dc-c-smoke --pause-seconds 60
python3 arms/managed_agents/bootstrap.py destroy --name dc-c-smoke

# Production sessions: create + pause only (no smoke on the measured sessions)
for n in 2pct 30pct state g1m g15m g1h g6h g24h; do
  python3 arms/managed_agents/bootstrap.py create --mode custom --secret-mode spec \
    --image "$IMAGE" --name dc-c-$n
  python3 arms/managed_agents/bootstrap.py pause --name dc-c-$n
done
```

Set `"install_dir": "/opt/dcagent"` on every `managed_agents` entry in `targets.json`.

**Host mode (default)** — `up` runs create, install, smoke, **reset**, and pause (reset
clears the smoke transcript):

```bash
python3 arms/managed_agents/bootstrap.py up \
  --names dc-c-2pct,dc-c-30pct,dc-c-state,dc-c-g1m,dc-c-g15m,dc-c-g1h,dc-c-g6h,dc-c-g24h
```

**Host-mode caveat for the write-up:** the adapter's own (idle) agent process shares the sandbox with our daemon. The idle-billing awake-idle window (Tradeoff 2) shows how much CPU that uses when idle. Report that number. The article measured ~635 MB RSS at idle (opencode ~412 MB, platform services ~190 MB, our agent ~33 MB).

## 3. Generate schedules

```bash
python3 -m experiments.make_schedules --out sched/
# If MODEL_ID is not llama-4-maverick, pass your measured per-call latency:
# python3 -m experiments.make_schedules --out sched/ --model-latency <seconds>
cat sched/plan.json        # event counts and estimated duty cycle for each schedule
```

`--seed` makes the event content identical across arms. `sched/` is listed in `.gitignore` (same as `runs/`), so do **not** expect a normal `git commit` of it. Archive `sched/` next to the run artifacts under `runs/` (or `git add -f sched/` only if you deliberately want it in version control).

Article inventory (workload wakes the article counted):

| Workload               | Volume                                                                   |
|------------------------|--------------------------------------------------------------------------|
| Main week, 2% profile  | 234 events/arm                                                           |
| Main week, 30% profile | 1,311 events/arm                                                         |
| Wake-latency probes    | idle gaps 1 min / 15 min / 1 h / 6 h / 24 h; 603 probes/arm              |
| State-size sweeps      | 10 KB → 100 MB; 50 wakes/size on each serverless arm (**200** wakes/arm) |
| Across 23 targets      | **6,844** workload wakes in the published run (6,842 succeeded)          |

`plan.json` / the JSONL files also include **seed** events that set workspace size before each state-sweep bucket (4 seeds × 2 serverless arms = 8). A full `sched/` tree therefore shows about **204** lines per state target (4 seeds + 200 probes) and about **6,852** scheduled lines overall. The article's **6,844** counts workload wakes and excludes those seeds.

## 4. Run the experiments

Run idle billing **to completion** before starting `driver.run`. Both create Harness usage on the same team; overlapping them contaminates the hourly usage export you need for Tradeoff 2.

### Idle billing (Tradeoff 2; about **20 h** wall time)

Holds `mars-2vcpu-4gb` sessions across four windows (`--hours` = **6** in the article) with quiet gaps so the hourly usage export can separate most of them:

```bash
python3 -m experiments.idle_billing --hours 6 --gap-hours 1 --out runs/exp2.json
```

| Window            | What it is                                              | What the article uses it to show                                                             |
|-------------------|---------------------------------------------------------|----------------------------------------------------------------------------------------------|
| **W1 busy**       | one core burning, `keep_warm`                           | CPU follows consumption (~51.6% of 2 vCPUs in the article)                                   |
| **W2 awake-idle** | **same** session, immediately after W1, nothing running | CPU near zero, **memory still bills**; ~$0.039/hr at list under full 4 GB allocation         |
| **W3 paused**     | paused right after boot, for `--hours`                  | CPU and memory stop; storage remains billable per the launch post                            |
| **W4 auto-pause** | left idle with the platform idle timeout                | ends when the session auto-pauses (~15 min; article: **907.5 s**), **not** a full 6 h window |

**W1 and W2 are back-to-back** on one `keep_warm` session (no gap between them). Discard the mixed UTC hour that straddles the W1→W2 boundary when you match the invoice. Wall clock is roughly 6 + 6 + 1 + 6 + 1 + ~0.25 ≈ **20 h**, not 24.

Match the remaining UTC windows in `runs/exp2.json` against the hourly usage export. Pause/resume dollars in the article are measured usage × published Harness Runtime rates, not a per-session invoice (Managed Agents ran on a shared team). Every figure assumes full 4 GB memory allocation while awake (the more expensive reading of the pricing page; idle RSS was only ~635 MB). A dedicated-team bill that lines memory charges up against RSS vs 4 GB is the only clean way to settle which reading is correct; the article could not.

`pause_policy: auto` and a short `idle_timeout: "1m"` are **article findings, not automated here**. W4 already exercises the default ~15-minute idle suspend. A spec with `idle_timeout: "1m"` was accepted but paused only after 781 s (BUGLOG P-003); reproducing that is a one-off create + wait, not a flag on `driver.run`.

### Main week + wake probes + state sweeps (Tradeoffs 1, 3, 4; one driver, one week)

Only after idle billing has finished and you have copied `runs/exp2.json` aside:

```bash
python3 -m driver.run --targets targets.json --run-dir runs/week1 sched/*.jsonl
```

- If the driver dies, restart with `--resume` **with the same env vars** listed in §2. It keeps the original start time and skips events already logged.
- `kill -USR1 <driver pid>` dumps every thread's stack to stderr if the run looks stuck.
- Check daily with `python3 -m analysis.analyze runs/week1`. Watch for errors, `state_lost`, `conflict`, divergences and `session_lost`.

The docs say idle sessions can be destroyed after an inactivity period, but they don't say how long. The 24-hour probe bucket is the canary for this. If a session is destroyed, the log shows `session_lost`. Record it in `BUGLOG.md`. Recovery:

1. `bootstrap.py destroy --name dc-c-<…>` if the name is still listed (ignore errors if already gone).
2. Recreate and pause: host mode `bootstrap.py up --names dc-c-<…>` (or create + install + reset + pause); custom mode create + pause only.
3. **Restart** `python3 -m driver.run --resume …` as a **new process**. `ManagedAgentsTarget` caches `session_id` after the first lookup; a live driver that already saw the dead sid will keep using it. A fresh `--resume` process re-resolves the name.

In the published run, all seven 24-hour probes found their session paused and intact.

### Engineering tax (Tradeoff 5)

```bash
python3 tools/count_tax.py > runs/week1/tax.json
```

Keep `BUGLOG.md` up to date *during* the week. It's the part of the tax no tool can count.

## 5. Analyze, bills, and charts

When the week ends, run a final analysis pass and the list-price helpers:

```bash
python3 -m analysis.analyze runs/week1
# Default --fn-mem-gib 0.5 matches main/probe (512 MB). analyze applies ONE memory to
# every Functions target. For b-state (1 GB), compute GiB-s by hand from summary.json:
#   b-state functions.billed_s × 1.0
#   (equivalently functions.gib_s × 2 when the summary was built at --fn-mem-gib 0.5)
# Do not re-run with --fn-mem-gib 1.0 unless you first copy summary.json aside — a re-run
# overwrites it and would overstate main/probe GiB-s at 1.0 GiB.
cp runs/week1/summary.json runs/week1/summary.fn0.5.json   # optional safety copy

# Prefer the article curves script (cpu_util=0.11, no wake-overhead term):
python3 article/duty_cost.py
# analysis.cost_model curves defaults are NOT the article curves (--cpu-util 0.3, --wakes 1000).
# To approximate published crossovers with cost_model instead:
#   python3 -m analysis.cost_model curves --cpu-util 0.11 --wakes 0 --out runs/week1/curves.csv
#   python3 -m analysis.cost_model curves --cpu-util 1.0 --wakes 0 --out runs/week1/curves-busy.csv

# Optional charts (needs matplotlib):
python3 -m venv .venv && source .venv/bin/activate
pip install -r article/requirements-charts.txt
python3 article/charts.py
```

`summary.json` → `list_price_estimate_usd_per_month` is the list-price prediction. The article's Tradeoff 1 table excludes the $0.05/GiB-month `SESSION_STORAGE` line; `analysis/cost_model.py` and `article/duty_cost.py` include ~$0.05, so model points sit slightly above the table for pause/resume.

Record actual charges per resource in `runs/week1/bills.json` (a manual record from the usage/invoice export, filtered to the run window). Do not fold the driver Droplet into any arm.

```json
{"a-2pct": {"droplet": 0.00}, "b-2pct": {"functions_gib_s": 0, "usd": 0.00},
 "shared_b": {"postgres": 0.00, "spaces": 0.00},
 "c-2pct": {"cpu": 0.00, "memory": 0.00, "storage": 0.00, "snapshots": 0.00},
 "inference": {"usd": 0.00, "input_tokens": 0, "output_tokens": 0}}
```

Reconcile before you write anything up:

- **Functions:** gross list-price table path vs invoice (~**1.6×** GiB-s in the published run for the main-run profiles at 512 MB; residual unexplained).
- **Postgres:** invoice additional-storage vs list-price 10 × $0.215 = $2.15.
- **Harness / pause-resume:** usage × published rates under full 4 GB allocation; not a per-session invoice on a shared team.

## 6. Tear down

Before you destroy anything, copy artifacts off the driver to your workstation:

```bash
# From your workstation:
scp -r root@<driver>:/root/agent-duty-cycle/runs ./
scp -r root@<driver>:/root/agent-duty-cycle/sched ./
scp root@<driver>:/root/agent-duty-cycle/BUGLOG.md ./
```

Destroy Managed Agents sessions and their saved Environment Configs from the driver (or workstation, with `DIGITALOCEAN_TOKEN` set), then delete Droplets **from the workstation** so you do not strand yourself mid-teardown:

```bash
for n in 2pct 30pct state g1m g15m g1h g6h g24h; do
  python3 arms/managed_agents/bootstrap.py destroy --name dc-c-$n
done
python3 arms/managed_agents/bootstrap.py cleanup-configs --prefix dc-

# Confirm you are on the experiment namespace before undeploy --all:
doctl serverless status
# Expect the dc-agent / fn-… namespace you created. Then:
doctl serverless undeploy --all          # NYC1 — irreversible for that namespace

doctl databases delete <pg-id>           # NYC3
# From the workstation (not the driver being deleted). Names must match §2:
doctl compute droplet delete a-2pct a-30pct a-probe driver   # RIC1
# delete the Spaces bucket contents and bucket last (NYC3; check for orphaned ws-*.tar: BUGLOG K-1)
# Re-tighten Postgres trusted sources if you left them open for Functions.
```

## Measurement notes for a valid reproduce

- **Latency (a)/(c):** `analysis.analyze` uses `platform_overhead = driver_total_s − agent-internal time`. In `driver/targets.py`, `driver_total_s` stops before the post-result pause, so analyzer overhead for arm (c) is resume + exec (and any result polling). Pause is recorded separately as `pause_s`. Resume alone was 0.24–0.42 s at p50 in the article. If Tradeoff 3 prose includes the post-result pause in the ~1 s figure, trust the analyzer.
- **Latency (b):** do not use driver round trip − agent time for published wake overhead. Use activation-record fields (`fn_wait_ms`, `fn_init_ms`, `fn_duration_ms`); `analysis.analyze` already applies `overhead_of` that way.
- **Functions async >30 s:** a blocking call past 30 seconds returns only an activation ID. Activation records are kept only for **asynchronous** invocations, so long events must be invoked asynchronously and polled (the driver polls every 2 s). Use `fn_duration_ms`, `fn_wait_ms` and `fn_init_ms` from the **activation record** for latency and cost, not the driver round trip. Results in the article became visible 3.1–3.5 s after the function finished. Activation-record overhead excludes in-handler state reload (Tradeoff 4's cold-container 0.46 s floor and up).
- **Duty cycle:** what was *achieved* comes from the logs (`busy_fraction`). What was *billed as awake* is computed per arm (`billed_awake_fraction`). Report both. For pause/resume with `min_gap_to_pause_s: 60`, billed-awake can exceed busy (article: 32.25% vs 31.97% at 30%).
- **Pause policy:** `pause_policy: explicit` with `min_gap_to_pause_s: 60` on the main sessions is the policy the article used. Probes/state use `0` so deliberate idle gaps stay paused. Relying on the 15-minute idle suspend instead is the out-of-the-box behaviour (see W4); it is not automated as a second week-long arm in this repo.
- **Memory billing assumption:** report pause/resume dollars under **full 4 GB allocation** while awake unless a dedicated-team bill settles measured vs allocated. CPU follows consumption; memory follows allocation and keeps running while awake-idle.
- **Pause/resume $ vs invoice:** Managed Agents on a shared team may not yield a per-session invoice. Prefer usage × list rates, and say so.
- **Tokens:** totals should be *compared* across arms for the same schedule. Matching totals check the harness comparison; they do **not** guarantee equal token bills. Rates are the same everywhere; measured counts can still differ (temperature-0 replies still vary in length). The article's 30% runs agreed within ~8%; the 2% runs varied more.
- **Functions ~1.6×:** if you publish dollar conclusions from Functions compute, cross-check invoiced GiB-s against billed-awake × memory; the article saw ~1.6× and left a residual unexplained.
- **Driver cost:** keep it out of every arm's total.

## Final checklist (Tradeoff → where to look)

| Article table                           | Primary source after a reproduce                                                                                                                              |
|-----------------------------------------|---------------------------------------------------------------------------------------------------------------------------------------------------------------|
| Tradeoff 1 (busy / billed awake / $/mo) | `runs/week1/summary.json` → `duty_cycle.*`; list-price from analyzer + `article/duty_cost.py` (not bare `cost_model curves` defaults); `bills.json` is manual |
| Tradeoff 2 (idle windows)               | `runs/exp2.json` UTC windows ↔ hourly usage export (drop the W1/W2 mixed hour)                                                                                |
| Tradeoff 3 (wake latency)               | `summary.json` → `latency_s.platform_overhead` per probe target (resume+exec; `pause_s` separate — see Measurement notes)                                     |
| Tradeoff 4 (state size)                 | `summary.json` → `by_state_size` (b-state / c-state); b-state GiB-s = `functions.billed_s` × 1.0                                                              |
| Tradeoff 5 (engineering tax)            | `runs/week1/tax.json` + `BUGLOG.md`                                                                                                                           |
| Token parity check                      | `summary.json` → `tokens` per target (check, not a bill guarantee)                                                                                            |
| Invoice caveats                         | Functions ~1.6× vs table; Postgres storage gap; Harness = usage × rates                                                                                       |
