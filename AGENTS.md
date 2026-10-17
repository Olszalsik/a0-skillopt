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
| `REMEDIATION_ROADMAP.md` | The 2026-10-04 audit remediation: 5 phases, 28 findings, v1.8.35-1.8.39 |
| `api/` | One module per route group under `/api/plugins/skillopt/*` |
| `PLAN_informed_approval_autonomous_mode.md` | The v1.8.34 design: informed approval + dual mode, with the reasoning behind the one-resolver and default-inert choices |
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
- **Gating:** `sleep_runner.py` (paths, config, subprocess launch, sidecars, `validate_proposal`), `replay_harness.py` (counterfactual replay, mock + real executors), `ab_harness.py` (synthetic A/B, advisory only), `governance.py` (per-skill policy: opt in/out, immutable, pause, scope, approval, **and the `evolution_mode` resolver**), `proposal_view.py` (**v1.8.34**: bounded, display-only `ProposalSummary` for the approval UI — never gates).
- **Pacing and state:** `cadence.py`, `budget.py` (daily cap per skill), `cycle_history.py` (append-only per-cycle record, compacted), `fragment_store.py` (addressed fragments, whole-file snapshots), `failure_memory.py` (failure attribution into the next prompt).
- **Scoring:** `reward_model.py` (trained model or heuristic), `llm_judge.py` (outcome labelling, burst-protected, never raises), `judge_client.py` (judge provider resolution, connectivity probe), `chat_model.py` (resolves the `chat` sentinel to the active A0 chat model).
- **Privacy:** `privacy.py` owns rollout sanitization and common-credential redaction; apply it both before persistence and again at every provider/replay boundary so old records receive the same protection.
- **Plumbing:** `bridge.py` (rollouts as a plugin-local Claude Code history cache), `setup_env.py` (env file as `${VAR}` references, never plaintext secrets).

## Local Contracts

