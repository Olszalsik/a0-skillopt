# SkillOpt Plugin — Changelog

All notable changes to this plugin are documented here. The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [1.8.52] - 2026-10-17

Suite-hermeticity follow-up. Running the suite on a **live install** (the v1.8.51
verification method) had exposed the remaining way operator state reaches the tests:
`config.json` itself. Suite now **360 cases** — 358 existing + 2 hermeticity cases —
**all passing both with a production `config.json` in the tree and without one**
(CI conditions).

### High: 13 governance tests read the live install's `config.json` (C1)

`governance._default_policy()` read `<plugin_root>/config.json` directly, so on a live
install the operator's `default_policy` (`mode`, `require_human_approval`,
`min_interval_seconds`, `daily_budget_cents`) bled into every rail the suite exercises:
13 adoption/governance cases read `skipped` / `require_human_approval_pending` while the
identical suite stayed green in **CI — which only passed the suite because CI has no
`config.json`**. The suite was hermetic on every path *except the one file the WebUI
also writes*; a suite red/green on a live install depended on operator intent, not on
plugin behaviour — the "passes in CI, fails in the real install" class inverted.

`governance.py` gains the same test-isolation hook every other state module already
carries (the `inner_loop.state_dir` / `fragment_store.set_plugin_root_for_tests`
pattern): a module-level `default_policy_config_path` override, read by
`_default_policy_config_path()`. **Live code never sets it.** A side benefit: the
resolver now goes through `_sleep_runner()` (both-import-names aware) instead of a
namespace-only inline import that degraded to built-ins via exception on a bare run.

### High: a live `config.json` also filled the `/mode` wipe-guard precondition (C2)

`t_v1834_mode_route_no_config_wipe` simulates an unreadable defaults source and asserts
the degraded `_read_disk()` result lacks `governance`. On a live install the production
`config.json` — read through `api/config.py`'s real `CONFIG_PATH` — filled exactly that
void, failing the precondition while nothing in the code under test had changed. The
test now points `CONFIG_PATH` at a nonexistent tmp path for the degraded leg, the same
per-test control the write leg above it already had.

### Tests
- Suite-wide sandbox in `main()`: `governance.default_policy_config_path` → nonexistent
  tmp path (bit-for-bit CI conditions: no config file), restored + tmp tree removed on
  the way out, WARN banner if install unavailable.
- `t_v1852_default_policy_seam`: fixtures through the seam — merged over built-ins,
  keys the fixture omits keep built-ins, malformed / missing / non-dict default_policy
  degrade to built-ins rather than raising or merging garbage.
- `t_v1852_suite_config_hermeticity`: the guard that fails if `main()`'s sandbox is
  lost and the install's `config.json` leaks values no built-in default matches.
- Also carried from the 2026-10-17 CI run #48 post-mortem: `SKILLOPT_JUDGE_THROTHLE_S`
  → `SKILLOPT_JUDGE_THROTTLE_S` in `t_v1818_pacing_deterministic` — the typo made the
  injected-clock case silently run against the 1.5 s default instead of its intended
  0.02 s interval, and leaked the junk env key for the rest of the run (masked, since
  its assertions only demanded ≥ 0.02 s).

### Verification
Run on a container-local copy of the live tree, both directions: **360/360 with the
production `config.json` present, 360/360 without it.** The 13 previously failing
governance cases and the C2 precondition now pass with the real file in place; CI runs
of the same suite (no config file) are unaffected.

## [1.8.51] - 2026-10-05


Sweep follow-up. Removing the 16 inert 0-byte `_default.pre_adopt.md` files surfaced two
defects that only a **live-tree** run could show. Suite unchanged at **354 cases, all
passing — now verified in the real install, not just a scratch tree.**

### High: the suite wrote into production `fragments/` (S1)

`fragments/` was the last state path the suite wrote into directly: `fragment_store.
_plugin_root()` was hardcoded to the plugin directory, and `_purge_skill_fragments()`
even `rmtree`'d the real `fragments/SKILL/` between tests. So a suite run on a live
install created fixture skill directories and wrote 0-byte `_default.pre_adopt.md` files —
**it recreated exactly what the sweep had just removed.** `staging/` and `logs/runs/` were
already sandboxed; this was the third path nobody had closed.

`fragment_store.set_plugin_root_for_tests()` now redirects the snapshot tree, and the
suite points it at a temp directory for the whole run, printing the path and restoring it
on the way out. Verified by file count across a live run: `fragments/` 563 files before,
563 after, zero temp directories left behind.

### High: three cases were machine-dependent (S2)

The policy-scope cases (`T9`, `T9b`, `X5`) passed in the scratch tree and **failed in the
live install**: stage 0 (the A/B harness) ran first and rejected with
`ab_harness_rejected`, so the stage under test was never reached — and the harness reads
its *own* settings, so `merged_config()['ab_harness_enabled'] == False` does not stop it.

Every case that drives `validate_proposal` at a stage later than 0 now passes
`ab_harness_enabled=False`. A case that reaches a stage only when an unrelated earlier
stage happens to pass is not a test of that stage; this is the "passes on my machine"
class, and the only reason it survived three audit passes is that the suite had been run
in a scratch tree the whole time.

### Verification

354/354 in the **live** tree. Zero-byte `_default.pre_adopt.md`: 16 → 13 (recreated by
the pre-sandbox run) → **0**, and still 0 after a subsequent suite run. Non-zero
`pre_adopt` (8) and all 533 `_default.v*.md` snapshots verified byte-identical by hash
manifest across the sweep.

---

## [1.8.50] - 2026-10-05

Fifth-pass audit remediation: an independent audit of the gating/authorisation path and
the data/provider path at v1.8.49. **11 findings fixed**, 2 more verified real and
deliberately left alone with the reasoning recorded. Suite 347 -> **354**.

### Critical: the fragment API applied nothing (F8)

`write_fragment` documented that it rewrites `SKILL.md`. It wrote the new text to
`<id>.current.md` — a file with **exactly one writer and no reader anywhere in the tree** —
left the body untouched, and returned `ok: True`. Since `read_fragments` recomputes text
from the body, every reader saw the OLD text.

So a fragment edit was a silent no-op, and `rollback_fragment` — its only caller — reported
a successful recovery that changed nothing. Worse, an existing test *pinned* it: the case
asserted that `.current.md` exists **and** that the body is not rewritten, describing this
as a deliberate "minimal implementation". A documented non-implementation is still a
non-implementation.

It now splices the fragment's selector span into the body and writes through
`adopt_write_skill`. It refuses — by name — what it cannot do: a selector that matches
nothing, an edit identical to what is already there, and any path outside the configured
skills dir (which would need a second, unsanctioned writer). And it re-emits the
frontmatter **verbatim**, because re-rendering it through `yaml.safe_dump` reformatted the
operator's metadata and made a rollback unable to restore the file byte-for-byte — the
difference was introduced by the restore itself.

### High: all skills shared one rollback directory (F7)

Per-fragment snapshots were keyed on `Path(SKILL.md).stem` — which is `SKILL` for **every**
skill. Two skills computed their version numbers from each other's history and overwrote
the same `<id>.v<N>.md`, and `/fragments/rollback` could restore one skill's bytes into
another. Keyed on the skill name, and the hardcoded `"fragments"` root now honours
`fragment_snapshot_dir` — the same key the status route reports.

### High: one valueless YAML key killed the loop (G3)

`_run` coerced the sleep interval *after* the `try` that keeps the thread alive.
`auto_loop_interval_sec:` (valueless → `None`) made `int(None)` raise `TypeError`, escape
`_run` entirely, and kill the only thread that ticks the plugin — nothing could run again
for the life of the process. The arithmetic now sits inside the guard.

### The rest

- **G1** the bridge's redaction pass rewrote the transcript files *outside* the lock added
  in v1.8.48, reopening the very race it closed.
- **G4** the pre-persist redaction flag was computed in two places and read nowhere; the
  staged document a reviewer reads may not be the model's output. Carried on the result,
  logged by the drain.
- **G2** `bool(data.get("pause"))` and `!!o.pause` — the F10 coercion bug's third and fourth
  copy, on the switch that freezes autonomy: the string `"false"` paused it. Both now defer
  to `api/config.py`'s vetted coercion.
- **F4** the tool's single-flight probe swallowed its exception and failed OPEN while the
  route defers on the same fault — and the permissive one is the path an agent drives
  automatically.
- **F5** the drain created `usr/skills/<name>/` before the validation gate; the same line
  v1.8.48 removed from the tool.
- **F10** a post-adopt **rollback** was audited and logged as `adopted`, so the trail
  answered "yes" to "is the live skill the one the gate approved?". Now
  `adopted_rolled_back`, with every consumer testing a prefix.
- **G9** the grace window had a ceiling but no floor: a future `started_ts` yields a
  negative age, which satisfied `<= GRACE` for as long as the skew lasts.

### Left alone, on purpose

**F1/F2** (stage 0.5 is unreachable, and would run *instead of* the whole-file gate) and
**F3** (the three adopt paths pass different `official_gated` values) are verified real,
but fixing them blind would either enable a rail with known gaps or make the route *skip*
a gate on provenance it has not verified. Both are gate-design changes; the reasoning is
in `AGENTS.md` so a later pass does not undo the decision.

### Verification

354/354 green; **43 mutations, 0 survivors**. Three new cases here were decorative first
and are worth naming: comparing a mkdir against the *governance* gate (which runs earlier,
so a mkdir in between passed), a substring assertion satisfied by the corrected comment
that quotes the old expression, and a `Pass`-in-handler check that only fired when the
handler contained *nothing else*. Plus one harness lesson: a byte-length-identical
mutation can be masked by a stale `.pyc`, so the mutation runner clears `__pycache__`
before every run.

---

## [1.8.49] - 2026-10-05

Fourth-pass remediation: a **regression audit of v1.8.48**. One auditor was given the full
change list plus ten targeted questions about the new code and asked to find what the
previous release broke or left behind. **7 findings, all fixed.** Suite 339 -> **347**.

The first three audit passes found silent no-ops. This one found something sharper: **a fix
that inverted its own intent**, and a **recovery path that could destroy the thing it
recovers**. Eight of the auditor's ten questions came back clean — the new locks have no
lock-order inversion and no re-entry, `adopt_write_skill`'s gate covers every branch,
`_version_of`'s four callers all pass a shape it can parse.

### Critical: `/rollback` could empty a live skill (X1)

`restore_default_snapshot` restores whichever `_default.pre_adopt.md` it finds. For a skill
the plugin created from nothing, that file is legitimately **0 bytes** — which is the state
`adopt_write_skill` itself produces (`current = ""` when the live file does not exist). A
0-byte file *is* a file, so it shadowed every real `_default.vN.md`: the operator clicked
`rollback(skill)`, got `{"ok": true, "bytes": 0}`, and the live skill was emptied. The
route then logged a successful `rolled_back` cycle entry.

**16 such snapshots already existed on this install.** The refusal now lives in the choke
point (a blank document never reaches a live file, whatever the caller), with
`restore_default_snapshot` also treating a 0-byte pre_adopt as absent.

### High: the interval rail got MORE permissive, because a docstring lied (X2/X3)

v1.8.48 fixed the `min_interval_seconds` rail to search every log generation, iterating
"newest first". The helper it calls, `_log_generations()`, has documented **"oldest first"**
since it was written — while returning `[base, .1]`, where `base` is the NEWEST generation
(rotation moves the old content into `.1` and replaces the live file with an empty one). So
`reversed(...)` walked the oldest first and returned the **oldest** eligible decision. An
older `last_ts` makes `(now - last_ts)` *larger*, so the rail waved through exactly the
window it exists to close.

The fix is not clever, it is verified: the helper now returns the order it always claimed,
and the test asserts the chosen row **arithmetically** — the newest decision is inside a
1 h interval and the oldest is outside it, so the choice demonstrably changes the answer.
The same root cause made the dashboard's `last_decisions` stale for one rotation window per
skill.

### Medium: the new single-flight rule could defer adoption forever (X4)

v1.8.48 made an unrecorded pid mean "in flight", closing the spawn window between writing
the placeholder sidecar and `Popen` returning. Correct — and unbounded. `Popen` returns a pid
without validating the target, so a worker that dies instantly (import error, renamed script)
never rewrites its sidecar and leaves `status: pending, pid: 0` in place. Nothing else
removes it: the orphan reaper requires the proposal to be gone first, and the harvest timeout
is autonomous-only. The scan is directory-wide, so in **manual** mode one such sidecar
deferred **every adopt for every skill, forever** — the F8 "defers forever" shape in a new
place. Bounded now by `PENDING_PID_GRACE_S`, and an unknown *age* counts as stale rather than
as evidence of a running worker.

### The rest, briefly

- **X5** `allowed_fragments` was silently skipped on every whole-file skill: `read_fragments`
  yields only the implicit `_default`, and the old `if _named:` turned the operator's
  strongest scope constraint into a no-op. It defers now, and the refusal says how to opt out
  — a rail that can only ever say no gets removed.
- **X6** `_record_error` copied `_save_state`'s atomic shape but not its guarded `fsync`, so
  an fsync failure discarded the newest error record and left an older one rendering.
- **X7** `cadence`'s module docstring still promised the v1.1.0 migration three lines above
  the note that removed it.

### Verification

347/347 green; 33 mutations, 0 survivors. Two new cases here were decorative on the first
draft and are worth recording: `fsync_guards` was satisfied by the function's *own* outer
`try` (the fix must be checked by walking to the **nearest enclosing handler**), and one
assertion sat behind `if live_file.is_file():`, which on this machine asserted nothing. Both
now use AST parent-walks and temp-tree fixtures.

---

Third-pass audit remediation, **Phase 7**. A third independent audit over the whole plugin
at v1.8.47 — six auditors on disjoint scopes, every candidate verified against the quoted
lines. **25 findings, all fixed.** Suite 306 -> **339**.

The common shape is unchanged from the first two passes and, if anything, sharper: again
**every finding was a component not doing what the code around it assumed**. But this pass
found two things the earlier ones did not — a **guard that was structurally incapable of
firing**, and **eight test bodies that had silently stopped running**.

### Critical: the "refuse when the snapshot fails" guard could never fire (T1)

`adopt_write_skill` wrapped `fragment_store.snapshot_default` in `try/except Exception`
and reported `"snapshotted": True`. But `snapshot_default` is documented **never raises** —
it returns `{ok: False, error}`. So the guard could not fire, the result was discarded, and
a snapshot that failed for a mundane reason (read-only mount, ENOSPC, permissions) fell
straight through to the live write and announced success. That is the unrecoverable case:
`/rollback` would have had no target. It now tests the result, and refuses.

The reason this survived two prior audits is worth recording: the guard *looked* correct.
An audit that asks "is there a try around the snapshot?" gets the wrong answer, and the
existing test stubbed `adopt_write_skill` itself — the function the defect lives in.

### Critical: the rollback wrote a live `SKILL.md` with `Path.write_text` (T2)

`restore_default_snapshot` wrote the live skill directly, named in the contract as the
v1.8.24 P0 defect that must not reappear on any path, and discarded the result of its own
reversibility snapshot. It now delegates to `adopt_write_skill`, so adopt and rollback
share one choke point. Alongside it: that function imported `sleep_runner` namespace-only
with no fallback (G11), so `/rollback` returned `{ok: False}` from its catch-all outside
the A0 runtime — a recovery path that silently did nothing.

### High: informed approval could not tell a rejected replay from an accepted one (T4)

`proposal_view` read `accepted` and the judged-pair count off the **top level** of the
real-gate sidecar, while the only writer nests them under `verdict`. Both values were
`None`, forever — so both approval surfaces rendered an accepted replay identically to a
rejected one. The defect lived precisely where this plugin's evidence model is supposed to
be strongest.

### High: a `NameError` could escape the engine runner (T5)

`official_adapter` called `logging.getLogger` inside a handler for a *non-fatal* redaction
fault, without importing `logging`. The `NameError` propagated out of
`run_official_sleep_cycle`, skipping the gate marker, the summary and — in the drain — the
whole tick. Asserted from now on by an AST check, which no comment can satisfy.

### High: one malformed policy value disarmed two rails (T9)

The three policy-scope constraints shared a single `try`. A non-numeric
`max_verbosity_delta_ratio: "0.1x"` — copied verbatim from the operator's policy file —
raised, the handler logged at DEBUG, and `forbid_patterns` **and** `allowed_fragments` never
ran. The two strongest operator constraints, silently inert, on a path to a live
`SKILL.md`. Each constraint now evaluates independently and each refusal names the key that
failed.

### High: eight test bodies had silently stopped running (T8)

A scripted edit once consumed eight `@test` decorators. The functions stayed defined, the
file compiled, the suite stayed green — and eight assertions stopped running, including
`drain_suggestions` and `cadence.list_skills_with_state`, which have no other coverage. Two
names were registered twice, so a label could not be traced to its body. `Local Contracts`
now requires every `t_*` definition to be registered exactly once, and the suite asserts it
of itself.

Reviving one of the eight immediately surfaced a stale 1970-era fixture, and the mutation
check found that **five of the 26 new cases were decorative on their first draft** — they
asserted on a helper the readers did not use, on a function's *source* where the
**docstring quotes the fix**, and on a substring the *comment* also contains. All five were
rewritten with AST-based structural assertions. That is the standing lesson of this file,
now confirmed a third time: a test that reimplements or paraphrases the logic under test is
worse than no test, because it is green while the code is broken.

### The rest, briefly

- **T3** the harvest path's redaction record sat behind a `return` and was duplicated three
  times, so the path an unattended install takes most often recorded nothing. **G4** and
  that record are a pair: `redaction-notice` was missing from `MACHINE_DECIDED_BY`, so the
  drain's own machine row satisfied the human-approval gate.
- **T6/T7** an unrecorded pid now reads as in flight (the window between the pending
  sidecar and `Popen` was one in which single-flight reported "no gate"), and the
  pid-recording write checks its result.
- **G1/G2/G3/P3** two inert settings, three lexicographic snapshot readers (one of which
  overwrote a live rollback target), and two unlocked whole-file rewrites. Atomic-replace
  is not a substitute for mutual exclusion.
