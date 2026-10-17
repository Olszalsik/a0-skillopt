# SkillOpt — Informed Approval + Dual-Mode Evolution: Implementation Plan

Target version: **1.8.34** (current 1.8.33; keep `plugin.yaml`, `plugin.py`, `hooks.py`,
`execute.py` `EXPECTED_VERSION` identical per the child DOX "Before you finish" rule).

Scope: two requirements — (1) an approval surface that shows *what* is being changed and
*why*, not just a name; (2) a toggleable manual/autonomous operating mode, defaulting to
manual.

---

## 1. Verified current state

All line references are against the working tree at v1.8.33.

### 1.1 There are two approval surfaces, and both are name-only

**A. Governance card** — `webui/config.html:146-175`. Per opted-in skill it renders only
the skill name, `last_decisions[s].reason`, and a paused flag, then four buttons
(`governanceApprove` / `governancePause` / `governanceResume`). Backing route
`api/governance_status.py` returns `{policy, markers, eligible, reason, last_human_decision}` —
**no proposal identity, no proposal content, no pointer to a staged file**. A user staring at
this card cannot tell *which* pending change they are approving.

**B. Staged proposals card** — `webui/config.html:120-144`. Shows skill, byte size,
`last_outcome`, `lift_pp`, `n_held_out`, `gate_reason`. The one field designed for a human
summary is a placeholder:

```python
# api/staged.py:65
"diff_summary": f"{size} bytes",
```

and it is never rendered by the template at all.

**C. `scripts/run_sleep_cycle.py:217-221`** emits `status='PENDING_HUMAN_APPROVAL'` carrying
only `staged_path` + `staged_size`. The path is available; nothing reads it.

### 1.2 Why nothing richer exists: proposals and the approval ledger are disjoint

- `sleep_runner.find_staged_proposals()` (`sleep_runner.py:461`) scans **staging top level only**.
- The approval ledger (`logs/runs/governance.log`) is **per-skill**, written by
  `mark_decision` / `mark_human_decision` (`governance.py:429`, `:442`). It has no
  `proposal_id` column.
- The proposal's identity *is* its filename stem (`proposal_id` in `api/adopt.py:71`, `:124`).
- There is no proposal→decision linkage in either direction, and **no diff or summary code
  anywhere in the plugin** (`difflib` / `unified_diff` / `rationale` / `intent`: zero hits in
  `helpers/`).

So requirement 1 is genuinely new plumbing, not a UI tweak.

### 1.3 Requirement 2 is ~70% already built — this is the most important finding

The autonomous half of "fully autonomous mode" **already exists and is wired**:

| Piece | Location | State |
| --- | --- | --- |
| `auto_adopt` switch | `default_config.yaml:48`, default `false` | shipped |
| Drain gated on it | `auto_loop.py:288` → `_auto_adopt` (`:768`) | shipped |
| Burst bound | `auto_adopt_max_per_tick: 5` (`default_config.yaml:54`) | shipped |
| Override of the approval gate | `governance.check_skill_eligible(..., auto_adopt=)` step 7 (`governance.py:422`) | shipped (v1.8.18 P2.1) |
| Both call sites pass the flag | `auto_loop.py:534`, `auto_loop.py:1280` | shipped |
| Manual default | `governance.default_policy.require_human_approval: true` (`default_config.yaml:531`) | shipped |
| UI toggle | `config.html:64` (`x-model="cfg.auto_adopt"`) | shipped |

Per-skill governance, the adoption gate, staging, rollback, and the real-gate confirmation
path are all present. **The gap is not capability — it is that there is no first-class mode
concept, no daily safety budget for autonomous operation, no distinct audit trail for
machine-made decisions, and no UI that makes the risk legible before the toggle is flipped.**

### 1.4 Adjacent live bug found during this survey (fix in P0)

`config.html:426-433` — `save()` POSTs a hard-coded 6-key payload. The Alpine `cfg` object
declares `privacy_redact_secrets`, `privacy_include_tool_args/results`,
`rollout_retention_days`, `replay_evalkit_*` (`:263-271`) and they render as editable
controls, but **none of them are in the save payload** — they are silently discarded on every
save. Same for `refresh()` (`:318-320`), which only copies keys already present in `cfg`.

This is the exact class of defect that produced the v1.8.2 config-wipe incident, and any new
key added by this plan will hit it unless the save path is fixed first. **P0 blocks P3.**

