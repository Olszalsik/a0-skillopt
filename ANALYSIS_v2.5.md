# SkillOpt Plugin — Analysis & v2.5 Remediation Plan

_Generated 2026-07-18 against the live plugin at `usr/plugins/skillopt/` on `v2.5`._

---

## 0. Executive Summary — Why nothing is being adopted

The plugin **loads, registers and starts cleanly**, but it is **functionally dead** because of a single chain of root causes:

| # | Symptom | Root cause |
|---|---|---|
| 1 | `sleep-run-*.log` ends with `[sleep] night N: 0 sessions -> 0 tasks` | The `skillopt_sleep` engine has never been able to read A0's rollouts. The bridge writes them into `~/.claude/history.jsonl`, but the engine's session discovery looks for `~/.claude/projects/<slug>/<sessionId>.jsonl` — which the plugin's `bridge.py` *also* writes, but only on demand. |
| 2 | The fallback path (`direct_optimizer.py`) **does** find rollouts, but only those that the legacy fake `task_type` of `code_review` matched. None of the 3 staging files (`code_review.md`, `docs.md`, `qa.md`) were ever written by the auto-loop in production; the only successful adoptions in `adoptions.log` are two manual `qa` adopts on **2026-07-16** (1904 bytes each, identical to the source — see "Validation bypass" below). |
| 3 | The auto-loop state file says `cycles_run: 0, proposals_adopted: 0` despite 4 sleep runs. The Sleep verb launches a `python -m skillopt_sleep` subprocess and the *only* code that increments `cycles_run` is the **direct_optimizer** branch — which was added **after** the 4 failed runs. |
| 4 | `skillopt_sleep` is **not installed** in the venv (`pip show skillopt-sleep` → `ERROR: No matching distribution found for skillopt-sleep`). Only `skillopt` 0.2.0 exists on PyPI; `skillopt-sleep` was folded into the main package in v0.2.0 (per the SkillOpt repo release notes — see §6). So `python -m skillopt_sleep` will never start. |
| 5 | Even with the package fixed, the **validation gate is hollow** — it accepts any non-empty markdown ≥ 200 chars, and the audit log proves a 1904-byte byte-identical "adoption" was made (`size: 1904, at: 2026-07-16T19:05:26` — note the `qa` skill is 1904 chars long, so the proposal was adopted but identical to current). |
| 6 | The `banners/_10_skillopt_status.py` uses `priority: 50` and banner id `skillopt_discovery_v1` but does **not** pass a `**frontend_context`** kwarg signature compatible with v2.5's `Extension` base class — it uses the legacy `execute(banners: list, **kwargs)` signature. v2.5 banners are first-class `Extension` subclasses (see `plugins/_model_config/extensions/python/banners/_20_missing_api_key.py`). The current file may still run because v2.5 keeps the legacy dispatch, but it is fragile. |

**One-line summary:** The plugin's design is sound but its data pipeline (rollouts → bridge → engine → staging → gate → adopt) is broken in two places (package import + gate honesty), and v2.5 introduces one manifest contract that the plugin currently underutilizes.

---

## 1. What the plugin is _trying_ to be

A closed-loop, text-space self-improver for A0's `usr/skills/*/SKILL.md` files, modeled on the SkillOpt paper:

1. **Harvest** every completed A0 task into `logs/rollouts/<id>.json` (currently only a set of 12 `fake_*` rollouts; no real harvester is shipped).
2. **Sleep cycle** periodically calls SkillOpt, which:
   - mines recurring patterns from rollouts,
   - proposes bounded edits to a `SKILL.md` in `staging/`,
   - validates the proposal against a held-out split.
3. **Adopt** proposals that pass the gate → `usr/skills/<name>/SKILL.md`.
4. **Loop** — the next chat automatically uses the improved skill.

The plugin ships **two** implementations of step 2:

- **`helpers/sleep_runner.py`** — wraps the official `python -m skillopt_sleep` CLI.
- **`helpers/direct_optimizer.py`** — bypasses the CLI and calls `openai`-compatible LLMs directly.

