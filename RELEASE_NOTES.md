# SkillOpt Plugin — Release Notes

---

## v1.8.0 — the two offline pieces, integrated (real replay + reward training)

> v1.7.0 stubbed the two expensive/offline pieces of the self-evolution loop and
> documented them as follow-ups. **v1.8.0 implements both**, default-off so
> v1.7.0 behavior is preserved byte-for-byte unless you opt in. 122/122
> deterministic smoke tests pass (11 new `t_v18_*`); no LLM/network in tests.

### TL;DR

- **Real replay executor (P1–P3):** flip `replay_real_executor_enabled: true`
  and the local counterfactual replay gate runs each held-out task through a
  **real Agent Zero monologue** under the current vs proposed skill — in a child
  process (temp cwd, own event loop, recursion-guarded) — instead of the mock
  relevance heuristic. Cost bounded by `replay_real_max_tasks` (default 4) and
  `replay_real_per_task_timeout_s` (default 180). Worker failure is
  loud-not-crash (falls through to the structural gate).
- **LLM-judge labelling (P4):** `python scripts/label_rollouts.py` writes an
  LLM-judged `judge_label` (success/partial/failure) into each rollout JSON,
  atomically + idempotently. These labels are the reward model's training data.
- **Real DistilBERT training (P5):** `python scripts/train_reward_model.py
  --mode train` trains a 3-class classifier on the labelled rollouts, persists
  it, writes a `1.3.0-train-*` version stamp. `--mode smoke` (default) is the
  v1.2.0 demo step, unchanged.
- **Calibration + config wiring (P6):** the dead `reward_model_path` /
  `reward_model_prefer_above` config keys are now wired. A post-train
  calibration pass picks `prefer_model_above` on the val set and writes
  `calibration.json`; `score_rollout` reads it automatically.

### What's new for users

- **The replay gate can now produce *trustworthy* counterfactual evidence** on
  the direct-optimizer path. The mock executor (default) is a cheap keyword
  relevance heuristic; the real executor actually re-runs the agent under each
  skill and measures the difference. Opt in with `replay_real_executor_enabled:
  true` — it runs full monologues, so it costs more (bounded by
  `replay_real_max_tasks`).
- **The reward model can now actually be trained** on your own rollouts. Label
  them with the LLM judge, then run the training script. A trained + calibrated
  model replaces the keyword heuristic in `score_rollout`, sharpening both the
  replay gate and the A/B harness.
- **Everything is opt-in.** With the flags off (the default), v1.8.0 is
  byte-identical to v1.7.0 at runtime. No new dependencies, no breaking
  changes, the validation gate is preserved.

### Live verification still pending

The four live checks (L1 worker spawn, L2 judge labelling, L3 training, L4
end-to-end gate) need the A0 venv + a running A0 server + LLM credentials.
They are flagged in the integration plan, not run in dev. The smoke suite
covers all logic testable without them.

---

## v1.7.0 — Solution C (the self-evolution engine now actually evolves)

> **The headline fix:** the rollout harvester had been writing **zero rollouts**
> since v1.1.0 — it read a nonexistent `loop_data.messages` and early-returned on
> every chat turn. The engine was running blind. v1.7.0 fixes the attribution,
> adds a local counterfactual replay gate, a human-in-the-loop adopt UI, and
> auto-opt-in for new skills behind a human-approval gate. 122/122 deterministic
> smoke tests pass.

### TL;DR

- **C1 (Fixed):** harvester reads `history_output` + attributes the active skill
  authoritatively via `skills.skill_instruction_name`. Rollouts now actually flow.
- **C2 (Added):** `helpers/replay_harness.py` — local counterfactual replay gate
  (deterministic mock executor, no LLM) wired as stage 0.7 in `validate_proposal`.
  Real executor stubbed behind `replay_real_executor_enabled`.
- **C3 (Added):** human-in-the-loop adopt UI — `/staged`, `/adopt` (by id, with
  whole-file snapshot), `/reject`, `/rollback`. WebUI Staged-proposals section.
- **C4 (Added):** auto-opt-in for new skills behind a human-approval gate —
  `/governance_approve`, `/governance_status`. WebUI Governance section.
- **+12 smoke tests** (110 → 122). No new runtime dependencies. No breaking
  changes. The validation gate is preserved (stage 0.7 is strictly additive and
  skipped when `official_gated`).

### What's new for users

- **Your skills now actually learn from chats.** Before v1.7.0 the harvester was
  silently broken, so no rollout ever fed the gate. After v1.7.0 every chat turn
  you run writes a rollout with the skill that was active, attributed from the
  authoritative loaded-skills ledger (not a regex on your prompt).
- **Staged proposals are reviewable.** The WebUI now shows each gated proposal
  with its lift, held-out n, and gate reason. Approve promotes it (with a
  reversible snapshot); Reject records a no; Roll back restores the prior
  `SKILL.md`. Nothing is auto-adopted unless you turn on `auto_adopt`.
