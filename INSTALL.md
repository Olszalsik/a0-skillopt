# SkillOpt Plugin — Install

> Cross-platform install for the SkillOpt self-evolution plugin. Linux, Windows, Docker, and dev installs are covered.

---

## What this plugin is and isn't (read first)

**SkillOpt** is a **self-evolution engine for Agent Zero skill documents**. Every chat that the agent finishes becomes a training signal; the engine rewrites `SKILL.md` files under `<a0>/usr/skills/<name>/` to improve them, with a strict held-out validation gate so a regression never lands silently.

SkillOpt is:
- ✅ A **closed loop between execution and skill learning** — agent chats → harvested rollouts → held-out-validated SKILL.md edits → the next chat uses the better skill. All training is on-device, on the agent's own trajectories. No data leaves the host.
- ✅ A **statistical confidence layer** — A/B harness with confidence intervals, reward model scorer, per-cycle dashboard with audit trail.
- ✅ A **per-skill governance layer** — opt-out / rate-limit / immutable / opt-in policies so you can lock down compliance skills and opt others in.
- ✅ **Plugin-local + stdlib-only** — no third-party dependencies beyond what A0 already ships.

SkillOpt is NOT:
- ❌ A **model fine-tuner**. It edits skill *text*, not LLM *weights*. The underlying chat model is unchanged.
- ❌ A **skill authoring tool**. It rewrites existing skills based on usage; it does not help you write a skill from scratch.
- ❌ A **training-data collector**. SkillOpt never sends your rollouts anywhere. All computation is local.

If you want any of those three things, this is the wrong plugin — use SkillOpt's own benchmark/authoring tools instead.

---

## TL;DR (the A0 Docker image)

The plugin is already at `/a0/usr/plugins/skillopt/` in the standard A0 Docker image. Verify:

```bash
cd /a0/usr/plugins/skillopt
/opt/venv/bin/python execute.py
```

If you see `Health check PASSED`, you're done. Skip to [SETUP.md](SETUP.md).

---

## Linux (native, no Docker)

```bash
# 1. Locate the A0 project root (where /opt/venv-a0 lives)
A0_ROOT="${A0_ROOT:-/path/to/agent-zero}"

# 2. Copy the plugin into usr/plugins
cp -r usr/workdir/skillopt-plugin "$A0_ROOT/usr/plugins/skillopt"

# 3. Install the Python package into the A0 venv
"$A0_ROOT/.venv/bin/python" -m pip install skillopt
# (or wherever the A0 venv lives; check helpers/plugins.py for detection)

# 4. Verify
cd "$A0_ROOT/usr/plugins/skillopt"
"$A0_ROOT/.venv/bin/python" execute.py
```

If the A0 venv lives somewhere other than `/opt/venv-a0`, set the env var:

```bash
export A0_VENV_PYTHON=/path/to/your/venv/bin/python
```

The plugin's `hooks.py` and `sleep_runner.py` both honour this override.

---

## Windows (PowerShell 5.1+)

```powershell
# 1. Locate the A0 project root (A0 dev installs put .venv under the project root)
$env:A0_ROOT = "C:\path\to\agent-zero"

# 2. Copy the plugin into usr/plugins
Copy-Item -Recurse usr\workdir\skillopt-plugin "$env:A0_ROOT\usr\plugins\skillopt"

# 3. Install the Python package into the A0 venv
& "$env:A0_ROOT\.venv\Scripts\python.exe" -m pip install skillopt

# 4. Verify
cd "$env:A0_ROOT\usr\plugins\skillopt"
& "$env:A0_ROOT\.venv\Scripts\python.exe" execute.py
```

If your venv lives at `venv\` instead of `.venv\`, set:

```powershell
$env:A0_VENV_PYTHON = "$env:A0_ROOT\venv\Scripts\python.exe"
```

---

## Docker (the standard A0 image)

```bash
# From the A0 project root, mount or copy the plugin
docker cp usr/workdir/skillopt-plugin a0:/a0/usr/plugins/skillopt

# Open a shell in the container
docker exec -it a0 bash

# Verify
cd /a0/usr/plugins/skillopt
/opt/venv/bin/python execute.py
```

The A0 Docker image already includes the `skillopt` package; no `pip install` needed.

---

## Development install (workdir <-> live sync)

The recommended dev loop is to keep the workdir at `/a0/usr/workdir/skillopt-plugin/` and rsync it into `/a0/usr/plugins/skillopt/` after each change:

```bash
# add to your shell rc / .bashrc
sync_skillopt() {
  rsync -a --delete \
    /a0/usr/workdir/skillopt-plugin/ \
    /a0/usr/plugins/skillopt/
}
sync_skillopt
```

After any change:

```bash
sync_skillopt
cd /a0/usr/plugins/skillopt && /opt/venv/bin/python execute.py
```

---

## Verifying the install

`execute.py` runs 6 checks:

1. **All 30 required files present.** Catches incomplete copies / interrupted rsyncs.
2. **Manifest is valid YAML and version is 1.1.0.** Catches corrupt `plugin.yaml`.
3. **`skillopt_sleep` is importable, version 0.2.0.** Catches a missing `pip install`.
4. **Toggle state is readable.** `ON`, `OFF`, or `DEFAULT`.
5. **Rollout + staging snapshot.** Tells you how much data the loop has.
6. **Honest health check.** Returns 5 if the loop was supposed to have run but didn't.

If any check fails, the script returns a non-zero exit code and a clear reason. **There is no green tick when broken.**

---

## What gets installed

The `install()` hook in `hooks.py` does one thing: `pip install skillopt` in the A0 venv. It does NOT install `[webui]`, `[alfworld]`, `[claude]`, or `[qwen]` extras — those conflict with A0's dependency graph and pull hundreds of MB of unused packages. If you need a specific extra:

```bash
/opt/venv/bin/python -m pip install skillopt[alfworld]
```

The plugin will not be affected; it only imports the base `skillopt` library and the `skillopt_sleep` module.

---

## Uninstalling

```bash
# Remove the plugin directory
rm -rf /a0/usr/plugins/skillopt

