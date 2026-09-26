"""Best-effort privacy controls for rollout persistence and model prompts."""
from __future__ import annotations

import re
from typing import Any

_SECRET_VALUE = r"[^\s,;\"'<>]{8,}"
_ASSIGNMENT_RE = re.compile(
    r"(?P<prefix>\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|"
    r"password|passwd|secret|token|authorization)\b\s*[:=]\s*)"
    r"(?P<quote>[\"']?)(?P<value>" + _SECRET_VALUE + r")(?P=quote)",
    re.IGNORECASE,
)
_BEARER_RE = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE)
_URL_CREDENTIAL_RE = re.compile(r"\b(https?://)[^/@\s:]+:[^/@\s]+@", re.IGNORECASE)
_TOKEN_RE = re.compile(
    r"\b(?:AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
    r"xox[baprs]-[A-Za-z0-9-]{10,}|sk-[A-Za-z0-9_-]{16,})\b"
)
_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----.*?"
    r"-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
    re.IGNORECASE | re.DOTALL,
)


def _config() -> dict[str, Any]:
    try:
        from usr.plugins.skillopt.helpers import sleep_runner  # type: ignore
    except ImportError:
        try:
            from helpers import sleep_runner  # type: ignore
        except ImportError:
            return {}
    try:
        cfg = sleep_runner.merged_config()
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def privacy_settings(config: dict[str, Any] | None = None) -> dict[str, bool]:
    """Return conservative controls; explicit config can override defaults."""
    cfg = config if isinstance(config, dict) else _config()

    def as_bool(value: Any, default: bool) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"true", "1", "yes", "on"}:
                return True
            if normalized in {"false", "0", "no", "off"}:
                return False
        return default

    return {
        "redact_secrets": as_bool(cfg.get("privacy_redact_secrets"), True),
        "include_tool_args": as_bool(cfg.get("privacy_include_tool_args"), False),
        "include_tool_results": as_bool(cfg.get("privacy_include_tool_results"), False),
    }


def redact_text(value: Any, *, enabled: bool = True) -> str:
    """Redact common credential forms without raising on malformed values."""
    text = str(value or "")
    if not enabled or not text:
        return text
    text = _PRIVATE_KEY_RE.sub("[REDACTED_PRIVATE_KEY]", text)
    text = _BEARER_RE.sub("Bearer [REDACTED]", text)
    text = _URL_CREDENTIAL_RE.sub(r"\1[REDACTED]@", text)
    text = _TOKEN_RE.sub("[REDACTED_TOKEN]", text)

    def replace_assignment(match: re.Match[str]) -> str:
        value = match.group("value")
        # Do not turn ordinary short values (e.g. token=none) into noise.
        if len(value.strip(".xX*")) < 8:
            return match.group(0)
        return match.group("prefix") + match.group("quote") + "[REDACTED]" + match.group("quote")

    return _ASSIGNMENT_RE.sub(replace_assignment, text)


def sanitize_rollout(record: dict[str, Any], *, config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return a sanitized shallow copy suitable for writing or model input."""
    settings = privacy_settings(config)
    out = dict(record)
    redact = settings["redact_secrets"]
    for key in ("task", "last_response"):
        if key in out:
            out[key] = redact_text(out[key], enabled=redact)
    if "fragments_active_text" in out:
        out["fragments_active_text"] = redact_text(out["fragments_active_text"], enabled=redact)
    trajectory = out.get("trajectory")
    if isinstance(trajectory, list):
        safe_steps = []
        for step in trajectory:
            if not isinstance(step, dict):
                safe_steps.append(redact_text(step, enabled=redact))
                continue
            safe = dict(step)
            for field, include in (("args", settings["include_tool_args"]),
                                   ("result", settings["include_tool_results"])):
                if not include:
                    safe.pop(field, None)
                elif field in safe:
                    safe[field] = redact_text(safe[field], enabled=redact)
            safe_steps.append(safe)
        out["trajectory"] = safe_steps
    return out