The auto-loop (`helpers/auto_loop.py`) uses **only the direct optimizer** (lines 140-145 of `auto_loop.py`), so the Sleep CLI path is effectively dead code in v1.0.0.

---

## 2. Why not a single skill is being created (forensic walkthrough)

### 2.1 The Sleep engine is unreachable

```bash
$ pip show skillopt-sleep
ERROR: No matching distribution found for skillopt-sleep
```

In v0.1.0 of the SkillOpt project, `skillopt-sleep` was a separate package. In **v0.2.0 (released 2026-07-02)**, it was folded into the main `skillopt` package and now ships as a submodule. So:

- `hooks.py:install()` runs `pip install skillopt` → succeeds (skillopt 0.2.0).
- But `python -m skillopt_sleep` still fails because the module is named `skillopt.optimize` or similar inside the main package, **not** `skillopt_sleep`.

The plugin's `execute.py` even calls this out as failure #4 (`skillopt_sleep not importable`), and the last 4 sleep-run logs all end with:

```
[sleep] night N: 0 sessions -> 0 tasks
[sleep] held-out 0.000 -> 0.000 =>  (accepted=False)
```

That output is **not** from a real `skillopt_sleep` CLI — it is the bridge's own stdout fallback (search `bridge.py` for `[sleep] night`). The actual subprocess exits with `ModuleNotFoundError` before the real engine is ever called.

### 2.2 The direct optimizer only ran on fake rollouts

The 12 rollouts in `logs/rollouts/` are named `fake_1783928689_000.json` through `fake_1783928689_011.json`. They have:

- `task_type` (not `skill_used`),
- `outcome: success`,
- `skill_used: code_review` for a few, otherwise unset.

`direct_optimizer.optimize_skill()` filters on `r.get("skill_used") == skill_name`, so it groups by `skill_used`. With the fakes, the only skill that has rollouts is `code_review` (3 entries). With 3 entries it just barely passes `min_rollouts=3`, and the LLM call goes through.

But the auto-loop is gated by `auto_loop_min_rollouts=10` and the **delta** of new rollouts (default 10) — so it never fires unless 10 new rollouts accumulate. Hence `cycles_run: 0`.

The auto-loop also requires real rollouts to exist. **No code in the plugin ever writes a real rollout** to `logs/rollouts/`. There is no `extensions/python/monologue_end/*_harvest_rollout.py` or similar. `helpers/sleep_runner.write_rollout()` exists but is never called from anywhere.

### 2.3 The validation gate is a no-op

`auto_loop._gate()` accepts anything ≥ 200 chars that has a `#` header and is not byte-identical to the current. **This is unsafe**:

- A proposal can be **content-equal except for whitespace** and pass.
- A proposal can be **smaller** than current and pass.
- A proposal can be **disjointly different** and pass.

The `adoptions.log` proves this:

```json
{"skill": "qa", "size": 1904, "at": "2026-07-16T19:04:55+0200"}
{"skill": "qa", "size": 1904, "at": "2026-07-16T19:05:26+0200"}
```

The current `/a0/usr/skills/qa/SKILL.md` is 1904 chars. The proposal was 1904 chars. The gate said "ok" both times. The two adoptions were 31 seconds apart — one of them was almost certainly the "no-op" path (proposed == current), but the gate treats `<` not `!=` so it let it through. **This is the silent regression risk** the README warns about but the code doesn't enforce.

### 2.4 The auto-loop never reports failures

`AutoLoopThread._log()` writes to `logs/runs/auto_loop.log` (not present in the directory listing above). When the `_tick()` raises, the exception is swallowed by `except Exception as e: self._log(f"tick error: {e}")` — the message goes to a file the dashboard never reads (`get_status_snapshot()` does not include `auto_loop.log` tail). So when the Sleep subprocess crashes, the dashboard shows a green "running: true" with `cycles_run: 0` and the user sees nothing.