- **One operating mode, one resolver (v1.8.34).** `governance.evolution_mode` (`manual` | `autonomous`, default `manual`) is the **single** operator-facing switch. Top-level `auto_adopt` is a **legacy alias**. Every consumer calls `governance.resolve_evolution_mode(cfg)` — **never `cfg["auto_adopt"]` directly**, because the old `bool(cfg.get("auto_adopt", False))` is `None` for `auto_adopt:` with no value (the drain silently stops) and `True` for the string `"false"` (the drain silently starts). The resolver strictly coerces, falls back to `manual` (the safe direction), and writes a rejected value to `logs/runs/auto_loop.log` naming the value and the valid set — once per distinct value, not once per tick. `logs/runs/.autonomous_pause` is checked **first** and overrides everything, so an operator can freeze autonomy without editing `config.json` (which the WebUI also writes) or restarting.
- **Precedence is decided against the OPERATOR's config, never the merged one (v1.8.34).** `evolution_mode` ships as `manual`, so `merged_config()` **always** contains it — a plain "canonical key wins" rule silently overrides an install whose `config.json` says `auto_adopt: true`, so upgrading would quietly stop unattended writes to live skills. That was measured on this repo's own `config.json`, not hypothesised. `resolve_evolution_mode()` therefore consults `_operator_config()` (the framework registry, or `{}` when unavailable; an explicit `cfg` argument *is* the operator's view) and only then applies the shipped default. Order: kill switch → operator's `evolution_mode` → operator's `auto_adopt` → shipped default. A shipped default must never outrank an explicit operator setting. `t_v1834_upgrade_preserves_operator_choice` pins this; `source` reports `evolution_mode` / `auto_adopt_legacy` / `evolution_mode_default` / `kill_switch` so the UI can show provenance instead of a silent change.
- **`api/mode.py` writes through `api/config.py`, never through `merged_config()`.** `merged_config()` returns an **empty** dict (measured: 0 of 79 keys) whenever the defaults YAML is unreadable, and `save_plugin_config` **replaces** the `governance` section rather than deep-merging it — so persisting the merged blob would, in that condition, write `governance: {evolution_mode: ...}` alone and destroy `default_policy` plus every `autonomous_*` rail. That is the v1.8.2 config-wipe incident reached through a different door. The route builds its payload with `api/config.py`'s vetted `_read_disk()` + `_clean_overrides()`, sends only the two keys it owns, and **refuses** rather than writing when the config cannot be read in full (`t_v1834_mode_route_no_config_wipe`). Its response is built from the config it *just wrote*, not a re-read, so the UI cannot show a stale mode when `clear_plugin_cache()` has not yet taken effect. Use `from helpers import plugins as plugins_helper` — the same form as `api/config.py`; `import helpers.plugins` can fail where the other succeeds.
- **Autonomy overrides exactly one thing.** `auto_adopt=True` bypasses `require_human_approval_pending` and nothing else. These six rails are evaluated **above** that step and must still bite: `.skillopt.optout`, `.skillopt.pause_until`, `mode: immutable`, `mode: opt_in`/`opt_out` without the marker, `min_interval_seconds`, `daily_budget_cents`. `t_autonomous_cannot_bypass_rails` asserts each one with `auto_adopt=True`; keep it that way, and keep it on a fresh skill per case so one seeded decision cannot shadow another assertion. Autonomy is additionally bounded by `governance.autonomous_max_adoptions_per_day` (default 5 — `auto_adopt_max_per_tick` × a 30-min tick is ~240 potential writes/day, so the daily bound is the thing that actually limits it), `autonomous_max_adoptions_per_skill_per_day`, and `autonomous_min_evidence_rollouts` (**default 0, inert**: a rollout *count* is a weak proxy for justification, and `rollout_retention_days` deletes rollout files, so a non-zero default would block every autonomous adoption after a prune). A rail that cannot be evaluated must **defer**, never wave through. A deferred adoption returns `"skipped"` and stays in staging — caps that reset tomorrow belong with the governance skips, not with the rejects. The rollout-count cache behind the last rail is invalidated at the top of every `_tick`: the cache sits on the single long-lived thread instance, so without that it would be frozen at whatever the first drain ever saw.
- **A machine decision is auditable forever.** Every autonomous adoption writes `governance.log` through the existing `mark_human_decision` with `decided_by="autonomous"`, `proposal_id`, and an evidence `note`. Do not add a second ledger: `decided_by` already discriminates human from machine, and one queryable history beats two. `autonomous_audit: false` silences the row, but that is the documented cost of the off-by-default guarantee — the row is what makes the guarantee checkable after the fact. Counters live in `state["autonomous_adoptions"]` inside the **single-writer** `.auto_loop_state.json` (prune to today + yesterday); `get_loop_state()` reads it for the dashboard and must never write it.
- **Precedence is decided against the OPERATOR's config, and production call sites pass NO argument (v1.8.35, audit F1).** `evolution_mode` ships as `manual`, so `merged_config()` **always** contains it. Deciding precedence from a merged blob let the shipped default outrank an install whose `config.json` says `auto_adopt: true`, so upgrading silently stopped unattended writes — measured live, with the drain's own canary firing every tick and `/mode` reporting the opposite answer from the drain. Two halves, both required: `_operator_config()` prefers `_framework_registry_config()` whenever the registry yields anything, **and** all eight production call sites call `resolve_evolution_mode()` with no argument. Fixing only the resolver leaves production broken, which is exactly what shipped in v1.8.34. A test asserts the no-argument form on the source, because a merged blob re-introduces the defect precisely. A caller can therefore no longer force a mode by passing a `cfg` — the registry is the single truth, and tests must drive it. Order: kill switch → operator's `evolution_mode` → operator's `auto_adopt` → shipped default. `source` reports `evolution_mode` / `auto_adopt_legacy` / `evolution_mode_default` / `kill_switch`.
- **A rail that cannot be evaluated DEFERS; it never waves through (v1.8.35 audit F8; re-audit R2 v1.8.42).** `check_skill_eligible` is the authorisation step, so its failure mode must be restrictive. It used to be permissive twice over: the readers called `_runs_dir()` (which ends in a `mkdir`) outside their `try`, and the sole caller logged `"fall through"` and returned **eligible** — skipping optout, immutable, pause, cadence and budget alike, after which autonomy wrote a live skill for a paused one. Now: reads are guarded, a raising check returns `governance_unavailable` with a logged `DEFER` and a `_record_error` entry, absent governance is a broken install rather than a permissive one, and an unreadable `.skillopt.pause_until` returns `pause_marker_unreadable` instead of reading as "not paused". **Both call sites are covered:** `_skill_eligible_for_cycle` (v1.8.35) *and* `_adopt_one` (v1.8.42) — the F8 fix corrected only the first, because `_auto_adopt` calls `_adopt_one` directly and never routes through the sibling, so a test of the sibling kept passing while the drain's real gate failed open. `_adopt_one` returns `"skipped"`, which is a DEFERRAL: the proposal stays in staging and may be eligible next tick. `_record_error` is a **module-level** function; calling it via `self.` raises AttributeError inside the very except block meant to be safe. Cadence and budget keep their own fall-through deliberately — they only throttle work and cannot authorise it. The deferral must stay *visible*: the reason string and the log line are the operator's only signal.
- **The human-approval gate is a HUMAN gate (v1.8.35, audit F9).** `_human_approved` must reject rows written by a machine, or one autonomous adoption satisfies `require_human_approval` and switching back to manual passes with no human ever having clicked Approve. The test is "was this decided by a machine?", implemented as a small **allow-list of machine values** (`MACHINE_DECIDED_BY`) — not an allow-list of human ones. `decided_by` is free-form: the HTTP route passes through whatever the caller sends and a named operator is legitimate, so requiring the literal `"user"` would silently invalidate real approvals. Rows predating the field default to human. This filter must NOT be copied into `_decided_proposal_ids`, which legitimately wants both kinds so a card reports an autonomous adoption as *decided* rather than pending.
- **Budget and evidence state must be readable, not only writable (v1.8.35, audit F7/F5).** A rollover or an invalidation that lives only on the write path is not a rollover: `can_spend`/`get_status` must roll the day over themselves, or hitting the cap deadlocks permanently (the reset lived in `record_spend`, which only runs after a cycle the cap gates). The per-tick rollout-count cache must be an **instance** attribute initialised in `__init__` and cleared by an instance method — a class-level invalidation is silently shadowed by the first instance and never fires again.
- **The single-writer state file is written atomically (v1.8.35, audit F6).** `_save_state` uses write-temp + `os.replace` + `fsync`, like `cadence.py` and `budget.py`. This is not symmetry for its own sake: `get_loop_state()` reads the same file from the HTTP request thread, and a torn read is swallowed by `_load_state`, which returns **defaults** — so a 0-byte window resets the counters *and* zeroes `last_rollout_count_at_cycle`, which fires an immediate unscheduled cycle. Clean the temp file up on any failure so a repeated crash cannot litter `logs/runs/`.
- **A human rejection is a RAIL, not an approval-shaped flag (v1.8.36, audit F2).** Autonomy may bypass "has nobody approved this document?" — that is what autonomy means. It must never bypass "a human explicitly refused this document." So the rejection check lives at `check_skill_eligible` **step 6.5**, deliberately *outside* the `auto_adopt` bypass, and returns `human_rejected`. Before this, a denial only flipped `_human_approved()` to False — which parks the skill in *manual* mode — so autonomy skipped it and re-adopted the exact bytes the reviewer refused.
  - The rail is **staleness-aware** and must stay that way: it binds only the document staged *before* the denial (mtime comparison), so a new proposal for the same skill is judged fresh and a re-approval clears it immediately. Without that it becomes a permanent mute with no way back.
  - The mtime comparison needs `_REJECTION_MTIME_TOLERANCE_S`. The ledger stores microsecond ISO; `st_mtime` is a full-resolution float. Without the tolerance the comparison misses by ~0.5 µs for a write that causally happened first, and the rail is silently inert — which is exactly what happened on the first implementation.
  - A machine row must never arm it, and an unreadable/absent ledger is **restrictive** (it is the path to a live file).
- **Rejection means "leave the pending queue", not "delete" (v1.8.36, audit F2/L3).** `/reject` validates the id against the staged queue, **quarantines** the document (preserved verbatim under `staging/rejected/` with its sidecars), and records the denial bound to that `proposal_id`. An arbitrary id must never become the `skill` on an audit row: a row naming a not-yet-staged stem makes `find_pending_proposal` report the skill's *next* proposal as already decided, and the card silently stops prompting. Both the quarantine and the ledger write are surfaced in the response and shown in the UI — a rejection that only wrote a row is the defect.
- **The three adopt paths are ONE gate (v1.8.36, audit M7/M8; re-audit R1 v1.8.41).** The HTTP route, the agent tool, and the background drain must all run the same chain. All three refuse an opted-out / immutable / paused / rate-limited skill; all three defer while a real-gate replay is in flight (`find_pending_real_gate_sidecar`, which deliberately ignores a *dead* pid); all three consume the proposal on success, carrying every sidecar. The agent tool additionally **fails closed** when governance is unavailable or raises, and passes `skill_name=` to `validate_proposal` — omitting it made the two adopt paths run different gates. **One implementation, not three spellings (v1.8.41, re-audit R1).** The chain lives in `api/adopt.py::_check_governance()` and `tools/skillopt_sleep.py` **delegates to it**; R1 existed only because the tool had a hand-written copy and the route had none, and nothing ever diffed them. `t_v1836_all_adopt_paths_agree` pins the behaviour — and it is verified load-bearing by mutation (disabling the helper makes it fail). Two rules that came out of R1: a test whose assertions do not match its name is worse than no test, and a **refusal must be attributable** — the test stubs `validate_proposal` so the only thing that can refuse is governance, and includes a control case proving the route *does* adopt when governance permits. Reachability must never decide strength.
- **Only `sleep_runner.adopt_write_skill()` may write a live `SKILL.md` (v1.8.36, audit F3).** Snapshot, then write-temp + `os.replace`, and **refuse when the snapshot fails** — a failed snapshot leaves `/rollback` with no target, which is the unrecoverable case. Every path (route, tool, drain) must route through it and must return `ok: false` when it refuses; a refused adopt is still audited so the trail shows an attempt that did not land. `Path.write_text` on a live skill is the v1.8.24 P0 defect and must not reappear on any path.
- **Decision endpoints require an explicit, strictly coerced intent (v1.8.36, audit F10).** `/governance_approve` treats a missing `approved` as an **error**, not a default to guess, and coerces through the same dialect as `api/config.py` — real booleans, 0/1, and unambiguous string spellings. `bool(value)` is wrong in both directions here: `bool("false")` is `True`, which would approve an explicit denial. Validate `skill` before it reaches a path builder (as `api/governance_pause.py` does), and **report** a failed marker write rather than returning `ok: true` over a partial effect.
- **A `proposal_id` that matches nothing is an error, not a hint (v1.8.36, audit M5).** `/adopt` used to fall through to the newest staged file, so a reviewer could approve proposal A and have proposal B written. `api/proposal.py` and `/adopt` must both resolve by **stem equality** against the real queue. "Adopt latest" remains only for the genuinely argument-less call.
- **Exactly ONE `ApiHandler` subclass per `api/*.py` module (v1.8.37, audit F4).** The framework binds `classes[0]` from `load_classes_from_file`, which iterates `reversed(inspect.getmembers(...))` — the **alphabetically last** matching class. Two handler classes in one module means the *first-sorting* one is silently unreachable, with no error anywhere. This bit **twice**: `api/fragments.py` (`Fragments` read lost to `FragmentsRollback` write) and `api/cycles.py` (`Cycle` detail lost to `Cycles` list) — the second found by the new test, not by the audit. Both are now split (`fragments_rollback.py`, `cycle.py`). `t_v1837_api_dispatch_matches_intent` simulates the framework's selection across every `api/*.py`, which nothing previously could: every other test imports handler classes **by name** and so never exercises the dispatcher.
- **A helper's config read must attempt the namespace import FIRST (v1.8.37, audit F11).** In the A0 runtime the plugin is `usr.plugins.skillopt.helpers.*`, so `helpers` is the **framework** package and a bare `from helpers.X import ...` raises on every call. Four sites did that with no namespace attempt — `failure_memory` (×3) and `ab_harness` — each wrapped in a bare `except`, so the failure was invisible and every `failure_memory_*` / `ab_harness_*` key silently resolved to its hardcoded default (`failure_memory_enabled: false` did nothing). Direction does not matter (`setup_env.py` is bare-first with a namespace fallback); what must never exist is a bare import with **no** namespace attempt at all. Asserted across all of `helpers/`.
- **Never import a module that does not exist (v1.8.37, audit F12).** `cycle_history` read its config from `helpers.config_loader`, which is not in this tree, so both `cycle_history_*` keys were dead since they were declared. Read config through `sleep_runner.merged_config()`. When auditing for this, check the **AST** for import statements — a substring search flags the docstring that explains why the module is gone.
- **`BudgetTracker` is constructed only through `budget.tracker_from_config` (v1.8.37, audit F13).** Nine call sites each built one with a different ad-hoc subset of the `budget:` block, which is exactly why `daily_cap_cents` and `reset_hour_utc` were declared but never supplied — the effective cap was always the module default. The factory also honours `budget.enabled` (read by nothing until now) and coerces a malformed value to the default rather than raising. The tracker that **records** spend must be built by the same factory as the one that **refuses** it, or they accumulate against different budgets. `daily_cap_cents: 0` keeps its meaning "no cap".
- **`cadence.enabled` and `cadence.target_rollouts` are live (v1.8.37, audit F13).** `target_rollouts` is the "hot" threshold — how many new rollouts earn the floor interval instead of the ceiling — and it was never passed to `compute_next_run`. `cadence.enabled: false` unwinds the block via the internal `_CadenceDisabled` sentinel so the budget gate still runs, rather than logging a scary line.
- **A config key's reader must be a `cfg`-derived access, not a signature (v1.8.37, audit F13).** `t_v1824_config_keys_have_readers` greps the leaf name anywhere in the sources, so a key appearing only in a helper's own parameter list counts as "read" while no caller supplies it — which is how five inert keys passed. When adding a setting, confirm the value reaches the code that uses it; `t_v1837_reader_test_detects_unwired_keys` pins the distinction. Note also that `0` must never be collapsed by an `or <default>` idiom: it broke `cycle_history_max_entries: 0` ("no compaction") in the very fix that made the key live.
- **A still-running official engine is `deferred`, not `fallback_to_direct` (v1.8.37, audit F14).** On timeout the engine is left detached and may write `staging/<skill>.md` seconds later, so the result must carry `deferred: True` — which the caller uses to skip the competing direct cycle and to not charge cadence/budget. Without the flag, two engines write the same staged file and one "cycle" is billed as one while being two.
- **The approval ledger is durable; `governance.log` is not (v1.8.38, audit M2).** `governance.log` rotates at 5 MB and the previous generation is **deleted**, so before v1.8.38 every approval older than one rotation window silently vanished: `_human_approved` returned False, a `require_human_approval` skill reverted to *pending* with no visible cause, and `decided_by="autonomous"` audit rows were lost — contradicting the "auditable forever" contract. `mark_human_decision` now also appends to `logs/runs/approvals_index.jsonl`, which is never rotated and holds only the latest row per (skill, proposal), so it is bounded by the number of skills and outstanding proposals rather than by history. Readers consult the log, its `.1` generation, **and** the index, newest-wins.
  - Whenever a file lands in `logs/runs/`, add it to `reset_for_tests()` **and** to the pollution guard. The index initially did neither, so test rows accumulated across runs and `t_c4_governance_approve_endpoint`'s "before approval" assertion saw the *previous run's* approval — caught live, not theorised. An index that outlives its test changes eligibility.
- **Ledger timestamps are compared as PARSED EPOCHS (v1.8.38, audit M1).** The two ledgers disagree about format **and** timezone: `governance.log` writes microsecond ISO in UTC, `cycle_history.jsonl` writes `strftime("%Y-%m-%dT%H:%M:%S%z")` — no fraction, **local** time. `_decided_proposal_ids` merged them with a string compare, so `'.' > '+'` and the differing offsets decided the winner rather than the clock, and an automatic adoption could be attributed to a human approval. Use `governance._parse_ts`, which handles both shapes (including the `+0200` offset `fromisoformat` rejects before 3.11). A naive timestamp stays **naive** — `strftime` produced it in local time, so reinterpreting it as UTC would introduce a fresh offset bug.
- **`_last_human_decision` picks the newest by timestamp, not the last line (v1.8.38).** Strictly more correct than "last line wins", because log generations merged after a rotation can be out of line order. A consequence worth knowing: a fixture with a far-future `ts_iso` will outrank every later real decision, so timestamp probes belong on their own skill.
- **`_skill_dir` implements BOTH of `safe_skill_dir`'s containment layers (v1.8.38, audit M3).** Name validation, then a check that the **resolved** path is still inside the skills dir. It previously had only the first, so a symlinked skill directory let governance's marker writes escape the tree while the adopt path refused the same name. Unsafe names also no longer share one `_invalid_/` bucket: each gets a subdirectory keyed by a digest of the raw name, so two bad names cannot share a policy scope, the mapping stays deterministic (the audit trail remains meaningful), and the unsafe string itself never reaches a path.
- **An explicit `0` is a value, not an absence (v1.8.38, audit M4).** `int(cfg.get(key, D) or D)` turns a deliberate `0` back into the default. Use `governance._opt_int(value, default)`, which defaults only on `None` (and on a bool, which is an `int` subclass and would otherwise silently become 1). For the two governance rails `0` means "no limit": `min_interval_seconds: 0` disables the wait, `daily_budget_cents: 0` disables the cap — the latter matching `budget.BudgetTracker`, where `daily_cap_cents <= 0` is documented as unlimited. Never use the `or <default>` idiom for a numeric setting.
- **A config WRITE must invalidate this plugin's own registry memo (v1.8.43, re-audit R3).** `sleep_runner._framework_registry_config()` memoises the framework read for 5 s. That TTL is safe for reads and unsafe for writes: `plugins.clear_plugin_cache()` drops the **framework's** cache, not this plugin's memo, so after a write the resolver kept answering from the memo. Two consequences — `POST /mode` reported the **pre-write** mode (and the UI rendered it immediately), and, worse, after the operator switched **autonomy off** the drain could keep resolving `autonomous` and write a live `SKILL.md`. Every write path (`api/mode.py`, `api/config.py`) must call **both** `clear_plugin_cache()` and `sleep_runner.invalidate_framework_config_cache()`. The remaining window is an external hand-edit, bounded by the same TTL; `.autonomous_pause` is the immediate, cache-independent lever because it is a file-existence check.
- **"Could not read" is not "nothing to record" (v1.8.43, re-audit R4).** The ledger readers are **tri-state**: a module-level `_UNREADABLE` sentinel when a source cannot be evaluated, distinct from `None`/`[]` meaning genuinely absent. `_UNREADABLE` is **restrictive** on every gate; absence is not. The rejection rail previously ended `except Exception: return True  # Undecidable => restrictive` while both helpers swallowed everything and returned `None`, so the branch was **dead code** and an unreadable ledger silently disarmed the human-rejection rail. The tri-state is what makes the restrictive branch reachable — and it is also what stops the opposite bug: a fresh install has no ledger, and if that read as a fault every skill would be permanently blocked and evolution could never start. A generator cannot return a value, so `_iter_governance_rows` reports its fault through the module flag `_LOG_READ_FAILED`. **Both** directions are asserted.
- **Every provider call is BOUNDED, and the SDK must not retry underneath us (v1.8.44, re-audit R5).** `OpenAI(base_url=..., api_key=...)` inherits a **600 s read timeout and 2 internal retries**. That is unacceptable for any call made inside `llm_judge`'s `BoundedSemaphore(1)`: one non-responding endpoint stops every other judge caller, and `judge_client.judge_rollouts` is a synchronous loop called inline by `run_sleep_cycle.py`, so a cycle can hang for the whole backlog. Pass `timeout=` (via `llm_call_timeout_s`, clamped to `(0, 600]` — a non-positive timeout means "block forever" to httpx) **and `max_retries=0`**, because `llm_judge` owns the retry policy and owns it better: pacing-aware, bounded, 429-aware. Nesting the two produced un-paced nested attempts.
- **A judge failure names the model that was tried (v1.8.44, re-audit R6).** `judge_client`'s escalation only runs when the first call produced no label, and it reads `base.get("model")` to decide whom to re-ask with a bigger budget. **Both** of `judge_outcome`'s failure shapes must therefore carry `model`; when they did not, `judge_name` was always `''`, the ladder collapsed to the chat slot (the most expensive model in the preset), and the label's provenance misreported who judged it. And the escalation must go through `llm_judge.judge_call()` — **not** `direct_optimizer._call_llm` — so it inherits the limiter, the `SKILLOPT_JUDGE_THROTTLE_S` pacing and the retries. First attempt and escalation share one guarded path (`_guarded_call`) so they cannot drift.
- **An A/B harness must score each arm's OWN text (v1.8.44, re-audit R7).** `_replay_under_skill` preferred `rollout["fragments_active_text"]` over its `skill_text` **argument**, and `run_ab` passes the two arms as that argument. The harvester sets that field for any skill without a `fragments:` list, and `read_fragments` returns an implicit `_default` fragment whose text is the whole `SKILL.md` for exactly those skills — so both arms got byte-identical input, `lift_pp` was `0.0`, and enabling `ab_harness_enabled` blocked ALL evolution. Arm text first; the fragment snapshot is only the fallback for an arm that has none. A harness that scores both arms identically is not conservative, it is broken.
- **A documented timeout must actually exist (v1.8.44, re-audit R19).** `refine_intent_with_llm` accepted a `timeout_s` that nothing read while calling an HTTP hook synchronously, so a hanging hook hung the **staging write path**. It now runs on a worker thread with a hard deadline; an over-running hook is **abandoned** (a daemon thread cannot be cancelled) and the heuristic stands, with a module-level slot cap so abandoned hooks cannot accumulate. Any knob the code reads belongs in `default_config.yaml` — an undeclared setting is unsettable *and* invisible to the name-grep reader test.
- **Operator tooling must name what it measured (v1.8.44, re-audit R23).** `scripts/calibrate_judge.py` documented "uses the production LLM judge by default" while both no-flag branches returned the deterministic **stub**, then printed `OK: judge is calibrated`. Offline-by-default is fine; invisible is not. A tool that reports a verdict must name the subject, and must send every input its question needs — `human_label` is a comparison *between* two arms, so arm A has to reach the judge.
- **State must agree with itself (v1.8.45, re-audit R8-R12, R18, R20, R21, R22).** Nine findings, one theme. Concretely, the rules:
  - **Every reader of a ledger sees the same history.** `_decided_proposal_ids()` reads the live log, its rotated generations AND the durable index - so `find_pending_proposal` cannot report "pending" for a decision `_human_approved` can see. Any new reader must use the same helpers (`_iter_governance_rows`, `_read_approvals_index`), never a bespoke open of one file. And `_runs_dir()` ends in a `mkdir`, so it is never called on an unguarded path.
  - **Ledger reads are memoised, and a write invalidates.** The scan is cached under a generation counter bumped by every append, so N per-skill lookups cost one scan while a decision made a moment ago is visible at once. **A TTL alone would be wrong**: "approve, then refresh" must not show the pre-decision state.
  - **One budget per skill, built one way.** Only `budget.tracker_from_config` constructs a tracker (asserted across `helpers/`), always WITH `cfg` - otherwise `budget.enabled`, `daily_cap_cents` and `reset_hour_utc` silently differ between the drain and the gate, and an HTTP thread becomes a writer. `default_when_disabled=True` covers status reads when the gate is off.
  - **An audit row records the RESOLVED outcome, never the intent.** `_adopt_one` funnels every exit through one `_audit(outcome)` writer; `passed`/`outcome` come from the outcome and the gate's verdict lives in `gate_passed`. The row template carries **no** `passed` key, because a dead field becomes a live false statement the moment the overwrite is removed. Assert the invariant across **all** rows, not `rows[-1]`.
  - **An argument a tool accepts must be honoured.** `verb=adopt skill=` resolves by stem equality and errors on no match, like `api/adopt.py`. A discarded argument is a silent wrong-target write.
  - **A partial failure is a failure.** `/reject` reports the combined quarantine + ledger outcome; a half-done reject leaves the document queued with no rail armed.
  - **Absence of a response is not a response.** `inner_loop` treats a transport error as a retry, never as "the model had nothing to say" - only a real decline enqueues the no-op that retires a rollout. Both paths share `_bump_skip`, so an endpoint outage costs retries, not training signal.
  - **A failed one-time load stays one-time.** `reward_model._load_attempted` gates the retry AND is set before any failure exit, not on the success path.
- **Redaction is asserted BEHAVIOURALLY, never structurally (v1.8.46, re-audit R13/R14/R16).** Four of the second pass's privacy findings were **silent no-ops**: a sanitizer called and its result discarded, a key sanitized that the payload does not have, a list field skipped by a `isinstance(v, str)` filter, a flag computed and read by nobody. "The function is called" passes for every one of them. Only asserting that a credential is **absent from the bytes that actually get written** catches them - so any new egress or persistence path gets a behavioural case that pushes a credential through and greps the output. When sanitising a payload, handle the shape that **exists** (`_build_rollout_payload` emits `{task, trajectory, ...}`, no `rollouts` key) and add a no-op guard that **refuses** when sanitisation provably changed nothing - a silent no-op is the failure, so it must be loud.
- **Model output is redacted before persistence AND at the live write (v1.8.46, re-audit R16).** `adopt_write_skill` is the last gate; the **staging writers are the first**. Without the staging gate the unredacted bytes live in `staging/<skill>.md`, `staging/adopted/*.md`, and the summary sidecar's diff. Redaction is also `trajectory`-aware: every string field on a step is redacted, and `args`/`result` are **popped** at the shipped defaults - which means the frozen held-out records are structurally lighter than the source rollouts, not merely redacted.
- **A redaction that happened is recorded (v1.8.46, re-audit R15).** `adopt_write_skill` returns `redacted`; every caller must act on it. The drain logs it, records `output_redacted` on the audit row, and writes a `decided_by="redaction-notice"` ledger row; the HTTP route surfaces `redacted` so the operator sees a live skill that is not byte-identical to what was proposed. A flag nobody reads is the same defect as a gate nobody runs.
- **Persistence boundaries refuse rather than degrade (v1.8.46, re-audit R13).** If the privacy helper is unavailable, or sanitisation raises, the real gate is **skipped** - unsanitised rollout records are never frozen to `logs/runs/*.tasks.json` for the worker to re-read into a live agent context. `bridge.py` raises in the same situation; both are correct. A log line is not a substitute for not writing the file.
- **A file that is rewritten wholesale is under a lock (v1.8.46, re-audit R17).** `_compact_approvals_index` replaces the index, and three writers append to it. Append and compaction share `_APPROVALS_INDEX_LOCK`; nothing else may `os.replace` that file. A rewrite that races its own appenders drops rows **silently** - the same failure the index was created to prevent, reintroduced in the file created to fix it.
- **The plugin never destroys a credential it cannot re-create (v1.8.46, re-audit R24).** `_provenance_ref` returns `""` when no visible source matches, and the plaintext is **preserved** and reported as `unresolved`. The old `${KEY}` self-reference resolved to nothing, so a legacy `.skillopt-env` holding the only copy of a working credential was rewritten into a dangling reference and the next LLM call failed. This deliberately narrows an older "no plaintext on disk" assertion: the plugin did not put that plaintext there, and breaking a working secret is worse than flagging it.
- **One reader per ledger, and no bespoke ledger reads in a route (v1.8.47, re-audit R25).** Any consumer that opens `governance.log` itself will eventually disagree with the gate - the log rotates, the durable index exists precisely so readers do not have to, and machine rows must be filtered. Delegate to `governance._last_human_decision` / `_decided_proposal_ids`. Two readers of one ledger is how R8 happened.
- **A never-raises function must degrade to something SAFE, not to its input (v1.8.47, re-audit R26).** `sanitize_rollout_records` previously appended the raw record when `sanitize_rollout` raised. A redactor that falls back to the input on failure has not failed safely. The fallback keeps the SHAPE (the held-out gate compares pairs by index and reads fields by name) and drops every free-text VALUE.
- **Rewriting a file wholesale needs a lock, and so does replacing it (v1.8.47, re-audit R17/R28).** Both `_compact_approvals_index` and the bridge index replace a file that other code appends to or reads from. A torn or lost write is silent: the reader degrades to "nothing processed yet" and the system re-does work, duplicating history.
- **A long-running detached subprocess is single-flight and closes its handle (v1.8.47, re-audit R27).** Record the pid, decide liveness with `sleep_runner.is_running` (`os.kill(pid, 0)` is Ctrl+C on Windows), refuse a second run with a readable reason, and close the parent's copy of the log handle after `Popen` duplicates it.
- **The record of a run that did NOT execute is the one that must not be lost (v1.8.47, re-audit R31).** `run_sleep_cycle.py`'s `backend_guard` refusal used to emit while `_LOG_PATH` was still `None`, so `emit()` wrote nothing. Bind the log path before anything that can refuse.
- **A comment that documents a contract is a test (v1.8.47, re-audit R30/R32).** The dashboard described `/reject` as audit-only two versions after it began quarantining, and `_deep_copy`'s docstring claimed a safety property its fallback did not have. Both are asserted now. Where a docstring *quotes* a wrong claim while correcting it, assert the BEHAVIOUR instead - a substring check cannot tell a claim from its refutation.
- **Staging is the only path to a live skill.** `find_staged_proposals()` scans staging top level only, so a consume or quarantine must move the proposal *and* its marker/real-gate/summary sidecars. Governance skips stay in staging (the skill may become eligible later); rejects and adopts do not. Only `sleep_runner.adopt_write_skill()` may write a live `SKILL.md` — snapshot, then atomic replace. The agent-callable adopt tool is as exposed as the background loop.
- **A proposal summary is display-only and never gates (v1.8.34).** `helpers/proposal_view.py` turns a staged proposal plus its gate evidence into one bounded `ProposalSummary` (`skillopt.proposal_summary/1`) persisted as `staging/<skill>.md.summary.json`. Nothing in `validate_proposal` or `check_skill_eligible` reads it — `intent` is advisory text, so a wrong or missing classification cannot change whether a proposal is adoptable. `PROPOSAL_SUMMARY_SUFFIX` is declared in **both** `sleep_runner` and `proposal_view` (it cannot import it: `proposal_view` imports `sleep_runner`, and `_move_staged_proposal` must carry the sidecar); a smoke test asserts the two agree. An orphaned sidecar in staging top level is the documented real-gate deadlock, so the summary sidecar is in the move tuple from the same commit that writes it. `get_summary()` computes on read when the sidecar is absent, which is what backfills every proposal staged before v1.8.34 — there is no migration step. Two correctness rules that are easy to undo by accident:
  - The diff counters exclude the `---`/`+++` file headers. Counting `+++ b/staged` as an addition made a pure deletion report `added_lines=1`, which pushed `classify_intent()` past its remove-only branch and mislabelled a clean condensation as `unknown`.
  - Truncation is explicit (`truncated: true` + a visible marker) and the marker is reserved **out of** `diff_max_chars`, so the returned text never exceeds the cap. A reviewer who thinks they saw the whole diff when they did not is the failure this module exists to prevent.
- **Routes resolve a proposal by stem match, never by path (v1.8.34).** `api/proposal.py` and `api/staged.py` derive a `proposal_id` from the staging **filename stem**. A caller-supplied path is never read, joined, or resolved — the id is compared for equality against real stems. Both routes are plain `ApiHandler` POSTs inheriting the framework's auth + CSRF, with no exemption. `api/config.py` coerces every client value to the type of its default and **reports** uncoercible ones in `rejected` instead of aborting the save; nested sections merge one level deep with per-leaf coercion so a partial `governance` payload cannot clobber its siblings.
- **The settings page has one allowlist, and a control must be in it (v1.8.34).** `webui/config.html` holds `SKILLOPT_EDITABLE_KEYS`, the single source of truth for **both** `save()` and `refresh()`. Before this, `save()` hard-coded 6 keys while the page declared 13, so five rendered controls (all three `privacy_*`, `rollout_retention_days`, and the three `replay_evalkit_*`) were silently discarded on every save — the same class of defect as the v1.8.2 config wipe. `t_v1834_ui_editable_keys_complete` asserts every `x-model="cfg.<key>"` is in the list, and that `auto_adopt` is **not** in it: that key is the legacy alias owned by the Evolution mode card via `/mode`, and a second control for one setting is the defect this work set out to remove.
- **Informed approval must reach BOTH surfaces.** There are two: `webui/config.html` (Alpine, inlines its bindings per card) and `webui/skillopt-dashboard.js` (framework-free, pure `data-*` renderers). v1.8.34 initially rewrote only the first, and the second kept discarding the summary — two approval surfaces, one still blind, which is the problem the feature removes. `renderStagedProposals` must carry `summary` and `pending_approval`, and `renderProposalSummary` / `renderPendingApproval` / `renderEvolutionMode` must stay pure and defensive: every field defaulted, because a reviewer must never see a broken card where the evidence should be, and `intent` must degrade to `unknown` rather than throw. `t_v188_ui_wiring` asserts equivalent capability per surface rather than a shared symbol, since the two implementations are deliberately different.
- **`window.SkillOptDashboard` must actually be assigned (v1.8.34).** The object was declared `const` inside the IIFE and never exposed, so the whole documented dashboard extension point was unreachable — invisible only because no consumer referenced it yet. Any new renderer is dead code until `window.SkillOptDashboard = SkillOptDashboard` runs; keep that assignment and the assertion that checks it. Bump the `cb=` cache-bust in `extensions/webui/page-head/skillopt-head.html` whenever this file changes, or a browser serves the stale script and the UI is silently dead; the assertion is `cb=\d+`, not a pinned literal, precisely so bumps need no test edit.
- **`/staged`'s `pending_approval` carries the summary too.** The Governance card reads that map (fetched once by `/staged` and shared, so both cards render ONE document), not `/governance_status`'s per-skill block. Attaching the summary in only one of the two routes left the card's informed block permanently empty in the live UI. Both routes attach it, and a test asserts they resolve the same text — otherwise a reviewer can approve one card's view of a document while looking at another's.
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
- **Model selection is by SLOT, never by name (v1.8.28).** `optimizer_model` / `target_model` / `judge_model` take a slot sentinel from `chat_model.SLOTS` — `"chat"` (high-reasoning work: proposing rewrites) or `"utility"` (bulk classification: judge labelling) — and resolve at call time to the active `_model_config` preset's model for that slot, including its provider `api_base`/`api_key`. **No concrete model name may be hardcoded** in `helpers/`; `t_v1828_config_has_no_pinned_model` fails the run if one appears. The job → slot mapping lives in `default_config.yaml`, so moving a job to another model tier is a config edit, not a code change — this is what lets a better preset upgrade the plugin with no code change. Two rules follow, and both were real bugs:
  - A sentinel must never reach a provider as a literal model id. The old `model != "chat"` tests in `llm_judge._judge_model` and `direct_optimizer._call_llm` did exactly that: `"utility"` would have been sent as a model name. Gate every sentinel check on `chat_model.is_slot()`, never a string compare.
  - A sentinel **passed in** is a request and must be honoured. `_judge_model` used to discard it and fall back to `optimizer_model`, so asking for `"utility"` silently ran the chat model.
  - `chat` resolution is cached per slot (`get_slot_connection`); `_CACHE` remains the chat-slot view for existing readers. A missing slot fails soft as `{'ok': False, 'error': ...}` naming the slot, and an unknown slot name normalizes to `chat`.
  - **`SKILLOPT_OPTIMIZER_MODEL` / `SKILLOPT_JUDGE_MODEL` are pins, not overrides.** Setting either silently defeats the sentinels — that is how this install came to be calling `minimax-m3` while its active A0 model was `glm-5.3-flash`. They are deliberately unset in `usr/.env` and `logs/runs/.skillopt-env`; do not re-add them without a deliberate reason.
- **`official_backend` passthrough derives from `setup_env.BACKENDS` (v1.8.28).** `official_adapter.PASSTHROUGH_BACKENDS` is computed from that tuple (`BACKENDS` minus `auto` and `mock`) — never re-declared. The adapter used to keep a private literal that disagreed with `BACKENDS` in **both** directions: it listed `mock|codex|copilot|cursor|pi|handoff`, which the plugin never passes, while omitting `openai_compatible|qwen|minimax`, which it does. The failure was silent and self-concealing: the flag was dropped, the engine used its own default (`mock`), and the plugin's own `backend_guard` then refused — so a *correct* config looked like a refusal. `mock` is excluded so it can never reach a real run (the deliberate test path is `--allow-mock-backend` in `run_sleep_cycle.py`, which bypasses this mapping), and `auto` is excluded because omitting the flag already means "let the engine choose". A rejected value is written to `logs/runs/auto_loop.log` naming both the value and the valid set, so the next occurrence is self-diagnosing. When a backend is added to `BACKENDS`, it becomes selectable here with no code change.
- **The plugin owns its engine dependency (`ensure_engine`, v1.8.28).** Only `/a0` is bind-mounted, so `/opt/venv-a0` loses `skillopt` on every rebuild; the pin `ENGINE_REQUIREMENT` therefore lives in `official_adapter`, inside the surviving tree. `_run_engine_for_skill` calls `ensure_engine()` before probing, so a rebuild self-heals instead of silently reverting to `direct_optimizer`. **Do not** add the install to `docker/run/fs/ins/install_A0.sh`: that is a tracked core file serving an ignored plugin, and it would push the dependency into every user's image. `ensure_engine` never raises — it returns `{ok, installed, available, reason, py}`, re-probes with `force=True` after a successful pip (the cached "absent" result would otherwise make success look like failure), and rate-limits retries to `INSTALL_RETRY_AFTER_S` so a down network cannot spawn pip every tick.
- **Judge pacing spaces ACTUAL starts, not planned ones (v1.8.28).** `_throttle_wait()` holds `_throttle_lock` **across** the sleep and stamps the real start via `_monotonic()`. The v1.8.16 code reserved an ideal slot, released the lock, then slept, so the starts that actually hit the provider could land closer together than `SKILLOPT_JUDGE_THROTTLE_S` under OS timer jitter — defeating the 429 protection pacing exists to provide. Serializing the wait is the correct semantic; parallelism still comes from the call duration. Tests must not assert sub-timer-granularity wall-clock spacing: on Windows the timer granularity is ~15.6ms, so `min(gap) >= 15ms` is un-flakeable by construction. Assert serialized pacing on the wall clock, and the exact spacing invariant on the injected `_monotonic` seam (`t_v1818_pacing_deterministic`).
- **Safety.** Do not intentionally store secrets in the plugin tree. The env file holds `${VAR}` references, values live in the project's `usr/.env`, and dry-run output exposes key names and booleans only. Rollout redaction is best-effort, not a guarantee that all secrets or PII are found. Preserve auth and CSRF on every route — there is **no** exception any more. The hub-status route used to be one (read-only public data), but it can spawn `scripts/check_hub_status.py` on a stale payload, so an anonymous caller could trigger process spawns; it is now GET-only with the framework defaults inherited.
- **Rollout privacy.** New rollout task/response/fragment text and any opt-in tool payloads pass through `helpers/privacy.py`; tool arguments and results default to excluded. Direct LLM prompts, the official transcript bridge, and real held-out replay must sanitize again because old rollout files may predate the controls. If the privacy helper is unavailable, skip the harvest/bridge/replay or refuse the provider call; never silently continue with raw inputs. Redaction is best-effort and must not be described as a complete secret/PII filter.
- **Rollout retention.** `rollout_retention_days` defaults to `0` (disabled). When positive, `sleep_runner.enforce_rollout_retention()` removes only expired, top-level `logs/rollouts/*.json` files during an auto-loop tick. It must not touch bridge caches, frozen replay task files, staged proposals, or run history. Cleanup failure is logged and cannot stop the loop; when files are removed, adjust the persisted count baseline so the count trigger does not stall.
- **Before you finish.** Run the suite after *every* edit, not just a compile check: `py_compile` cannot catch a trailing comma that silently wraps a 2-tuple into a 1-tuple, and a broad `except` swallows the resulting unpack error. Keep the version string identical across `plugin.yaml`, `plugin.py`, `hooks.py`, and `execute.py`.

### Third-pass contracts (v1.8.48)

These are the invariants that release established. Each is pinned by a case that
was **verified load-bearing by mutation** (all 25 fixes: reintroducing the defect
individually makes the suite fail).

- **A snapshot failure is a RETURN value, not an exception (v1.8.48).** `fragment_store.snapshot_default` is documented "never raises" — it returns `{ok: False, error}`. Therefore `adopt_write_skill` must test `snapshot.get("ok")`, and must refuse the live write when it is false. An `except` around that call is *structurally incapable* of firing: the guard looked present, cost a full release audit to find, and left a failed snapshot (read-only mount, ENOSPC) overwriting a live `SKILL.md` while reporting `"snapshotted": True` — the unrecoverable case with no `/rollback` target. When adding a snapshot-like call: **check the result, not just the exception.**
- **Every path to a live `SKILL.md` goes through `adopt_write_skill` (v1.8.48).** The rollback (`fragment_store.restore_default_snapshot`) used `Path.write_text` — the v1.8.24 P0 defect on a second path — and discarded the result of its own reversibility snapshot. It now delegates, so adopt and rollback share one snapshot + temp + `os.replace` choke point. Do not add a writer; add a caller.
- **`fragment_store` resolves `sleep_runner` through `_sleep_runner()` (v1.8.48).** The module used three different import shapes, one of which was namespace-only with no fallback: `restore_default_snapshot` — i.e. `/rollback` — raised `No module named 'usr'` outside the A0 runtime and returned `{ok: False}` from its catch-all. A recovery path that silently does nothing is the same defect class as a rail that is inert. This is the F11 shape, now banned module-wide.
- **`fragment_store` config readers use `merged_config()` (v1.8.48).** Reading `default_config()` pinned `fragment_snapshot_dir` and `fragment_max_history_per_id` to their shipped values: declared, documented, editable, inert. F13 class.
- **Snapshot versions order NUMERICALLY (v1.8.48).** `fragment_store._version_of()` is the single ordering helper, used by `_list_snapshots`, `_latest_version`, `restore_default_snapshot` and `list_fragment_history`. It accepts a `Path` **or** the string form of a path field. Lexicographic ordering is the trap `_prune_history` documents 90 lines above: it put `v9` after `v12`, so `write_fragment` overwrote the live `intro.v10.md` snapshot — a real rollback target.
- **`cycle_history` and the bridge index are rewritten under a lock (v1.8.48).** `cycle_history` appends from five writers (the drain thread plus three HTTP handlers) and compacts the same file; without a shared lock a row appended between the compaction's read and its `os.replace` was lost from BOTH files. `bridge_rollouts_to_claude_history` is a read-modify-write over `.bridge_index.json` reachable from the loop thread, `POST /sleep` and the agent tool; without the lock the second `os.replace` dropped the first run's rows, re-bridging rollouts and duplicating transcript rows. v1.8.47 made those writes atomic; a lock is what makes the whole sequence safe. Atomic-replace is not a substitute for mutual exclusion.
- **Every governance decision reader is rotation-aware (v1.8.48).** `rotate_log_if_large` replaces `governance.log` with an EMPTY file, so any reader that opens the live generation alone reports "no decision" for one tick per rotation. `get_governance_status` and `_last_eligible_decision_ts` (the `min_interval_seconds` rail — one of the six autonomy-immune rails) now use `_iter_governance_rows()` / `_log_generations()`. Two readers of one ledger is how R8 happened; the status route must delegate to the shared readers, never re-open the file.
- **A machine row must name itself in `MACHINE_DECIDED_BY` (v1.8.48).** The drain's redaction notice writes `decided_by="redaction-notice"` with `approved=True`. That value is in the allow-list, so such a row can never satisfy `require_human_approval`. An allow-list of machine values is only as good as its contents.
- **The harvest path records output redaction (v1.8.48).** `_harvest_real_gate` carries exactly one redaction record, at a level where it executes. It used to sit behind an unconditional `return` and be duplicated three times — the signature of a botched edit — so the path an unattended install takes most often wrote no ledger row at all. Structural assertions (count the `if _w.get("redacted")` sites; none may be reachable only after a `return`) catch this; a substring count cannot.
- **Informed approval reads the NESTED real-gate verdict (v1.8.48).** The only writer sets `payload['status']='done'` and `payload['verdict']=verdict`, with `accepted` and `n` INSIDE the verdict. Reading them off the top level returned `None` forever, so both approval surfaces rendered an accepted replay identically to a rejected one.
- **An unrecorded pid is IN FLIGHT (v1.8.48).** The spawner writes the pending sidecar before `Popen` with `pid: 0` and fills it in afterwards; `is_running(0)` is False, so for the whole spawn window single-flight reported "no gate in flight" and an adopt could consume the proposal and orphan the worker's verdict. Only a KNOWN-dead pid is exempt — that exemption is deliberate and stays. The pid-recording write must check its result; an unchecked failure leaves `pid: 0` in a live sidecar.
- **The real-gate worker honours `--tasks-file` (v1.8.48).** It is REQUIRED by that module's own TRAP-A contract. The path came only from the sidecar, so an unreadable sidecar fell back to `Path('')` → `frozen tasks file missing: .` — exit 1, `status: failed`, the documented fail-OPEN adopt — with the correct absolute path sitting in argv.
- **A running engine whose marker could not be written is DEFERRED (v1.8.48).** `official_adapter` returns `deferred: True` on that path. Without it, `fallback_to_direct` made the caller start a competing cycle against a running engine AND left the pid unrecorded. Same class as the F14 fix.
- **Every module-level name a module uses is imported (v1.8.48).** `official_adapter` called `logging.getLogger` in a non-fatal redaction handler with no `import logging`; the resulting `NameError` escaped `run_official_sleep_cycle` and skipped the gate marker, the summary and the whole tick. Assert it with an AST check (imports vs. loaded names), which no comment can satisfy.
- **The three policy-scope constraints are evaluated SEPARATELY (v1.8.48).** They shared one `try`, so a single malformed value (`max_verbosity_delta_ratio: "0.1x"`, copied verbatim from the operator's policy file) made `forbid_patterns` and `allowed_fragments` — the strongest operator constraints — never execute. Each constraint has its own `try` and each refusal names the constraint that failed, so the operator can tell which key to fix. A rail that cannot be evaluated must DEFER (refuse), never wave through.
- **`judge_model` is honoured (v1.8.48).** `_judge_model` consults the configured slot (`judge_model`, then `target_model`) before `optimizer_model`. The shipped `judge_model: utility` had been declared since v1.8.28 and read by nothing, so every label ran on the chat slot — the most expensive model in the preset — while the operator's setting did nothing (the R6 complaint applied to every label, not just escalated ones). When the configured slot is unresolvable it falls back to `chat`; a sentinel must never reach a provider as a literal model id.
- **The privacy no-op guard compares the payload it sanitised (v1.8.48).** `inner_loop` compares the SANITISED payload against the raw one. Comparing the raw payload against the whole request body — which always carries `model` and `prompt` — can never be equal, so the "sanitisation ran and changed nothing" check was dead code, i.e. the exact silent no-op it exists to catch.
- **`backend` is wired (v1.8.48).** `setup_env.apply("")` falls back to `setup_env.configured_backend()`, which reads the `backend` setting. It had been declared in YAML and rendered as a `<select>` in the settings page since v1.1 and read by nothing. F13 class.
- **Every caller acts on `adopt_write_skill`'s result (v1.8.48).** The drain records redaction, `api/adopt.py` reports `snapshot.ok` from `_w["snapshotted"]` (it used to hardcode `True`, leaving the UI's "(snapshot FAILED)" branch unreachable), and the agent tool surfaces the redaction note and creates nothing ahead of the governance gate. A flag nobody reads is the same defect as a gate nobody runs.
- **The dashboard does not pre-coerce the approval intent (v1.8.48).** `api/governance_approve.py` exists to accept an explicit denial; `!!approved` in the client re-introduced F10 one layer earlier (`'false'` → `true`). One coercion dialect, owned by the server.
- **An explicit `0` is a value (v1.8.48).** `int(cfg.get(k, 5) or 5)` turned "stop unattended writes" into 5 per tick while the `< 1` clamp below it proved sub-1 values were expected; `int(... or 1)` charged 1 per cycle for a call the same config made free. Use `auto_loop._int_or_default` / `_cost_per_call_cents`. This idiom is banned for numeric settings.
- **`_record_error` writes atomically (v1.8.48).** Two readers sit off the writer thread (the status route and the monologue_start warning) and both degrade to "no error" on a torn read — so precisely when the loop is failing, the banner can read clean.
- **The retention baseline adjustment is persisted where it is logged (v1.8.48).** The nearest `_save_state` was inside the cycle branch, which the pruning itself makes less likely to run, so a stale too-high base pinned `new_rollouts` at 0 until `removed + min_new` fresh rollouts arrived. F6 class.
- **The inner loop is restarted independently of the outer loop (v1.8.48).** `start_inner_loop` sat behind an early return that tested only the OUTER thread, so a dead inner loop was never restarted for the life of the process and the suggestion queue silently stopped filling. It self-guards on its own thread, so calling it unconditionally is safe.
- **`cadence` never touches the auto-loop's state file (v1.8.48).** Its "legacy" migration target was `.auto_loop_state.json` — the same name the auto-loop took over for its live SINGLE-WRITER state — so a skill named `_default` made cadence copy unrelated keys and then UNLINK the file the loop thread owns. The migration is deleted, not repaired; there was nothing left to migrate.
- **The real-gate sidecar name is `<skill>.md.md.realgate.json` (v1.8.48).** `REAL_GATE_SIDECAR_SUFFIX` already contains `.md` and the path helper appends `p.suffix` too, so the doubled `.md` is the canonical name on disk — writer, single-flight glob, `_move_staged_proposal` and every reader agree. **Do not "tidy" this**: renaming orphans every in-flight sidecar, whose absence is the fail-OPEN path. Derive the name with `sleep_runner._real_gate_sidecar_path()` in tests instead of spelling it.
- **`reward_model.reset_model_cache()` exists (v1.8.48).** Two docstrings promised it. The one-time load gate (R22) is deliberate; the missing lever was not — without it, a first scoring call on a fresh install latched `_load_attempted` and a freshly trained model was never loaded for the life of the process.
- **Every test function is registered exactly once (v1.8.48).** A third scripted edit silently consumed eight `@test` decorators: the functions stayed defined, the file compiled, the suite stayed green, and **eight bodies stopped running** — including `drain_suggestions` and `cadence.list_skills_with_state`, which have no other coverage. Assert every `t_*` definition carries a `test(...)` decorator and that no name carries two. Two names were registered twice, so a label could not be traced to its body.
- **A test that survives its own defect's restoration is a finding in its own right (v1.8.48).** Five of the 26 new cases were decorative on the first draft and were rewritten: asserting on a helper the readers did not use; asserting on a function's source where the DOCSTRING quotes the fix; matching a substring the COMMENT also contains; and calling a probe without its required argument. **AST-based assertions for structural claims** (does a call exist? does an import exist? does a `try` block exist?) — comments and docstrings cannot satisfy them. Two more traps from the next pass: "some `try` contains `fsync`" is satisfied by the function's own outer `try`, so **walk to the nearest enclosing handler**; and an assertion guarded by `if some_file.is_file():` silently asserts nothing on a machine where that file is absent — build the fixture in a temp tree instead.

### Regression-pass contracts (v1.8.49)

The fourth pass audited v1.8.48 for what it broke and left behind, with ten
explicit questions about the new code. Seven findings; the two worst are the
reason this section exists.

- **No write path may put an EMPTY document into a live `SKILL.md` (v1.8.49).** `adopt_write_skill` refuses blank content *before* the snapshot. `restore_default_snapshot` also treats a 0-byte `_default.pre_adopt.md` as absent — it is what `snapshot_default` writes for a skill the plugin created from nothing, so a 0-byte file (which IS a file) shadowed every real `_default.vN.md`; one `rollback(skill)` click returned `{"ok": true, "bytes": 0}` and emptied the live skill. **16 such snapshots already existed on this install.** Rollback is a recovery path; a recovery path that destroys the thing it recovers is worse than the fault it repairs.
- **A helper's documented ORDER is part of its contract (v1.8.49).** `_log_generations()` claims "oldest first" while returning `[base, .1]` — base being the NEWEST generation, because rotation moves old content into `.1` and replaces the live file with an empty one. v1.8.48 added `reversed(...)` for a "newest first" scan and therefore read the OLDEST eligible decision for the `min_interval_seconds` rail: an older `last_ts` makes `(now - last_ts)` larger, so the rail waved through the window it exists to close. The fix inverted its own intent **because it trusted a docstring that had always lied.** Two rules: when you fix a reader's data source, verify the *direction* of "newest wins" arithmetically, not by reading the loop; and when a docstring and an implementation disagree, the docstring is a finding in its own right.
- **An in-flight window needs a ceiling (v1.8.49).** `PENDING_PID_GRACE_S` (300 s) bounds the unknown-pid case. "Unrecorded pid means in flight" is right for the spawn window and catastrophic past it: `Popen` returns a pid without validating the target, so a worker that dies instantly never rewrites its sidecar, and nothing else removes it (the orphan reaper needs the proposal gone first; the harvest timeout is autonomous-only). The scan is directory-wide, so in manual mode one such sidecar deferred EVERY adopt for EVERY skill, forever — the F8 "defers forever" shape in a new place. An unknown *age* is not evidence of a running worker: treat it as stale.
- **A scope constraint that cannot be evaluated DEFERS, including when the skill has no named fragments (v1.8.49).** `allowed_fragments` on a whole-file skill is unenforceable (`read_fragments` yields only the implicit `_default`), and the old `if _named:` skipped it silently: `allowed_fragments: ["intro"]` bought nothing. It now refuses with `policy_scope_fragments_unevaluable`, naming the key and the skill. The refusal text tells the operator that removing the key opts out — a rail that can only ever say no gets removed, so say how to opt out.
- **`fsync` is guarded everywhere it appears (v1.8.49).** `_record_error` copied `_save_state`'s atomic shape but not its guarded fsync; an `OSError` propagated to the cleanup branch, which unlinked the temp — so the CURRENT error was discarded and an older one kept rendering.
- **A docstring must not document a deleted migration (v1.8.49).** `cadence`'s module docstring still promised the v1.1.0 migration three lines above the note that removed it. `load_per_skill_state` must not delete or rewrite `.auto_loop_state.json`, which the auto-loop single-writes.

### Fifth-pass contracts (v1.8.50)

The fifth pass found what four had missed: **the fragment API applied nothing**, in
both directions, and its only caller reported a recovery that never happened.

- **A function that claims to write must write (v1.8.50).** `write_fragment` documented that it rewrites `SKILL.md`. It wrote the new text to `<id>.current.md` — a file with **one writer and no reader anywhere in the tree** — left the body untouched, and returned `ok: True`. Because `read_fragments` recomputes text from the body, every reader saw the OLD text: a fragment edit was a silent no-op, and `rollback_fragment` (its only caller) reported a successful recovery that changed nothing. It now splices `_selector_span(...)` into the body, re-emits the frontmatter **verbatim**, and writes through `adopt_write_skill`. The general rule: **if the only way to tell the function worked is a file nobody reads, the function did not work.** An existing case pinned the defect — it asserted `.current.md` exists *and* the body is not rewritten — so a deliberate "minimal implementation" was documented as intent and then protected by a test.
- **A write function must refuse what it cannot do (v1.8.50).** An unmatched selector, an edit identical to what is there, or a path outside the configured skills dir each return `ok: False` naming the reason. A second, unsanctioned writer is the alternative and is not allowed. `rollback_fragment` converts the "already identical" refusal into a successful **no-op**, because there the target state *is* the request.
- **Only the body is ours to rewrite (v1.8.50).** Re-emitting the frontmatter through `yaml.safe_dump` normalised quoting and indentation, so an edit to one section also reformatted the operator's metadata — and a rollback could no longer restore the file, because the difference was introduced by the restore. `_raw_frontmatter_block` reuses the original text verbatim.
- **A per-skill store is keyed per skill (v1.8.50).** `Path(SKILL.md).stem` is `SKILL` for **every** skill, so all per-fragment snapshots shared `fragments/SKILL/`: two skills computed `next_n` from each other's history and overwrote the same rollback target, and `/fragments/rollback` could restore one skill's bytes into another. Key on the name (`skill_path_to_name`), and honour `fragment_snapshot_dir` — the same key `get_fragments_status()` reports, so the status route cannot point at a directory that holds no rollback target.
- **The thread's own guard must cover the thread's own arithmetic (v1.8.50).** `_run` coerced the sleep interval *after* the `try` that keeps the loop alive: `auto_loop_interval_sec:` (valueless YAML → `None`) made `int(None)` raise `TypeError`, escape `_run` entirely and kill the only thread that ticks the plugin. The coercion now sits inside the guard and uses `_int_or_default`. When you add arithmetic to a worker loop, it belongs inside the loop's own `try`.
- **A rewrite of an appended-to file belongs to the same critical section (v1.8.50).** The bridge's redaction pass rewrites `history.jsonl` and the per-session transcripts — the files the append below extends — and it ran *outside* the lock v1.8.48 added. Atomic-replace and mutual exclusion are not alternatives; a rewrite needs both, in one section.
- **One coercion dialect, on the kill switch too (v1.8.50).** `api/mode.py` used `bool(data.get("pause"))` and the dashboard used `!!o.pause` — the F10 bug class, third and fourth copy, on the switch that freezes autonomy (`'false'` paused it). Both now defer to `api/config.py`'s vetted `_coerce_like_default`, which rejects an un-coercible value instead of guessing. Assert it with an **AST** check for `bool(...get("pause"))`: the corrected comment quotes the old expression.
- **Two callers of one gate must agree on failure (v1.8.50).** The agent tool's single-flight probe swallowed its exception and proceeded, while `api/adopt.py`'s identical probe DEFERS — and the permissive one is the path an agent drives automatically. It now defers with a `DEFERRED` message. A handler containing `pass` on a gate path is the smell; assert no `Pass` statement exists in any handler of that module.
- **An outcome must describe the document's final state (v1.8.50).** A post-adopt **rollback** restored the previous bytes, yet the audit row, the cycle-history row and the log line all said `adopted` — so anyone reading the trail to answer "is the live skill the one the gate approved?" was told yes. The outcome is `adopted_rolled_back`, and every consumer tests it with `startswith("adopted")` (an exact `==` then quarantined the proposal). The write and the counters are unchanged: it did happen, and the net effect was reverted.
- **Every window needs a floor as well as a ceiling (v1.8.50).** v1.8.49 bounded the unknown-pid window from above only: a *future* `started_ts` yields a negative age, which satisfies `<= PENDING_PID_GRACE_S` for as long as the skew lasts. The condition is `0.0 <= age <= GRACE`.

### Hermeticity contracts (v1.8.52)

- **A suite must not READ operator state, not merely not write it (v1.8.52).** The
  suite was sandboxed on every path it *writes* (`logs/runs/`, `staging/`,
  `fragments/`) and still read the one file the WebUI also writes: 13 governance
  tests inherited the live install's `default_policy` (`require_human_approval`,
  `mode`) through `governance._default_policy()` and read `skipped` on a live
  install while CI passed — **CI was green only because CI has no `config.json`**,
  i.e. the suite's verdict on a live install was a function of operator intent, not
  of plugin behaviour. The fix is the same hook every other state module carries
  (`governance.default_policy_config_path`, the `inner_loop.state_dir` pattern),
  installed suite-wide in `main()` at a nonexistent tmp path — bit-for-bit CI
  conditions — and restored on the way out. The mirror find: the degrade path of
  the v1.8.34 `/mode` wipe-guard precondition was additionally filled by the real
  `config.json` through `api/config.py`'s `CONFIG_PATH`, fixed per-test the same way
  that test family already controls `CONFIG_PATH` for its write leg. And the
  v1.8.28 injected-clock case leaked a typo'd env key (`THROTHLE_S`) for the rest
  of the run — masked because the assertion only demanded `>=` the interval it
  meant to pin. A typo'd TEST-ONLY constant that nothing reads is invisible in a
  passing suite; assert the knob is actually in force when the knob is the point.

