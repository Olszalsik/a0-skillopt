"""
SkillOpt plugin - user-triggered self-check script.

Run from the Plugins UI. Useful for:
- verifying the plugin files are in place after install/update
- printing diagnostic info (version, paths, package importability, config)
- checking for the presence of harvested rollouts and staged proposals

v1.1.0 changes:
- The health check now FAILS (non-zero exit) when the auto-loop is
  supposed to have run but never did. v1.0 always returned 0 with
  "Health check PASSED" even when cycles_run=0 and rollouts >= the
  threshold - i.e. the loop was broken and the user couldn't tell.
  A green tick when nothing works is the worst kind of UX.

Returns 0 on success, non-zero on failure.
"""

import json
import os
import sys


PLUGIN_NAME = "skillopt"
EXPECTED_VERSION = "1.6.1"


def main() -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    print(f"[{PLUGIN_NAME}] Plugin dir: {here}")

    # --- 1. Required files ---
    required = [
        "plugin.yaml",
        "README.md",
        "LICENSE",
        "default_config.yaml",
        "hooks.py",
        "tools/__init__.py",
        "tools/skillopt_status.py",
        "tools/skillopt_sleep.py",
        "tools/skillopt_setup.py",
        "helpers/__init__.py",
        "helpers/sleep_runner.py",
        "helpers/auto_loop.py",
        "helpers/direct_optimizer.py",
        "helpers/bridge.py",
        "helpers/reward_model.py",
        "helpers/ab_harness.py",
        "helpers/fragment_store.py",
        "helpers/inner_loop.py",
        "helpers/cadence.py",
        "helpers/budget.py",
        "helpers/failure_memory.py",  # v1.3.0 (Day-4 item 6)
        "helpers/cycle_history.py",   # v1.4.0 (Day-5 item 7)
        "helpers/governance.py",      # v1.5.0-Dev (Day-5 item 8)
        "helpers/official_adapter.py",  # v1.6.0 (Solution B official-engine bridge)
        "api/cycles.py",              # v1.4.0 (Day-5 item 7)
        "api/audit_log.py",           # v1.4.0 (Day-5 item 7)
        "scripts/train_reward_model.py",
        "scripts/calibrate_judge.py",
        "tests/__init__.py",
        "tests/smoke.py",
        "api/__init__.py",
        "api/status.py",
        "api/sleep.py",
        "api/adopt.py",
        "api/loop.py",
        "api/config.py",
        "api/fragments.py",
        "webui/config.html",
        "webui/skillopt-dashboard.js",
        "extensions/webui/page-head/skillopt-head.html",
        "extensions/webui/sidebar-end/skillopt-card.html",
        "extensions/python/banners/_10_skillopt_status.py",
        "extensions/python/agent_init/_50_skillopt_auto_loop.py",
        "extensions/python/hooks/_post_skill_adopt.py",
        "extensions/python/monologue_end/_60_skillopt_harvest_rollout.py",
        "extensions/python/monologue_start/_40_skillopt_warn.py",
        "agents/skillopt_trainer/agent.yaml",
    ]
    missing = [p for p in required if not os.path.isfile(os.path.join(here, p))]
    if missing:
        print(f"[{PLUGIN_NAME}] ERROR: missing files:")
        for m in missing:
            print(f"  - {m}")
        return 1
    print(f"[{PLUGIN_NAME}] OK: all 47 required files present")  # v1.5.0-Dev: +1 (governance); v1.4.0: +3 (cycle_history, cycles, audit_log)

    # --- 2. Manifest sanity ---
    try:
        import yaml  # type: ignore
    except ImportError:
        yaml = None

    with open(os.path.join(here, "plugin.yaml"), "r", encoding="utf-8") as f:
        manifest = f.read()
    if yaml is not None:
        try:
            data = yaml.safe_load(manifest)
        except Exception as e:
            print(f"[{PLUGIN_NAME}] ERROR: plugin.yaml is not valid YAML: {e}")
            return 2
    else:
        data = {}
        for line in manifest.splitlines():
            if ":" in line and not line.lstrip().startswith("#"):
                k, _, v = line.partition(":")
                data[k.strip()] = v.strip()

    name = (data.get("name") or "").strip()
    version = (data.get("version") or "").strip()
    if name != PLUGIN_NAME:
        print(f"[{PLUGIN_NAME}] ERROR: plugin name is {name!r}, expected {PLUGIN_NAME!r}")
        return 3
    if version != EXPECTED_VERSION:
        print(f"[{PLUGIN_NAME}] WARN: plugin version is {version!r}, expected {EXPECTED_VERSION!r}")
    else:
        print(f"[{PLUGIN_NAME}] OK: manifest version {version}")

    # --- 3. Python package importability ---
    pkg_info = {}
    try:
        import skillopt_sleep  # type: ignore
        pkg_info["present"] = True
        pkg_info["version"] = getattr(skillopt_sleep, "__version__", "unknown")
        pkg_info["path"] = os.path.dirname(skillopt_sleep.__file__)
        print(f"[{PLUGIN_NAME}] OK: skillopt_sleep importable, version {pkg_info['version']}")
    except ImportError as e:
        pkg_info["present"] = False
        pkg_info["import_error"] = str(e)
        # v1.6.0: the official package being absent is no longer a hard
        # failure — the auto-loop falls back to direct_optimizer when
        # use_official_engine is on but the package isn't importable.
        # Warn (so the user knows they're on the weaker fallback) but
        # don't fail the self-check.
        print(f"[{PLUGIN_NAME}] WARN: skillopt_sleep not importable: {e}")
        print(f"[{PLUGIN_NAME}]        the auto-loop will use the direct_optimizer fallback.")
        print(f"[{PLUGIN_NAME}]        install for the research-grade engine: pip install 'skillopt'")

    # --- 4. Toggle state ---
    toggle_on = os.path.isfile(os.path.join(here, ".toggle-1"))
    toggle_off = os.path.isfile(os.path.join(here, ".toggle-0"))
    if toggle_on:
        state = "ON"
    elif toggle_off:
        state = "OFF"
    else:
        state = "DEFAULT (enabled)"
    print(f"[{PLUGIN_NAME}] Toggle state: {state}")

    # --- 5. Rollout + staging snapshot ---
    rollouts = os.path.join(here, "logs", "rollouts")
    staging = os.path.join(here, "staging")
    rollout_count = 0
    if os.path.isdir(rollouts):
        rollout_count = len([n for n in os.listdir(rollouts) if not n.startswith(".")])
    staged = []
    if os.path.isdir(staging):
        staged = [n for n in os.listdir(staging) if not n.startswith(".")]
    print(f"[{PLUGIN_NAME}] Harvested rollouts: {rollout_count}")
    print(f"[{PLUGIN_NAME}] Staged proposals: {len(staged)} ({', '.join(staged[:5])}{'...' if len(staged) > 5 else ''})")

    # Auto-loop state
    auto_state_path = os.path.join(here, "logs", "runs", ".auto_loop_state.json")
    auto_state: dict = {}
    if os.path.isfile(auto_state_path):
        try:
            with open(auto_state_path) as f:
                auto_state = json.load(f)
            print(f"[{PLUGIN_NAME}] Auto-loop state:")
            print(f"[{PLUGIN_NAME}] running: {auto_state.get('running')}")
            print(f"[{PLUGIN_NAME}] cycles run: {auto_state.get('cycles_run', 0)}")
            print(f"[{PLUGIN_NAME}] adopted: {auto_state.get('proposals_adopted', 0)}")
            print(f"[{PLUGIN_NAME}] rejected: {auto_state.get('proposals_rejected', 0)}")
            if auto_state.get("last_error"):
                print(f"[{PLUGIN_NAME}] last error: {auto_state['last_error']}")
        except Exception as e:
            print(f"[{PLUGIN_NAME}] WARN: could not read auto-loop state: {e}")

    # v1.1.0: honest health check. v1.0 always said PASSED. Now we fail
    # if the loop was supposed to have run but didn't (rollouts >=
    # threshold, no cycles, auto_loop is enabled).
    last_err_path = os.path.join(here, "logs", "runs", ".auto_loop_last_error.json")
    if os.path.isfile(last_err_path):
        try:
            with open(last_err_path) as f:
                err = json.load(f)
            print(f"[{PLUGIN_NAME}] Last loop error: {err.get('where', '?')}: {err.get('type', '?')}: {err.get('message', '?')}")
        except Exception:
            pass

    cycles = int(auto_state.get("cycles_run", 0))
    cfg_path = os.path.join(here, "config.json")
    cfg: dict = {}
    if os.path.isfile(cfg_path):
        try:
            with open(cfg_path) as f:
                cfg = json.load(f)
        except Exception:
            pass
    threshold = int(cfg.get("auto_loop_min_rollouts", 10)) if cfg else 10
    auto_enabled = (cfg.get("auto_loop_enabled", True) if cfg else True)
    # If the auto-loop is enabled and we have enough rollouts to have
    # fired at least one cycle, but we haven't, that's a bug.
    loop_stalled = (
        auto_enabled
        and rollout_count >= threshold
        and cycles == 0
    )
    if loop_stalled:
        print()
        print(f"[{PLUGIN_NAME}] HEALTH CHECK FAILED.")
        print(f"[{PLUGIN_NAME}]   reason: auto_loop is enabled, rollouts={rollout_count} >= threshold={threshold},")
        print(f"[{PLUGIN_NAME}]   but cycles_run={cycles}. The loop never fired.")
        print(f"[{PLUGIN_NAME}]   inspect logs/runs/auto_loop.log and .auto_loop_last_error.json")
        return 5

    # --- 6. Summary ---
    print()
    print(json.dumps({
        "plugin": PLUGIN_NAME,
        "version": version,
        "expected_version": EXPECTED_VERSION,
        "toggle_state": state,
        "all_files_present": True,
        "package": pkg_info,
        "rollouts": rollout_count,
        "staged_proposals": staged,
        "auto_loop": auto_state,
    }, indent=2))
    print()
    print(f"[{PLUGIN_NAME}] Health check PASSED.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
