# SkillOpt Audit Remediation Roadmap

Derived from two read-only audits of 2026-10-04, both recorded in `AGENTS.md` →
*Open findings register*:

- **First pass** — 28 findings (F1-F14, M1-M14), remediated in Phases 1-5 below
  (v1.8.35-1.8.39) and self-audited in v1.8.40.
- **Second pass** — 32 further findings (R1-R32) at v1.8.40, scheduled in **Phase 6**.

Every finding ID below refers to one of those registers.

## Sequencing principle

Phases are ordered by **blast radius**, not by effort. The question for each phase is:
*if this is wrong, what does the running system do to a user's live `SKILL.md`, to the
audit trail, or to the operator's own explicit choices?*

Two rules govern every phase:

1. **Fail closed on the path to a live skill.** A rail that cannot be evaluated must
   *defer*; a gate that cannot be measured must not report success. Where a finding is
   currently fail-open toward writing a live file, that is the defect.
2. **Never silently change an operator's explicit choice.** A shipped default must never
   outrank a value the operator wrote. This is the invariant v1.8.34 got wrong (F1).

A phase is complete when the suite is green, `execute.py` is healthy, and the finding's
entry is **deleted** from the register (its invariant folded into `Local Contracts` if it
is a standing rule). Findings are not marked "fixed" in place — a register that only grows
becomes noise.

---

## Phase 1 — Safety rails and the autonomy contract  *(v1.8.35 · complete)*

**Goal:** every path that can write a live `SKILL.md` unattended is either correct or
closed. Nothing here touches the UI or the public route contracts, so the blast radius of
the change set is confined to the drain.

| ID | Finding | Why first |
| --- | --- | --- |
| **F1** | Resolver precedence read from the *merged* config, so the shipped `evolution_mode: manual` outranks the operator's `auto_adopt: true` | Live, actively misreporting, and it silently disables the whole autonomy feature. Fails toward manual, so not a safety hole — but every other autonomy finding below is untestable while it stands. |
| **F8** | `check_skill_eligible` can raise; the sole caller falls through to **eligible** | Fail-open toward writing a live skill on the exact path autonomy uses. Reachable whenever `logs/runs` is unwritable. |
| **F9** | `_human_approved` ignores `decided_by`, so an `autonomous` row satisfies `require_human_approval` | Defeats the human-approval gate the whole informed-approval feature exists to enforce. |
| **F7** | `budget.can_spend()` never rolls the day over; the cap latches permanently | Permanent silent exclusion of a skill with a misleading reason, and a self-referential deadlock (the rollover lives in `record_spend`, which only runs after a cycle that `can_spend` gates). |
| **F5** | Per-tick rollout-count invalidation sets a *class* attribute while the reader uses an *instance* attribute | The evidence rail compares against a snapshot frozen at the first drain. Inert at the shipped default; wrong for any operator who raises it. |
| **F6** | `_save_state()` is non-atomic while a concurrent reader exists on the HTTP thread | A torn read resets counters *and* zeroes `last_rollout_count_at_cycle`, which fires an unscheduled cycle. |

**Changes**

- `governance._operator_config()` prefers the operator's **persisted** config
  (`_framework_registry_config()`) whenever the registry yields a non-empty dict, and only
  treats a caller-supplied `cfg` as the operator view when no registry is available. This is
  the F1 fix: precedence must not be decided from a blob that already contains the shipped
  default.
- `auto_loop` governance failure returns `("governance_unavailable", not eligible)` instead
  of falling through; `_last_eligible_decision_ts`, `_human_approved` and the
  `.skillopt.pause_until` read all move inside their `try`.
- `_human_approved` requires `decided_by == "user"`.
- `BudgetTracker.can_spend()` / `get_status()` call `_maybe_rollover()`.
- `_invalidate_rollout_counts()` becomes an instance method; the attribute is initialised in
  `__init__`.