- **G5/G7/G6** two governance readers opened the live log generation alone, so every 5 MB
  rotation blanked the dashboard and briefly disarmed the `min_interval_seconds` rail.
- **P1/P2** the inner-loop no-op privacy guard could never fire, and `judge_model` was
  declared but unread — every label ran on the chat slot while the operator's setting did
  nothing.
- **U3-U10** an inert settings control, three callers that discarded the writer's result,
  a `!!` that re-introduced an already-fixed coercion bug one layer below the route that
  fixes it, and two `or <default>` collapses that turned "stop unattended writes" back into
  the default.
- **C4** the real-gate worker declared `--tasks-file` required by its own contract and never
  read it, so an unreadable sidecar produced the documented fail-OPEN adopt with the
  correct path sitting in argv.
- **A2/A4/A8/A10/A11, G10, P7, U5** an unpersisted state, an unrestarted inner loop, a
  cadence module that unlinked the auto-loop's live state file, a reload lever two
  docstrings promised and the code did not have, and a few more.

### Verification

339/339 green. All 25 fixes mutation-verified: each defect reintroduced individually makes
the suite fail. Run without touching a live install by copying the tree to a scratch
project root (`usr/plugins/skillopt` + `webui` + `usr/.env`); see `AGENTS.md` Verification
for why two cases depend on that root.

---

## [1.8.47] - 2026-10-04

Second-audit remediation, **Phase 6f: the last eight findings (R25-R32)**. With these,
**the second audit is fully closed: all 32 fixed**, and both audits together account for
**60**. Suite 204 -> **306**.

Individually these were the smallest findings of the two passes. Together they are why a
reader of this plugin could not trust what the comments said - which is its own kind of
defect.

### Fixed: the route carried a SECOND decision reader (R25)

`api/governance_status.py` had its own hand-rolled ledger reader that disagreed with the
gate in three ways simultaneously: it read the **live log only** (blind at the 5 MB
rotation while `_human_approved` still saw the approval), applied **no**
`MACHINE_DECIDED_BY` filter (an `autonomous` row reached the operator labelled as a human
decision), and took the **last line** rather than newest-by-timestamp. Two readers of one
ledger is precisely how R8 happened, so the route now delegates to
`governance._last_human_decision` and reads no ledger of its own.

### Fixed: the "never raises" sanitizer removed nothing when it failed (R26)

`sanitize_rollout_records` satisfied its never-raises contract by appending the **RAW
record** when `sanitize_rollout` raised - persisting exactly what the function exists to
remove. The fallback is now a **redacted shell**: field names and list positions survive
(the held-out gate compares pairs by index and reads fields by name), every free-text value
is dropped, and numeric counters are kept because they carry no text.

### Fixed: the A/B judge's HTTP egress posted rollout text raw (R29)

`_llm_judge_via_http` sent `task` + `last_response` unsanitised, which made
`inner_loop`'s claim that *"every other provider boundary sanitizes and fails closed"*
false. It now sanitizes, and **refuses** the call when the privacy helper is unavailable
rather than sending raw text. Dormant either way - but dormant by configuration, not by
design.

### Fixed: the training tool could run twice and leaked a handle (R27)

`skillopt_train kind=run` spawns an hours-long detached process. It recorded no state, so
nothing stopped a second concurrent run (both writing the same artifacts), and it kept its
copy of the log handle open forever - one leaked descriptor per run, and on Windows an
open handle also blocks rotation of that log. Now single-flight by **pid liveness**
(`sleep_runner.is_running`, not `os.kill(pid, 0)`, which is Ctrl+C on Windows), with the
handle closed after the spawn and the pid recorded atomically.

### Fixed: the bridge index was written non-atomically (R28)

A torn `.bridge_index.json` is swallowed by its reader into an **empty** index, after which
every rollout looks unprocessed and duplicate rows are appended to `history.jsonl` -
duplication no reader can distinguish from real history. Now temp-write + `os.replace`.

### Fixed: the record of the run that did NOT execute was discarded (R31)

`run_sleep_cycle.py`'s `backend_guard` refusal is precisely the line that must not be
lost - and it emitted while `_LOG_PATH` was still `None`, so `emit()` wrote nothing. The
path is now bound **before** the guard.

### Fixed: a fallback that claimed a safety property it did not have (R32)

`sleep_runner._deep_copy`'s docstring said returning the original object "is strictly
safer than returning a shared structure". It **is** a shared structure, so on a deepcopy
failure a caller mutating the result would corrupt the cached config parse for the life of
the process. The fallback now rebuilds dicts, lists and tuples and shares only
immutables, which cannot be mutated in place.

### Fixed: the dashboard documented a contract that had changed (R30)

`skillopt-dashboard.js` still described `/reject` as *"audit only; does not delete the
staged file"* - stale since v1.8.36, when a rejection began quarantining the proposal. A
maintainer trusting that comment would have reintroduced F2. It now states where the
document goes, when the behaviour changed, and that a partial failure reports `ok:false`.

### Tests

298 -> 306, eight new cases. **All 8 mutations detected.**

Three of my own tests needed tightening first, and all three failed the same way - they
asserted a string *exists* rather than that a guard still *gates*:

- R27 survived `if prev_pid:` -> `if False:` because three separate substring checks all
  still passed.
- R32's test asserted the false docstring claim was absent, but the corrected
  docstring **quotes** it while explaining the bug - a substring check cannot tell a claim
  from its refutation.
- R29's "privacy unavailable" simulation failed twice because the smoke harness mirrors
  `usr.plugins.skillopt.helpers.*` onto the bare namespace, so the import resolves
  through a **package attribute** and never consults `sys.modules`. Removing the attribute
  was required, not just setting `sys.modules[name] = None`.

Every one of those would have shipped the regression, and none was visible by reading.

---
## [1.8.46] - 2026-10-04

Second-audit remediation, **Phase 6e: privacy completion**. Fixes **R13, R14, R15, R16,
R17, R24**. The last six, and the only ones with a confidentiality dimension. All six were
the same shape: **a control that looked present and did not.**

### Fixed: the inner-loop egress sanitized a key the payload does not have (R14)

`_llm_suggest_via_http` called `sanitize_rollout_records(payload["rollouts"])` — but
`_build_rollout_payload` emits `{task, trajectory, last_response, ...}` and has **no
`rollouts` key**. So the call received `None`, its result was discarded, and **nothing was
sanitized**; `trajectory` is a *list*, which the per-field `isinstance(v, str)` filter
skipped anyway. The endpoint is one config value from being live, so enabling it would
have started shipping credentials. Both payload shapes are now handled by
`_sanitize_payload`, and a **no-op guard** refuses the request when sanitisation provably
changed nothing on a payload that held a credential shape — the silent no-op was the
failure, so it must now be loud.

That guard first tried to re-probe the *output* for a credential shape and matched its own
`[REDACTED]` marker (`API_KEY=[REDACTED]`), refusing to send whenever a task merely
mentioned a key. It now compares the body to the payload instead, which detects the no-op
without false positives.

### Fixed: model output is now redacted BEFORE it is persisted (R16)

Redaction happened only at the live write, so the **unredacted** bytes were already in
`staging/<skill>.md`, copied verbatim into `staging/adopted/*.md`, and embedded in the
summary sidecar's diff — the live-write redaction never removed them. Both staging writers
(`direct_optimizer`, `official_adapter`) now redact before writing, and the copy is
annotated so a redaction failure cannot be reported as a copy failure.

### Fixed: `adopt_write_skill`'s `redacted` flag was read by nobody (R15)

The writer computed it, its own docstring claimed *"the audit row can record it"*, and no
caller looked. A live skill altered by redaction left no trace in any ledger. The drain
now logs it, records `output_redacted` on the audit row, and writes a
`decided_by="redaction-notice"` ledger row; the HTTP route surfaces `redacted`.

### Fixed: the real-gate tasks file could be written unsanitized, silently (R13)

An unimportable privacy helper, or a sanitisation that raised, logged and **continued** —
writing raw rollout records to `logs/runs/*.tasks.json`, which the worker then re-reads
into a live agent context. Both failure paths now **skip the real gate** rather than
persist unsanitised records, matching `bridge.py`, which raises. The misleading comment
("sanitisation only rewrites text fields") is corrected: at the shipped defaults it
**pops** each trajectory step's `args`/`result`, so the frozen records are structurally
lighter than the source rollouts.

### Fixed: index compaction could drop a concurrent append (R17)

`_compact_approvals_index` did read → rewrite → `os.replace` with no lock while three
writers append to that file (two routes + the drain thread). A row appended between the
read and the replace was **dropped silently** — the exact failure M2 was filed for,
reintroduced in the file created to fix it. Append and compaction now share one
`_APPROVALS_INDEX_LOCK`, and the compaction temp file is cleaned up on failure.

### Fixed: `setup_env` destroyed an unmatched plaintext credential (R24)

`_provenance_ref` fell back to `${KEY}` when no visible source matched, which resolves to
nothing. A legacy `.skillopt-env` holding the only copy of a **working** credential was
rewritten into a dangling reference, the credential was destroyed, and the next LLM call
failed with *"No LLM API key found"*. There is no self-reference fallback now: an
unmatched plaintext value is **preserved** and reported as `unresolved`. A matched source
still converts as before.

**This deliberately narrows a v1.8.5 security assertion**, and the trade is explicit: the
property that matters is not "no plaintext on disk" (the plugin did not put it there — a
legacy hand-edit did) but **"the plugin does not break a working credential"**. Rewriting
it to a dangling reference is not the plugin's call to destroy a secret it cannot
re-create.

### Tests

292 → 298. **7 of 9 mutations detected**; the 2 skipped failed on the *mutation
script's* anchors, not on the tests.

Mutation testing earned its place twice more. The R13 test asserted a substring existed,
but there are two skip branches and removing one left the other's text — and my first
fix searched the whole remainder, so it found the other branch's wording. It is now a
**bounded** window. The R16 test asserted `redact_text(` appeared before the staging write,
which survives `if False:`; it now pins the guard and the call as one sequence. Both were
loose tests that would have shipped the regression, and neither was visible by reading.

---

## [1.8.45] - 2026-10-04

Second-audit remediation, **Phase 6d: ledger and state consistency**. Fixes **R8-R12, R18,
R20, R21, R22**. Nine findings, seven modules, one theme: **state that disagrees with
itself.** A budget recorded against one cap and checked against another; a card that knew
an approval the gate could not see; an audit row asserting a passing adoption for a write
that never happened; a rollout retired because nobody asked the question.

### Fixed: the proposal-decision map read only the live log (R8)

After the 5 MB rotation this is exactly the card/gate disagreement `Local Contracts`
warns about: `_human_approved` (which reads the index and the generations) knew an
approval existed while `find_pending_proposal` reported the proposal `"pending"`, so the
Governance card re-prompted for a change already decided. It also called `_runs_dir()` —
which ends in a `mkdir` — *outside* its `try`, contradicting its own "Never raises"
(the F8 pattern, third occurrence). It now reuses `_iter_governance_rows()` and
`_read_approvals_index()`, so the two readers see the same history.

### Fixed: the ledger was re-read once per governed skill (R20)

`find_pending_proposal` called the scan per skill and the scan read the whole ledger, so
the dashboard was **O(skills x ledger)** per refresh — 21 full reads at 21 decision rows,
growing with install age. Memoised with a 2 s TTL plus a generation counter bumped on
every append, so N lookups cost one scan while a decision made a moment ago is still
visible immediately. Measured: **25 lookups -> 1 scan**, and a write forces a re-scan.

### Fixed: a budget recorded against one cap and checked against another (R9, R10)

`governance`'s budget check called the factory with **no `cfg`**, so `budget.enabled` was
inert there and `reset_hour_utc` fell back to the module default (00:00) while the drain
used the operator's value — two different daily windows for one skill, plus an HTTP thread
becoming a reader/writer of the budget state file.

And a **tenth** bare `BudgetTracker(...)` had survived at the real-gate spend site, so the
real gate accumulated against the module default while the drain checked the operator's —
the exact regression the v1.8.37 F13 fix removed from nine sites, reappearing where the
fix did not look. Three further sites built one directly for status reads when the gate was
disabled; `tracker_from_config(..., default_when_disabled=True)` now covers those too, so
**"only the factory constructs a tracker" is literally true** and an assertion enforces it.

### Fixed: the audit trail claimed adoptions that never happened (R11)

The drain wrote `passed: <gate verdict>` **before** the write, and every later refusal —
failed snapshot, ungated-direct refusal, autonomous deferral — returned without correcting
it. So the trail asserted a *passing adoption* for a write that never landed.
`api/adopt.py` already wrote a correction row; the drain did not.

Every exit now goes through one `_audit(outcome)` writer, recording
`outcome` ∈ `proposed | adopted | rejected | refused | deferred | ungated_blocked`, with
the gate's own verdict kept in a separate `gate_passed` key. The row template carries
**no** `passed` key at all, because `_audit` is the only writer and always overwrites it —
a dead field there would become a live false statement the moment that overwrite was
removed. Mutation testing found this twice before it was pinned.

### Fixed: the agent adopt tool discarded its `skill` argument (R12)

`skillopt_sleep verb=adopt skill="foo"` called `_adopt()` with no argument and adopted
`staged[0]` — silently adopting a **different** skill than the one named. Audit M5 fixed
exactly this on the HTTP route; the agent path is the copy where no human is reading a
diff. It now resolves by stem equality and **errors** on no match, matching
`api/adopt.py`.

### Fixed: `/reject` reported success over two silent partial failures (R18)