---

## 2. Key architectural decision: one switch, not two

**Do not introduce a second flag next to `auto_adopt`.**

A plausible-looking design is "add `evolution_mode: manual|autonomous` and keep `auto_adopt`
for the drain". That is how the plugin ends up with two overlapping switches whose interaction
is undefined — the failure mode behind both `auto_adopt: null` silently disabling the loop
(`_auto_adopt` truthiness) and the v1.8.18 semantics change that surprised operators.

Instead:

> `auto_adopt` becomes the **legacy alias** of a single new operator-facing key. One resolver,
> one call site per consumer, one place that answers "is this install autonomous?"

```
governance.evolution_mode: manual   # new, canonical, default
        │
        ├── set to "autonomous"  ──────────────┐
        └── absent/empty                          │
                │                                 │
        auto_adopt: true   (legacy alias)  ──────┤
                                                 ▼
                                  resolve_evolution_mode(cfg) -> {mode, autonomous, source}
                                                 │
                    ┌────────────────────────────┼────────────────────────────┐
                    ▼                            ▼                            ▼
        auto_loop._tick (drain gate)   check_skill_eligible(auto_adopt=)   audit decided_by
        auto_loop.py:288               governance.py:422                 "autonomous"
```

Existing installs that already set `auto_adopt: true` keep working untouched, and
`source` in the resolved dict makes the provenance auditable.

---

## 3. Requirement 1 — Informed approval

### 3.1 Data structure: `ProposalSummary`

New module `helpers/proposal_view.py`. One schema, persisted as a sidecar beside the staged
proposal, following the plugin's established sidecar pattern (`.gate.json`, real-gate sidecar
and tasks files — `sleep_runner.py:1436`, `:1439`).

Sidecar path: `staging/<proposal_id>.md.summary.json`
(suffix constant `PROPOSAL_SUMMARY_SUFFIX = ".summary.json"`, sibling of
`OFFICIAL_GATE_MARKER_SUFFIX`).

```jsonc
{
  "schema": "skillopt.proposal_summary/1",

  "proposal_id": "document-query",     // staging filename stem
  "skill": "document-query",
  "created_at": "2026-10-03T12:29:31+00:00",
  "generator": "proposal_view",
  "generator_version": "1.8.34",
  "mode_at_generation": "manual",       // resolved evolution mode when written

  "intent": {
    "category": "bug_fix",              // bug_fix | performance | new_feature
                                       // | refactor | docs | unknown
    "confidence": 0.72,                 // 0.0 .. 1.0
    "source": "llm",                    // "llm" | "heuristic" | "absent"
    "headline": "Fix silent failures in the multi-page PDF extraction path"
  },

  "summary": "One plain-text paragraph, 40..600 chars. What changes and why, in the
              operator's terms. Never empty; falls back to a derived sentence.",

  "rationale": [                        // 1..6 bullets, evidence-anchored
    "Trigger: 23 of 158 recent rollouts failed on multi-page PDFs",
    "Change: added per-page error handling around step 3",
    "Expected effect: hard failures become reported partial results"
  ],

  "evidence": {
    "rollouts_analyzed": 158,
    "rollouts_recent": 23,
    "n_held_out": 8,
    "lift_pp": 6.4,
    "gate_outcome": "accept",                    // accept | reject | absent
    "official_gated": true,
    "real_gate": { "status": "done", "accepted": true, "n_pairs": 8 },
    "ab_harness": { "lift_pp": 6.4, "confidence": 0.7 },
    "failure_memory_hits": ["missing-example-block"]
  },

  "diff": {
    "available": true,
    "truncated": false,
    "added_lines": 12,
    "removed_lines": 3,
    "hunk_count": 2,
    "text": "--- a/usr/skills/document-query/SKILL.md\n+++ b/staging/document-query.md\n@@ ..."
  },

  "artifacts": {
    "proposal_path": "<abs>", "proposal_rel": "staging/document-query.md",
    "target_path": "<abs live SKILL.md>",
    "summary_rel": "staging/document-query.md.summary.json",
    "official_staging_dir": "<abs>",             // when official-gated
    "cycle_entry": { }
  },

  "warnings": ["official gate marker absent — gate provenance unknown"]
}
```

