# SkillOpt Plugin — Roadmap (post v1.1.0)

This document is the engineering roadmap for the SkillOpt self-evolution plugin. It captures what v1.1.0 ships, the architectural work still needed to make the loop robust, and the order in which to do it.

If you are a new contributor: start with the **Day 3** items. They are the highest-leverage missing pieces, all scoped to a few hundred lines of code each.

---

## Day 3 — DONE (v1.2.0, 2026-07-23)

All three Day-3 items shipped in v1.2.0. Total: **~2,100 LoC across 5 new files** (`helpers/fragment_store.py` 575 LoC, `helpers/reward_model.py` 366 LoC, `helpers/ab_harness.py` 593 LoC, `scripts/train_reward_model.py` 215 LoC, `scripts/calibrate_judge.py` 327 LoC) **plus targeted modifications** to the harvester (`extensions/python/monologue_end/_60_skillopt_harvest_rollout.py`), the validation gate (`helpers/sleep_runner.py`), and the auto-loop (`helpers/auto_loop.py`).

- **41/41 smoke tests green** (12 v1.1.0 regressions + 29 v1.2.0 cases covering the fragment store, the reward model, the A/B harness HTTP judge, the auto-loop wiring, and the calibrate_judge pipeline).
- **execute.py Health check PASSED** on the live install (`EXPECTED_VERSION=1.2.0`, manifest version 1.2.0).
- **Validation gate preserved** — `validate_proposal()` is the single source of truth; the new A/B harness stage (0) and per-fragment stage (0.5) are strictly additive. The `t_v121_gate_backward_compat` smoke test proves that callers without `skill_name` see the v1.1.0 gate exactly.
- **Failure modes are loud** — every fallback path is named (`heuristic_fallback` vs `heuristic_fallback_error` vs `heuristic_no_input`; `judge_unreachable` vs `ab_harness disabled by config`; `fragments` with `yaml_available`) and counted on the dashboard.

The loop now closes with statistical confidence: a proposal must beat the current skill on a paired A/B test of the agent's own rollouts (judge's win-rate by ≥ `ab_harness_min_lift_pp` with confidence ≥ `ab_harness_min_confidence`) — not on a synthetic held-out probe.


---

## Day 4 — DONE (v1.3.0, 2026-07-26)

All three Day-4 items shipped in v1.3.0. Total: **~1,850 LoC across 3 new files** (`helpers/inner_loop.py` ~650 LoC, `helpers/cadence.py` ~248 LoC, `helpers/budget.py` ~255 LoC, `helpers/failure_memory.py` ~700 LoC) **plus targeted modifications** to the auto-loop (`helpers/auto_loop.py`), the harvester (`extensions/python/monologue_end/_60_skillopt_harvest_rollout.py`), the validation gate (`helpers/sleep_runner.py`), and the status surface.

- **63/63 smoke tests green** (12 v1.1.0 regressions + 29 v1.2.0 cases + 22 v1.3.0 cases covering the inner loop, per-skill cadence + budget, and failure memory).
- **execute.py Health check PASSED** on the live install (`EXPECTED_VERSION=1.3.0`, manifest version 1.3.0).
- **Validation gate preserved** — `validate_proposal()` is unchanged across all 3 items. The 40 pre-existing tests still green.
- **Two-loop = clear contracts** — inner loop only writes to `logs/runs/suggestions/` and `inner_loop.log`; outer loop only reads from `suggestions/`. Failure memory only writes via `record_failure()`; outer loop only reads via `build_failure_context()`. Neither loop touches `SKILL.md`, `staging/`, or `logs/runs/adoptions.log` — that's the gate's job.

The loop is now a proper two-loop architecture: cheap per-rollout critique-and-suggest in the background, expensive per-skill targeted rewrites when there's real signal. Hot skills get more cycles, cold skills get fewer, and every skill has a hard daily budget cap.

---

## Day 5 — DONE (v1.4.0, 2026-07-28)

Item 7 (Per-cycle dashboard) shipped in v1.4.0. Total: **~480 LoC across 3 new files** (`helpers/cycle_history.py` 252 LoC, `api/cycles.py` 77 LoC, `api/audit_log.py` 103 LoC, plus ~50 LoC in `webui/skillopt-dashboard.js` extension) **plus targeted modifications** to the auto-loop (`helpers/auto_loop.py`, +31 LoC at the end of `_auto_adopt()`), the status surface (`helpers/sleep_runner.py:get_status_snapshot()` +`cycle_history` block), the version manifest (`plugin.yaml`, `execute.py`), and the config surface (`default_config.yaml`, `config.json` with the 4 `cycle_history_*` keys).

- **73/73 smoke tests green** (12 v1.1.0 regressions + 29 v1.2.0 cases + 22 v1.3.0 cases + 10 v1.4.0 cases covering the cycle-history helper, the cycles API, the audit_log API, and the status snapshot integration).
- **execute.py Health check PASSED** on the live install (`EXPECTED_VERSION=1.4.0`, manifest version 1.4.0). No WARN — clean version match.
- **Validation gate preserved** — `validate_proposal()` unchanged. The 63 pre-existing tests still green. The cycle_history write happens AFTER the gate; if the gate rejects, the cycle is still recorded (with `outcome="rejected"`) so the dashboard shows what the loop tried and why it failed.
- **Backwards-compat** — the v1.3.0 `logs/runs/adoptions.log` write inside `_auto_adopt()` is untouched. The cycle_history record sits alongside it as a richer companion; the audit_log API reads the existing adoptions.log file the same way Day-4 already did.
- **Failure modes are loud** — every public function in `cycle_history.py` returns a structured `{ok, error}` on failure instead of raising. The auto-loop's call site wraps it in `try/except` so a malformed entry or a missing directory can never break the cycle.

The closed loop is now fully observable. Every Sleep cycle — whether adopted or rejected, hot skill or cold, full rewrite or minimal targeted edit — gets a JSON record the dashboard can render, with links back to the rollouts the cycle used, the staged proposal the cycle produced, the audit-log line the cycle wrote, and the inner-loop / failure-memory / budget context the cycle had on hand when it made its decision. The gate is still the single source of truth; the dashboard is read-only.

---

## Solution B — DONE (v1.6.0 / v1.6.1, 2026-08-10)

**Bridge to the official Microsoft `skillopt_sleep` pipeline.** The auto-loop now
drives the official research-grade engine instead of the hand-rolled
`direct_optimizer`, which is demoted to a fallback used only when the official
package is absent. v1.6.1 verified the CLI flag mapping and `evaluate_gate`
signature against `microsoft/skillopt` @ HEAD (shallow-clone introspection; the
package is not installed in this dev env). 100/100 smoke tests.

- NEW `helpers/official_adapter.py` — cached `import skillopt_sleep` probe (60s
  TTL) + one official Sleep cycle per skill; fail-soft to direct on infra error;
  authoritative gate-reject (no direct fallback). Gate verdict read from
  `report.json`, not log scraping.
- `validate_proposal` gains `official_gated`; Stage 8 held-out is skipped when
  the official engine already ran its own monotonic held-out gate pre-staging.
- Per-skill `governance` + `cadence` + `budget` gating wired into the tick
  (previously computed-but-unused). `cycle_history` compaction. A/B harness
  demoted to advisory-only.

**Remaining live follow-up:** install `skillopt_sleep` into the A0 venv and run
one real `run` end-to-end with a configured backend to confirm the bridge
against the installed package, not just the source tree.

---

## Solution C (Core) — DONE (v1.7.0, 2026-08-11)

**The self-evolution engine now actually evolves.** Four phases. 122/122 smoke
tests. The headline fix: the v1.1.0 rollout harvester read a nonexistent
`loop_data.messages` and had been writing **zero rollouts** the entire time —
the engine was running blind. Solution C fixes that and adds the trust + control
layers on top.

