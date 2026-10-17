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

def _selector_span(body: str, selector: str) -> tuple[int, int]:
    """Character span of `selector`'s slice in `body`, or (-1, -1).

    The single source of truth for BOTH halves of the selector feature:
    `_apply_selector` returns the text, and `_splice_selector` replaces it.
    Before v1.8.50 there was no span at all, so a "fragment write" could only
    record the new text next to the snapshot and never touch the document -
    the function reported `ok: True` and `read_fragments` returned the OLD
    text, because the body's own slice was unchanged. Heading and regex
    selectors are resolved identically here: the matched line through to the
    next heading (or EOF).
    """
    if not body:
        return (-1, -1)
    sel = (selector or "").strip()
    if not sel:
        # An empty selector means the whole body.
        return (0, len(body))
    lines = body.splitlines(keepends=True)
    if sel.startswith("#"):
        start = None
        for i, ln in enumerate(lines):
            if ln.lstrip().lower().startswith(sel.lower()):
                start = i
                break
        if start is None:
            return (-1, -1)
        line_idx = start
    else:
        try:
            rx = re.compile(sel, re.MULTILINE)
        except re.error:
            return (-1, -1)
        m = rx.search(body)
        if not m:
            return (-1, -1)
        pos = 0
        line_idx = 0
        for i, ln in enumerate(lines):
            if pos <= m.start() < pos + len(ln):
                line_idx = i
                break
            pos += len(ln)
    end = len(lines)
    for j in range(line_idx + 1, len(lines)):
        if lines[j].lstrip().startswith("#"):
            end = j
            break
    starts = []
    pos = 0
    for ln in lines:
        starts.append(pos)
        pos += len(ln)
    char_start = starts[line_idx]
    char_end = starts[end] if end < len(lines) else len(body)
    return (char_start, char_end)


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

_TEST_ROOT: "Path | None" = None


def set_plugin_root_for_tests(root: "str | os.PathLike | None") -> None:
    """Redirect the snapshot tree (tests only). `None` restores the real root.

    v1.8.51 (sweep follow-up): `fragments/` is the one piece of production
    state the suite wrote into directly, because `_plugin_root()` was
    hardcoded to the plugin directory. Every fragment test therefore created
    `<plugin>/fragments/<fixture-skill>/` on a live install - which is how 0-byte
    `_default.pre_adopt.md` files accumulated there at all, and why a sweep of
    them was undone by the next suite run. `staging/` and `logs/runs/` are
    already sandboxed by the suite; this closes the third path.
    """
    global _TEST_ROOT
    _TEST_ROOT = Path(root) if root is not None else None


def _plugin_root() -> Path:
    if _TEST_ROOT is not None:
        return _TEST_ROOT
    return Path(__file__).resolve().parent.parent


def _sleep_runner():
    """Import `sleep_runner` through the namespace ladder, or None.

    v1.8.48 (audit G11). This module imported it THREE different ways: the
    bare `helpers.` name, a bare namespace-only `usr.plugins.skillopt.helpers`
    import, and a proper two-path ladder. The namespace-only form has no
    fallback, so `restore_default_snapshot` - i.e. the `/rollback` route -
    raised `No module named 'usr'` outside the A0 runtime and returned
    `{ok: False}` from its catch-all. That is the F11 shape the contract
    already forbids for the config readers; here it silently disabled a
    recovery path. One helper, both paths, used everywhere.
    """
    try:
        from usr.plugins.skillopt.helpers import sleep_runner  # type: ignore

        return sleep_runner
    except Exception:
        pass
    try:
        from helpers import sleep_runner  # type: ignore

        return sleep_runner
    except Exception:
        return None