**Bounds (all configurable, all with hard ceilings):** `summary_max_chars: 600`,
`diff_max_lines: 400`, `diff_max_chars: 20000`. On overflow set `diff.truncated = true` and
append a warning — never silently cut.

**Backfill.** All ~200 currently-staged files predate this. `build_summary()` reads
sidecar-if-present, else computes lazily on read and persists best-effort. No migration step,
no blocking startup work.

**Intent classification, deterministic-first.**
1. **Heuristic** over the diff hunks + `failure_memory` hits + gate evidence:
   error-handling additions / failure-memory categories → `bug_fix`; removed redundancy,
   lowered size with equal lift → `performance`; new `##` sections or example blocks →
   `new_feature`; reordering only → `refactor`; prose-only → `docs`; else `unknown`.
2. **LLM refinement** via `chat_model` slot `"utility"` (bulk classification — per the v1.8.28
   slot contract), bounded call, `source: "llm"`.
3. LLM failure, timeout, or malformed JSON → keep the heuristic result with
   `source: "heuristic"`.

`intent` is **advisory**. It is display-only and must never gate, never block staging, and
never be read by `validate_proposal`. `helpers/llm_judge.py` already establishes the
"never raises, burst-protected" pattern to copy.

### 3.2 Sidecar lifecycle — the trap that already bit this plugin

`staging/<x>.md` is moved by `move_staged_*` / `quarantine_staged_proposal`, and the existing
sidecar tuple at `sleep_runner.py:504-511` unlinks `.gate.json`, the real-gate sidecar, and the
real-gate tasks file. **A summary sidecar left behind is exactly the orphaned-sidecar deadlock
documented at `auto_loop.py:852-858`** — `_real_gate_cleanup` would eventually clear it, but the
general rule stands.

**Action:** add `PROPOSAL_SUMMARY_SUFFIX` to that tuple in the same commit as the summary
writer, and to `adopt.py` / `reject.py` consume paths. Add a smoke test that
adopt → no `.summary.json` orphans.

### 3.3 Backend changes

**`api/staged.py`** — replace the placeholder `diff_summary` with real fields and add the
pending-proposal linkage the governance card needs:

```python
"summary": <ProposalSummary or None>,
"pending_approval": {           # new: what the Governance card is actually gating
    "proposal_id": ...,
    "eligible": bool,
    "reason": "require_human_approval_pending",
    "staged_at": ...,
    "summary": <ProposalSummary or None>,
},
"decision_state": "pending" | "approved" | "rejected" | "none",
```

`pending_approval` is resolved by scanning staged proposals for one whose `skill` matches and
whose `proposal_id` has no `human_decision` / `adopted` / `rejected` row newer than its
`staged_at` in `adoptions.log` + `cycle_history.jsonl`. This is the missing link from §1.2.

**`api/proposal.py`** (new) — `POST /api/plugins/skillopt/proposal`, body `{proposal_id}`.

- Resolves the id **only** through `find_staged_proposals()` stem matching. Never accepts a
  caller-supplied path. This is a read of skill-adjacent user content served over HTTP; a
  traversal bug here leaks arbitrary files.
- Returns `{ok, proposal_id, summary, diff_text, warnings}`. Defaults to the summary's
  embedded (already-bounded) diff; `full: true` streams up to `diff_max_lines` for the
  "view full changes" affordance.
- `ApiHandler` subclass like its siblings, inheriting framework auth + CSRF. Per the DOX
  "Safety" rule there is no auth exemption anywhere in this plugin and none is added here.

**`api/governance_status.py`** — add per-skill `pending_proposal` (from the `pending_approval`
resolver above) so the Governance card can render the same informed payload without a second
round trip.

**`helpers/governance.py`** — add `find_pending_proposal(skill) -> dict | None` and extend
`mark_human_decision(skill, approved, decided_by, *, proposal_id=None, summary_id=None)` to
record the decision against a specific proposal. The extra kwargs are optional and default to
`None`, so all existing callers and the ~15 existing smoke assertions stay valid.

### 3.4 Frontend changes (`webui/config.html`)

**Governance card (`:160-174`)** — when `s` has a `pending_proposal`:

- intent badge (coloured by `intent.category`, grey when `unknown`)
- `summary` paragraph
- `rationale` bullet list
- `evidence` strip: lift, held-out n, gate outcome, real-gate status
- expandable `<details>` with the diff in a scrollable `<pre class="so-diff">`
- "View full proposed changes" → expands the uncapped diff via `/proposal`
- path line: `staging/<id>.md` (relative, copyable)