If the quarantine failed **and** the ledger write failed, the document stayed queued with no
rail armed — and the route returned `ok: true`. Same defect F10 fixed for the opt-in marker.
It now reports the combined outcome and names what is still true ("the proposal was NOT
moved out of the queue", "the rejection rail is inert"). A half-done reject is not a reject.

### Fixed: a transport error retired a rollout forever (R21)

`inner_loop` treated an unreachable endpoint as a completed judgement: it enqueued an
**empty** suggestion, and an empty suggestion is exactly how "the model had no useful
idea" is represented. So the rollout was marked done and its contribution to the training
signal was **permanently** retired — with no retry and no counter distinguishing the two
cases. Errors now consume the shared `_bump_skip` budget and are retried on a later tick;
a genuine decline still enqueues a no-op and still retires. An endpoint outage now costs
retries, not data.

### Fixed: a failed reward-model load was retried on every finished chat (R22)

`_load_attempted` was assigned in three places and read **nowhere**, and it was set only on
the *last* line of `_try_load_model` — so both failure paths returned without recording the
attempt. `_ensure_model_loaded()` therefore re-ran the full checkpoint load for every
finished chat when the directory was present but unloadable, holding `_lock` on the
harvester's latency path. The flag is now read as a retry guard and set **before** any exit.

### Tests

284 → 292, eight new cases. **All 11 mutations detected**, including the two that only
exposed themselves after a *second* pass: an assertion on `rows[-1]` alone could not see a
provisional row that a later row corrected, and a text search flagged the docstring
*describing* the `_runs_dir` bug as the bug (the same trap as `OpenAI(` appearing in the
R5 comment). Both are now AST- or all-rows-based.

## [1.8.44] - 2026-10-04

Second-audit remediation, **Phase 6c: the LLM boundary**. Fixes **R5, R6, R7, R19,
R23**. All five are the same shape: *the call we make to a provider does not do what the
code around it assumes.*

### Fixed: one unresponsive endpoint could hold the judge queue for ~2 hours (R5)

`direct_optimizer._call_llm` built its client as `OpenAI(base_url=..., api_key=...)`,
inheriting the SDK's **600 s read timeout and 2 internal retries**. That call runs inside
`llm_judge._judge_llm_call`, which holds `BoundedSemaphore(1)` — so one non-responding
endpoint stopped every other judge caller. `judge_client.judge_rollouts` is a synchronous
loop called inline by `run_sleep_cycle.py`, so a single cycle could hang for the entire
backlog.

Now bounded by `llm_call_timeout_s` (new, default 60; also `SKILLOPT_LLM_TIMEOUT_S`;
clamped to `(0, 600]` because a non-positive timeout means "block forever" to httpx).
`max_retries=0` **on purpose** — `llm_judge` owns the retry policy and owns it better
(pacing-aware, bounded, 429-aware); the SDK retrying underneath it produced un-paced
nested attempts, which is the 429 storm the burst-protection layers exist to prevent.
`ping_judge` already passed a timeout and `max_retries=1`; this was the production call it
claims to mirror exactly.

### Fixed: the judge's escalation ladder could not run (R6)

`judge_client.judge_rollout` only escalates when the first call produced no label, and it
reads `base.get("model")` to decide **whom** to re-ask with a bigger token budget. Neither
of `judge_outcome`'s two failure shapes carried `model`, so `judge_name` was always `''`,
the ladder collapsed to `[('', 6000)]`, and every rollout that failed because the judge
exhausted `max_tokens=300` on hidden tokens was re-asked of the **chat-slot model** — the
most expensive model in the preset — with the label's provenance misreporting which model
actually judged it. Both failure shapes now name the model that was tried.

Separately, the escalation ladder called `direct_optimizer._call_llm` **directly**,
skipping the in-flight limiter, the `SKILLOPT_JUDGE_THROTTLE_S` pacing and the retry
policy — while `judge_rollouts` looped it over up to `limit` rollouts on one thread. An
un-paced 429 storm arriving by the path meant to *rescue* a failed labelling. The
escalation now routes through `llm_judge.judge_call`, and the first attempt and the
escalated attempts share one guarded path (`_guarded_call`) so they cannot drift.

### Fixed: the A/B harness scored both arms identically (R7)

`_replay_under_skill` preferred `rollout["fragments_active_text"]` over its `skill_text`
**argument** — and `run_ab` passes `current_text` and `proposed_text` as that argument.
The harvester sets that field for any skill without a `fragments:` list, and
`read_fragments` returns an implicit `_default` fragment whose text is the **whole
`SKILL.md`** for exactly those skills.

So for essentially every rollout of every ordinary skill both arms received byte-identical
input, `score_a == score_b`, the judge returned `tie` at confidence 0.5, `lift_pp` was
`0.0`, and `validate_proposal` took the `ab_harness_rejected` branch with a reason string
that reads like a real measurement. **Enabling `ab_harness_enabled` blocked ALL skill
evolution.**

The arm's own text is now authoritative, with the fragment snapshot kept as the fallback
for an arm that has none (a rollout captured before the skill existed). A harness that
scores both arms identically is not a conservative harness — it is a broken one. Worth
noting: the judge already scaled confidence with the score gap, so once the arms genuinely
differ the default judge can clear `min_confidence`; the flat `0.5` was only ever the tie
case.

### Fixed: the intent refinement's documented timeout did not exist (R19)

`refine_intent_with_llm` accepted a `timeout_s` that nothing read, and `hook(prompt)` is a
synchronous call into an HTTP helper — so a hanging hook hung the **staging write path**
indefinitely. The hook now runs on a worker thread with a hard deadline; an over-running
hook is abandoned (a daemon thread cannot be cancelled) and the heuristic stands.
Abandoned hooks are capped by a module-level slot limit so they cannot accumulate.
`intent_llm_timeout_s` is also now **declared** in `default_config.yaml` — it was read by
the code but unsettable, which is how the name-grep reader test could not see it.

### Fixed: `calibrate_judge` reported on the stub while claiming the judge (R23)

The docstring said *"Uses the production LLM judge by default"* while **both** no-flag
branches returned `ab_harness._default_judge_fn` — the deterministic keyword stub. So
`python scripts/calibrate_judge.py` printed `OK: judge is calibrated (kappa …)` about the
**stub**, with nothing to tell an operator otherwise.

Three fixes: `_make_judge` now returns `(callable, description)` and the report names the
judge under test, with a stub run labelled `HARNESS OK` and stating plainly that nothing
was measured about the production judge; **arm A is now sent** (`human_label` is a
comparison *between* the arms, and the judge only ever saw B); and the scores are derived
from the response lengths instead of being a constant `+0.2`, which had made
`score_b - score_a` always positive so the stub answered `win` for **every** pair and the
reported kappa was a function of the human-label distribution alone. Offline-by-default is
a legitimate choice — it just has to be visible.

### Tests

277 → 284. **All five mutations detected** — reintroducing each defect individually makes
the suite fail:

| Mutation | Caught by |
| --- | --- |
| no timeout on the client | *"constructed without a timeout"* |
| `model` dropped from the judge exception path | *"the exception path does not carry `model`"* |
| fragment snapshot overrides arm text | *"the proposed arm was scored on …CURRENT-ARM"* |
| hook called inline (timeout unused) | *"a hanging hook blocked for 30.0s despite timeout_s=1"* |
| arm A dropped from the calibration payload | *"arm A is still never sent to the judge"* |

Two of these were caught only because the mutation harness exists: the R7 and R23 tests
initially matched the wrong thing — an `OpenAI(` inside the very comment explaining the
fix, and a branch slice that cut off before the return it was asserting on. Both were
test defects, not product defects, and both are recorded as the kind of thing a negative
test gets wrong when nobody checks it against a real failure.

## [1.8.43] - 2026-10-04

Second-audit remediation, **Phase 6b: stale authorisation decisions**. Fixes **R3** and
**R4**. Both are the same class as R2 — an **absent or stale answer read permissively**
on the path to a live file.

### Fixed: a mode write was invisible for up to 5 seconds (R3)

`sleep_runner._framework_registry_config()` memoises the framework plugin-config read for
5 s. That TTL is safe for reads and unsafe for writes, because `plugins.clear_plugin_cache()`
drops the **framework's** cache, not **this plugin's** memo. Two consequences:

- `POST /mode` builds its response from the config it just wrote and passes it to
  `resolve_evolution_mode(cfg)` — which prefers the registry whenever the registry yields
  anything. So the response reported the **pre-write** mode, and `webui/config.html`
  rendered that stale value straight after the operator toggled it.
- Worse, in the **authorisation** direction: the drain reads the mode through the same
  memo. After the operator switched **autonomy off**, the drain could keep resolving
  `autonomous` for up to the TTL and write a live `SKILL.md`. A stale permissive answer on
  a decision that authorises writes.

`sleep_runner.invalidate_framework_config_cache()` is new, and **both** write paths
(`api/mode.py` and `api/config.py`) now call it alongside `clear_plugin_cache`. The
remaining window is an external hand-edit of `config.json`, which is bounded by the same
TTL — and `.autonomous_pause` remains the immediate, cache-independent lever, because it is
a file-existence check.

### Fixed: an unreadable ledger silently DISARMED the rejection rail (R4)

`_human_rejected_current_proposal` ended `except Exception: return True  # Undecidable =>
restrictive`, but both of its helpers swallowed every exception and returned `None`, so
nothing inside the rail's `try` could raise — **the restrictive branch was dead code**.
"No decision recorded" and "the ledger could not be read" were the same value, and the rail
read both as "nothing to enforce". An unreadable `approvals_index.jsonl` therefore
disarmed the human-rejection rail, and the drain adopted a document a human had
explicitly refused — with no log line and no reason string.

Readers are now **tri-state**: `_last_human_decision`, `_read_approvals_index` and
`_newest_staged_mtime` return a module-level `_UNREADABLE` sentinel when they cannot
evaluate, distinct from `None`/`[]` meaning genuinely absent. `_UNREADABLE` is
restrictive; absence is not. The tri-state exists precisely so the two are separable: a
fresh install has no ledger at all, and if that read as a fault **every** skill would be
permanently blocked and evolution could never start. Both directions are asserted.

A generator cannot return a value, so `_iter_governance_rows` reports a read fault through
a module flag (`_LOG_READ_FAILED`) that `_last_human_decision` reads and resets.

### Tests: mutation testing found two decorative tests

Both R4 tests initially **stubbed `_read_approvals_index` to return `_UNREADABLE`** — the
function under test — so restoring the buggy conflation left the suite green. Rewritten to
drive the **real** readers through a real `builtins.open` / `Path.is_file` failure.

A second mutation pass found the coverage was still incomplete: the row-reading `except`
was exercised but the `is_file()` guard's `except` was not, so reverting *that* branch to
`return []` also left the suite green. It now needs an odd filesystem state to reach, but
R4's claim is that **no** unevaluable branch may read permissively, so it is asserted.

**Both mutations are now detected.** (R3's mutation — dropping the memo invalidation —
was detected on the first attempt.)

A third `@test` decorator was lost to a bulk edit during this work: the function was
defined but never registered, so the one test that drove the row-reading `except` did not
run. That is the **third** time a scripted replacement ate a decorator in this file. The
rule is now in `AGENTS.md`: *never rewrite a test block by script without asserting the
registered count.*

### Tests

272 → 277.

## [1.8.42] - 2026-10-04

Second-audit remediation, **Phase 6a (continued): the drain's own pre-write gate**. Fixes
**R2**. Evidence in `AGENTS.md` → *Open findings register — RE-AUDIT*; phasing in
`REMEDIATION_ROADMAP.md` → Phase 6a.

### Fixed: the drain's only pre-write governance gate still failed OPEN (R2)

`auto_loop._adopt_one` logged `"governance: … check failed; falling through"` and
continued into `validate_proposal` → `adopt_write_skill` → a live
`usr/skills/<n>/SKILL.md`. `_auto_adopt` calls `_adopt_one` **directly** and never routes
through `_skill_eligible_for_cycle`, so this was the *only* governance evaluation on the
drain's path to a live file. A raising check — an unreadable ledger, a `mkdir` on a full
disk, an ImportError — therefore disabled optout, immutable, pause, cadence, budget **and**
the human-rejection rail for that proposal, and the drain promoted a live skill for a skill
the operator had opted out of.

v1.8.35's F8 fix corrected the **sibling** gate (`_skill_eligible_for_cycle`) and the F8
test drove **that sibling**, so the fix looked complete and was not. This is the same shape
as R1, one function along: *a partial fix, covered at the wrong site.*

`_adopt_one` now fails **closed** and returns `"skipped"` — the correct outcome rather than
a new one, because a governance that cannot be evaluated is a **deferral**: the proposal
STAYS in staging and may become eligible next tick. It is not a rejection and must never be
a write. The deferral is both logged with `DEFER` and passed to `_record_error`, so the
dashboard shows it; `_record_error` is called as a **module-level** function, because the
v1.8.35 fix used `self.` and would have raised `AttributeError` inside its own except
handler.

### Tests: the coverage goes where the code is

The R2 tests drive **`_adopt_one` itself**. A test of the sibling would have kept passing
after R2 was reintroduced — which is exactly what happened for a release. They stub
`validate_proposal` so a refusal can only come from governance, assert the negative that
matters (**no `adopt_write_skill` call**), and include a control case proving the same
function *does* write when governance permits, so "always defer" cannot pass either.

**Verified load-bearing by mutation:** restoring `pass  # fall through` makes the suite fail
with *"_adopt_one wrote ['r2skill'] even though the governance check RAISED"*.

A structural guard (`t_v1842_all_write_paths_are_gated`) now asserts, **per function**, that
everything reaching `adopt_write_skill` has a governance evaluation ahead of it — with two
documented exemptions, each carrying its reason: `_harvest_real_gate` (governance ran at
spawn time; the verdict sidecar is the record) and `adopt_write_skill` itself (the sanctioned
low-level writer, correctly ungated — which is precisely why every *caller* must gate).

That check had to be written carefully to be worth anything: it strips each function's
docstring first, because several functions *mention* `adopt_write_skill()` in prose and an
earlier draft matched the prose, reporting a false positive on the agent tool.

`allow_ungated_direct_adopt` is also noted: `_adopt_one` refuses an ungated direct adoption
unless the operator opts in (v1.8.24 P1.6), which is a separate correct refusal — the R2
control case opts in explicitly so the only gate under test is governance.

### Tests

268 → 272.

## [1.8.41] - 2026-10-04

Second-audit remediation, **Phase 6a: the authorisation path**. Fixes **R1**, the only
finding where one click by one person reaches a live file with every rail disabled.
`REMEDIATION_ROADMAP.md` → Phase 6; evidence in `AGENTS.md` → *Open findings register —
RE-AUDIT*.

### Fixed: the human Approve button bypassed every governance rail (R1)

`api/adopt.py` contained **zero** references to `governance`. It called
`validate_proposal`, `adopt_write_skill` and four helpers — but
`check_skill_eligible` is the *only* implementation of `.skillopt.optout`,
`mode: immutable`, `.skillopt.pause_until`, `min_interval_seconds`,
`daily_budget_cents` and the v1.8.36 rejection rail. Clicking **Approve** on the
Staged card therefore wrote a live `usr/skills/<n>/SKILL.md` for a skill the operator
had opted out of, paused or marked immutable — **or whose proposal a human had
explicitly rejected**, since `/governance_approve {approved:false}` records the denial
without quarantining, leaving the document staged and still adoptable.

This was the one surface with no agent and no second pair of eyes, which made it the
worst possible place for the gate to be missing.

The route now runs the chain before `validate_proposal`, with
`auto_adopt=resolve_evolution_mode()["autonomous"]` resolved by **no argument** so the
operator's persisted config is authoritative — byte-identical to the other two paths.
It fails **closed** in both directions: an unimportable governance module and a raising
check both refuse.

### Fixed: one gate, one implementation

R1's root cause was **two hand-written spellings** of the same gate. The agent tool got
a chain in v1.8.36; the route was never given one, and nothing ever diffed them. The
chain is now a single helper, `_check_governance()`, in `api/adopt.py`, and
`tools/skillopt_sleep.py` **delegates to it** — so the human path and the agent path
cannot drift apart again. A test asserts the delegation, because "they both call
`check_skill_eligible`" is not the same guarantee as "they call the same thing".

### Fixed: a false claim in the test suite

`t_v1836_all_adopt_paths_agree` was *named* "all adopt paths agree", its `_ok` line read
`"drain, route and tool all refuse what the rails forbid"`, and its comment said
*"The HTTP route refuses."* The body asserted only that a proposal was still staged —
under a comment conceding **"the route does not call governance"**. It passed because
the thin fixture failed `validate_proposal` first.

That comment was written during v1.8.36, when the missing gate was found and
rationalised instead of fixed. **A test whose assertions do not match its name is worse
than no test, because it manufactures confidence in a guarantee that does not exist** —
and it is how R1 survived a full remediation cycle plus a self-audit.

The test is rewritten to drive all three paths against the same rails, to stub
`validate_proposal` so a refusal can only come from governance, to assert the negative
that matters (**no live `SKILL.md` afterwards**), and to include a **control case**
proving the same route *does* adopt when governance permits — so "always refuse" cannot
pass either.

**Verified load-bearing by mutation:** disabling `_check_governance` makes it fail with
*"the ROUTE adopted a skill blocked by `opted_out_via_marker`*. The previous version
passed with the gate absent.

### Also in this release

- **`target.parent.mkdir` removed from the route.** `adopt_write_skill` already creates
  the parent, so doing it in the handler was redundant *and* a side effect ahead of the
  gate: a refused adopt left an empty `usr/skills/<name>/` behind.
- **The real-gate single-flight check no longer swallows.** It was a fail-*open*
  wrapper (`except Exception: pass`) around the check that prevents two writers for one
  proposal. A pending replay now defers with a reason; an unevaluable check refuses.
- **The gate's outcome is reported honestly.** `governance: "refused"` with the rail's
  reason is distinct from `governance: "unavailable"` for an unevaluable gate. The first
  draft conflated them and told the operator "unavailable" for an ordinary refusal.
- **Five route-mechanics tests now opt their fixture skill in** (`_adopt_opt_in`). They
  test proposal_id resolution, atomic writes, snapshot refusal and single-flight — none
  is a test about governance. Stubbing the gate out instead would have recreated the
  very failure being fixed.

### Tests

268 → 268 (one test rewritten and strengthened; six fixture opt-ins added).

## [1.8.40] - 2026-10-04

Self-audit of the audit remediation. No behavioural change to the plugin; this release
exists because the strongest criticism available after shipping five phases of fixes is
"but who checked the fixes?", and the answer was "the tests the same work wrote".

### The remediation applied to itself

The 2026-10-04 audit used four techniques, three of them **structural** rather than
grep-based. Re-running them against this work's own ~1,500 lines:

| Check | Result |
| --- | --- |
| Bare `helpers.*` imports with no namespace attempt (the F11 class) | 0 |
| Writes to `runs/` or a live `SKILL.md` without temp+replace (F3/F6) | 0 |
| `except: pass` immediately after a state write (the F8 class) | 0 |
| Config keys whose **name** is read but whose **value** never arrives (F13) | 0 defects, 1 blind spot |
| Tests that assert a literal they just wrote, or reimplement the logic | 1 of 59 |
| Existing contracts (markup, JS, route inventory, documented keys, versions) | 0 broken |

### Fixed: the F1 auto-loop test asserted on a COPY of the logic

`t_v1835_autoloop_resolves_mode_from_merged` reimplemented the drain gate
(`def canary(cfg, autonomous)`) and asserted on the transcription. That is exactly the
weakness that let F1 ship in the first place: the real gate was wrong while this test
passed, because the test never called it.

It now drives the **real** `AutoLoopThread._tick`, patching only its I/O edges
(`enforce_rollout_retention`, `list_rollouts`, `_log`) and observing whether
`_auto_adopt` was invoked — which *is* the resolved mode, because that call is the
gate's only effect at step 3. `_auto_adopt` is recorded rather than run, so no adoption
occurs: the assertion is about the decision, not the write.

### Fixed: the config-reader test asks the wrong question, permanently

`t_v1824_config_keys_have_readers` greps a key's NAME, which passes for a key that only
appears in a helper's own signature — precisely how `budget.daily_cap_cents` stayed
inert under a green suite. The audit said F13 "requires fixing the test, not just the
wiring"; this completes that.

`t_v1839_config_values_reach_consumers` asks the question that matters: set a
**distinctive** value and assert it **arrives**. Every setting added in v1.8.34-1.8.39
is covered, including the ones read through a loop (`budget` via a tuple, the
`governance.autonomous_*` rails via an `"autonomous_" + key` prefix) whose full dotted
name never appears in the source at all. A behavioural test also removes the accidental
coupling: the name-grep test passed for those rails only because `autonomous_config()`
happens to reuse the same short names in its internal dict.

### Tests

267 → 268. The self-audit also caught a tautology introduced while writing the new
test itself (`assert X is not None or True`), which is precisely the failure mode the
audit was looking for — and a dropped `@test` decorator that had silently unregistered a
test while the suite still reported green.

## [1.8.39] - 2026-10-04

Audit remediation, **Phase 5: privacy, resources, performance** — and the **close of the
audit**: all 28 findings from the 2026-10-04 read-only audit are now fixed, across
v1.8.35 - v1.8.39. See `REMEDIATION_ROADMAP.md` for the phasing and `AGENTS.md` for the
standing invariants each fix produced.

| Phase | Version | Findings | Theme |
| --- | --- | --- | --- |
| 1 | 1.8.35 | F1, F5-F9 | Safety rails and the autonomy contract |
| 2 | 1.8.36 | F2, F3, F10, M5-M8 | The human approval path is authoritative |
| 3 | 1.8.37 | F4, F11-F14 | Inert configuration and latent dispatch |
| 4 | 1.8.38 | M1-M4 | Ledger durability and path safety |
| 5 | 1.8.39 | M9-M14 | Privacy, resources, performance |

Suite: 204 → **267** deterministic cases.

### Fixed: MODEL OUTPUT was never redacted (M9)

`privacy.py` sanitized rollout **input** at every provider and replay boundary, and fails
closed there. The model's **returned** text was never sanitized, so it flowed
`_extract_skill_text` → staging → approval → live `SKILL.md` verbatim. A credential
echoed back from a prompt became part of a live skill document, and from there part of the
system prompt on every later load.

`adopt_write_skill()` is the single choke point every adopt path goes through (the HTTP
route, the agent tool, both drain paths), so redacting there covers all of them at once.
It is deliberately the **last** gate rather than the only one — redaction is best-effort by
contract and must not be the sole defence. The result now carries `redacted: True/False`
on **every** return path, so the audit row can record that a document was changed on the
way in.

Two persistence/egress paths also carried raw rollout text:

- the **real-gate tasks file** froze whole rollout records to `logs/runs/*.tasks.json` and
  the worker re-read them on every replay, so an unsanitized credential captured before the
  privacy controls existed was written to disk and then re-injected into a live agent
  context. Sanitized at the persistence boundary now (order and length preserved — the
  held-out gate compares by index).
- `inner_loop._llm_suggest_via_http` posted raw rollout text to an arbitrary configured
  endpoint — dormant only because `inner_loop_llm_endpoint` defaults to empty, which is a
  config accident rather than a control. It now sanitizes **and fails closed**: with the
  privacy helper unavailable the request is not sent.

### Fixed: the redactor missed the most common credential form (M10)

`_ASSIGNMENT_RE` led with `\b`, and `_` is a **word character** — so there was no boundary
in `AZURE_OPENAI_API_KEY=<secret>` and the entire alternative failed to match. Only the
literal `sk-` / `ghp_` patterns rescued the best-known cases, so the single most common
credential shape on the planet slipped through a redactor that read as comprehensive.
Replaced with `(?<![A-Za-z0-9])`, which permits a preceding underscore while still
refusing a match mid-run.

Also closed: non-HTTP URL credentials (`postgres://`, `mongodb://`, `redis://` — including
the empty-username `redis://:password@host`), DSA/PGP/ENCRYPTED/PuTTY key headers,
assignments inside escaped-JSON strings, and `secret_key` / `private_key` key names.

**Deliberately still not attempted:** generic high-entropy base64. A rule that redacts any
long base64 string would shred legitimate hashes, checksums and encoded examples to catch
a secret inside one — a worse trade than the gap, so it stays a documented limitation
(asserted by a test so it cannot be quietly forgotten). Redaction remains best-effort, not
a guarantee.

### Fixed: subprocess log handles were never closed (M11)

`Popen` duplicates the descriptor, so the parent did not need to keep the log handle open —
and leaving it open leaked one descriptor per cycle. On Windows an open handle also blocks
`os.replace`, so rotation silently stopped working for any log that had been written to.
Both spawn paths now close their handle after the spawn, guarded so a close failure can
never lose the pid.

### Fixed: four run logs grew without bound (M12)

`adoptions.log` (both adopt paths), `failure_memory.log` and `fragments.log` never called
the existing `rotate_log_if_large`. The latter two are fully re-read on every status poll,
so the cost grew with install age. All three now rotate, best-effort, so a rotation failure
never stops an audit row being written.

### Fixed: `default_config()` re-parsed 32 KB of YAML on a per-chat path (M13)

It sat on the reward-model scoring path (`score_rollout` → `model_path` →
`merged_config` → `default_config`), which runs once per finished chat in the harvester and
2×N per real gate. The parse is now cached on the file's `(mtime, size)`; the result is a
deep copy, because callers mutate it and that is why the original docstring concluded no
cache was possible. **Measured: 58.6 ms → 0.31 ms per call, 190×.** Editing
`default_config.yaml` still takes effect immediately.

### Fixed: both banner CTAs dispatched a dead action (M14)

`open-plugin-config:skillopt` matches no branch in
`welcome-store.js:executeBannerAction`, which only handles `open-modal:` and `open-url:`.
The button rendered and did nothing — worse than no button, because it looked like a
working control. Both banners now use
`open-modal:/usr/plugins/skillopt/webui/config.html`, the replacement this repo's own
`scripts/gen_missing_agents_md.py` had already recorded. The test asserts against the
WebUI **source**, not a literal, so the next rename fails there rather than in a user's face.

### Tests

259 → 267, eight new deterministic cases. One product bug was caught by the new tests
during this phase: `sanitize_rollout` rewrote a present-but-`None` `task` to `""`, a lossy
type change to persisted data made for no benefit — redaction now only touches string
fields.
## [1.8.38] - 2026-10-04


Audit remediation, **Phase 4: ledger durability and path safety**. Phase 1 gates, Phase 2
the approval path, Phase 3 settings and routes; Phase 4 is about the things that must
stay **true over time and on disk**. Findings M1-M4 from `AGENTS.md` → *Open findings
register*; sequencing in `REMEDIATION_ROADMAP.md`.

The shared theme: **a fact that is correct when written and wrong later, for reasons
nothing reports.**

### Fixed: rotation silently revoked every older approval (M2)

`governance.log` rotates at 5 MB and the previous generation is **deleted**. Every
approval older than one rotation window therefore vanished: `_human_approved` returned
False, a `require_human_approval` skill reverted to *pending* with no visible cause, and
`decided_by="autonomous"` audit rows were lost — contradicting the "auditable forever"
contract the whole dual-mode feature rests on.

`mark_human_decision` now also appends to `logs/runs/approvals_index.jsonl`, written by
the same function (so still one writer), never rotated, and holding only the latest row
per (skill, proposal) — bounded by the number of skills and outstanding proposals rather
than by history. Readers consult the log, its `.1` generation, **and** the index,
newest-wins. An install that predates the index still benefits from generation
fallback.

### Fixed: the two ledgers were merged by string-comparing timestamps (M1)

`governance.log` writes microsecond ISO in **UTC**; `cycle_history.jsonl` writes
`strftime("%Y-%m-%dT%H:%M:%S%z")` — no fraction, **local** time. `_decided_proposal_ids`
merged them with a plain string compare, so `'.' > '+'` plus the differing offsets
decided the winner rather than the clock, and an automatic adoption could be attributed
to a human approval. The merge now compares **parsed epochs** via a shared
`_parse_ts` that also handles the `+0200` offset `fromisoformat` rejects before 3.11.

Relatedly, `_last_human_decision` now picks the newest decision by timestamp rather than
by line order — strictly more correct, since generations merged after a rotation can be
out of line order.

### Fixed: `_skill_dir` had only one of two containment layers (M3)

`safe_skill_dir` documents name validation **and** a post-resolve containment check.
`_skill_dir` implemented only the first, so a symlinked skill directory let governance's
marker writes escape the skills tree while the adopt path refused the same name. Both
layers now run there, so the two agree.

Unsafe names also no longer share a single `_invalid_/` bucket: each gets a
subdirectory keyed by a digest of the raw name, so two different bad names cannot share
a policy scope (and `auto_optin_new_skill` can no longer `mkdir` and populate a shared
directory), the mapping stays deterministic so the audit trail remains meaningful, and
the unsafe string itself never reaches a path.

### Fixed: an explicit `0` in a policy was turned back into the default (M4)

`int(policy.get(key, D) or D)` meant `min_interval_seconds: 0` became a 3600-second
floor and `daily_budget_cents: 0` became a 100c cap — while `budget.BudgetTracker`
documents `daily_cap_cents <= 0` as unlimited, so governance contradicted the very
module it delegates to. Both now read through `_opt_int`, which defaults only on `None`.
`0` means "no limit" in both rails, and positive values still bite.

### Tests

252 → 259, seven new deterministic cases. One v1.8.35-era case was corrected for the new
newest-by-timestamp semantics (its year-2999 probe row would otherwise outrank every
later decision, so it now runs against its own skill).

The M2 case caught a real leak in its own fix: the durable index is a **new** file in
`logs/runs/`, and `reset_for_tests()` wiped only `governance.log` — so test rows
accumulated across runs and `t_c4_governance_approve_endpoint`'s "before approval"
assertion saw the previous run's approval and found the skill already eligible. Caught
live, cleaned, and both `reset_for_tests()` and the pollution guard now name the file.
That is the standing rule this exposed: **whenever a file lands in `logs/runs/`, it must
be added to `reset_for_tests()` and to the guard.**

---

## [1.8.37] - 2026-10-04

Audit remediation, **Phase 3: inert configuration and latent dispatch**. Phase 1 made
the gates correct, Phase 2 made the human approval path authoritative; Phase 3 makes
the **settings** do what they say and the **routes** mean what their path says.
Findings F4, F11-F14 from `AGENTS.md` → *Open findings register*; sequencing in
`REMEDIATION_ROADMAP.md`.

The shared theme: **a declared setting or a documented route that does nothing**,
and a test suite structurally unable to notice either.

### Fixed: two API routes silently answered with the wrong handler (F4)

The framework binds one handler per `api/<name>.py` module, taking `classes[0]` from
`load_classes_from_file`, which iterates `reversed(inspect.getmembers(...))` — the
**alphabetically last** matching class. Two modules declared two handlers each:

- `api/fragments.py`: `Fragments` (read) lost to `FragmentsRollback` (write), so
  `GET /fragments` returned the rollback handler's *"missing one of: skill,
  fragment_id, target_version"* error and a fragment **write** was reachable at a
  read-shaped path.
- `api/cycles.py`: `Cycle` (detail) lost to `Cycles` (list), so the documented
  `/cycle/<id>` answered with a **list payload**. The audit found only the first;
  the new structural test found the second.

Both are now one handler per module (`fragments_rollback.py`, `cycle.py`).
`t_v1837_api_dispatch_matches_intent` simulates the framework's selection across every
`api/*.py` — a check nothing previously could make, because every other test imports
handler classes **by name** and never exercises the dispatcher.

### Fixed: a whole subsystem's configuration was inert (F11)

In the A0 runtime the plugin is imported as `usr.plugins.skillopt.helpers.*`, so
`helpers` is the **framework** package and a bare `from helpers.sleep_runner import
merged_config` raises on every call. Four sites did that with **no** namespace
attempt — `helpers/failure_memory.py` (×3) and `helpers/ab_harness.py` — each wrapped
in a bare `except`. The failure was invisible and permanent: every `failure_memory_*`
and `ab_harness_*` key resolved to its hardcoded default, so
`failure_memory_enabled: false` did nothing. The rest of the plugin already used the
two-path idiom; these four did not.

### Fixed: `cycle_history` read its config from a module that does not exist (F12)

`helpers/cycle_history.py` imported `usr.plugins.skillopt.helpers.config_loader`, and
there is no `config_loader.py` in this tree. Every call raised, the `except` returned
the default, and `cycle_history_enabled` / `cycle_history_max_entries` were dead since
they were declared. Both now read through `sleep_runner.merged_config()`.

### Fixed: five declared budget/cadence settings were read by nothing (F13)

`BudgetTracker(...)` was constructed at **nine** call sites, each with a different
ad-hoc subset of the `budget:` block — which is why `daily_cap_cents` and
`reset_hour_utc` were declared but never supplied and the effective cap was always the
module default. `budget.enabled`, `cadence.enabled` and `cadence.target_rollouts` were
read by nothing at all.

All budget construction now goes through one factory, `budget.tracker_from_config()`,
so the settings cannot drift per call site again. The tracker that **records** spend is
built by the same factory as the one that **refuses** it — otherwise they accumulate
against different budgets. `budget.enabled: false` and `cadence.enabled: false` are
honoured, the latter via an internal `_CadenceDisabled` sentinel so the budget gate
still runs.

**The test that should have caught this was itself blind.** `t_v1824_config_keys_have_readers`
greps the leaf name anywhere in the sources, so a key appearing only in a helper's own
signature counted as "read" while no caller supplied it. A companion case now asserts
the distinction, so the next inert key cannot pass either.

### Fixed: a timed-out official engine was not marked `deferred` (F14)

On timeout the official engine is left detached and may still write
`staging/<skill>.md` seconds later, but the result carried `fallback_to_direct: True`
**without** `deferred: True` — which the caller reads to skip the competing direct
cycle. So after a timeout both engines wrote the same staged file, and cadence/budget
were charged once for what was actually two cycles. The branch now reports `deferred`
and names the still-running pid in its reason.

### Tests

244 → 252, eight new deterministic cases, including a source-level assertion that no
`helpers/` import lacks a namespace attempt and a structural assertion over the
framework's handler selection for every `api/*.py`.

The `cycle_history_max_entries` case immediately caught a bug in the fix itself: the
`or 500` idiom collapsed `0`, which that function's own docstring defines as "no
compaction".
## [1.8.36] - 2026-10-04


Audit remediation, **Phase 2: the human approval path is authoritative**. Phase 1 made
the gates correct; Phase 2 makes the routes a person actually clicks behave the way
the approval UI claims they do. Findings F2, F3, F10, M5-M8 (and L3) from
`AGENTS.md` → *Open findings register*; sequencing in `REMEDIATION_ROADMAP.md`.

The shared thesis: **the three adopt paths — HTTP route, agent tool, and background
drain — must be the same gate. A path that is easier to reach must not be weaker.**

### Fixed: a human rejection was advisory, so the drain re-adopted the document (F2)

`/reject` wrote two audit rows and returned. It did not quarantine the file, did not
record the denial in the governance ledger, and **there was no "human said no" rail
anywhere** — the only approval gate (`require_human_approval`) is exactly the one the
autonomous override skips. So on a later tick the drain re-read the same rejected file
and wrote it into the live `SKILL.md`, contradicting the documented rule that rejects do
not stay in staging.

Three changes:

1. `/reject` now validates the id against the staged queue (closing L3: an arbitrary
   string used to become the `skill` on both audit rows, so a row naming a
   not-yet-staged stem made `find_pending_proposal` report the skill's *next* proposal
   as already rejected and the Governance card silently stopped prompting).
2. `/reject` **quarantines** the proposal — the document is preserved verbatim under
   `staging/rejected/` with all its sidecars, it simply leaves the pending queue — and
   records the denial bound to that `proposal_id`. Both outcomes are surfaced in the
   response and shown in the UI.
3. New rail at `governance.check_skill_eligible` **step 6.5**, deliberately *outside*
   the `auto_adopt` bypass. Autonomy may bypass "has nobody approved this document?"
   — that is what autonomy means. It must never bypass "a human explicitly refused
   this document."

The rail is **staleness-aware**, which is what keeps it from becoming a mute button: a
denial binds only the proposal it was made about (compared against the staged file's
mtime), so a *new* proposal for the same skill is judged on its own merits and a
re-approval clears it immediately. A machine row never arms it.

Its first implementation was silently inert and a probe caught it: the ledger stores
microsecond-precision ISO while `st_mtime` is a full-resolution float, so `ts >=
staged_at` failed by ~0.5 µs for a write that causally happened first.
`_REJECTION_MTIME_TOLERANCE_S` absorbs the precision gap and nothing else.

### Fixed: `/adopt` wrote the live skill non-atomically and overwrote without a snapshot (F3)

The route snapshotted, **swallowed a snapshot failure**, then wrote with
`Path.write_text` — open-truncate-then-write. That is the v1.8.24 P0 defect on the one
route a human clicks: a failed snapshot left `/rollback` with no target while the
overwrite proceeded (the unrecoverable case `adopt_write_skill`'s own docstring says
must refuse), and a mid-write stop left a half-written live skill.

The write now goes through `sleep_runner.adopt_write_skill()` — the only sanctioned
writer, which the drain and the agent tool already used — and the route returns
`ok: false` when it refuses. A refused adopt is still audited, so the trail shows an
attempt that did not land rather than nothing at all.

### Fixed: `/governance_approve` could approve by accident (F10)

`bool(data.get("approved", True))` on an endpoint that records a human decision *and*
touches the opt-in marker: a body with no `approved` key approved, and
`{"approved": "false"}` approved because `bool("false")` is `True`. The field is now
**required** and strictly coerced (real booleans, 0/1, and the unambiguous string
spellings) — the same dialect `api/config.py` uses, so there is one coercion rule in
the plugin rather than three. `skill` is validated before it reaches a path builder,
matching `api/governance_pause.py`, and a failed opt-in marker write is now **reported**
rather than swallowed behind `ok: true`.

### Fixed: `/adopt` silently adopted the wrong proposal (M5)

A supplied `proposal_id` that matched no staged stem fell through to `staged[0]`. A
reviewer could read proposal A's summary and diff, click Approve, and have proposal
B written instead. `api/proposal.py` already treated stem-equality as a hard boundary;
this was the one route that treated a non-match as "no constraint". A non-match is now
an error, and "adopt latest" remains only for the genuinely argument-less call.

### Fixed: the manual adopt paths skipped real-gate single-flight and never consumed (M7)

Neither the route nor the tool honoured `find_pending_real_gate_sidecar()`, so an
operator could adopt a proposal whose independent replay was still running — and if
the worker later returned a negative verdict, `_harvest_real_gate` would act on the
same file again. Neither consumed the proposal, so it stayed queued and the drain
re-attempted it. Both now defer while a gate is in flight and consume on success,
carrying every sidecar.

### Fixed: the agent-callable adopt tool skipped governance entirely (M8)

`skillopt_sleep verb=adopt` went straight from `find_staged_proposals()` to
`validate_proposal()` to `adopt_write_skill()`, so an agent could write a live
`SKILL.md` for a skill carrying optout / immutable / pause / interval / budget markers.
It also omitted `skill_name=`, so the two adopt paths ran **different gates**. The tool
now runs the same governance chain as the drain and the route, and fails **closed** if
governance is unavailable or raises. The child DOX states the intent plainly: "the
agent-callable adopt tool is as exposed as the background loop."

### Fixed: the settings page reported a failed snapshot as success (M6)

The adopt status ended in `|| true`, so `lastOk` was always `true` and the message
rendered green even when it read `FAILED` — and after F3 the snapshot failing now
blocks the write, so the two had to be brought in line. Colour follows the text, and
the message also reports whether the proposal actually left the queue. The reject
message now says whether the proposal really moved out of it.

### Tests

235 → 244, nine new deterministic cases. One v1.8.34-era test was updated rather than
the code: `t_c3_reject_records_cycle_history` rejected an id that was never staged,
which the new L3 validation correctly refuses — it now stages a real proposal and
asserts the full flow (quarantine, preservation, ledger, rail armed).

The new cases assert both causal directions of the rejection rail, and pin that all
three adopt paths refuse what the rails forbid.

---

## [1.8.35] - 2026-10-04

Audit remediation, **Phase 1: safety rails and the autonomy contract**. Every item
below was a defect on the path that writes a live `SKILL.md` unattended, found by
the read-only audit recorded in `AGENTS.md` → *Open findings register*. Phasing and
the full list: `REMEDIATION_ROADMAP.md`.

### Fixed: autonomous mode was inert on any install using the legacy alias (F1)

`governance.evolution_mode` ships as `manual`, so `merged_config()` **always**
contains it — and all eight production call sites passed that merged blob to
`resolve_evolution_mode(cfg)`. Precedence therefore matched on the *shipped
default*, the legacy alias was never consulted, and an install whose
`config.json` says `auto_adopt: true` resolved to **manual** while reporting
`source: "evolution_mode"` — a provenance that was false. Meanwhile `/mode`
(no-argument call) consulted the operator's own settings and reported
`autonomous`, so the UI and the drain disagreed about the same install.

Live evidence: the drain's own canary line fired on every tick
(`logs/runs/auto_loop.log`, 08:37 / 09:07 / 09:37 / 10:08 on 2026-10-04).

The fix has two halves and **both were required** — fixing only the resolver
leaves production broken, which is exactly what shipped:

1. `_operator_config()` prefers the operator's persisted config
   (`_framework_registry_config()`) whenever the registry yields anything.
2. Every production call site now calls `resolve_evolution_mode()` with **no
   argument**. A test asserts this on the source, because a merged blob
   re-introduces the defect precisely.

The v1.8.34 guard missed this because it called the bare resolver — a path
production never took. `t_v1835_autoloop_resolves_mode_from_merged` now drives
the drain gate itself.

### Fixed: a raising governance check disabled every rail (F8)

`check_skill_eligible` could raise — `_runs_dir()` ends in a `mkdir` and sat
outside the `try` in two readers, and the `.skillopt.pause_until` read was
unguarded. The sole production caller logged `"fall through"` and continued to
**eligible**, so optout, immutable, pause, cadence and budget were all skipped
and autonomy then wrote a live skill for a paused one.

Now fail-**closed**: `governance_unavailable`, a logged `DEFER`, and
`_record_error` so the dashboard shows it. Absent governance is also treated as
a broken install rather than a permissive one. Cadence and budget keep their own
fall-through — those only throttle work and cannot authorise it.

### Fixed: an autonomous decision satisfied the human-approval gate (F9)

`_human_approved()` filtered on `event == "human_decision"` only, while the
drain's audit hook writes `decided_by="autonomous"` rows through the same
function. After one autonomous adoption, switching back to manual passed
`require_human_approval` with no human ever clicking Approve.

The filter excludes the **machine** values (`MACHINE_DECIDED_BY`), not
everything that is not literally `"user"` — `decided_by` is free-form and a named
operator is legitimate, so an allow-list of human strings would have silently
invalidated real approvals. Rows predating the field default to human.

### Fixed: the daily budget cap latched permanently (F7)

`_maybe_rollover()` was reachable only from `record_spend()`, which itself runs
only *after* a cycle that `can_spend()` gates. Hitting the cap therefore
deadlocked: the skill reported "daily cap exceeded" every tick, forever, with no
reset. `can_spend()` and `get_status()` now roll over on the read path too.

### Fixed: the per-tick rollout-count invalidation was a no-op (F5)

`_invalidate_rollout_counts()` assigned a **class** attribute while the reader
used `getattr(self, ...)` and wrote `self._rollout_counts_cache`. Once the
instance attribute existed it shadowed the class one permanently, so the cache
stayed frozen at whatever the first drain saw — the exact staleness the code
claimed to prevent. It is now an instance method, initialised in `__init__`.
Inert at the shipped default (`autonomous_min_evidence_rollouts: 0`), actively
wrong for any operator who raises it.

### Fixed: the last non-atomic state write (F6)

`_save_state()` used a bare `write_text` while `api/loop.py` reads the same file
on the HTTP request thread. A torn read is swallowed by `_load_state`, which
returns defaults — resetting counters *and* zeroing
`last_rollout_count_at_cycle`, which fires an unscheduled cycle. Now
write-temp + `os.replace` + `fsync`, matching `cadence.py` and `budget.py`,
with the temp file cleaned up on any failure.

### Tests

226 → 235, nine new deterministic cases (no LLM, network, or clock dependence),
including two cross-checks that the whole fail-closed set holds together under
autonomy and that an unreadable pause marker defers rather than reading as
"not paused".

One v1.8.34 test was corrected rather than the code: `t_autonomous_postcheck_rolls_back`
drove the mode by passing a `cfg`, which after F1 no longer overrides the
operator's persisted config — so its "manual is inert" case had been running with
the rails **active** and passing for the wrong reason. It now drives the
registry, as production does.

---

## [1.8.34] - 2026-10-03

Approval now shows *what* would change and *why*, and the plugin has a
first-class manual/autonomous switch that is **off by default**.

### Added: informed approval

Staged proposals and the Governance card render a bounded `ProposalSummary`
(`helpers/proposal_view.py`, schema `skillopt.proposal_summary/1`, persisted as
`staging/<skill>.md.summary.json`): an intent classification, a one-paragraph
summary, evidence-anchored rationale bullets, the gate evidence, and a unified
diff against the live `SKILL.md`. `POST /api/plugins/skillopt/proposal` serves
one proposal at a time for the uncapped diff view.

Two gaps were closed rather than just restyled. The only field built for a
human summary had been `"diff_summary": f"{size} bytes"` — a stringified byte
count, which the template never even rendered. And proposals had no link to
decisions at all: `staging/` keys on the filename stem while `governance.log`
keys on the skill, so the Governance card could not say *which* document its
Approve button referred to. `governance.find_pending_proposal()` resolves that
link, and `mark_human_decision()` takes optional `proposal_id` / `summary_id` /
`note` so a decision binds to the file the reviewer actually read.

Intent is **advisory**: nothing in `validate_proposal` or
`check_skill_eligible` reads it. A misclassified intent cannot change whether a
proposal is adoptable. Proposals staged before this release are summarised on
first view — no migration step.

Rejecting now asks for a reason, which is recorded and fed into failure memory
instead of the fixed string *"user rejected via UI"*.

### Added: dual-mode operation

`governance.evolution_mode` (`manual` | `autonomous`, default `manual`) is the
single operator-facing switch. The pre-existing top-level `auto_adopt` is
demoted to a **legacy alias**, read only when `evolution_mode` is absent, so
existing installs keep their behaviour; `evolution_mode` wins when both are
present. A second parallel mode flag was deliberately not added — two
overlapping switches with undefined interaction is precisely the failure mode
behind the `auto_adopt: null` stall and the v1.8.18 semantics change.

`governance.resolve_evolution_mode(cfg)` is the only way to read the answer, and
replaces three call sites that used `bool(cfg.get("auto_adopt", False))`:
that expression is `None` for `auto_adopt:` with no value (the drain silently
stops) and `True` for the string `"false"` (the drain silently starts). The
resolver strictly coerces, falls back to `manual`, and writes a rejected value
to `logs/runs/auto_loop.log` naming the value and the valid set — once per
distinct value, not once per tick.

`logs/runs/.autonomous_pause` is checked **first** and overrides both config
sources, so autonomy can be frozen without editing `config.json` (which the
WebUI also writes) or restarting.

### Added: autonomous safety rails

`auto_adopt_max_per_tick` is 5 and a tick is every 30 minutes, so the
theoretical ceiling on writes to live skills was ~240/day with **no daily
bound**. Autonomy is now bounded by `autonomous_max_adoptions_per_day` (5),
`autonomous_max_adoptions_per_skill_per_day` (2),
`autonomous_min_evidence_rollouts` (0 — inert), `autonomous_require_post_adopt_check`
(true), and `autonomous_audit` (true). A cap refusal returns `skipped` and
leaves the proposal in staging, because a cap that resets tomorrow is a
deferral, not a rejection.

`autonomous_min_evidence_rollouts` defaults to **0 rather than 20**, a change
from the original design: a rollout *count* is a weak proxy for justification
(the held-out gate is the real measurement), and `rollout_retention_days`
deletes rollout files, so a non-zero default would block every autonomous
adoption after a prune, with proposals parked in staging and no obvious cause.

Every autonomous adoption is recorded in `governance.log` through the existing
`mark_human_decision` with `decided_by: autonomous` — one ledger, using the
discriminator that already existed, so a machine decision stays permanently
distinguishable from a human one.

### Fixed: five settings-page controls were silently discarded on save

`webui/config.html`'s `save()` hard-coded a six-key POST body while the page
declared thirteen, so `privacy_redact_secrets`, `privacy_include_tool_args`,
`privacy_include_tool_results`, `rollout_retention_days`, and the three
`replay_evalkit_*` settings rendered as live controls and were thrown away on
every save — the same class of defect as the v1.8.2 config wipe. `SKILLOPT_EDITABLE_KEYS`
is now the single source of truth for both `save()` and `refresh()`, and
`refresh()` no longer hides a server value whose key it did not already know.

### Fixed: `api/config.py` persisted client values verbatim

The endpoint stored whatever the client sent (`clean[k] = v`) and `_read_disk()`
applied no validation, so `{"governance": {"autonomous_require_post_adopt_check":
"yes"}}` was stored as the string `"yes"` and reached every consumer
unvalidated. Values are now coerced to the type of their default; an
uncoercible value is reported in a new `rejected` list and **skipped** rather
than aborting the save. Nested sections merge one level deep with per-leaf
coercion, so a partial `governance` payload cannot clobber its siblings.

### Fixed: diff counters counted the `+++` file header as an addition

`compute_diff` counted the `+++ b/staged` header line as an added line. This
was not cosmetic: a pure deletion reported `added_lines=1`, which pushed
`classify_intent()` past its remove-only branch and mislabelled a clean
condensation as `unknown`. Truncation also now reserves its marker *out of*
`diff_max_chars`, so the returned text never exceeds the advertised cap.

### Fixed: the suite leaked a proposal sidecar into production `staging/`

Once `/staged` began building summaries, `t_c3_staged_endpoint_returns_evidence`
left `c3_staged_skill.md.summary.json` behind on every run — its cleanup removed
only the `.md`. `staging/` is the one directory where a leaked file is an
operational hazard rather than clutter, because `find_staged_proposals()` scans
its top level. The fixture cleanup now removes its sidecars, and the existing
fixture-pollution guard (which previously checked only `logs/runs`) was extended
to cover `staging/`.

### Fixed: upgrading no longer silently stops an autonomous install

`governance.evolution_mode` ships as `manual`, so `merged_config()` always
contains it. The first implementation resolved precedence against the merged
config, which meant that on an install whose `config.json` said
`auto_adopt: true` — **including this repository's own** — upgrading to
v1.8.34 silently switched unattended writes to live skills off, with no log
line and no UI indication.

Precedence is now decided against the **operator's persisted** config (the
framework registry, or `{}` when unavailable), and only then falls back to the
shipped default: kill switch → operator's `evolution_mode` → operator's
`auto_adopt` → shipped default. A shipped default never outranks an explicit
operator setting. `source` reports `evolution_mode`, `auto_adopt_legacy`,
`evolution_mode_default`, or `kill_switch`, so the Evolution mode card shows
which input won instead of the mode changing behind the operator's back.

