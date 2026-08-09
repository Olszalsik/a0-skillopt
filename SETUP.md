# SkillOpt Plugin — First-run Setup

> A 5-minute checklist for new users. Get from "I just installed the plugin" to "the loop ran its first cycle and wrote a critique."

---

## 0. Prerequisites (1 minute)

- [ ] The plugin is at `/a0/usr/plugins/skillopt/` (or wherever you put it).
- [ ] The A0 venv is reachable. The default is `/opt/venv-a0/bin/python`. Override with `export A0_VENV_PYTHON=/your/path` if needed.
- [ ] An LLM credential is set: `AZURE_OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, or `OLLAMA_API_KEY`.

## 1. Run the self-check (10 seconds)

```bash
cd /a0/usr/plugins/skillopt
/opt/venv/bin/python execute.py
```

You should see:

```
[skillopt] OK: all 30 required files present
[skillopt] OK: manifest version 1.4.0
[skillopt] OK: skillopt_sleep importable, version 0.2.0
[skillopt] Toggle state: ON
[skillopt] Harvested rollouts: 0
[skillopt] Staged proposals: 0
... 
[skillopt] Health check PASSED.
```

If anything is missing or red, see [INSTALL.md](INSTALL.md) — the error message tells you exactly which step to re-do.

## 2. Configure the backend (30 seconds)

The default `backend: auto` reads your env vars. If you haven't set one globally, the safest place is the plugin's per-cycle env file:

```bash
PLUGIN_ENV=/a0/usr/plugins/skillopt/logs/runs/.skillopt-env
touch "$PLUGIN_ENV"

# Pick ONE provider and uncomment the right block:

# --- Azure OpenAI ---
# echo 'export AZURE_OPENAI_ENDPOINT=https://your-resource.openai.azure.com/' >> "$PLUGIN_ENV"
# echo 'export AZURE_OPENAI_API_KEY=sk-...' >> "$PLUGIN_ENV"
# echo 'export AZURE_OPENAI_DEPLOYMENT=minimax-m3' >> "$PLUGIN_ENV"

# --- Anthropic ---
# echo 'export ANTHROPIC_API_KEY=sk-ant-...' >> "$PLUGIN_ENV"

# --- OpenAI-compatible ---
# echo 'export OPENAI_API_KEY=sk-...' >> "$PLUGIN_ENV"
# echo 'export OPENAI_BASE_URL=https://your-endpoint/v1' >> "$PLUGIN_ENV"

# --- Ollama (local or cloud) ---
# echo 'export OLLAMA_API_KEY=your-ollama-cloud-key' >> "$PLUGIN_ENV"
# echo 'export OPENAI_BASE_URL=https://ollama.com/v1' >> "$PLUGIN_ENV"
```

> **Why a per-plugin env file?** The plugin's optimiser subprocess inherits only a sanitised environment. The shell env vars from `~/.bashrc` are NOT passed through (that would leak unrelated secrets into the Sleep engine). The env file is the right place.

## 3. Toggle the loop ON (10 seconds)

In the WebUI:

- **Settings → Developer → SkillOpt**
- Set `auto_loop_enabled` to **`true`**
- Set `auto_adopt` to **`false`** for the first few cycles (you want to read the critiques before letting it auto-promote)
- **Save**

Or via the API:

```bash
curl -X POST http://localhost:50001/api/plugins/skillopt/config \
  -H 'Content-Type: application/json' \
  -d '{"auto_loop_enabled": true, "auto_adopt": false}'
```

## 4. Wait for the harvester to write rollouts (varies)

Every chat you have with A0 writes one rollout to `logs/rollouts/<uuid>.json`. By default the loop waits until 10 are accumulated before running:

```bash
ls /a0/usr/plugins/skillopt/logs/rollouts/ | wc -l
```

The number should grow after each chat. If it's stuck at 0, check:

- [ ] The monologue_end hook is installed: `ls /a0/usr/plugins/skillopt/extensions/python/monologue_end/_60_skillopt_harvest_rollout.py`
- [ ] The A0 framework loaded the hook — open the WebUI and look for the SkillOpt banner card on the Welcome screen
- [ ] The plugin is **enabled** (not toggled OFF)

## 5. Run a manual harvest to see the data flow (30 seconds)

```bash
curl -X POST http://localhost:50001/api/plugins/skillopt/sleep \
  -H 'Content-Type: application/json' \
  -d '{"verb": "harvest"}'
```

Then check:

```bash
# The bridge file (plugin-local cache)
ls /a0/usr/plugins/skillopt/.cache/claude_code/ 2>/dev/null