- `_save_state()` uses write-temp + `os.replace` + `fsync`, matching `cadence.py` and
  `budget.py`.

**New tests** — the F1 test must drive the **auto-loop** with `merged_config()`, not the bare
resolver, because that distinction is precisely why the original guard passed while
production was broken:

- `t_v1835_merged_config_does_not_outrank_operator_choice`
- `t_v1835_autoloop_resolves_mode_from_merged_config`
- `t_v1835_governance_failure_is_fail_closed`
- `t_v1835_autonomous_decision_does_not_satisfy_human_gate`
- `t_v1835_budget_daily_rollover_unlatches_cap`
- `t_v1835_rollout_count_cache_invalidated_per_tick`
- `t_v1835_state_write_is_atomic`

**Risk:** F8's fail-closed change will stall evolution if governance is broken. That is the
intended trade (a machine-written change to a live file must never be the fall-through), but
it must be *visible*: the log line and `governance_unavailable` reason are the only signal
an operator gets, so both are asserted.

---

## Phase 2 — Make the human approval path authoritative  *(v1.8.36 · complete)*

**Goal:** the routes a person clicks behave the way the approval UI claims.

| ID | Finding |
| --- | --- |
| **F2** | `/reject` is advisory only — no quarantine, no recorded denial, no "human said no" rail, so the drain re-adopts it |
| **F3** | `api/adopt.py` writes the live skill with `write_text` and proceeds when the snapshot failed |
| **F10** | `/governance_approve` defaults `approved` to `True` and treats `"false"` as approval |
| **M5** | `/adopt` silently falls back to the newest proposal when `proposal_id` matches nothing |
| **M6** | The settings page reports a failed snapshot as success (`\|\| true`) |
| **M7** | Neither manual adopt path honours real-gate single-flight, the ungated-adopt refusal, or consumes the proposal |
| **M8** | `tools/skillopt_sleep.py verb=adopt` skips the governance chain entirely |

**Key design decision:** F2 needs a **new rejection rail** in `check_skill_eligible`,
evaluated *above* the `auto_adopt` bypass exactly like the other six rails. A human "no" is
not an approval gate that autonomy may override — it is a decision about a specific
proposal, and it must bind to `proposal_id` (which `mark_human_decision` already supports)
with a staleness check so a *new* proposal for the same skill is not blocked by an old
rejection.

F3 is a one-line redirection to `sleep_runner.adopt_write_skill()`, which already
snapshot-then-atomically-replaces and refuses when the snapshot fails. F10 reuses
`api/config.py::_coerce_like_default` rather than inventing a third coercion dialect.

**Tests:** reject-then-tick does not re-adopt; adopt is atomic and refuses a failed
snapshot; missing/-stringly-typed `approved` cannot approve; a non-matching `proposal_id`
errors instead of adopting the newest; the adopt banner is not green on a failed snapshot.

---

## Phase 3 — Restore inert configuration and latent dispatch  *(v1.8.37 · complete)*

**Goal:** every declared setting does what it says, and every route is reachable.

| ID | Finding |
| --- | --- |
| **F11** | Bare `helpers.*` imports make all `failure_memory_*` / `ab_harness_*` config inert |
| **F12** | `cycle_history` imports a `config_loader` module that does not exist |
| **F13** | Five `budget`/`cadence` keys are read by nothing — and the reader test cannot catch it |
| **F4** | `POST /fragments` binds `FragmentsRollback`; the read handler is unreachable |
| **F14** | Official-engine timeout is not marked `deferred`, so two engines write one staged file |

**F13 requires fixing the test, not just the wiring.** `t_v1824_config_keys_have_readers`
greps for the leaf name anywhere in the sources, so a key that appears only in a helper's own
signature counts as read. The hardened test must additionally assert that each key is
reachable from a `cfg[...]` access or an explicit kwarg at a call site — otherwise the next
inert key passes too.