### Fixed: `/mode` could have wiped the governance section

The route persisted `merged_config()` directly. `merged_config()` returns an
**empty** dict (measured: 0 of 79 keys) whenever the defaults YAML is
unreadable, and `save_plugin_config` **replaces** the `governance` section
rather than deep-merging it — so in that condition a single UI toggle would
have written `governance: {evolution_mode: ...}` alone, destroying
`default_policy` and every `autonomous_*` rail. The v1.8.2 config-wipe
incident class reached through a different door.

The write now goes through `api/config.py`'s already-tested `_read_disk()` +
`_clean_overrides()`, sends only the two keys it owns, carries the complete
governance section, and **refuses** rather than writing when the config cannot
be read in full. Its response is built from the config it just wrote, not a
re-read, so the UI cannot display a stale mode when `clear_plugin_cache()` has
not yet taken effect.

### Fixed: the gated-cycle runner reported a bare truthiness mode and no content

`scripts/run_sleep_cycle.py` still read `bool(cfg.get('auto_adopt'))` — the same
bug fixed in `auto_loop` — so its `PENDING_HUMAN_APPROVAL` status could
contradict what the loop was actually doing. It now reports the resolved mode
and its source. That emit is the operator-facing "a change is waiting for you"
record, and it named a path and a byte count; it now carries the bounded
proposal summary too, so reading the cycle log tells you what is being proposed
without opening the file. The runner still never adopts.

