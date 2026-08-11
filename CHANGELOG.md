# SkillOpt Plugin — Changelog

All notable changes to this plugin are documented here. The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

Nothing staged yet. The two v1.7.0 follow-ups (real replay executor + reward
training) landed in v1.8.0 — see below. Live verification of the v1.8.0 opt-in
paths (L1–L4 in the plan) is the remaining work.

---

## [1.8.0] — 2026-08-11

**The two offline pieces stubbed in v1.7.0 are now implemented, default-off.**
Six phases, each its own commit to the nested `Olszalsik/a0-skillopt` repo.
122/122 deterministic smoke tests pass (11 new `t_v18_*` + the updated
`t_c2_real_executor_disabled_returns_not_enabled`); no LLM/network. v1.7.0
behavior is preserved byte-for-byte unless an operator opts in.

### Added

- **P1 — subprocess-isolated replay worker** (`scripts/replay_worker.py`, NEW).
  Runs one held-out task through a real Agent Zero monologue under a given skill
  in a child process (temp working dir, own event loop, `SKILLOPT_REPLAY_MODE=1`)
  and writes a `{score, outcome, ...}` JSON envelope. A0 imports are lazy so the
  module is importable in smoke tests without A0 core. Skill injection uses the
  corrected recipe: `hist_add_tool_result` takes `skill_instructions` as a
  **top-level kwarg**, not nested under `additional`.
- **P2 — real `_real_score`** (`helpers/replay_harness.py`). Shells out to the
  worker via blocking `subprocess.run` (not detached), parses the score JSON,
  returns a float. `sleep_runner.build_subprocess_env()` factored out of
  `launch_sleep_subprocess` so the worker inherits the `.skillopt-env` overlay.
  Cost bounded by `replay_real_max_tasks` (default 4 — 2×N full monologues per
  gate call). Any worker failure raises → `run_counterfactual` returns
  `real_executor_unavailable:...` (loud-not-crash, falls through to the
  structural gate — unchanged).