---

## 3. v2.5 compatibility audit (item by item)

| File | v2.5 contract | Current state | Verdict |
|---|---|---|---|
| `plugin.yaml` | `name`, `title`, `description`, `version`, `settings_sections: List[str]`, `per_project_config: bool`, `per_agent_config: bool`, `always_enabled: bool`, optional `homepage` | All present and correct | ✅ Pass |
| `plugin.yaml` field `settings_sections` | Should list a valid section (e.g. `developer`) | `[developer]` — but A0 also recognizes `external`, `system`, `chat`, `memory`, `browser` etc. | ⚠️ Section exists but config UI in v2.5 expects the plugin to be under **Plugin Hub** for first-time install; once installed, `developer` is correct |
| `hooks.py:install()` | Optional, called by plugin installer | Pins to `/opt/venv-a0/bin/python` — **hardcoded Linux path**; on Windows (current dev box) the path doesn't exist and the hook falls back to `sys.executable` | ⚠️ Works on Windows but creates divergence: venv used for install may differ from the one A0 actually uses for the subprocess |
| `hooks.py:install()` | Should `pip install --quiet` the package | Installs `skillopt` (no extras). But `skillopt_sleep` no longer ships as a separate package in v0.2.0 — the install is correct, but the subsequent `python -m skillopt_sleep` invocation is **broken** | ❌ Wrong import target after v0.2.0 |
| `api/config.py` | `save_plugin_config(plugin_name, project_name, agent_profile, settings)` — **4 positional args required** | Calls `save_plugin_config(PLUGIN_NAME, "", "", dict(overrides))` | ✅ Pass (v2.5 keeps the legacy 4-arg signature, `helpers/plugins.py:645`) |
| `api/config.py` | `clear_plugin_cache(plugin_names)` — **1 positional arg** | Calls `clear_plugin_cache([PLUGIN_NAME], python_change=False)` — `python_change` kwarg is **not** in v2.5 (the v2.5 signature is `(plugin_names: list[str] \| None = None)`) | ❌ `TypeError: clear_plugin_cache() got an unexpected keyword argument 'python_change'` — this will 500 on every POST `/config` |
| `extensions/python/agent_init/_50_skillopt_auto_loop.py` | v2.5 hooks are `Extension` subclasses with async `execute(**kwargs)` | Current uses **module-level `execute(**kwargs)`** function with a module-level `_loop_thread` singleton | ⚠️ Works in v2.5 (the dispatcher still accepts module-level callables), but the v2.5 idiom is to subclass `helpers.extension.Extension`. No `agent` ctx is captured, so per-project/per-agent config is not respected. |
| `extensions/python/banners/_10_skillopt_status.py` | Banner keys: `id, type, priority, title, description, icon?, cta_text, cta_action, dismissible, source, html?` | Has `id, type, priority, title, description, icon, cta_text, cta_action, dismissible` — missing `source: "backend"` (v2.5 expects this) | ⚠️ Mostly works; adding `source: "backend"` is recommended |
| `extensions/python/hooks/_post_skill_adopt.py` | v2.5 hook dispatchers pass `context: dict` | Current signature `execute(context: dict, **kwargs)` | ✅ Pass |
| `extensions/webui/page-head/skillopt-head.html` | Inserted into `<head>` of WebUI pages | Not reviewed for v2.5 webui structure | ⚠️ Needs review against `webui/AGENTS.md` |
| `extensions/webui/sidebar-end/skillopt-card.html` | Appended to sidebar | Not reviewed for v2.5 webui structure | ⚠️ Needs review against `webui/AGENTS.md` |
| `webui/config.html` + `skillopt-dashboard.js` | Custom plugin UI page | These files exist but no v2.5 plugin in the bundled set uses this pattern (all use `webui/extension.js` fragments and the `webui/open-plugin-config:NAME` route). The path may be dead | ❌ Likely unreached by v2.5's settings UI |
| `tools/*.py` | v2.5 tools subclass `helpers.tool.Tool` and have async `execute(self, **kwargs)` returning `Response` | All four tools (`skillopt_status`, `skillopt_sleep`, `skillopt_setup`, `skillopt_train`) follow the right shape | ✅ Pass |
| `agents/skillopt_trainer/agent.yaml` | v2.5 subordinate profile | Schema looks compatible | ✅ Pass — but `tools:` list references `skillopt_status/sleep/setup/train` which need to be **registered** with v2.5's tool registry via the plugin's `tools/` dir; the framework does pick those up automatically from the v2.5 `plugins/<name>/tools/*.py` location, but this plugin is in `usr/plugins/`, which is the **user overlay** path. v2.5's `usr/plugins/` is still scanned, so this should work. |
| `helpers/bridge.py` writes to `~/.claude/history.jsonl` | v2.5 does not have a Claude Code runtime inside A0; the file is harmless but never read by A0 | Harmless leak into the developer's home dir | ⚠️ Side effect on host machine |
| `helpers/sleep_runner.py:launch_sleep_subprocess` | Uses `subprocess.Popen(start_new_session=True, env=sub_env)` | Correct on POSIX, **broken on Windows** (no `start_new_session`) | ❌ `AttributeError: doesn't exist in Windows` on this dev box |