F4 is fixed by one handler class per file, plus a new assertion over
`load_classes_from_file` for every `api/*.py`, because the suite currently imports handler
classes by name and is structurally incapable of noticing a dispatch mismatch.

---

## Phase 4 — Ledger durability and path-safety consistency  *(v1.8.38 · complete)*

**Goal:** the audit trail is trustworthy over time, and there is one path resolver.

| ID | Finding |
| --- | --- |
| **M1** | `_decided_proposal_ids` compares two incompatible timestamp formats across timezones |
| **M2** | Log rotation silently revokes every prior approval; readers ignore `.1` |
| **M3** | `_skill_dir` drops `safe_skill_dir`'s post-resolve containment layer; unsafe names share `_invalid_/` |
| **M4** | `or <default>` swallows an explicit `0` in `min_interval_seconds` / `daily_budget_cents` |

M2 is the sharpest: a `require_human_approval` skill silently reverts to *pending* when
`governance.log` rotates, and `decided_by="autonomous"` rows are lost — which contradicts the
"auditable forever" contract. Fix by keeping N generations and having the readers fall back,
plus a durable compact approvals index for the part that must never expire.

M1 and M4 are both "two code paths disagreeing about the same intent"; fix them together
with a shared timestamp parser and a shared optional-int reader.

---

## Phase 5 — Resources, privacy, performance  *(v1.8.39 · complete)*

**Goal:** bounded resource use and honest redaction. No correctness impact on the gate.

| ID | Finding | Note |
| --- | --- | --- |
| **M9** | Model **output** is never redacted; real-gate tasks file persisted unsanitized; `inner_loop` LLM egress unsanitized | Input redaction is correct and fails closed. The gap is output and two persistence paths. `adopt_write_skill` is the single choke point for a last-chance pass. |
| **M10** | `privacy.py` misses prefixed env-var names (`AZURE_OPENAI_API_KEY`), non-HTTP URL credentials, DSA/PGP/PuTTY headers, base64 envelopes | `\b` before an alternative preceded by `_` never matches — the most common credential form slips through. |
| **M11** | Subprocess log handles never closed (~1/cycle; blocks rotation on Windows) | `try/finally` around `Popen`. |
| **M12** | `adoptions.log`/`fragments.log`/`failure_memory.log` never rotated; whole file read per poll; two full status snapshots per page refresh | Structural cost that scales with install age. |
| **M13** | `default_config()` re-parses 32 KB of YAML uncached, on the per-chat reward-model path | Cache on mtime. |
| **M14** | Both banner CTAs dispatch `open-plugin-config:`, which the WebUI no longer implements | One-line fix; the button currently renders and does nothing. |

M9/M10 are the only items here with a confidentiality dimension. They are grouped with
performance because neither blocks the gate, but if this roadmap is triaged under time
pressure, **M9's output-redaction gap should be pulled forward** — it is the only finding
that can move a credential into a live skill document.

---

## Phase 6 — Second audit remediation: the PLAN  *(v1.8.41-v1.8.47)*

**Why this phase exists.** Phases 1-5 fixed the *first* audit's 28 findings, and v1.8.40
audited that remediation against itself. A **second, independent pass** at v1.8.40 — two
auditors on disjoint scopes — then found **32 more (R1-R32)**, recorded in `AGENTS.md` →
*Open findings register — RE-AUDIT*.

Two of the top three are defects **in the Phase 1-5 fixes**, and both have the same
shape: **a partial fix that looked complete because the test covered the sibling, not
the site.**

- **R2** — v1.8.35's F8 fix made `_skill_eligible_for_cycle` fail-closed and the test
  drove that function. But `_auto_adopt` calls `_adopt_one` *directly*, so
  `_adopt_one` was the drain's only pre-write governance gate — and it still failed
  open. The fix was real and the coverage was aimed at the wrong function.
- **R1** — Phase 2 found the HTTP adopt route had no governance check, and the response
  was a **comment in the test explaining why that was acceptable**. The route still had
  no governance check. A test whose assertions do not match its name is worse than no
  test, because it manufactures confidence in a guarantee that does not exist.

