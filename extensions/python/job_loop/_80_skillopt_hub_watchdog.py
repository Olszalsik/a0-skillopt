"""SkillOpt - Hub PR #512 merge watchdog (job_loop extension, added v1.8.15).

The framework fires this hook from its background scheduler loop
(helpers/job_loop.py scheduler_tick() -> call_extensions_async("job_loop")),
which runs on the scheduler's asyncio loop - never the main chat thread.
While PR #512 (skillopt in the agent0ai/a0-plugins Plugin Hub) is open, this
hook periodically refreshes logs/hub_status.json by running
scripts/check_hub_status.py as a NON-BLOCKING asyncio subprocess with a
strict 3s timeout, throttled by the state file mtime: the script only runs
when the file is older than 1800s (or missing).

Once the PR merges, the first tick past the throttle window records
MERGED_INDEXED / MERGED_UNINDEXED into logs/hub_status.json; the
/api/plugins/skillopt/hub_status endpoint reads the same file, so WebUI
consumers see the transition within one throttle window without manual
watchdog runs.

Design notes:
- stdlib only (asyncio, logging, sys, time, pathlib).
- execute() never raises into the job loop: broad try/except, logged.
- Never blocks a loop: asyncio.create_subprocess_exec + asyncio.wait_for.
  On timeout the child is killed explicitly (wait_for only abandons the
  wait) and the stale state file is left for the next tick to retry.
- Single-flight via a module-level flag: overlapping ticks (long subprocess
  tail) never double-spawn the watchdog script. Cross-module duplication
  with api/hub_status.py's refresh guard is impossible in practice (mtime
  throttle) and harmless if it ever happens (the script appends atomically).
- Paths are derived from this file's location, not CWD.
- Subprocess uses sys.executable (framework runtime, same convention as
  api/hub_status.py); the script is pure stdlib.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from pathlib import Path

from helpers.extension import Extension

log = logging.getLogger(__name__)

THROTTLE_S = 1800.0        # skip when logs/hub_status.json is younger than this
SUBPROCESS_TIMEOUT_S = 3.0 # hard cap for the watchdog subprocess

_THIS_DIR = Path(__file__).resolve().parent      # .../extensions/python/job_loop
_PLUGIN_ROOT = _THIS_DIR.parent.parent.parent    # .../skillopt
_STATE_FILE = _PLUGIN_ROOT / "logs" / "hub_status.json"
_SCRIPT = _PLUGIN_ROOT / "scripts" / "check_hub_status.py"

# Process-wide single-flight flag: the check-and-set below is atomic on a
# single event loop (no await between check and set). Deliberately a plain
# flag, not an asyncio.Lock - locks bind to the loop that first awaits them,
# while ad-hoc callers (acceptance checks) run on fresh loops.
_RUNNING = False


class HubWatchdogExtension(Extension):

    def __init__(self, agent: "Agent|None" = None, **kwargs):
        # Framework instantiates cls(agent=agent); the None default also lets
        # the class be constructed bare (acceptance / ad-hoc diagnostics).
        super().__init__(agent, **kwargs)

    async def execute(self, **kwargs) -> None:
        # Broad contract: background exceptions are caught, logged, and never
        # propagate into Agent Zero's job loop.
        try:
            if not self._is_stale():
                log.debug("[skillopt] hub watchdog: state file fresh, skipping")
                return
            await self._run_watchdog()
        except Exception as e:  # noqa: BLE001 - deliberate catch-all
            log.warning(
                "[skillopt] hub watchdog extension error: %s: %s",
                type(e).__name__,
                e,
            )

    def _is_stale(self) -> bool:
        try:
            age = time.time() - _STATE_FILE.stat().st_mtime
        except FileNotFoundError:
            return True
        return age >= THROTTLE_S

    async def _run_watchdog(self) -> None:
        global _RUNNING
        if not _SCRIPT.is_file():
            log.warning("[skillopt] hub watchdog: script missing: %s", _SCRIPT)
            return
        if _RUNNING:
            log.debug("[skillopt] hub watchdog: refresh already running, skipping")
            return
        _RUNNING = True
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, str(_SCRIPT),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                returncode = await asyncio.wait_for(
                    proc.wait(), timeout=SUBPROCESS_TIMEOUT_S
                )
                log.info(
                    "[skillopt] hub watchdog refresh finished (exit %s)",
                    returncode,
                )
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                    await proc.wait()
                except Exception:  # noqa: BLE001 - best-effort kill
                    pass
                log.warning(
                    "[skillopt] hub watchdog subprocess timed out after %.1fs "
                    "(killed; stale state file left for next tick)",
                    SUBPROCESS_TIMEOUT_S,
                )
        finally:
            _RUNNING = False