## Open findings register — SIXTH pass 2026-10-17 (closed)

Raised by running the v1.8.51 suite on the live install, then by the CI run #48
post-mortem (flaky wall-clock pacing assert, already fixed in v1.8.28).

| ID | Sev | Finding | Site |
| --- | --- | --- | --- |
| C1 | High | `governance._default_policy()` read `<plugin_root>/config.json` directly → 13 governance tests read the live install's operator `default_policy` (`got 'skipped'` / `require_human_approval_pending`); CI green only because CI lacks the file | `helpers/governance.py`, `tests/smoke.py` |
| C2 | High | the v1.8.34 `/mode` wipe-guard's degraded-read precondition was satisfied by the install's real `config.json` through `api/config.py`'s `CONFIG_PATH`, failing on a live install with the code unchanged | `tests/smoke.py` |
| C3 | Low | `t_v1818_pacing_deterministic` set `SKILLOPT_JUDGE_THROTHLE_S` (typo) — the deterministic case ran against the 1.5 s default instead of its 0.02 s interval and leaked the junk key into `os.environ` | `tests/smoke.py` |

Fixes (v1.8.52, suite 358 → **360**, green both with and without a production
`config.json`): C1 → `governance.default_policy_config_path` seam + suite-wide
sandbox in `main()`; C2 → `CONFIG_PATH` pointed at a nonexistent tmp path for the
degraded leg; C3 → the env key spelled correctly. Verified on a container-local
copy in both directions; the production tree is untouched by a suite run.