- **C1 — ground-truth attribution.** Harvester reads `history_output` and
  attributes the active skill via `skills.skill_instruction_name` (per-turn walk,
  last match wins) + `get_loaded_skill_names` fallback. `SKILLOPT_REPLAY_MODE`
  recursion guard.
- **C2 — local counterfactual replay gate.** NEW `helpers/replay_harness.py`:
  deterministic mock executor (relevance heuristic, no LLM) scores current vs
  proposed on held-out rollouts; strict-monotonic lift ≥ `gate_min_improvement_pp`
  over ≥ `replay_min_n`. Wired as **stage 0.7** (skipped when `official_gated`).
  Real executor implemented in v1.8.0 (subprocess-isolated A0 loop); mock stays default.
- **C3 — human-in-the-loop adopt UI.** `/staged` + `/adopt` (by id, with
  whole-file snapshot) + `/reject` + `/rollback`. `fragment_store` whole-file
  snapshot/restore keyed on skill-name string. WebUI Staged-proposals section.
- **C4 — auto opt-in with guardrails.** `governance.auto_optin_new_skill` creates
  `.skillopt.optin` + `policy.json` (`require_human_approval: true`) for new
  skills; `/governance_approve` + `/governance_status` endpoints; WebUI Governance
  section. Immutable/opted-out skills never touched.

---

## v1.8.0 — DONE (2026-08-11): the two offline pieces, integrated

Solution C deliberately stubbed the two expensive/offline pieces and documented
them as follow-ups. **v1.8.0 implements both**, default-off so v1.7.0 behavior
is preserved byte-for-byte unless an operator opts in. 122/122 deterministic
smoke tests still pass (11 new `t_v18_*` cases + the updated `t_c2_*`); live
verification (real A0 runtime + LLM) is flagged below.

### 10. Real A0-agent-loop replay executor — DONE (v1.8.0, P1–P3)

The C2 local replay gate now has a real executor behind
`replay_real_executor_enabled`. `helpers/replay_harness.py:_real_score` shells
out to the new `scripts/replay_worker.py`, which runs each held-out task through
a real Agent Zero monologue (all tools) under the current vs proposed skill in a
**child process** — temp working directory, own event loop,
`SKILLOPT_REPLAY_MODE=1` — and writes a `{score, outcome, ...}` JSON envelope.
The subprocess design gives side-effect containment (temp cwd), a clean async
boundary (no "event loop already running" when called from the auto-loop thread
or an async WebUI handler), and recursion isolation. Skill injection uses the
corrected recipe: `hist_add_tool_result` takes `skill_instructions` as a
**top-level kwarg**, not nested under `additional`. Cost is bounded by
`replay_real_max_tasks` (default 4 — the real executor runs 2×N full monologues
per gate call) and `replay_real_per_task_timeout_s` (default 180). The
`sleep_runner.py` call-site (stage 0.7) now passes `executor="real"` when the
flag is on; off = byte-identical to v1.7.0. On any worker failure the gate
returns `real_executor_unavailable:...` and falls through to the structural
gate (loud-not-crash, unchanged).

### 11. DistilBERT reward-model training + LLM judge — DONE (v1.8.0, P4–P6)