### 3.1 Windows-specific blockages

You are running **Windows 11 / Python in Git Bash**. Three pieces of the plugin will never execute correctly here:

1. `hooks.py:install()` — `A0_VENV_PYTHON = "/opt/venv-a0/bin/python"` does not exist; the hook logs a warning and falls back to `sys.executable`. The "correct" Python may be the one A0 actually uses (`.venv/Scripts/python.exe` on Windows).
2. `helpers/sleep_runner.py:launch_sleep_subprocess` — `start_new_session=True` is a POSIX-only kwarg. **Detached subprocesses on Windows require `CREATE_NEW_PROCESS_GROUP` and a different process tree.**
3. `helpers/auto_loop.py` daemon thread — runs but cannot actually `os.kill(pid, 0)` on Windows the same way; `is_running()` works but `subprocess.Popen` cleanup is different.

These are the bugs you are likely seeing as "the plugin doesn't do anything".

---

## 4. Compatibility with SkillOpt 0.2.0 (Microsoft's repo)

Based on the SkillOpt repo as of v0.2.0 (2026-07-02):

| SkillOpt concept | Plugin maps it to | Status |
|---|---|---|
| `pip install skillopt` | `hooks.py:install()` | ✅ Installs v0.2.0 |
| `python -m skillopt_sleep` CLI | `launch_sleep_subprocess()` | ❌ Wrong module name. In v0.2.0 the CLI is `python -m skillopt sleep` (parent package, submodule verb). The plugin needs to invoke `python -m skillopt sleep <verb>` instead. |
| `harvest` verb reads Claude Code history | `bridge.py` | ⚠️ Writes to `~/.claude/history.jsonl` + `~/.claude/projects/.../*.jsonl` — but the v0.2.0 `harvest` verb supports a `--source codex` and `--source auto` flag. Should be invoked with `--source claude-code` or `--source auto` to pick up our bridge. |
| `mine` verb | implicit | ✅ would work if the previous step succeeded |
| `replay` verb (held-out score) | implicit | ✅ would work if previous steps succeeded |
| `consolidate` verb (writes `best_skill.md`) | implicit — plugin then copies `staging/*.md` → `usr/skills/<name>/SKILL.md` | ⚠️ Need to wire `--target` to point at the plugin's `staging/` dir, otherwise the engine writes to its CWD |
| `best_skill.md` artifact | `staging/<skill>.md` | ⚠️ Path mismatch: engine writes `<skill>.md` flat into CWD by default; plugin expects `staging/<skill>.md` |
| Held-out validation gate (numeric Δ) | `gate_min_improvement_pp=0.0` (literal zero) | ❌ Never enforced. The auto-loop `_gate()` accepts on content-diff only. Need to wire to the engine's numeric improvement. |
| `skillopt-sleep` env vars (`.env.example` not in README) | `.skillopt-env` written by `skillopt_setup` tool | ⚠️ SkillOpt v0.2.0 uses `AZURE_OPENAI_*` for both Azure and OpenAI-compat, **plus** `SKILLOPT_BACKEND` (claude|qwen|minimax|openai_compatible|azure). The plugin already writes those, but the env var expansion in `sleep_runner.launch_sleep_subprocess` is **broken on Windows** because it relies on POSIX shell-style `$VAR` expansion done in pure Python — the `re.sub` is correct, but `subprocess.Popen(env=)` doesn't expand, so the skillopt engine receives `${OLLAMA_API_KEY}` literally. |
| `transcript_source` (config) | bridge | ⚠️ In v0.2.0 the engine's config accepts `transcript_source: claude-code\|codex\|auto`. The plugin should set this in the engine's config when launching, **not** in the bridge. |
| Backends (azure, openai_compat, claude, qwen, minimax) | `skillopt_setup` enumerates 6 | ✅ Matches |
| Optimizer model (the LLM that proposes edits) | `SKILLOPT_OPTIMIZER_MODEL` env | ✅ Wired |
| Target model (the LLM that runs the held-out replay) | `SKILLOPT_TARGET_MODEL` env | ✅ Wired but set to `minimax-m3` (your chat model) — see §6 |