## Open findings register — SWEEP follow-up 2026-10-05 (closed)

Raised by the 0-byte `_default.pre_adopt.md` sweep, not by an audit pass. Both were
real, both were mine, and both were invisible while the suite only ever ran in a
scratch tree.

| ID | Sev | Finding | Site |
| --- | --- | --- | --- |
| S1 | High | the suite wrote into PRODUCTION `fragments/` — the last unsandboxed state path — because `fragment_store._plugin_root()` was hardcoded to the plugin dir and `_purge_skill_fragments()` even `rmtree`'d the real `fragments/SKILL/`. Running the suite on a live install created fixture skill directories and the 0-byte artifacts a later sweep had to remove, i.e. **the suite recreated what the sweep deleted** | `helpers/fragment_store.py`, `tests/smoke.py` |
| S2 | High | three cases passed in the scratch tree and **failed in the live install**: stage 0 (A/B harness) ran first and rejected with `ab_harness_rejected`, and the harness reads its own settings, so `merged_config()['ab_harness_enabled'] == False` does not stop it. A case that reaches a stage only when an unrelated earlier stage happens to pass is machine-dependent | `tests/smoke.py` |

Fixes:

- **`fragment_store.set_plugin_root_for_tests(root)`** redirects the snapshot tree, and
  `tests/smoke.py` points it at a temp directory for the whole run, printing the path and
  restoring `None` + removing the tree on the way out. `staging/` and `logs/runs/` were
  already sandboxed; this closed the third path. `_purge_skill_fragments()` now clears the
  sandbox instead of production, and the one case that asserted against
  `PLUGIN_ROOT / "fragments"` resolves the root through the module. **A suite run must be
  a no-op on production state** — verify with a file count before and after, not by
  reading the pollution guard, which only watches `logs/runs/`.