**Staged proposals card (`:127-143`)** — same block, driven by the new `p.summary`, plus a
**Reject reason** prompt (`rejectProposal` currently hardcodes
`reason: 'user rejected via UI'` at `:358` — a free-text reason is what makes a later
re-autonomy decision informed; reject-with-reason feeds `failure_memory`).

**Load order.** `refreshStaged()` (`:332`) already runs before `refreshGovernance()` (`:328`);
store `stagedProposals` and derive the governance card's pending block from it. One fetch,
one consistent render.

---

## 4. Requirement 2 — Dual-mode operation

### 4.1 Mode resolver

`helpers/governance.py`:

```python
EVOLUTION_MODES = ("manual", "autonomous")

def resolve_evolution_mode(cfg: dict | None = None) -> dict[str, Any]:
    """Single source of truth for manual vs autonomous. Never raises.

    Returns {"mode", "autonomous", "source", "raw", "killed"}.
    """
```

Resolution order (first match wins):

1. **Kill switch.** `logs/runs/.autonomous_pause` present → `{"mode": "manual",
   "autonomous": False, "killed": True}`. Checked *first*, so it stops autonomy even if config
   says autonomous. Written by `api/loop.py`-style admin action; deleted to resume.
2. `governance.evolution_mode` — a canonical string in `EVOLUTION_MODES`.
3. `auto_adopt` — legacy boolean alias (coerced via the same strict parser).
4. Default → `manual`.

**Strict coercion, never a bare truthiness test.** `cfg.get("auto_adopt", False)` is `None`
for `auto_adopt:` with no value, and `True` for the string `"false"` — both wrong, both
already capable of silently disabling the drain. Accept only real booleans and the exact
strings `true/false/yes/no/1/0` (case-insensitive). Anything else → `manual` **plus** a line
in `logs/runs/auto_loop.log` naming the value and the valid set, mirroring the
`official_backend` rejection precedent (`official_adapter`, documented in the DOX). A bad
setting must be *self-diagnosing on the next read*, not a silent default.

Consumers must call the resolver, never read `cfg["auto_adopt"]` directly:

| Site | Current | Change |
| --- | --- | --- |
| `auto_loop.py:288` | `if cfg.get("auto_adopt", False):` | `if resolve_evolution_mode(cfg)["autonomous"]:` |
| `auto_loop.py:534` | `auto_adopt=bool(cfg.get("auto_adopt", False))` | `auto_adopt=autonomous` |
| `auto_loop.py:1280` | same | same |
| `run_sleep_cycle.py:215-216` | reads both raw | report `resolved_mode` in the staging emit |

`run_sleep_cycle.py` **still never adopts** — that invariant is explicit in its docstring
(`:16-18`) and must be preserved. Autonomous mode changes nothing about it.

### 4.2 Autonomous-mode safety rails

Autonomy is default-OFF and must stay bounded. Existing rails that autonomous mode must
**not** be able to relax — all evaluated at `governance.py:364-414`, i.e. strictly *before* the
approval step at `:422`:

- `.skillopt.optout` marker → `opted_out_via_marker`
- `.skillopt.pause_until` marker → `paused_until_marker`
- `mode: immutable` → `mode_immutable`
- `mode: opt_in` / `opt_out` without `.skillopt.optin` → `mode_*_no_marker`
- `min_interval_seconds` → `rate_limited_min_interval`
- `daily_budget_cents` → `daily_budget_exceeded`

Add `t_autonomous_cannot_bypass_safety_rails`: for each of those six, assert
`check_skill_eligible(skill, auto_adopt=True)` still returns the non-eligible reason.

**New rails (genuinely missing):**

| Key | Default | Why |
| --- | --- | --- |
| `autonomous_max_adoptions_per_day` | `5` | `auto_adopt_max_per_tick: 5` × a 30-min tick = a theoretical **240/day**. There is no daily bound on autonomous writes to live skills today. This is the single most important addition in the plan. |
| `autonomous_max_adoptions_per_skill_per_day` | `2` | Stops one skill churning while the global cap looks healthy. |
| `autonomous_require_post_adopt_check` | `true` | Reuse the real-gate executor for one post-adopt held-out confirmation; auto-`rollback` via the existing `fragment_store.snapshot_default` + `/rollback` path when it fails. |
| `autonomous_min_evidence_rollouts` | `20` | Refuse to auto-adopt a proposal built from a thin rollout base. |
| `autonomous_audit` | `true` | Force (below). |