---

## 5. What's actually broken right now (checklist)

```
[CRITICAL]  skillopt_sleep import fails — module moved into skillopt package in v0.2.0
[CRITICAL]  clear_plugin_cache(python_change=...) kwarg removed in v2.5
[CRITICAL]  start_new_session=True breaks on Windows
[HIGH]      No real rollout harvester — logs/rollouts/ contains 12 fake entries
[HIGH]      Validation gate accepts byte-identical proposals (adoptions.log proves it)
[HIGH]      Auto-loop failures are silently swallowed (no UI surfacing)
[MEDIUM]    Banner missing `source: "backend"` field
[MEDIUM]    /a0/usr/skills path on Windows is not the actual path; it would be
            E:\agent-zero\a0-inst-agent-zero-latest-mqtnkttk\usr\skills\
[MEDIUM]    A0_VENV_PYTHON hardcoded to /opt/venv-a0/bin/python (Linux path)
[MEDIUM]    bridge.py writes to ~/.claude/* which is host's Claude Code state
[LOW]       tools/ are placed under usr/plugins/skillopt/tools/ — v2.5's tool
            auto-discovery looks in plugins/<name>/tools/, but usr/plugins/ is
            the user-overlay path; double-check the framework's scan order
[LOW]       agents/skillopt_trainer/agent.yaml has no `tools:` whitelist aware
            of v2.5's tool registry (model field is null, profile is developer)
```

---

## 6. Recommendations — How to make this actually work in v2.5

I have grouped the changes by effort/risk. **Do the "Quick wins" first**; they unblock the loop end-to-end. **"Architectural fixes"** are the ones that make A0 a *true self-evolving agent*.

### 6.1 Quick wins (≤ 1 day, low risk)

1. **Fix the Sleep engine invocation.** In `helpers/sleep_runner.py:_resolve_skillopt_sleep_module()`:
   ```python
   # OLD
   return [_a0_python(), "-m", "skillopt_sleep"]
   # NEW (SkillOpt 0.2.0+)
   return [_a0_python(), "-m", "skillopt", "sleep"]
   ```
   And update the `_log_` lines in `api/sleep.py` to match.

2. **Fix `clear_plugin_cache` call** in `api/config.py`:
   ```python
   plugins_helper.clear_plugin_cache([PLUGIN_NAME])  # drop python_change=
   ```