- **Every case that drives `validate_proposal` at a stage later than 0 must pass
  `ab_harness_enabled=False`.** `None` lets stage 0 consult the harness's own config, so
  the harness can reject before the stage under test is ever reached. Anything targeting
  stage 0 itself passes nothing.

## Open findings register — FIFTH audit 2026-10-05 (closed)

Two auditors on the gating/authorisation path and on the data/provider path.
Register closed in v1.8.50 with the deliberate exceptions below.

| ID | Sev | Finding | Site |
| --- | --- | --- | --- |
| F8 | Critical | `write_fragment` documented a live rewrite but wrote a file with no reader; a fragment edit and its only caller (`rollback_fragment`) were silent no-ops that returned `ok: True` | `helpers/fragment_store.py` |
| F7 | High | per-fragment snapshots keyed on `Path(SKILL.md).stem` — `SKILL` for every skill — so skills shared one directory and overwrote each other's rollback targets | `helpers/fragment_store.py` |
| F6 | Medium | that helper hardcoded `"fragments"`, ignoring the `fragment_snapshot_dir` the status route reports | `helpers/fragment_store.py` |
| G3 | High | the tick's sleep interval was coerced outside the thread guard; one valueless YAML key killed the only ticking thread | `helpers/auto_loop.py` |
| G1 | Medium | the bridge's redaction pass rewrote the transcript files outside the lock added in v1.8.48 | `helpers/bridge.py` |
| G4 | Medium | the pre-persist redaction flag was computed in two places and read nowhere | `helpers/direct_optimizer.py`, `helpers/auto_loop.py` |
| G2 | Medium | `bool(data.get("pause"))` and `!!o.pause`: a third and fourth copy of the F10 coercion bug, on the autonomy kill switch | `api/mode.py`, `webui/skillopt-dashboard.js` |
| F4 | Medium | the tool's single-flight probe swallowed its exception and failed OPEN while the route defers on the same fault | `tools/skillopt_sleep.py` |
| F5 | Medium | the drain created `usr/skills/<name>/` before the validation gate (the same line v1.8.48 removed from the tool) | `helpers/auto_loop.py` |
| F10 | Medium | a post-adopt rollback was audited and logged as `adopted` | `helpers/auto_loop.py` |
| G9 | Low | the v1.8.49 grace window was unbounded below; a future `started_ts` parked adoption | `helpers/sleep_runner.py` |

