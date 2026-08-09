"""SkillOpt setup tool — translate A0's existing LLM env into SkillOpt's expected shape.

SkillOpt has an env-var naming gotcha: it reuses the `AZURE_OPENAI_*`
family for plain OpenAI, and `AZURE_OPENAI_ENDPOINT` is required for
every OpenAI auth mode. This tool reads A0's existing chat-LLM env
(usually `A0_CHAT_LLM_*` or a `chat_llm` provider config) and writes
the equivalent SkillOpt env into a small `.skillopt-env` file in the
plugin's runs dir. The next `skillopt_sleep` invocation will `source`
this file automatically.

Args:
  backend: auto | azure_openai | openai_compatible | claude | qwen | minimax
  dry_run: true | false (default false)
"""

from __future__ import annotations

import os
from pathlib import Path

from helpers.tool import Response, Tool  # type: ignore

from usr.plugins.skillopt.helpers import sleep_runner  # type: ignore


ENV_FILENAME = ".skillopt-env"


# Common A0 env var names (vary by deployment — extend as needed)
A0_VAR_MAP = {
    # A0 chat LLM endpoint and key
    "A0_CHAT_LLM_BASE_URL": "AZURE_OPENAI_ENDPOINT",
    "A0_CHAT_LLM_API_KEY": "AZURE_OPENAI_API_KEY",
    "A0_LLM_API_KEY": "AZURE_OPENAI_API_KEY",
    "OPENAI_API_KEY": "AZURE_OPENAI_API_KEY",
    "OPENAI_BASE_URL": "AZURE_OPENAI_ENDPOINT",
    "ANTHROPIC_API_KEY": "ANTHROPIC_API_KEY",
}


def _build_env_block(backend: str) -> dict[str, str]:
    """Map A0's env into SkillOpt's expected variable names."""
    out: dict[str, str] = {}
    for src_var, dst_var in A0_VAR_MAP.items():
        v = os.environ.get(src_var)
        if v:
            out[dst_var] = v

    # Azure API version has a sensible default; keep whatever A0 exposes
    if not out.get("AZURE_OPENAI_API_VERSION"):
        out["AZURE_OPENAI_API_VERSION"] = os.environ.get(
            "A0_CHAT_LLM_API_VERSION", "2024-12-01-preview"
        )

    # Pick the right auth mode for the requested backend
    if backend == "openai_compatible":
        out["AZURE_OPENAI_AUTH_MODE"] = "openai_compatible"
    elif backend == "azure_openai":
        # Default to API key; user can override by setting the env var themselves
        out.setdefault("AZURE_OPENAI_AUTH_MODE", "api_key")
    # claude / qwen / minimax use the backend's own vars (already mapped above)

    return out


class SkilloptSetup(Tool):
    async def execute(self, **kwargs) -> Response:
        backend = (self.args.get("backend") or "auto").lower()
        dry_run = str(self.args.get("dry_run") or "").lower() in ("1", "true", "yes")

        if backend not in ("auto", "azure_openai", "openai_compatible", "claude", "qwen", "minimax"):
            return Response(
                message=f"Unknown backend: {backend!r}. Valid: auto, azure_openai, openai_compatible, claude, qwen, minimax.",
                break_loop=False,
            )

        if backend == "auto":
            # Pick the first backend whose key is present in the env
            for candidate in ("openai_compatible", "azure_openai", "claude", "qwen", "minimax"):
                env = _build_env_block(candidate)
                if env.get("AZURE_OPENAI_API_KEY") or env.get("ANTHROPIC_API_KEY"):
                    backend = candidate
                    break
            else:
                backend = "openai_compatible"

        env = _build_env_block(backend)
        env["SKILLOPT_BACKEND"] = backend

        target = sleep_runner.runs_dir() / ENV_FILENAME
        body = "\n".join(f'export {k}="{v}"' for k, v in env.items()) + "\n"

        if dry_run:
            return Response(
                message=(
                    f"[dry-run] Would write {len(env)} env vars to {target}:\n\n"
                    + body
                    + f"\nBackend: {backend}"
                ),
                break_loop=False,
            )

        sleep_runner.runs_dir().mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")

        # Also surface the merged config for transparency
        cfg = sleep_runner.merged_config()
        cfg["backend"] = backend
        return Response(
            message=(
                f"Setup complete. Backend={backend}, env written to {target}.\n"
                f"Use `skillopt_sleep verb=run` to start a cycle — it will "
                f"`source` this file automatically.\n\n"
                f"Vars set ({len(env)}): {', '.join(sorted(env.keys()))}"
            ),
            break_loop=False,
        )
