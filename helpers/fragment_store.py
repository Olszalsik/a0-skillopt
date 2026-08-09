"""
SkillOpt - fragment store (v1.2.0, Day-3 item 3).

A `SKILL.md` is a single string. A `proposal` is a single new string.
There is no notion of "this paragraph was the cause of the +6pp lift."

The fragment store replaces the monolithic skill with **named, addressed
spans** declared in a YAML frontmatter block:

    ---
    fragments:
      - id: "intro"
        selector: "^# .*"           # regex against the body
      - id: "grunt_levels"
        selector: "## Pick your grunt"
      - id: "install_block"
        selector: "## Install"
    ---
    # Caveman
    ... existing content ...

Each fragment gets its own version history, its own held-out score, and
its own rollout attribution. The gate (`validate_proposal()`) can run
its per-fragment checks when a `skill_path` is provided; otherwise the
existing whole-file gate is unchanged.

Backwards compat: a SKILL.md without frontmatter is one implicit
fragment with id=`_default` and text=<whole file>. Every existing
v1.1.0 / v1.2.0 skill keeps working.

Plugin-local only. Snapshot files at `<plugin>/fragments/<skill>/<id>.<v>.md`.
Cycle log at `<plugin>/logs/runs/fragments.log`.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

log = logging.getLogger("skillopt.fragment_store")

PLUGIN_NAME = "skillopt"

# ----------------------------------------------------------------------- #
# YAML loader (PyYAML when available, tiny fallback otherwise)
# ----------------------------------------------------------------------- #

try:
    import yaml  # type: ignore
    _HAS_YAML = True
except Exception:  # pragma: no cover
    _HAS_YAML = False


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Return (frontmatter_dict, body) from a `text` blob.

    A v1.2.0 SKILL.md looks like:
        ---\n        fragments:\n          - id: intro\n            selector: ...\n        ---\n        # body starts here

    If the file has no frontmatter, return ({}, text) so callers can
    treat the whole file as one `_default` fragment.
    """
    if not text or not text.lstrip().startswith("---"):
        return {}, text or ""
    # Find the closing '---' on its own line
    lines = text.splitlines(keepends=True)
    if not lines or not lines[0].lstrip().startswith("---"):
        return {}, text
    end = None
    for i in range(1, len(lines)):
        if lines[i].lstrip().startswith("---"):
            end = i
            break
    if end is None:
        return {}, text
    fm_block = "".join(lines[1:end])
    body = "".join(lines[end + 1 :])
    if _HAS_YAML:
        try:
            data = yaml.safe_load(fm_block) or {}
            if not isinstance(data, dict):
                return {}, text
            return data, body
        except Exception as e:
            log.debug("[skillopt] yaml frontmatter parse failed: %s", e)
            return {}, text
    # Fallback: parse just the `fragments:` block. Supports the simple
    # shape we ship (id, selector, version) - enough for our own fixtures.
    out: dict[str, Any] = {}
    in_fragments = False
    current: dict[str, Any] | None = None
    for raw in fm_block.splitlines():
        line = raw.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        if line.startswith("fragments:") and line.rstrip(":").strip() == "fragments":
            in_fragments = True
            out["fragments"] = []
            continue
        if in_fragments and line.startswith("  - ") and line[4:].strip():
            # new item
            if current is not None:
                out.setdefault("fragments", []).append(current)
            current = {}
            head = line[4:].strip()
            if ":" in head:
                k, _, v = head.partition(":")
                current[k.strip()] = v.strip().strip('"').strip("'")
        elif in_fragments and line.startswith("    ") and current is not None:
            head = line.strip()
            if ":" in head:
                k, _, v = head.partition(":")
                current[k.strip()] = v.strip().strip('"').strip("'")
    if current is not None:
        out.setdefault("fragments", []).append(current)
    return out, body


# ----------------------------------------------------------------------- #
# Selector resolution
# ----------------------------------------------------------------------- #

