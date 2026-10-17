"""SkillOpt full training loop tool — STUB for v1.

The full `skillopt` training loop is a much heavier operation than
`skillopt_sleep` — it needs benchmark data the user has to provide
and runs for hours. For v1 we keep this as a thin wrapper around
`python -m skillopt` (note: the underscore CLI binary isn't always
on PATH; the module form is portable).

If you need the full training loop, install the relevant benchmark
extra (`pip install 'skillopt[alfworld]'` etc.) and provide a
benchmark manifest, then call this tool with `kind=run`.

Args:
  kind: info | run | validate (default info)
  config: path to a SkillOpt YAML config (required for kind=run)
"""

from __future__ import annotations

import json
import os
import subprocess
import time

from helpers.tool import Response, Tool  # type: ignore

from usr.plugins.skillopt.helpers import sleep_runner  # type: ignore


class SkilloptTrain(Tool):
    async def execute(self, **kwargs) -> Response:
        kind = (self.args.get("kind") or "info").lower()

        if kind == "info":
            return self._info()
        if kind == "run":
            return self._run()
        if kind == "validate":
            return self._validate()

        return Response(
            message=f"Unknown kind: {kind!r}. Valid: info, run, validate.",
            break_loop=False,
        )

    def _info(self) -> Response:
        info: dict[str, object] = {"available": False}
        try:
            import skillopt  # type: ignore
            info["available"] = True
            info["version"] = getattr(skillopt, "__version__", "unknown")
            info["package_path"] = os.path.dirname(skillopt.__file__)
            info["submodules"] = sorted(
                d for d in os.listdir(info["package_path"])
                if os.path.isdir(os.path.join(info["package_path"], d))
            )
        except ImportError as e:
            info["error"] = str(e)
        info["note"] = (
            "Full training is a v2 feature. Use skillopt_sleep for the "
            "self-evolution loop. To run a benchmark, install the relevant "
            "extra (e.g. `pip install 'skillopt[alfworld]'`) and provide a "
            "SkillOpt YAML config."
        )
        return Response(
            message=json.dumps(info, indent=2, default=str),
            break_loop=False,
        )

    def _run(self) -> Response:
        cfg_path = (self.args.get("config") or "").strip()
        if not cfg_path or not os.path.isfile(cfg_path):
            return Response(
                message=(
                    "kind=run requires a valid `config` path to a SkillOpt YAML. "
                    "See https://microsoft.github.io/SkillOpt/docs/guideline.html "
                    "for the config schema."
                ),
                break_loop=False,
            )
        # v1.8.47 (re-audit R27): single-flight. A training run is hours
        # long; launching a second silently doubles CPU/GPU load and both
        # write the same artifacts. Liveness is decided with
        # `sleep_runner.is_running`, not `os.kill(pid, 0)` - the latter is
        # Ctrl+C on Windows.
        state_path = sleep_runner.runs_dir() / "train_state.json"
        try:
            prev = json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:
            prev = {}
        prev_pid = prev.get("pid") if isinstance(prev, dict) else None
        if prev_pid:
            try:
                _busy = sleep_runner.is_running(int(prev_pid))
            except Exception:
                _busy = False
            if _busy:
                return Response(
                    message=(
                        f"A training run is already in flight (pid={prev_pid}). "
                        f"Wait for it to finish, or stop that process "
                        f"first - one at a time."
                    ),
                    break_loop=False,
                )

        cmd = [sleep_runner._a0_python(), "-m", "skillopt", "--config", cfg_path]
        log = sleep_runner.runs_dir() / "train.log"
        log_fh = open(log, "ab", buffering=0)
        proc = subprocess.Popen(cmd, stdout=log_fh, stderr=subprocess.STDOUT, start_new_session=True)
        # v1.8.47 (R27): close OUR copy of the handle. `Popen` duplicates
        # it for the child, so the parent does not need it - and an open
        # handle also blocks rotation of this log on Windows.
        try:
            log_fh.close()
        except Exception:
            pass
        # Record the pid so the next invocation can detect it and an
        # operator can find it. Atomic, like every other small state file.
        try:
            state_path.parent.mkdir(parents=True, exist_ok=True)
            _st = state_path.with_suffix(".json.tmp")
            _st.write_text(json.dumps({
                "pid": proc.pid,
                "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "log": str(log),
                "config": cfg_path,
            }), encoding="utf-8")
            os.replace(str(_st), str(state_path))
        except Exception:
            pass
        return Response(
            message=(
                f"Full training run launched: pid={proc.pid}, "
                f"log={log}.\n\nNOTE: this is a v2 feature and may take hours. "
                f"Prefer `skillopt_sleep verb=run` for incremental improvement."
            ),
            break_loop=False,
        )

    def _validate(self) -> Response:
        # v1.8.24: this used to point at extensions/python/hooks/
        # _post_skill_adopt.py, which was dead - `hooks` is not an extension
        # point in this framework, and the file was a module-level function
        # rather than an Extension subclass, so the loader could never have
        # found it. The gate is not a hook: it is sleep_runner.validate_proposal,
        # called by every adopt path. Deleted with the file.
        return Response(
            message=(
                "Validation is not a hook. It is `sleep_runner.validate_proposal`, "
                "called by every adopt path (the auto-loop drain, "
                "`skillopt_sleep verb=adopt`, and `POST /adopt`). It checks the "
                "framework skill schema first, then the structural stages, and "
                "the harvest applies the real-gate verdict: a completed negative "
                "verdict quarantines, and only a missing verdict fails open."
            ),
            break_loop=False,
        )