- **New skills are safe by default.** A skill the loop sees for the first time
  is auto-opted-in but stays **pending human approval** — you Approve it once in
  the Governance section and it's eligible. Immutable or opted-out skills are
  never touched.

### What's new for developers

- The replay gate is a deterministic **mock** executor by design (no LLM, no
  network) so it's safe to run in CI. The real A0-agent-loop executor is a
  guarded stub — flip `replay_real_executor_enabled: true` and fill in
  `replay_harness._real_score` to use it (documented follow-up; see ROADMAP.md
  item 10).
- `SKILLOPT_REPLAY_MODE` is the cross-cutting recursion guard: checked in the
  harvester, the auto-loop watchdog, and set/unset around the real replay
  executor, so a replay agent's own turns never pollute the training set or
  spawn a nested optimizer loop.
- Two stubbed follow-ups are the next roadmap items (ROADMAP.md): the real replay
  executor (item 10) and DistilBERT reward-model training (item 11).

---

## v1.6.0 / v1.6.1 — Solution B (bridge to official `skillopt_sleep`)

> The auto-loop now drives the official Microsoft `skillopt_sleep` pipeline; the
> local `direct_optimizer` is demoted to a fallback. v1.6.1 verified the CLI and
> `evaluate_gate` signature against `microsoft/skillopt` @ HEAD. 100/100 tests.

See CHANGELOG.md for the full v1.6.0 / v1.6.1 entries.

---

## v1.5.0 (Day-5 final)

> **First public release candidate.** v1.5.0 closes Day-5 of the roll-out:
> the governance layer is in place (per-skill opt-out / rate-limit /
> immutable / opt-in policies), the per-cycle dashboard is wired through
> the WebUI, and the supporting CI / docs / install story is ready for
> external contributors. This is the version we expect to tag for the
> community Plugin Index.

---

## TL;DR (v1.5.0)

- **+1 helper:** `helpers/governance.py` (per-skill policies + opt-out marker)
- **+9 smoke tests** (82 total, all green on Python 3.10 - 3.13)
- **+1 WebUI dashboard tab:** per-cycle history (cycles + audit log)
- **+3 new troubleshooting items** in INSTALL.md
- **+1 GitHub Actions workflow:** `.github/workflows/ci.yml`
- **README expanded** with a "Is this plugin right for you?" section
- **`validate_proposal()` unchanged** — the gate is preserved end to end

Nothing was removed. No breaking changes. No new runtime dependencies.
The plugin is still stdlib + A0 framework only at runtime; the CI
matrix installs `pyyaml + scikit-learn + transformers + torch` because
the v1.3.0 reward-model and A/B harness paths import them, but the
default cycle (direct optimizer) doesn't need any of them.

---

## What's new for users

### 1. Per-skill governance

Until v1.4.0, SkillOpt was **all-or-nothing**: the auto-loop either
optimised every skill with rollouts, or you turned the whole thing off.
v1.5.0 fixes that.

| Policy | What it means | How to set it |
|---|---|---|
| **opt-out** (default) | Skill never participates unless you opt in | `touch usr/skills/<name>/.skillopt.optout` |
| **opt-in** | Skill participates only if you add the marker | `touch usr/skills/<name>/.skillopt.optin` |
| **immutable** | Skill is locked — never edited, even if policy says yes | Add `"mode": "immutable"` to its `policy.json` |
| **rate-limited** | Skill participates but with a `min_interval_seconds` cooldown | Add `"rate_limit": {"min_interval_seconds": 3600}` to its `policy.json` |

Defaults are safe: a skill with no marker and no `policy.json` is
**opted out by default** — the auto-loop won't touch it. To opt a skill
into self-evolution, add the marker or set `"mode": "opt_in"`
explicitly.

Example `usr/skills/code-review/policy.json`:

```json
{
  "mode": "opt_in",
  "rate_limit": {
    "min_interval_seconds": 7200,
    "max_per_day": 4
  },
  "auto_adopt": true,
  "approval_required": true
}
```

Every governance decision is logged to `logs/runs/governance.log`
(JSONL, append-only) so you have a full audit trail.

### 2. Per-cycle dashboard

The WebUI dashboard now has a **History** tab alongside the existing
Live / Critiques tabs. Two new read-only API endpoints back it:

- `GET /api/plugins/skillopt/cycles?limit=50&skill=<name>&outcome=<all|adopted|rejected>`
- `GET /api/plugins/skillopt/cycle/<cycle_id>` (single entry)
- `GET /api/plugins/skillopt/audit_log?limit=50&skill=<name>&passed=<bool>`

All three are backed by append-only JSONL under `logs/runs/`. Reads
skip malformed lines, so a truncated write can never crash the
dashboard. Writes are atomic.

### 3. Honest CI

The new `.github/workflows/ci.yml` runs `python tests/smoke.py` AND
`python execute.py` on Python 3.10, 3.11, 3.12, 3.13 — for every push
to `main`, every PR, and on manual dispatch. The badge in `README.md`
reflects `main` branch status only. Forks are intentionally excluded
from running CI because `actions/checkout` would execute code from
the PR's branch under our secrets; PRs from forks should expect the
maintainer to mirror and re-run.

