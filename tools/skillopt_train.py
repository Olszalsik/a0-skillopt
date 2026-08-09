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
        cmd = [sleep_runner._a0_python(), "-m", "skillopt", "--config", cfg_path]
        log = sleep_runner.runs_dir() / "train.log"
        log_fh = open(log, "ab", buffering=0)
        proc = subprocess.Popen(cmd, stdout=log_fh, stderr=subprocess.STDOUT, start_new_session=True)
        return Response(
            message=(
                f"Full training run launched: pid={proc.pid}, "
                f"log={log}.\n\nNOTE: this is a v2 feature and may take hours. "
                f"Prefer `skillopt_sleep verb=run` for incremental improvement."
            ),
            break_loop=False,
        )

    def _validate(self) -> Response:
        return Response(
            message=(
                "Validation gate is implemented in the post-adopt hook "
                "(extensions/python/hooks/_post_skill_adopt.py) and runs "
                "automatically when you call `skillopt_sleep verb=adopt`."
            ),
            break_loop=False,
        )