### Fixed: the dashboard namespace was never exposed (dead extension point)

`webui/skillopt-dashboard.js` declared `const SkillOptDashboard = {...}` inside
its IIFE and **never assigned it to `window`**, so `window.SkillOptDashboard`
was `undefined` — while its own header comment promised "a small
`window.SkillOptDashboard` namespace that the WebUI config page and the
sidebar card can hook into". The entire documented dashboard extension point
was unreachable; any page calling `SkillOptDashboard.staged()` would have got a
`TypeError`. Nothing failed visibly only because no consumer referenced it yet,
which is how it stayed unnoticed. Now assigned (guarded, for the older browsers
this WebUI supports and for a double-injected script).

### Fixed: the dashboard's approval surface dropped the summary

`renderStagedProposals` mapped a `/staged` response down to
skill/proposal_id/size/lift/gate_reason and discarded everything else — so
while the settings page gained an intent badge, a rationale and a diff, the
dashboard stayed a name-and-byte-count card. Two approval surfaces, one of them
still blind, is the exact problem this feature set out to remove. It now carries
`summary` and `pending_approval`, with three new pure defensive renderers
(`renderProposalSummary`, `renderPendingApproval`, `renderEvolutionMode`) and
`/proposal`, `/mode`, and a proposal-binding `approveGovernance` client.

### Fixed: the Governance card would have shown "No summary available"

`/staged`'s `pending_approval` entries carried no `summary` — only
`/governance_status`'s per-skill block did. Since the Governance card reads the
`/staged` map (fetched once and shared so both cards render one document), its
informed block was permanently empty in the live UI. Both routes now attach the
summary, and a test asserts they resolve the *same* text, so a reviewer cannot
approve one card's view of a document while looking at another's.

### Fixed: stale dashboard JS could be served from cache

The script tag pinned `cb=3`, so shipping changed dashboard JS kept the same
URL and a browser could serve the old file — a silently dead UI. Bumped to
`cb=4`, and the smoke assertion now checks for a numeric cache-bust parameter
instead of one literal, so future bumps need no test edit.

### Tests

22 new deterministic cases (204 → 226), no LLM, network, or clock dependence.
The load-bearing one is `t_autonomous_cannot_bypass_rails`, which asserts that
optout, immutable, pause, opt-in/opt-out markers, and `min_interval_seconds` all
still bite with `auto_adopt=True`, and that the approval gate is the *only*
thing autonomy yields — each on a fresh skill so one seeded decision cannot
shadow another assertion; and \	_v1834_upgrade_preserves_operator_choice\ plus
\	_v1834_mode_route_no_config_wipe\, which pin that upgrading cannot silently
stop an autonomous install and that one UI toggle cannot delete the settings.

---

## [1.8.29] - 2026-09-26

### Added: configurable rollout expiry

`rollout_retention_days` opts into deleting aged rollout JSON files from the
plugin's top-level `logs/rollouts/` directory. It defaults to `0` (disabled),
so existing data remains intact unless the operator sets an expiry. Cleanup
runs during auto-loop ticks, reports removals, and adjusts the count-based
cycle baseline so expiration cannot stall future cycles. Bridge caches,
staging tasks, and run history are outside this cleanup boundary.

### Tests

Added deterministic coverage for disabled retention and expiry boundaries.

## [1.8.28] - 2026-09-26

Model selection follows Agent Zero's presets (no pinned model names), and the
official engine's backend set stops silently dropping valid values. This
release also adds opt-in rollout privacy controls and an optional upstream
paired evalkit report for completed real replay gates.

### Added: rollout privacy controls

New rollouts omit tool arguments/results by default and redact common
credential patterns. Existing rollouts are sanitized again at direct model,
official bridge, and real replay boundaries. Redaction is best-effort; the
plugin-local Claude history cache is migrated without rewriting A0's own
history or external cache overrides.

### Added: optional upstream evalkit report

`replay_evalkit_enabled` attaches paired statistics from the installed
`skillopt_sleep.evalkit` CLI to completed real-gate sidecars when supported.
The capability is probed before use and report errors never change the gate
or adoption outcome.

### Fixed: a model name was pinned in the environment, silently overriding the preset

`usr/.env` and `logs/runs/.skillopt-env` both set
`SKILLOPT_OPTIMIZER_MODEL=minimax-m3` and `SKILLOPT_JUDGE_MODEL=minimax-m3`.
Those env vars take precedence over the config sentinels, so the plugin was
calling **MiniMax while the active Agent Zero model was
`glm-5.3-flash` (`ollama_cloud`)** — a provider mismatch that presented only
as poor optimization quality, never as an error. Both are now removed (with a
comment explaining that setting them is a deliberate pin) and the YAML
sentinels take effect.

### Added: the `utility` model slot

`helpers/chat_model.py` resolved exactly one sentinel, `'chat'`. The
`_model_config` preset already carries a `utility` slot that nothing read, so
the resolver is now slot-parameterised (`resolve_slot_model`,
`get_slot_connection`, `SLOTS`, `is_slot`, `normalize_slot`) with per-slot
caching. `resolve_chat_model` / `get_chat_connection` / `_CACHE` are retained
as the `chat`-slot view, so existing readers are unaffected.

Job → slot mapping, in config rather than code:

| Key | Slot | Why |
| --- | --- | --- |
| `optimizer_model` | `chat` | proposing skill rewrites is high-reasoning work |
| `target_model` | `chat` | held-out replay must match production |
| `judge_model` | `utility` | outcome labelling is bulk classification |

Both track the active preset, so upgrading the preset upgrades the plugin
with no code change. `t_v1828_config_has_no_pinned_model` fails the run if a
concrete model name is hardcoded in `helpers/`.

### Fixed: a slot sentinel could reach the provider as a literal model id

`llm_judge._judge_model` and `direct_optimizer._call_llm` both gated on
`model != "chat"`. With `judge_model: "utility"` that comparison is true, so
the string `"utility"` would have been sent as the model name. Both now use
`chat_model.is_slot()`.

### Fixed: a requested slot was discarded

`_judge_model("utility")` discarded the argument and fell back to
`optimizer_model`, so asking for the utility slot silently ran the *chat*
model. A passed-in slot is a request and is now honoured (env still wins, as
a deliberate pin).

### Fixed: valid `official_backend` values were silently dropped