def _config_int(key: str, default: int | None = None) -> int | None:
    """Read an int config key, tolerating a missing or unreadable config.

    Never raises: a snapshot helper must not fail because the config is odd.

    v1.8.48 (audit G1): reads `merged_config()`, NOT `default_config()`. The
    shipped YAML is only half of the truth - the operator's settings live in
    the framework registry and are overlaid by `merged_config()`. Reading the
    YAML alone pinned `fragment_snapshot_dir` / `fragment_max_history_per_id`
    to their shipped values, so both were declared, documented and editable
    and changed nothing. This is the F13 class the contract names: a setting
    that is declared but unread is a switch an operator can flip that does
    nothing.
    """
    sr = _sleep_runner()
    if sr is None:
        return default
    try:
        raw = sr.merged_config().get(key, default)
    except Exception:
        try:
            raw = sr.default_config().get(key, default)
        except Exception:
            return default
    if raw is None or isinstance(raw, bool):
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _config_str(key: str, default: str) -> str:
    sr = _sleep_runner()
    if sr is None:
        return default
    try:
        raw = sr.merged_config().get(key, default)
    except Exception:
        try:
            raw = sr.default_config().get(key, default)
        except Exception:
            return default
    return raw if isinstance(raw, str) and raw.strip() else default


def _version_of(path: "Path | str") -> int:
    """Numeric version of a snapshot filename, or -1 when it has none.

    Accepts a `Path` or the string form, because `list_fragment_history`
    sorts on the `path` FIELD it just built - which is a string.

    v1.8.48 (audit G2). Three readers sorted snapshots by NAME, which is the
    exact trap `_prune_history` documents 90 lines above: `_default.v9.md`
    sorts after `_default.v12.md`. So `_latest_version` reported `v9` for a
    skill at `v12`, `write_fragment` computed `next_n = 10` and OVERWROTE the
    live `intro.v10.md` snapshot, `restore_default_snapshot` restored `v9`,
    and `list_fragment_history` emitted `v1, v10, v11, v12, v2`. One helper,
    used everywhere, makes the module self-consistent again.
    """
    name = path.name if hasattr(path, "name") else str(path)
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    m = re.search(r"\.v?(\d+)\.md$", name)
    return int(m.group(1)) if m else -1


def _prune_history(directory: Path, keep: int | None) -> int:
    """Drop the oldest versioned snapshots, keeping the newest `keep`.

    v1.8.25. `fragment_max_history_per_id` was declared since v1.6.0 and read
    by nothing, so a long-lived install accumulated one file per adopt per
    skill forever. That is an unbounded-growth risk on the plugin's own disk,
    and it is why `fragments/` needed manual cleanup.

    Only versioned snapshots are pruned. `_default.pre_adopt.md` (the current
    rollback target) and `*.current.md` (the live version) are never touched -
    pruning either would destroy the ability to roll back.
    """
    if not keep or keep <= 0 or not directory.is_dir():
        return 0

    versioned: list[tuple[int, str, Path]] = []
    for path in directory.glob("*.md"):
        name = path.name
        if name.endswith(".current.md") or name == "_default.pre_adopt.md":
            continue
        # Numeric version, NOT the name. Sorting on the name is a real trap:
        # lexicographically `_default.v9.md` sorts AFTER `_default.v12.md`, so
        # a name-ordered prune keeps the OLD versions and drops the new ones -
        # silently discarding the recent history an operator would roll back to.
        m = re.match(r"^(?:_default|[\w.-]+?)\.v(\d+)\.md$", name)
        if m:
            versioned.append((int(m.group(1)), name, path))
            continue
        m = re.match(r"^(?:_default|[\w.-]+?)\.(\d+)\.md$", name)
        if m:
            versioned.append((int(m.group(1)), name, path))
    if len(versioned) <= keep:
        return 0
    # Highest version first, so the tail is the oldest.
    versioned.sort(key=lambda item: (item[0], item[1]), reverse=True)
    dropped = 0
    for _num, _name, path in versioned[keep:]:
        try:
            path.unlink()
            dropped += 1
        except OSError:
            pass
    if dropped:
        try:
            _append_fragment_log(
                f"action=prune dir={directory.name!r} dropped={dropped} keep={keep}"
            )
        except Exception:
            pass
    return dropped


