# SkillOpt Self-Evolution Engine

[![Version](https://img.shields.io/badge/version-1.8.0-blue)](#)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![SkillOpt](https://img.shields.io/badge/powered%20by-microsoft%2Fskillopt-blueviolet)](https://github.com/microsoft/SkillOpt)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-3776AB)](#)

> Your Agent Zero skills train themselves. Every chat teaches them.

**SkillOpt** turns the agent's own task trajectories into better skill documents — automatically, with a held-out validation gate, and without ever silently overwriting your work. It's the missing closed loop between execution and skill learning.

**v1.7.0 (Solution C)** closed the loop for real: the rollout harvester now actually records every chat (it had been silently writing nothing since v1.1.0), a local counterfactual replay gate scores proposed skills on held-out tasks, a human-in-the-loop adopt UI lets you Approve/Reject/Rollback each staged proposal, and new skills auto-opt-in behind a human-approval gate. The auto-loop drives the official Microsoft `skillopt_sleep` pipeline (Solution B, v1.6.0) and falls back to a local optimizer when the package is absent.

**v1.8.0** implements the two pieces Solution C deliberately stubbed — both **opt-in and default-off**, so v1.7.0 behavior is preserved byte-for-byte unless you flip the flags:
- **Real A0-agent-loop replay executor** (`replay_real_executor_enabled: true`): the local replay gate re-runs each held-out task through a real Agent Zero monologue under the current vs proposed skill (subprocess-isolated: temp cwd, own event loop, recursion-guarded) instead of the mock keyword heuristic. Cost is bounded by `replay_real_max_tasks` (default 4) and `replay_real_per_task_timeout_s` (default 180).
- **DistilBERT reward-model training**: an LLM-judge labelling pass (`scripts/label_rollouts.py`) writes outcome labels into your rollouts, then `scripts/train_reward_model.py --mode train` trains a 3-class classifier on them, a calibration pass picks the `prefer_model_above` threshold, and `score_rollout` uses the trained + calibrated model instead of the heuristic. The previously-dead `reward_model_path` / `reward_model_prefer_above` config keys are now wired.

Live verification of the opt-in paths (real A0 runtime + LLM credentials) is still pending; the 163/163 deterministic smoke suite covers all logic testable without them.

---

## Is this plugin right for you?

Read this before you install — it should take 30 seconds and save you an afternoon.

✅ **Install SkillOpt if:**
- You have an Agent Zero agent with **5+ skills in active use** and you want them to improve over time without you hand-editing every `SKILL.md`.
- You're OK with **per-skill governance** (global default is opt-out for safety). New skills seen in rollouts are auto-opted-in but stay **pending human approval** — nothing is ever adopted silently. You can also mark any skill `immutable` or `.skillopt.optout` and the loop will never touch it.
- You want a **closed loop between agent execution and skill learning** with a **strict held-out validation gate** (official engine gate + a local counterfactual replay gate) so a regression never lands silently.
- You're happy to ship a **local-first** self-evolution engine — all training is on-device on the agent's own trajectories; no data leaves the host.

🚫 **Don't install SkillOpt if:**
- You want to **fine-tune the underlying model weights**. SkillOpt edits skill *text*, not LLM weights. Use a model fine-tuner instead.
- You want a **skill authoring tool** for new skills. SkillOpt rewrites existing skills based on usage; it doesn't help you write one from scratch.
- You're running the agent in a **regulated environment** where any automatic file edit to `usr/skills/` would be a compliance violation. Even with opt-in per-skill, the loop is automated.
- You're on **Python 3.9 or older**. SkillOpt v1.5.0 requires Python 3.10+ (same as Agent Zero itself; CI matrix covers 3.10-3.13).
- You don't yet have **rollouts from agent runs**. SkillOpt needs at least `auto_loop_min_rollouts` (default 10) finished chats before the first cycle is meaningful. With zero rollouts the loop will spin but produce nothing useful.

If you're still unsure: open a chat with the agent, finish 10+ real tasks, then install SkillOpt. The first cycle will mine those rollouts and propose one improvement per skill that has enough data.

---

## What it does

```
   ┌──────────────┐    ┌──────────────┐    ┌──────────────┐
   │  monologue   │───▶│  rollouts/   │───▶│  Sleep /     │
   │  ends (chat) │    │  *.json      │    │  direct LLM  │
   └──────────────┘    └──────────────┘    └──────┬───────┘
                                                  │
                                                  ▼
   ┌──────────────┐    ┌──────────────┐    ┌──────────────┐
   │  usr/skills/ │◀───│  validation  │◀───│  staging/    │
   │  <name>/     │    │  gate        │    │  <name>.md   │
   │  SKILL.md    │    │  (strict)    │    │  + critique  │
   └──────────────┘    └──────────────┘    └──────────────┘
```

1. **Harvest** — every A0 chat that completes drops a `rollout` (the task, the trajectory, the outcome, the skill used) into `logs/rollouts/`.
2. **Mine** — the background loop wakes up, sees enough new rollouts, and either calls the SkillOpt Sleep engine (`python -m skillopt_sleep run`) or runs a direct LLM optimisation pass on the current skill.
3. **Validate** — every proposal goes through `validate_proposal()`: no empty, no malformed, no byte-identical no-ops, no silent truncations, and (optionally) only if the held-out score improved by ≥ `gate_min_improvement_pp`.
4. **Adopt** — if the gate passes, the proposal is written to `usr/skills/<name>/SKILL.md` and audited. Otherwise the reason is logged to `logs/runs/critiques/<name>_<ts>.md` and the user is shown the diff in the dashboard.
5. **Loop** — A0's next chat automatically uses the improved skill. No model retraining, no API rewrites, no human in the loop.

---

## Before / After

| v1.0.0 (this plugin, 3 weeks ago) | v1.4.0 (now) |
|---|---|
| Nothing ever wrote a rollout → Sleep had nothing to mine | `monologue_end` hook writes a rollout per chat turn |
| `validate_proposal()` was 5 lines → let a 1904→1904 no-op through | 8-stage gate: byte-eq, ws-eq, shrink ceiling, headers, example block, min chars, held-out delta |
| `/api/plugins/skillopt/config` 500'd on every save (`python_change` kwarg removed in v2.5) | Uses the new `clear_plugin_cache([name])` signature |
| `install()` was Linux-only | Cross-platform: Linux `/opt/venv-a0` + Windows `.venv\Scripts\python.exe` |
| `subprocess.start_new_session=True` crashed on Windows | `creationflags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP` on Windows |
| Direct optimiser hardcoded `gemma4:31b` | Resolved from `SKILLOPT_OPTIMIZER_MODEL` env / `.skillopt-env` / config |
| `execute.py` health check always said PASSED, even when the loop never ran | Honest check: returns 5 with a clear reason when `rollouts >= threshold` AND `cycles == 0` |
| Bridge wrote to `~/.claude/` → polluted the host | Plugin-local cache by default (`<plugin>/.cache/claude_code/`); host mode opt-in via `SKILLOPT_BRIDGE_TO_HOST=1` |

---

## Quick start (60 seconds)

### 1. Install the plugin

The plugin is already at `/a0/usr/plugins/skillopt/` in the standard A0 Docker image. If you're on a fresh host:

```bash
# from /a0 (the A0 project root)
cp -r usr/workdir/skillopt-plugin usr/plugins/skillopt
cd usr/plugins/skillopt
/opt/venv/bin/python execute.py   # should print 'Health check PASSED'
```

### 2. Open the WebUI

- **Settings → Developer → SkillOpt** — every knob, with inline help.
- **Plugins → SkillOpt → Run** — the dashboard, with live status, staged proposals, and per-cycle critiques.

### 3. Configure the backend

The default `backend: auto` picks the first LLM credential it finds in `AZURE_OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `API_KEY_OLLAMA_CLOUD`, or the framework's chat model config. To pin a specific backend:

```bash
# via the env file the plugin reads every cycle
echo 'export AZURE_OPENAI_ENDPOINT=https://your-resource.openai.azure.com/' >> /a0/usr/plugins/skillopt/logs/runs/.skillopt-env
echo 'export AZURE_OPENAI_API_KEY=sk-...' >> /a0/usr/plugins/skillopt/logs/runs/.skillopt-env
echo 'export SKILLOPT_OPTIMIZER_MODEL=your-model-name' >> /a0/usr/plugins/skillopt/logs/runs/.skillopt-env
```

### 4. Run your first cycle

From the WebUI: **Plugins → SkillOpt → Run Sleep (dry-run)** — mines, proposes, but doesn't adopt. Read the critique in `logs/runs/critiques/`.

From a shell:

```bash
curl -X POST http://localhost:50001/api/plugins/skillopt/sleep \
  -H 'Content-Type: application/json' \
  -d '{"verb":"dry-run"}'
```

When you trust the output, set `auto_adopt: true` in the settings (or POST to `/api/plugins/skillopt/config`). The loop will then promote gate-passing proposals automatically.

---

## Requirements

- **Python 3.10+** (A0 already requires this)
- **An LLM credential** for at least one of: Azure OpenAI, OpenAI-compatible, Anthropic, local Ollama
- **The `skillopt` package** is auto-installed by `hooks.py:install()`. No manual `pip install` step required.
- **No `[webui]`, `[alfworld]`, `[claude]`, `[qwen]` extras.** They conflict with A0's dependency graph and pull hundreds of MB of unused packages. Install them manually only if you actually need a specific benchmark.

---

## Configuration cheat sheet

| Setting | Default | What it controls |
|---|---|---|
| `backend` | `auto` | Which LLM the optimiser talks to. `auto` reads env vars in priority order. |
| `auto_loop_enabled` | `true` | Master switch for the background loop. |
| `auto_loop_interval_sec` | `1800` | How often the loop wakes up. 30 min is a good baseline. |
| `auto_loop_min_rollouts` | `10` | New rollouts required before a cycle is worth running. |
| `auto_loop_skill_target` | `""` | Focus the loop on one skill. Empty = all skills with data. |
| `auto_adopt` | `false` | Auto-promote gate-passing proposals. **Read critiques first.** |
| `gate_min_chars` | `200` | Reject proposals shorter than this. |
| `gate_max_shrink_ratio` | `0.5` | Reject proposals smaller than 50% of the current skill. |
| `gate_min_improvement_pp` | `5.0` | Reject if held-out improvement < 5 percentage points. Set to `0` to disable. |
| `max_runs_retained` | `10` | Cap on historical run directories. |
| `critique_dir` | `logs/runs/critiques` | Where per-cycle critiques land. |
| `optimizer_model` | `chat` | Model that proposes skill edits. The `chat` sentinel (v1.8.12) follows the active Agent Zero chat model. |
| `target_model` | `chat` | Model A0 runs in production; held-out replay uses it. The `chat` sentinel (v1.8.12) follows the active A0 chat model. |
| `privacy_redact_secrets` | `true` | Best-effort redaction of common credential patterns in new rollouts and before direct model calls. |
| `privacy_include_tool_args` | `false` | Store tool arguments in new rollouts. Enable only when needed; values are still redacted when redaction is on. |
| `privacy_include_tool_results` | `false` | Store tool results in new rollouts. Enable only when needed; values are still redacted when redaction is on. |
| `replay_evalkit_enabled` | `false` | Attach optional upstream paired statistics to a completed real replay gate when the installed package supports evalkit. Informational only. |

For the full reference with comments, see `default_config.yaml`.

### Rollout data and privacy

SkillOpt stores task text and the final response locally for learning. Tool
arguments and results are excluded by default. Common API-key, bearer-token,
private-key, and URL-credential patterns are redacted before new rollout data
is written, before direct optimizer/judge prompts are sent, before legacy
rollouts are bridged to the official engine, and before held-out replay calls
Agent Zero. You can inspect the effective controls in the status payload's
`privacy` block.

Redaction is best-effort and cannot identify every secret or personal detail.
Review harvested tasks before enabling a provider-backed cycle. Setting
`privacy_redact_secrets` to `false` disables these safeguards; avoid doing so
for sensitive conversations. Previously written rollout files are not
rewritten automatically, though model-bound paths sanitize them before use.

### Optional upstream evalkit report

When `replay_evalkit_enabled` is on, a completed real replay may attach a
paired-statistics report to its gate sidecar. SkillOpt probes the installed
`skillopt_sleep.evalkit` CLI before use, since evalkit is not present in every
released package. The report reuses scores already collected by the real
gate, is bounded, and cannot change the gate verdict or adoption decision.
Unsupported versions and report errors leave the ordinary gate result intact.

## Judge burst protection (v1.8.16)

`helpers/llm_judge.py` wraps every judge LLM call in a three-layer burst guard:

1. **In-flight limiter** — a `threading.BoundedSemaphore` caps concurrent judge calls
   (`SKILLOPT_JUDGE_MAX_CONCURRENCY`, default `1` = strict serialization; raise it for bounded
   parallelism).
2. **Reservation pacing** — each call reserves its slot start under a lock, so consecutive
   HTTP attempts stay at least `SKILLOPT_JUDGE_THROTTLE_S` apart (default `1.5`, `0` disables; knob
   since v1.8.11) even when many callers race.
3. **Backoff retries** — retryable failures (HTTP 429 / rate limit, 5xx, timeouts, connection
   errors) retry with exponential backoff and half-to-full jitter, honoring `Retry-After` when the
   SDK exposes it. Non-retryable errors fail fast; retry exhaustion re-raises into the never-raises
   wrapper, so a judge failure degrades to `{label: None, error}` instead of crashing a cycle.

The knobs are environment variables (not config keys):

| Env var | Default | What it controls |
|---|---|---|
| `SKILLOPT_JUDGE_MAX_CONCURRENCY` | `1` | Max concurrent judge calls (strict serialization at 1). |
| `SKILLOPT_JUDGE_THROTTLE_S` | `1.5` | Minimum seconds between consecutive judge HTTP attempts (0 disables). |
| `SKILLOPT_JUDGE_RETRY_MAX` | `3` | Retry attempts for retryable judge failures. |
| `SKILLOPT_JUDGE_RETRY_BASE_S` | `0.5` | Exponential backoff base delay (half-to-full jitter). |
| `SKILLOPT_JUDGE_RETRY_MAX_S` | `8.0` | Upper cap on a single backoff delay. |

Retry telemetry lives in `helpers.llm_judge._retry_stats` (attempts / retries / exhausted);
`_reset_burst_state()` resets limiter / pacing / retry state (test isolation). Non-judge LLM
callers (the direct optimizer) are untouched by the burst guard.

---

## Hub submission status (v1.8.13+)

SkillOpt's Plugin Hub submission (agent0ai/a0-plugins PR #512) is monitored by
`scripts/check_hub_status.py`, which persists `{latest, history}` to `logs/hub_status.json`
(`latest.status`: OPEN_PENDING / MERGED_INDEXED / MERGED_UNINDEXED, or ERROR when a probe itself
failed).

**Endpoint:** `GET` / `POST` `/api/plugins/skillopt/hub_status` (`api/hub_status.py`). Read-only,
public, non-sensitive — auth/CSRF are relaxed. Semantics:

- **Execution cache:** a fresh payload (file mtime age <= 3600 s / 1 hour) is returned as-is with
  HTTP 200 — no subprocess is spawned.
- **Refresh:** a stale or missing payload triggers exactly one refresh: `scripts/check_hub_status.py`
  runs as an asyncio subprocess under a hard **3 s** timeout (killed on overrun; the server event
  loop is never parked on a network timeout). Concurrent requests share a single refresh via a
  process-wide guard; waiters poll file freshness instead of spawning duplicates.
- **Failure:** a failed refresh (spawn error, timeout, file still missing/stale) returns HTTP 500
  with a structured `{status: ERROR, message: ...}` body. A probe that itself reports `ERROR` is
  served as honest data with HTTP 200.

**Background watchdog (v1.8.15):** the job_loop extension
`extensions/python/job_loop/_80_skillopt_hub_watchdog.py` (`HubWatchdogExtension`) is fired by
Agent Zero's background scheduler tick and keeps the same state file fresh with a **1,800 s mtime
throttle** — the probe runs only when `logs/hub_status.json` is older than 30 minutes (or
missing), each run capped at 3 s, single-flight (overlapping ticks never double-spawn), and
background errors are logged and never propagate into the job loop. Once the Hub PR merges, the
first tick past the throttle window records MERGED_INDEXED / MERGED_UNINDEXED, and the endpoint
surfaces the transition within one throttle window — no manual watchdog runs needed.

---

## The validation gate (how we know we're not breaking things)

`validate_proposal()` in `helpers/sleep_runner.py` is the shared gate. It rejects a proposal if any of the following is true:

1. The proposal is empty.
2. The proposal has no Markdown headers (likely malformed).
3. The proposal is shorter than `gate_min_chars` (200 by default).
4. The proposal has no triple-backtick example block (the Sleep engine always emits one).
5. The proposal is byte-identical to the current skill.
6. The proposal equals the current skill after whitespace normalisation (catches the 1904→1904 'no-op' case).
7. The proposal is smaller than `gate_max_shrink_ratio` (50% by default) of the current skill.
8. The Sleep engine reported a held-out score and the delta is below `gate_min_improvement_pp` (5pp by default). Skipped for the direct-optimizer path, which has no numeric gate.

If a proposal is rejected, the reason is logged to `logs/runs/adoptions.log` and surfaced in the dashboard. Nothing is written to `usr/skills/`.

---

## Files

| Path | Purpose |
|---|---|
| `plugin.yaml` | Plugin manifest |
| `hooks.py` | Lifecycle: `install()`, `pre_update()`, `uninstall()` |
| `default_config.yaml` | All settings, documented |
| `execute.py` | Self-check script |
| `tools/` | Agent-callable tools |
| `api/` | HTTP API handlers |
| `api/hub_status.py` | `GET/POST /api/plugins/skillopt/hub_status` — hub merge/indexing status (v1.8.13) |
| `webui/` | Settings page + dashboard |
| `helpers/` | Sleep runner, auto-loop, bridge, direct optimiser |
| `extensions/python/` | Lifecycle hooks (monologue_end, agent_init, hooks, banners) |
| `extensions/python/job_loop/_80_skillopt_hub_watchdog.py` | Background watchdog refreshing `logs/hub_status.json` (v1.8.15; 1800 s mtime throttle) |
| `extensions/webui/` | Head + sidebar HTML injectors |
| `agents/skillopt_trainer/` | Subordinate profile |
| `staging/` | Where proposals land before promotion |
| `logs/rollouts/` | Where the harvester writes per-task records |
| `logs/runs/` | Cycle logs, state files, audit logs, env file, critiques |
| `logs/hub_status.json` | Hub watchdog state: `{latest, history}` from `scripts/check_hub_status.py` |

---

## What it does NOT do (v1)

- **It does not train the underlying model weights.** That's not SkillOpt's approach. SkillOpt optimises the *text* (skill documents) the agent reads at inference time.
- **It does not auto-install `[alfworld]`, `[claude]`, `[webui]`, `[qwen]` extras.** Those are for SkillOpt's own benchmark suite, which is irrelevant to A0. Install them manually if you need them.
- **It does not run the Sleep engine as a long-lived daemon.** Each cycle is a one-shot subprocess. This is deliberate: the engine is happy to run for ~10 min per cycle, then exits. The plugin's outer loop decides *when* to call it.
- **It does not ship a pre-trained reward model.** The `_60_skillopt_harvest_rollout.py` heuristic classifier is a string-match (good enough for early adoption; see ROADMAP.md for the next iteration).
- **It does not delete your skills on uninstall.** Set `SKILLOPT_PURGE_ON_UNINSTALL=1` if you want it to.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `execute.py` → `HEALTH CHECK FAILED. reason: ... but cycles_run=0` | The loop is enabled, rollouts ≥ threshold, but no cycle has ever run | Check `logs/runs/auto_loop.log` and `.auto_loop_last_error.json` |
| `No LLM API key found` | The optimiser subprocess can't read your key | Set the env var in `logs/runs/.skillopt-env`, NOT just in your shell |
| The Sleep subprocess starts but writes nothing | `harvest` verb filtered out all sessions (CWD/slug mismatch) | Verify the bridge is writing to `<plugin>/.cache/claude_code/`, and that path is the CWD of the Sleep subprocess |
| `pip install 'skillopt'` failed in `install()` | You're not running in the A0 venv | Set `A0_VENV_PYTHON=/path/to/a0/venv/bin/python` in your env, or run the hook from the venv directly |
| Dashboard shows rollouts but no staged proposals | The optimiser's prompt produced empty / malformed output | Check `logs/runs/critiques/` for the model's response and the gate's rejection reason |

---

## License

MIT. SkillOpt itself is MIT-licensed by Microsoft Research. See `LICENSE`.