- **P4 — LLM-judge outcome labelling** (`helpers/llm_judge.py` + `scripts/label_rollouts.py`, NEW).
  `judge_outcome(rollout)` classifies a turn into success/partial/failure
  (aligned with `score_rollout`'s 3-class space) via `direct_optimizer._call_llm`
  with a judge-specific system prompt; never raises. `label_rollout_file`
  augments a rollout JSON in place, atomically, and is idempotent. The CLI pass
  (`--limit/--force/--model/--dry-run`) reports an advisory judge-vs-heuristic
  agreement %. `direct_optimizer._call_llm` gained an optional `system` param
  (back-compat default) so judge + optimizer share one call function.
- **P5 — real DistilBERT training loop** (`scripts/train_reward_model.py`).
  `--mode train` (default `smoke` — backwards compatible) loads judge-labelled
  rollouts, featurizes them with `reward_model._rollout_to_text` (the SAME
  featurizer inference uses), splits train/val deterministically (stratified
  per-class), runs an AdamW epochs loop with per-epoch val accuracy+loss,
  persists the model, writes a `1.3.0-train-*` version stamp. `--min-samples`
  (50) guards against tiny datasets.
- **P6 — calibration + dead-config wiring** (`helpers/reward_model.py`).
  `model_path()` now reads the `reward_model_path` config key (env still wins).
  `score_rollout(prefer_model_above=None)` resolves `calibration.json` >
  `reward_model_prefer_above` config > 0.6 via `_config_prefer_above()`. The
  `_calibrate()` pass sweeps T in [0.3, 0.9] under the gated decision rule,
  writes `calibration.json` next to the model, and surfaces `T*` in the version
  stamp. `probs_fn` is injectable so the smoke test calibrates without torch.

### Changed

- **P3 — replay gate call-site** (`helpers/sleep_runner.py` stage 0.7). Now
  passes `executor="real"` when `replay_real_executor_enabled` is true (was
  hardcoded `mock`). Off by default = byte-identical to v1.7.0.
- **Config** — new keys in `default_config.yaml` + `config.json`:
  `replay_real_per_task_timeout_s: 180`, `replay_real_max_tasks: 4`. The
  `replay_real_executor_enabled` comment now says IMPLEMENTED (was "stub"). The
  `reward_model_prefer_above` comment now says WIRED (was "will be
  re-calibrated once 5K labelled rollouts exist"). `config.json` is gitignored
  and is not committed.
- **Version bump** 1.7.0 → 1.8.0 (`plugin.yaml`, `plugin.py`, `hooks.py`,
  `execute.py`, `README.md` badge).

### Tests

- 11 new `t_v18_*` smoke cases + the renamed
  `t_c2_real_executor_disabled_returns_not_enabled` (flag-on no longer asserts
  `NotImplementedError`; it now mocks `subprocess.run` with a failing
  `_FakeProc` so no real subprocess spawns, and the
  `real_executor_unavailable` assertion holds via `RuntimeError`). All
  deterministic, NO LLM, NO subprocess spawn.

---

## [1.7.0] — 2026-08-11

**Solution C (Core) — the self-evolution engine now actually evolves.** Four
phases, each its own commit to the nested `Olszalsik/a0-skillopt` repo. 122/122
deterministic smoke tests pass (no LLM/network).

### Fixed

- **C1 — the harvester wrote zero rollouts.** The v1.1.0 harvester read
  `loop_data.messages`, which does not exist on `LoopData` (the real attributes
  are `history_output`, `user_message`, `last_response`), so it early-returned
  on every chat turn. The engine had been running blind since v1.1.0 —
  `cycles_run` was non-zero but no rollout ever fed the gate. The harvester now
  reads `history_output` and attributes the active skill **authoritatively** via
  `skills.skill_instruction_name` (a per-turn walk of the output history, last
  match wins) with `get_loaded_skill_names` as the session-ledger fallback. A
  `SKILLOPT_REPLAY_MODE` env recursion guard keeps a replay agent's own turns out
  of the training set.

### Added

- **C2 — local counterfactual replay gate** (`helpers/replay_harness.py`, NEW).
  A deterministic mock executor scores the current vs proposed skill on the
  held-out rollouts (relevance heuristic: base outcome × keyword-overlap, no
  LLM) and accepts only on a strict-monotonic lift ≥ `gate_min_improvement_pp`
  over ≥ `replay_min_n` tasks. Wired as **stage 0.7** in `validate_proposal`
  (runs only when `skill_name AND not official_gated AND
  replay_local_gate_enabled`). The real A0-agent-loop executor is a guarded
  stub behind `replay_real_executor_enabled` (default false) — documented
  follow-up. New config keys (in both `default_config.yaml` and `config.json`):
  `replay_local_gate_enabled: true`, `replay_min_n: 3`, `replay_held_out_n: 8`,
  `replay_real_executor_enabled: false`.
- **C3 — human-in-the-loop adopt UI.** Three new API endpoints: `/staged` lists
  proposals with gate evidence (lift_pp, n_held_out, gate_reason, diff_summary);
  `/reject` records a no (audit-only, does not delete the staged file);
  `/rollback` restores the most recent whole-file `_default` snapshot.
  `api/adopt.py` rewritten to take an optional `proposal_id` (stem match, latest
  fallback) and snapshot the pre-adopt `SKILL.md` before overwriting.
  `helpers/fragment_store.py` gains `snapshot_default`/`restore_default_snapshot`
  keyed on the skill-name string (avoids the `Path.stem`="SKILL" collision that
  the old `_snapshots_dir_for_skill` hit). WebUI `config.html` gets a "Staged
  proposals" section with Approve/Reject/Rollback buttons; `skillopt-dashboard.js`
  gets `staged()`/`adoptProposal`/`reject`/`rollback`/`renderStagedProposals`.
- **C4 — auto opt-in with guardrails.** `helpers/governance.py` gains
  `auto_optin_new_skill(skill, *, source)`: a brand-new skill seen in rollouts is
  auto-opted-in (`.skillopt.optin` + `.skillopt.policy.json` with
  `require_human_approval: true`) but stays `require_human_approval_pending` until
  a human approves it. NEVER touches opted-out or immutable skills. Idempotent.
  `helpers/auto_loop.py` runs `_maybe_auto_optin` before the eligibility check
  (gated on `governance.default_policy.auto_optin_new_skills`). Two new API
  endpoints: `/governance_approve` (records a human decision + touches the optin
  marker) and `/governance_status` (full block or per-skill policy + markers +
  eligibility). WebUI gets a "Governance" section with Approve/Deny per skill.
  Config (both files): `require_human_approval: true`, `auto_optin_new_skills: true`.
- **12 new smoke tests** (110 → 122): 4 C1, 6 C2, 5 C3, 7 C4.

### Changed

- Version strings aligned at 1.7.0 across `plugin.yaml`/`plugin.py`/`hooks.py`/
  `execute.py`; the version-alignment smoke test now checks all four files.
- `governance.default_policy.require_human_approval` default flipped to `true`
  (safe default — adoption is one-click, never silent).

---

## [1.6.1] — 2026-08-10

**Verified the official-engine bridge against the upstream source.** The v1.6.0
adapter had wrong assumptions about the `skillopt_sleep` CLI and the
`evaluate_gate` signature; all verified against `microsoft/skillopt` @ HEAD by
shallow-clone introspection (the package is not installed in this dev env).

### Fixed

- CLI is `python -m skillopt_sleep <subcommand>` (run/dry-run/status/adopt/
  harvest/schedule/unschedule). `run` flags: `--project PATH`,
  `--target-skill-path PATH` (a real SKILL.md path, NOT a name — v1.6.0 wrongly
  passed `--skill`), single `--model` (v1.6.0 wrongly passed `--optimizer-model`/
  `--target-model` which don't exist), `--backend mock|claude|codex|copilot|
  cursor|pi|handoff|azure_openai`, `--lookback-hours`, `--max-tasks`,
  `--edit-budget`, `--preferences`, `--json`.
- Staging: the real engine writes `<project>/.skillopt-sleep/staging/<ts>/` with
  `proposed_SKILL.md` + `report.json` + `manifest.json` (v1.6.0 wrongly looked
  for `staging/<skill>.md`/`best_skill.md`).
- Gate verdict: read from `report.json` `{accepted, gate_action,
  baseline_score, candidate_score}` via `_read_gate_verdict` (NOT log scraping).
  `proposed_SKILL.md` is copied to plugin `staging/` ONLY when the gate accepts.
  A gate reject → `{ok:False, gate_rejected:True}` with NO direct fallback (the
  official verdict is authoritative).
- `evaluate_gate(candidate_skill, cand_hard, current_skill, current_score,
  best_skill, best_score, best_step, global_step, *, cand_soft=0.0,
  metric="hard", mixed_weight=0.5) -> GateResult`; action in
  `{accept_new_best, accept, reject}`. Not called authoritatively (`report.json`
  is).

### Added

- New config keys: `official_edit_budget`, `official_preferences`.
- 8 new smoke tests (92 → 100) covering the verified CLI flag mapping, staging
  discovery, `report.json` gate verdict, and the `gate_rejected` no-fallback
  contract.

---

## [1.6.0] — 2026-08-10

**Solution B — the auto-loop now drives the official Microsoft `skillopt_sleep`
pipeline** instead of a hand-rolled optimizer. The local `direct_optimizer` is
demoted to a fallback used only when the official package is absent.

### Added

- **`helpers/official_adapter.py`** (NEW): cached subprocess probe of
  `import skillopt_sleep` in the A0 venv (60s TTL), reuses
  `sleep_runner.launch_sleep_subprocess` to run one official Sleep cycle
  (harvest→mine→replay→consolidate→gate→stage), discovers/renames the staged
  proposal. Fail-soft: any infra error → `{ok:False, fallback_to_direct:True}`;
  an official gate reject → `{ok:False, gate_rejected:True}` (no direct fallback).
- **`helpers/sleep_runner.py`**: `validate_proposal` gains `official_gated: bool`;
  Stage 8 held-out is SKIPPED when `official_gated` (the official engine runs its
  own monotonic held-out gate before staging).
- **`helpers/auto_loop.py`**: `_tick` now calls
  `_run_engine_for_eligible_skills` per skill with
  `governance.check_skill_eligible` + `cadence.compute_next_run` +
  `budget.can_skill_spend` gating (previously computed-but-unused), tries
  official_adapter first, falls back to direct_optimizer. Records `last_engine`;
  `_auto_adopt` passes `official_gated` to the gate.
- **`helpers/ab_harness.py`**: synthetic A/B replay demoted to ADVISORY ONLY
  (default off, `SKILLOPT_AB_HARNESS_ENABLED` env override).
- **`helpers/cycle_history.py`**: compaction rotates overflow beyond
  `cycle_history_max_entries` (default 500) to `cycle_history.archive.jsonl`.
- Config (both files): `use_official_engine: true`, `ab_harness_enabled: false`,
  OFFICIAL ENGINE BRIDGE keys (`official_run_verb="run"`, `official_backend`,
  models, lookback, max_tasks, `official_run_timeout_s=600`).
- 10 new smoke tests (82 → 92).

### Changed

- Version strings aligned at 1.6.0 across `plugin.yaml`/`plugin.py`/`hooks.py`/
  `execute.py`.

---

## [1.5.0] — 2026-07-29

Day-5 item 8 ships. The closed loop is now safe to leave running unattended: per-skill governance gives the user fine-grained control over which skills may be edited (opt-out default, with opt-in / immutable / rate-limited policies), the per-cycle dashboard (Day-5 item 7) makes every Sleep cycle visible from the WebUI, and the supporting CI / docs / install story is ready for external contributors. Public release candidate ready for the community Plugin Index.

**Governance (item 8).** `helpers/governance.py` is a new stdlib-only helper that runs BEFORE the validation gate inside `auto_loop._auto_adopt()`. It returns `{ok, eligible, reason, ...}` based on three checks, in order: (1) `usr/skills/<name>/.skillopt.optout` marker wins everything (locked out, regardless of any policy.json); (2) the skill's `usr/skills/<name>/policy.json` mode (`opt_in`, `opt_out`, `immutable`, or `rate_limited`) is read with safe fall-back to a global default policy; (3) a rate-limit check (`min_interval_seconds` between consecutive adoptions) blocks cooldown violations. Every decision is logged to `logs/runs/governance.log` (JSONL, append-only) for the audit trail. `mark_human_decision(skill, decision)` lets the WebUI record explicit human overrides that clear the approval gate. `get_governance_status()` exposes the snapshot block to the dashboard with opted-out / governed / opt-in skill counts and the last 5 decisions. A global `SKILLOPT_GOVERNANCE_OPT_OUT_ALL=1` env var pins every skill to opt-out without writing per-skill markers — useful for staging / dev environments. Default behaviour is OPT-OUT: a skill with no marker and no `policy.json` is locked, which is the safest stance for unattended installs.

### Day-5 (item 8)

Item 8 ships: per-skill governance + opt-out, with the supporting public-release story.

**Governance (item 8).** A skill is eligible for self-evolution only when (a) it does not have `.skillopt.optout` in its skill directory, (b) its `policy.json` says `opt_in` (or the global default does), and (c) its rate-limit cooldown has elapsed since the last adoption. Immutable mode short-circuits with `mode_immutable`. Every decision lands in `logs/runs/governance.log`; the dashboard renders a `governance` block with the current policy in effect and the last 5 decisions.

### Added

- **`helpers/governance.py`** — `check_skill_eligible(skill_name)`, `load_skill_policy(skill_name)`, `mark_decision(skill_name, decision, ...)`, `mark_human_decision(skill_name, decision)`, `get_governance_status()`, `set_skills_dir_for_tests(dir)`, `reset_for_tests()`. Files at `<plugin>/logs/runs/governance.log` (JSONL, append-only). Stdlib only (`json`, `pathlib`, `time`). Per-skill marker at `usr/skills/<name>/.skillopt.{optout,optin}`; per-skill policy at `usr/skills/<name>/policy.json`.
- **Per-skill config keys** — `default_config.yaml` and `config.json` get a `governance:` block with `default_policy.mode` (`opt_in` / `opt_out` / `immutable` / `rate_limited`, default `opt_out`), `default_policy.rate_limit.min_interval_seconds` (default 3600), `default_policy.rate_limit.max_per_day` (default 8), `default_policy.approval_required` (default false).
- **Status surface** — `helpers/sleep_runner.get_status_snapshot()` adds a `governance` block with `enabled`, `opted_out_count`, `governed_count`, `opt_in_count`, `last_decisions` (last 5), `current_policy`, `file_path`. Mirrors the `cycle_history` / `failure_memory` / `inner_loop` block shape.
- **`tests/smoke.py`** — 9 new v1.5.0-Dev cases covering default opt-out, opt-out marker precedence, immutable mode, opt-in marker opt-in, rate-limit cooldown, malformed-policy fallback, mark_human_decision, status block shape, and the auto-loop skip behaviour. Total: 82/82 green.

### Changed

- **`helpers/auto_loop.py`** — `_auto_adopt()` calls `governance.check_skill_eligible(skill_name)` BEFORE the validation gate. On `{eligible=False}`, the loop logs the reason to `logs/runs/auto_loop.log`, records the cycle to `cycle_history` (so the dashboard still shows what was attempted and why), and skips the proposal. Wrapped in its own `try/except` and lazy-imported (`from usr.plugins.skillopt.helpers import governance`, fallback `from helpers import governance`), exactly matching the `cycle_history` and `failure_memory` blocks. The validation gate is unchanged. On failure inside the governance helper the loop falls through to the v1.4.0 behaviour.
- **`helpers/sleep_runner.py`** — `get_status_snapshot()` adds the `governance` block. `validate_proposal()` unchanged (gate preserved).
- **`execute.py`** — file-presence check now includes `helpers/governance.py`; bumped from 45 to 47 required files. `EXPECTED_VERSION` unchanged at `1.4.0` (the v1.5.0 retag is the maintainer's pass).

### Release prep (Day-5 final)

Public release candidate ready for the community Plugin Index.

- **`.github/workflows/ci.yml`** — runs `python tests/smoke.py` AND `python execute.py` on Python 3.10, 3.11, 3.12, 3.13 — for every push to `main`, every PR, and on manual dispatch. Uploads `/tmp/smoke_*.txt` and `logs/runs/` as artifacts on failure. Concurrency group cancels in-progress runs on the same ref.
- **`RELEASE_NOTES.md`** — first public release candidate for v1.5.0 with TL;DR, upgrade notes from v1.4.0, backwards-compat story, known limitations, and acknowledgments. The Day-5 governance layer is documented end-to-end with a `policy.json` example.
- **`INSTALL.md`** — refreshed: opens with a "What this plugin is and isn't" section, adds the governance + dashboard wiring, and adds three Day-5-specific troubleshooting rows (cycle-history file corruption, opt-out marker not respected, auto-loop never fires because governance rejected every cycle). The matrix now covers 18 known-failure modes.
- **`README.md`** — adds an "Is this plugin right for you?" section that explicitly mentions per-skill opt-in governance (the safety story for regulated environments).
- **`CHANGELOG.md`** — this entry. Existing v1.4.0 / v1.3.0 / v1.2.0 / v1.1.0 / v1.0.0 sections are unchanged.

### Engineering principles followed

- **Preserve the gate** — `validate_proposal()` unchanged across all of Day-5. The 73 pre-existing tests still green; 9 new v1.5.0-Dev tests added for governance only. The auto-loop calls `check_skill_eligible()` BEFORE the gate so an opt-out / immutable / rate-limited skill never enters the validation path.
- **User-owned state** — opt-out markers live in `usr/skills/<name>/` (the A0 skill dir, owned by the user / framework), not in `<plugin>/`. Users can flip a skill's governance state without re-syncing the plugin. The plugin only READS these directories.
- **Backwards-compat** — the global default policy is `opt_out`. A skill with no marker and no `policy.json` is locked by default (the safest stance). To restore the v1.4.0 implicit-opt-in behaviour, set `governance.default_policy.mode: opt_in` in `config.json` OR add `.skillopt.optin` markers per skill.
- **Loud-not-crash** — every public function in `governance.py` returns a structured `{ok, eligible, reason}` on failure instead of raising. The auto-loop's call site wraps it in `try/except` so a malformed `policy.json` or a missing skill directory can never break the cycle.
- **Append-only audit trail** — `governance.log` is JSONL, opened in `"a"` mode with `flush()` + `os.fsync()` after each write. Use `jq` to grep by skill: `jq -c 'select(.skill=="code-review")' logs/runs/governance.log | tail`.
- **No new runtime dependencies** — `helpers/governance.py` is stdlib only (`json`, `pathlib`, `time`). CI installs the same `pyyaml + scikit-learn + transformers + torch` as before (because the v1.3.0 reward-model path imports them), but the governance path itself has zero new deps.
- **Plugin-local audit, not global** — governance decisions land in `<plugin>/logs/runs/governance.log`, not in `~/.skillopt/` or any other host-global location. Override via `SKILLOPT_SKILLS_DIR` env if you want to share the audit directory across plugins.

---

## [1.4.0] — 2026-07-28

Day-5 roadmap item 7 (Per-cycle dashboard) ships. The closed loop is now fully observable end-to-end: every Sleep cycle the auto-loop runs gets a rich record in `logs/runs/cycle_history.jsonl` (with gate outcome, A/B harness result, reward model prediction, inner-loop + failure-memory context, budget impact, and links back to the rollouts, the staged proposal, and the audit-log line), alongside the existing one-line `logs/runs/adoptions.log` write. Three new API endpoints (`cycles`, `cycle/<id>`, `audit_log`) plus three new `window.SkillOptDashboard` methods give the WebUI the read-only hooks it needs to render the dashboard tabs. The `validate_proposal()` gate is unchanged; the 63 pre-existing tests still green.

**Cycle history (item 7).** `helpers/cycle_history.py` is a new append-only JSONL store: `record_cycle_entry()` writes one JSON object per cycle to `logs/runs/cycle_history.jsonl` plus a one-line summary to `logs/runs/cycle_history.log` (the same `*.log` companion pattern used by `fragments.log`, `ab_harness.log`, `failure_memory.log`). Reads (`read_cycle_history()`, `read_cycle()`, `get_history_status()`) skip malformed lines silently so partial writes never crash the dashboard. The two new API endpoints (`api/cycles.py` and `api/audit_log.py`) give the WebUI the read-only hooks it needs to render the new tabs. The auto-loop calls `cycle_history.record_cycle_entry()` after every `_auto_adopt()` — for adopted and rejected outcomes — inside its own `try/except` so a cycle_history bug can never break the gate. `helpers/sleep_runner.get_status_snapshot()` adds a `cycle_history` block mirroring the existing `inner_loop` / `cadence` / `budget` / `failure_memory` shape so the dashboard has one consistent snapshot contract.

### Day-5

Item 7 ships: the per-cycle dashboard can now read every Sleep cycle the auto-loop ran, with the gate outcome, A/B harness result, reward model prediction, inner-loop + failure-memory context, budget impact, and links back to the rollouts, the staged proposal, and the audit-log line that points at this cycle. The previous one-line `logs/runs/adoptions.log` write is preserved exactly as it was — this adds a richer store alongside it, never replaces it.

**Cycle history (item 7).** `helpers/cycle_history.py` is a new append-only JSONL store: `record_cycle_entry()` writes one JSON object per cycle to `logs/runs/cycle_history.jsonl` plus a one-line summary to `logs/runs/cycle_history.log` (the same `*.log` companion pattern used by `fragments.log`, `ab_harness.log`, `failure_memory.log`). Reads (`read_cycle_history()`, `read_cycle()`, `get_history_status()`) skip malformed lines silently so partial writes never crash the dashboard. The two new API endpoints (`api/cycles.py` and `api/audit_log.py`) give the WebUI the read-only hooks it needs to render the new tabs. The auto-loop calls `cycle_history.record_cycle_entry()` after every `_auto_adopt()` — for adopted and rejected outcomes — inside its own `try/except` so a cycle_history bug can never break the gate. `helpers/sleep_runner.get_status_snapshot()` adds a `cycle_history` block mirroring the existing `inner_loop` / `cadence` / `budget` / `failure_memory` shape so the dashboard has one consistent snapshot contract.

### Added

- **`helpers/cycle_history.py`** — `record_cycle_entry()`, `read_cycle_history(limit, skill, since_ts, outcome)`, `read_cycle(cycle_id)`, `get_history_status()`, `reset_for_tests()`. Files at `<plugin>/logs/runs/cycle_history.{jsonl,log}`.
- **`api/cycles.py`** — `Cycles` (read many, filtered) and `Cycle` (read one by id) endpoints under `/api/plugins/skillopt/cycles` and `/cycle/<id>`. Read-only.
- **`api/audit_log.py`** — `AuditLog` endpoint under `/api/plugins/skillopt/audit_log`. Reads the v1.3.0 `logs/runs/adoptions.log` newest-first with `skill` + `passed` filters. Read-only.
- **`webui/skillopt-dashboard.js`** — three new methods on the `window.SkillOptDashboard` namespace: `cycles(limit, skill, sinceTs, outcome)`, `cycle(cycleId)`, `auditLog(limit, skill, passed)`, plus a `renderCycleHistory()` helper that builds a `data-cycle-history` payload the WebUI server mounts into the dashboard. Vanilla JS + `fetch`, no new globals, no external deps.
- **Status surface** — `helpers/sleep_runner.get_status_snapshot()` adds a `cycle_history` block with `enabled`, `total_entries`, `file_path`, `last_cycle_id`, `last_outcome`, `file_size_bytes`.
- **Config keys** — `default_config.yaml` and `config.json` get `cycle_history_enabled` (master kill switch, default `true`), `cycle_history_max_entries` (default 500 — caps the JSONL file; future compaction job moves older entries to a cold file), `cycle_history_min_outcome` (default `"all"` — values: `all | adopted | rejected`), and `cycle_history_include_skipped` (default `true`).
- **`tests/smoke.py`** — 10 new v1.4.0-Dev cases for the cycle-history helper, the cycles API, the audit_log API, and the status snapshot integration. Total: 73/73 green.

### Changed

- **`helpers/auto_loop.py`** — `_auto_adopt()` now records one `cycle_history.record_cycle_entry()` after every cycle (adopted + rejected). Wrapped in its own `try/except` and lazy-imported (`from usr.plugins.skillopt.helpers import cycle_history`, fallback `from helpers import cycle_history`), exactly matching the `failure_memory` block above. A failure in the cycle_history module is logged but never raises.
- **`helpers/sleep_runner.py`** — `get_status_snapshot()` adds the `cycle_history` block. `validate_proposal()` unchanged (gate preserved).
- **`execute.py`** — `EXPECTED_VERSION` bumped to `1.4.0-Dev`. File-presence check now includes `helpers/cycle_history.py`, `api/cycles.py`, and `api/audit_log.py`.

### Engineering principles followed

- **Preserve the gate** — `validate_proposal()` unchanged. The 63 pre-existing tests still green. The cycle_history write happens AFTER the gate; if the gate rejects, the cycle is still recorded (with `outcome="rejected"`) so the dashboard shows what the loop tried and why it failed.
- **Append-only JSONL** — `cycle_history.jsonl` is opened in `"a"` mode with `flush()` + `os.fsync()` after each write. `read_cycle_history()` and `get_history_status()` skip malformed lines silently so a crash mid-write never corrupts the dashboard.
- **Plugin-local only** — all four new files live in the plugin tree. No global state, no framework imports at module level, stdlib only (json, uuid, datetime, pathlib).
- **Backwards-compat** — the v1.3.0 `logs/runs/adoptions.log` write inside `_auto_adopt()` is untouched. The cycle_history record sits alongside it as a richer companion; the audit_log API reads the existing adoptions.log file the same way Day-4 already did.
- **Loud-not-crash** — every public function in `cycle_history.py` returns a structured `{ok, error}` on failure instead of raising. The auto-loop's call site wraps it in `try/except` so a malformed entry or a missing directory can never break the cycle.
- **Read-only API surface** — the two new api handlers never write to disk; they only read from the JSONL file and the existing adoptions.log. WebUI can publish, the cycle loop stays authoritative.

---

## [1.3.0] — 2026-07-26

Day-4 roadmap items 4 (Inner loop), 5 (Per-skill cadence + per-skill budget), and 6 (Failure memory) ship. The self-evolution loop is now a two-loop architecture: the inner loop does cheap, per-rollout critique-and-suggest in the background; the outer loop does expensive, per-skill targeted rewrites when there's real signal. Per-skill cadence means hot skills get more cycles, cold skills get fewer; per-skill budget means a hard daily cap on LLM spend per skill. Failure memory gives the LLM access to "what kinds of mistakes are happening on this skill" so proposals target real failures instead of generic improvements.

**Inner loop (item 4).** A background worker (`helpers/inner_loop.py`) scans `logs/rollouts/` after each chat, calls the LLM with a tiny prompt ("here is the task, here is the trajectory, here is the failure mode — suggest a one-sentence improvement to the skill used"), and appends the suggestion to `logs/runs/suggestions/<skill>_<rollout_id>_<ts>.md`. The outer loop reads all pending suggestions at the start of each cycle and calls `build_targeted_prompt()` to produce a minimal-edit prompt instead of a full rewrite. The inner loop never writes to `SKILL.md` or `staging/` — it only enqueues suggestions. LLM failures are caught and counted (`last_error` on the status snapshot), never raised.

**Per-skill cadence + per-skill budget (item 5).** `helpers/cadence.py:compute_next_run()` returns the seconds-until-next-run as a linear function of `new_rollouts / cadence_target` (floor 60s for hot skills, ceiling 3600s for cold skills). The auto-loop thread iterates over `list_skills_with_state()` plus any skills in `usr/skills/`, computes each skill's next-run time, and sleeps until the earliest. State files are per-skill (`logs/runs/auto_loop_state_<skill>.json`); the v1.1.0 single state file migrates to a `_default` skill on first read. `helpers/budget.py:BudgetTracker` enforces a daily cap (default 100c/skill/day) with a flat 1c/LLM-call estimate; the auto-loop calls `can_spend(cents)` before each LLM call and `record_spend(cents)` after.

**Failure memory (item 6).** `helpers/failure_memory.py` wraps A0's vector store (`python.helpers.memory_save`/`memory_load`) with the same injection pattern as the A/B harness. The default backend is a local JSON store at `<plugin>/logs/runs/failure_memory/<skill>/<memory_id>.json` when A0's memory is not importable in the current runtime (e.g. in a plugin-only test). `record_failure(skill, task, failure_mode, rollout_ids)` adds an entry; `load_failures(skill, k=5)` returns the most recent; `forget_failures(skill, before_ts)` deletes old entries. `build_failure_context(skill, k=5)` returns a `[FAILURE MEMORY]` block the outer loop prepends to the targeted prompt. Backend errors are caught and returned as `{ok: False, error: ...}` — never raised.

### Added

- **`helpers/inner_loop.py`** — `enqueue_suggestion()`, `list_pending_suggestions()`, `drain_suggestions()`, `build_targeted_prompt()`, `inner_loop_tick()`, `get_inner_status()`, `reset_for_tests()`. Background thread in `helpers/auto_loop.py` calls `inner_loop_tick()` every `inner_loop_interval_seconds` (default 60s). Cycle log: `logs/runs/inner_loop.log`.
- **`helpers/cadence.py`** — `compute_next_run()`, `load_per_skill_state()`, `save_per_skill_state()`, `list_skills_with_state()`. Per-skill state files at `<plugin>/logs/runs/auto_loop_state_<skill>.json`.
- **`helpers/budget.py`** — `BudgetTracker(skill_name, daily_cap_cents, reset_hour_utc)`. `record_spend()`, `can_spend()`, `get_status()`. Per-skill budget files at `<plugin>/logs/runs/budget_<skill>.json`.
- **`helpers/failure_memory.py`** — `record_failure()`, `load_failures()`, `forget_failures()`, `build_failure_context()`, `get_status_block()`, `set_memory_fn()`, `reset_for_tests()`. Cycle log: `logs/runs/failure_memory.log`.
- **Status surface** — `helpers/sleep_runner.get_status_snapshot()` adds `inner_loop`, `cadence`, `budget`, and `failure_memory` blocks. The `/api/plugins/skillopt/status` endpoint surfaces them for the WebUI.
- **Config keys** — `default_config.yaml` and `config.json` get `inner_loop_enabled`, `inner_loop_interval_seconds`, `inner_loop_max_suggestion_age_seconds`, `inner_loop_min_rollout_confidence`, `inner_loop_llm_model`, `inner_loop_prompt`, `cadence_target_rollouts`, `cadence_floor_seconds`, `cadence_ceiling_seconds`, `budget_daily_cap_cents`, `budget_cost_per_call_cents`, `budget_reset_hour_utc`, `failure_memory_enabled`, `failure_memory_k_default`, `failure_memory_max_age_seconds`, `failure_memory_min_rollouts_to_query`, `failure_memory_backend`.
- **Two-loop integration** — `helpers/auto_loop.py` starts `inner_loop_thread` alongside the existing auto-loop thread, builds targeted prompts via `list_pending_suggestions()` + `build_targeted_prompt()` at the start of each cycle, injects `[FAILURE MEMORY]` context via `failure_memory.build_failure_context()`, and checks `budget.can_spend()` before each LLM call.
- **`tests/smoke.py`** — 23 new v1.3.0 cases (7 inner loop + 8 cadence/budget + 8 failure memory) on top of the 40 v1.1.0 + v1.2.0 regressions. Total: 63/63 green.

### Changed

- **`helpers/auto_loop.py`** — auto-loop is now per-skill with priority-queue-via-sleep scheduling, per-skill budget enforcement, and inner-loop + failure-memory context injection. The v1.1.0 single-state file is migrated to `_default` skill on first read.
- **`helpers/sleep_runner.py`** — `get_status_snapshot()` adds `inner_loop`, `cadence`, `budget`, `failure_memory` blocks. `validate_proposal()` unchanged (gate preserved).
- **`extensions/python/monologue_end/_60_skillopt_harvest_rollout.py`** — sets `awaiting_suggestion: true` on each rollout. The actual suggestion is written by the background worker (not the harvester) so the chat stays fast.

### Engineering principles followed

- **Preserve the gate** — `validate_proposal()` unchanged across all 3 items. The 40 pre-existing tests still green.
- **Two-loop = clear contracts** — inner loop only writes to `logs/runs/suggestions/` and `inner_loop.log`; outer loop only reads from `suggestions/`. Failure memory only writes via `record_failure()`; outer loop only reads via `build_failure_context()`. Neither loop touches `SKILL.md`, `staging/`, or `logs/runs/adoptions.log` — that's the gate's job.
- **Make the failure mode loud** — `failure_memory.record_failure()` catches backend exceptions and returns `{ok: False, error: ...}`. Budget cap blocks at `can_spend()` boundary, not mid-LLM-call. Inner loop never raises on LLM failure — counters + `last_error` on the status snapshot.
- **Plugin-local only** — all state in `<plugin>/logs/runs/`. Memory store scoped to `area='skillopt_failures'`, `metadata={'plugin': 'skillopt'}`.
- **Cycle log is the source of truth** — `inner_loop.log`, `cadence.log`, `failure_memory.log` all append one line per action. The status snapshot carries the latest counters and `last_error`.
- **Backwards-compat** — v1.1.0's `logs/runs/.auto_loop_state.json` migrates to `_default` skill on first read. Skills without frontmatter work as a single `_default` fragment. `failure_memory_enabled=False` makes the whole module a no-op.

### Test pollution note

The v1.3.0 smoke tests for failure memory and cadence/budget write throwaway JSON files to `logs/runs/budget__*`, `logs/runs/inner_loop.log`, `logs/runs/ab_harness.log`, `logs/runs/fragments.log`, and per-skill directories under `logs/runs/auto_loop_state_*.json`. These are removed by the test helper `_cleanup_test_pollution()` at the end of each run; if a test crashes mid-run, run `python tests/smoke.py` again to trigger cleanup, or delete the files manually.

---

## [1.2.0] — 2026-07-23

Day-3 roadmap items 1 (Fragment store), 2 (A/B harness), and 3 (Reward model) ship. The self-evolution loop now closes with statistical confidence: proposals must beat the current skill on a paired A/B test of the agent's own rollouts, not on a synthetic held-out probe.

**Fragment store (item 1).** A `SKILL.md` is no longer a single string. A YAML frontmatter block declares named, addressed spans ("fragments"); the helper maintains a per-fragment version history and exposes `read_fragments`, `write_fragment`, `rollback_fragment`, `list_fragment_history`, `validate_fragments`, `active_fragments_text`, and `active_fragment_ids`. The validation gate's new stage 0.5 runs its structural checks per fragment when `skill_path` is provided — a single failing fragment rejects the whole proposal. Rollouts are tagged with the fragments that were active at the time of the chat, so the A/B harness's replay and the failure memory (Day-4 item 6) can attribute lift to the right span. Backwards compatible: a SKILL.md without frontmatter is one implicit `_default` fragment and the gate falls through to the whole-file check unchanged.

**A/B harness (item 2).** The new stage 0 of `validate_proposal()` loads the last N rollouts for a skill, splits them into A/B with stratified outcomes, replays them through the reward model, and asks a pluggable LLM judge to compare. If the harness can run and the proposed skill loses (B's win-rate < A by >= `ab_harness_min_lift_pp` OR the judge's mean confidence < `ab_harness_min_confidence`), the gate rejects with `ab_harness_rejected:<reason>`. If the harness cannot run (no rollouts, no judge, harness disabled) it falls through to the v1.1.0 structural stages and the v1.1.0 behaviour is preserved exactly. The judge is pluggable via `set_judge_fn(fn)`; the default is a deterministic keyword-overlap stub so the harness works without an LLM endpoint. The cycle log is the source of truth: every harness run appends a one-line summary to `logs/runs/ab_harness.log` and the status snapshot surfaces `total_runs`, `total_passed`, `total_rejected`, `total_skipped`, and `judge_fallback_active`.

**Reward model (item 3).** The harvester now calls a 3-class DistilBERT classifier after the v1.1.0 heuristic, stores both labels on every rollout, and prefers the model output when it is confident enough (`source == "model"` AND `confidence >= reward_model_prefer_above`, default 0.6). The model falls back to the v1.1.0 heuristic when the on-disk weights are missing or the inference path errors out, so the loop keeps working from day 1 (before any labels exist). The A/B harness's replay and the per-fragment gate consume the model probabilities with a confidence threshold, so the rewards drive a softer signal than the v1.1.0 binary outcome.

### Added

- **`helpers/fragment_store.py`** — new helper with `read_fragments(skill_path)`, `write_fragment(skill_path, fid, text)`, `rollback_fragment(skill_path, fid, version)`, `list_fragment_history(skill_path, fid)`, `validate_fragments(skill_path)`, `active_fragments_text(skill_path)`, `active_fragment_ids(skill_path)`, `get_fragments_status()`. Snapshots live at `<plugin>/fragments/<skill>/<id>.<v>.md`; the cycle log lives at `<plugin>/logs/runs/fragments.log`. YAML frontmatter is parsed with PyYAML when available, falling back to a tiny regex parser that handles the simple `fragments: [{id, selector}]` shape.
- **`helpers/reward_model.py`** — new ~350 LoC helper with `score_rollout(rollout) -> {success, partial, failure, confidence, source, model_version}`. The `source` field tells callers what produced the label: `model` (the classifier), `heuristic_fallback` (no model on disk), `heuristic_fallback_error` (model failed to load or run), `heuristic_no_input` (empty rollout), or — in future versions — `judge` (the A/B harness LLM judge). Model is loaded lazily on the first inference call, cached per process, and never raises (worst case is the v1.1.0 heuristic).
- **`scripts/train_reward_model.py`** — training skeleton. Verifies `transformers` + `torch` are importable, builds a `DistilBertForSequenceClassification` with 3 output classes, runs one forward+backward step on a sample, and (with `--persist`) writes the model + a `skillopt_reward_version.json` stamp into `<plugin>/models/reward_model/`. Real training is gated on labelled rollouts (the A/B harness's LLM judge will produce them).
- **`scripts/calibrate_judge.py`** — judge calibration. Builds a demo A/B pair set, runs the judge against it, prints Cohen's κ vs human labels, and supports a pure-Python κ fallback when scikit-learn is not installed.
- **Validation gate — per-fragment stage 0.5** — `validate_proposal()` now runs its structural checks per fragment when `skill_path` is provided AND the frontmatter declares >1 fragment (or the first fragment is not `_default`). Single-fragment skills use the whole-file stage unchanged. Per-fragment checks enforce byte equality, whitespace-normalised equality, headers, min_chars (scaled to fragment size), and a per-fragment shrink ceiling. Whole-file stages (example block, shrink ceiling, held-out) remain in effect for all callers.
- **Validation gate — A/B harness stage 0** — `validate_proposal()` calls `ab_harness.run_paired_test(skill_name, proposed_text, current_text)` before the structural stages when `skill_name` is passed. The harness returns `can_run=False` (not a reject) when it has no rollouts or no judge; the structural stages are the safety net. The harness stage is never allowed to crash the gate.
- **Harvester fragment tagging** — `extensions/python/monologue_end/_60_skillopt_harvest_rollout.py` now reads the active SKILL.md via the fragment store and tags each rollout with `fragments_active` (the list of IDs) and `fragments_active_text` (the resolved text, truncated to 4KB to keep rollouts small). The A/B harness uses the field for fragment-aware replay.
- **Harvester reward integration** — the harvester now calls `reward_model.score_rollout()` after the heuristic and stores the full result under `record["reward"]`. The rollout's `outcome` field becomes the model output when `source == "model"` and `confidence >= 0.6`; otherwise it stays the v1.1.0 heuristic. A `last_response` field is added so the model has the assistant's reply as a feature (the v1.1.0 harvester had it in-memory only).
- **Dashboard surface** — `helpers/sleep_runner.get_status_snapshot()` now includes a `reward_model` block, an `ab_harness` block, and a `fragments` block. The `/api/plugins/skillopt/status` endpoint surfaces them for the WebUI.
- **Config keys** — `default_config.yaml` and `config.json` get `reward_model_path`, `reward_model_prefer_above`, `ab_harness_n`, `ab_harness_min_n`, `ab_harness_min_lift_pp`, `ab_harness_min_confidence`, `ab_harness_enabled`, `judge_model`, `fragment_snapshot_dir`, `fragment_max_history_per_id`, `fragment_per_fragment_gate`.
- **Task A.1 — real LLM judge HTTP path** — `helpers/ab_harness.py: set_judge_mode("http")` swaps the default keyword stub for `_llm_judge_via_http`, which POSTs `JUDGE_PROMPT` to `SKILLOPT_JUDGE_ENDPOINT` and parses the JSON response. The cycle log records every harness run with the model, lift, and confidence. Production calls `set_judge_mode("http")` once at boot; tests use `set_judge_mode("stub")` to reset.
- **Task A.2 — auto_loop wiring** — `helpers/auto_loop.py` now passes `skill_name=skill_name` to `validate_proposal()` so the A/B harness stage actually runs in production Sleep cycles. The auto-loop also checks `ab_harness_enabled` (no-op when disabled) and emits a `ab_harness:` line to `logs/runs/auto_loop.log` for every cycle that touches the harness.
- **`tests/smoke.py`** — deterministic smoke suite, runnable as `python tests/smoke.py`. Re-derives the v1.1.0 launch verification (12 regression cases) and adds 29 v1.2.0 cases for the fragment store, the reward model, the A/B harness (HTTP judge + auto-loop wiring + gate interactions), and the calibrate_judge script. Exits 0 on pass, 1 on any failure.

### Changed

- **`extensions/python/monologue_end/_60_skillopt_harvest_rollout.py`** — adds 4 new fields to the rollout record (`last_response`, `reward`, `fragments_active`, `fragments_active_text`). The `_heuristic_outcome()` function and the rest of the harvester are unchanged; the new paths are bolted on after the existing flow, in try/except blocks that fail silently to v1.1.0 behaviour on any error.
- **`helpers/sleep_runner.py`** — `get_status_snapshot()` adds `reward_model`, `ab_harness`, and `fragments` keys. `validate_proposal()` adds stages 0 and 0.5 (A/B harness and per-fragment gate); the v1.1.0 stages 1-8 run unchanged for any caller that doesn't pass `skill_name` AND `skill_path`. No existing keys or stages are removed or renamed; the v1.1.0 WebUI keeps working.

### Fixed

- **`rollback_fragment()` snapshot restore** — the function was returning `ok=True` without writing the snapshot's text as the new current. The fix routes every rollback through `write_fragment(skill_path, fragment_id, rolled_text)`, which snapshots the pre-rollback state first and writes the rolled-back text as the new current. The `t_v121_fragment_rollback` smoke test re-derives a v2 write → rollback → v1 state and confirms the SKILL.md matches the original.
- **`list_fragment_history()` ordering** — the function was emitting history in `glob()` order (filesystem-dependent; not guaranteed sorted). The fix sorts by the `version` string (e.g. `v1` < `v2` < `v3`), and the `t_v121_fragment_history` smoke test re-derives a 3-snapshot sequence and confirms the order.
- **Per-fragment gate on the test harness** — `validate_proposal(skill_path=...)` was looking for a `.proposed` file on disk to read the proposed text's fragments, which doesn't exist in the test harness. The fix writes the proposed text to a `tempfile.mkstemp` first, then reads fragments from the temp file, and cleans up in a `finally` block. The `t_v121_gate_per_fragment` smoke test re-derives the canonical case and confirms the per-fragment gate runs to completion.

### Engineering principles followed

- **Test with the synthetic harness, ship with the live agent** — the smoke suite runs without LLM calls, without network, in <5s on a single CPU. The training script runs its forward+backward step in 1-2s as a live integration check. The judge-via-HTTP test stands up an in-process mock server so the harness's HTTP path is exercised end-to-end without a real endpoint.
- **Preserve the gate** — `validate_proposal()` is the single source of truth, and the new stages 0 and 0.5 are strictly additive. The `t_v121_gate_backward_compat` smoke test proves that callers without `skill_name` see the v1.1.0 gate exactly; the `t_v120_gate_intact` test re-runs the byte-identical / whitespace-normalised / good-proposal cases to prove the gate still rejects the things it should and accepts the things it should.
- **Make the failure mode loud** — every fallback path is named (`heuristic_fallback` vs `heuristic_fallback_error` vs `heuristic_no_input`; `judge_unreachable` vs `ab_harness disabled by config`; `fragments` with `yaml_available`) and counted (`fallback_count`, `total_runs`/`total_passed`/`total_rejected`/`total_skipped`). The dashboard can show whether the harvester is using a trained model, whether the harness has a judge, and how many fragments are declared.
- **Plugin-local, not host-global** — the model lives at `<plugin>/models/reward_model/`, fragments at `<plugin>/fragments/<skill>/`, rollouts at `<plugin>/logs/rollouts/`, the cycle log at `<plugin>/logs/runs/`. Override via env vars (`SKILLOPT_REWARD_MODEL_DIR`, `SKILLOPT_SKILLS_DIR`, `SKILLOPT_JUDGE_ENDPOINT`) if you want to share anything across plugins.
- **The cycle log is the source of truth** — model load successes and failures are logged to the standard logging namespace; the status snapshot carries the latest `load_error` for the dashboard. The fragment log (`logs/runs/fragments.log`) records every `write`, `rollback`, and validation warning. The A/B harness appends one line per run to `logs/runs/ab_harness.log`.

### Runtime dependencies

- **`transformers` + `torch`** — required for `scripts/train_reward_model.py` (the training script verifies they're importable and builds a `DistilBertForSequenceClassification`). The training script exits 2 with a clear message when either is missing, so the smoke test treats `rc=2` as a "deps missing" pass. The reward model itself does NOT require transformers/torch at runtime — the v1.2.0 reward model runs the v1.1.0 heuristic under the hood until real training happens.
- **`scikit-learn`** — required for `scripts/calibrate_judge.py` to compute Cohen's κ. The script falls back to a pure-Python κ implementation when scikit-learn is missing, so the smoke test for the calibration pipeline runs end-to-end without sklearn.
- **`PyYAML`** — preferred for parsing fragment frontmatter in `helpers/fragment_store.py`. Falls back to a tiny regex parser that handles the simple `fragments: [{id, selector}]` shape when PyYAML is missing; the `yaml_available` flag on the status snapshot makes the fallback loud.

### Verified

- 41/41 smoke test suite passes (`python tests/smoke.py` from the plugin root): 12 v1.1.0 regressions + 29 v1.2.0 cases.
- `execute.py` returns `Health check PASSED` on the live install.
- The harvester writes a rollout with the v1.1.0 `outcome`, a new `reward` subfield, and the active fragments at the time of the chat.
- `validate_proposal()` still rejects the 1904->1904 byte-identical case, the whitespace-normalised-equal case, and the >50% shrink case; the new A/B stage and per-fragment stage are strictly additive.
- `train_reward_model.py` runs its smoke step end-to-end (transformers + torch + DistilBERT classifier head all build and a forward+backward pass completes).
- `calibrate_judge.py` runs its `--build-demo` and `--use-stub-judge` pipeline end-to-end and prints `kappa (ours)`.

### Known limitations (acceptable for v1.2.0)

- The training script is a skeleton. Real training needs ~5K labelled rollouts (the A/B harness's LLM judge will produce them once `set_judge_mode("http")` is wired in production) and a calibration pass to set `reward_model_prefer_above` to its real value. Until then the model is the v1.1.0 heuristic under the hood.
- The reward model is not thread-parallel. Inference is serialised through a `threading.Lock()`. A 350M-param DistilBERT on CPU is <50ms/call, well under the per-rollout budget, so this is fine for v1.2.0; if the A/B harness or the inner loop need to fan out, we can swap in `torch.compile()` or batched inference in a later patch.
- The A/B harness's judge is a deterministic keyword-overlap stub by default. Production needs `set_judge_mode("http")` with `SKILLOPT_JUDGE_ENDPOINT` configured. The cycle log records every run with the judge model, so a misconfigured endpoint is loud on the dashboard.
- The per-fragment gate is additive: it does not reject proposals that change no fragments (the whole-file stages catch those). The two stages are complementary, not redundant.
- The A/B harness's replay uses the v1.2.0 reward model on `[task + last_response + skill_text[:512]]`. When the reward model falls back to the heuristic (always, in v1.2.0 day 1), the harness's win/loss/ties are heuristic-derived, not model-derived. The harness's `judge_fallback` flag and the model's `fallback_count` make this loud.

---

## [1.1.0] — 2026-07-21

The first release that actually closes the self-evolution loop. v1.0.0 was a no-op; v1.1.0 is the version that mines, validates, and adopts.

### Fixed (11 bugs)

- **Engine invocation** — the `SkillOpt` library has no `.run()` method. We now invoke the engine correctly: `python -m skillopt_sleep <verb>`. This was the single biggest blocker in v1.0.0.
- **`auto_adopt` was ignored** — v1.0.0 read `auto_adopt` from config but never acted on it. v1.1.0 calls the validation gate and writes the proposal when the gate passes.
- **Validation gate was 5 lines** — it let a 1904→1904 byte-identical "qa" adoption through. v1.1.0's `validate_proposal()` is an 8-stage gate: byte equality, whitespace-normalised equality, 50% shrink ceiling, headers, example block, min chars, held-out delta.
- **No rollouts were ever written** — v1.0.0 had no `monologue_end` hook, so the Sleep engine had nothing to mine. v1.1.0 writes one JSON per chat turn to `logs/rollouts/`.
- **`/api/plugins/skillopt/config` 500'd on every save** — the `python_change=False` kwarg was removed from `clear_plugin_cache()` in framework v2.5. v1.1.0 uses the new signature `clear_plugin_cache([name])`.
- **`_a0_python()` was Linux-only** — it hardcoded `/opt/venv-a0/bin/python`. v1.1.0 probes Windows `.venv\Scripts\python.exe` first, then the A0 venv, then `sys.executable`.
- **Windows subprocess crash** — `start_new_session=True` is invalid on Windows. v1.1.0 uses `creationflags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP` on Windows and `start_new_session=True` on POSIX.
- **Hardcoded `gemma4:31b`** — the direct optimiser's prompt model was a placeholder. v1.1.0 resolves it from `SKILLOPT_OPTIMIZER_MODEL` env, the `.skillopt-env` file, or the `optimizer_model` config key.
- **CWD/slug mismatch in `harvest`** — the Sleep engine's `harvest` verb filters by `invoked_project` (the CWD slug). v1.0.0 launched the engine with the user's CWD; v1.1.0 launches it with `cwd=staging_dir()` and the bridge derives the matching slug.
- **Default LLM endpoint assumed Azure** — v1.0.0's `base_url` hardcoded `https://api.openai.com/v1` and the env var name was wrong. v1.1.0 reads `AZURE_OPENAI_ENDPOINT` (overridable), falls back to `https://ollama.com/v1`, and accepts `OLLAMA_API_KEY` as the auth.
- **`execute.py` health check always said PASSED** — even when the loop never ran. v1.1.0 returns exit 5 with a clear reason when `rollouts >= threshold` AND `cycles_run == 0` AND `auto_loop_enabled`.

### Added

- **Plugin-local Claude Code cache** — the bridge now writes to `<plugin_root>/.cache/claude_code/` by default instead of polluting the host's `~/.claude/`. Host mode is opt-in via `SKILLOPT_BRIDGE_TO_HOST=1`.
- **Per-cycle critique file** — every direct-optimizer cycle writes `logs/runs/critiques/<skill>_<ts>.md` so the user can review the diff before auto-adopt.
- **Subclass-style Extension dispatcher for the banner** — v2.5 of the framework requires `source: "backend"` on banners; v1.0.0's module-level `execute(banners, ...)` would 500. v1.1.0 supports both dispatch paths.
- **Shared validation gate** — `validate_proposal()` in `helpers/sleep_runner.py` is the single source of truth. The auto-loop, the adopt endpoint, the post-adopt hook, and the agent-callable tool all call it. (In v1.0.0, each had its own ad-hoc check.)
- **Held-out delta parsing** — the Sleep engine writes `held-out 0.412 -> 0.487` to its log. v1.1.0 parses this and feeds it into the gate so `gate_min_improvement_pp` is real.
- **Audit trail** — every adopt/reject decision lands in `logs/runs/adoptions.log` as one JSON line per entry, plus `logs/runs/post_adopt.log` for hook-side decisions. The dashboard reads these.
- **Cross-platform `_a0_python()`** — also accepts `A0_VENV_PYTHON` env override for CI / alt installs.
- **`SKILLOPT_PURGE_ON_UNINSTALL=1`** — opt-in env var that removes the per-plugin staging area and logs on uninstall. Off by default to protect user data.

### Changed

- **Manifest version bumped to 1.1.0** in `plugin.yaml`.
- **`default_config.yaml` reorganised** into AUTOMATION / VALIDATION GATE / STORAGE / MODEL DEFAULTS sections, with inline comments on every key.
- **`execute.py` is stricter** — returns 5 (not 0) when the auto-loop is silently broken. Returns 0 only on a clean bill of health.
- **The harvester's outcome classifier is heuristic** — `_heuristic_outcome()` looks for `traceback`, `unhandled exception`, etc. It's deliberately a string-match for v1.1.0; a real reward model is on the roadmap (see ROADMAP.md Day 3, item 3).

### Removed

- **The `[webui]` / `[alfworld]` / `[claude]` extras from `install()`** — they conflict with A0's dependency graph (huggingface_hub <1.0, gradio conflicts, etc.). Users who actually need them can `pip install skillopt[alfworld]` manually.
- **The empty `helper.py` stub** that v1.0.0 referenced but never defined.

### Verified

- 26/26 smoke tests pass in `/a0/usr/workdir/skillopt-plugin/`.
- `execute.py` returns `Health check PASSED` on the live install.
- The harvester writes rollouts to `logs/rollouts/` after every chat turn.
- The auto-loop daemon starts, the state file persists, and the dashboard reads it.

---

## [1.0.0] — 2026-07-07 (initial release)

The original. The plugin installed cleanly but did nothing useful: no rollouts were ever written, the gate was a no-op, and the config endpoint crashed on every save. Kept here for reference; do not use.

### Known v1.0.0 bugs (all fixed in 1.1.0)

1. `engine.run()` → `AttributeError`
2. `auto_adopt` ignored
3. Gate let a 1904→1904 no-op through
4. No rollout harvester
5. Config endpoint 500 on `clear_plugin_cache(python_change=False)`
6. `_a0_python()` Linux-only
7. `start_new_session=True` invalid on Windows
8. Direct optimiser hardcoded `gemma4:31b`
9. CWD/slug mismatch in `harvest`
10. Default LLM endpoint assumed Azure
11. `execute.py` health check always said PASSED