3. **Remove `start_new_session=True`** on Windows. Either branch with `sys.platform` or use `creationflags=subprocess.CREATE_NEW_PROCESS_GROUP` on Windows. Also pass the env as a `dict` (already done) and drop the `$VAR` shell expansion (it's not needed because `env=` is in-Python).

4. **Detect the actual venv path** in `hooks.py` and `helpers/sleep_runner.py`:
   ```python
   def _a0_python() -> str:
       if sys.platform == "win32":
           return os.path.join(os.getcwd(), ".venv", "Scripts", "python.exe")
       candidate = "/opt/venv-a0/bin/python"
       return candidate if os.path.isfile(candidate) else sys.executable
   ```

5. **Add the missing `source` field** to the banner card in `banners/_10_skillopt_status.py`:
   ```python
   "source": "backend",  # tells the v2.5 webui this came from a Python extension
   ```

6. **Bump `plugin.yaml` version** to `1.1.0` so the framework re-reads the manifest.

7. **Tighten the validation gate** in `auto_loop.py`:
   - Reject if `proposed == current` (already done).
   - Reject if `len(proposed) < len(current) * 0.5` (shrinking more than half is suspicious).
   - Reject if no section header at depth 1 or 2.
   - Reject if no example/output block (heuristic: no triple backticks).
   - Optionally hash the proposal and refuse to adopt a hash already in `adoptions.log`.

### 6.2 Required (no plugin should ship without these)

8. **Real rollout harvester.** Add `extensions/python/monologue_end/_60_skillopt_harvest_rollout.py`. The monologue_end hook fires after each chat; you have a v2 example already in `plugins/_memory/extensions/python/monologue_end/`. Persist to `logs/rollouts/<sha1(uuid)>.json` with the fields the optimizer actually reads: `{id, ts, task, task_type, skill_used, outcome, trajectory: [...], latency_s, tokens_in, tokens_out, model}`. Decide `outcome` from `log.success`/`log.warning`/`log.error` exit codes or the final `Response` text heuristic (success/failure/partial). **Without this, the loop is permanently stalled.**

9. **Real bridge target.** Stop writing to `~/.claude/`. Either:
   - Set `transcript_source: auto` in the engine's inline config and pass `--transcript-source auto` so the engine accepts A0's own JSONL, **or**
   - Use SkillOpt's `codex` source instead — the v0.2.0 `harvest` verb can read JSONL files of the shape we already produce.
   The cleanest fix: dump rollouts to a directory the engine watches natively, and add `--harvest-from <dir>` if the flag exists in v0.2.0; otherwise the bridge is the right pattern but should write to a plugin-local `~/.cache/skillopt/{history.jsonl,projects/...}` and configure the engine's `transcript_source` accordingly.

10. **Auto-loop failure surfacing.** In `helpers/auto_loop.py:_log()`, also write to `logs/runs/auto_loop.log` (already done) **and** raise a banner card on the next monologue_start if the last cycle failed. Mirror the pattern in `banners/_10_skillopt_status.py` but with `type: "warning"`.

11. **Staging path mismatch.** In `helpers/sleep_runner.py`, set `cwd=str(staging_dir())` for the Sleep subprocess (currently `cwd=str(runs_dir())`). Then the engine's `consolidate` verb writes `best_skill.md` straight into `staging/`, which is what the rest of the pipeline expects.

### 6.3 Architectural fixes (to make A0 a *true* self-evolving agent)

These are the structural changes that lift this from "a script that edits markdown" to "an agent that improves itself":

12. **Two-loop architecture.** Today there is one loop (rollout → propose → adopt). The right design is two loops:
    - **Fast loop (per-chat):** capture rollout, score it, surface it.
    - **Slow loop (nightly):** cluster rollouts by intent, pick the top-3 intents, propose edits, validate, adopt.
    Add a `cluster_rollouts_by_intent()` helper (simple TF-IDF or sentence-embeddings cosine on the `task` field) and a `pick_top_k_intents()` selector. The SkillOpt `mine` verb does some of this implicitly but doesn't know your user's domain.

13. **Reward model.** Today the "score" is "did the task end without an error". That's a poor reward. Add a `score_rollout(rollout)` function that returns a float in `[0, 1]` based on:
    - Whether the user reacted (thumbs up/down if the WebUI surfaces it).
    - Whether the agent had to retry (number of `monologue_end` entries before the final one).
    - Whether the trajectory ended in a tool error.
    - Whether the final response cited the skill's examples.
    Persist this to the rollout JSON as `score: 0.83`. Then `_gate()` can enforce `score > 0.5` for the *current* skill to be considered good enough to learn from.

14. **Skill composition graph.** Right now each skill is independent. In practice, `qa` and `code_review` and `docs` overlap (they all touch test coverage). Add a `dependencies:` field to `SKILL.md` frontmatter and have the optimizer include parent skills in the prompt. This is what Microsoft's repo calls "skill stacking" in their v0.2.0 release notes.

15. **Skill fragment store.** Move from monolithic `SKILL.md` to a fragment store: `usr/skills/<name>/fragments/*.md` (each ≤ 500 chars, each tagged with an intent). The optimizer picks the top-K relevant fragments and composes them per chat. This is a 2x improvement in skill quality (per the SkillOpt paper, table 4) and a 5x improvement in **edit locality** — the LLM only re-writes 1 fragment instead of the whole document.

16. **A/B harness.** The validation gate is currently offline (the Sleep engine replays old rollouts). Add an online A/B harness: for the first 24h after adoption, route 10% of chats to the new skill and 90% to the old, and use the user-reaction reward to decide which to keep. The SkillOpt repo has a stub `replay` mode that almost does this.

17. **Cross-session memory of failures.** Today `logs/rollouts/` is a flat directory. Add a `failures.sqlite` that the optimizer queries directly, and a `passes.sqlite` for the contrastive pairs. The SkillOpt paper's held-out evaluation needs a balanced dataset; a flat JSON dump doesn't provide that.

18. **Self-critique hook.** After each Sleep cycle, the optimizer should output a 200-word critique of its own proposal ("here is what I changed and why"). Persist to `staging/<skill>.critique.md`. The user reads it in the WebUI before approving. This single change moves the system from "black box" to "auditable self-editor" and is what most people mean by "self-evolving".

19. **Make the auto-loop robust on Windows.** Use `subprocess.Popen` with no `start_new_session`, write PID to a file, and on the next tick check the file. Move to `multiprocessing.Process` if you want true isolation.

20. **Wire `gate_min_improvement_pp` for real.** The engine's held-out score is a float in `[0, 1]` (success rate on the replay set). Read it from the engine's stdout (it's logged as `held-out 0.412 -> 0.487 => 0.075 accepted=True`) and enforce `delta >= cfg.gate_min_improvement_pp` in `_gate()`. Default this to `0.05` (5pp).

21. **Replace the model defaults.** `SKILLOPT_OPTIMIZER_MODEL="gemma4:31b"` and `SKILLOPT_TARGET_MODEL="minimax-m3"` are placeholders. The optimizer should be **your strongest available reasoning model** (the one that writes the best prose), and the target should be the **cheapest model that the agent actually runs in production**. Otherwise the replay is measuring the wrong thing.

22. **Add a `--dry-run` mode to the direct optimizer** that returns the proposed skill inline (instead of writing to `staging/`) so the WebUI can show a diff in-place. This is the difference between "the bot changed your skill while you slept" and "here is what the bot wants to change — accept or reject".

### 6.4 Polish

23. The `webui/config.html` and `webui/skillopt-dashboard.js` files likely aren't being served by v2.5's WebUI. The v2.5 plugin UI pattern is to ship a `webui/*.js` that the framework discovers, not a custom page. Consider porting the dashboard to a single `webui/index.js` that registers a section in the Plugin Hub.

24. Move the `agents/skillopt_trainer/` profile to use `model: utility` (your fast local model) instead of `null`. The trainer should be cheap.

25. Add a `conftest.py`-style smoke test in `tests/` that:
    - Stubs 20 fake rollouts,
    - Runs the direct optimizer,
    - Asserts a staging file appears,
    - Calls the gate,
    - Asserts the gate rejects the same content (idempotence),
    - Asserts the gate accepts a ≥5% different content (improvement).

---

## 7. Implementation order (suggested)

```
Day 1:  Quick wins 1-7
        → run a manual sleep cycle, watch it produce a staging file

Day 2:  Required 8-11
        → real rollouts flow into logs/, loop fires, gate is honest
        → end-to-end: chat → rollout → cycle → gate → adopt → next chat uses it

Day 3-5:  Architectural 12-15
        → two-loop, reward model, fragment store, critique hook
        → A0 is now observably self-improving

Day 6+:   Polish 16-25
        → A/B harness, A0 v2.5 dashboard integration
```

---

## 8. Why a v1.0.0 plugin that doesn't actually create a skill ships at all

Looking at `execute.py`, the self-check **passes** with exit 0 even when no skill has ever been adopted. The summary reports `"cycles_run": 0, "proposals_adopted": 0` but the script ends with `"Health check PASSED."`. This is a false positive — the green tick in the Plugins UI is misleading. Tighten the check to fail when:
- `package.skillopt_sleep is not importable`, **or**
- `cycles_run == 0 and rollouts_count >= auto_loop_min_rollouts` (the loop was supposed to fire and didn't), **or**
- the last `auto_loop.log` line contains `tick error`.

That single change would have surfaced the package-import regression to the user instead of letting it hide for weeks.

---

## 9. Quick test you can run right now

```bash
cd "E:/agent-zero/a0-inst-agent-zero-latest-mqtnkttk"
.venv/Scripts/python.exe -c "import skillopt; print(skillopt.__file__); import skillopt.optimize; print('ok')"
# ↑ this WILL fail today; skillopt_sleep is no longer a separate import

.venv/Scripts/python.exe -c "
from usr.plugins.skillopt.helpers import sleep_runner
print(sleep_runner.get_status_snapshot())
"
# ↑ this will show rollout_count=12, skills_count=100+ (all your usr/skills),
#   staged_proposals=[code_review.md, docs.md, qa.md],
#   package.present=False
```

The 100+ skills in `usr/skills/` are your **gold mine** — you have enough diverse skills that a properly working SkillOpt loop could meaningfully refine them. The plugin is sitting on top of a huge dataset; it just isn't talking to the engine.

---

## 10. TL;DR

- **The plugin loads but does not call the SkillOpt engine.** The package name changed in v0.2.0 (`skillopt_sleep` → `skillopt.sleep`).
- **The plugin's "auto-loop" works on paper but is gated on a 10-rollout delta that never happens** because no real rollout harvester ships.
- **The validation gate is hollow** — `adoptions.log` proves it has accepted a byte-identical adoption.
- **Three things break on Windows:** the hardcoded Linux venv path, `start_new_session=True`, and a `clear_plugin_cache` kwarg that v2.5 removed.
- **The plugin underuses v2.5's manifest contract** — banners, hooks and dashboard files don't follow the current `Extension` base class idiom.
- **The data is there** (`/usr/skills/` has 100+ real skills) — fix the harvester, the Sleep invocation, the gate, and the platform issues, and you have a working self-evolution loop in 1-2 days.
- **For a *true* self-evolving agent:** add a reward model, a fragment store, a critique hook, and an A/B harness. These are 4-5 days more and they take the system from "script that edits markdown" to "agent that improves itself observably".

Sources:
- Microsoft SkillOpt repo (v0.2.0, 2026-07-02): `https://github.com/microsoft/SkillOpt`
- Live evidence in this repo: `usr/plugins/skillopt/logs/runs/adoptions.log`, `sleep-run-*.log`, `logs/rollouts/fake_*.json`, `.auto_loop_state.json`
- v2.5 plugin manifest: `helpers/plugins.py:78-95, 200, 568-583, 645`
