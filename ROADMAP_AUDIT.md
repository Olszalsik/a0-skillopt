# SkillOpt Plugin — Roadmap Audit (ROADMAP_AUDIT.md)

Generated: 2026-09-17 (CEST). Audit executed live in `/a0/usr/plugins/skillopt/` (read-only except this file). Method: full filesystem inventory, `git log/status/rev-list/remote/tags`, extraction of `plugin.yaml`, `ROADMAP.md`, `CHANGELOG.md`, plus a live run of the hermetic smoke suite during this audit. No phantom claims: every item below was verified on disk or in git in this session.

## 0. Snapshot (verified)

- Repo: local `main` HEAD `8885c53` — v1.8.12 follow-up (hermetic rollouts-dir isolation for the two v1.2.0 A/B no-rollouts tests). Worktree **clean** (no uncommitted changes).
- Remote: `origin https://github.com/Olszalsik/a0-skillopt.git`; `origin/main` = `967d6b5` (tag `v1.8.9`). **Local main is 5 commits ahead, unpushed**:
  1. `6f08f6e` v1.8.10 — claude-home bridge wiring, atomic rollout writes, chat_shepherd harvest filter, first live gated cycle (fail-closed reject)
  2. `d532baf` v1.8.11 — mock-scorer task-side coverage fix (keyword dilution 45→81), judge burst throttle
  3. `3d1d723` v1.8.12 — chat-model sentinel (`helpers/chat_model.py`), optimizer/target/judge follow the active A0 chat model
  4. `8bd8cf4` ROADMAP docs entry for v1.8.12
  5. `8885c53` v1.8.12 follow-up — hermetic test isolation
- `plugin.yaml` version: **1.8.12**; activation: `.toggle-1` (plugin active).
- Deployed-copy parity: `/a0/usr/workdir/skillopt-plugin/plugin.yaml` also reports 1.8.12 (independent git repo).
- **Live smoke suite run during this audit**: `tests/smoke.py` → **150 registered / 150 passed / 0 failed**. Reward model loads in the deployed runtime (`models/reward_model/model.safetensors`, 104/104 weights).

## 1. Implemented components (by proposed phase)

### Phase 1 — Plugin Foundation & Manifest — DONE (since v1.1.0)
- `/a0/usr/plugins/skillopt/plugin.yaml` — manifest, version 1.8.12
- `/a0/usr/plugins/skillopt/plugin.py` — bootstrap
- `/a0/usr/plugins/skillopt/hooks.py` — install + pre_update wiring (8.5 KB)
- `/a0/usr/plugins/skillopt/default_config.yaml` — documented configuration knobs (19.7 KB)
- `/a0/usr/plugins/skillopt/.toggle-1` — active
- Supporting root files: `README.md`, `SETUP.md`, `INSTALL.md`, `RELEASE_NOTES.md`, `CHANGELOG.md` (72 KB, full v-history), `ROADMAP.md` (42 KB), `ANALYSIS_v2.5.md`, `AGENTS.md`, `LICENSE`, `execute.py`, `.github/workflows/ci.yml`, `plugin-hub/index.yaml` (hub submission record)

### Phase 2 — Trajectory Logging & Rollout Capture — DONE (operational)
Extensions under `extensions/python/`:
- `monologue_end/_60_skillopt_harvest_rollout.py` — live rollout harvesting on turn end
- `agent_init/_50_skillopt_auto_loop.py` — auto-loop bootstrap
- `monologue_start/_40_skillopt_warn.py` — pause/warn hook
- `banners/_10_skillopt_status.py` — status banner
- `hooks/_post_skill_adopt.py` — post-adopt hook
WebUI injection: `extensions/webui/page-head/skillopt-head.html`, `extensions/webui/sidebar-end/skillopt-card.html`
Data: `logs/rollouts/*.json` — 40+ live harvested rollouts (zero-rollout harvesting bug fixed in v1.8.9; atomic writes added v1.8.10); `logs/rollouts_backup/` — 69 synthetic/fixture rollouts.