`official_adapter` gated `--backend` on a private literal that disagreed with
`setup_env.BACKENDS` in **both** directions: it listed
`mock|codex|copilot|cursor|pi|handoff` (never passed) and omitted
`openai_compatible|qwen|minimax` (passed by the env builder). The failure was
self-concealing — the flag vanished, the engine fell back to its own default
(`mock`), and the plugin's own `backend_guard` then refused, so a *correct*
configuration looked like a refusal. Notably `openai_compatible` is the
backend an OpenAI-compatible chat model needs.

`PASSTHROUGH_BACKENDS` is now derived from `setup_env.BACKENDS` (minus `auto`
and `mock`) by import, so the two cannot drift again and a new backend
becomes selectable with no code change. A rejected value is written to
`logs/runs/auto_loop.log` naming the value *and* the valid set, so the next
occurrence is self-diagnosing instead of silent.

### Added: the plugin installs its own engine (self-healing)

Only `/a0` is bind-mounted into the container, so `/opt/venv-a0` loses
`skillopt` on every rebuild — the plugin used to revert to
`direct_optimizer` with no error. The pin `ENGINE_REQUIREMENT` now lives in
`official_adapter`, inside the surviving tree, and `ensure_engine()`
re-installs it when the probe finds it missing. `_run_engine_for_skill` calls
it before probing, so a rebuild self-heals.

This is deliberately **not** added to `docker/run/fs/ins/install_A0.sh`.
That is a tracked core file serving an ignored plugin, and it would put a
`skillopt` dependency into every user's image whether or not they use the
plugin. `ensure_engine` never raises (returns
`{ok, installed, available, reason, py}`), re-probes with `force=True` after
a successful pip, and rate-limits retries to 15 minutes so a down network
cannot spawn pip on every tick.

### Fixed: judge pacing spaced planned starts, not actual ones

`_throttle_wait()` reserved an *ideal* slot, released the lock, then slept.
That spaces the *planned* starts, but the ones that actually hit the provider
are subject to OS timer jitter — the Windows default timer granularity is
~15.6ms, the same order as small throttle intervals — so real attempts could
land closer together than `SKILLOPT_JUDGE_THROTTLE_S`, defeating the 429
protection pacing exists to provide.

It now holds the lock **across** the sleep and stamps the real start, so
spacing holds by construction and an oversleep pushes the next slot out
rather than being absorbed. The wait is serialized, which is the correct
semantic for pacing; judge calls still run concurrently afterwards.

### Fixed: two tests that could not pass reliably

Both pacing tests asserted `min(gap) >= 0.015` on wall-clock against a 20ms
interval. Given ~15.6ms timer granularity that is un-flakeable by
construction — it failed roughly 1 run in 3, and it was *detecting real
jitter* in the old implementation rather than flaking for no reason. They now
assert that pacing is serialized (robust, order-of-magnitude) on the wall
clock, and the exact spacing invariant — including late-wake and clock-stall
behaviour — is pinned deterministically on a new `_monotonic` seam via
`t_v1818_pacing_deterministic`. 30 consecutive runs of both now pass.

### Fixed: harvester test stub had drifted from the module

`_fake_sleep_runner` did not expose `merged_config()`. The harvester resolves
privacy controls through it *before* persisting any task text and skips the
rollout when it is absent, so the three v1.7.0 C1 cases were failing on
`[]` — the privacy gate failing closed against an incomplete stub, not a
product defect.

### Tests

+13 (`t_v1828_*`, `t_v1818_pacing_deterministic`). Suite is 201/201, stable
across repeated runs.

---

## [1.8.26] - 2026-09-26

Closes the remaining roadmap items, and fixes two bugs that only became
visible under live testing on Agent Zero v2.13.

### Fixed: the test suite was writing into the production logs

**Found by live testing, not by the suite.** The repository is bind-mounted
into the container, so this suite and the running server share one
`logs/runs/` tree. Tests that reached `_adopt_one` or the drain appended
fixture-named rows to production logs: **140 rows** across `auto_loop.log`,
`adoptions.log`, `failure_memory.log` and the `real_gate_worker_*` files,
naming skills like `v1821_govskip_skill` and `a0-debug-plugin`.

That is worse than untidy. `auto_loop.log` is the log an operator reads to
decide whether the loop is working, so test rows in it make a real stall
indistinguishable from routine activity - the v1.8.17 RC8 "silent-failure UX"
problem recurring through a different door. The plugin's own changelog
claimed the leak was fixed in v1.8.21 P4; it was fixed for the files that guard
checked by *name* and not for the shared audit and loop logs.

- New `_sandbox_runs_dir(tmp)` helper, applied to every test that writes run
  state or logs.
- `t_p4_no_fixture_pollution` now inspects log **content**, not just filenames,
  across every `logs/runs/*.log`.
- Purged the 140 rows, after restoring a backup taken when the first, broader
  purge attempt turned out to also match genuine engine output.

A module-wide `runs_dir` patch was tried and **reverted**: it contained the
leak but broke `v1.8.4 SECURITY`, which legitimately reads the production
`.skillopt-env`. The comment at the sandbox site records that, so it is not
retried blindly.

### Fixed: snapshot pruning kept the wrong versions

`fragment_max_history_per_id` was declared since v1.6.0 and read by nothing,
so a long-lived install accumulated one file per adopt per skill forever - an
unbounded-growth risk on the plugin's own disk. Now wired, along with
`fragment_snapshot_dir`.

The first implementation sorted candidates by **file name**, so
`_default.v9.md` outranked `_default.v12.md` and a prune would have discarded
the newest history and kept the oldest - exactly the snapshots an operator
would want to roll back to. The new test caught it on first run; the sort is
now numeric. `_default.pre_adopt.md` (the current rollback target) and
`*.current.md` (the live version) are never pruned.

### Fixed: cadence bounds were declared but never applied

`cadence.floor_seconds` and `cadence.ceiling_seconds` were declared since
v1.6.0, but both call sites invoked `compute_next_run(new_n)` with no
arguments, so it silently fell back to its own defaults. An operator tuning
them saw no effect and got no warning. Both call sites now pass the configured
values, with an `isinstance` guard so a malformed section degrades to the
default rather than raising inside the tick.

These four keys leave the known-dead allowlist, which the reader test checks
in both directions - so a key that gets wired must be removed from it, and a
key that becomes unread again is caught.

### Remaining known-dead config keys: 6, each with a reason

`official_target_model` (no such flag on the real CLI) · `log_level` (a plugin
must not own root logging) · `skill_target` and `sleep_schedule` (superseded
by per-skill governance and the A0 scheduler) · `cycle_history_include_skipped`
and `cycle_history_min_outcome` (writer-side filters not implemented).

### Documentation

`AGENTS.md` claimed nested config sections arrive as strings and that the
budget cap was silently disabled, and that hub-status was a deliberate
auth exception. Both were true when written and are false as of 1.8.25/26,
so both statements are corrected - a DOX file that contradicts its own code is
worse than a short one.

### Tests

189 cases, **189 passing on Linux under Agent Zero v2.13** (verified in the
live container). On Windows the suite is 186/189: `v1.8.5 SECURITY: apply ...
atomic 0600` asserts POSIX permission bits and cannot pass there, and the two
`v1.8.16` judge-throttle tests are wall-clock sensitive and flake
independently of any change.

### Known limitation, stated rather than hidden

A production log row that names a **real** skill (e.g. `a0-debug-plugin`)
cannot be attributed to a test from the log alone, so the guard reports it as
a NOTE instead of failing. Its content is truthful - that skill really is
opted out. Rows carrying an unambiguous test signature still fail the run.

---

## [1.8.25] - 2026-09-26

Finishes the P0/P1/P2 remediation roadmap that followed the 1.8.24 incident.
Every item below is either a security fix or a guard that makes a previously
silent defect fail loudly.

### Security: an unauthenticated subprocess spawn (P0.5)

`api/hub_status.py` advertised `GET|POST` with `requires_auth() -> False` and
`requires_csrf() -> False`. The handler is not side-effect free: a stale or
missing payload makes it spawn `scripts/check_hub_status.py`, so an anonymous
caller could trigger process spawns on a network-reachable instance, rate
limited only by the 3600 s payload cache. The "read-only public status
payload" justification did not hold - nothing consumed it anonymously (the
WebUI dashboard never called it, and the background watchdog reads the file
directly).

POST is removed and the framework's auth + CSRF defaults are inherited, so
requiring CSRF is now free. No handler in the plugin relaxes auth any more.

### Security: path traversal via the skill name (P1.8)

Thirteen sites joined a caller-supplied skill name straight onto the skills
directory, reachable from the adopt/staged/fragments HTTP endpoints and from
staged proposal files, which are LLM-written. New
`sleep_runner.is_safe_skill_name` / `safe_skill_dir` / `safe_skill_md` make
traversal unrepresentable and re-check that the resolved path is still inside
the skills directory after normalisation.

The guard is deliberately **not** the framework's `^[a-z0-9-]+$` charset. That
is a naming policy for authored skills; enforcing it here rejected names this
plugin's own subsystems use (`skillA`, `v1821_drain_good`,
`skillopt_prop_*`). An earlier revision did exactly that and broke 27 tests.
What the guard rejects is only what can escape: separators, `..`, dot-prefixes,
drive-relative forms, NUL, and Windows reserved device names.

`governance._skill_dir` validates the name with the shared predicate but joins
it onto its OWN base. Delegating the join to `safe_skill_dir` pointed every
governance test at the *production* skills tree, because that module's sandbox
is `_TEST_SKILLS_DIR` rather than the `SKILLOPT_SKILLS_DIR` env override -
reintroducing the v1.8.19 P4 test-isolation defect.

### Correctness: an ungated direct-path adoption (P1.6)

"The official package is absent, so fall back" was silently equivalent to
"adopt ungated": the mock counterfactual gate is advisory, and a real-gate
spawn only happens when the gate is enabled and there are enough held-out
rollouts. With neither, only the structural pre-filter stood between a
proposal and the live skill.

New `allow_ungated_direct_adopt` (default **false**) refuses such a
proposal, leaves it in staging for human review, and logs how to opt in.
Official-gated proposals are exempt - the upstream monotonic gate is
authoritative and already ran. Manual `/adopt` is unaffected.

### Correctness: a dead extension (P1.7)

`extensions/python/hooks/_post_skill_adopt.py` targeted `hooks`, which is not
an extension point in this framework, and was a module-level function rather
than an `Extension` subclass, so the loader could never have found it. It is
deleted, along with its `execute.py` and smoke-suite manifest entries. The
`skillopt_train` tool's `validate` verb had told the agent the validation gate
"is implemented in the post-adopt hook"; it now names the real gate.

### Correctness: nested config sections were unreachable (P2.11)

`default_config()` used a flat hand-rolled parser, so a bare `budget:`,
`cadence:` or `governance:` header was stored as the **empty string**.
`cfg.get("budget", {}).get(...)` then raised `AttributeError` - and because
the surrounding `except` fell through, the daily budget cap was silently
disabled in production. `default_config()` now uses `yaml.safe_load` when
PyYAML is importable (it ships with Agent Zero) and keeps the flat parser only
as a no-dependency fallback, which now omits section headers rather than
fabricating `""` for them.

### Diagnostics: the health check's stall test (P2.12)

`execute.py` reported a false "HEALTH CHECK FAILED" whenever rollouts exceeded
the threshold and no cycle had run, including when run standalone on a machine
where Agent Zero is not running. The stall test now requires positive evidence
that a loop thread is registered; a genuine running-but-stalled loop still
fails. A companion smoke test asserts every key declared in
`default_config.yaml` is read by the plugin's own source, with an explicit
allowlist for keys known to be dead and a reason each.

### Documentation (P2.10)

`AGENTS.md` rewritten as a DOX document: 54,402 -> 11,196 bytes. The
version-by-version history was already in `CHANGELOG.md`. Two phantom modules
(`helpers/audit_log.py`, `helpers/config_loader.py` - the real ones are
`api/audit_log.py` and `api/config.py`) and every hardcoded test count are
gone, so the doc can no longer rot. The fail-open/fail-closed gate matrix, the
single-writer state discipline, the Windows `os.kill` and zombie-liveness
traps, and the `py_compile`-cannot-catch-a-trailing-comma lesson were kept.

### Tests

186 cases, 185 passing. The one failure is the pre-existing
`v1.8.5 SECURITY: apply ... atomic 0600` test, which asserts POSIX permission
bits and cannot pass on Windows. `v1.8.16` (judge throttle pacing) is
timing-sensitive and flakes independently of these changes.

Five new cases: the framework-schema gate, the harvest decision matrix, the
skill-name guard, the ungated-adopt refusal, and the declared-key-has-a-reader
check. The hub_status contract test was **inverted**: it pinned
`requires_auth() is False`, so it asserted the vulnerable behaviour.

---

## [1.8.24] - 2026-09-26

### Fix: a partial upgrade of the install could not adopt an unloadable skill

An auto-adopted proposal stripped the YAML frontmatter from
`usr/skills/scheduled-tasks/SKILL.md`, after which the framework's own
`validate_skill_md` rejected the file and the skill silently disappeared from
the agent. Three independent defects combined to allow it.

#### 1. A completed real-gate verdict was treated as no verdict

`_harvest_real_gate` assumed every `ok=False` meant "could not run" and
adopted. The sidecar that caused the incident was:

```json
{ "status": "done", "gate_passed": false,
  "verdict": { "ok": false, "reason": "insufficient_usable_pairs:1 usable (2 task failures)" } }
```

The worker **ran to completion** and returned a verdict that did not clear the
bar. A completed measurement is evidence, not an absence of evidence.

The matrix is now:

| sidecar | outcome |
|---|---|
| `done` + `ok:true` + `accepted` | adopt |
| `done` + `ok:true` + rejected | quarantine |
| `done` + `ok:false` | **quarantine** (was: adopt) |
| `failed` / stale / unknown status | adopt (no verdict was produced) |

Fail-open is reserved for the case it was written for: the worker never
produced a verdict. The risk is asymmetric — a quarantined proposal sits in
`staging/rejected/` and an operator can re-approve it, whereas an adopted
proposal overwrites a live skill.

#### 2. No stage validated the frontmatter

`validate_proposal`'s nine stages check emptiness, headers, length, example
block, byte/whitespace equality and shrink ratio. **None parse YAML
frontmatter**, so a proposal that dropped it passed every stage. A new stage
runs first and delegates to the framework's own `helpers.skills.split_frontmatter`
(falling back to an equivalent local implementation when that module cannot be
imported, and failing closed if neither can run).

#### 3. The auto-adopt paths took no snapshot and wrote non-atomically

Only `api/adopt.py` snapshotted. Both auto-loop paths and the agent-callable
`skillopt_sleep` tool overwrote the live skill with `Path.write_text` and no
backup — which is why the damage was unrecoverable via `/rollback`, and why a
kill mid-write would leave a half-written skill document. All three now go
through `sleep_runner.adopt_write_skill()`, which snapshots first (refusing to
overwrite if the snapshot fails) and then replaces atomically via a temp file.

#### 4. Root cause: the generator was never told to keep the frontmatter

The gate alone would have blocked *all* evolution. Auditing `staging/`,
**32 of 32** existing proposals were missing their frontmatter block.
`direct_optimizer`'s system prompt said only *"output ONLY the improved skill
document"*, which reads as "rewrite the prose", so the model consistently
discarded the metadata. Fixed at the source:

- the system prompt now requires the frontmatter block be reproduced
  byte-for-byte at the top, and states that compressing the body does not
  license touching metadata;
- `restore_frontmatter()` re-attaches the original block when a model drops it
  anyway. `name` and `description` are identity, not prose, so the optimizer
  should not be rewriting them.

#### Recovery

`usr/skills/scheduled-tasks/SKILL.md` was restored from the tracked source at
`skills/scheduled-tasks/SKILL.md` and verified byte-identical. The proposal
that damaged it is quarantined in `staging/rejected/` and renamed
`.INVALID-FRONTMATTER` so a re-scan cannot pick it up.

#### Tests

Two regression tests added, and both are the incident reproduced:

- `v1.8.24: validate_proposal enforces the framework skill schema` — asserts
  the helper's error strings and that the gate rejects the actual proposal
  that caused the incident, while still accepting a valid rewrite;
- `v1.8.24: harvest fails CLOSED on a completed negative verdict, open only
  when no verdict was produced` — the full five-row decision matrix, and it
  asserts a quarantined proposal does **not** overwrite the live skill.

Suite: 184 tests, 183 passing. The single failure is the pre-existing
`v1.8.5 SECURITY: apply ... atomic 0600` test, which asserts POSIX permission
bits and cannot pass on Windows. `v1.8.16` (judge limiter throttle pacing) is
timing-sensitive and flakes independently of this change.

#### Housekeeping

- `fragments/` is now gitignored wholesale. It is pre-adopt snapshot state —
  the same class as `staging/` and `logs/` — but only two sub-patterns were
  ignored, so ~400 test-generated directories surfaced as untracked noise. A
  snapshot is a copy of a live user skill file and must never be committed.

---

## [1.8.23] - 2026-09-24

### Fix: first real-gate run diagnosed — timeout under-budget + honest sidecar labeling

The first-ever async real-gate run (2026-09-24, `agent-zero-api-handler-routing`)
produced `insufficient_usable_pairs: 0 usable (3 task failures)`: all 6
monologue replays hit the 450s per-task budget. Root causes fixed:

- **Timeout under-budget**: production `config.json` ran
  `replay_real_per_task_timeout_s: 450` — BELOW the harness's own documented
  ceiling of 600 (`default_config.yaml` + `replay_harness` default; ROADMAP
  751 marks 600 as the sanctioned ceiling, beyond it operator-level).
  → `config.json` 450→600; `auto_loop._real_gate_spawn` default 450→600;
  `replay_gate_worker` default 450→600.
- **Timeout diagnosability**: `replay_worker` prints flushed phase marks
  (`init_done` / `context_ready` / `monologue_done` elapsed seconds);
  `replay_harness._real_score` preserves the killed worker's phase marks in
  the timeout `RuntimeError` (was: opaque "timed out after 450s" with zero
  diagnostics). Next timeout will show exactly which phase ate the budget.