def _snapshots_root() -> Path:
    p = _plugin_root() / _config_str("fragment_snapshot_dir", "fragments")
    p.mkdir(parents=True, exist_ok=True)
    return p


def _snapshots_dir_for_skill(skill_path: str | os.PathLike) -> Path:
    """Snapshot dir for one skill, keyed on its NAME.

    v1.8.50 (audits F6/F7): two defects lived here and both are visible in the
    one line:

    1. It used `Path(skill_path).stem`, and **every** `SKILL.md` has the stem
       `SKILL` — so all per-fragment snapshots shared `fragments/SKILL/`.
       `write_fragment` on skill A and skill B computed `next_n` from each
       other's history and overwrote the same `<id>.v<N>.md`: a lost rollback
       target, and `POST /fragments/rollback` able to restore one skill's
       bytes into another.
    2. It read the config key nowhere and hardcoded `"fragments"`, while
       `_snapshots_root()` honours `fragment_snapshot_dir` and
       `get_fragments_status()` reports *that* root — so changing the key
       pointed the dashboard at a directory holding no rollback target.

    Key on the skill name (as `_snapshots_dir_for_skill_name` already does) and
    honour the same config key.
    """
    p = _plugin_root() / _config_str("fragment_snapshot_dir", "fragments") \
        / skill_path_to_name(skill_path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _snapshots_dir_for_skill_name(skill_name: str) -> Path:
    """Snapshot dir keyed on the skill-name STRING explicitly (v1.7.0).

    The older `_snapshots_dir_for_skill` uses `Path(skill_path).stem`, but
    every SKILL.md has stem ``SKILL`` — so all whole-file skills' snapshots
    collided under ``fragments/SKILL/`` (side-finding #4). The whole-file
    snapshot/restore helpers below key on the skill NAME so a caveman
    snapshot never overwrites a code-review snapshot. The name is
    sanitised to a filesystem-safe segment.
    """
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", str(skill_name or "").strip()) or "_unnamed"
    p = _plugin_root() / _config_str("fragment_snapshot_dir", "fragments") / safe
    p.mkdir(parents=True, exist_ok=True)
    return p


def _fragment_log_path() -> Path:
    p = _plugin_root() / "logs" / "runs" / "fragments.log"
    p.parent.mkdir(parents=True, exist_ok=True)
    # v1.8.39 (audit M12): this log grew without bound and is fully re-read by
    # get_fragments_status() on every status poll, so cost grew with install
    # age. Rotate on access, like governance.log and adoptions.log.
    try:
        from usr.plugins.skillopt.helpers import sleep_runner as _sr  # type: ignore
    except Exception:
        try:
            from helpers import sleep_runner as _sr  # type: ignore
        except Exception:
            _sr = None  # type: ignore[assignment]
    if _sr is not None:
        try:
            _sr.rotate_log_if_large(p)
        except Exception:
            pass
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
        key=_version_of,
    )


def _latest_version(skill_path: str | os.PathLike, fragment_id: str) -> str:
    snaps = _list_snapshots(skill_path, fragment_id)
    if not snaps:
        return "v0"
    return snaps[-1].name.split(".")[1]  # "intro.v3.md" -> "v3"
    # v1.8.48 (audit G2): ordering is numeric via `_version_of`, so the LAST
    # entry is the highest version. Name ordering put `v9` after `v12`.


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


def _raw_frontmatter_block(raw: str) -> str | None:
    """The verbatim `---\\n...\\n---\\n` prefix of a document, or None.

    v1.8.50 (audit F8). Re-rendering the parsed frontmatter through
    `yaml.safe_dump` normalises quoting and indentation, so an edit meant to
    touch one section also reformatted the operator's metadata - and a
    rollback could no longer restore the file byte-for-byte, because the
    difference was introduced by the restore itself. Only the body is ours.
    """
    if not raw.startswith("---"):
        return None
    # The closing delimiter is the first `---` line after the opening one.
    lines = raw.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return "".join(lines[: i + 1])
    return None


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


