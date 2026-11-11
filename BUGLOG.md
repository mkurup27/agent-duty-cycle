# Bug log (experiment 6: the engineering tax)

Record every bug as you hit it, in the order you hit it. Don't tidy it later: the
article needs the raw count, the symptoms, and how long each one took to find.

**Arm** is `a` (always-on), `b` (stateless) or `c` (pause/resume). **Class** is one of:

- `serialization`: state that didn't round-trip
- `consistency`: lost or stale updates, conflicts, versioning
- `rehydration`: state missing or slow after a wake
- `lifecycle`: a process or connection didn't survive
- `harness`: bugs in the test tooling. These don't count as tax, but log them anyway.

| ID | Date | Arm | Class | Symptom | Root cause | Found by | Time to find + fix | Counts as tax? |
|---|---|---|---|---|---|---|---|---|
| B-001 | 2026-10-27 | b | serialization | (none yet: caught before first run) | `workspace_signature()` used Python `hash()`, which is salted per process. The stored signature could never match one computed in the next container, so every save after a cold start would re-upload the whole workspace. | Code review while building | ~10 min | Yes |
| B-002 | 2026-10-27 | c | harness | Arm (c) used ~10% more input tokens than (a) and (b) on an identical schedule | The bootstrap smoke test sent 2 probe events to the same daemon later used for the measured run. Its transcript then carried 4 extra messages into every model call. Fixed with `bootstrap.py reset` (fresh daemon after smoke). | Token-parity check in `analysis.analyze` during the local rehearsal | ~15 min | No (harness) |

| B-003 | 2026-10-27 | a | harness | All 7 always-on agents crash-looped: `No module named core.server` | The repo zip carried 0600/0700 file modes; the service runs as the unprivileged `dcagent` user and could not read `/opt/dcagent/core`. Fixed: `setup.sh` now runs `chmod -R a+rX`, zip rebuilt with normal modes. | `journalctl` on first deploy | ~10 min | No (harness) |
| B-004 | 2026-10-27 | b | harness | `pg8000.connect` failed: `module 'scramp' has no attribute 'make_channel_binding'` | pg8000 1.31.2 pinned without its auth dependency; pip resolved scramp 1.5.0, whose API changed. Fixed by pinning `scramp==1.4.5`. Only an unpinned-dependency bug, but it is the kind the stateless arm attracts: it needs a DB driver the other arms never import. | First real DB connection from the driver | ~10 min | Borderline: dependency tax (record, judge in write-up) |
| B-005 | 2026-10-27 | c | harness | Exp 2 rehearsal died with an SSL read timeout on the first exec | The CPU burner was spawned without redirecting stdout/stderr, so it held the exec call's output pipe open and the exec could not return until the burn ended. Also: a crash left sessions (one keep_warm, i.e. billing) alive. Fixed: burner uses DEVNULL; idle_billing now destroys every session it created on exit, even after an error. | Real-API rehearsal | ~15 min | No (harness) |
| B-006 | 2026-10-28 | b | harness | A 45 s stateless event never returned; the driver polled silently for ~17 min | The driver invoked Functions with `blocking=true`. Past the 30 s window DO returned `202` + an activationId, but blocking activations are not retained, so that record never appears and the result is lost. Fixed: every Functions event is now invoked non-blocking and its (retained) activation record polled with 0.2 s→2 s back-off. Found in the real-infra dry run, would have broken every >30 s event of the week. | Real-API test of the long-event path | ~45 min | No (harness) - but see P-004 |
| B-007 | 2026-10-29 | c | lifecycle | Week run, first ~2.7 h: 9 of 10 `c-g15m` probes spent ~121 s in a single model call (arm (a) on the same events: ~1 s) | After a 15-min pause the sandbox's kept-alive HTTPS socket still looked open but was dead; the request waited the full 120 s read timeout, then reconnected and succeeded. Fixed in core (all arms): connections idle > 30 s are evicted before use. Run aborted and restarted. | `analysis.analyze` busy-fraction outlier, confirmed per event | ~30 min | **Yes, pause/resume arm**: frozen processes keep dead network state |
| B-008 | 2026-10-29 | all | harness | 12 events failed with HTTP 529; 2 later approvals reported `state_lost` | The model client did not retry transient provider errors; the failed incident left no record, so its approval looked like lost state. Fixed: retry 429/5xx/529 with 2-4-8-16 s back-off (all arms); the analyzer now counts such approvals as `cascade_after_failed_incident`, not lost state. | Same check | ~20 min | No (harness) |
| B-009 | 2026-11-05 | all | harness | Final `analysis.analyze runs/week2` crashed: `KeyError: 'managed_agents'` | The B-008 change reused the variable `kind` (the arm type) for the error type, so any target with a failed event lost its arm type. Day-1 checks had no errors and never hit it. Fixed (`err_kind`) + regression test. No data affected: the analyzer only reads the logs. | Final analysis | ~5 min | No (harness) |