**Verified real, deliberately NOT changed.** Three findings are correct but must not be
"fixed" blind, and the reason is recorded so a later pass does not undo the decision:

- **F1/F2 — stage 0.5 (per-fragment gate) is unreachable, and substitutive.** No production
  caller passes `skill_path=`, and `skill_path_to_skill` maps a file whose stem is not a
  live skill to `None`. Wiring stage 0.5 would turn a documented dead switch into a live
  rail — and it would then run *instead of* the whole-file gate, and ignore a deletion
  of a not-allowed fragment. That is a gate-design change with live consequences and no
  way to test the official path here; it is left for a deliberate design pass.
- **F3 — the three adopt paths pass different `official_gated` values.** The drain reads
  the proposal's provenance marker; the route and tool do not, so an official-gated
  proposal adopted manually is gated twice (more work, not less safety). Making the route
  *skip* stage 0.7 on provenance it has not verified would be less safe; the correct fix
  is to read provenance from the summary sidecar, which is also a design change.

### Areas the fifth pass checked and found clean

`validate_proposal`'s stage order and boundary comparisons; the six autonomy-immune
rails against the `auto_adopt` bypass; `_move_staged_proposal`'s sidecar tuple; the
`submissions/` receipt trail; the judge limiter, pacing and escalation provenance;
`privacy.py`'s patterns against the real payload shapes; `direct_optimizer`'s malformed
response handling; `failure_memory`'s state reads; `reward_model`'s `reset_for_tests`
contract; and every `api/*.py` route's auth/CSRF and one-handler-per-module shape.