# Optionally remove the Python package (only if no other plugin uses it)
/opt/venv/bin/python -m pip uninstall skillopt

# Optionally remove per-plugin state (staging, rollouts, logs)
SKILLOPT_PURGE_ON_UNINSTALL=1 /opt/venv/bin/python /a0/usr/plugins/skillopt/hooks.py
# (you'll need to temporarily restore the plugin to call this)
```

By default, the plugin does **not** delete your skills on uninstall. Set `SKILLOPT_PURGE_ON_UNINSTALL=1` to opt in.

---

## Troubleshooting

### Cycle-history file is corrupt / the per-cycle dashboard 500s

The per-cycle history is stored append-only at `logs/runs/cycle_history.jsonl`. If the file gets truncated mid-write (rare on Docker, common on Windows+network-share), the next read can crash. Wipe and let the loop regenerate:

```bash
rm -f /a0/usr/plugins/skillopt/logs/runs/cycle_history.jsonl
rm -f /a0/usr/plugins/skillopt/logs/runs/cycle_history.log
```

The next cycle writes a fresh file. No data loss for any other component.

### Per-skill opt-out marker is being ignored

A `usr/skills/<name>/.skillopt.optout` marker only blocks the auto-loop if the governance helper is actually present in this plugin copy. Verify `helpers/governance.py` is on the file-presence check (`execute.py` step 1 — look for the `47 required files present` line) and that `logs/runs/governance.log` gets written when you expect. If the file is missing, re-copy from the workdir (`sync_skillopt` on Linux, `robocopy` on Windows) and re-run `python execute.py`.

### The auto-loop never fires (no `cycles_run`)

Two common causes, both silent:

1. `auto_loop_enabled: false` in `config.json` (master kill switch). Flip to `true` and re-run `python execute.py`.
2. The rollout count never crosses `auto_loop_min_rollouts` (default 10). Check `GET /api/plugins/skillopt/status` — the `auto_loop.rollouts` field tells you the current count. Either lower the threshold for testing or have the agent finish a few more chats to accumulate rollouts.

---

## Day-5 troubleshooting matrix (quick reference)

| Symptom | Likely cause | Fix |
|---|---|---|
| Per-cycle dashboard 500s on read | `cycle_history.jsonl` is corrupt | `rm logs/runs/cycle_history.jsonl` |
| `.skillopt.optout` marker is ignored | `helpers/governance.py` missing from plugin copy | Re-copy from workdir; confirm `execute.py` shows 47 required files |
| Auto-loop never fires | `auto_loop_enabled: false` or rollouts < `auto_loop_min_rollouts` | Flip toggle to `true`; lower threshold for testing; finish more chats |

---

### `execute.py` → `ERROR: skillopt_sleep not importable`

The package isn't installed in the A0 venv. From a shell in the A0 container:

```bash
/opt/venv/bin/python -m pip install skillopt
```

If `/opt/venv/bin/python` is the wrong interpreter, set `A0_VENV_PYTHON` (see above) and re-run.

### `pip install` works but `execute.py` still can't import

You're probably running `execute.py` with a different Python than the one `pip install` used. Check:

```bash
which python
python -c "import sys; print(sys.executable)"
/opt/venv/bin/python -c "import sys; print(sys.executable)"
```

They should match. If not, use the full path to the A0 venv's Python.

### The plugin loads but the dashboard shows zero rollouts

The harvester extension is the data source. Check that it's being loaded:

```bash
ls -la /a0/usr/plugins/skillopt/extensions/python/monologue_end/_60_skillopt_harvest_rollout.py
```

If the file is missing, the plugin wasn't fully installed. Re-copy from the workdir.

### Windows: `start_new_session=True` is not a valid kwarg

You're running an old version of `sleep_runner.py`. v1.1.0 uses `creationflags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP` on Windows. Re-copy the plugin from the workdir.

### Memory plugin's TimeoutError crashes the agent loop

This is a separate bug in the core `_memory` plugin, fixed in v1.2.0 (see `_memory/extensions/python/message_loop_prompts_after/_50_recall_memories.py` and `_91_recall_wait.py`). The fix wraps the search in a safe-task that swallows `TimeoutError` and `CancelledError`. Apply it via the standard A0 plugin update path.
