"""SkillOpt setup tool — translate A0's existing LLM env into SkillOpt's expected shape.

SkillOpt has an env-var naming gotcha: it reuses the `AZURE_OPENAI_*`
family for plain OpenAI, and `AZURE_OPENAI_ENDPOINT` is required for
every OpenAI auth mode. This tool reads A0's existing chat-LLM env
and writes the equivalent SkillOpt env into `.skillopt-env` in the
plugin's runs dir. The next `skillopt_sleep` invocation will `source`
this file automatically.

v1.8.5 SECURITY: credential values are NEVER written to `.skillopt-env`.
Mapped keys are written as `${SOURCE_NAME}` references (resolution via
`sleep_runner._expand_env`: os.environ first, then the framework
usr/.env fallback). Credential values live only in `<project>/usr/.env`
(chmod 600, outside the plugin repo). Dry-run output contains key NAMES
and resolution booleans only — never values. An existing plaintext
credential found in the file is rewritten to a reference by the
sanitize pass. See helpers/setup_env.py.

Args:
 backend: auto | azure_openai | openai_compatible | claude | qwen | minimax
 dry_run: true | false (default false)
"""

from __future__ import annotations

from helpers.tool import Response, Tool  # type: ignore

try:
    from usr.plugins.skillopt.helpers import setup_env, sleep_runner  # type: ignore
except Exception:  # dev/test import path
    from helpers import setup_env, sleep_runner  # type: ignore


class SkilloptSetup(Tool):
    async def execute(self, **kwargs) -> Response:
        backend = (self.args.get("backend") or "auto").lower()
        dry_run = str(self.args.get("dry_run") or "").lower() in ("1", "true", "yes")

        if backend not in setup_env.BACKENDS:
            return Response(
                message=(
                    f"Unknown backend: {backend!r}. "
                    f"Valid: {', '.join(setup_env.BACKENDS)}."
                ),
                break_loop=False,
            )

        names = setup_env._source_names()
        if backend == "auto":
            for candidate in ("openai_compatible", "azure_openai", "claude", "qwen", "minimax"):
                block = setup_env.build_env_block(candidate, names)
                if any(str(v).startswith("${") for v in block.values()):
                    backend = candidate
                    break
            else:
                backend = "openai_compatible"

        if dry_run:
            block = setup_env.build_env_block(backend, names)
            refs = [
                (
                    k
                    + " -> "
                    + ("resolves" if setup_env.ref_resolves(v) else "UNRESOLVED (add to usr/.env)")
                )
                for k, v in sorted(block.items())
                if str(v).startswith("${")
            ]
            literals = [k for k, v in sorted(block.items()) if not str(v).startswith("${")]
            return Response(
                message=(
                    "[dry-run] Would write key REFERENCES to "
                    f"{sleep_runner.runs_dir() / setup_env.ENV_FILENAME}"
                    " (no plaintext values):\n"
                    + "\n".join(refs)
                    + "\nLiterals (non-secret): "
                    + ", ".join(literals)
                    + f"\nBackend: {backend}"
                ),
                break_loop=False,
            )

        result = setup_env.apply(backend)
        if not result.get("ok"):
            return Response(
                message=f"Setup failed: {result.get('error')}", break_loop=False
            )
        cfg = sleep_runner.merged_config()
        cfg["backend"] = backend
        return Response(
            message=(
                f"Setup complete. Backend={result['backend']}, env written to "
                f"{result['path']} (atomic, chmod 600).\n"
                "Credential keys were written as ${VAR} references; values live "
                "in the project usr/.env only.\n"
                f"Keys ({len(result['keys'])}): {', '.join(result['keys'])}\n"
                "Sanitized (plaintext -> reference): "
                + (", ".join(result["fixed"]) or "none")
                + "\nUnresolved references (add these names to usr/.env): "
                + (", ".join(result["unresolved"]) or "none")
                + "\nUse `skillopt_sleep verb=run` to start a cycle — it will "
                "`source` this file automatically."
            ),
            break_loop=False,
        )