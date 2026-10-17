"""Best-effort privacy controls for rollout persistence and model prompts."""
from __future__ import annotations

import re
from typing import Any

_SECRET_VALUE = r"[^\s,;\\\"'<>]{8,}"
# v1.8.39 (audit M10): the leading `\b` was the bug. `\b` is a WORD
# boundary and `_` is a word character, so in `AZURE_OPENAI_API_KEY=<secret>`
# there is no boundary before `API_KEY` and the whole alternative failed to
# match. Only the literal `sk-` / `ghp_` patterns rescued the best-known
# cases, so the most common credential form on the planet slipped through.
#
# `(?<![A-Za-z0-9])` is the correct guard: it permits a preceding `_` (so
# `AZURE_OPENAI_API_KEY` matches) while still refusing a match in the middle
# of an alphanumeric run. It cannot false-positive on `my_api_key_backup=`,
# because the pattern still requires `\s*[:=]` immediately after the name.
#
# The trailing `\b` is kept, and the quote group now accepts an ESCAPED
# quote so the same rule fires inside a JSON string embedded in text.
_ASSIGNMENT_RE = re.compile(
    r"(?P<prefix>(?<![A-Za-z0-9])(?:api[_-]?key|access[_-]?token|auth[_-]?token|"
    r"client[_-]?secret|private[_-]?key|secret[_-]?key|"
    r"password|passwd|secret|token|authorization)\b\s*[:=]\s*)"
    r"(?P<quote>\\?[\"']?)(?P<value>" + _SECRET_VALUE + r")(?P=quote)",
    re.IGNORECASE,
)
_BEARER_RE = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE)
# v1.8.39 (audit M10): was `https?://` only, so `postgres://user:pw@host`,
# `mongodb://`, `redis://`, `amqp://` sailed through. Now any scheme.
_URL_CREDENTIAL_RE = re.compile(
    # The user part is `*`, not `+`: `redis://:password@host` has an EMPTY
    # username, which is the normal form for a Redis ACL-less URL and was
    # missed by the `+`. The password part stays `+`.
    r"\b([a-z][a-z0-9+.\-]*://)[^/@\s:@]*:[^/@\s]+@", re.IGNORECASE)