### Phase 3 — Trajectory Reflection & Bounded Edit Engine — DONE
`helpers/`: `direct_optimizer.py` (bounded add/delete/replace proposals), `ab_harness.py`, `fragment_store.py` + `fragments/` store, `inner_loop.py` (per-task critique/suggest), `cadence.py`, `cycle_history.py`, `chat_model.py` (v1.8.12 chat-sentinel resolver), `official_adapter.py` (Microsoft SkillOPT adapter), `bridge.py` (claude-home bridge, v1.8.10), `setup_env.py`. Live artifacts: `logs/runs/suggestions/*.md`, `logs/runs/critiques/`.

### Phase 4 — Validation Gate & Sandbox Evaluator — DONE (gate preserved fail-closed)
`helpers/`: `replay_harness.py` (offline replay executor + size-invariant mock scorer, v1.8.11 fix), `llm_judge.py` (burst-throttled, v1.8.11), `reward_model.py` + `models/reward_model/` (trained DistilBERT: `model.safetensors`, `tokenizer.json`, `calibration.json`, `skillopt_reward_version.json`), `governance.py` (per-skill policy scopes v1.8.6, pause/resume v1.8.8), `budget.py` (two-tier soft/hard caps v1.8.7), `failure_memory.py` (rolling backups v1.8.7).
Live evidence: `staging/.skillopt-sleep/staging/20260915-125330/` (manifest/report/diagnostics) + `.cache/.skillopt-sleep/state.json` — night 1, 14 tasks harvested, candidate **rejected fail-closed** by the gate (exact intended behavior). `staging/security-scan-untrusted-plugin.md` staged skill.

### Phase 5 — Agent Tools & WebUI API — DONE
`tools/` (4): `skillopt_setup.py`, `skillopt_train.py`, `skillopt_status.py`, `skillopt_sleep.py`
`api/` (15 handlers): `status.py`, `config.py`, `cycles.py`, `fragments.py`, `adopt.py`, `reject.py`, `rollback.py`, `staged.py`, `sleep.py`, `loop.py`, `audit_log.py`, `governance_approve.py`, `governance_pause.py`, `governance_status.py`
`webui/`: `skillopt-dashboard.js` (CSRF-safe since v1.8.9), `config.html` (one-click pause/resume, v1.8.8), `thumbnail.png`, `docs/screenshot-*.png`

### Phase 6 — SkillOpt-Sleep Offline Background Engine — DONE (operational)
- `helpers/sleep_runner.py`, `helpers/auto_loop.py`
- `scripts/`: `train_reward_model.py`, `label_rollouts.py`, `calibrate_judge.py`, `replay_worker.py`
- `agents/skillopt_trainer/agent.yaml` — dedicated subordinate profile
- Runtime: `logs/runs/sleep-run-2026091*.log` (3 real runs), `.cache/.skillopt-sleep/state.json` (night 1 complete)

## 2. Recorded roadmap vs proposed 6-phase roadmap

| # | Proposed phase | Status | Key evidence (verified) |
|---|----------------|--------|--------------------------|
| 1 | Manifest & hooks | DONE (v1.1.0) | plugin.yaml v1.8.12, hooks.py, default_config.yaml, .toggle-1 |
| 2 | Trajectory capture extension | DONE (v1.7.0; fixed v1.8.9; v1.8.10 atomic writes) | monologue_end harvester + 40+ live rollouts |
| 3 | Optimization core & bounded edits | DONE (v1.1.0–v1.4.0) | direct_optimizer.py, inner_loop.py, cadence.py, fragment_store.py |
| 4 | Validation gate & sandbox evaluator | DONE (gate v1.1.0; reward model + judge v1.8.0; scorer fix v1.8.11) | replay_harness.py, llm_judge.py, models/reward_model/ |
| 5 | Tools & API routes | DONE (v1.4.0–v1.8.8) | 4 tools, 15 API handlers, dashboard + config UI |
| 6 | SkillOpt-Sleep engine | DONE (v1.8.0; first live gated cycle v1.8.10) | sleep_runner.py, 4 scripts, trainer agent, staging evidence |