**Sequencing.** Grouped by *what could reach a live file*, not by severity alone:

### 6a — The authorisation path (R1, R2) · v1.8.41

| ID | Finding |
| --- | --- |
| **R1** | `api/adopt.py` runs **no** governance check. Clicking Approve writes a live `SKILL.md` for a skill that opted out, is immutable, is paused, or whose proposal a human **rejected**. Also: rewrite the test that falsely claimed otherwise, and remove the `mkdir` side effect that runs before the gate. |
| **R2** | `_adopt_one` fails **open** on a raising governance check — the drain's only pre-write gate. |

R1 is first because it is the only remaining finding where **one click by one person**
reaches a live file with every rail disabled, and no agent or second pair of eyes is
involved.

**Done when:** all three adopt paths issue the *identical* governance call
(`check_skill_eligible(skill, auto_adopt=resolve_evolution_mode()["autonomous"])`),
all three **fail closed** when it raises, no path creates a directory before the gate,
and a single test drives all three against the same six rails plus the rejection rail —
asserting the negative (no `SKILL.md` written), not a proxy.

### 6b — Stale authorisation decisions (R3, R4) · v1.8.42

| ID | Finding |
| --- | --- |
| **R3** | `/mode` can report the mode it just replaced (registry cache wins over the passed config), and for up to 5 s after switching **autonomy off** the drain still believes it is autonomous. |
| **R4** | The rejection rail's "undecidable ⇒ restrictive" branch is **dead code**, so an unreadable ledger silently disarms it. |

These are grouped because both are "the answer is stale or absent, and the code treats
that as the permissive reading" — the same class as R2, on the decision rather than the
gate.

### 6c — The LLM boundary (R5, R6, R7, R19, R23)

| ID | Finding |
| --- | --- |
| **R5** | `direct_optimizer._call_llm` sets no timeout: ~2 h per rollout holding the judge's only semaphore. |
| **R6** | The judge's escalation ladder is dead code (failure shapes carry no `model`), so every failed label is re-asked of the **most expensive** model. |
| **R7** | The A/B harness compares the current skill against itself, so lift is always 0; enabling it blocks all evolution. |
| **R19** | `refine_intent_with_llm`'s `timeout_s` is unused and the hook call is unbounded; `intent_llm_enabled` can never be true. |
| **R23** | `calibrate_judge.py` calibrates the stub, never sends `response_a`, and feeds fabricated scores. |

R5 and R6 are the same defect seen from two ends: an escalation path that owns neither
a timeout nor the model identity it is supposed to retry.

### 6d — Ledger and state consistency (R8, R9, R10, R11, R17, R18, R20, R25)

Reader/ledger disagreements, a tenth unwired `BudgetTracker` site, an unaudited refused
adopt, an unlocked index compaction, `ok: true` over partial failures, and O(skills ×
log) dashboard reads. Individually small; together they are the difference between a
ledger you can reason about and one you cannot.

### 6e — Privacy completion (R13, R14, R15, R16, R26, R29)

Phase 5's M9/M10 landed the *input* and *output* redaction boundaries. These are the
remaining holes: the real-gate tasks file persists raw records when sanitisation fails,
`inner_loop`'s egress never touches the trajectory (and its sanitised branch is dead
code), the `redacted` flag is discarded by every caller, and model output is redacted
only at the live write — so the raw bytes survive in `staging/`.

### 6f — Resource, cost and correctness (R12, R21, R22, R24, R27, R28, R30-R32)

The agent tool dropping its `skill` argument (M5, still live on the agent path),
`inner_loop` retiring a rollout on a transport error, `reward_model` reloading a broken
checkpoint on every chat, `setup_env` deleting an unmatched credential, a non-atomic
bridge index that duplicates harvest rows, and the stale/incorrect comments and docstrings
that mislead the next maintainer.