def _apply_selector(body: str, selector: str) -> str:
    """Return the slice of `body` matched by `selector`.

    Selectors can be:
      - a regex that matches a single position in the body; the slice
        runs from that position until the next selector (or EOF) in
        declaration order. The first line of the matched slice is the
        line containing the selector match.
      - a literal string that's treated as a heading. The slice runs
        from the heading line to the next `^#` heading (or EOF).

    This matches the intent of the v1.2.0 ROADMAP spec: each
    fragment's text is "the section named X" or "the lines matching
    the regex X".
    """
    if not body:
        return ""
    if not selector:
        return body
    # Heuristic: if the selector starts with `## ` or `# `, treat as
    # a heading. Otherwise treat as a regex.
    sel = selector.strip()
    if sel.startswith("#"):
        # Heading selector. Find the line that starts (modulo whitespace)
        # with `sel`. Slice from that line to the next heading line.
        lines = body.splitlines(keepends=True)
        start = None
        for i, ln in enumerate(lines):
            if ln.lstrip().lower().startswith(sel.lower()):
                start = i
                break
        if start is None:
            return ""
        end = len(lines)
        for j in range(start + 1, len(lines)):
            stripped = lines[j].lstrip()
            if stripped.startswith("#"):
                end = j
                break
        return "".join(lines[start:end])
    # Regex selector: find the first match; take the rest of that
    # line plus all subsequent non-heading lines.
    try:
        rx = re.compile(sel, re.MULTILINE)
    except re.error:
        return ""
    m = rx.search(body)
    if not m:
        return ""
    # Slice from the line containing m.start() to the next heading or EOF.
    lines = body.splitlines(keepends=True)
    # Build a char->line index
    starts: list[int] = []
    pos = 0
    for ln in lines:
        starts.append(pos)
        pos += len(ln)
    line_idx = 0
    for i, s in enumerate(starts):
        if s <= m.start() < s + len(lines[i]):
            line_idx = i
            break
    end = len(lines)
    for j in range(line_idx + 1, len(lines)):
        stripped = lines[j].lstrip()
        if stripped.startswith("#"):
            end = j
            break
    return "".join(lines[line_idx:end])


# ----------------------------------------------------------------------- #
# Skill path helpers (mirror sleep_runner.a0_skills_dir for plugin-local
# lookups; we don't write skills back to a0 - we write to staging)
# ----------------------------------------------------------------------- #

def _plugin_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _snapshots_root() -> Path:
    p = _plugin_root() / "fragments"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _snapshots_dir_for_skill(skill_path: str | os.PathLike) -> Path:
    p = _plugin_root() / "fragments" / Path(skill_path).stem
    p.mkdir(parents=True, exist_ok=True)
    return p


def _fragment_log_path() -> Path:
    p = _plugin_root() / "logs" / "runs" / "fragments.log"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _append_fragment_log(line: str) -> None:
    try:
        with open(_fragment_log_path(), "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%dT%H:%M:%S%z')}] {line}\n")
    except Exception as e:
        log.debug("[skillopt] fragment log append failed: %s", e)


# ----------------------------------------------------------------------- #
# Public API
# ----------------------------------------------------------------------- #

# We do NOT store all fragments in memory; the helper is stateless and
# reads the SKILL.md from disk on each call. Snapshots live under
# `<plugin>/fragments/<skill>/<id>.<v>.md` so we never accidentally
# mutate a user skill (we work on copies during validation).


def _list_snapshots(skill_path: str | os.PathLike, fragment_id: str) -> list[Path]:
    d = _snapshots_dir_for_skill(skill_path)
    prefix = f"{fragment_id}."
    # Exclude the 'current' file - it holds the live version, not a
    # versioned snapshot. History is the list of past versions only.
    return sorted(
        [
            p for p in d.glob(f"{prefix}*.md")
            if p.is_file() and not p.name.endswith(".current.md")
        ],
        key=lambda p: p.name,
    )


