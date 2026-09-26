# skillopt

> SkillOpt self-evolution engine for Agent Zero: harvests the agent's own task rollouts, drives the official `skillopt_sleep` pipeline (or a local fallback optimizer), gates every proposed `SKILL.md` rewrite, and stages gated edits for adoption.

## Purpose

`usr/skills/<name>/SKILL.md` documents are the trainable surface here, not model weights. A harvester records each finished chat as a rollout, a two-loop background engine turns rollouts into improved skill documents, a validation gate decides whether an edit is safe, and a staging queue plus WebUI give a human the final say. Every path is default-safe: proposals are staged rather than written, gates fail closed on a real measurement, and a live skill is only ever written through a snapshotting atomic replace.

## Architecture

```
monologue_end ─▶ logs/rollouts/*.json ─▶ outer loop ─▶ official engine | direct_optimizer
                     └────────────▶ validate_proposal ─▶ staging/ ─▶ drain ─▶ usr/skills/<n>/SKILL.md
```

- **Gate.** `helpers/sleep_runner.validate_proposal`: stage 0 A/B harness, 0.5 per-fragment, 0.7 local replay, 0.75 per-skill policy scope, structural stages, then held-out. A user scope constraint outranks the official engine's quality judgment; a completed official measurement outranks the local numeric gate.
- **Real confirmation gate** (async, single-flight): a detached `scripts/replay_gate_worker.py` re-runs held-out tasks through real A0 monologues and writes a verdict sidecar beside the proposal; a later tick harvests it.

### Layout

| Path | Purpose |
| --- | --- |
| `helpers/` | Engine internals (module list below) |
| `api/` | One module per route group under `/api/plugins/skillopt/*` |
| `tools/` | Agent-callable tools: sleep, status, setup, train |
| `extensions/python/` | Hooks: `agent_init` (loop start/stop), `monologue_end` (harvester), `monologue_start` (warning), `banners`, `job_loop` (hub watchdog) |
| `extensions/webui/` | Head/sidebar HTML injectors |
| `webui/` | Dashboard JS + settings page |
| `scripts/` | Standalone CLI entry points (subprocess-isolated work) |
| `tests/` | `smoke.py`: self-contained deterministic suite, no pytest |
| `staging/` | Queue: top level awaiting drain, plus `adopted/`, `rejected/` |
| `logs/` | `rollouts/`; `runs/` (state, audit logs, critiques, env file) |
| `models/reward_model/` | Trained reward-model artifacts |
| `agents/skillopt_trainer/` | Subordinate agent profile |
| `plugin.py`, `hooks.py`, `plugin.yaml`, `execute.py` | Manifest, version, lifecycle, self-check |

### `helpers/`

- **Loops:** `auto_loop.py` (outer daemon, tick gating, drain, real-gate spawn/harvest), `inner_loop.py` (per-rollout suggestions with skip-retirement).
- **Optimizers:** `official_adapter.py` (probe, drive upstream, classify verdicts), `direct_optimizer.py` (local fallback plus the shared `_call_llm`).
- **Gating:** `sleep_runner.py` (paths, config, subprocess launch, sidecars, `validate_proposal`), `replay_harness.py` (counterfactual replay, mock + real executors), `ab_harness.py` (synthetic A/B, advisory only), `governance.py` (per-skill policy: opt in/out, immutable, pause, scope, approval).
- **Pacing and state:** `cadence.py`, `budget.py` (daily cap per skill), `cycle_history.py` (append-only per-cycle record, compacted), `fragment_store.py` (addressed fragments, whole-file snapshots), `failure_memory.py` (failure attribution into the next prompt).
- **Scoring:** `reward_model.py` (trained model or heuristic), `llm_judge.py` (outcome labelling, burst-protected, never raises), `judge_client.py` (judge provider resolution, connectivity probe), `chat_model.py` (resolves the `chat` sentinel to the active A0 chat model).
- **Plumbing:** `bridge.py` (rollouts as a plugin-local Claude Code history cache), `setup_env.py` (env file as `${VAR}` references, never plaintext secrets).

