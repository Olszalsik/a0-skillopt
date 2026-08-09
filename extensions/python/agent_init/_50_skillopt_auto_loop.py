"""
SkillOpt auto-loop starter — Agent Zero extension hook (with watchdog).

This hook is called by the A0 framework on agent_init. To make the loop
robust across A0 process restarts (the framework only fires agent_init on
new agent contexts, not every process startup), this extension starts a
long-lived daemon *watchdog* thread on every agent_init call. The watchdog
periodically checks that the actual auto-loop thread is alive, and
re-starts it if it has died. As long as one agent_init hook fires per
process, the loop is self-healing from then on.

The thread is a daemon, so it never blocks A0 shutdown. The state file
(logs/runs/.auto_loop_state.json) persists across restarts so the loop
picks up where it left off.

This is the user-facing implementation of "I want it to work
automatically" — set the toggles in the WebUI once and forget.
"""

from __future__ import annotations

import logging
import threading
import time

log = logging.getLogger(__name__)


# Module-level singletons so the threads are started exactly once per process.
_loop_thread = None
_loop_lock = threading.Lock()
_watchdog_thread = None


def _get_config() -> dict:
    """Read the SkillOpt plugin config from the framework, with a fallback to
    the deployed plugin's on-disk config.json.

    The framework's plugin config registry may return empty in some lifecycle
    contexts (e.g. when the agent_init hook runs before the per-project config
    has been hydrated, or if the user has not yet opened the plugin's WebUI
    config page). When that happens, the auto-loop thread spins forever on
    `_sleep(30)` and never fires a cycle. To keep the loop honest, we always
    fall back to reading the deployed config.json when the framework lookup
    returns nothing usable.
    """
    cfg: dict = {}
    # 1. Try the framework's plugin config registry first.
    try:
        from helpers import plugins as plugins_helper  # type: ignore
        cfg = plugins_helper.get_plugin_config("skillopt") or {}
    except Exception as e:
        log.debug("[skillopt] framework config read failed: %s", e)
    if cfg:
        return cfg
    # 2. Fallback: read the deployed plugin's config.json directly.
    try:
        import json
        from pathlib import Path
        # /a0/usr/plugins/skillopt/extensions/python/agent_init/_50_...py
        # walk 4 .parent() calls up to the plugin root
        plugin_root = Path(__file__).resolve().parent.parent.parent.parent
        cfg_path = plugin_root / "config.json"
        if cfg_path.is_file():
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            log.info(
                "[skillopt] config loaded from on-disk config.json (%d keys)",
                len(cfg),
            )
            return cfg
    except Exception as e:
        log.warning("[skillopt] config.json fallback failed: %s", e)
    return cfg


def _start_loop_if_needed() -> None:
    """Start the auto_loop + inner_loop threads if they're not already alive.

    Idempotent and safe to call from multiple contexts (the watchdog calls
    it every ~30s, but the underlying start is gated by is_alive()).
    """
    global _loop_thread
    with _loop_lock:
        if _loop_thread is not None and _loop_thread.is_alive():
            return  # already running, no-op
        try:
            from usr.plugins.skillopt.helpers import auto_loop  # type: ignore
        except Exception as e:
            log.warning("[skillopt] cannot import auto_loop: %s", e)
            return
        _loop_thread = auto_loop.AutoLoopThread(get_config=_get_config)
        _loop_thread.start()
        log.info(
            "[skillopt] auto-loop started (pid=%s, thread=%s)",
            __import__("os").getpid(),
            _loop_thread.name,
        )
        # v1.3.0 (Day-4 item 4): start the inner-loop thread alongside
        # the auto-loop. Same lifecycle, faster cadence, separate
        # responsibilities. The inner loop NEVER writes to staging/ or
        # SKILL.md - it only produces per-rollout suggestions. The
        # auto-loop reads them via list_pending_suggestions() at the
        # start of each cycle. See helpers/inner_loop.py for the
        # inner-loop contract.
        inner = auto_loop.start_inner_loop(get_config=_get_config)
        if inner is not None:
            log.info(
                "[skillopt] inner-loop started (pid=%s, thread=%s)",
                __import__("os").getpid(),
                inner.name,
            )
        else:
            log.debug("[skillopt] inner-loop disabled or failed to start")


def _watchdog_loop() -> None:
    """Daemon that periodically checks the auto_loop thread and restarts it
    if it has died. Runs forever. Started once per process by agent_init.

    Without this, the auto_loop thread can silently die when:
    - the A0 process is restarted by the watchdog (no new agent_init fires
      on framework startup alone, only on new chat contexts)
    - the framework's plugin config storage is briefly empty during a
      per-project config reload (the thread spins on empty config and the
      user sees nothing in the dashboard)
    - any transient exception kills the thread silently (the run() loop
      already catches Exception, but if the thread itself crashes outside
      the try/except, nothing restarts it)
    """
    log.info(
        "[skillopt] auto-loop watchdog started (pid=%s)",
        __import__("os").getpid(),
    )
    while True:
        try:
            _start_loop_if_needed()
        except Exception as e:
            log.warning("[skillopt] watchdog tick error: %s", e)
        time.sleep(30)


def _start_watchdog() -> None:
    """Start the watchdog daemon thread exactly once per process."""
    global _watchdog_thread
    if _watchdog_thread is not None and _watchdog_thread.is_alive():
        log.debug("[skillopt] watchdog already running")
        return
    _watchdog_thread = threading.Thread(
        target=_watchdog_loop,
        name="skillopt-auto-loop-watchdog",
        daemon=True,
    )
    _watchdog_thread.start()
    # Also try to start the loop immediately so the first tick happens
    # without waiting 30s for the watchdog to fire.
    _start_loop_if_needed()


def execute(**kwargs):  # type: ignore[no-untyped-def]
    """A0 extension entry point — called on agent_init.

    Instead of starting the loop directly here (which would mean the loop
    only starts when this hook fires, and the hook only fires on new
    chat contexts), we start the watchdog. The watchdog then takes care
    of starting the loop thread itself, and keeps it alive across the
    many failure modes that would otherwise leave the user with a silent,
    non-firing auto-loop.
    """
    _start_watchdog()