State: extend the existing single-writer `logs/runs/.auto_loop_state.json`
(`auto_loop.py:66-97`, write-temp + `os.replace`) with an `autonomous_adoptions` block keyed
by UTC date. **Do not add a second writer** — the DOX "State files are single-writer" rule is
explicit and a second writer to that file has already been an audit finding.

### 4.3 Autonomous audit trail

Every autonomous adoption writes to the **existing** `governance.log` via the **existing**
`mark_human_decision` signature, with `decided_by="autonomous"`:

```json
{"ts_iso": "...", "skill": "document-query", "event": "human_decision",
 "approved": true, "decided_by": "autonomous",
 "proposal_id": "document-query", "summary_id": "...",
 "mode": "autonomous", "evidence": {"lift_pp": 6.4, "n_held_out": 8,
 "real_gate": "accepted"}}
```

Reusing `decided_by` (already a parameter at `governance.py:442`) rather than inventing a
second ledger keeps one queryable history. A machine decision must be distinguishable from a
human one **forever** — that is the price of the OFF-by-default guarantee, and the reason this
is not optional.

### 4.4 Frontend changes

- **Mode selector** in a new "Evolution mode" card, above Governance: a two-state control with
  the current resolved mode, its `source` (`evolution_mode` vs legacy `auto_adopt`), and
  `killed` state when the marker file is set. Default renders as **Manual**.
- Turning it ON must show an explicit, non-dismissible-ish confirmation stating: autonomous
  mode writes to live `SKILL.md` files without asking; the daily caps; the audit trail; and
  the per-skill opt-out escape hatch.
- Governance card renders a persistent "AUTONOMOUS — changes will be applied without your
  approval" banner when active, so the mode is never invisible.
- `config.html:64` `x-model="cfg.auto_adopt"` is replaced by the canonical
  `cfg.governance_evolution_mode`. Keep `auto_adopt` in the save payload as the mirrored
  legacy value so old config consumers and any external tooling stay consistent.

### 4.5 Config changes (`default_config.yaml`)

```yaml
governance:
  # v1.8.34: the single operator-facing operating mode.
  #   manual     — improvements are proposed and wait for an explicit human decision
  #   autonomous — improvements that pass every gate are adopted without asking
  # OFF by default. `auto_adopt` is the legacy alias and is only read when
  # `evolution_mode` is absent.
  evolution_mode: manual
  autonomous_max_adoptions_per_day: 5
  autonomous_max_adoptions_per_skill_per_day: 2
  autonomous_require_post_adopt_check: true
  autonomous_min_evidence_rollouts: 20
  autonomous_audit: true
```

**Smoke-suite coupling (non-negotiable).** `t_v1824_config_keys_have_readers`
(`tests/smoke.py:349`) scans every leaf key against plugin source and fails on any key nothing
reads, with a **bidirectional** `KNOWN_DEAD` allowlist. Six new keys → all six must be read in
`helpers/`, and `auto_adopt` must remain read by `resolve_evolution_mode`. A declared-but-unread
key is a switch the operator can flip that does nothing.

### 4.6 `api/config.py` — send shape and the missing coercion

`api/config.py` already does two things this plan depends on, verified in the v1.8.33 tree:

- **Whitelist + empty-body-is-a-read** (`:88-108`) — the v1.8.2 config-wipe fix. A `governance`
  POST cannot persist anything outside the merged defaults.
- **One level of nested dict merge** (`:103-106`) — posting
  `{"governance": {"evolution_mode": "autonomous"}}` already works: `inner = dict(merged[k])`
  then `inner.update(v)` shallow-merges, preserving the other `governance.*` siblings. So
  **nested-path support is not needed**; the Alpine `save()` payload must simply send a nested
  `governance` object rather than dotted keys.

The real gap is **type coercion**: `clean[k] = v` at `:107` persists the client's value
verbatim, and `_read_disk()` (`:49-66`) likewise applies no validation. So
`{"governance": {"autonomous_require_post_adopt_check": "yes"}}` lands in `config.json` as the
string `"yes"` and reaches every consumer unvalidated.