- **Honest sidecar labeling**: the spawn-time placeholder
  `gate_passed: true` made completed sidecars read as "gate passed" even
  when the verdict said could-not-measure. Spawn now records
  `pre_gate_recorded: true` + `gate_passed: null`; the worker sets
  `gate_passed = verdict.ok` on done (False on failed). No consumer reads
  `gate_passed` (harvest keys on sidecar presence) — labeling-only change.
  The on-disk 2026-09-24 sidecar retro-fixed (`gate_passed: false`).
- **Re-adoption of a changed skill verified intended**: the 09-24
  re-adoption of `agent-zero-api-handler-routing` was a genuine v2 (adds the
  `methods`/405 handler knowledge from newer rollouts; live SKILL.md matches).
  No-op re-adoption is already rejected by `validate_proposal` stages 5/6
  (byte + whitespace-normalised identical).
- **Test isolation (caught live by the v1.8.19 P4 pollution test)**:
  `t_v121_judge_via_http` wrote its rollout fixtures into the PRODUCTION
  `logs/rollouts` dir; the live inner-loop tick could scan them mid-test
  and enqueue fixture suggestions into production run state (observed
  2026-09-24: 6 files, one tick). The test now sandboxes
  `ab_harness._rollouts_dir` to a tmpdir; leaked production files deleted.
  Related: `t_v188_governance_pause_api` now tolerates a relocated suite
  copy (no `PLUGIN_ROOT.parents[2]` IndexError when the plugin isn't 3
  levels under an A0 root — discovered running the suite from a
  container-local /tmp copy to bypass a 9p-flap D-state wedge).

### Docs

- AGENTS.md §1.8.23; production adoptions.log evidence recorded.

---

## [1.8.22] - 2026-09-24

### Fix: smoke suite no longer bypasses LLM stubs when /a0 is importable (canonical module identity)

- **Root cause (proven 2026-09-23):** the production helpers use two-path imports
  (prefer `usr.plugins.skillopt.helpers.*`, fall back to bare `helpers.*`). When
  `tests/smoke.py` runs with /a0 on sys.path (PYTHONPATH=/a0, `python -m`, or inside
  the A0 framework runtime), BOTH variants import and Python treats them as two
  different module objects: stubs applied to the bare variant are silently bypassed
  and judge / ab-harness / replay-gate tests hit the real configured LLM.
  Reproduction: PYTHONPATH=/a0 produced live minimax-m3 judge calls, a real
  `replay_worker.py` spawn, and 3-6 order-dependent failures per run
  (evidence: workdir logs `skillopt_suite_pypath.log`, `skillopt_suite_pypath_fixed2.log`).
- **Fix (test process only):** `tests/smoke.py` now canonicalizes module identity at
  header time — synthetic namespace packages (`usr`, `usr.plugins`,
  `usr.plugins.skillopt`), eager aliasing of the bare `helpers`/`api`/`tools` top
  packages, and eager aliasing of every bare `helpers.*` leaf into the
  `usr.plugins.skillopt.helpers.*` `sys.modules` slots. Both import forms resolve
  through the module cache to ONE object, so test stubs, `chat_model._CACHE` and
  ab-harness registries stay single-instance in any launch environment. Production
  two-path behavior is untouched; the dual-instance `is not` guards in older tests
  degrade to no-ops under canonical identity.
- **New regression tests (v1.8.22 section, 2 cases):** namespace imports alias the
  bare modules (single identity); a bare-variant `_call_llm` stub is honoured through
  the judge two-path import (no real LLM call).
- **Verification:** full suite 172/172 PASS under PYTHONPATH=/a0 (the previously
  failing condition) and 172/172 plain; zero real LLM calls
  (logs: `skillopt_suite_pypath_fixed3.log`, `skillopt_suite_plain_fixed.log`).
- Version bumped to 1.8.22 in plugin.yaml / plugin.py / hooks.py / execute.py.

### Feature: async real-executor confirmation gate + advisory mock gate (P3 residual)

Closes the adoption-throughput problem (the last P3 residual): the mock
counterfactual's keyword heuristic rejected 5 of 7 real staged proposals as
noise on the 2026-09-23 drain, while the real replay executor was unusable
synchronously (2xN full monologues vs a 600s drain budget).

- **Advisory mock (default):** `replay_mock_enforce: false` — a losing mock
  verdict logs and proceeds; `true` restores the v1.8.19 1.0pp hard bar.
- **Async confirm stage:** direct proposals spawn a DETACHED real-gate
  worker (`scripts/replay_gate_worker.py`) and park pending; the verdict
  lands in a `.md.realgate.json` sidecar; the next drain tick harvests it
  (accepted -> adopt, measured regression -> quarantine, not-run/failed/
  stale -> loud fail-open adopt).
- **Single-flight + frozen tasks:** the sidecar is written before spawn and
  blocks further spawns until harvested; the held-out set is frozen into a
  `.md.realgate.tasks.json` at spawn time (no drift).
- **Budget:** 450s per-task x 3 tasks (worst case 45 min, out-of-thread);
  spawn records `replay_real_gate_cost_cents` (6c) in the budget tracker.
- **Restart safety:** orphaned sidecars cleaned, mirror rehydrated, 4h
  stale window, ctypes `is_running(pid)` on win32.
- **TRAP A/B honored:** worker knobs are CLI-passed (config.json invisible
  in the worker); stage 0.7 runs MOCK whenever the async gate owns real
  confirmation (no double spend).
- Observability: drain summary `pending=`, Loop-card real-gate line,
  per-task `latency_s` in real verdicts.
- Smoke: 11 new t_v1822_* cases; two legacy tests pinned to the retired
  semantics; suite 183/183.

## [1.8.21] - 2026-09-23

### Fix: auto-adopt drains the staging queue (head-of-line + quarantine + mock-backend guard)

- **Head-of-line fix (P2 follow-up):** `_auto_adopt` previously examined
  only the newest staged proposal and returned on the first
  governance-skip or gate-reject - one malformed proposal blocked every
  staged proposal behind it, and throughput was one adoption attempt per
  30-min tick. Now every candidate is attempted per tick, bounded by the
  new `auto_adopt_max_per_tick` (default 5, newest first); a per-candidate
  exception can never stall the drain.
- **Quarantine (proven live 2026-09-23):** a degenerate Sep-22 proposal
  (zero `#` markdown headers) was re-rejected 14x over ~14h while 6
  well-formed proposals waited behind it. New
  `sleep_runner.quarantine_staged_proposal()` / `consume_staged_proposal()`
  move rejected proposals to `staging/rejected/` and adopted ones to
  `staging/adopted/` (official-gate marker sidecar travels along;
  `find_staged_proposals()` scans staging top-level only, so moved files
  can never re-enter the queue). Governance skips stay in staging - the
  skill may become eligible later. The live malformed file was purged to
  `staging/rejected/` at deploy time.
- **Adopted proposals are consumed:** previously an adopted proposal stayed
  in staging and would be no-op-rejected against the now-identical live
  skill forever, polluting failure memory and the audit log each tick.
- **Mock-backend guard (P3 enforcement):** `scripts/run_sleep_cycle.py`
  now fails fast (exit 1, `INFRA_FAILED`) when `official_backend` is
  unset or "mock" unless `--allow-mock-backend` is passed: the mock
  scorer always rejects by construction (proven live 2026-09-23: 40
  tasks "replayed" in 1.1s, held-out 0.2 -> 0.2, tokens_used=0). The
  operator's `use_official_engine: false` exists for exactly this reason;
  the runner can no longer burn a cycle on a guaranteed rejection.
- Smoke: 3 new cases (drain+quarantine+consume end-to-end, governance
  skip stays in staging, runner refuses mock backend); suite 170/170.
  Version parity: plugin.yaml / plugin.py / hooks.py / execute.py.

## [1.8.20] - 2026-09-23

### Fix: gated cycle runner completes its first fully healthy lifecycle

- New `helpers/judge_client.py` + `scripts/run_sleep_cycle.py`: gated
 sleep-cycle runner (ingestion -> judge -> official gate -> summary,
 fail-closed, no ungated fallback). The judge phase samples rollouts
 through the configured judge endpoint and escalates max_tokens on
 JSON parse failures.
- v1.8.20 (P5) zombie-aware liveness: `sleep_runner.is_running` treats
 `/proc/<pid>/stat` state Z as not-running (Linux). An exited-but-
 unreaped detached engine child answers `os.kill(pid, 0)` and spun the
 official-gate poll loop until the full timeout even though
 report.json was already written. Verified live: poll broke 2.1s after
 engine exit instead of 900s.
- v1.8.20 (P6) no-proposal classification: `official_adapter` maps a
 gate-verdict night that staged no proposal (edits=[] under the mock
 backend) to `gate_rejected` (exit 3) instead of INFRA_FAILED.
 Fail-closed: nothing adopted, live SKILL.md untouched, official
 staging preserved.
- Folded in deployed-but-uncommitted hardening: `bridge.py` dual-layout
 import (framework layout with plugin-root fallback); `sleep_runner`
 v1.8.19 P3/RC4 (`validate_proposal` honours caller-resolved
 `ab_harness_enabled`; mock replay gate gets its own bar
 `replay_mock_gate_min_improvement_pp` default 1.0, regressions still
 reject) and P4 (a raising registry config read falls back to the YAML
 defaults); `tests/smoke.py` v1.8.12 follow-up #2 (hermetic `llm_model`
 pin in `t_v121_inner_tick_failing_llm`).
- Live-cycle proof (10:52): judge 3/3 valid labels; engine header shows
 the plugin-local `--claude-home` redirect with 120 sessions / 40 tasks
 harvested; summary `GATE_REJECTED` exit 3.
- Smoke: 1 new case `t_v1820_zombie_aware_is_running`; suite 167/167.
- `execute.py` EXPECTED_VERSION aligned to 1.8.20 (version parity with
 plugin.yaml, v1.8.11 convention).

## [1.8.19] - 2026-09-22

### P3 — real gates (autonomy remediation, all landed)

- Mock replay executor gets its own bar `replay_mock_gate_min_improvement_pp`
  default 1.0 (the 5pp real-data bar rejected every honest proposal at 2.08pp
  noise); mock counterfactual gate (stage 0 A/B harness) is advisory-only by
  default, `SKILLOPT_AB_HARNESS_ENABLED` opts a caller in;
  `validate_proposal` gains `ab_harness_enabled` (the auto-loop previously
  passed a dead guard); `ab_harness._config()` failure-fallback corrected.
- Inner loop: `inner_loop_min_rollout_confidence` 0.4 -> 0.25;
  `inner_loop_skip_retire_after: 3` retires an eternally-skipped rollout with
  a no-op suggestion; consecutive-skip counters persisted to
  `logs/runs/.inner_loop_skips.json`.
- Real replay executor: pairwise per-task failure handling (failed task
  dropped with an error envelope, gate proceeds on usable pairs, hard-reject
  below `replay_min_n`); budget = 2 x max_tasks x per_task_timeout_s,
  documented in `default_config.yaml`.
- Live verification post-restart: `cycles_run` 0 -> 12+, inner loop drained
  the eternal backlog (scanned=50 skipped=50 -> suggested=50 -> scanned=0).
- Live bugs fixed: (a) `cfg.get("budget", {})` returns a STRING - the nested
  `budget:` YAML section is mangled by merged_config()'s flat light-parse -
  which silently DISABLED the daily budget cap and never recorded spend
  (dict accepted only now); (b) a raising framework-registry config read
  bubbled out of `merged_config()` into the bare `config.json` parity
  fallback ({} on a wiped config.json - the silent-spin input); failed
  registry reads now fall back to the YAML defaults.

### P4 — hygiene (all landed)

- Three test-isolation leaks from production state fixed (inner-loop log +
  skip counters to a tmp sandbox; fake rollouts to a sandbox with
  `ab_harness._rollouts_dir` + `sleep_runner.list_rollouts` patched;
  budget-tracker state to a sandboxed `state_dir`).
- Regression guard tests (run last): `t_p4_empty_config_yields_defaults`
  (wiped config.json must still yield full YAML defaults),
  `t_p4_no_fixture_pollution` (fails on fixture-named files in production
  run state).
- Pollution purged: 45 fixture suggestion files, budget test state, 59
  "simulated LLM outage" inner_loop.log rows; debug artifacts archived to
  `logs/runs/_debug_archive_20260922/`.
- Smoke: 166/166.

## [1.8.18] - 2026-09-22

### P0 + P1 + P2.1 — autonomy remediation code-complete (163/163 smoke)

- P0.1-P0.5 config integrity: `webui/config.html` `refresh()` reads via GET;
  `api/config.py` POST guards empty bodies + whitelists known keys; WebUI
  surfaces loop liveness/config state; `agent_init._get_config()` merges
  `default_config.yaml` + `config.json` (+ framework overlay) so empty
  config can never silence the loop; operator intent (`auto_adopt: true`)
  persisted to `config.json`; loop-tick telemetry (`last_tick_at`,
  `cfg_empty_since`).
- P2.1 adoption autonomy: `governance.check_skill_eligible(skill, *,
  auto_adopt=False)` - the pending-approval block is overridden by an
  explicit `auto_adopt: true` (per-skill opt-out / immutable / pause still
  win).
- P1 trigger: per-skill "new rollouts since the persisted
  `last_rollout_count_at_cycle`" instead of the in-memory per-tick delta;
  `_maybe_auto_optin` runs on EVERY tick with new rollouts.
- P1 hotfix during verification: a trailing comma after
  `governance.check_skill_eligible(...)` wrapped the 2-tuple in a 1-tuple
  and broke every `_auto_adopt` unpack (caught by
  `t_v150_governance_auto_loop_skip`; `py_compile` cannot catch
  tuple-wrapping commas - run the smoke suite after every code edit).

## [1.8.17] - 2026-09-19

### Fixed: dockerized replay executor - post-purge seed ordering, tools namespace, timeout ceiling

- `scripts/replay_worker.py` `_run_monologue()`: the dockerized marker
  (`helpers.runtime.args['dockerized']='true'`) is now seeded AFTER the
  `sys.modules` purge in `_setup_syspath()`. The previous pre-purge seed bound
  the orphaned runtime module object; the framework re-imported a fresh runtime
  whose args lacked the marker, so extension calls routed over the RFC bridge
  (rfc_url/rfc_port_http) which has no in-container listener and the monologue
  died on the 50081 dial.
- `scripts/replay_worker.py` `_run_monologue()`: force-resolve the `tools`
  package binding and append the framework tools dir to its `__path__` before
  agent construction. The plugin regular `tools/` package (with `__init__.py`)
  can win the `tools` binding during framework import, after which
  `agent.get_tool()`'s unregistered-tool fallback raised ModuleNotFoundError
  for `tools.unknown`, crashing the monologue. With the fix the fallback module
  is always reachable and the agent loop recovers gracefully from bad tool
  calls (validated: successful probes exercised the fallback and ran to a
  scored envelope).
- `default_config.yaml` + `helpers/replay_harness.py`:
  `replay_real_per_task_timeout_s` default 180 -> 600. Validated real-executor
  monologues run 288-323s in this container, so the 180s ceiling guaranteed
  TimeoutExpired -> fail-closed `real_executor_unavailable` on every real
  replay task, leaving the opt-in executor non-functional despite a healthy
  worker. `replay_real_executor_enabled` stays opt-in (false).

### Validated (direct evidence)

- Held-out replay, `security-scan-untrusted-plugin` task 1, hostile cwd
  (plugin root), 480s budget: score 1.0, outcome success, source model,
  322.9s, rc 0.
- Held-out task 2 (different harvested fixture, same skill), hostile cwd:
  score 1.0, success, 288.0s, rc 0.
- Task 1 again from the benign cwd /a0: score 1.0, success, 315.5s, rc 0 -
  benign-path parity proven; the two earlier benign-cwd timeouts (300s/480s
  budgets) were backend latency variance, not a code regression.
- Full smoke suite: 163/163 PASS.

## [1.8.16] - 2026-09-19

### Added: judge burst protection - limiter + pacing + backoff retries

- `helpers/llm_judge.py`: three-layer standard-library burst protection for
  judge LLM calls, keeping `judge_outcome`'s never-raises contract and
  leaving all non-judge LLM callers (direct_optimizer users) untouched:
  1. **In-flight limiter** - `threading.BoundedSemaphore` caps concurrent
     judge calls at `SKILLOPT_JUDGE_MAX_CONCURRENCY` (default 1 = strict
     serialization; raise to allow bounded parallelism). The judge surface
     is synchronous, so a thread semaphore is the correct stdlib limiter;
     an asyncio semaphore cannot gate cross-thread sync callers.
  2. **Reservation pacing** - `_throttle_wait()` now reserves each call's
     slot start at `last + interval` under a lock (replacing the racy
     read-sleep-write stamp), so consecutive HTTP attempts stay >=
     `SKILLOPT_JUDGE_THROTTLE_S` apart (default 1.5; 0 disables) even when
     N callers race; the `SKILLOPT_JUDGE_THROTTLE_S` env contract and the
     `_throttle_state['last']` shape are unchanged.
  3. **Backoff retries** - `_judge_llm_call()` retries retryable failures
     (HTTP 429 / rate limit, 5xx, timeouts, connection errors) with
     exponential backoff + half-to-full jitter, honoring `Retry-After`
     when the SDK exposes it. Knobs: `SKILLOPT_JUDGE_RETRY_MAX` (default
     3), `SKILLOPT_JUDGE_RETRY_BASE_S` (0.5), `SKILLOPT_JUDGE_RETRY_MAX_S`
     (8.0). Non-retryable errors fail fast; retry exhaustion re-raises the
     last error into the never-raises wrapper -> `{label: None, error}`.
  Retry telemetry is exposed via `llm_judge._retry_stats`
  (attempts / retries / exhausted) and `_reset_burst_state()` provides
  test isolation.
- `tests/smoke.py`: 6 new v1.8.16 cases - limiter serialization under 6
  threads, reservation pacing + bounded parallelism at concurrency 4, 429
  retry with jittered backoff, exhaustion containment, fail-fast + default
  knobs, Retry-After honor. Suite: 163 cases.

## [1.8.15] - 2026-09-18

### Added: hub watchdog background job_loop extension