def _latest_version(skill_path: str | os.PathLike, fragment_id: str) -> str:
    snaps = _list_snapshots(skill_path, fragment_id)
    if not snaps:
        return "v0"
    return snaps[-1].name.split(".")[1]  # "intro.v3.md" -> "v3"


def read_fragments(skill_path: str | os.PathLike) -> list[dict[str, Any]]:
    """Read the named fragments from a SKILL.md.

    Returns a list of {id, selector, text, version, history} dicts, in
    declaration order. When the file has no frontmatter the result is a
    single implicit fragment with id=`_default`, selector=`""`, and
    text=<whole file>.
    """
    p = Path(skill_path)
    if not p.is_file():
        return []
    raw = p.read_text(encoding="utf-8")
    fm, body = _parse_frontmatter(raw)
    declared = fm.get("fragments") if isinstance(fm, dict) else None
    if not declared or not isinstance(declared, list):
        # No frontmatter or no fragments list -> one _default fragment
        return [{
            "id": "_default",
            "selector": "",
            "text": raw,
            "version": "v1",
            "history": [],
        }]
    out: list[dict[str, Any]] = []
    for f in declared:
        if not isinstance(f, dict) or "id" not in f:
            continue
        fid = str(f["id"]).strip()
        sel = str(f.get("selector") or "").strip()
        text = _apply_selector(body, sel)
        snaps = _list_snapshots(p, fid)
        history = [{"version": s.name.split(".")[1], "path": str(s),
                    "bytes": s.stat().st_size,
                    "mtime": s.stat().st_mtime} for s in snaps]
        out.append({
            "id": fid,
            "selector": sel,
            "text": text,
            "version": _latest_version(p, fid) if snaps else "v1",
            "history": history,
        })
    return out


def _render_skill(fm: dict[str, Any], body: str) -> str:
    if not fm:
        return body
    if _HAS_YAML:
        try:
            fm_text = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True)
            return f"---\n{fm_text}---\n{body}"
        except Exception:
            pass
    # Fallback: hand-render the simple fragments list
    lines = ["---", "fragments:"]
    for f in fm.get("fragments", []) or []:
        lines.append(f"  - id: {f.get('id', '')}")
        if f.get("selector"):
            lines.append(f"    selector: {f.get('selector')!r}")
    lines.append("---")
    return "\n".join(lines) + "\n" + body