- **P4 — LLM-judge labelling pass.** NEW `helpers/llm_judge.py`:
  `judge_outcome(rollout)` classifies an agent turn into success/partial/failure
  (aligned with `score_rollout`'s 3-class space) via `direct_optimizer._call_llm`
  with a judge-specific system prompt; never raises. NEW
  `scripts/label_rollouts.py` is the CLI pass (idempotent, atomic in-place
  augmentation, `--limit/--force/--model/--dry-run`, advisory judge-vs-heuristic
  agreement %).
- **P5 — real training loop.** `scripts/train_reward_model.py --mode train`
  loads judge-labelled rollouts, featurizes them with
  `reward_model._rollout_to_text` (the SAME featurizer inference uses), splits
  train/val deterministically (stratified per-class), runs an AdamW epochs loop
  with per-epoch val accuracy+loss, persists the model, writes a `1.3.0-train-*`
  version stamp. `--mode smoke` (default) is the v1.2.0 demo step, unchanged.
- **P6 — calibration + dead-config wiring.** `reward_model.model_path()` now
  reads the `reward_model_path` config key (env still wins);
  `score_rollout(prefer_model_above=None)` resolves `calibration.json` >
  `reward_model_prefer_above` config > 0.6 via `_config_prefer_above()`. The
  `_calibrate()` pass (called from `cmd_train`) sweeps T in [0.3, 0.9] under the
  gated decision rule and writes `calibration.json` next to the model; `T*` is
  surfaced in the version stamp.

**Remaining live follow-up:** the four live checks (L1 worker spawn, L2 judge
labelling, L3 training, L4 end-to-end gate) need the A0 venv + a running A0
server + LLM credentials — they are flagged in the plan, not run in dev. The
smoke suite covers all logic testable without them.

---

## v1.8.12 — DONE (2026-09-16): every model knob follows the active A0 chat model

`optimizer_model` / `target_model` / `judge_model` were pinned to `minimax-m3`
as an implicit default, so switching the model in the Agent Zero UI never
reached the plugin — operators had to edit three config keys by hand.
**v1.8.12 introduces the `chat` sentinel**: all three knobs now default to
`chat`, which resolves at call time to the active Agent Zero chat model via
the framework `_model_config` preset (live check: `glm-5.3-flash` @
`ollama_cloud`), including its provider `api_base` and `api_key`.
`minimax-m3` is retired as an implicit default but still honored when set
explicitly. 150/150 smoke tests pass (4 new `t_v1812_*` cases); the health
check passes with all four version surfaces aligned.

### The resolver — helpers/chat_model.py (207 LoC)

- `get_chat_connection()` returns the full connection `{ok, provider, model,
  api_base, api_key}`: provider + model come from the `_model_config` plugin
  (config.json `model_preset` → presets.yaml chat slot); `api_base` from the
  preset override, else `conf/model_providers.yaml` — found by a recursive
  provider search so layout drift across framework versions cannot break it;
  api_key from `API_KEY_<PROVIDER>` env/dotenv.
- 60 s TTL cache (`clear_cache()`, `force=`); `SKILLOPT_A0_ROOT` override
  makes every test hermetic; read paths never raise.
- `effective_model(model)`: concrete names pass through untouched
  (`conn=None`, legacy behaviour preserved); the sentinel (or empty) resolves,
  with an empty result meaning no active chat model.

### Wiring — five consumption points

| Call site | Sentinel behavior |
|---|---|
| `direct_optimizer._call_llm` | resolved + connection overrides api_base/api_key; unresolved → loud RuntimeError |
| `llm_judge._judge_model` | recorded judge_model is always the concrete model used |
| `ab_harness` HTTP judge binding | resolved at bind time; unresolved fails at bind |
| `inner_loop` suggestion path | resolved per tick; unresolved → existing stub path |
| `official_adapter --model` | concrete name for the external CLI |

### Verification

- `tests/smoke.py`: 150/150 (new cases: sentinel resolution + TTL cache,
  concrete passthrough, unresolved-sentinel loud failure, judge-chain
  resolution).
- `execute.py health`: PASSED; version aligned in plugin.yaml / plugin.py /
  hooks.py / execute.py — the v1.8.7 alignment test caught the missed
  hooks.py bump before commit.
- Live resolution verified against the real framework config; no
  `.skillopt-env` model pins bypass the sentinel; the merged-config reality
  check reads all three knobs as `chat`.
- Process catches en route: the compile gate flagged an indentation fault in
  the official_adapter insert before anything shipped.

16 files changed, +499/−31 (commit 3d1d723, tag v1.8.12).


## Where we are (v1.1.0)

**The closed loop runs end-to-end.** A0 chats → rollouts → background loop → Sleep engine (or direct LLM) → staged proposal → strict validation gate → adoption or rejection with a one-line reason.

The 11 v1.0 bugs are fixed (see `CHANGELOG.md`). The plugin passes its own `execute.py` health check. A 26-case smoke test suite passes. The dashboard renders the live state without timing out.

What v1.1.0 does **not** do:

1. The outcome classifier is a string-match (`traceback` → failure). A 2-line heuristic is fine for early adoption; a small trained classifier would be better.
2. The validation gate is structural (byte equality, length, headers, example block) plus an optional held-out delta. It does not measure **task-level** outcomes on held-out rollouts (it trusts the engine's score).
3. The auto-loop runs on a single cadence (every 30 min by default). There is no per-skill cadence, no per-skill budget, no per-skill history.
4. The Sleep engine and the direct optimiser produce proposals of different quality, and we treat them identically downstream. The direct optimiser has no numeric held-out at all.
5. We have no notion of a **fragment** — a small, addressed piece of skill text that can be independently edited, scored, and rolled back. Today, a proposal replaces the whole skill or nothing.

---

## Why this matters

The v1.1.0 gate is good enough to prevent silent regressions. It is not yet good enough to guarantee **improvements**. Two failure modes are still common:

- **False positive:** the LLM produces a proposal that is structurally fine, gets a `+6pp` held-out score from the engine's bundled grader, and the gate accepts it — but the agent actually gets *worse* on the rollouts the grader never showed. The grader is a synthetic probe, not the agent's real workload.
- **False negative:** the LLM produces a genuinely better proposal that loses 2pp on the synthetic probe (because the new phrasing reads weird in isolation) and the gate rejects it. The agent never sees the improvement.

Solving both means doing the A/B harness (Day 3, item 2) and the fragment store (Day 3, item 1).

---

## Day 3 — The data model & the A/B harness

> **Scope:** ~600 lines of code. Two-week effort. Highest leverage.

### 1. Fragment store ✅ DONE in v1.2.0 (2026-07-23)

Today a `SKILL.md` is a single string. A `proposal` is a single new string. There is no notion of "this paragraph was the cause of the +6pp."

The fragment store replaces the monolithic skill with **named, addressed spans**:

```yaml
# usr/skills/caveman/SKILL.md (v2 with frontmatter)
---
fragments:
  - id: "intro"
    selector: "^# .*"           # first heading + first paragraph
  - id: "grunt_levels"
    selector: "## Pick your grunt"
  - id: "install_block"
    selector: "## Install"
---

# Caveman
... existing content ...
```

Each fragment gets its own version history (`fragments/intro.v3.md`), its own held-out score, and its own roll-out attribution (`intro` improved because of rollouts 17, 19, 23).

**Why:** when a proposal regresses, the fragment store can roll back just the offending fragment instead of reverting the whole skill. When a proposal helps, the store can promote the winning fragment to the canonical version. And critically, the LLM prompt becomes *targeted*: "improve `intro` using these 5 failed rollouts," instead of "rewrite the whole skill."

**Implementation:**
- New helper `helpers/fragment_store.py` with `read_fragments(skill_path)`, `write_fragment(skill_path, fid, text)`, `rollback_fragment(skill_path, fid, version)`.
- The `validate_proposal()` gate runs per-fragment instead of per-skill.
- The `bridge` writes per-fragment attribution into each rollout (which fragment the agent used when it failed).
- The harvester tags each rollout with the fragments that were active when the task ran.

**What shipped in v1.2.0:**
- `helpers/fragment_store.py` — `read_fragments()`, `write_fragment()`, `rollback_fragment()`, per-fragment gate results, YAML frontmatter parsing (yaml fallback to regex), status snapshot block. ~575 LoC.
- `extensions/python/monologue_end/_60_skillopt_harvest_rollout.py` — emits `fragments_active` and `fragments_active_text` on every rollout, and the per-fragment `reward.fragments` subblock. The 3 surgically-fixed rollback + per-fragment-gate bugs are covered by the v1.2.0 smoke regressions.
- `helpers/sleep_runner.py` — `validate_proposal()` adds stage 0.5 (per-fragment gate) for callers that pass `skill_name` AND `skill_path`; the v1.1.0 gate is byte-identical for any caller that doesn't.
- `tests/smoke.py` — fragment store cases (read/write/rollback/per-fragment-gate/JSON contract) are in the 29 new v1.2.0 cases.
- Status surface — `get_status_snapshot()` exposes a `fragments` block with `yaml_available`, `version_history_present`, `last_rollback`.

### 2. A/B harness ✅ DONE in v1.2.0 (2026-07-23)

The current gate accepts or rejects a proposal based on the Sleep engine's synthetic held-out score. The A/B harness replaces that with a **paired test on the agent's own rollouts**:

1. Take the last N rollouts for skill X (default N=30).
2. Split into 15 "A" and 15 "B", stratified by outcome.
3. Replay A under the current skill. Replay B under the proposed skill. (A "replay" = the agent sees the rollout's task, runs, and we measure whether the trajectory matches the recorded one.)
4. Score both with an LLM judge: "is the trajectory correct, idiomatic, and useful?"
5. Adopt the proposal if and only if the judge's win-rate on B > A by ≥ 5pp AND the judge's confidence is ≥ 0.6.

**Why:** this is the only gate that measures what we actually care about (does the agent get better at the agent's real tasks) instead of what the engine's synthetic probe rewards.

**Implementation:**
- New helper `helpers/ab_harness.py` with `run_paired_test(skill_name, proposed_text, n=30) -> {wins, losses, ties, confidence, passed}`.
- Wire `validate_proposal()` to call the harness before accepting. If the harness can't run (no rollouts, no LLM), fall back to the structural gate.
- The harness uses the cheap `target_model` (the one A0 actually runs in prod) to keep replay cost down.
- A small calibration script `scripts/calibrate_judge.py` that runs the judge on a labelled set of A/B pairs and prints Cohen's κ vs human judgement.

**What shipped in v1.2.0:**
- `helpers/ab_harness.py` — `run_paired_test(skill_name, proposed_text, n=30) -> {wins, losses, ties, confidence, passed, fallback_reason}`, the pluggable `set_judge_fn()` interface, the keyword-overlap stub, the `_llm_judge_via_http` path (`POST` → JSON → never raise), the cycle-log writer (`logs/runs/ab_harness.log`), and the status snapshot block. ~593 LoC.
- `scripts/calibrate_judge.py` — runs the judge on a labelled A/B pair set, computes Cohen's κ (sklearn when present, pure-Python fallback), prints the confusion matrix, and reports the recommended `ab_harness_min_lift_pp` / `ab_harness_min_confidence` for a target κ. ~327 LoC.
- `helpers/auto_loop.py` — the `auto_loop` now calls `set_judge_fn(_llm_judge_via_http)` when an LLM endpoint is configured (Task A.2 follow-up). The 8-stage gate plus the new A/B stage 0 fire in the same order; the harness's verdict lands first and the structural stages only run on a harness PASS.
- `tests/smoke.py` — 9 A/B harness cases (HTTP judge stub, auto-loop wiring, gate interactions, calibrate_judge κ math) are in the 29 new v1.2.0 cases.
- Status surface — `get_status_snapshot()` exposes an `ab_harness` block with `enabled`, `total_runs`, `total_passed`, `total_rejected`, `total_skipped`, `judge_fallback_active`, `last_error`.
- Default config + `config.json` get `ab_harness_enabled`, `ab_harness_min_lift_pp`, `ab_harness_min_confidence`, `ab_harness_n_rollouts`, `ab_harness_judge_mode`.

### 3. Reward model ✅ DONE in v1.2.0 (2026-07-23)

~~The harvester's `_heuristic_outcome()` is a string-match. It confuses "the agent hit a recoverable exception and recovered" with "the agent hit a recoverable exception and didn't recover." It has no calibration.~~

~~Replace it with a small reward model: a 350M-parameter classifier fine-tuned on ~5K labelled agent rollouts (we collect the labels by asking the LLM judge from the A/B harness). The reward model is called inline by the harvester; it returns a probability vector `[P(success), P(partial), P(failure)]` and a confidence.~~

**What shipped in v1.2.0:**
- `helpers/reward_model.py` — the model, the lazy loader, the heuristic fallback, the status snapshot. ~350 LoC.
- `scripts/train_reward_model.py` — the training skeleton (deps check + model build + one forward+backward step + optional persist). ~200 LoC. Real training is gated on labelled data, which the A/B harness (item 2 below) will produce.
- Harvester integration in `extensions/python/monologue_end/_60_skillopt_harvest_rollout.py` — calls `score_rollout()` after the heuristic, stores both labels under `record["reward"]`, prefers the model when `source == "model"` AND `confidence >= 0.6` (configurable via `reward_model_prefer_above`).
- `tests/smoke.py` — 20-case deterministic suite (12 v1.1.0 regressions + 8 v1.2.0 reward-model cases).
- Status surface — `get_status_snapshot()` exposes a `reward_model` block (`path`, `model_present_on_disk`, `model_loaded`, `model_version`, `fallback_count`, `model_call_count`, `load_error`).
- Default config gets `reward_model_path` and `reward_model_prefer_above`.

**Design choice (intentional, may be revisited):** the v1.2.0 reward model is *not yet trained*. The training script is wired up but the labelled dataset (5K rollouts labelled by the A/B harness LLM judge) does not exist yet because the A/B harness itself is Day-3 item 2. Until then, every call to `score_rollout()` returns a `heuristic_fallback` result that is bit-identical to the v1.1.0 heuristic. The `fallback_count` on the dashboard makes this loud.

**Why:** the reward model feeds the A/B harness (the "did this rollout succeed?" question is the harness's input) and the direct optimiser (today the optimiser weights successes and failures equally; with probabilities, we can weight by confidence).

**Implementation:**
- New helper `helpers/reward_model.py` with `score_rollout(rollout) -> {success, partial, failure, confidence}`.
- The classifier is a fine-tuned DistilBERT (350M params, runs in <50ms on a single CPU core). Trained via the standard HuggingFace `transformers` Trainer; the training script lives in `scripts/train_reward_model.py`.
- The harvester calls `score_rollout()` after the heuristic and stores both. The harness prefers the model score when it disagrees with the heuristic.

---

## Day 4 — Two-loop architecture + per-skill cadence

> **Scope:** ~400 lines. One-week effort.

### 4. Inner loop: per-task critique-and-suggest ✅ DONE in v1.3.0 (2026-07-26)

Today every Sleep cycle is "rewrite the whole skill." That is expensive (one LLM call per cycle) and coarse. The inner loop is **per-rollout**:

1. After every chat, the harvester writes the rollout.
2. A separate background worker picks up the rollout, calls the LLM with a *tiny* prompt: "here is the task, here is the trajectory, here is the failure mode (if any). Suggest a one-sentence improvement to the skill used."
3. The suggestion is appended to a queue (`logs/runs/suggestions/<rollout_id>.md`).
4. When the outer loop fires, it reads all pending suggestions and produces one targeted proposal — not a full rewrite.

**Why:** suggestions accumulate, the LLM does the cheap work often, and the outer loop only does the expensive work when there's a real signal. This is the SkillOpt paper's "gradient" idea applied at the rollout level.

### 5. Outer loop: per-skill cadence + per-skill budget ✅ DONE in v1.3.0 (2026-07-26)

Today the auto-loop runs every 30 min for every skill. Per-skill cadence means: skills that are hot (lots of new rollouts) get more cycles, skills that are cold get fewer. Per-skill budget means: a hard cap on LLM spend per skill per day (default $1).

**Implementation:**
- The `auto_loop` state file becomes per-skill: `logs/runs/auto_loop_state_<skill>.json`.
- The thread runs in a priority queue, ordered by `new_rollouts / cadence_target`.
- A small `BudgetTracker` reads `OLLAMA_API_KEY` usage (or whatever billing signal the LLM provider exposes) and blocks the loop when the daily cap is hit.

### 6. Failure memory ✅ DONE in v1.3.0 (2026-07-26)

The fragment store is the structural part. The **failure memory** is the searchable part: a per-skill index of "the agent failed on this kind of task because of this kind of mistake." Indexed by task embedding + failure-mode tag.

**Why:** the harvester writes rollouts; the A/B harness replays them; the failure memory tells the LLM *what kinds of mistakes are happening* so the next proposal targets them. Without this, the optimiser is just a generic skill-rewriter.

**Implementation:**
- New helper `helpers/failure_memory.py` with `add_failure(skill, task, embedding, failure_mode)`, `query_failures(skill, task_embedding, k=5)`.
- Backed by the same vector store the A0 memory plugin uses (so it inherits the embedding backend, the search threshold, the recall cadence).
- The inner loop (item 4) writes to failure memory; the outer loop reads from it when constructing the prompt.

---

## Day 5 — Observability, governance, and the public release

> **Scope:** ~300 lines + docs. Two-week effort.

### 7. Per-cycle dashboard

Today the dashboard shows the last cycle's verdict. v2 shows the *full history*: every cycle, every proposal, every gate decision, every critic score. Each entry links to the rollouts, the proposal diff, and the audit log.

**Why:** when the loop is autonomous, the user needs to be able to go back and ask "why did it adopt this proposal three weeks ago?" The audit log is already there (`logs/runs/adoptions.log`); it needs a UI.

### 8. Governance: per-skill policies + opt-out

Some skills should never be auto-modified. (E.g. a user-written `SKILL.md` that took them 2 weeks to tune.) The plugin should support a per-skill opt-out: `usr/skills/<name>/.skillopt-opt-out` (a single-line file) means "the loop must skip this skill." A per-skill policy file means "only allow changes to fragment X," "only allow improvements that don't increase verbosity," etc.

**Why:** autonomy without governance is a recipe for surprise. The user should be able to say "I trust you to touch most skills, but leave my hand-tuned ones alone."

### 9. Public release

The plugin should ship on the Agent Zero Plugin Hub. Steps:

1. Audit pass: re-read every file for security (no `eval`, no shell injection via rollouts, no unbounded file writes).
2. The `a0-plugin-router` skill runs a full review.
3. Open a PR against `agent0ai/a0-plugins`. The PR description links to this ROADMAP, the CHANGELOG, and the smoke test report.
4. Pin a release tag.
5. Update the Plugin Hub listing with the new screenshots and the before/after demo GIF.

---

## How to implement best — engineering principles

These are the cross-cutting rules every item above should follow.

### 1. Test with the synthetic harness, ship with the live agent

Every new feature gets two test surfaces: a deterministic synthetic test (no LLM, hand-crafted rollouts) and a live test (one real chat, one real cycle). The synthetic test runs in CI; the live test runs in the smoke harness.

The smoke harness is the one in `usr/workdir/skillopt-plugin/`. Add a new test case per item, keep the existing 26 cases green.

### 2. Preserve the gate

No item above may weaken the validation gate. The gate is the only thing between the user and a silent regression. New features can *add* checks (per-fragment, per-rollout, per-judge) but must not remove existing ones.

### 3. Make the failure mode loud

If the LLM returns empty, the loop must say so in the dashboard. If the judge can't be reached, the loop must say so. If the rollout is malformed, the loop must say so. The pattern is: **log it to the per-cycle critique, surface it on the dashboard, fail closed** (don't adopt when in doubt).

### 4. Plugin-local, not host-global

Everything in this plugin lives under `/a0/usr/plugins/skillopt/`. No writes to `~/.claude/`, `~/.config/`, the user's home dir, or the global A0 state. The bridge to `~/.claude/` is opt-in via `SKILLOPT_BRIDGE_TO_HOST=1`.

### 5. The cycle log is the source of truth

Every cycle produces a log under `logs/runs/sleep-*.log`. The dashboard reads these, not the live state. The state file (`logs/runs/.auto_loop_state.json`) is just a cache. The critique file (`logs/runs/critiques/<skill>_<ts>.md`) is the human-readable summary. The adoption log (`logs/runs/adoptions.log`) is the audit trail.

When in doubt, write a new line to the log.

### 6. Two-loop = clear contracts

When the inner loop and the outer loop land (Day 4), the contract is:
- Inner loop writes only to `logs/runs/suggestions/` and `logs/runs/failures/`.
- Outer loop reads from `logs/runs/suggestions/`, `logs/runs/failures/`, and `logs/rollouts/`.
- Outer loop writes only to `staging/`, `logs/runs/critiques/`, `logs/runs/adoptions.log`, and `usr/skills/<name>/SKILL.md`.
- Neither loop writes to the other's read-paths.

This means the two loops can be developed, tested, and deployed independently.

### 7. The reward model is the bottleneck

Item 3 is the bottleneck. The A/B harness (item 2) depends on it for grading. The failure memory (item 6) depends on it for failure-mode tags. The per-skill cadence (item 5) depends on it for "is this rollout worth re-running?"

If you can only do one item, do the reward model. The rest get easier once it lands.

---

## What to NOT build

These are tempting but actively harmful:

- **A web UI for editing proposals.** The plugin is autonomous; a manual edit UI is a footgun. If the user wants to edit a skill, they edit the SKILL.md directly and the next loop sees it.
- **A "dry-run" toggle that just shows the prompt.** The dry-run mode already exists (the Sleep engine's `dry-run` verb) and it shows the full prompt + the full model output. There is no need for a second dry-run layer on top of it.
- **A "scheduled" mode that runs the loop at fixed wall-clock times.** The per-skill cadence (item 5) replaces this. Fixed-time scheduling is a holdover from cron-era thinking; the right primitive is "when this skill has enough new data."
- **A second LLM judge that's different from the reward model.** The judge and the reward model are the same problem (score a rollout). One component, one prompt, one calibration set.
- **A custom vector store.** Use the one A0's memory plugin already provides. Two vector stores in one agent is operational pain (two indexes to back up, two thresholds to tune, two failure modes).

---

## Open questions

These need answers before items 4-6 can land:

1. **What is the right LLM judge prompt?** The harness needs a prompt that produces a score the agent's users agree with. We have no labelled set yet. Day 3's calibration script (item 2) is the right place to build one.
2. **How do we back up the failure memory?** The vector store is on disk. A daily export to `<plugin>/backups/` is probably the right answer; a remote upload is overkill for v2.
3. **What is the per-skill budget cap?** $1/day? $5? The cap is a per-deployment knob; the default should be conservative and easy to bump.
4. **Who is the LLM judge?** Same as the reward model (a small classifier) or a larger model (GPT-4o, Claude Sonnet)? The trade-off is cost vs accuracy. Day 3's calibration script will tell us.
5. **How do we surface "the loop is stuck on this skill"?** Today the dashboard shows the last verdict. v2 needs a "stuck on <skill> for N days" indicator, with a one-click "skip this skill for a week."

---

## Tracking

This roadmap lives at the plugin root. The CHANGELOG.md tracks what has actually shipped. The smoke test in `usr/workdir/skillopt-plugin/` is the live signal that the loop still works.

Last updated: 2026-09-15 (v1.8.9 zero-rollout fix).

### Status addendum (2026-08-31, post v1.8.0 live verification)

- **L1–L4 live checks: DONE** — L1 worker spawn, L2 judge labelling (65 rollouts, idempotent+atomic), L3 DistilBERT training (val_acc=0.364, 1 epoch, calibrated via calibration.json, model at models/reward_model/), L4 mock gate all PASS on the live A0 runtime with Ollama Cloud endpoint. L4-real monologue requires a reachable A0 server LLM endpoint; harness fails loud (`real_executor_unavailable`), not a code bug.
- **Security audit (item 9, step 1): DONE** — static scan across helpers/, scripts/, api/, tools/, extensions/: no `eval()`/`exec()`, no `pickle`, no `os.system`. PyTorch `.eval()` hits are inference mode; subprocess usage is controlled (Sleep engine launch, cwd=staging, arg-list form); `__import__` uses are limited to `os.getpid()` and test-only module loading.
- **Solution B live follow-up: DONE** — `skillopt_sleep` 0.2.0 confirmed installed in the A0 venv; `probe_official(force=True)` returns `available=true, error=null`. The adapter bridge now runs against the installed package, not just the source tree.
- **Smoke suite**: 133/133 green (all pre-existing v1.2.0/v1.5.0 isolation failures + the v1.6.1 package-presence assumption fixed; replay namespace fix committed as `91e8999`).
- **Still open**: item 8 formal completion (per-skill policy scopes), item 9 public release (steps 2–5: plugin review, hub PR, release tag, hub listing), open questions 2/3/5 (failure-memory backup, budget cap tuning, stuck-skill indicator).

### Status addendum (2026-09-13, item 9 public release)

- **Release tag v1.8.5: PUSHED** — commit 489358f (annotated tag on origin); repo public with MIT LICENSE at root; `plugin.yaml` `name: skillopt` verified by hub CI.
- **Screenshots published** — docs/screenshot-main|settings|config.png on origin/main; main shot cropped right of the sidebar so private chat titles are not exposed; fail-state capture pruned.
- **Plugin Hub PR: OPEN + CI PASS** — agent0ai/a0-plugins#512 (branch `add-skillopt` @ b41bb85), `validate` = success. Root cause of the first failure: branch built on a stale fork main, so the validator's two-dot diff saw multiple modified plugin folders; fixed by rebuilding the branch as one clean commit on fresh upstream main.
- **Hub listing** — pending maintainer review/merge of #512 (auto-updates the generated index on merge).
- **Fork hygiene** — Olszalsik/a0-plugins `main` synced to upstream main (cf2f7c7) via the merge-upstream API for future submissions.

### Status addendum (2026-09-14, item 8 formal completion)

- **Item 8 per-skill policy scopes: DONE** - `.skillopt.policy.json` per
  skill supports `allowed_fragments`, `max_verbosity_delta_ratio`, and
  `forbid_patterns`, enforced by sleep_runner stage 0.75 and returned as
  `policy_scope_*` rejection reasons; auto_loop passes `skill_name` to the
  gate. Verification: smoke 139/139, v1.8.4 security block 3/3,
  policy-scope sanity 4/4, framework-runtime compile OK.
- **Open question 5 (stuck-skill indicator): IMPLEMENTED in v1.8.6** -
  `governance.get_stuck_skills()` (consecutive rejects, no adoption in the
  window) is surfaced in the status snapshot; `.pause_until` now accepts
  ISO dates (the skip mechanism). The one-click dashboard part remains
  UI-side.
- **Still open**: open question 5 one-click UI (dashboard button),
  item 9 hub merge (PR #512 open).

### Status addendum (2026-09-14, open questions 2 + 3 shipped as v1.8.7)

- **Open question 2 (failure-memory backup): DONE** - every successful
  `record_failure` writes a timestamped per-skill snapshot plus an atomic
  `latest.json` mirror under `logs/runs/failure_memory_backups/<skill>/`;
  keep-last-N via `failure_memory_backup_keep` (default 5, env
  `SKILLOPT_FM_BACKUP_KEEP` wins, 0 disables); restore = copy a snapshot
  back over the store. Verified: keep-3-of-5 rotation, latest.json mirror,
  kill switch (smoke + functional check).
- **Open question 3 (per-skill budget cap): DONE (two-tier)** -
  `BudgetTracker` gains `soft_warn_pct` (config `budget.soft_warn_pct`,
  default 80): the soft warning fires when the daily total crosses 80%
  of the cap (`record_spend` returns `soft_warning`, `get_status` exposes
  `soft_threshold_cents` / `soft_triggered`); the hard gate at 100% is
  unchanged; auto_loop logs the soft warning to the cadence log.
  `soft_warn_pct: 0` disables the soft tier entirely.
- **Still open**: open question 5 one-click UI, item 9 hub merge (PR #512 open).

### Status addendum (2026-09-14, open question 5 one-click UI shipped as v1.8.8)

- **Open question 5 (one-click pause/resume): DONE** - governance
  helpers `pause_skill(skill, hours=24)` / `resume_skill(skill)`, the
  name-validated `/governance_pause` endpoint, dashboard JS methods,
  and config.html UI (Pause 24h / Resume buttons per opted-in skill,
  `Paused: N` stat, per-skill paused chip) wired to the `paused` list
  already surfaced by governance_status. Verified: smoke 144/144,
  framework-runtime compile OK, node --check OK.
- **Still open**: item 9 hub merge (PR #512 open); first live gated
  cycle (needs A0 restart to load v1.8.8 and a reachable LLM endpoint).

### Status addendum (2026-09-15, v1.8.9 zero-rollout fix)

- **Zero rollouts since v1.7.0: root cause found + fixed** - two
  independent harvester bugs: (1) `_flatten_content()` never matched
  the real framework content-dict keys (`user_message` /
  `ai_response`), so every live turn flattened to an empty string and
  the hook early-returned; (2) the `SkilloptHarvestRollout` wrapper
  dropped the framework agent instance, so authoritative skill
  attribution could never fire on live dispatches. Both fixed;
  verified offline (10/10 shape cases, full-record E2E dispatch) and
  live (3 rollouts written by the persistent runtime within 30
  minutes, one with correct skill attribution).
- **CSRF-safe dashboard calls folded in** (uncommitted v1.8.8
  follow-up): `call()` fetches `/api/csrf_token`, sends
  `x-csrf-token`, retries once on 403; cache-bust `cb=3`.
- **Smoke**: post-fix installed-repo run 141/144 - the 2 C3 `/adopt`
  failures were environmental (stale real sleep-run log parsed as
  held-out evidence, delta 0.0pp vs required 5.0pp; the clean workdir
  suite passed 134/134) and are fixed by hiding real sleep-run logs
  in those tests; the OQ5 failure was the new `cb=3` assertion
  (updated).
- **Done (v1.8.10)**: first live gated cycle ran end-to-end - claude-home
  bridge wiring fixed the engine harvest (14 sessions -> 14 tasks), the
  direct optimizer produced a real minimax-m3 proposal, and the local
  replay gate rejected it fail-closed (mock-scorer keyword dilution,
  45 -> 81 keywords, 0.4603 -> 0.4541). Atomic rollout writes and the
  chat_shepherd harvest filter shipped in the same release.
- **Still open**: item 9 hub merge (PR #512 open); replay-gate scorer fix CLOSED in v1.8.11 (task-side coverage: covered task tokens / total task tokens, size-invariant in [0,1]; end-to-end multi-keyword gate acceptance pinned by the v1.8.13 follow-up smoke tests); judge endpoint burst throttling CLOSED in v1.8.16 (in-flight limiter + reservation pacing + 429/5xx/transport backoff retries; 6 smoke cases).
  passes converge over sequential re-runs).


### Status addendum (2026-09-17, item 10 hub watchdog shipped)

- **Item 9 (hub merge of PR #512): PENDING MAINTAINER ACTION** -
  PR #512 open with mergeable_state=clean and validate=success on head
  b41bb85 (touches exactly plugins/skillopt/; the earlier a0-bot
  "exactly one plugin folder" failure predates the current head and is
  resolved). No human reviews or maintainer feedback. Release hygiene
  closed the same day: tags v1.8.10/6f08f6e, v1.8.11/d532baf,
  v1.8.12/3d1d723 pushed to origin (v1.8.11/12 had never been tagged
  locally); CI green on 4fad40a (run 35239756114, all 4 Smoke jobs)
  after the hermetic llm_model test pin.
- **Item 10 (PR merge monitoring + post-hub indexing verification):
  IN-PROGRESS (tooling shipped)** - NEW `scripts/check_hub_status.py`
  (229 LoC, pure stdlib, standalone/scheduled utility; zero imports
  from plugin.py/hooks.py/api/tools/extensions - no blocking network
  calls in agent execution paths). Queries PR #512 state; when merged,
  verifies indexing against the canonical generated catalog release
  asset (`generated-index` -> index.json, shape: plugins dict keyed by
  name plus version field; 185 plugins on 2026-09-17, skillopt absent
  pre-merge), with https://www.agent-zero.ai/p/plugins/ probed as
  supplemental evidence only (SPA HTML shell with no inline plugin
  data, so a missing literal there is NOT non-indexing evidence).
  Emits OPEN_PENDING / MERGED_INDEXED / MERGED_UNINDEXED (plus an
  explicit ERROR on query failure - never guesses), prints status JSON
  to stdout, atomically appends to `logs/hub_status.json` (latest plus
  rolling history, keep-last-50). Verified: py_compile OK, --selftest
  5/5 status-mapping cases, live acceptance run exit 0 -> OPEN_PENDING
  (correct pre-merge state). v1.8.13 adds `api/hub_status.py` - REST
  surface GET/POST `/api/plugins/skillopt/hub_status` serving the
  persisted payload: fresh (mtime <= 3600s) served as-is, stale/missing
  triggers exactly one watchdog refresh under a hard 3s timeout with a
  process-wide single-flight guard, refresh failure returns structured
  HTTP 500; watchdog-reported ERROR is served as honest 200 data.
  Suite 155/155 (5 new handler tests).
- **Still open**: item 9 hub merge (PR #512 awaiting maintainer;
  re-run scripts/check_hub_status.py after merge until MERGED_INDEXED -
  the generated-index release regenerates asynchronously after merge);
 replay-gate scorer fix - CLOSED in v1.8.11 (task-side coverage, size-invariant in [0,1]; end-to-end multi-keyword gate acceptance pinned by the v1.8.13 follow-up tests).

### Status addendum (2026-09-18, hub watchdog shipped as v1.8.15; hub check + sleep-runner dry-run verification)

- **v1.8.15 released (commit 1808ddf, tag v1.8.15, CI green)**: new
  background `job_loop` extension
  `extensions/python/job_loop/_80_skillopt_hub_watchdog.py` refreshes
  `logs/hub_status.json` at most every 1800s while PR #512 is pending
  (3s subprocess cap, non-blocking, loop-safe single-flight). Smoke
  157/157.
- **Hub status check (manual run 2026-09-18T21:13:45Z)**:
  `scripts/check_hub_status.py` exit 0, valid JSON on stdout; PR #512
  still OPEN_PENDING (mergeable_state=clean), generated index live with
  185 plugins, `skillopt` not yet indexed (expected pre-merge);
  `logs/hub_status.json` fresh, history now 9 entries.
- **Sleep-runner dry-run verified** (first official
  `python -m skillopt_sleep dry-run --json` pass through the v1.8.10
  bridge wiring; `sleep_runner.py` itself is the shared library, the
  CLI is the package entrypoint): 79 rollouts bridged, 74 sessions
  harvested, 40 tasks mined, replay+gate executed in <1s (mock backend,
  rc=0, JSON stdout). Gate action `reject`, baseline 0.071 = candidate
  0.071, `staging_dir` empty — fail-closed exactly as designed.
- **Non-destructiveness proven by before/after SHA snapshots**:
  `staging/`, `/a0/usr/skills`, `~/.claude`, `~/.codex` and
  `~/.skillopt-sleep` byte-identical after the dry-run; engine
  `state.save()` and staging writes confirmed hard-gated behind
  `if not dry_run:` in cycle.py. Only the in-plugin bridge cache index
  moved.
- **Validation-gate readiness**: read-only `validate_proposal()` probe
  on the real staged proposal (`security-scan-untrusted-plugin.md`,
  6911 chars vs live 3051) executes all gate stages and returns a
  structural REJECT — no triple-backtick example block. Gate machinery
  healthy; the staged proposal needs an example block before it could
  ever pass adoption.
- **Still open**: item 9 hub merge (PR #512 open; v1.8.15 watchdog now
  monitors automatically); staged security-scan proposal remediation
  (example block); first live gated sleep cycle on a real backend
  (mock-only in this container); judge endpoint burst throttling (CLOSED in v1.8.16).

### Status addendum (2026-09-19, structural remediation verified; full gate-pipeline evaluation)

- **Staged security-scan proposal remediation: CLOSED** - balanced ```bash example block inserted (staged file 7747 chars vs live 3051; frontmatter + headings intact). Read-only structural probe returned (True, ok), clearing the previously recorded structural REJECT (no triple-backtick example block). Live skill and framework code untouched (sha-verified before/after).
- **Full gate-pipeline evaluation (read-only)** recorded to logs/sleep_runner.log (unit full_gate_evaluation): structural PASS (ok); example block present and balanced (2 fences); size bounds pass (7747 >= 200 min_chars; growth vs 3051, shrink ceiling not triggered; note: no separate token-bound stage exists in the current gate - min_chars + max_shrink_ratio are the implemented equivalent); policy scope pass (opt_out global default, no scope keys); counterfactual mock executor over 7 held-out rollouts -> hard_current 0.4694, hard_proposed 0.4902, lift 2.08 pp, accepted=False (rejected_insufficient_lift:2.08<5.0); A/B harness -> config-disabled advisory skip (ab_harness_enabled: false), can_run=False, completed without error. Integrated gate verdict: REJECT (replay_gate_rejected: rejected_insufficient_lift:2.08<5.0) - fail-closed mechanics preserved; no adoption performed.
- **Hub PR #512 status (watchdog-refreshed logs/hub_status.json)**: OPEN_PENDING as of probe 2026-09-19T14:30:32Z (mergeable_state=clean, head e0d6d328361e7efd8bc8e20c47fc64803b5d96db, history 18 entries; skillopt not yet indexed - expected pre-merge).
- **Still open**: item 9 hub merge (PR #512 open; v1.8.15 watchdog monitors automatically); first live gated sleep cycle on a real backend (mock-only in this container; mock replay lift currently 2.08 pp vs the 5.0 pp gate threshold).



### Status addendum (2026-09-19 late, v1.8.17: dockerized replay executor repaired + validated end-to-end)

- **Two worker defects fixed in `scripts/replay_worker.py`** (both previously fatal):
  1. Post-purge dockerized seed - the marker is now set after `_setup_syspath()`'s
     `sys.modules` purge; the pre-purge seed bound the orphaned runtime module and
     extension calls routed over the RFC bridge (50081 dial, no in-container listener).
  2. Tools namespace - force-resolve `tools` and append the framework tools dir to
     `__path__` before agent construction, so `get_tool`'s unknown-tool fallback
     (`tools.unknown`) is always importable; bad tool calls now degrade gracefully
     instead of crashing the monologue.
- **Timeout ceiling**: `replay_real_per_task_timeout_s` 180 -> 600 (config + harness
  fallback); validated monologues run 288-323s, so 180s fail-closed every real replay.
- **Direct probe evidence** (SKILLOPT_REPLAY_MODE=1, PYTHONPATH=/a0, hostile
  cwd=plugin root unless noted; scored envelopes, source=model):
  - held[0] security-scan task: score 1.0 / success / 322.9s / rc 0
  - held[1] different harvested fixture: score 1.0 / success / 288.0s / rc 0
  - held[0] from benign cwd /a0: score 1.0 / success / 315.5s / rc 0 (the two earlier
    benign-cwd timeouts at 300s/480s budgets were backend latency variance)
- **Suite**: 163/163 PASS. The premature `replay_real_executor_enabled` default flip
  was reverted (stays opt-in=false) after it flipped the C2 gate test onto the real
  executor path; the C2 test premise (mock default) is intact.
- **Still open**: item 9 hub merge (PR #512 open, watchdog-monitored); first live gated
  sleep cycle on a real backend - now unblocked by the timeout fix (executor opt-in is
  a config decision at that point).


### Status addendum (2026-09-19 23:15 CEST: first live gated sleep cycle executed — fail-closed on backend latency)

- **Official live gated cycle executed end-to-end** (unit `real_executor_live_cycle`,
  record #4 in `logs/sleep_runner.log`; evidence sidecar
  `logs/runs/live_cycle_verdict_20260919T2311.json`):
  - v1.8.17 worker fixes validated in the production gate path: the 18:42
    ImportError did NOT recur; the worker spawned under the exact gate env
    (`SKILLOPT_A0_ROOT`/`PYTHONPATH` unset in both parent and gate env; the
    `.skillopt-env` overlay is a no-op — comment-only lines), ran a real
    monologue actively (~30% CPU, cwd in its own temp dir), and temp files
    were cleaned by the `finally` block.
  - Executor opted in in-memory (`replay_real_executor_enabled=True` in the
    passed runtime config); `default_config.yaml` untouched (empty git diff;
    framework registry unreachable in standalone drivers, so merged_config is
    YAML-only) — the shipped default stays opt-in=false.
  - Mandated bounds kept: `replay_real_per_task_timeout_s=600`,
    `replay_real_max_tasks=4`, 7 held-out rollouts available, gate bar
    `gate_min_improvement_pp=5.0` / `replay_min_n=3`.
  - Verdict: `ok=False`, reason `real_executor_unavailable:RuntimeError:replay
    worker timed out after 600.0s` (elapsed 600.1s) →
    `adoption=gate_not_run_fail_closed`, `exit_code=0`. No adoption performed;
    staged proposal remains staged; structural + mock stages remain the active
    gate path (fail-closed, loud-not-crash, no fake score).
- **Cause of the fail-close: backend latency regression, not a hang**:
  - 21:05 direct probes: single monologues completed 288–323s (active preset
    era `2 Agent`).
  - 22:38 diagnostic (diag-only 900s budget, setsid-detached): same task and
    skill exceeded 900s while the worker stayed active (~13.6% CPU) and trivial
    backend calls remained ~1.3s → slow-completion regime, not stuck.
  - Prime suspect (unverified): framework Active Preset flipped `2 Agent` →
    `3 Agent` between 22:17 and 22:58, changing chat-model chain depth for
    every fresh worker monologue. The plugin-side resolver still reports
    `2 Agent` (record backend block), so the effective flip lives in the
    framework settings/env layer — outside plugin authority to change.
- **Cleanup**: driver + worker processes stopped; /tmp replay artifacts,
  diag drivers and logs removed; no leftover processes. Only logs and
  ROADMAP changed in the repo (logs gitignored).
- **Still open**: item 9 hub merge (PR #512 open, watchdog-monitored); retry
  of the live gated cycle once backend latency normalizes — precondition:
  one direct probe monologue completing < ~300s (and/or the framework back on
  the known-fast `2 Agent` routing); the mandated 600s ceiling stays unchanged.


### Status addendum 2 (2026-09-19 23:40 CEST: latency precondition met; live cycle re-run fail-closed again on first monologue)

- **Latency precondition probe (unit `single_monologue_latency_probe`, record #5)**:
  held[0] (`fc6e928419cd4e8186a891b6f4736c18`) against the staged proposal under a
  360s ceiling -> **score 1.0, elapsed 201.1s** (< 300s pass bar; comparators:
  322.9s at 21:05, >900s at 22:38). Chained setsid driver auto-proceeded to the
  full cycle on pass, per the documented go/no-go.
- **Full live gated cycle re-run (record #6, evidence
  `logs/runs/live_cycle_verdict_20260919T2334.json`)**: fail-closed AGAIN at the
  FIRST monologue - task[0] under the CURRENT skill exceeded the mandated 600s
  ceiling (elapsed 600.1s, `real_executor_unavailable:RuntimeError:replay worker
  timed out after 600.0s`); adoption `gate_not_run_fail_closed`, exit 0; probe
  context embedded in the record; no adoption performed; staged proposal stays
  staged.
- **Cause refinement - preset-flip hypothesis REFUTED**: the 23:21 probe ran
  fast (201.1s) under the now-active `3 Agent` preset, while the `2 Agent` era
  already produced a >900s window at 22:38 under the same staged skill ->
  monologue latency is dominated by **minute-scale backend throughput variance**
  (ollama_cloud), not preset routing and not skill text (skill-length effect
  cannot be fully excluded as a secondary factor: current-skill monologues still
  have no completed-timing datapoint - both live-cycle first monologues hit the
  ceiling).
- **Gate-design implication**: a single fast probe does not guarantee ~8
  consecutive fast monologues; at tonight's variance the mandated 600s ceiling
  fails closed on the first slow monologue (observed twice tonight, 23:11 and
  23:34, both fail-closed with clean records).
- **Refined retry precondition**: two consecutive probe monologues < 300s within
  one ~10 min window (ideally at low-contention hours). Raising
  `replay_real_per_task_timeout_s` beyond 600 would be an operator-level config
  decision, out of plugin scope; default stays 600.
- **Cleanup**: driver exited cleanly, worker child gone, /tmp replay artifacts
  removed, no orphaned processes. Repo diff: ROADMAP only (logs gitignored).
- **Still open**: item 9 hub merge (PR #512 open, watchdog-monitored); live
  gated-cycle retry under the refined two-probe stability precondition.


### Status addendum 3 (2026-09-20 16:40 CEST: dual-probe stability check FAILED - no full cycle)

- **Two-probe stability precondition executed** (records #7/#8
  `single_monologue_latency_probe` probe_index 1/2, stability record #9
  `dual_probe_stability_check`; console evidence
  `logs/runs/dualprobe_console_20260920T1634.txt`):
  - Probe 1: held[0] (`fc6e9284...`) vs staged proposal, 360s ceiling ->
    **timeout 360.1s** (comparator: 201.1s score 1.0 at 23:21 last night).
  - Probe 2: held[1] (`fa875ba6...`) vs staged proposal, 360s ceiling ->
    **timeout 360.1s**.
  - AND-gate: stability_pass=False, decision `no_full_cycle`, exit 0 -
    full cycle NOT launched, fail-closed state preserved (flag never set,
    no adoption attempted, staged proposal stays staged).
- **Interpretation**: fifth consecutive backend-slow window across ~17h
  (22:38 >900s, 23:11 and 23:34 first-monologue timeouts at 600s, 16:21
  dual-probe 2x timeout at 360s). The same fixture completed in 201.1s under
  the same backend/preset 17h ago, so this is **sustained backend contention**
  (ollama_cloud/glm-5.3-flash), confirmed by the chained driver probing both
  fixtures back-to-back with identical timeouts.
- **Observed pattern**: worker processes stay active (high CPU) until the
  ceiling kill - slow-completion regime, never a hang; probe-stage ceilings
  (360s) fail ~40% of monologues that would pass at 600s, but tonight even
  600s was insufficient.
- **Cleanup**: driver exited cleanly after the stability record; no worker
  children; /tmp replay artifacts removed; repo diff ROADMAP-only
  (logs gitignored).
- **Still open**: item 9 hub merge (PR #512 open, watchdog-monitored); live
  gated-cycle retry under the refined two-probe stability precondition -
  tonight's data shows the precondition itself is currently un-meetable
  during backend contention windows; no plugin-side lever exists within the
  mandated 600s ceiling.

### Status addendum 4 (2026-09-21 10:05 CEST: dual-probe stability check
retried per approved unit - FAILED again, no full cycle)

- **Mandate**: superior-approved retry of the two-probe stability
  precondition before any live gated cycle (post `ad0fd8d` CI-green
  state; 360s per-probe ceiling, 300s pass bar, fail-closed AND-gate).
- **Two-probe stability precondition executed** (records #10/#11
  `single_monologue_latency_probe` probe_index 1/2, stability record #12
  `dual_probe_stability_check`; console evidence
  `logs/runs/dual_probe_console_20260921T094641.txt`):
  - Probe 1: held[0] (`fc6e9284...`) vs staged proposal, 360s ceiling ->
    **timeout 360.1s**.
  - Probe 2: held[1] (`fa875ba6...`) vs staged proposal, 360s ceiling ->
    **timeout 360.1s**.
  - AND-gate: stability_pass=False, decision `no_full_cycle`, exit 0 -
    full cycle NOT launched, fail-closed state preserved (flag enabled
    in-memory for the driver process only, `default_config.yaml`
    untouched, no adoption attempted, staged proposal stays staged).
- **Interpretation**: sixth consecutive backend-slow window across ~35h
  (22:38 >900s, 23:11 and 23:34 first-monologue timeouts at 600s, 16:21
  dual-probe 2x timeout at 360s, 09:46 dual-probe 2x timeout at 360s).
  Identical fixtures, backend (`ollama_cloud`/`glm-5.3-flash`, preset
  `2 Agent`), and probe mechanics as the 09-20 attempt produced matching
  outcomes (360.1s both) - the contention regime is unchanged overnight
  and through the morning; last sub-300s monologue remains the 201.1s
  score-1.0 fixture ~35h ago.
- **Observed pattern**: unchanged - workers stay active (high CPU) until
  the ceiling kill (slow-completion regime, never a hang); both fixtures
  independently exceeded 360s, so probe-stage ceilings keep failing
  monologues that would pass at 600s, and even 600s was insufficient in
  both full-cycle attempts.
- **Cleanup**: driver exited 0 after the stability record; no worker
  children; /tmp probe skill/task/out temps auto-removed; pid + progress
  log cleaned (driver retained at `/tmp/skillopt_dual_probe_driver.py`
  per the Sep-19 driver precedent); repo diff ROADMAP-only (logs
  gitignored).
- **Still open**: item 9 hub merge (PR #512 open, watchdog-monitored);
  live gated-cycle retry under the two-probe stability precondition -
  now 3 probe sessions / 6 timeouts with no sub-300s monologue since the
  201.1s comparator; ad-hoc retries are burning probe budget during
  contention, so the next attempt should be scheduled for an off-peak
  window rather than polled.

### Status addendum 5 (2026-09-21 14:20 CEST: single readiness probe -
contention persists, live-cycle retries parked)

- **Mandate**: single-monologue latency probe against the staged proposal
  to test whether `ollama_cloud` contention cleared (<300s) before
  spending a full two-probe stability pass (post `e255f2a` CI-green
  state; 360s ceiling, 300s pass bar, read-only).
- **Probe #13 executed** (record #13 `single_monologue_latency_probe`
  probe_index 1; console evidence
  `logs/runs/single_probe_console_20260921T134345.txt`):
  - held[0] (`fc6e9284...`) vs staged proposal, 360s ceiling ->
    **timeout 360.1s** (13:43:46 -> 13:49:46).
  - Verdict: ok=False, pass bar 300s NOT met; decision
    `contention_persists` -> two-probe stability check NOT signaled.
- **Protocol refinement**: a single sentinel probe (1x360s worst case)
  now precedes the dual-probe pass (2x360s) so contention polling no
  longer burns two-probe budget; the dual pass runs only when the
  sentinel completes under the 300s bar.
- **Interpretation**: seventh probe timeout since the 201.1s comparator
  (~41h ago), across 4 sessions (2x600s first-monologue 09-19, 2x360.1s
  dual-probe 09-20, 2x360.1s dual-probe 09-21 morning, 1x360.1s sentinel
  09-21 13:43). The contention regime is unchanged through the
  afternoon; even the cheap sentinel did not complete.
- **Decision**: live gated-cycle retries remain **parked pending an
  off-peak backend window**; ad-hoc polling is unproductive (the
  sentinel probe itself was the poll and it timed out). Staged proposal
  stays staged; adoption gate unchanged.
- **Integrity**: staging sha16 `4495a3010f0417a1` unchanged
  before/after; driver enabled the real-executor flag in-memory only
  (`default_config.yaml` untouched); exit 0; no orphaned processes;
  probe temp files auto-removed; no commits made during the probe (log
  append only, gitignored).
- **Still open**: item 9 hub merge (PR #512 open, watchdog-monitored);
  two-probe stability pass -> live gated-cycle retry, parked for an
  off-peak window per this addendum.