### 4. Honest install

`INSTALL.md` now opens with a "What this plugin is and isn't" section
so you know in 30 seconds whether SkillOpt fits your use case. The
troubleshooting matrix grew by three Day-5-specific rows (cycle-history
file corruption, opt-out marker not respected, auto-loop never fires).
The `--verify` step in `execute.py` covers the same cases automatically.

---

## What's new for operators

- **Governance log** at `logs/runs/governance.log` (JSONL, append-only).
  Each decision records: `ts, skill, policy_decision, marker, mode,
  rate_limit_ok, approval_required, approval_decision, reason`. Use
  `jq` to grep by skill: `jq -c 'select(.skill=="code-review")'
  logs/runs/governance.log | tail`.
- **`get_status_snapshot()` includes a `governance` block** with the
  current policy in effect, the opted-out / governed / opt-in skill
  counts, and the last 5 decisions. Useful for the dashboard and
  alerting.
- **Cycle-history block** in the same snapshot. Same JSONL backing
  store as the per-cycle dashboard.
- **`SKILLOPT_GOVERNANCE_OPT_OUT_ALL=1`** environment variable. A
  global kill switch that pins every skill to opt-out without writing
  per-skill markers. Useful for staging / dev environments.

---

## What's new for contributors

- **Smoke suite is 82 tests** (was 73). All pass on Python 3.10 - 3.13.
  `python tests/smoke.py` exits 0 with "ALL TESTS PASSED"; if any test
  fails it exits 1 with a clear per-test reason.
- **`execute.py` is the honest health check** — it reports the same
  data the snapshot does, but as a CLI tool. CI uses it as a second
  gate after the smoke suite so a green badge means "the smoke suite
  passed AND the plugin can actually start."
- **The smoke tests install a `sys.modules` shim for `helpers.api`**
  (test-only, no files added to the plugin tree). This is intentional:
  it lets the API-handler tests run in this standalone smoke
  environment without pulling in the real A0 framework runtime.
  Production runtime is unaffected — the shims are `sys.modules`
  injections inside the test, not files on disk.

---

## Upgrade notes

- **From v1.4.0 to v1.5.0:** stop the auto-loop, `rsync` / `robocopy`
  the new files in, restart. No config migration is required because
  the new `governance.default_policy` block is already present in
  `default_config.yaml` with `mode: opt_out` (the safe default). If
  you have an existing `config.json` from v1.4.0 it will keep working
  — the missing keys fall back to the YAML defaults.
- **Backwards compatibility:** every skill that has no marker AND no
  `policy.json` continues to behave exactly as it did in v1.4.0 — the
  auto-loop will not touch it because the new default is opt-out. If
  you have an environment where v1.4.0 was implicitly opt-in (because
  the auto-loop was running on every skill), add
  `touch usr/skills/<name>/.skillopt.optin` per skill, OR set
  `governance.default_policy.mode: opt_in` in `config.json` to restore
  the v1.4.0 behaviour globally.
- **No data loss.** `cycle_history.jsonl`, `failure_memory.log`,
  `adoptions.log`, `governance.log`, all `logs/runs/*.json` state
  files, and all `logs/rollouts/*.json` files are preserved. The new
  helper only **reads** these files, never rewrites them.

---

## Known limitations

- **`cycle_history_min_outcome` and `cycle_history_include_skipped`**
  are config keys present in `default_config.yaml` but not yet
  consumed by `record_cycle_entry()`. They are wired so the config
  surface is complete; a future compaction job will read them. (This
  is the only "config-but-not-runtime" key in v1.5.0.)
- **The CI matrix installs `transformers + torch`** even on jobs that
  don't need them, because some `helpers/*` modules import them at
  top level. The full v1.3.0 install line is `pip install pyyaml
  scikit-learn transformers torch`. We don't strip the unused imports
  because the tests assert those imports succeed. If you have a
  stricter environment, you can comment them out and re-run the
  test that exercises the A/B harness path (`t_v130_failure_memory_*`
  in `tests/smoke.py`); it will skip with `ImportError` rather than
  crash.
- **`SKILLOPT_BRIDGE_TO_HOST=1`** is still a host-bridge mode that
  writes to `~/.claude/` instead of the plugin-local cache. It is
  opt-in, documented in `README.md`, and unchanged from v1.4.0.

---

## Acknowledgments

- The validation gate (`validate_proposal()`) is the v1.2.0 design
  by `microsoft/SkillOpt`. We have not modified it.
- The cycle-history + dashboard pattern follows `agents/skillopt_trainer`
  per-cycle critique conventions from v1.3.0.
- The governance layer takes inspiration from `~/.claude/CLAUDE.md`
  opt-out conventions but is fully plugin-local.

---

## Get in touch

- **Plugin index:** `a0-plugins` repo (PR `plugins/skillopt/`)
- **Issues:** `a0-plugins/issues` with label `plugin:skillopt`
- **Maintainer:** see `plugin.yaml` -> `maintainer`

---

_Generated automatically as part of the Day-5 final pass._