Add coercion at the endpoint — a value that cannot be coerced to the default's type is logged
and skipped, never aborting the save (mirrors `initialize._args_override()` in the root DOX).
Keep the strict parser in `resolve_evolution_mode` as well: it must hold for configs written
by hand or by an older build, not only for values that passed through this endpoint.

---

## 5. Phase roadmap

Each phase is independently shippable; `python tests/smoke.py` runs after **every** edit
(child DOX: `py_compile` cannot catch a trailing comma silently wrapping a 2-tuple into a
1-tuple, and that exact bug shipped once at `auto_loop.py:1273-1279`).

### P0 — Groundwork and bug fixes *(no user-visible feature)*
1. Fix `config.html` `save()` (`:426-433`) to send every editable key, driven by an explicit
   allowlist shared with `refresh()`'s copy-in. Restores the five silently-dropped privacy /
   evalkit settings. **Blocks P3.**
2. Version bump 1.8.33 → 1.8.34 across the four files.
3. Add `PROPOSAL_SUMMARY_SUFFIX` plumbing to the `sleep_runner.py:504-511` sidecar tuple.
4. Backfill-safe reader stubs for `ProposalSummary` returning `{"available": False}`.

*Exit:* suite green; a privacy toggle survives a save/reload round trip.

### P1 — `ProposalSummary` backend
1. `helpers/proposal_view.py`: `build_summary`, `compute_diff`, `classify_intent`,
   `read_summary`, `write_summary`, all bounded and non-raising.
2. Wire generation at both staging points: `official_adapter` copy-to-staging and the direct
   optimizer's staging write.
3. Extend `api/staged.py` (`pending_approval`, `decision_state`, real `summary`); extend
   `api/governance_status.py` with `pending_proposal`.
4. Extend `governance.mark_human_decision` with optional `proposal_id` / `summary_id`.
5. `api/proposal.py` (new route, path-safe resolution).

*Exit:* every staged proposal has a summary with a non-empty `summary` and an `intent`; no
`.summary.json` orphans after a full consume/quarantine cycle.

### P2 — Informed approval UI
1. Shared Alpine component `proposalBlock(summary)` in `config.html` — intent badge, summary,
   rationale, evidence strip, expandable diff, full-changes toggle, path line.
2. Wire into both the Governance and Staged cards.
3. Reject-with-reason prompt feeding `failure_memory`.
4. Unbounded-diff guard: `full: true` capped at `diff_max_lines`.

*Exit:* a reviewer can state, without opening a file, *what* would change and *why* — for both
the governance-pending skill and every staged proposal.

### P3 — Mode switch
1. `governance.resolve_evolution_mode()` + strict coercion + log-on-invalid.
2. `governance.evolution_mode` in `default_config.yaml`; update the three `auto_loop.py` call
   sites and `run_sleep_cycle.py`'s emit.
3. Nested-`governance` object + type coercion in `api/config.py` (`:100-108`).
4. Evolution-mode card in the UI, with the explicit ON confirmation and the persistent
   autonomous banner.
5. `.autonomous_pause` kill-switch marker + resolver precedence.

*Exit:* `auto_adopt: true` alone still produces autonomous behaviour (back-compat);
`evolution_mode: "autonomous"` alone does too; a garbage value produces `manual` **and** a
diagnostic log line; the kill switch beats both.

### P4 — Autonomous rails
1. `autonomous_max_adoptions_per_day` + per-skill variant, in the single-writer state file.
2. `autonomous_min_evidence_rollouts` enforcement before adopt.
3. `autonomous_require_post_adopt_check` + auto-rollback on failed post-adopt confirmation.
4. `autonomous_audit` rows (`decided_by: "autonomous"`) in every autonomous adopt path.

*Exit:* a 24h autonomous run cannot exceed the daily cap; a regression auto-rolls back; every
autonomous change is separable from a human one in `governance.log`.

### P5 — Regression suite
New `t_*` functions in `tests/smoke.py` (self-contained, deterministic, no LLM/network/clock):