### Sequencing note

6a is a single focused change and should land alone — it touches the authorisation path
for every adopt, and bundling it with anything else makes a regression hard to attribute.
6b follows because it shares 6a's "permissive reading of an absent answer" class. 6c-6f
are independent and can be scheduled against spare capacity.

---

## Phase 6 — Second-pass audit remediation · **COMPLETE**  *(the outcome)*

A **second, independent read-only audit** at v1.8.40 (two auditors, disjoint scopes)
found **32 further findings (R1-R32)** — all closed. Evidence in `AGENTS.md` →
*Open findings register — RE-AUDIT*.

| Sub-phase | Version | Findings | Theme |
| --- | --- | --- | --- |
| 6a | 1.8.41-1.8.42 | R1, R2 | The authorisation path — the HTTP route ran no governance gate; the drain's own pre-write gate failed open |
| 6b | 1.8.43 | R3, R4 | Stale / absent authorisation answers — a 5 s memo outliving a write; "could not read" conflated with "nothing to record" |
| 6c | 1.8.44 | R5, R6, R7, R19, R23 | The LLM boundary — an unbounded call, a dead escalation ladder, an A/B harness comparing both arms to themselves |
| 6d | 1.8.45 | R8-R12, R18, R20, R21, R22 | Ledger and state consistency — two readers of one ledger disagreeing |
| 6e | 1.8.46 | R13, R14, R15, R16, R17, R24 | Privacy completion — six controls that looked present and did not |
| 6f | 1.8.47 | R25-R32 | Divergence, fallbacks, stale contracts |

**All 32 second-pass findings are closed, and the register in `AGENTS.md` is empty.**

Suite: 204 → **306** deterministic cases. Every fix is pinned by a test that was
**verified load-bearing by mutation** — reintroducing each defect individually makes
the suite fail.

### What the two passes had in common

Twenty of the sixty findings were **silent no-ops**: a sanitizer called and its result
discarded, a key sanitized that the payload does not have, a cache TTL that was safe for
reads and unsafe for writes, a flag computed and read by nobody, a dict literal that made
two branches identical, two readers of one ledger that disagreed. Not one of them is
findable by grep, and several are invisible to a test that asserts the *function is
called* — only a behavioural assertion catches them.

The last eight (R25-R32) added a fifth shape: **a comment or docstring that no longer
described the code**. Two readers of this plugin would have got different answers from
the docs and the implementation.

So the method is recorded as standing guidance: mutation-test any fix whose test could
plausibly be decorative, and **assert that a guard still gates, not that a string still
exists**. Nine of my own tests failed that standard during this work — three of them in
the final phase alone, all invisible by reading:

- a substring check that could not tell a false claim from the sentence *quoting* it;
- three independent substring checks that all survived `if prev_pid:` → `if False:`;
- a `sys.modules[name] = None` simulation of an unavailable module, which did nothing
  because the harness mirrors modules onto a package **attribute** that the import
  resolves through first.

### Also fixed along the way

A test defect worth recording: `t_c3_reject_records_cycle_history` rejected a proposal id
that was never staged — which the v1.8.36 id validation correctly refuses. It now stages a
real proposal and asserts the full flow.

---

## Out of scope (recorded, not scheduled)

- **`skillopt-dashboard.js` / `config.html` renderers** — the v1.8.34 informed-approval work
  is complete on both surfaces and validated by 110 in-node checks; no audit finding against
  them survived verification.
- **Framework-side changes** (`helpers/api.py`, `helpers/modules.py`) — F4's root cause is the
  framework's alphabetical class selection. The plugin-side fix (one class per file) is
  sufficient and is the only change this plugin owns; altering framework dispatch would
  affect every plugin.
- **`v1.8.26 persist_labels` tests** present in `usr/workdir/skillopt-plugin/tests/smoke.py`
  but not in this tree — a separate workstream, deliberately not merged.