## Local Contracts

- **Staging is the only path to a live skill.** `find_staged_proposals()` scans staging top level only, so a consume or quarantine must move the proposal *and* its marker/real-gate sidecars. Governance skips stay in staging (the skill may become eligible later); rejects and adopts do not. Only `sleep_runner.adopt_write_skill()` may write a live `SKILL.md` — snapshot, then atomic replace. The agent-callable adopt tool is as exposed as the background loop.
- **Real-gate matrix: fail closed on a measurement, fail open only on its absence.**

  | Sidecar | Outcome |
  | --- | --- |
  | `done`, verdict ok and accepted | adopt |
  | `done`, verdict ok and not accepted | quarantine |
  | `done`, verdict not ok (too few usable pairs, etc.) | quarantine — the worker finished; that is evidence, not absence of evidence |
  | `failed`, stale, unknown status | adopt, with a loud reason tag |

  Fail-open exists so a transient executor outage cannot permanently block the drain, and the risk is asymmetric: a quarantined proposal waits in `staging/rejected/` for an operator to re-approve; an adopted one overwrites a live skill.
- **Never double-count a gate.** `official_gated=True` means upstream already ran its monotonic held-out gate, so stage 8 is skipped and both local gates are bypassed. While the async real gate owns confirmation, stage 0.7 runs the *mock* executor even when `replay_real_executor_enabled` is true — no double spend, no long block in the drain. The mock verdict is advisory unless `replay_mock_enforce` is set; the mock gets its own lift bar, the real executor keeps the full one.
- **Sidecar-first single-flight.** The pending sidecar is written *before* the worker spawns, and adoption skips while any pending sidecar exists, so at most one real gate is in flight and nothing bypasses on flight state. The held-out task list is frozen into a tasks file; the worker never rescans rollouts. Workers take every knob on the command line — a bare `helpers` import cannot see `config.json`, so resolution happens in-process.
- **Recursion guard.** `SKILLOPT_REPLAY_MODE` is process-global and inherited by subprocesses. Set it in parent *and* child before creating the replay `AgentContext`; the harvester and the loop watchdog honour it, so replay turns never enter the training set or spawn a nested loop.
- **Config.** `default_config()` parses `default_config.yaml` with `yaml.safe_load`, so a nested `budget:` / `cadence:` / `governance:` section arrives as a **dict** and `cfg.get("budget", {}).get(...)` works. This matters: the previous flat hand-rolled parser stored a bare section header as the empty string, `.get()` on it raised `AttributeError`, and the surrounding `except` fell through — silently disabling the daily budget cap in production. The flat parser survives only as a no-dependency fallback and must keep **omitting** section headers rather than fabricating `""`. `merged_config()` must also never raise: the framework does not merge `default_config.yaml`, so a wiped `config.json` must still resolve the full YAML defaults, and a failing registry read is treated as empty rather than bubbled.
- **Every declared config key must be read by something.** `t_v1824_config_keys_have_readers` scans each leaf key in `default_config.yaml` against the plugin's own source and fails on any key nothing reads, with an explicit allowlist for the ones known to be dead and a reason each. The allowlist is checked in both directions, so a key that gets wired must be removed from it. A setting that is declared but unread is a switch an operator can flip that does nothing.
- **State files are single-writer.** The auto-loop thread is the only writer of `logs/runs/.auto_loop_state.json`; cadence, budget, cycle-history, and sidecar writes go through write-temp + `os.replace`. Endpoints and the WebUI read. Do not add a second writer — and the test suite must not be one: sandbox the inner-loop state dir, rollouts dir, held-out loader, and budget state dir, and patch `_save_state` to a no-op. A pollution guard test fails the run if fixture-named files appear in live run state.
- **Framework and portability gotchas.** `LoopData` exposes `history_output` / `user_message` / `last_response`, not `messages`; attribute the active skill from `skills.skill_instruction_name`, with the session ledger only as fallback. `start_new_session` crashes on Windows (use `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP` there); `os.kill(pid, 0)` is Ctrl+C on Windows, so liveness is ctypes-based; a Linux zombie (state `Z`) is not running.
- **Safety.** Secrets never enter the plugin tree: the env file holds `${VAR}` references, values live in the project's `usr/.env`, and dry-run output exposes key names and booleans only. Preserve auth and CSRF on every route — there is **no** exception any more. The hub-status route used to be one (read-only public data), but it can spawn `scripts/check_hub_status.py` on a stale payload, so an anonymous caller could trigger process spawns; it is now GET-only with the framework defaults inherited.
- **Before you finish.** Run the suite after *every* edit, not just a compile check: `py_compile` cannot catch a trailing comma that silently wraps a 2-tuple into a 1-tuple, and a broad `except` swallows the resulting unpack error. Keep the version string identical across `plugin.yaml`, `plugin.py`, `hooks.py`, and `execute.py`.