def skill_path_to_name(skill_path: str | os.PathLike) -> str:
    """The skill NAME for a live `SKILL.md` path (`<skills>/<name>/SKILL.md`).

    v1.8.50 (audit F8). `adopt_write_skill` takes a name, not a path; handing
    it `SKILL.md` would have written the whole-file snapshots and the live
    document into `usr/skills/SKILL.md` — a different, nonexistent skill.
    """
    return Path(skill_path).parent.name or "SKILL"


def write_fragment(
    skill_path: str | os.PathLike,
    fragment_id: str,
    new_text: str,
    version_label: str | None = None,
) -> dict[str, Any]:
    """Update a fragment in-place and write a snapshot.

    **The live document is rewritten (v1.8.50).** The body is spliced at the
    fragment's selector span, the frontmatter is re-rendered, and the write
    goes through `sleep_runner.adopt_write_skill` — the one sanctioned writer,
    which snapshots the whole file first, so the edit itself is reversible.

    Before v1.8.50 this function stored the new text in a
    `<id>.current.md` file that **no reader consulted**, left the body
    untouched, and still returned `ok: True` — so `read_fragments` returned
    the OLD text, a fragment edit was a silent no-op, and `rollback_fragment`
    (its only caller) reported a successful recovery that changed nothing.

    Refuses, rather than degrading, when: the selector matches nothing in the
    live body; the new text equals what is already there; or yaml is
    unavailable and the frontmatter could not be re-rendered without loss.

    Returns {ok, version, bytes_written, snapshot_path, applied} or
    {ok=False, error, ...} on failure. Never raises on bad input.
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
    # v1.8.50 (audit F8): APPLY the edit. The docstring promised "the body
    # rebuilt by re-applying each fragment's selector"; the implementation
    # wrote the new text to a `<id>.current.md` file that NOTHING reads, left
    # the body untouched, and still returned `ok: True`. `read_fragments`
    # recomputes text from the body, so every reader saw the OLD text: a
    # fragment edit, and `rollback_fragment`, both reported success while
    # changing no document. Splice the selector's span and go through the one
    # sanctioned live writer.
    sel = str(declared[target_idx].get("selector") or "")
    span_start, span_end = _selector_span(body, sel)
    if span_start < 0:
        return {
            "ok": False,
            "error": f"selector for fragment {fragment_id!r} matched nothing "
                     f"in the live body, so the edit cannot be applied",
            "selector": sel,
        }
    new_body = body[:span_start] + (new_text or "") + body[span_end:]
    if new_body == body:
        return {
            "ok": False,
            "error": f"fragment {fragment_id!r} is already identical to the "
                     f"requested text; nothing to apply",
            "version": new_version,
        }
    if not _HAS_YAML or not isinstance(fm, dict):
        # Hand-rendering the frontmatter would drop the keys the fallback
        # cannot reproduce, so refuse rather than rewrite a lossy document.
        return {
            "ok": False,
            "error": "yaml is unavailable, so the frontmatter cannot be "
                     "re-rendered; refusing to rewrite the skill",
        }
    # v1.8.50: re-emit the ORIGINAL frontmatter text verbatim rather than
    # re-rendering the parsed mapping. `yaml.safe_dump` normalises quoting and
    # indentation, so a re-render turned `- id: "intro"` into `- id: intro` and
    # made a fragment edit - or a rollback - a formatting change to a document
    # whose body it did not have to touch. Only the body is ours to rewrite.
    fm_text = _raw_frontmatter_block(raw)
    if fm_text is None:
        return {"ok": False,
                "error": "could not locate the raw frontmatter block; refusing "
                         "to rewrite the skill"}
    new_raw = fm_text + new_body
    sr = _sleep_runner()
    if sr is None:
        return {"ok": False, "error": "sleep_runner unavailable; refusing to "
                                      "rewrite the live skill"}
    # Only a skill INSIDE the configured skills tree may be rewritten, and only
    # through `adopt_write_skill` - the one writer that snapshots and replaces
    # atomically. An arbitrary path (a fixture, a skill the plugin does not own)
    # would otherwise be written by a second, unsanctioned writer; refuse
    # instead, naming what was passed.
    try:
        skills_root = Path(sr.a0_skills_dir()).resolve()
        target_resolved = p.resolve()
    except Exception:
        skills_root, target_resolved = None, p
    if skills_root is not None:
        try:
            inside = target_resolved.is_relative_to(skills_root)
        except AttributeError:  # pragma: no cover - Python < 3.9
            inside = str(target_resolved).startswith(str(skills_root))
    if not inside:
        return {
            "ok": False,
            "error": f"{p} is not inside the configured skills dir "
                     f"({skills_root}); refusing to write a document this "
                     f"plugin does not own",
        }
    # The snapshot above is the fragment's; the writer takes the whole-file
    # one, so the document itself stays reversible either way.
    w = sr.adopt_write_skill(skill_path_to_name(p), new_raw)
    if not w.get("ok"):
        return {"ok": False, "error": f"live write refused: {w.get('error')}",
                "version": new_version, "snapshot_path": str(snap_path)}
    _append_fragment_log(
        f"action=write skill={p.stem!r} fragment={fragment_id!r} "
        f"version={new_version} bytes={len(new_text)} snapshot={snap_path.name}"
    )
    return {
        "ok": True, "version": new_version, "bytes_written": len(new_text),
        "snapshot_path": str(snap_path),
        "previous_bytes": len(prev_text),
        "applied": True, "redacted": bool(w.get("redacted")),
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
        # v1.8.50: "already identical" is the DESIRED state for a rollback,
        # not a failure - the operator asked for exactly this text. `write_
        # fragment` refuses a no-op edit because an edit that changes nothing
        # is suspicious; here it is the whole point.
        if 'already identical' in str(result.get("error") or ""):
            _append_fragment_log(
                f"action=rollback skill={p.stem!r} fragment={fragment_id!r} "
                f"no-op (already at the target text)"
            )
            return {"ok": True, "restored_from": str(target_snap),
                    "current_version": result.get("version"),
                    "changed": False}
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


# ----------------------------------------------------------------------- #
# v1.7.0 (Solution C, Phase C3): whole-file _default snapshot/restore
# ----------------------------------------------------------------------- #
# The v1.2.0 `write_fragment` `_default` branch overwrites the SKILL.md
# with NO snapshot — there is no pre-edit history for whole-file skills,
# so an adopt could not be rolled back to the byte-for-byte prior file.
# These two helpers give the adopt UI a reversible whole-file snapshot.
# They key on the skill NAME (not Path.stem) so skills don't collide.


def snapshot_default(skill_name: str, current_bytes: str) -> dict[str, Any]:
    """Snapshot the whole-file SKILL.md bytes BEFORE an adopt overwrites
    it, so the adopt is reversible via `restore_default_snapshot`.

    Writes ``<plugin>/fragments/<skill>/_default.pre_adopt.md``. If one
    already exists (a previous adopt's snapshot that was never restored
    past), the existing file is versioned aside to ``_default.vN.md`` so
    the chain of pre-adopt states is preserved. Keys on the skill-name
    string (see `_snapshots_dir_for_skill_name`). Never raises — a
    snapshot failure returns ``{ok: False, error}`` and the caller decides
    whether to proceed with the adopt anyway.
    """
    try:
        d = _snapshots_dir_for_skill_name(skill_name)
        primary = d / "_default.pre_adopt.md"
        if primary.is_file():
            n = 1
            for p in d.glob("_default.v*.md"):
                m = re.match(r"_default\.v(\d+)\.md$", p.name)
                if m:
                    n = max(n, int(m.group(1)) + 1)
            primary.rename(d / f"_default.v{n}.md")
        primary.write_text(current_bytes, encoding="utf-8")
        pruned = _prune_history(d, _config_int("fragment_max_history_per_id", 0))
        _append_fragment_log(
            f"action=snapshot_default skill={skill_name!r} bytes={len(current_bytes)}"
            + (f" pruned={pruned}" if pruned else "")
        )
        return {"ok": True, "snapshot_path": str(primary),
                "bytes": len(current_bytes), "pruned": pruned}
    except Exception as e:
        log.debug("[skillopt] snapshot_default failed: %s", e)
        return {"ok": False, "error": str(e)}


def restore_default_snapshot(skill_name: str) -> dict[str, Any]:
    """Restore the most recent whole-file ``_default`` snapshot to
    ``<a0>/usr/skills/<skill_name>/SKILL.md``.

    The CURRENT (pre-rollback) bytes are snapshotted FIRST (by
    `adopt_write_skill`) so the rollback is itself reversible — rolling
    back does not destroy the state you rolled back from. Returns
    ``{ok, restored_from, bytes}`` or ``{ok: False, error}`` when there is
    no snapshot to restore, **or when that pre-rollback snapshot failed**
    (v1.8.48 / audit T2: `snapshot_default` never raises - it returns
    ``{ok: False}`` - so the result must be checked or the rollback
    destroys the state it is rolling back FROM with no recovery target).
    The live write is delegated to `sleep_runner.adopt_write_skill`, so a
    rollback uses the same snapshot + temp + `os.replace` choke point as
    every other path to a live `SKILL.md`; a bare `Path.write_text` here
    was the v1.8.24 P0 defect reappearing on a second path.
    Never raises.
    """
    try:
        sleep_runner = _sleep_runner()
        if sleep_runner is None:
            return {"ok": False,
                    "error": "sleep_runner unavailable; refusing to restore"}
        d = _snapshots_dir_for_skill_name(skill_name)
        snap = d / "_default.pre_adopt.md"
        # v1.8.49 (audit X1): a 0-byte pre_adopt is not a usable rollback
        # target - it is what `snapshot_default` writes for a skill the plugin
        # created from nothing (`current = ""`). Since a 0-byte file IS a file,
        # it shadowed every real `_default.vN.md` and one rollback emptied the
        # live skill. Treat it as absent and fall through to the newest
        # versioned snapshot. Belt-and-braces: `adopt_write_skill` now refuses
        # an empty document outright.
        if snap.is_file() and snap.stat().st_size == 0:
            snap = None  # type: ignore[assignment]
        if snap is None or not snap.is_file():
            vers = sorted(d.glob("_default.v*.md"), key=_version_of)
            vers = [v for v in vers if v.stat().st_size > 0]
            if not vers:
                return {"ok": False,
                        "error": f"no non-empty _default snapshot for skill "
                                 f"{skill_name!r}"}
            snap = vers[-1]
        restored = snap.read_text(encoding="utf-8")
        target = sleep_runner.safe_skill_md(skill_name)
        # v1.8.48 / audit T2: the pre-rollback snapshot is `adopt_write_skill`'s
        # job - it snapshots the CURRENT bytes, refuses when that snapshot
        # fails, then replaces atomically. Doing it here as well would write
        # two identical pre_adopt generations per rollback, and doing it here
        # with `Path.write_text` was the v1.8.24 P0 defect on a second path.
        _w = sleep_runner.adopt_write_skill(skill_name, restored)
        if not _w.get("ok"):
            return {"ok": False,
                    "error": f"rollback write refused: {_w.get('error')}",
                    "restored_from": str(snap)}
        _append_fragment_log(
            f"action=restore_default skill={skill_name!r} "
            f"bytes={len(restored)} from={snap.name}"
        )
        return {"ok": True, "restored_from": str(snap),
                "bytes": len(restored), "redacted": bool(_w.get("redacted"))}
    except Exception as e:
        log.debug("[skillopt] restore_default_snapshot failed: %s", e)
        return {"ok": False, "error": str(e)}


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
    out.sort(key=lambda r: _version_of(r["path"]))
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