The plugin's own `ROADMAP.md` is a **superset** of the proposal: items 1–11 are all DONE through v1.8.12, with status addenda recorded through 2026-09-15. No proposed phase is missing or partially implemented.

## 3. Where development left off — pending items

1. **Unpushed commits**: 5 local commits ahead of `origin/main` (listed in §0). Hub PR #512 was cut from the v1.8.5 line (`origin/main` at `7bd1328`), so the PR does not yet contain v1.8.10–v1.8.12.
2. **Item 9 — hub merge**: PR #512 open (recorded in `ROADMAP.md` addenda; not re-verified against GitHub live in this audit).
3. **Optional**: `replay_real_executor_enabled` (real replay executor) remains off by default; the mock-scorer size-invariance fix (v1.8.11) addressed the diagnosed 45→81 keyword-dilution spurious reject that motivated enabling it.
4. **No partial/stub modules found**: worktree clean, no unstaged files, pycache present for py3.12/3.13 (framework recently exercised), no stub-only files in the inventory.

## 4. Documented deviations from the proposed design (intentional)

1. **Transcript path**: proposed `data/transcripts/` implemented as `logs/rollouts/` (gitignored runtime data) + `logs/rollouts_backup/` fixtures.
2. **Promotion target**: validated skills promote into `usr/skills/` via the staging/adopt/rollback chain (`staging/`, `api/adopt.py`, `api/rollback.py`) rather than overwriting a single `best_skill.md`.
3. **No `skillopt_eval.py` tool**: evaluation is exposed through `/api/loop`, `helpers/replay_harness.py`, and the `skillopt_train.py`/`skillopt_sleep.py` tools instead of a dedicated eval tool.
4. **Model routing**: as of v1.8.12, optimizer/target/judge models follow the active Agent Zero chat model (`chat` sentinel, `helpers/chat_model.py`); `minimax-m3` retired as implicit default.
5. **Beyond the proposal**: reward-model training pipeline, LLM-judge calibration, governance (per-skill policies + one-click pause), budget caps with soft warnings, failure memory with rolling backups, fragment store, auto-loop cadence, plugin-hub submission — all shipped above and beyond the 6 phases.

## 5. Next single unit of work

**Push the 5 unpushed commits to `origin/main`** (`git push origin main`), then update hub PR #512 so it carries v1.8.10–v1.8.12 (currently based on the v1.8.5 line). This is the smallest atomic step that unblocks item 9 (hub merge).

## 6. Constraints compliance

- Audit performed read-only: no functional code modified (verified via clean `git status` before and after; the only new file is this `ROADMAP_AUDIT.md`).
- No files touched outside `usr/plugins/skillopt/` (the `/a0/usr/workdir/skillopt-plugin` mirror was only read for version parity).
- Smoke-suite temp log (`/tmp/skillopt_smoke_audit.log`) is disposable and outside the plugin tree.

## 7. Post-audit addendum (2026-09-17, same day — sync executed)

- The 5 commits listed in §0 were pushed to `origin/main` as a **clean fast-forward** (`967d6b5..8885c53`, no force, linear history verified beforehand). Local `main` and `origin/main` both at `8885c53`; `git status` reports `Your branch is up to date with 'origin/main'`.
- Hub PR #512 (head `Olszalsik:add-skillopt` @ `b41bb85`, base `agent0ai:main`) updated via GitHub REST API at 2026-09-17T12:39:40Z: body now lists v1.8.6–v1.8.12 and the smoke line reads 150/150 green on v1.8.12. No PR-branch commits were needed — `plugins/skillopt/index.yaml` links the plugin repo's `main` branch, which now serves v1.8.12.
- Optional hygiene remaining: remote release tags `v1.8.10`–`v1.8.12` not yet pushed.
- Next unit of work: item 9 — hub merge of PR #512 (maintainer side).