- New extension `extensions/python/job_loop/_80_skillopt_hub_watchdog.py`
  (class `HubWatchdogExtension`): fired by the framework scheduler's
  `job_loop` tick, it refreshes `logs/hub_status.json` by running
  `scripts/check_hub_status.py` as a non-blocking asyncio subprocess with a
  strict 3s timeout (child killed on timeout), throttled by the state file
  mtime to at most one refresh per 1800s while Hub PR #512
  (agent0ai/a0-plugins Plugin Hub) is pending. On merge, the first stale
  tick records MERGED_INDEXED / MERGED_UNINDEXED, and the
  `/api/plugins/skillopt/hub_status` endpoint surfaces the transition
  within one throttle window — no manual watchdog runs needed.
- Safety contract: stdlib only; broad try/except so background exceptions
  are logged and never propagate into the job loop; loop-safe single-flight
  module flag (deliberately not asyncio.Lock, which binds to the loop that
  first awaits it).
- Verification: acceptance refresh + throttle skip in both directions,
  instrumented timeout path, single-flight spawn count (1 spawn for 2
  concurrent calls), and framework loader discovery
  (`_get_extension_classes('job_loop')` registers the class).
- Version strings aligned to 1.8.15 across plugin.yaml / plugin.py /
  hooks.py / execute.py.

## [Unreleased]

### Test: multi-keyword mock-scorer parity (v1.8.13 follow-up)

- No scorer code change required: the requested normalization (ratio in
  [0,1], consistent for single- and multi-keyword tasks) already shipped in
  v1.8.11 (d532baf) as task-side coverage - covered task tokens divided by
  total task tokens. Implementing the literal keyword-set-size ratio instead
  would reintroduce the original 45->81 dilution defect.
- 2 new smoke tests: exact coverage-ratio ladder (0.6/0.7/0.9/1.0 with noise
  immunity) and end-to-end run_counterfactual(mock) gate acceptance on
  multi-keyword tasks (better-but-larger accepted at +10pp; noise growth is
  rejected_no_lift, never rejected_regression). Suite: 157/157.
- ROADMAP: stale Still-open replay-gate scorer clauses closed out.

## [1.8.13] - 2026-09-18

### Add: hub_status API endpoint (roadmap item 10 tooling)

- New `api/hub_status.py`: GET/POST `/api/plugins/skillopt/hub_status`
  serves the hub-merge / catalog-indexing payload persisted by
  `scripts/check_hub_status.py` in `logs/hub_status.json`
  ({latest: {...}, history: [...]}).
- Fresh payload (file mtime <= 3600s) is returned as-is with no
  subprocess spawn.
- Stale or missing payload triggers exactly one watchdog refresh under
  a hard 3s timeout (asyncio subprocess, killed on overrun; the server
  event loop is never parked on network timeouts). Concurrent requests
  share a single refresh via a process-wide guard; waiters poll for
  freshness instead of spawning duplicates.
- Refresh failure (spawn error, timeout, file still missing/stale)
  returns HTTP 500 with a structured JSON body
  ({status: ERROR, message: ...}); a watchdog-reported ERROR payload
  is served as honest data with HTTP 200.
- Auth/CSRF relaxed for this read-only, non-sensitive public status,
  mirroring the framework's own read-only GET endpoints. Flask is
  imported lazily; CI (no flask) gets a dict error fallback carrying
  an explicit http_status field.
- 5 new deterministic smoke tests (suite now 155 cases): handler
  contract, fresh no-spawn, stale single-refresh, structured 500,
  timeout kill.

## [1.8.12] - 2026-09-16

### Change: optimizer / target / judge models follow the active Agent Zero chat model

- New `helpers/chat_model.py`: the `chat` sentinel (new default for
  `optimizer_model`, `target_model`, `judge_model`) resolves at call time
  to the framework `_model_config` active preset chat slot - provider,
  model, api_base (preset, else `conf/model_providers.yaml`) and api_key
  (`API_KEY_<PROVIDER>` env/dotenv). Cached 60s; `SKILLOPT_A0_ROOT`
  override for hermetic tests; never raises.
- `minimax-m3` retired as an implicit default everywhere; still available
  via explicit configuration or env.
- Sentinel resolution wired into: `direct_optimizer._call_llm` (with
  connection override), the judge model chain, the A/B HTTP judge
  binding, inner-loop suggestions (unresolved model falls back to the
  stub path), and the official adapter `--model`.
- 4 new smoke cases (`t_v1812_*`); suite: 150/150.

## [1.8.11] - 2026-09-15

### Fix: mock-scorer keyword dilution (45->81 kw reject)

- replay_harness._mock_score() overlap is now TASK-side coverage: the fraction
  of the task's salient tokens covered by at least one directive keyword.
- Size-invariant: adding keywords never lowers a task score (the diagnosed
  45->81 keyword-dilution reject); pure noise keywords cannot raise it.
- Outcome base semantics unchanged; new size-invariance smoke test
  (t_v1811_scorer_size_invariant). Smoke suite: 145/145.

### Fix: judge burst throttle (429 bursts during batch labelling)

- judge_outcome() now spaces consecutive judge LLM calls at least
  SKILLOPT_JUDGE_THROTTLE_S seconds apart (env override, default 1.5, 0
  disables) via a module-level monotonic gate (_throttle_wait).
- Never-raises contract unchanged; new spacing smoke test
  (t_v1811_judge_throttle).

## [1.8.10] - 2026-09-15

### Fix: Sleep engine mined 0 sessions from bridged rollouts (claude-home wiring)

- `launch_sleep_subprocess()` now appends `--claude-home <bridge_root>` to the
  engine command whenever the plugin-local bridge cache differs from
  `~/.claude`. The engine previously read the real (stale) `~/.claude` home,
  found none of the bridged sessions and harvested 0 sessions every cycle.
  Verified live: harvest 14 sessions -> 14 tasks -> gate -> staging.

### Fix: atomic rollout writes (reader-race hardening)

- `write_rollout()` now writes to a temp file and `os.replace()`s it into
  place, so concurrent readers (judge labelling, harvest, reward scoring)
  never observe a partially written rollout JSON.

### Fix: framework shepherd prompts no longer harvested as tasks

- The rollout harvester skips user messages starting with
  `[chat_shepherd]` - framework recovery nudges, not user tasks. They
  poisoned labels: the judge rated them failure/partial while the keyword
  heuristic called every non-error response success (0-10% agreement).

### Live gated cycle: first end-to-end run (fail-closed reject, artifact-diagnosed)

- Full pipeline ran live: the direct optimizer produced a real minimax-m3
  proposal for `security-scan-untrusted-plugin` from 7 live rollouts
  (6 success / 1 failure), and the local replay gate REJECTED it
  (`rejected_regression`, mean 0.4603 -> 0.4541).
- Verdict root cause: the deterministic mock scorer computes
  overlap = hits / len(directive_keywords); the proposal grew the keyword
  surface 45 -> 81, halving overlap for keyword-hitting tasks and forcing a
  spurious regression verdict. The gate behaved correctly fail-closed; the
  scorer limitation documented in replay_harness.py is the live follow-up.
- Judge labelling: 10/19 rollouts labelled; the judge endpoint truncates
  responses under burst load (empty-body parse errors), so label passes
  converge over sequential re-runs rather than one burst.

- Smoke: 144/144 - the version-alignment test now derives the
  expected version from plugin.yaml instead of pinning 1.8.9, and
  execute.py EXPECTED_VERSION was bumped with the release.

## [1.8.9] - 2026-09-15

### Fix: zero rollouts on every live turn (harvester content extraction + attribution)

- `_flatten_content()` now also prefers the real framework
  content-dict keys `user_message` and `ai_response`. Runtime-observed
  shapes: user turns arrive as `{'user_message': ...}` dicts and final
  responses as `{'ai_response': ...}` dicts; the old preference tuple
  (`text`, `content`, `tool_result`, `message`, `preview`) never
  matched them, so the harvester flattened every live turn to an empty
  string and early-returned on `if not user_msg` — zero rollouts
  written since v1.7.0 despite green unit tests.
- `SkilloptHarvestRollout.execute()` now forwards the framework agent
  instance (`kwargs['agent']`) into the module-level `execute()`,
  restoring authoritative `skill_used` attribution from the
  `loaded_skills` ledger on live dispatches.
- Verified offline: 10/10 shape cases against runtime-observed
  payloads plus a full end-to-end hook dispatch producing a complete
  rollout record (task, skill_used, outcome, trajectory, reward,
  awaiting_suggestion). Verified live: 3 rollouts written by the
  persistent runtime within 30 minutes of the fix, one with correct
  skill attribution.
- Folded in the uncommitted v1.8.8 dashboard CSRF follow-up: `call()`
  fetches `/api/csrf_token`, sends `x-csrf-token`, and retries once on
  403 after token rotation; head cache-bust bumped to `cb=3` (smoke
  test updated accordingly).
- Test hermeticity: the two C3 `/adopt` tests now temporarily hide real sleep-run logs (restored in finally) so the /adopt gate never reads stale held-out evidence from the live runtime.
  held-out source so they pass regardless of real sleep logs on disk.

## [1.8.8] - 2026-09-14

### Open question 5: one-click dashboard pause/resume (ROADMAP)
- Governance helpers: `pause_skill(skill, hours=24)` writes the
  `.skillopt.pause_until` skip marker (epoch or ISO-8601, both
  supported since v1.8.6) and `resume_skill(skill)` removes it;
  both are idempotent and log to governance.log.
- New endpoint `POST /api/plugins/skillopt/governance_pause`
  (api/governance_pause.py): validates skill names (path traversal
  rejected), routes to the helpers, returns `{ok, skill, hours}`.
- Dashboard JS: `governancePause(skill, hours)` and
  `governanceResume(skill)` methods delegating to the endpoint.
- Config UI (Governance card): Pause 24h / Resume buttons per
  opted-in skill, a `Paused: N` stat, and a per-skill paused chip,
  reading the `paused` list already surfaced by governance_status
  (no backend change needed).
- page-head cache-bust bumped (cb=2).
- Tests: +2 v1.8.8 smoke tests (pause/resume roundtrip + API
  validation; dashboard UI wiring). Suite: 144 tests.
## [1.8.7] - 2026-09-14

### Open questions 2 + 3: failure-memory backups + budget soft tier (ROADMAP)
- Budget two-tier cap: `budget.soft_warn_pct` (default 80) adds a soft
  warning tier in `BudgetTracker.record_spend` (`soft_warning` flag) and
  `get_status` (`soft_threshold_cents`, `soft_triggered`); the hard gate
  at 100% of the daily cap is unchanged. auto_loop logs the soft warning
  into the cadence log; `soft_warn_pct: 0` disables the soft tier.
- Failure-memory rolling backup: every successful `record_failure` now
  writes a timestamped per-skill snapshot plus an atomic `latest.json`
  mirror under `logs/runs/failure_memory_backups/<skill>/`, keep-last-N
  via `failure_memory_backup_keep` (default 5; env `SKILLOPT_FM_BACKUP_KEEP`
  wins; 0 disables). The backup path is returned as `backup`; restore =
  copy a snapshot back over the local store. Never crashes the record path.
- New smoke tests: budget soft tier (warn-once-at-threshold, hard gate,
  disable switch) and backup rotation (keep-3-of-5, latest.json mirror,
  keep-0 kill switch). Version pins aligned at 1.8.7.

## [1.8.6] - 2026-09-14

### Governance and gate: item 8 per-skill policy scopes (ROADMAP)
- Policy scope overlay: a per-skill `.skillopt.policy.json` now constrains
  loop edits with three user keys: `allowed_fragments` (only these fragment
  ids may change), `max_verbosity_delta_ratio` (relative growth cap vs
  current), and `forbid_patterns` (banned literal substrings).
- NEW stage 0.75 per-skill policy scope gate in
  sleep_runner.validate_proposal (runs only when the skill has named
  fragments and a policy file exists; unchanged fragments are skipped; a
  user constraint, not a quality judgment).
- auto_loop now always passes skill_name into validate_proposal so the
  scope gate can address the skill; stage 0 stays unconditional and
  run_paired_test keeps returning can_run=False when no judge is
  configured (advisory-only under default configs).
- Governance: .pause_until markers now accept ISO dates (YYYY-MM-DD) in
  addition to epoch seconds; list_governed walks pauses; the status
  snapshot gains a stuck block (skills with consecutive rejects and no
  adoption).
- Test tooling: boundary-safe policy-scope fixtures (mutations stay inside
  fragment interiors so both parses segment identically).
- Verification: smoke 139/139, v1.8.4 security block 3/3, policy-scope
  sanity 4/4, framework-runtime compile OK.

## [1.8.5] — 2026-09-12

### Security: skillopt_setup can no longer write plaintext credentials
- NEW helpers/setup_env.py — credential-safe env-file builder/sanitizer:
mapped source vars are written as ${SOURCE_NAME} references (resolved at
consumption time via the shared sleep_runner._expand_env: os.environ
first, then the v1.8.4 usr/.env fallback). Only non-secret literals
(api version, auth mode, backend) are written verbatim.
- Sanitize pass: any plaintext credential value found in an existing
.skillopt-env is rewritten to a reference (provenance match against
visible source variables when possible, self-${KEY} otherwise);
merge-preserving so unrelated keys survive re-runs.
- Atomic write (tmp + os.replace) with 0600 permissions.
- Dry-run output now contains key NAMES and resolution booleans only —
it previously echoed full plaintext values into the chat transcript.
- Smoke suite: +2 v1.8.5 security tests (build/sanitize with provenance
and idempotency; apply end-to-end with merge preservation and 0600).

## [1.8.4] — 2026-09-12

### Security: plaintext credentials removed from the plugin tree
- Optimizer credentials migrated from `logs/runs/.skillopt-env` (plaintext)
  to the framework's `<project>/usr/.env` (chmod 600, outside the plugin
  repo, loaded by the framework at startup). `.skillopt-env` now contains
  `$VAR`/`${VAR}` references only — the file is kept because 12 code paths
  require it to exist.
- `sleep_runner._dotenv_fallback()`: portable `<project>/usr/.env` parser
  wired into the shared `_expand_env`, so indirection resolves in the live
  backend (a pre-migration process) and in bare-python subprocesses
  (replay workers) that lack the names in `os.environ`. No hardcoded
  paths; the v1.8.1 Windows-portability contract is preserved.
- `direct_optimizer`: the no-API-key error now points to `usr/.env`.
- Smoke suite: +3 tests (fallback parser, indirection expansion with a
  monkeypatched fallback, live env-file reference check).

## [1.8.3] — 2026-09-12

### Fixed
- **Extension hooks were silently never dispatched by the live runtime — the root cause of the v1.8.2 dashboard showing zeros.** The A0 extension loader (`helpers.modules.load_classes_from_folder`) only discovers `Extension` subclasses, but all four skillopt hooks were bare module-level `execute()` functions: present in the tree, discoverable by nothing. No auto-loop start, no rollout harvest, no loop-error warnings, no discovery banner ever fired in a live backend, while the smoke suite kept passing because it calls `execute()` directly and masked the dead wiring. Each hook file now also defines a delegating `Extension` subclass (SkilloptAutoLoopStarter, SkilloptLoopErrorWarn, SkilloptHarvestRollout, SkilloptDiscoveryBanner) that the loader discovers and dispatches; the module-level `execute()` implementations are unchanged and remain the single source of truth, so direct callers (and the smoke suite) keep working.
- **Process-wide thread guards in the auto-loop starter.** The loader re-imports the extension module per profile/project, resetting the module-level `_watchdog_thread` global, so duplicate `skillopt-auto-loop` and `skillopt-auto-loop-watchdog` threads could accumulate across contexts. Both start paths now dedupe by live thread name before spawning.

## [1.8.1] — 2026-08-11

**Windows portability — every hardcoded `/a0/...` container path fixed.** The plugin was
silently dead on Windows installs: 10 consumers resolved files/dirs under a Linux path that
never exists there, so every opt-in path silently saw an empty skills list, a missing
last-error file, and a missing workdir. v1.7.0/v1.8.0 behavior is preserved byte-for-byte on
container layouts (the container path is kept as a secondary candidate everywhere it applied).

### Fixed
- `helpers/sleep_runner.py` — `a0_skills_dir()` now derives from `plugin_root()`
  (`<project>/usr/plugins/skillopt` → `<project>/usr/skills`) instead of the hardcoded
  `Path("/a0/usr/skills")`; honors the `SKILLOPT_SKILLS_DIR` override. `plugin_root()`
  resolves via the framework's `plugins.plugin_root()` (cached 60s), falling back to this
  file's own location (4 parents up from `helpers/`) when the framework module is absent.
- `helpers/auto_loop.py` — `rotate_log_if_large()` recreates an empty live log after rotating
  (append-mode writers already recreated the file; tailing readers did not).
- `helpers/governance.py` — the `plugin_root()`- and `budget`-fallback imports resolve via
  the framework path first (portable), falling back to `helpers.*` last.
- `helpers/inner_loop.py` — the `sleep_runner` fallback import resolves via the framework
  path first (portable).
- `helpers/official_adapter.py` — the `sleep_runner` fallback import resolves via the
  framework path first (portable); `_resolve_skill_path()` resolves via
  `sleep_runner.a0_skills_dir()` (honors `SKILLOPT_SKILLS_DIR`) instead of a hardcoded
  `<a0>/usr/skills`.
- `helpers/ab_harness.py`, `helpers/replay_harness.py` — fallback imports resolve via the
  framework path first (portable).
- `extensions/python/monologue_start/_40_skillopt_warn.py` — the last-error file resolves from
  this extension's own location first (portable); the container path is a secondary
  candidate. The warning banner now fires on Windows installs.
- User-facing strings: the staged-proposal env-file instructions
  (`helpers/direct_optimizer.py`) and the `api/adopt.py` docstring reference plugin-relative
  paths instead of `/a0/usr/plugins/skillopt/...`.

### Verified
- 50 files `py_compile` OK; 133/133 smoke tests pass (11 `t_v18_*` new; the full
  deterministic suite, no LLM/network); 13/13 targeted v1.8.1 probes pass
  (two-path imports, `_expand_env`, `is_running`, gate marker, rotation, merged-config);
  `execute.py --self-check` Health check PASSED (manifest version 1.8.1); 8/8 skills
  resolve on the live install.

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