## Platform observations (not our bugs; candidates for the "honest limits" section)

| ID | Date | Service | What happened | Handling |
|---|---|---|---|---|
| P-001 | 2026-10-27 | Harness Runtime (preview) | `POST /v2/agents/sessions` returned HTTP 500 `credential storage request failed` for a valid spec (the 4th session created in ~5 min; the 3 before it succeeded). request_id `32056e28-313d-455a-8113-1ae26cd8e9b1` | Session create now retries 5xx with backoff and checks by name first, so no duplicates; retries are logged (`api_retries`). |
| P-002 | 2026-10-27 | Harness Runtime (preview) | P-001 became permanent: every create **with a spec secret** fails with the same 500 (request_id `92b301f6-55ce-49d3-823b-9d8ca3536799`), while an identical create **without** secrets succeeds. Team B holds >300 saved Environment Configs (mostly another member's `mabench-*` runs; every YAML create persists one, with its credentials). Likely a team-level credential-storage limit or fault. | Specs no longer carry secrets; the model key is copied into the sandbox at install (`.model_key`, mode 600). `bootstrap.py cleanup-configs` removes our own `dc-*` configs. Reported to the Managed Agents team. Write-up note: a stateful platform has its own control-plane state to manage. |
| P-003 | 2026-10-27 | Harness Runtime (preview) | Spec `idle_timeout: "1m"` was accepted without warning, but the session auto-paused only after **781 s** (~13 min, `pause_reason: idle`). Either short timeouts are clamped to a floor or the field is not honoured for this adapter. | Pause policy stays `explicit` (driver pauses right after each event). Report the 781 s as the effective out-of-the-box idle tail; re-checked in the real exp 2 run: with `15m` configured the session paused after **907.5 s** (`pause_reason: idle`), so the default is honoured and only the short value was not. |
| P-004 | 2026-10-28 | Functions | A blocking REST invocation that outlives the 30 s window is held open until the function finishes (40.1 s for a 40 s event), then returns only `202 {activationId}`; that activation's record could not be retrieved afterwards (404 for ~17 min), so the caller never receives the result. Non-blocking invocations are retained and retrievable (record appeared ~3 s after completion). | All arm-(b) calls use non-blocking invoke + poll. Write-up: one more piece of plumbing the stateless arm needs for long tool calls. |
| P-005 | 2026-10-29 | Harness Runtime | Pause/resume preserves processes and open sockets, but a connection that was open across a ~15-min pause is dead on resume and not reported as closed: the next request hangs until the client's read timeout. (60 s pauses in the smoke test were fine.) | Client-side idle eviction (B-007). Write-up: "state survives the pause; the network around it does not". |
| P-006 | 2026-10-29 | Serverless Inference | HTTP 529 "Platform overloaded" on 12 events across all three arms in the first ~2.7 h of the week run (llama-4-maverick). | Retries with back-off (B-008); retry counts and wait time are logged per call. |

## Known, accepted issues (design limits, not bugs yet)

- **K-1 (b, consistency):** a crash between the Spaces PUT and the Postgres commit leaks one
  `ws-<version>.tar` object. Fixing it needs a GC sweep, which is more tax.
- **K-2 (b, rehydration):** a warm container's leftover workspace is wiped and re-downloaded on
  every invocation, because another container may have committed newer state. A
  version-checked cache would cut latency, but it would add tax and a new class of
  stale-cache bugs.
- **K-3 (b, serialization):** changes are detected by path, size and mtime_ns. A rewrite that
  keeps the same size and the same mtime_ns would be missed. This is theoretical with this agent.
- **K-4 (b, all):** the agent's state is plain dicts and lists on purpose. Real agents hold
  framework objects, open clients and iterators, so the tax measured here is a **lower bound**.
- **K-5 (b, lifecycle):** the helper subprocess and model connection survive only inside
  a warm container. The logs report how often that happened.

## During the live run

Add rows above. For each one, also copy the failing event's JSON line from
`runs/<id>/<target>.jsonl` into `runs/<id>/bugs/B-xxx.json`.