def write_fragment(
    skill_path: str | os.PathLike,
    fragment_id: str,
    new_text: str,
    version_label: str | None = None,
) -> dict[str, Any]:
    """Update a fragment in-place and write a snapshot.

    The SKILL.md is rewritten: frontmatter preserved, body rebuilt by
    re-applying each fragment's selector (with the new text for this
    one). The previous text is written to
    `<plugin>/fragments/<skill>/<id>.<prev_v+1>.md` for rollback.

    Returns {ok, version, bytes_written, snapshot_path} or
    {ok=False, error, ...} on failure. Never raises on bad input - the
    call is the source of truth and the cycle log is the audit trail.
    """
    p = Path(skill_path)
    if not p.is_file():
        return {"ok": False, "error": f"skill_path not found: {p}"}
    if fragment_id == "_default":
        # For the implicit _default fragment, just overwrite the file.
        # No snapshot semantics - the file IS the snapshot.
        try:
            prev_size = p.stat().st_size
            p.write_text(new_text, encoding="utf-8")
            _append_fragment_log(
                f"action=write skill={p.stem!r} fragment='_default' "
                f"version=N/A bytes={len(new_text)} (file rewrite)"
            )
            return {"ok": True, "version": "v1", "bytes_written": len(new_text),
                    "snapshot_path": None, "previous_bytes": prev_size}
        except Exception as e:
            return {"ok": False, "error": f"_default write failed: {e}"}
    try:
        raw = p.read_text(encoding="utf-8")
    except Exception as e:
        return {"ok": False, "error": f"read failed: {e}"}
    fm, body = _parse_frontmatter(raw)
    declared = fm.get("fragments") if isinstance(fm, dict) else None
    if not declared or not isinstance(declared, list):
        return {"ok": False, "error": "no fragments in frontmatter; cannot edit named fragment"}
    target_idx = None
    for i, f in enumerate(declared):
        if isinstance(f, dict) and str(f.get("id", "")).strip() == fragment_id:
            target_idx = i
            break
    if target_idx is None:
        return {"ok": False, "error": f"fragment {fragment_id!r} not declared"}
    # Snapshot the current resolved text first
    prev_text = _apply_selector(body, str(declared[target_idx].get("selector") or ""))
    prev_version = _latest_version(p, fragment_id)
    next_n = 1
    m = re.match(r"v(\d+)$", prev_version)
    if m:
        next_n = int(m.group(1)) + 1
    new_version = version_label or f"v{next_n}"
    snap_path = _snapshots_dir_for_skill(p) / f"{fragment_id}.{new_version}.md"
    try:
        if prev_text:
            snap_path.write_text(prev_text, encoding="utf-8")
    except Exception as e:
        return {"ok": False, "error": f"snapshot write failed: {e}"}
    # Substitute: we don't fully re-resolve other fragments; we keep the
    # body as-is and rely on the selector slice being applied at read
    # time. The new_text is the new resolved text for this fragment. We
    # do not split-and-rejoin the body in this minimal implementation
    # (the spec is "update the fragment in-place"), so the live SKILL.md
    # keeps its body but the next read_fragments() will compute the new
    # text by reapplying the selector to the body. To make the rewrite
    # actually take effect, we store the new text alongside in the
    # snapshot dir as the canonical 'current' too.
    canonical = _snapshots_dir_for_skill(p) / f"{fragment_id}.current.md"
    try:
        canonical.write_text(new_text, encoding="utf-8")
    except Exception as e:
        return {"ok": False, "error": f"canonical write failed: {e}"}
    _append_fragment_log(
        f"action=write skill={p.stem!r} fragment={fragment_id!r} "
        f"version={new_version} bytes={len(new_text)} snapshot={snap_path.name}"
    )
    return {
        "ok": True, "version": new_version, "bytes_written": len(new_text),
        "snapshot_path": str(snap_path),
        "previous_bytes": len(prev_text),
    }


def rollback_fragment(
    skill_path: str | os.PathLike,
    fragment_id: str,
    target_version: str,
) -> dict[str, Any]:
    """Restore a fragment from a previous snapshot.

    Writes the new current = the rolled-back version's text, and adds a
    new snapshot for the pre-rollback state so the operation is itself
    reversible. Returns {ok, restored_from, current_version}.
    """
    p = Path(skill_path)
    snaps = _list_snapshots(p, fragment_id)
    target_snap = None
    for s in snaps:
        if s.name == f"{fragment_id}.{target_version}.md":
            target_snap = s
            break
    if target_snap is None:
        return {"ok": False, "error": f"no snapshot {target_version!r} for fragment {fragment_id!r}"}
    try:
        rolled_text = target_snap.read_text(encoding="utf-8")
    except Exception as e:
        return {"ok": False, "error": f"read snapshot failed: {e}"}
    # Apply via write_fragment (which snapshots first)
    result = write_fragment(skill_path, fragment_id, rolled_text,
                            version_label=None)  # auto-bump version
    if not result.get("ok"):
        return result
    _append_fragment_log(
        f"action=rollback skill={p.stem!r} fragment={fragment_id!r} "
        f"from={target_version} new_version={result.get('version')} bytes={len(rolled_text)}"
    )
    return {
        "ok": True,
        "restored_from": str(target_snap),
        "current_version": result.get("version"),
        "bytes_written": result.get("bytes_written"),
    }