## Work Guidance

- **Agent tools.** `skillopt_sleep(verb=dry-run|run|status|adopt|harvest, skill="")` — `status` and `adopt` run inline, the rest launch a detached cycle and return a pid plus log path. `skillopt_status(detail=summary|rollouts|skills|staged)` is read-only, `skillopt_setup(backend=…, dry_run)` writes env references, `skillopt_train(kind=info|run|validate)` wraps the upstream training loop.
- **HTTP.** One module per group in `api/`; read `config` with GET — POST is a write that whitelists known keys, so an empty-body POST is rejected rather than wiping settings.
- **Sleep loop.** The engine is a one-shot `python -m skillopt_sleep <verb>` subprocess, not a daemon; the plugin decides when it runs. The adapter maps the verb, resolves the target skill to a real `SKILL.md` path, and reads the verdict from the engine's report file rather than scraping logs.
- **Auto loop tick.** Read merged config, log `last_tick_at`, auto-opt-in newly seen skills, and consider a cycle when rollouts since the *persisted* `last_rollout_count_at_cycle` cross the threshold (a per-tick delta never fires on a slow harvest). Per skill: governance eligibility → cadence → budget. Then drain staging: attempt every candidate up to `auto_adopt_max_per_tick`, newest first, never letting one candidate's failure stall the rest.
- **Scripts.** `python scripts/run_sleep_cycle.py` runs judge → official gate → summary and fails fast on a mock backend unless `--allow-mock-backend` is passed. Offline: `label_rollouts.py`, `train_reward_model.py --mode train`, `calibrate_judge.py`, `replay_worker.py`, `replay_gate_worker.py`, `check_hub_status.py`.

## Verification

From the plugin root:

- `python tests/smoke.py` — the deterministic suite (no LLM, no network, no clock dependence; subprocess, LLM, and async paths are mocked). Trust its exit code, not a remembered count.
- `python execute.py` — health check; fails loudly when rollouts clear the threshold but no cycle has ever run.
- `python -c "import skillopt_sleep"` in the A0 venv — confirms the official package is present, otherwise the direct-optimizer path is in use.
- `python scripts/train_reward_model.py --mode smoke` — reward-model plumbing without a training run.
- Live pilot on one `.skillopt.optin` skill: a real chat writes a rollout with a non-empty `skill_used`; the cycle stages a proposal with a gate reason in the cycle history; the drain adopts or quarantines it; the dashboard's audit log agrees.

## See also

- [README.md](README.md) — user-facing docs, config cheat sheet, gate stage list, troubleshooting
- [CHANGELOG.md](CHANGELOG.md) — version history and incident write-ups
- [ROADMAP.md](ROADMAP.md) — planned work
- [default_config.yaml](default_config.yaml) — every setting with comments (flat keys only)
- `helpers/` has no local DOX file; the module list above is its contract.