## Open findings register — FOURTH audit 2026-10-05 (closed)

A **regression** pass over v1.8.48: one auditor given the full change list plus ten
targeted questions about the new code, asked to find what the release broke or left
behind. Register closed in v1.8.49.

| ID | Sev | Finding | Site |
| --- | --- | --- | --- |
| X1 | Critical | `/rollback` could empty a live skill: `adopt_write_skill` had no emptiness guard and the 0-byte `pre_adopt` shadowed every real generation (16 such files already existed) | `helpers/sleep_runner.py`, `helpers/fragment_store.py` |
| X2/X3 | High | v1.8.48's own fix inverted its intent: `_log_generations()` lies about its order, so `reversed()` took the OLDEST decision — the `min_interval_seconds` rail became MORE permissive, and the dashboard showed stale decisions | `helpers/governance.py` |
| X4 | Medium | the new "unrecorded pid is in flight" rule had no ceiling, so a dead worker's sidecar deferred every adopt for every skill, forever, in manual mode | `helpers/sleep_runner.py` |
| X5 | Medium | `allowed_fragments` was silently skipped on every whole-file skill — the strongest scope constraint was inert | `helpers/sleep_runner.py` |
| X6 | Low | `_record_error`'s fsync was unguarded, so an fsync failure discarded the newest error record | `helpers/auto_loop.py` |
| X7 | Low | `cadence`'s docstring still promised the migration v1.8.48 deleted | `helpers/cadence.py` |

The auditor's own answers are worth keeping: `adopt_write_skill`'s snapshot gate, the
three-`try` structure, `_version_of`'s callers, both new locks (no lock-order inversion,
no re-entry), `_judge_model`'s chat fallback, `apply("")`'s guard, and the inner loop's
restart were all checked clean — **Q1-Q10, eight of ten clean.**

## Open findings register — THIRD audit 2026-10-05 (closed)

A third, independent pass over the whole plugin at v1.8.47: six auditors on
disjoint scopes (`auto_loop`/extensions, governance/budget/cadence, sleep_runner/
official_adapter, replay/AB/proposal_view, privacy/LLM/inner-loop, api/UI/tools/scripts),
every candidate verified against the quoted lines before being recorded. Method note:
read-only, no suite run, no plugin imports — see `Work Guidance`.

**This register is EMPTY.** All 25 findings were fixed in v1.8.48 and their
invariants live in `Local Contracts` above, each pinned by a case verified
load-bearing by mutation. A fixed finding is deleted, not annotated.

| ID | Sev | Finding | Site |
| --- | --- | --- | --- |
| T1 | Critical | the "refuse when the snapshot fails" guard could not fire and its result was discarded; a failed snapshot still overwrote the live skill and claimed `snapshotted: True` | `helpers/sleep_runner.py` |
| T2 | Critical | the rollback wrote a live `SKILL.md` with `Path.write_text` (v1.8.24 P0 on a second path) and discarded its reversibility snapshot's result | `helpers/fragment_store.py` |
| G11 | High | `restore_default_snapshot` imported `sleep_runner` namespace-only with no fallback, so `/rollback` returned `{ok: False}` outside the A0 runtime | `helpers/fragment_store.py` |
| T4 | High | informed approval read `accepted`/`n_pairs` off the sidecar's top level while the writer nests them under `verdict`; accepted and rejected replays rendered identically | `helpers/proposal_view.py` |
| T5 | High | `logging` used in a non-fatal handler but never imported; the `NameError` escaped `run_official_sleep_cycle` | `helpers/official_adapter.py` |
| T9 | High | the three policy-scope constraints shared one `try`, so one malformed value disarmed `forbid_patterns` AND `allowed_fragments` | `helpers/sleep_runner.py` |
| T6 | High | a pending sidecar with an unrecorded pid read as "no gate in flight" for the whole spawn window | `helpers/sleep_runner.py` |
| T5b | High | an active-run-marker failure returned `fallback_to_direct` without `deferred`, racing a running engine | `helpers/official_adapter.py` |
| T8 | High | eight test functions had lost their `@test` decorator (and two names were double-registered), so eight bodies silently stopped running | `tests/smoke.py` |
| T3 | Medium | the harvest path's output-redaction record sat behind a `return` and was duplicated three times | `helpers/auto_loop.py` |
| T7 | Medium | the pid-recording sidecar write ignored its failure while the write three lines earlier checked it | `helpers/auto_loop.py` |
| G4 | Medium | `redaction-notice` was missing from `MACHINE_DECIDED_BY`, so the drain's own machine row armed the human-approval gate | `helpers/governance.py` |
| G1 | Medium | `fragment_store` read `default_config()`, pinning both fragment settings to shipped values | `helpers/fragment_store.py` |
| G2 | Medium | three readers sorted snapshots by name; `v9` sorted after `v12`, so `write_fragment` overwrote a live snapshot | `helpers/fragment_store.py` |
| G3 | Medium | `cycle_history` compaction rewrote a five-writer append target with no lock; a row could be lost from both files | `helpers/cycle_history.py` |
| P3 | Medium | the bridge index was an unlocked read-modify-write reachable from the loop thread, `POST /sleep` and the tool | `helpers/bridge.py` |
| G5/G7 | Medium | two governance readers opened the live log generation alone, so every 5 MB rotation blanked the status and the `min_interval_seconds` rail | `helpers/governance.py` |
| G6 | Low | the stuck-skill loop broke on the first `adopted` entry in a chronological list, keeping the oldest timestamp | `helpers/governance.py` |
| G10 | Medium | cadence's legacy migration targeted the auto-loop's live single-writer state file and unlinked it for a skill named `_default` | `helpers/cadence.py` |
| A2 | Medium | the retention baseline adjustment was logged but written only in the cycle branch it suppresses | `helpers/auto_loop.py` |
| A4 | Medium | the inner loop's start sat behind an early return that tested only the outer thread | `extensions/python/agent_init/_50_skillopt_auto_loop.py` |
| A8/A11 | Medium | `or <default>` collapsed explicit `0` for `auto_adopt_max_per_tick` and `cost_per_call_cents` | `helpers/auto_loop.py` |
| A10 | Low | `_record_error` wrote non-atomically while two readers degrade to "no error" on a torn read | `helpers/auto_loop.py` |
| P1 | Medium | the inner-loop no-op privacy guard compared the raw payload against the whole request body, so it could never fire | `helpers/inner_loop.py` |
| P2 | Medium | `judge_model` was declared and read by nothing; every label ran on the chat slot | `helpers/llm_judge.py` |
| P7 | Low | two docstrings promised `reset_model_cache()`; it did not exist | `helpers/reward_model.py` |
| U3 | Medium | the `backend` settings control persisted a key nothing read | `helpers/setup_env.py`, `webui/config.html` |
| U4 | Low | the agent tool discarded the writer's `redacted` flag and created the skill directory before the gate | `tools/skillopt_sleep.py` |
| U9 | Low | `api/adopt.py` hardcoded `snapshot.ok = True`, so the UI's failure branch was unreachable | `api/adopt.py` |
| U7 | Medium | `!!approved` in the dashboard re-introduced the F10 coercion one layer below the route that exists to fix it | `webui/skillopt-dashboard.js` |
| U5 | Low | `prefer_model_above or 0.6` collapsed a calibrated `0.0` back to the hardcoded threshold | `extensions/python/monologue_end/_60_skillopt_harvest_rollout.py` |
| C4 | Medium | the real-gate worker declared `--tasks-file` required and never read it, so a missing sidecar produced the fail-OPEN `frozen tasks file missing: .` | `scripts/replay_gate_worker.py` |

Two further items were investigated and closed as **not findings**: the `staging/`
pollution observation below (still open, unchanged), and the `_probe`/`ab_harness`
items the fourth and later passes confirm independently.

### Areas the third pass checked and found clean

`adopt_write_skill`'s temp+`os.replace` write itself; the auto-loop's thread guard
and watchdog reaping; every `api/*.py` route's auth/CSRF and one-handler-per-module
shape; `bridge.py`'s redaction boundaries and index atomicity; the replay harness's
strict-monotonic lift rule and `insufficient_n` handling; the judge's burst limiter,
pacing and never-raises contract; `privacy.py`'s credential patterns against the
real payload shapes; `chat_model`'s cache invalidation; and the promotion-path
`improve_part` gate.

## Open findings register — RE-AUDIT 2026-10-04 (second pass)

The first audit's 28 findings were fixed in v1.8.35-1.8.39 and self-audited in
v1.8.40. This is a **second, independent pass** over the whole plugin at v1.8.40, with
two auditors on independent scopes. Every entry below was **verified by direct inspection**
of the quoted lines before being recorded — the number in brackets is how many auditors
found it independently. Method note: read-only, no suite run, no plugin imports.

**This register is now EMPTY.** Every finding from the second pass was fixed
in v1.8.41 - v1.8.47, and the `Local Contracts` above carries each
invariant. Keep it that way: a fixed finding is deleted, not annotated.
The historical first-pass register further down says the same of its 28.

### Critical
_None._

Both Critical findings from this pass were fixed: **R1** in v1.8.41 (the HTTP
adopt route ran no governance gate) and **R2** in v1.8.42 (the drain’s own
pre-write gate failed open). Their invariants now live in `Local Contracts`, and
`t_v1836_all_adopt_paths_agree` plus `t_v1842_all_write_paths_are_gated` assert them
per adopt path — verified load-bearing by mutation, not just present.

### High

_None._