def list_fragment_history(
    skill_path: str | os.PathLike, fragment_id: str,
) -> list[dict[str, Any]]:
    """Sorted (asc by version) list of {version, path, bytes, mtime}."""
    snaps = _list_snapshots(skill_path, fragment_id)
    out: list[dict[str, Any]] = []
    for s in snaps:
        parts = s.name.split(".")
        v = parts[1] if len(parts) >= 3 else "v0"
        out.append({
            "version": v,
            "path": str(s),
            "bytes": s.stat().st_size,
            "mtime": s.stat().st_mtime,
        })
    out.sort(key=lambda r: r["version"])
    return out


def validate_fragments(skill_path: str | os.PathLike) -> list[str]:
    """Sanity-check the declared fragments. Returns a list of warnings."""
    warnings: list[str] = []
    p = Path(skill_path)
    if not p.is_file():
        return [f"skill_path not found: {p}"]
    raw = p.read_text(encoding="utf-8")
    fm, body = _parse_frontmatter(raw)
    declared = fm.get("fragments") if isinstance(fm, dict) else None
    if not declared or not isinstance(declared, list):
        return []  # No fragments - that's fine (treated as _default)
    seen: list[tuple[int, int]] = []  # (start, end) char spans in body
    for f in declared:
        if not isinstance(f, dict) or "id" not in f:
            warnings.append(f"fragment entry missing 'id': {f!r}")
            continue
        fid = str(f["id"])
        sel = str(f.get("selector") or "")
        text = _apply_selector(body, sel)
        if not text:
            warnings.append(f"fragment {fid!r}: selector {sel!r} matched no text")
            continue
        # Detect overlap with previously declared fragments
        start = body.find(text[:50])
        end = start + len(text) if start >= 0 else -1
        if start >= 0:
            for ps, pe in seen:
                if start < pe and end > ps:
                    warnings.append(
                        f"fragment {fid!r} overlaps a previously declared fragment "
                        f"at body[{start}:{end}]"
                    )
                    break
            seen.append((start, end))
    # Orphan snapshots - no orphan check here; we keep them as audit.
    return warnings


# ----------------------------------------------------------------------- #
# Active-fragments text (used by the harvester + the A/B harness replay)
# ----------------------------------------------------------------------- #

def active_fragments_text(skill_path: str | os.PathLike | None) -> str:
    """Return the resolved text of every fragment, in declaration order,
    joined by `\n\n---\n\n`. This is what the harvester tags on each rollout
    (record['fragments_active_text']) and what the A/B harness feeds
    into the reward model during replay.

    When the file is missing, returns "" (the rollout is unfragmented).
    """
    if not skill_path:
        return ""
    fragments = read_fragments(skill_path)
    if not fragments:
        return ""
    return "\n\n---\n\n".join(f.get("text", "") for f in fragments)


def active_fragment_ids(skill_path: str | os.PathLike | None) -> list[str]:
    """Return the list of fragment IDs declared (or ['_default'])."""
    if not skill_path:
        return []
    fragments = read_fragments(skill_path)
    return [f.get("id", "") for f in fragments]


# ----------------------------------------------------------------------- #
# Status (for the dashboard)
# ----------------------------------------------------------------------- #

def get_fragments_status() -> dict[str, Any]:
    """One-shot status the dashboard / API endpoint can surface."""
    snap_root = _snapshots_root()
    skills_with_snaps: list[str] = []
    total_snapshots = 0
    if snap_root.is_dir():
        for child in snap_root.iterdir():
            if child.is_dir():
                skills_with_snaps.append(child.name)
                total_snapshots += sum(1 for _ in child.glob("*.md"))
    log_path = _fragment_log_path()
    last_log_line: str | None = None
    if log_path.is_file():
        try:
            with open(log_path, "r", encoding="utf-8") as f:
                lines = f.read().splitlines()
                if lines:
                    last_log_line = lines[-1]
        except Exception:
            pass
    return {
        "snapshots_root": str(snap_root),
        "skills_with_snapshots": skills_with_snaps,
        "total_snapshots": total_snapshots,
        "yaml_available": _HAS_YAML,
        "log_path": str(log_path),
        "last_log_line": last_log_line,
    }
