# skillopt

> Microsoft SkillOpt text-space skill optimizer, bridged as an Agent Zero self-evolution engine. Harvests the agent's own task rollouts, drives the official `skillopt_sleep` pipeline (or a fallback direct optimizer), gates proposals behind a monotonic validation gate, and stages gated skill edits for human-in-the-loop adoption.

**Version:** 1.6.1 · **Plugin ID:** `skillopt`

## Purpose

Treats `usr/skills/<name>/SKILL.md` documents as trainable text parameters: the engine reads the
agent's own task trajectories (rollouts), groups them by skill, proposes improved skill documents,
and promotes them only through a validation gate. v1.6.0 (Solution B) makes the auto-loop drive the
official Microsoft `skillopt_sleep` pipeline instead of a hand-rolled optimizer, demoting the local
`direct_optimizer` to a fallback used only when the official package is absent.

## Architecture (v1.6.0)

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
  held-out gate before staging, so a staged proposal has already passed it by construction). The local
  synthetic A/B "replay" harness is advisory-only and OFF by default in v1.6.0 (it cannot do real
  counterfactuals without the future Solution-C env adapter).
- **Per-skill governance** (`helpers/governance.py`): `opt_out`/`opt_in`/`immutable`/`rate_limited`,
  per-skill `policy.json`, `.skillopt.optout`/`.skillopt.optin` markers. The tick now calls
  `check_skill_eligible()` + `cadence.compute_next_run()` + `budget.can_skill_spend()` before each
  per-skill cycle (previously computed-but-unused).
- **Cycle history** (`helpers/cycle_history.py`): append-only JSONL; v1.6.0 compaction rotates overflow
  beyond `cycle_history_max_entries` (default 500) to a cold `cycle_history.archive.jsonl`.
- **Harvester** (`extensions/python/monologue_end/_60_skillopt_harvest_rollout.py`): fires after every
  chat turn, extracts task/trajectory/outcome, writes `logs/rollouts/<id>.json` (no LLM call).
- **Integration:** lifecycle hooks (`hooks.py`), 5 extension hooks, 8 HTTP API endpoints, 4 agent tools,
  `skillopt_trainer` subordinate agent, WebUI dashboard (`webui/skillopt-dashboard.js`).

## Ownership / Layout

- `extensions/` — monologue-end harvester, monologue-start warning, post-adopt safety net, banner
- `helpers/` — auto_loop, inner_loop, official_adapter, direct_optimizer, sleep_runner, bridge,
  ab_harness, governance, cadence, budget, fragment_store, failure_memory, reward_model, cycle_history,
  audit_log, config_loader
- `webui/` — dashboard + config UI
- `tests/smoke.py` — 92 deterministic tests (no LLM/network)

## Local Contracts

- Skill edits are STAGED, never auto-applied unless `auto_adopt: true` (default false). With
  `auto_adopt: false` the user reviews each staged proposal in the WebUI before promotion.
- The official engine runs its own monotonic held-out gate before staging; the local gate adds a cheap
  structural pre-filter and delegates the held-out decision when `official_gated=True`.
- `use_official_engine: true` is the v1.6.0 default; when the official package is absent the loop
  silently falls back to `direct_optimizer` (logged) — it can never make things worse.
- The synthetic A/B replay harness is ADVISORY ONLY and OFF by default (`ab_harness_enabled: false`);
  opt in via the `SKILLOPT_AB_HARNESS_ENABLED=1` env var for the harness-functionality smoke tests.

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

## Verification

- `python tests/smoke.py` — 92 deterministic tests (no LLM/network).
- `python -c "import skillopt_sleep"` in the A0 venv confirms the official package (else fallback).
- Dry-run against the 5 synthetic rollouts with `use_official_engine: true`, `auto_adopt: false` → a
  proposal lands in `staging/` with a gate reason recorded in `cycle_history.jsonl`.
- One real `.skillopt.optin` pilot skill: full harvest→cycle→gate→stage; inspect the dashboard audit
  log. Then `auto_adopt: true`; confirm `usr/skills/<name>/SKILL.md` is overwritten, a fragment
  snapshot is written, and `post_adopt.log` records the safety-net re-validation. Roll back via the
  fragment store; confirm the live skill restores.

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

## See also

- `plugin.yaml` — manifest (name, version, settings_sections, per_project_config, per_agent_config)
- `default_config.yaml` — defaults incl. the OFFICIAL ENGINE BRIDGE (v1.6.0) section
- `helpers/official_adapter.py` — the Solution B bridge (probe + run_official_sleep_cycle)
- `helpers/direct_optimizer.py` — the fallback optimizer (v1.6.0 FALLBACK ROLE)
- `README.md` — user-facing docs
- Framework references: `helpers/plugins.py` (lifecycle), `helpers/api.py` (API dispatch),
  `helpers/ui_server.py` (asset serving)

## Version history

- 1.0.0–1.5.0 — hand-rolled engine: two-loop architecture, structural gate, synthetic A/B harness,
  fragment store, failure memory, governance, cycle history (never ran: `cycles_run: 0`).
- 1.6.0 — Solution B: bridge to official `skillopt_sleep`; direct_optimizer demoted to fallback;
  gate delegates held-out to the official engine (`official_gated`); per-skill cadence/budget/
  governance wired into the tick; version alignment; cycle_history compaction; A/B advisory-off.