# skillopt

> Microsoft SkillOpt text-space skill optimizer, bridged as an Agent Zero self-evolution engine. Harvests the agent's own task rollouts, drives the official `skillopt_sleep` pipeline (or a fallback direct optimizer), gates proposals behind a monotonic validation gate, and stages gated skill edits for human-in-the-loop adoption.

**Version:** 1.8.1 · **Plugin ID:** `skillopt`

## Purpose

Treats `usr/skills/<name>/SKILL.md` documents as trainable text parameters: the engine reads the
agent's own task trajectories (rollouts), groups them by skill, proposes improved skill documents,
and promotes them only through a validation gate. v1.6.0 (Solution B) makes the auto-loop drive the
official Microsoft `skillopt_sleep` pipeline instead of a hand-rolled optimizer, demoting the local
`direct_optimizer` to a fallback used only when the official package is absent. v1.7.0 (Solution C)
fixes the silently-broken rollout harvester (it read a nonexistent `loop_data.messages`), adds a
local counterfactual replay gate (deterministic mock executor + real-executor stub), a
human-in-the-loop adopt UI (Approve/Reject/Rollback with whole-file snapshots), and auto-opt-in for
new skills behind a human-approval guardrail. v1.8.0 implements the two pieces v1.7.0 deliberately
stubbed — both opt-in and default-off (v1.7.0 behavior is preserved byte-for-byte unless an operator
flips the flags): a subprocess-isolated real A0-agent-loop replay executor, and DistilBERT
reward-model training fed by an LLM-judge labelling pass, with a calibration step that picks the
`prefer_model_above` threshold and wires the previously-dead `reward_model_path` /
`reward_model_prefer_above` config keys.