_TOKEN_RE = re.compile(
    r"\b(?:AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
    r"xox[baprs]-[A-Za-z0-9-]{10,}|sk-[A-Za-z0-9_-]{16,})\b"
)
# v1.8.39 (audit M10): generalised from `RSA |EC |OPENSSH ` to ANY algorithm
# label, so DSA / PGP / ENCRYPTED private keys are covered too, plus PuTTY's
# entirely different key-file header.
_PRIVATE_KEY_RE = re.compile(
    # ` BLOCK` is optional and required for PGP, whose header ends
    # `... PRIVATE KEY BLOCK-----` rather than `... PRIVATE KEY-----`.
    r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----.*?"
    r"-----END (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----",
    re.IGNORECASE | re.DOTALL,
)
_PUTTY_KEY_RE = re.compile(
    r"PuTTY-User-Key-File-\d+:[^\n]*"
    r"(?:\n[^\n]*){0,12}?"
    r"(?:Private-Lines?:\s*\d+|Private-MAC:)",
    re.IGNORECASE,
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
    """Redact common credential forms without raising on malformed values.

    Still best-effort, not a guarantee — see the module docstring's known
    limitations. v1.8.39 (audit M10) closed the gaps that let the most
    common forms through: the `\b` word-boundary bug on underscore-prefixed
    env var names, non-HTTP URL credentials, DSA/PGP/ENCRYPTED/PuTTY keys,
    and assignments inside escaped-JSON strings.

    Deliberately NOT attempted: generic high-entropy base64 blobs. A rule
    that redacts any long base64 string would shred legitimate skill
    documents (hashes, checksums, encoded examples) to catch a secret
    inside one. That trade is worse than the gap, so it stays a documented
    limitation rather than a noisy heuristic."""
    text = str(value or "")
    if not enabled or not text:
        return text
    text = _PRIVATE_KEY_RE.sub("[REDACTED_PRIVATE_KEY]", text)
    text = _PUTTY_KEY_RE.sub("[REDACTED_PRIVATE_KEY]", text)
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
    # v1.8.39 (audit M9): only STRING fields are redacted. `redact_text`
    # does `str(value or "")`, so a present-but-null `task` was silently
    # rewritten to `""` - a lossy type change to persisted data, made for no
    # benefit. A non-string field cannot contain a credential pattern in a
    # way redaction can act on, so it is left exactly as it was.
    for key in ("task", "last_response"):
        if key in out and isinstance(out[key], str):
            out[key] = redact_text(out[key], enabled=redact)
    if isinstance(out.get("fragments_active_text"), str):
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
            # v1.8.45 (audit R14): redact the step's OWN free-text fields
            # too. `args`/`result` were the only fields considered, so a
            # `text` / `content` / `name` / `observation` on a step shipped
            # verbatim - and the inner-loop egress passes whole trajectories,
            # so a credential quoted in a tool's output or a note left the
            # process. Any string field is a candidate; the two policy fields
            # above have already been popped or redacted, so redacting them
            # again is a no-op.
            for field, val in list(step.items()):
                if field in ("args", "result"):
                    continue
                if isinstance(val, str):
                    safe[field] = redact_text(val, enabled=redact)
            safe_steps.append(safe)
        out["trajectory"] = safe_steps
    return out


def _redacted_shell(record: dict[str, Any]) -> dict[str, Any]:
    """A same-shaped record with every free-text value dropped.

    v1.8.47 (re-audit R26). Used when `sanitize_rollout` raises. Keeps the
    keys - the held-out gate reads `task` / `last_response` / `trajectory`
    by name and compares pairs BY INDEX, so the shape must survive - and
    replaces every value with a marker. A redactor that falls back to the
    input on failure has not failed safely.
    """
    shell: dict[str, Any] = {}
    for key, val in record.items():
        if key in ("trajectory",):
            steps = val if isinstance(val, list) else []
            shell[key] = [
                ({k: "[REDACTED]" for k in s} if isinstance(s, dict)
                 else "[REDACTED]")
                for s in steps
            ]
        elif isinstance(val, str):
            shell[key] = "[REDACTED]"
        elif isinstance(val, (int, float, bool)) or val is None:
            # Counters and flags carry no free text and are useful to a reader.
            shell[key] = val
        else:
            shell[key] = "[REDACTED]"
    return shell


def sanitize_rollout_records(
    records: Any, *, config: dict[str, Any] | None = None
) -> Any:
    """Sanitize a LIST of rollout records. Non-list input passes through.

    v1.8.39 (audit M9). The real-gate worker freezes whole rollout records to
    `logs/runs/*.tasks.json` and re-reads them on every replay, so an
    unsanitized credential captured before the privacy controls existed (or
    missed by the best-effort redactor) was being written verbatim to disk and
    then re-injected into a live agent context. Sanitizing at the persistence
    boundary means the durable artefact is safe regardless of what a future
    reader does.

    Never raises, and preserves list order and length - the held-out gate
    compares by index, so a dropped or reordered element would silently change
    which pairs are measured.
    """
    if not isinstance(records, list):
        return records
    out = []
    for rec in records:
        if isinstance(rec, dict):
            try:
                out.append(sanitize_rollout(rec, config=config))
            except Exception:
                # v1.8.47 (re-audit R26): the "never raises" contract was
                # satisfied by appending the RAW record - persisting exactly
                # what this function exists to remove. Fall back to a
                # REDACTED SHELL instead: the element's position and the
                # gate's field names are preserved (the held-out gate compares
                # by index) while every free-text value is dropped.
                out.append(_redacted_shell(rec))
        else:
            out.append(rec)
    return out