# The cycle log
ls -t /a0/usr/plugins/skillopt/logs/runs/sleep-*.log | head -1 | xargs tail -50
```

You should see a `history.jsonl` in the cache (or a new Claude Code session directory), and the cycle log should show the Sleep engine reading from it.

## 6. Run a dry-run cycle (1-2 minutes)

```bash
curl -X POST http://localhost:50001/api/plugins/skillopt/sleep \
  -H 'Content-Type: application/json' \
  -d '{"verb": "dry-run"}'
```

Wait ~60 seconds, then check:

```bash
# A new cycle log
ls -t /a0/usr/plugins/skillopt/logs/runs/sleep-*.log | head -2

# The staging area (proposal lives here until the gate passes)
ls /a0/usr/plugins/skillopt/staging/

# The critique (always written, even for dry-runs)
ls -t /a0/usr/plugins/skillopt/logs/runs/critiques/
```

Open the critique file in any Markdown viewer. It contains:
- The skill name
- The proposal vs. the current text (diff)
- The gate's verdict and reason
- The model's held-out score (if the Sleep engine reported one)

## 7. (Optional) Promote the proposal manually

If the dry-run looks good and you want to skip the auto-adopt gate:

```bash
# 1. Pick the staged file
ls /a0/usr/plugins/skillopt/staging/
# (e.g. code_review.md)

# 2. Adopt it via the API
curl -X POST http://localhost:50001/api/plugins/skillopt/adopt \
  -H 'Content-Type: application/json' \
  -d '{"skill": "code_review", "source": "staging/code_review.md"}'
```

The post-adopt hook runs the validation gate one more time. If it passes, the new `SKILL.md` is in `usr/skills/<name>/SKILL.md` and the next chat uses it.

## 8. Turn on auto-adopt (1 minute)

Once you've reviewed 2-3 critiques and you're happy with what the loop is proposing:

- **Settings → Developer → SkillOpt** → `auto_adopt` = **`true`** → **Save**

From this point on, every Sleep cycle that passes the validation gate auto-promotes. You'll see a one-line entry in `logs/runs/adoptions.log` for every adoption, and the dashboard updates in real time.

## 9. (Optional) Install the subordinate profile

If you want to delegate one-off Sleep runs to a specialised agent:

```bash
# Already present in the plugin at agents/skillopt_trainer/agent.yaml
# The WebUI picks it up automatically. Use:
call_subordinate(
    profile='skillopt_trainer',
    message='Run a dry-run Sleep cycle for the code_review skill and report the verdict.'
)
```

## 10. Done — what's next?

- The loop runs in the background. Tail its log: `tail -f /a0/usr/plugins/skillopt/logs/runs/auto_loop.log`
- Review critiques weekly: `ls -lt /a0/usr/plugins/skillopt/logs/runs/critiques/ | head`
- Audit adoptions: `cat /a0/usr/plugins/skillopt/logs/runs/adoptions.log`
- Read the [ROADMAP.md](ROADMAP.md) for what v2 is going to add (the A/B harness, the fragment store, the failure memory)

---

## Common first-run issues

| Symptom | Likely cause | Fix |
|---|---|---|
| Health check says `loop stalled: ... but cycles_run=0` | The loop is on, ≥10 rollouts, but `auto_loop.log` shows the Sleep subprocess never started | Check the env file. The most common cause is a missing API key in `logs/runs/.skillopt-env`. |
| Dry-run writes an empty proposal | The LLM is returning empty / getting rate-limited / wrong model | Check the cycle log for the model's raw response. Try a different `optimizer_model` (e.g. `minimax-m3`). |
| The gate rejects every proposal with `whitespace-equal` | The optimiser is rewriting without changing anything (LLM too small) | Bump the `optimizer_model` to a stronger model, or set `gate_max_shrink_ratio=0.0` to allow same-length edits. |
| `OSError: [Errno 8] Exec format error` on Windows | You ran `execute.py` from WSL or Git Bash with the wrong Python | Use the full path: `& "C:\path\to\.venv\Scripts\python.exe" execute.py` |
| The `monologue_end` hook never fires | The hook file isn't loaded by the framework | Restart A0 after copying the plugin. The hook is registered at startup, not on import. |

---

## Where to get help

- **Issues** — open a ticket with the cycle log + the critique file attached
- **Discord** — see the A0 community server (link in the README at the A0 repo)
- **Read the code** — the plugin is small enough (~1000 LoC). Start at `helpers/sleep_runner.py` and `helpers/auto_loop.py`