All seven High findings from this pass were fixed: **R1** (v1.8.41, the HTTP adopt route
ran no governance gate), **R2** (v1.8.42, the drain's own pre-write gate failed open),
**R3** (v1.8.43, a mode write was invisible for 5 s), **R4** (v1.8.43, an unreadable
ledger disarmed the rejection rail), **R5** (v1.8.44, an unbounded LLM call held the
judge's single slot), **R6** (v1.8.44, the judge's escalation ladder was dead code) and
**R7** (v1.8.44, the A/B harness scored both arms identically). Their invariants now live
in `Local Contracts`, and each fix is pinned by a test that was **verified load-bearing by
mutation** — reintroducing the defect individually makes the suite fail, which is the only
way to be sure a negative-path test is not decoration.

The common shape worth carrying forward: **every one was a component not doing what the
code around it assumed.** A cache TTL that was safe for reads and unsafe for writes; two
helpers that returned the same value for "absent" and "unreadable"; a client built with
defaults meant for interactive use inside a single-slot semaphore; a preference that
silently overrode its own argument; a dict literal that made two branches identical.

### Medium


### Low (selected — full list in the audit output)


### Observation, not a finding

`staging/adopted/c3a_adopt__adopt2026*.md` and `staging/rejected/c3_reject_skill__reject2026*.md`
are present, dated today: fixture-named documents in production `staging/`. The pollution
guard scans `logs/runs/` only, so it cannot observe `staging/`, which `Local Contracts`
itself calls production state. Worth deciding whether `staging/` belongs in its scope.

### Areas both auditors checked and found clean

Sentinel-never-reaches-a-provider (every traced path); unresolvable-slot handling;
`reward_model` division-by-zero and float-gate comparisons; judge limiter thread-safety
and lock ordering; thread/subprocess lifecycle on loop start/stop and watchdog timeout
reaping; `setup_env` credential *disclosure* (no secret reaches a log, repr or exception
message); judge label provenance (an absence is never persisted as a label);
`_opt_int`; `_parse_ts`; the rejection staleness comparison; `_skill_dir`'s two
containment layers; `compute_diff` truncation bounds; index growth bounds; one-handler-per-module
across all 20 `api/*.py`; and the `default_config()` mtime cache against stat/read
interleaving and concurrent callers.

## Open findings register — FIRST audit 2026-10-04 (closed)

Every entry below has been **fixed and removed**; each invariant now lives in
`Local Contracts` above. The register is intentionally empty and must stay that way — a
finding that is fixed is deleted, not annotated, and a register that only grows becomes
noise. Re-auditing means adding **new** entries here with fresh evidence.

Method note for whoever audits next: do **not** run `tests/smoke.py` and do **not** import
plugin modules during a read-only audit. The suite writes fixtures into production
`staging/` and `logs/runs/`, and importing creates `__pycache__`. Use Read/Grep/Glob plus
in-memory checks (`ast.parse`, `node --check`). Note also that four of the 28 findings
were only findable **structurally** — by simulating the framework's handler selection, by
tracing a config key to its consumer, and by checking what a test suite is structurally
incapable of seeing. Grep alone found none of them.
## Audit remediation: complete

Two read-only audits of 2026-10-04 produced **60 findings**: 28 in the first pass
(F1-F14, M1-M14) and 32 in a second, independent pass (R1-R32). All 60 are
remediated across v1.8.35 - v1.8.46 and their invariants now live in `Local Contracts`.
`REMEDIATION_ROADMAP.md` records the phasing; `CHANGELOG.md` records each release.

| Phase | Version | Findings | Theme |
| --- | --- | --- | --- |
| 1 | 1.8.35 | F1, F5-F9 | Safety rails and the autonomy contract |
| 2 | 1.8.36 | F2, F3, F10, M5-M8 | The human approval path is authoritative |
| 3 | 1.8.37 | F4, F11-F14 | Inert configuration and latent dispatch |
| 4 | 1.8.38 | M1-M4 | Ledger durability and path safety |
| 5 | 1.8.39 | M9-M14 | Privacy, resources, performance |

Second pass (R1-R32), each phase one release:

| Phase | Version | Findings | Theme |
| --- | --- | --- | --- |
| 6a | 1.8.41-1.8.42 | R1, R2 | The authorisation path |
| 6b | 1.8.43 | R3, R4 | Stale / absent authorisation answers |
| 6c | 1.8.44 | R5, R6, R7, R19, R23 | The LLM boundary |
| 6d | 1.8.45 | R8-R12, R18, R20, R21, R22 | Ledger and state consistency |
| 6e | 1.8.46 | R13-R17, R24 | Privacy completion |
| 6f | 1.8.47 | R25-R32 | Divergence, fallbacks, stale contracts |

Third pass (T1-T9, G1-G11, P1-P3, A2-A11, U3-U10, C4), one release:

| Phase | Version | Findings | Theme |
| --- | --- | --- | --- |
| 7 | 1.8.48 | T1-T9, G1-G11, P1-P3, A2-A11, U3-U10, C4 | The authorisation path, the silent writers, and the silent tests |
| 8 | 1.8.49 | X1-X7 | Regression pass over v1.8.48: an empty rollback and an inverted rail |
| 9 | 1.8.50 | F1-F10, G1-G9 | Fifth pass: the fragment API applied nothing |

Suite: 204 → **354** deterministic cases. Every fix is pinned by a test
**verified load-bearing by mutation** — reintroducing each defect
individually makes the suite fail (43 mutations, 0 survivors). Eleven new
cases were decorative on their first draft across the three passes and were
rewritten — see the `Local Contracts` note on structural assertions. One more
trap, from the mutation harness itself: a mutation that is **byte-length
identical** (`[rotated, base]` → `[base, rotated]`) can be masked by a stale
`.pyc` under 1-second mtime granularity, so the harness clears `__pycache__`
before every run. A survivor that manual re-runs do not reproduce is usually
this, not a decorative test.

**On running the suite without touching production.** The suite writes fixtures into
production `staging/` and `logs/runs/`, which the two guards added in v1.8.42 detect
but cannot undo. For verification that must not alter a live install, copy the plugin
tree to a scratch project root — `<scratch>/usr/plugins/skillopt` plus `<scratch>/webui`
and a scratch `usr/.env` — and run `tests/smoke.py` there. Two cases need that root:
`M14` resolves the WebUI three levels up from `PLUGIN_ROOT`, and the v1.8.5
`setup_env` case derives its provenance from `<project>/usr/.env`, so it passes only
on an install whose env actually declares the credential — a machine-dependent
result, not a plugin defect.

**The remediation was then audited against itself** in v1.8.40, re-running the
same structural checks over the ~1,500 lines it added: 0 bare imports, 0
non-atomic writes, 0 swallowed state writes, 0 broken contracts, and 0 real
tautological tests. Two weaknesses were found and fixed — see the CHANGELOG.
The standing lesson: **a test that reimplements the logic under test is worse
than no test**, because it is green while the code is broken. That is how F1
shipped, and it is now the shape of test this plugin writes.

## Work Guidance

- **Agent tools.** `skillopt_sleep(verb=dry-run|run|status|adopt|harvest, skill="")` — `status` and `adopt` run inline, the rest launch a detached cycle and return a pid plus log path. `skillopt_status(detail=summary|rollouts|skills|staged)` is read-only, `skillopt_setup(backend=…, dry_run)` writes env references, `skillopt_train(kind=info|run|validate)` wraps the upstream training loop.
- **Audit method under a live runtime.** A read-only audit of this plugin must **not** run `tests/smoke.py` (it writes fixtures into production `staging/` and `logs/runs/` — see the two guards added for exactly that) and must **not** import plugin modules (that writes `__pycache__`). Use Read/Grep/Glob plus in-memory checks such as `ast.parse(pathlib.Path(p).read_text(...))` and `node --check` for JS. `Local Contracts` is the authority on what is deliberate; a swallowed exception is only a finding when it hides a **state-write failure** or makes a **safety rail** inert.
- **Never rewrite a test block by script without asserting the registered count.** A scripted replacement that recomputes a function's span can silently consume its `@test` decorator; the function stays defined, the file still compiles, the suite still runs green, and one test simply stops running. That happened **three times** in this file (v1.8.36, v1.8.41, v1.8.43) and it cost a full release each time. After any scripted edit to `tests/smoke.py`, assert both that it still parses **and** that the registered test count went **up** by the number added.
- **Mutation-test a fix whose test could plausibly be decorative.** For R3 and R4 the first-draft tests passed with the bug restored, because they stubbed the function under test. A negative-path fix is exactly where this bites: the test asserts an error branch, and an error branch is easy to satisfy by stubbing. Verify by reintroducing the bug and confirming the suite fails — and when a mutation is *not* detected, check whether the mutation targeted a path the test aims at before concluding the test is good.
- **Never trust a single class-per-file assumption in `api/`.** The framework binds `classes[0]` from `load_classes_from_file`, which returns the **alphabetically last** matching class. Two `ApiHandler` subclasses in one module means the read-shaped one is silently unreachable (F4). Keep one handler per module and assert it in the suite.
- **Config keys must be traced from `cfg` to the consumer, not grep-matched.** `t_v1824_config_keys_have_readers` searches for the leaf name anywhere in the sources, so a key that only appears in a helper's own signature counts as "read" while no caller ever supplies it (F13). When adding a setting, confirm the value reaches the code that uses it.

- **HTTP.** One module per group in `api/`; read `config` with GET — POST is a write that whitelists known keys, so an empty-body POST is rejected rather than wiping settings. `POST /mode` is the single read/write surface for the operating mode: it returns the **resolved** answer plus `source` (canonical key vs legacy alias), the kill-switch state, and the rail caps, so the UI can state them *before* the operator turns autonomy on. It takes `adopted_today` as an input from the loop-state mirror and must never read `.auto_loop_state.json` itself — that would make it a second reader/writer of a single-writer file.
- **Sleep loop.** The engine is a one-shot `python -m skillopt_sleep <verb>` subprocess, not a daemon; the plugin decides when it runs. The adapter maps the verb, resolves the target skill to a real `SKILL.md` path, and reads the verdict from the engine's report file rather than scraping logs.
- **Auto loop tick.** Read merged config, log `last_tick_at`, auto-opt-in newly seen skills, and consider a cycle when rollouts since the *persisted* `last_rollout_count_at_cycle` cross the threshold (a per-tick delta never fires on a slow harvest). Per skill: governance eligibility → cadence → budget. Then drain staging: attempt every candidate up to `auto_adopt_max_per_tick`, newest first, never letting one candidate's failure stall the rest.
- **Scripts.** `python scripts/run_sleep_cycle.py` runs judge → official gate → summary and fails fast on a mock backend unless `--allow-mock-backend` is passed. Offline: `label_rollouts.py`, `train_reward_model.py --mode train`, `calibrate_judge.py`, `replay_worker.py`, `replay_gate_worker.py`, `check_hub_status.py`.
- **Judge persistence boundary.** `helpers.judge_client.judge_rollouts()` is intentionally stateless and returns labels in memory; it does not write rollout JSON. `helpers.llm_judge.label_rollout_file()` is the separate atomic, persistent labeling path used by `scripts/label_rollouts.py`. The gated-cycle runner currently calls the stateless path, so the same rollout can be judged again on later runs. Do not describe gated-cycle labels as persisted; if changing this, preserve atomic writes and distinguish label provenance/model/timestamp from the original rollout data.
- **Official-engine readiness.** `scripts/run_sleep_cycle.py` refuses an unset or mock `official_backend` unless explicitly allowed for testing. The automated adapter also treats the official report as authoritative and must not fall back to an ungated local rewrite after an official gate rejection. A successful mock-backed run is not evidence that useful optimization is operational.
- **Audit freshness.** `ROADMAP.md` has historical status addenda; use its dated “Current audit and next work” section as the current local-code snapshot. Older statements about versions, hub PR state, or runtime health are historical and must not be presented as current without rechecking.
- **Upstream compatibility.** The Microsoft `SkillOpt` repository's `main` branch can be ahead of the installed `skillopt_sleep` package. Treat features documented only on `main` (for example `evalkit`, multi-skill fan-out/subset adoption, and newer transcript/backend options) as optional capabilities: detect CLI support before passing flags or relying on output layouts, and keep the current single-skill/staging path as the compatibility fallback. Upstream source integrations for other agents are not a replacement for Agent Zero's native `monologue_end` capture.
- **Evalkit report isolation.** `replay_evalkit_enabled` is opt-in. The real-gate worker may attach upstream paired statistics only after the local replay has completed; probe the installed module's help for every required option and store only a bounded JSON report. Missing support, malformed data, or process errors are report-unavailable states and must not alter the gate verdict, sidecar status, or adoption path. This report uses already-scored gate pairs and is descriptive, not an independent confirmation set.

## Verification

From the plugin root:

- `python tests/smoke.py` — the deterministic suite (no LLM, no network, no clock dependence; subprocess, LLM, and async paths are mocked). Trust its exit code, not a remembered count. One platform note: the `0600` env-file assertion is POSIX-only (Windows `os.chmod` reports `0o666`, so the test asserts intent there and the real check belongs in the container). Judge pacing spacing is asserted on the injected `_monotonic` seam, not wall-clock — do not reintroduce a sub-15ms `min(gap)` assertion.
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