- `t_v1834_summary_has_required_fields` — non-empty summary, valid intent category, bounded diff
- `t_v1834_summary_backfills_missing_sidecar` — pre-1.8.34 staged file computes on read
- `t_v1834_summary_sidecar_not_orphaned` — adopt + quarantine leave staging clean
- `t_v1834_proposal_route_rejects_path_traversal` — `../../usr/.env` returns no content
- `t_v1834_staged_pending_approval_linkage` — governance card resolves the right proposal
- `t_v1834_decision_recorded_against_proposal` — proposal_id round-trips through `mark_human_decision`
- `t_v1834_mode_defaults_to_manual` — empty config, `{}` config, absent config
- `t_v1834_mode_legacy_auto_adopt_alias` — `auto_adopt: true` → autonomous
- `t_v1834_mode_invalid_value_falls_back_and_logs` — `"banana"`, `null`, `"false"` (string)
- `t_v1834_mode_kill_switch_beats_config`
- `t_autonomous_cannot_bypass_safety_rails` — all six rails, with `auto_adopt=True`
- `t_autonomous_daily_cap_enforced`
- `t_autonomous_post_adopt_failure_rolls_back`
- `t_autonomous_audit_row_distinguishable`
- `t_v1824_config_keys_have_readers` — updated, both directions

### P6 — Docs and hub framing
1. `README.md`: mode section, cap table, "autonomous writes to live skills" warning.
2. `AGENTS.md` (child DOX): the one-switch rule, the six un-relaxable rails, the
   `PROPOSAL_SUMMARY_SUFFIX` sidecar rule, the resolver-must-be-called rule.
3. `CHANGELOG.md` + `RELEASE_NOTES.md`; bump the hub `index.yaml` entry.
4. Update the `scripts/gen_missing_agents_md.py:371-382` skillopt summary — it still claims
   *"SkillOpt suggestions are PROPOSED, never auto-applied"*, which is **already false** at
   v1.8.18 and becomes more false after this work.
5. Link from `ROADMAP.md`'s dated audit section.

---

## 6. Verification

| Command | Asserts |
| --- | --- |
| `python tests/smoke.py` | Full deterministic suite. Trust the exit code, not a remembered count. |
| `python execute.py` | Manifest version matches `EXPECTED_VERSION`; loop liveness. |
| `python -c "import skillopt_sleep"` (A0 venv) | Official engine present (else the direct-optimizer path is live). |

Manual pilot on one `.skillopt.optin` skill, **manual mode**: a real chat writes a rollout with
a non-empty `skill_used` → a cycle stages a proposal → the dashboard card shows intent,
summary, and diff → Approve writes `usr/skills/<n>/SKILL.md` → the row in `governance.log`
carries `decided_by: "user"` and the matching `proposal_id`.

Manual pilot, **autonomous mode**: same pipeline with no human action → adoption with
`decided_by: "autonomous"` → daily-cap counter increments → a second attempt on the same day is
refused with a logged reason → deleting `.autonomous_pause` and flipping back to `manual`
returns the install to pending-approval behaviour on the next tick.

Container note: new plugin Python requires a container restart to load; a restart risks the
winnat port-bind failure (`net stop winnat && net start winnat`, then `docker restart`).

---

## 7. Risks and traps

1. **A second overlapping mode flag.** Highest risk. Mitigated by §2 — one resolver, legacy key
   demoted to alias, no direct `cfg["auto_adopt"]` reads outside the resolver.
2. **`config.html` save/copy allowlists silently dropping new keys.** §1.4. Already dropping
   five live settings today. P0 blocks P3.
3. **Summary sidecars orphaned by consume/quarantine.** §3.2. Precedent: the real-gate orphan
   deadlock at `auto_loop.py:852-858`.
4. **`t_v1824_config_keys_have_readers` failing on six new keys.** §4.5. Bidirectional
   allowlist — wiring a key requires removing its entry.
5. **Loose boolean coercion** (`auto_adopt: null` → no drain; `"false"` → drain). §4.1 strict
   parser plus log-on-invalid.
6. **A second writer to `.auto_loop_state.json`.** DOX "State files are single-writer".
7. **Unbounded diff in an HTTP response.** §3.3 — hard caps, `truncated` flag, no
   caller-supplied paths.
8. **Autonomy as a licence to skip rails.** §4.2 — the six rails are structurally upstream of
   the override; a test asserts each one.
9. **Intent classification becoming a hidden gate.** §3.1 — display-only, never read by
   `validate_proposal`, LLM failure degrades to heuristic.
10. **Stale documentation asserting proposals are never auto-applied.** Already false at
    v1.8.18 (`scripts/gen_missing_agents_md.py:378`); fixed in P6.