**v1.8.1 (Windows portability):** fixes every hardcoded `/a0/...` container path that made the
plugin silently dead on Windows installs. A0 skills now resolve via
`sleep_runner.a0_skills_dir()` (`<project>/usr/skills`, honoring the `SKILLOPT_SKILLS_DIR`
override) instead of a hardcoded Linux path in 5 consumers (the framework path resolver, the
framework-path fallbacks in 5 helper modules, the official-adapter skill resolver, and the
monologue-start warning banner's last-error file). The logger-rotation helper now recreates an
empty live log after rotating (append-mode writers already recreated it; readers did not). All
paths in user-facing strings and staged-proposal instructions are plugin-relative.

## Architecture (v1.8.1)

- **Two-loop:** outer `AutoLoopThread` (`helpers/auto_loop.py`, 600s) + inner `InnerLoopThread`
  (`helpers/inner_loop.py`, 60s suggestion miner).
- **Official-engine bridge** (`helpers/official_adapter.py`, NEW v1.6.0, **v1.6.1 verified**): probes
  `import skillopt_sleep` in the A0 venv (cached 60s), then reuses `sleep_runner.launch_sleep_subprocess`
  to run one official Sleep cycle (harvest→mine→replay→consolidate→gate→stage). Fail-soft: any infra
  error returns `{ok: False, fallback_to_direct: True}`; an official gate REJECT returns
  `{ok: False, gate_rejected: True}` (the auto-loop does NOT fall back to direct on a reject — the
  official verdict is authoritative). **v1.6.1**: the CLI flag mapping and staging discovery were
  verified against `microsoft/skillopt` @ HEAD — `--project`, `--target-skill-path` (a real SKILL.md
  path), single `--model`, `--backend`, `--lookback-hours`, `--max-tasks`, `--edit-budget`,
  `--preferences`, `--json`; staging lands in `<a0>/.skillopt-sleep/staging/<ts>/` with the authoritative
  gate verdict in `report.json` (read via `_read_gate_verdict`, not log scraping).
- **Fallback optimizer** (`helpers/direct_optimizer.py`): the safety net; runs per-skill only when the
  official package is unavailable or an official run fails. Single full-rewrite LLM call + structural gate.
- **Validation gate** (`helpers/sleep_runner.validate_proposal`): Stages 1–7 structural pre-filter
  (empty, headers, min chars, example block, byte-identical, whitespace-normalised, shrink) always run;
  Stage 8 held-out is SKIPPED when `official_gated=True` (the official engine runs its own monotonic
  held-out gate before staging, so a staged proposal has already passed it by construction). **v1.7.0
  (C2)** adds **stage 0.7 — local counterfactual replay gate** (`helpers/replay_harness.py`): runs only
  when `skill_name AND not official_gated AND replay_local_gate_enabled`, scores the current vs proposed
  skill on the held-out rollouts with a deterministic mock executor (relevance heuristic, no LLM) and
  accepts only on a strict-monotonic lift ≥ `gate_min_improvement_pp` over ≥ `replay_min_n` tasks. **v1.8.0:
  the real A0-agent-loop executor is IMPLEMENTED** behind `replay_real_executor_enabled` (default false) —
  `replay_harness._real_score` shells out to `scripts/replay_worker.py`, which runs each held-out task
  through a real Agent Zero monologue under the given skill in a child process (temp cwd, own event loop,
  `SKILLOPT_REPLAY_MODE=1`) and writes a `{score, outcome, ...}` JSON envelope. Cost is bounded by
  `replay_real_max_tasks` (default 4 — 2×N full monologues per gate call) and `replay_real_per_task_timeout_s`
  (default 180). Any worker failure raises → `real_executor_unavailable:...` (loud-not-crash, falls through
  to the structural gate). The local synthetic A/B "replay" harness is advisory-only and OFF by default.
- **LLM-judge labelling + reward training (v1.8.0, P4–P6):** `helpers/llm_judge.py` classifies a rollout's
  turn outcome into success/partial/failure (aligned with `score_rollout`'s 3-class space) via
  `direct_optimizer._call_llm` with a judge-specific system prompt (never raises). `scripts/label_rollouts.py`
  is the idempotent CLI pass that augments each rollout JSON in place with `judge_label`/`judge_confidence`/
  `judge_reason`/`judge_model`/`judge_at` (atomic `os.replace`). `scripts/train_reward_model.py --mode train`
  loads the labelled rollouts, featurizes them with `reward_model._rollout_to_text` (the SAME featurizer
  inference uses), splits train/val deterministically, runs an AdamW epochs loop, persists the model, writes
  a `1.3.0-train-*` version stamp, and runs a calibration pass. `reward_model._calibrate` sweeps T in
  [0.3, 0.9] under the gated decision rule, writes `calibration.json`, and `score_rollout(prefer_model_above=None)`
  resolves `calibration.json` > `reward_model_prefer_above` config > 0.6 via `_config_prefer_above()`.
  `model_path()` now reads the `reward_model_path` config key (env still wins).
- **Per-skill governance** (`helpers/governance.py`): `opt_out`/`opt_in`/`immutable`/`rate_limited`,
  per-skill `policy.json`, `.skillopt.optout`/`.skillopt.optin` markers. The tick now calls
  `check_skill_eligible()` + `cadence.compute_next_run()` + `budget.can_skill_spend()` before each
  per-skill cycle (previously computed-but-unused). **v1.7.0 (C4)** adds `auto_optin_new_skill()`:
  a brand-new skill seen in rollouts is auto-opted-in (`.skillopt.optin` + `policy.json` with
  `require_human_approval: true`) but stays `require_human_approval_pending` until a human approves it
  via `/governance_approve`. Immutable/opted-out skills are never touched.
- **Cycle history** (`helpers/cycle_history.py`): append-only JSONL; v1.6.0 compaction rotates overflow
  beyond `cycle_history_max_entries` (default 500) to a cold `cycle_history.archive.jsonl`. v1.7.0 (C3)
  records `adopted`/`rejected`/`rolled_back` entries from the adopt/reject/rollback endpoints.
- **Harvester** (`extensions/python/monologue_end/_60_skillopt_harvest_rollout.py`): fires after every
  chat turn, extracts task/trajectory/outcome, writes `logs/rollouts/<id>.json` (no LLM call). **v1.7.0
  (C1) — FIXED:** the v1.1.0 harvester read `loop_data.messages`, which does not exist on `LoopData`
  (the real attributes are `history_output`/`user_message`/`last_response`), so it early-returned on
  every turn and wrote **zero rollouts**. It now reads `history_output` and attributes the active skill
  authoritatively via `skills.skill_instruction_name` (per-turn walk of the output history) with
  `get_loaded_skill_names` as the session-ledger fallback. A `SKILLOPT_REPLAY_MODE` env guard keeps the
  replay agent's own turns out of the training set.
- **Human-in-the-loop adopt UI (v1.7.0, C3):** `/staged` lists proposals with gate evidence
  (lift_pp, n_held_out, gate_reason, diff_summary); `/adopt` takes an optional `proposal_id` (falls
  back to the latest) and snapshots the pre-adopt `SKILL.md` via `fragment_store.snapshot_default`
  (keyed on the skill-name string, not `Path.stem`, to avoid the staged-proposal-stem collision);
  `/reject` records a no (audit-only, does not delete the staged file); `/rollback` restores the most
  recent whole-file `_default` snapshot (reversible — it snapshots the current bytes first).
- **Integration:** lifecycle hooks (`hooks.py`), 5 extension hooks, 13 HTTP API endpoints, 4 agent tools,
  `skillopt_trainer` subordinate agent, WebUI dashboard (`webui/skillopt-dashboard.js` + `config.html`
  with Staged-proposals + Governance sections).

## Ownership / Layout

- `extensions/` — monologue-end harvester, monologue-start warning, post-adopt safety net, banner
- `helpers/` — auto_loop, inner_loop, official_adapter, direct_optimizer, sleep_runner, bridge,
  ab_harness, governance, cadence, budget, fragment_store, failure_memory, reward_model, cycle_history,
  audit_log, config_loader, replay_harness (v1.7.0), llm_judge (v1.8.0)
- `scripts/` — train_reward_model (v1.8.0 `--mode train`), calibrate_judge, replay_worker (v1.8.0),
  label_rollouts (v1.8.0)
- `api/` — adopt, status, config, fragments (+rollback), cycles (+cycle), audit_log, loop, sleep,
  staged, reject, rollback, governance_approve, governance_status (NEW v1.7.0), hub_status (NEW v1.8.13)
- `webui/` — dashboard + config UI (Staged-proposals + Governance sections v1.7.0)
- `tests/smoke.py` — 163 deterministic tests (no LLM/network); the v1.8.0 opt-in paths are covered
  by mocked subprocess / LLM / asyncio cases (no real spawn), and the v1.8.13–v1.8.16 additions
  (5 hub_status + 2 multi-keyword scorer + 6 judge burst-protection cases) are fully mocked as well.

## Local Contracts

- Skill edits are STAGED, never auto-applied unless `auto_adopt: true` (default false). With
  `auto_adopt: false` the user reviews each staged proposal in the WebUI (Staged-proposals section,
  v1.7.0) before promotion. `/adopt` snapshots the pre-adopt `SKILL.md` so `/rollback` can reverse it.
- The official engine runs its own monotonic held-out gate before staging; the local gate adds a cheap
  structural pre-filter, a local counterfactual replay gate (stage 0.7, mock executor), and delegates
  the held-out decision when `official_gated=True`.
- `use_official_engine: true` is the v1.6.0 default; when the official package is absent the loop
  silently falls back to `direct_optimizer` (logged) — it can never make things worse.
- The synthetic A/B replay harness is ADVISORY ONLY and OFF by default (`ab_harness_enabled: false`);
  opt in via the `SKILLOPT_AB_HARNESS_ENABLED=1` env var for the harness-functionality smoke tests.
- **v1.7.0 (C4) guardrail:** `governance.default_policy.require_human_approval: true` is the safe
  default — adoption is one-click, never silent. New skills are auto-opted-in but stay
  `require_human_approval_pending` until a human approves via `/governance_approve`.
- **Recursion guard:** `SKILLOPT_REPLAY_MODE` (env, process-global, inherited by subprocesses) is
  checked in the harvester, the auto-loop watchdog, and set/unset around the real replay executor
  (parent + child both set it before creating the replay `AgentContext`), so a replay agent's own
  turns never pollute the training set or spawn a nested optimizer loop.
- **v1.8.0 opt-in guardrail:** the real replay executor and the trained reward model are BOTH
  default-off. `replay_real_executor_enabled: false` → the gate uses the mock executor (byte-identical
  to v1.7.0). No trained model on disk → `score_rollout` uses the heuristic fallback (byte-identical to
  v1.7.0). An operator must explicitly flip the flag / run the training script to activate them.

## v2.5 Status

- v2.5 banner CTA changed from `open-plugin-config:skillopt` (dead in v2.5) to
  `open-modal:/usr/plugins/skillopt/webui/config.html`.
- v1.6.0 (Solution B): official-engine bridge + gate delegation + per-skill tick gating +
  version alignment (plugin.py/hooks.py/plugin.yaml all 1.6.0) + cycle_history compaction +
  A/B harness advisory demotion. 92/92 smoke tests pass.
- v1.6.1 (verified): CLI flag mapping + staging discovery + `evaluate_gate` signature verified
  against `microsoft/skillopt` @ HEAD. Fixed the adapter: `--skill`→`--target-skill-path`,
  dropped nonexistent `--optimizer-model`/`--target-model` (single `--model`), added
  `--project`/`--edit-budget`/`--preferences`/`--json`, staging via `_find_staging_dir` +
  `report.json` gate verdict, `gate_rejected` contract (no direct fallback on official reject).
  100/100 smoke tests pass.
- v1.7.0 (Solution C — Core): C1 fixed the broken harvester (ground-truth skill attribution via
  `history_output` + `skill_instruction_name`); C2 added the local counterfactual replay gate
  (deterministic mock executor + real-executor stub, stage 0.7); C3 added the human-in-the-loop
  adopt UI (`/staged` + `/adopt` by id + `/reject` + `/rollback` with whole-file snapshots); C4
  added auto-opt-in for new skills behind a human-approval gate (`/governance_approve` +
  `/governance_status`). 122/122 smoke tests pass.
- v1.8.0 (the two offline pieces, integrated): P1 `scripts/replay_worker.py` (subprocess-isolated
  A0-agent-loop replay); P2 `replay_harness._real_score` shells out to it (blocking `subprocess.run`,
  `build_subprocess_env` factored out of `launch_sleep_subprocess`, `replay_real_max_tasks` cost cap);
  P3 `sleep_runner` stage-0.7 call-site passes `executor="real"` when the flag is on; P4
  `helpers/llm_judge.py` + `scripts/label_rollouts.py` (LLM-judge outcome labelling pass, idempotent +
  atomic); P5 `scripts/train_reward_model.py --mode train` (real DistilBERT training loop + dataset
  loader reusing `reward_model._rollout_to_text`); P6 `reward_model._calibrate` + `model_path` /
  `score_rollout` config wiring (`_config_prefer_above` reads `calibration.json` > config > 0.6). Both
  opt-in, default-off. 133/133 smoke tests pass. Live checks (L1–L4) pending the A0 venv + LLM creds.

## Verification

- `python tests/smoke.py` — 163 deterministic tests (no LLM/network); the v1.8.13–v1.8.16 additions
  (5 hub_status + 2 multi-keyword scorer + 6 judge burst-protection cases) mock subprocess / LLM /
  asyncio so no real spawn or network happens in the suite. The v1.8.15 watchdog is covered by
  acceptance checks (refresh / throttle skip / timeout / single-flight / loader discovery), not
  smoke cases.
- `python -c "import skillopt_sleep"` in the A0 venv confirms the official package (else fallback).
- Dry-run against the 5 synthetic rollouts with `use_official_engine: true`, `auto_adopt: false` → a
  proposal lands in `staging/` with a gate reason recorded in `cycle_history.jsonl`.
- One real `.skillopt.optin` pilot skill: full harvest→cycle→gate→stage; inspect the dashboard audit
  log. Then `auto_adopt: true`; confirm `usr/skills/<name>/SKILL.md` is overwritten, a fragment
  snapshot is written, and `post_adopt.log` records the safety-net re-validation. Roll back via the
  fragment store; confirm the live skill restores.
- **v1.7.0 live checks:** (C1) a real chat turn writes a `logs/rollouts/<id>.json` with a non-empty
  `skill_used` from `get_loaded_skill_names` (the rollouts dir was empty before the fix); (C2) on the
  direct path a mock-replay-accepted proposal shows `replay_gate` in the gate reason, a no-lift one is
  `replay_gate_rejected`; (C3) `/staged` returns lift/diff, `/adopt <id>` writes a `_default` snapshot,
  `/rollback` restores the pre-adopt bytes; (C4) a brand-new skill gets `.skillopt.optin` +
  `.policy.json` (`require_human_approval: true`) automatically and is `require_human_approval_pending`
  until `/governance_approve` is called, then `eligible`.
- **v1.8.0 live checks (L1–L4, require A0 venv + running A0 server + LLM creds — NOT run in dev):**
  (L1) with `replay_real_executor_enabled: true` + ≥3 held-out rollouts, `validate_proposal` on a
  staged proposal launches the worker, runs the monologue, writes a score JSON, and `_real_score`
  returns a float; (L2) `python scripts/label_rollouts.py --limit 5` writes `judge_label` on 5
  rollouts, a second run skips all 5 (idempotent); (L3) with ≥50 labelled rollouts,
  `python scripts/train_reward_model.py --mode train --epochs 1` writes `models/reward_model/` +
  `skillopt_reward_version.json` (`1.3.0-train-*`) + `calibration.json`, and `get_model_status()`
  shows `model_loaded: true`; (L4) after training, a direct-optimizer cycle uses the real executor +
  the trained reward model, with the verdict in `cycle_history.jsonl`.

## VERIFIED API NOTE (v1.6.1)

The `skillopt_sleep` CLI surface and the `evaluate_gate` signature were verified against the
upstream `microsoft/skillopt` source tree @ HEAD (2026-08-10) by shallow-clone + introspection.
The package is NOT installed in this dev env, so the live subprocess path is exercised only when
the user installs it — but the arg mapping, staging discovery, and gate-verdict reading in
`helpers/official_adapter.py` match the real package (not a guess):

- **CLI**: `python -m skillopt_sleep <subcommand>` — subcommands `run` / `dry-run` / `status` /
  `adopt` / `harvest` / `schedule` / `unschedule`. `run` flags: `--project`, `--target-skill-path`
  (a real SKILL.md path), `--backend mock|claude|codex|copilot|cursor|pi|handoff|azure_openai`,
  single `--model`, `--lookback-hours`, `--max-tasks`, `--edit-budget`, `--preferences`, `--json`.
- **Staging**: `<project>/.skillopt-sleep/staging/<ts>/` with `proposed_SKILL.md`, `report.json`,
  `report.md`, `manifest.json`. The authoritative gate verdict is in `report.json`:
  `{accepted, gate_action, baseline_score, candidate_score, night, edits}`.
- **evaluate_gate**: `evaluate_gate(candidate_skill, cand_hard, current_skill, current_score,
  best_skill, best_score, best_step, global_step, *, cand_soft=0.0, metric="hard",
  mixed_weight=0.5) -> GateResult` — action in `{accept_new_best, accept, reject}`. Both
  `skillopt_sleep.gate` (vendored) and `skillopt.evaluation.gate` (reference) are behaviourally
  identical. We do NOT call it authoritatively — the engine already ran it before staging; the
  verdict is read from `report.json`.

Remaining live follow-up: install `skillopt`/`skillopt_sleep` into the A0 venv and run one real
`run` end-to-end (with a configured backend) to confirm the bridge works against the installed
version, not just the source tree.

## HUB STATUS + WATCHDOG + JUDGE BURST (v1.8.13–v1.8.16, source-verified)

- **REST endpoint** — `api/hub_status.py`: `GET|POST /api/plugins/skillopt/hub_status` serves the
  `{latest, history}` payload persisted by `scripts/check_hub_status.py` to `logs/hub_status.json`
  (latest.status in OPEN_PENDING / MERGED_INDEXED / MERGED_UNINDEXED, or a probe-reported ERROR
  served as honest data with HTTP 200). Execution cache: a fresh payload (file mtime age <= 3600 s)
  is returned as-is with no spawn. Stale/missing triggers exactly one refresh: `sys.executable
  scripts/check_hub_status.py` as an asyncio subprocess under a hard 3 s timeout (killed on
  overrun; the server loop never parks on network timeouts). Concurrent requests share a single
  refresh via a process-wide `threading.Lock` guard (losers poll file freshness). Refresh failure
  returns HTTP 500 `{status: ERROR, message}` (no tracebacks). Auth/CSRF relaxed (read-only public
  status); Flask imported lazily (CI dict fallback carries `http_status`).
- **Background watchdog** — `extensions/python/job_loop/_80_skillopt_hub_watchdog.py`
  (`HubWatchdogExtension`, v1.8.15): fired from the framework scheduler's job_loop tick
  (`scheduler_tick()` → `call_extensions_async("job_loop")`), never the main chat thread. Mtime
  throttle `THROTTLE_S = 1800.0`: the probe runs only when `logs/hub_status.json` is older than
  1800 s (or missing); subprocess hard cap `SUBPROCESS_TIMEOUT_S = 3.0` (child killed on overrun);
  single-flight module flag (deliberately not `asyncio.Lock`, which binds to the first awaiting
  loop); broad try/except so background errors are logged and never propagate into the job loop;
  stdlib only. On merge, the first tick past the throttle window records MERGED_INDEXED /
  MERGED_UNINDEXED and the endpoint surfaces the transition within one throttle window.
- **Judge burst protection** — `helpers/llm_judge.py` (v1.8.16), judge calls only
  (direct_optimizer untouched; `judge_outcome` never-raises contract preserved): (1) in-flight
  limiter `threading.BoundedSemaphore`; (2) reservation pacing — `_throttle_wait()` reserves slot
  starts under a lock so consecutive HTTP attempts stay >= `SKILLOPT_JUDGE_THROTTLE_S` apart
  (replaces the racy read-sleep-write stamp); (3) backoff retries for 429 / rate limit, 5xx,
  timeouts, connection errors with half-to-full jitter + `Retry-After` honor; non-retryable fail
  fast; exhaustion re-raises into the wrapper → `{label: None, error}`. Env knobs (env-only, not
  config keys): `SKILLOPT_JUDGE_MAX_CONCURRENCY` (default 1), `SKILLOPT_JUDGE_THROTTLE_S` (1.5,
  0 disables; knob since v1.8.11), `SKILLOPT_JUDGE_RETRY_MAX` (3), `SKILLOPT_JUDGE_RETRY_BASE_S`
  (0.5), `SKILLOPT_JUDGE_RETRY_MAX_S` (8.0). Telemetry: `llm_judge._retry_stats`;
  `_reset_burst_state()` for test isolation.
- **Suite counts** — 163 deterministic tests: 150 (v1.8.12) → 155 (v1.8.13, +5 hub_status) →
  157 (v1.8.14, +2 multi-keyword scorer) → 163 (v1.8.16, +6 burst). All v1.8.13–v1.8.16 smoke
  additions are mocked (no real spawn/network). The v1.8.15 watchdog is verified by acceptance
  checks (refresh + throttle skip both directions, instrumented timeout, single-flight spawn
  count, loader discovery), not by smoke cases.

## See also

- `plugin.yaml` — manifest (name, version, settings_sections, per_project_config, per_agent_config)
- `default_config.yaml` — defaults incl. the OFFICIAL ENGINE BRIDGE (v1.6.0) section
- `helpers/official_adapter.py` — the Solution B bridge (probe + run_official_sleep_cycle)
- `helpers/direct_optimizer.py` — the fallback optimizer (v1.6.0 FALLBACK ROLE)
- `api/hub_status.py` — hub merge/indexing status endpoint (v1.8.13; 3600 s execution cache, 3 s refresh cap)
- `extensions/python/job_loop/_80_skillopt_hub_watchdog.py` — background job_loop watchdog (v1.8.15; 1800 s mtime throttle)
- `scripts/check_hub_status.py` — hub PR #512 probe persisting `logs/hub_status.json`
- `README.md` — user-facing docs
- Framework references: `helpers/plugins.py` (lifecycle), `helpers/api.py` (API dispatch),
  `helpers/ui_server.py` (asset serving)

## Version history

- 1.0.0–1.5.0 — hand-rolled engine: two-loop architecture, structural gate, synthetic A/B harness,
  fragment store, failure memory, governance, cycle history (never ran: `cycles_run: 0`).
- 1.6.0 — Solution B: bridge to official `skillopt_sleep`; direct_optimizer demoted to fallback;
  gate delegates held-out to the official engine (`official_gated`); per-skill cadence/budget/
  governance wired into the tick; version alignment; cycle_history compaction; A/B advisory-off.
- 1.6.1 — verified the `skillopt_sleep` CLI + `evaluate_gate` signature against `microsoft/skillopt`
  @ HEAD; fixed the adapter arg mapping (`--target-skill-path`, single `--model`, `--project`,
  `--edit-budget`, `--preferences`, `--json`), staging discovery, and `report.json` gate verdict.
- 1.7.0 — Solution C (Core): C1 ground-truth skill attribution (fixed the broken harvester that wrote
  zero rollouts); C2 local counterfactual replay gate (deterministic mock executor + real-executor
  stub, stage 0.7); C3 human-in-the-loop adopt UI (`/staged` `/adopt` `/reject` `/rollback` with
  whole-file snapshots); C4 auto-opt-in for new skills behind a human-approval gate
  (`/governance_approve` `/governance_status`). 122/122 smoke tests. Out-of-scope stubs (documented
  follow-ups): real A0-agent-loop replay executor + DistilBERT reward-model training.
- 1.8.0 — the two v1.7.0 stubs, integrated (both opt-in, default-off): P1 `scripts/replay_worker.py`
  (subprocess-isolated A0-agent-loop replay, temp cwd + own event loop + `SKILLOPT_REPLAY_MODE`);
  P2 `replay_harness._real_score` shells out to it (blocking `subprocess.run`, `build_subprocess_env`,
  `replay_real_max_tasks` cost cap); P3 stage-0.7 call-site passes `executor="real"` when enabled;
  P4 `helpers/llm_judge.py` + `scripts/label_rollouts.py` (LLM-judge outcome labelling, idempotent +
  atomic, advisory judge-vs-heuristic agreement %); P5 `scripts/train_reward_model.py --mode train`
  (real DistilBERT training loop, dataset loader reusing `reward_model._rollout_to_text`, deterministic
  stratified split, AdamW, `1.3.0-train-*` stamp); P6 `reward_model._calibrate` + `model_path` /
  `score_rollout` config wiring (`_config_prefer_above`: `calibration.json` > `reward_model_prefer_above`
  > 0.6; `reward_model_path` config now read, env wins). `direct_optimizer._call_llm` gained an optional
  `system` param so the judge reuses it. 133/133 smoke tests. Live checks L1–L4 pending A0 venv + LLM.
- 1.8.1–1.8.12 — see `CHANGELOG.md` (env-file indirection + setup-env sanitization, governance
  pause, scorer size-invariance + judge throttle, chat-model sentinel, hermetic test isolation;
  suite grew 133 → 150).
- 1.8.13 — hub_status REST endpoint (`api/hub_status.py`): GET|POST
  `/api/plugins/skillopt/hub_status` serving `logs/hub_status.json` with a 3600 s mtime execution
  cache, single-flight 3 s watchdog refresh, structured HTTP 500 on refresh failure; +5 smoke
  tests (suite 155).
- 1.8.14 — multi-keyword replay scorer parity tests (+2, suite 157): task-side coverage ratio
  (shipped in v1.8.11) pinned by an exact-score ladder + end-to-end gate acceptance on
  multi-keyword tasks; no scorer change (a literal keyword-set-size ratio would reintroduce the
  45→81 dilution defect).
- 1.8.15 — hub watchdog background job_loop extension
  (`extensions/python/job_loop/_80_skillopt_hub_watchdog.py`): 1800 s mtime throttle, non-blocking
  asyncio refresh of `logs/hub_status.json`, 3 s subprocess cap, single-flight, never raises into
  the job loop.
- 1.8.16 — judge burst protection in `helpers/llm_judge.py`: in-flight limiter + reservation
  pacing + 429/5xx backoff retries (`SKILLOPT_JUDGE_MAX_CONCURRENCY`, `SKILLOPT_JUDGE_THROTTLE_S`,
  `SKILLOPT_JUDGE_RETRY_MAX`, `SKILLOPT_JUDGE_RETRY_BASE_S`, `SKILLOPT_JUDGE_RETRY_MAX_S`); +6
  smoke tests (suite 163). Tag `v1.8.16` (d679385); hub PR #512 manifest references this release
  line in its head commit (e0d6d32).

## 1.8.17 Autonomy Failure Audit + Remediation Roadmap (2026-09-22)

Live audit found the plugin **never ran one autonomous cycle** in production
(`logs/runs/.auto_loop_state.json`: `cycles_run=0`, `proposals_adopted=0`). Harvesting works
(158 rollouts); everything downstream is dead. Full evidence + report:
`<a0>/tmp/skillopt-autonomy-analysis-2026-09-22.md` (mirrored in the repo-root `CLAUDE.md`).
Eight root causes:

1. **Config wipe (RC1):** `webui/config.html` `refresh()` calls `api('/config')`, whose default
   method is POST, with no body → `api/config.py` treats any POST as a save and writes `{}` to
   `config.json` on every page open (verified mtime 2026-09-22T17:24). With `config.json={}`,
   `get_plugin_config` returns `{}` (the framework does NOT merge `default_config.yaml`) and the
   `agent_init` `_get_config` fallback reads only `config.json` → the auto-loop thread receives
   empty config and spins forever on `if not cfg: _sleep(30)` with zero logs (outer loop silent
   since 2026-09-16).
2. **Trigger (RC2):** a cycle requires ≥10 NEW rollouts per 30-min tick; harvest yields ~25/day →
   never fires (`new_rollouts=3 < threshold=10`, last tick 2026-09-16).
3. **Governance chicken-and-egg + approval deadlock (RC3):** auto-optin only runs inside the
   (never-firing) cycle branch; `require_human_approval: true` (default + stamped into every
   auto-optin policy) blocks per-skill until a dashboard Approve; `auto_adopt: true` does not
   bypass it — no configuration yields full autonomy.
4. **Gates (RC4):** the mock counterfactual gate rejected the one real staged proposal (lift
   2.08pp < `gate_min_improvement_pp` 5.0); the real replay executor times out (one A0 monologue
   201–360s vs 2×N tasks inside 600s, plus live-server contention); the official engine runs with
   `--backend` defaulting to mock → `held-out 0.000 -> 0.000 => reject` every run.
5. **Inner loop (RC5):** reward class-confidence ~0.36 < `inner_loop_min_rollout_confidence` 0.4
   skips every rollout, and skipped rollouts are never marked processed → eternal churn
   (`scanned=50 skipped=50` every 60s); `InnerLoopThread.run` passes the nonexistent
   `cfg["llm_endpoint"]` key instead of `inner_loop_llm_endpoint` → stub suggestions only.
6. **Official-engine bridge (RC6):** `--backend` omitted ⇒ engine default mock; rollout bridge
   logged `bridge: 0 rollouts` on the 2026-09-15 run. The direct optimizer remains the only path
   that ever staged a real proposal (2026-09-19).
7. **Test pollution + false-done signal (RC7):** smoke fixtures leak into production state
   (`skillA/skillB` governance entries, `v121_*` ab_harness spam, `c2_replay_skill_5_test_fixture_*`
   suggestions); 163 green tests cover components only — no integration test runs the live loop
   end-to-end, which is how "done / waiting for merge" (hub PR #512, still OPEN_PENDING) happened.
8. **Silent-failure UX (RC8):** spinning on empty config produces no log line and no dashboard
   signal; `state["running"]` is `true` from thread start regardless of tick success.

### Remediation roadmap (approved 2026-09-22; execution order P0→P4)

- **P0 Config integrity:** `webui/config.html` `refresh()` reads via GET; `api/config.py` POST
  guards empty bodies + whitelists known keys; `agent_init._get_config()` merges
  `default_config.yaml` + `config.json` (+ framework overlay) so empty/missing config can never
  silence the loop; persist operator intent (`auto_adopt: true`); dashboard surfaces loop
  liveness + config-read state.
- **P1 Trigger:** per-skill "new rollouts since last cycle" (persisted) instead of per-tick
  threshold; `_maybe_auto_optin` runs every tick (out of the cycle branch); one-time seeding of
  optin markers for existing skills; every tick logs + `last_tick_at` persisted in state.
- **P2 Adoption autonomy:** `auto_adopt: true` overrides `require_human_approval` (operator's
  explicit autonomy opt-in; per-skill optout/immutable/pause still win; adopt burst cap reusing
  the budget tracker).
- **P3 Real gates:** mock counterfactual gate demoted to advisory (or `gate_min_improvement_pp`
  ~1 for the mock executor); real replay executor gets `2×max_tasks×per_task_timeout` cycle
  budgets + async staging→gating; official engine only stays primary if a real `--backend`
  produces one accepted run, else direct optimizer is primary; inner loop routes suggestions
  through the A0 chat-model sentinel and fixes the endpoint key + confidence semantics.
- **P4 Hygiene:** integration smoke test running the live loop end-to-end with `config.json={}`
  present (regression for the silent-spin class); all test fixtures redirected off production
  state; fixture pollution purged; docs/hub framing corrected re PR #512.

**Definition of done:** with `auto_adopt: on` and zero human input, within 24h of normal usage:
≥1 tick per eligible skill → ≥1 cycle → ≥1 staged proposal from real rollouts → passes a real
gate → adopted into `usr/skills/<name>/SKILL.md`, every step visible on the dashboard, surviving
container restart without losing settings.

## 1.8.18 Implementation status — P0 + P1 + P2.1 code-complete (2026-09-22, 163/163 smoke)

Applied after the §1.8.17 audit, in this order (P-number = roadmap entry):

- **P0.1–P0.5** (previous session): GET config + empty-POST read-guard +
  whitelist (`api/config.py`), WebUI GET refresh + status surfacing
  (`webui/config.html`), `_get_config()` → `sleep_runner.merged_config()`
  (extension), operator-intent `config.json` persisted, loop-tick
  telemetry (`last_tick_at`, `cfg_empty_since`).
- **P2.1**: `governance.check_skill_eligible(skill, *, auto_adopt=False)`
  - step 7 pending-approval block is overridden by an explicit
    `auto_adopt: true` (per-skill opt-out / immutable / pause still win).
- **P1**: `_tick` trigger = rollouts since the PERSISTED
  `state["last_rollout_count_at_cycle"]` (only advances when a cycle
  actually runs) instead of the in-memory per-tick delta; `_maybe_auto_optin`
  runs on EVERY tick with new rollouts, not only inside the cycle branch;
  stale `AutoLoopThread._last_rollout_count` removed.
- **P1 hotfix during verification**: the first P2.1 caller edit ended
  `governance.check_skill_eligible(...)` in a trailing comma, wrapping the
  2-tuple result in a 1-tuple; the `eligible, gov_reason =` unpack then
  raised "not enough values to unpack (expected 2, got 1)" on every
  `_auto_adopt` call - swallowed by the governance try/except, so no
  decision rows were ever logged and the gate fell through (caught by
  `t_v150_governance_auto_loop_skip`; fixed + comment left at the site).
  Lesson: `py_compile` cannot catch a tuple-wrapping comma - run the smoke
  suite after EVERY code edit, not just after compile checks.

Remaining: **P3** (mock-gate demotion, inner-loop confidence/`llm_endpoint`
fixes, replay-executor budgets) and **P4** (live-loop integration test,
log pollution purge, PR #512 framing). `config.json` currently carries two
deliberate deviations to re-visit: `auto_loop_min_rollouts: 3` (backlog
eager-fire; with P1 persisted semantics this ≈ one cycle per ~3 rollouts)
and `use_official_engine: false` (mock backend always rejects; direct
## 1.8.19 Implementation status — P3 + P4 complete (2026-09-22, 166/166 smoke)

### P3 — real gates (all landed)
- **Mock-gate demotion**: with `replay_real_executor_enabled` false, stage
  0.7 runs the mock executor against a dedicated
  `replay_mock_gate_min_improvement_pp: 1.0` bar (the 5pp real-data bar
  rejected every honest proposal at 2.08pp noise). Mock counterfactual
  gate (stage 0 A/B harness) is advisory-only by default;
  `SKILLOPT_AB_HARNESS_ENABLED` opts a caller in; `validate_proposal`
  gains `ab_harness_enabled` kwarg and the auto-loop passes it (was a
  dead guard). `ab_harness._config()` failure-fallback fixed (was
  inverted-True on config-read failure).
- **Inner loop (RC5)**: `inner_loop_min_rollout_confidence` 0.4 -> 0.25
  (class confidence ~0.36 skipped everything forever);
  `inner_loop_skip_retire_after: 3` — a rollout skipped 3x on confidence
  is retired with a no-op suggestion (kills the eternal 50-scan rescan);
  consecutive-skip counters persisted in logs/runs/.inner_loop_skips.json.
- **Real replay executor (RC4)**: pairwise per-task failure handling —
  a failed task is dropped with an error envelope, gate proceeds on
  usable pairs, hard-rejects below `replay_min_n` (3). Budget documented
  in default_config.yaml: 2 x replay_real_max_tasks x
  replay_real_per_task_timeout_s.
- **Live verification post-restart**: cycles_run 0 -> 12+, inner loop
  drained the eternal backlog (scanned=50 skipped=50 -> suggested=50 ->
  scanned=0), governance logging real decision rows.

### Live bugs found + fixed after restart
- **Budget config bug** (auto_loop.py, both `_should_run_skill` and
  `_mark_skill_cycle`): `cfg.get("budget", {})` returns a STRING — the
  nested `budget:` YAML section is mangled by merged_config()'s flat
  light-parse (bare key stored as "") — so `.get()` raised AttributeError
  on every cycle. In _should_run_skill the except "falls through", which
  silently DISABLED the daily budget cap; in _mark_skill_cycle spend was
  never recorded. Fix: accept dict only.
- **Registry-raise gap** (sleep_runner.merged_config): a RAISING
  framework-registry read bubbled out of merged_config() into the bare
  config.json parity fallback, which is {} on a wiped config.json — the
  exact v1.8.17 silent-spin input. Now guarded (failed registry read
  treated as empty; YAML defaults always win).

### P4 — hygiene (all landed)
- **Test isolation from production state (RC7, three live leaks found)**:
  1. `inner_loop.state_dir` module hook — inner_loop.log and
     .inner_loop_skips.json land in a tmp sandbox under tests; the suite
     was writing one "simulated LLM outage" row into the production
     inner_loop.log on EVERY run, and `reset_for_tests()` unlinked the
     PRODUCTION skip-counters file (now unlinks only under an active
     override).
  2. `_write_fake_rollouts` wrote fixtures into the PRODUCTION
     logs/rollouts/ via write_rollout() — shared with the live server
     process over 9p; after the P3 fix made the live tick functional it
     started scanning fixtures mid-suite and enqueuing real suggestions
     for them (8x v121_skill_gate_loser_test_fixture_*.md, proven
     2026-09-22). Now writes to a sandbox + patches ab_harness._rollouts_dir
     AND sleep_runner.list_rollouts (the stage-0.7 held-out loader reads
     the latter — missing that patch broke the C2 rejection test).
  3. Budget tests wrote production budget__test_skill_*.json state
     (budget_over has no cleanup) — all 7 BudgetTracker constructions now
     pass a sandboxed state_dir.
- **Regression guard tests (run last)**:
  `t_p4_empty_config_yields_defaults` — with an empty registry (== wiped
  config.json) merged_config() AND the agent_init extension's
  _get_config() must still return the full YAML defaults (the v1.8.17
  silent-spin class). `t_p4_no_fixture_pollution` — fails if
  fixture-named files appear in production run state (archives exempt).
- **Pollution purged**: 45 fixture suggestion files (v121_*/v122_*/
  v18_callsite/c2_replay), budget__test_skill_v122_budget_over.json,
  59 "simulated LLM outage" rows filtered from inner_loop.log; debug
  consoles + live_cycle_verdict JSONs moved to
  logs/runs/_debug_archive_20260922/.

### Open items
- PR #512 framing: description must be rewritten honestly (v1.8.17
  shipped "component-tests pass" with zero live autonomous cycles; now
  167/167 incl. integration + pollution guards, live loop cycling).
- config.json deviations to re-visit: `auto_loop_min_rollouts: 3`,
  `use_official_engine: false`.

## 1.8.20 Implementation status — gated sleep-cycle runner live (2026-09-23, 167/167 smoke)

- **Gated cycle runner:** new `helpers/judge_client.py` +
  `scripts/run_sleep_cycle.py` — ingestion -> judge (samples rollouts
  through the configured judge endpoint, escalates max_tokens on JSON
  parse failures) -> official gate -> summary. Fail-closed, no ungated
  fallback.
- **P5 zombie-aware liveness:** `sleep_runner.is_running` treats
  `/proc/<pid>/stat` state Z as not-running (Linux). An
  exited-but-unreaped detached engine child answers `os.kill(pid, 0)`
  and spun the official-gate poll loop until the full timeout even
  though report.json was already written. Verified live: poll broke 2.1s
  after engine exit instead of 900s. Regression test
  `t_v1820_zombie_aware_is_running` (real fork-produced zombie; the
  changelog's promised case had NOT landed before the 2026-09-23
  freeze — added post-freeze).
- **P6 no-proposal classification:** `official_adapter` maps a
  gate-verdict night that staged no proposal (edits=[] under the mock
  backend) to `gate_rejected` (exit 3) instead of INFRA_FAILED.
  Fail-closed: nothing adopted, live SKILL.md untouched, official
  staging preserved.
- **Folded in deployed-but-uncommitted hardening:** `bridge.py`
  dual-layout import (framework layout with plugin-root fallback);
  `sleep_runner` v1.8.19 P3/RC4 (`validate_proposal` honours
  caller-resolved `ab_harness_enabled`; mock replay gate gets its own
  bar) and P4 (a raising registry config read falls back to the YAML
  defaults); `tests/smoke.py` hermetic `llm_model` pin in the v1.8.12
  failing-LLM case.
- **Live-cycle proof (10:52):** judge 3/3 valid labels; engine header
  shows the plugin-local `--claude-home` redirect with 120 sessions /
  40 tasks harvested; summary `GATE_REJECTED` exit 3.
- **Post-freeze load state (2026-09-23):** v1.8.20 verified loaded live
  in the container (`plugin.PLUGIN_VERSION` == 1.8.20); auto-loop
  healthy across the freeze/restart — `cycles_run` 23, inner-loop ticks
  every ~64s, gates fail-closed (15 proposals rejected, direct engine).
  No further restart pending.
- Smoke: 1 new case `t_v1820_zombie_aware_is_running`; suite 167/167.
  CHANGELOG backfilled with the missing [1.8.18]/[1.8.19] entries
  (docs-only; derived from §1.8.18/§1.8.19 above).

## 1.8.21 Implementation status — staging drain: head-of-line + quarantine + mock guard (2026-09-23, 170/170 smoke)

Fixes the three blockers found by the 2026-09-23 morning live audit
(cycles_run 23, `passed: true` count in adoptions.log = 0, three
proposed mechanisms identified):

1. **Head-of-line blocking fixed:** `_auto_adopt` examined only
   `staged[0]` (newest mtime) and returned on the first governance-skip
   or gate-reject - one malformed proposal permanently blocked every
   staged proposal behind it, and throughput was one adoption attempt
   per 30-min tick. Now every candidate gets an attempt per tick,
   bounded by the new `auto_adopt_max_per_tick` (default 5, newest
   first); a per-candidate exception can never stall the drain; a
   `staged drain: attempted=.. adopted=.. quarantined=.. skipped=..`
   summary is logged when >1 candidate is attempted.
2. **Quarantine + consume:** new `sleep_runner.quarantine_staged_proposal`
   / `consume_staged_proposal` move rejected proposals to
   `staging/rejected/` and adopted ones to `staging/adopted/` (the
   official-gate marker sidecar travels along; adopted proposals keep
   the v1.8.1 marker-clear-first semantics so `find_staged_proposals`
   - which scans staging top-level only - can never re-see them).
   Previously rejects AND adopts sat in staging and were re-attempted
   forever (identical bytes fail deterministically). Governance skips
   STAY in staging: the skill may become eligible later. The live
   malformed Sep-22 proposal (`security-scan-untrusted-plugin.md`, zero
   `#` headers, re-rejected 14x over ~14h while 6 well-formed proposals
   waited) was purged to `staging/rejected/` at deploy time.
3. **Mock-backend guard (P3 enforcement):** `scripts/run_sleep_cycle.py`
   fails fast (exit 1 `INFRA_FAILED`) when `official_backend` is unset
   or `"mock"`, unless `--allow-mock-backend` is passed for a deliberate
   test run. Live proof this was pure waste: 40 tasks "replayed" in
   1.1s, held-out 0.2 -> 0.2, tokens_used=0, GATE_REJECTED every time.

- Test-isolation note (RC7 class): the drain tests patch
  `auto_loop._save_state` to a no-op - it writes the passed dict to the
  PRODUCTION `.auto_loop_state.json`.
- Smoke: 3 new cases (drain end-to-end: adopt consumed + reject
  quarantined + sidecar travel; governance skip stays in staging;
  runner mock-guard); suite 170/170. Version parity across plugin.yaml
  / plugin.py / hooks.py / execute.py.
- Committed `63249ed`, tag `v1.8.21`, pushed.
- Restart REQUIRED to load v1.8.21 live (drain + quarantine are
  auto-loop-thread code; the manual-runner guard is subprocess-fresh).
- **POST-RESTART PROOF (12:29, first tick on v1.8.21):** the drain ran
  `attempted=5 adopted=1 quarantined=4 skipped=0` on the real queue —
  **FIRST AUTONOMOUS ADOPTION EVER**: `agent-zero-api-handler-routing`
  passed the gate (adoptions.log `passed: true`, reason ok) and was
  written to `usr/skills/agent-zero-api-handler-routing/SKILL.md`
  (2841 bytes); the proposal consumed to `staging/adopted/`; the 4
  other staged proposals quarantined to `staging/rejected/` with real
  gate verdicts (3x replay_gate_rejected: rejected_regression, 1x
  rejected_no_lift). Zero human input. This is the §1.8.17
  definition-of-done milestone (≥1 full autonomous adopt).
## 1.8.22 part 2 — async real-executor confirmation gate + advisory mock (2026-09-24, 183/183 smoke)

Completes P3's residual: the adoption-throughput problem. Live evidence
(2026-09-23 drain on v1.8.21): 5 of 7 real-drain rejects were
mock-executor noise (`rejected_regression` / `insufficient_lift` on the
deterministic keyword heuristic) while the real replay executor remained
unusable synchronously (P3: 2xN monologues vs a 600s synchronous budget
- the auto-loop thread cannot block for 45 minutes).

Design (operator-approved via 3 decisions):
1. **Mock counterfactual gate is FULLY ADVISORY** (default
   `replay_mock_enforce: false`). A losing mock verdict logs
   `[skillopt] mock replay gate (advisory, not enforcing) for <skill>`
   and the proposal proceeds; `replay_mock_enforce: true` restores the
   v1.8.19 1.0pp hard bar (test t_c2...rejects pins that path).
2. **Real gate = async confirm stage.** Direct (non-official-gated)
   structurally-valid proposals get a DETACHED real-executor worker
   (`scripts/replay_gate_worker.py`, new) and PARK as pending; the
   verdict lands in a `.md.realgate.json` sidecar (write-then-rename);
   the NEXT drain tick harvests it: accepted -> adopt
   (`real_gate_accepted`), measured regression -> quarantine
   (`real_gate_reject`), not-run/failed/stale -> FAIL-OPEN adopt
   (loud tags `real_gate_not_run` / `real_gate_failed` /
   `real_gate_stale` - a paid verdict we could not obtain must not
   block the drain forever).
3. **Budget (operator-locked):** 450s per-task timeout x 3 tasks
   (`replay_real_per_task_timeout_s: 450`, `replay_real_max_tasks: 3`;
   worst case 2x3x450s = 45 min per proposal, one worker in flight).

Key mechanics:
- **Sidecar-first single-flight:** the pending sidecar is written BEFORE
  the worker spawns; `_adopt_one` skips (stays queued) whenever
  `find_pending_real_gate_sidecar()` is non-None, so at most one real
  gate runs at a time and every direct adoption eventually gets a real
  verdict (no bypass-by-flight-state).
- **Frozen tasks:** `_real_gate_spawn` freezes the held-out task list
  into `<staged>.md.realgate.tasks.json`; the worker NEVER rescans
  rollouts (spawn-time selection == run-time measurement; drift is
  structurally eliminated; the harvest re-check is advisory-only,
  tag `real_gate_drift`).
- **TRAP A honored:** the worker's bare `helpers` import cannot see
  config.json, so ALL knobs are CLI-passed by `_real_gate_spawn`
  (merged_config resolved in-process). Enforced by a docstring warning
  in replay_gate_worker.py.
- **TRAP B honored:** with the async gate on, stage 0.7 runs the MOCK
  executor even when `replay_real_executor_enabled: true` (no
  synchronous double spend, no 45-min drain block).
- **Restart safety:** `_real_gate_cleanup` rehydrates the
  `state["real_gate"]` mirror from a live pending sidecar and unlinks
  orphaned sidecars (missing proposal + dead pid); the harvest's
  stale/grace window (`replay_real_gate_stale_after_s`, 4h default)
  uses `sleep_runner.is_running(pid)`.
- **Budget accounting:** each spawn records
  `replay_real_gate_cost_cents` (default 6c) via the budget tracker
  (best-effort); `_V1822_RG_CFG` in tests pins it to 0 so the suite
  never touches the production budget state.
- **Official-gated proposals bypass the real gate entirely** (the
  upstream gate is authoritative) and adopt immediately.
- `_move_staged_proposal` carries the realgate sidecar + tasks file
  alongside the official marker (adopted/ and rejected/ both).
- Observability: `staging drain` summary gains `pending=`; the Loop
  status card shows the in-flight gate (skill/pid/age) via the
  `real_gate` state mirror; per-task `latency_s` is recorded in
  real-branch verdicts.

New/changed:
- `sleep_runner`: `REAL_GATE_SIDECAR_SUFFIX`/`REAL_GATE_TASKS_SUFFIX`,
  write/read/find sidecar helpers, `_detached_popen_kwargs()`
  (DETACHED_PROCESS|CREATE_NEW_PROCESS_GROUP on win32,
  start_new_session on POSIX), `launch_real_gate_worker()`,
  `is_running(pid)` ctypes-based on win32 (os.kill(pid,0) is Ctrl+C
  there); stage 0.7 executor/enforcement selection per the matrix above.
- `auto_loop`: `_real_gate_cleanup`, `_real_gate_enabled`,
  `_real_gate_spawn`, `_harvest_real_gate`; `_adopt_one` harvest
  branch + spawn branch + `real_gate` audit field
  ("bypassed"/"spawned"/"harvested"); `_load_state` defaults gain
  `real_gate: None`; `get_loop_state` exposes the mirror with age_s.
- `scripts/replay_gate_worker.py` (new): single-proposal real gate,
  JSONL phases (start/per_task/verdict/summary) to
  `logs/runs/real_gate_*.jsonl`, exit 0 on verdict (either polarity),
  1 -> sidecar status=failed.
- `replay_harness.run_counterfactual(real)`: per-task `latency_s`.
- config: new defaults in default_config.yaml (flat keys - the nested
  YAML parser lesson): `replay_mock_enforce: false`,
  `replay_real_gate_enabled: true`, `replay_real_gate_stale_after_s:
  14400`, `replay_real_gate_cost_cents: 6`. Production config.json
  enables the gate with `replay_real_executor_enabled: true`,
  `replay_real_per_task_timeout_s: 450`, `replay_real_max_tasks: 3`.
- Smoke: `_real_gate_env` isolation recipe (extends the v1.8.21 drain
  pattern with runs_dir + held-out/launch restore; `exist_ok` mkdirs),
  `_sleepy_child`, `_v1822_valid_proposal`, `_v1822_optin`; 11 new
  t_v1822_* cases (advisory matrix, official-gated bypass, spawn+park,
  single-flight, harvest adopt/quarantine/fail-open, stale/grace,
  restart rehydrate + toggle-off, latency, worker CLI contract). Two
  pre-existing tests updated to the new semantics (hard-reject test now
  pins `replay_mock_enforce: true`; executor-selection test pins the
  legacy sync path with `replay_real_gate_enabled: false`). Suite
  183/183.

Rollout: restart REQUIRED (auto-loop-thread code). Post-restart
observation checklist (first eligible tick): spawn log line
`real gate: spawned worker pid=...`, sidecar in staging
(`*.md.realgate.json`, status pending), worker JSONL growing,
next-tick harvest adopting/quarantining on the verdict, drain summary
`pending=` count, dashboard Loop card real-gate line, budget +6c.

## 1.8.23 — first real-gate run diagnosed: timeout under-budget + honest sidecar labeling (2026-09-24)

The first async real-gate run in production (2026-09-24,
`agent-zero-api-handler-routing`, worker log
`real_gate_worker_20260924T112712`) came back
`insufficient_usable_pairs: 0 usable (3 task failures)` — all 6 monologue
replays hit the 450s per-task budget, and the harvest fail-open adopted on
`real_gate_not_run`. Investigation findings + fixes:

- **Root cause (timeout)**: production `config.json` carried
  `replay_real_per_task_timeout_s: 450` — BELOW the harness's own documented
  600 ceiling (default_config.yaml:105, replay_harness default, ROADMAP 751
  "beyond 600 is operator-level"). A replay monologue subprocess pays a full
  framework init (initialize_agent + AgentContext + all plugin extensions)
  before the monologue even starts, so 450s was never a realistic budget on
  this instance. Fixed: config.json + auto_loop default + worker CLI default
  all 600. The ROADMAP-mandated ceiling stays 600 (raising further is an
  operator decision).
- **Timeout diagnosability**: `replay_worker._run_monologue` now prints
  flushed phase marks (`init_done` / `context_ready` / `monologue_done`
  elapsed seconds) and `_real_score`'s `TimeoutExpired` handler embeds the
  killed worker's phase marks in the raised `RuntimeError` — the next
  timeout shows exactly which phase ate the budget instead of an opaque
  "timed out after 450s".
- **Honest sidecar labeling**: spawn-time `gate_passed: true` was a
  placeholder that made COMPLETED sidecars read "gate passed" even when the
  verdict was could-not-measure (found by reading the live
  `...adopt20260924T122722.md.realgate.json`: `gate_passed: true` +
  `verdict.ok: false`). Spawn now writes `pre_gate_recorded: true` +
  `gate_passed: null`; the worker writes `gate_passed = verdict.ok` on done
  and `false` on failed. Verified nothing consumes `gate_passed` (harvest
  keys on sidecar presence via `read_real_gate_sidecar`); the on-disk
  09-24 sidecar retro-fixed to `gate_passed: false`. Spawn/pending fixtures
  updated in smoke.
- **Re-adoption of a changed skill = intended behavior** (flag #3 from the
  post-restart report, no code change): the 09-24 second adoption of
  `agent-zero-api-handler-routing` was a genuine v2 — it adds the
  `methods`/405 ApiHandler knowledge distilled from newer rollouts, and
  live SKILL.md byte-matches the 09-24 proposal. No-op re-adoption is
  already structurally rejected (validate_proposal stages 5 byte-identical
  + stage 6 whitespace-normalised). No dedupe gap exists.
- **Test-fixture leak caught + fixed (found by the v1.8.19 P4 pollution
  test during this run's smoke)**: the v1.2.0 HTTP-judge smoke test
  (`t_v121_judge_via_http`) wrote its `r0..r5` rollout fixtures into the
  PRODUCTION `logs/rollouts` dir, and the live inner-loop tick in the
  running container scanned them mid-test and enqueued 6 fixture
  suggestions into production run state (all timestamped to one tick —
  a race, not a deterministic leak; earlier runs got lucky). Fixed: the
  test now sandboxes `ab_harness._rollouts_dir` to a tmpdir (same pattern
  as the other ab_harness tests at smoke.py:1114/1183/1394); the 6 leaked
  production files (`logs/runs/suggestions/v121_http_judge_r*.md`)
  deleted.
- Note (unchanged design): harvest stays fail-OPEN on could-not-measure
  (v1.8.22 decision matrix — quarantining on a transient executor outage is
  the v1.8.21 head-of-line disease with worse blast radius) and fail-CLOSED
  on a real measurement. With the timeout fixed, the next run should
  produce an actual measurement and fail-closed semantics finally engage.
