"""
SkillOpt - audit_log API endpoint (v1.4.0-Dev, Day-5 item 7).

Routes (under /api/plugins/skillopt/):
  GET /audit_log?limit=50&skill=<name>&passed=<bool>
      Returns the most recent N adoptions.log entries (newest-first),
      filtered. Used by the per-cycle dashboard's Audit Log tab.

Backed by logs/runs/adoptions.log (the v1.3.0 audit trail written
by helpers/auto_loop.py). Read-only. Plugin-local.
"""

from datetime import datetime, timezone

from helpers.api import ApiHandler  # type: ignore


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _runs_dir():
    """Locate <plugin>/logs/runs/."""
    try:
        from usr.plugins.skillopt.helpers import sleep_runner  # type: ignore
        return sleep_runner.runs_dir()
    except Exception:
        from pathlib import Path
        here = Path(__file__).resolve()
        for ancestor in [here] + list(here.parents):
            candidate = ancestor / "logs" / "runs"
            if candidate.is_dir():
                return candidate
        return here.parent / "logs" / "runs"


def _parse_passed(v: str | None):
    """Map 'passed' / 'failed' / '' / None to True/False/None."""
    if v is None:
        return None
    s = str(v).strip().lower()
    if s in ("", "all"):
        return None
    if s in ("true", "passed", "1", "yes"):
        return True
    if s in ("false", "failed", "rejected", "0", "no"):
        return False
    return None


class AuditLog(ApiHandler):
    async def process(self, input_data, request):  # type: ignore[no-untyped-def]
        data = input_data or {}
        try:
            limit = int(data.get("limit") or 50)
        except (TypeError, ValueError):
            limit = 50
        skill = (data.get("skill") or "").strip() or None
        passed = _parse_passed(data.get("passed"))
        runs_dir = _runs_dir()
        audit = runs_dir / "adoptions.log"
        out: list[dict] = []
        file_size = 0
        try:
            if audit.is_file():
                file_size = audit.stat().st_size
                with open(audit, "r", encoding="utf-8") as f:
                    raw_lines = f.readlines()
                # Newest-first
                raw_lines.reverse()
                import json as _json
                for raw in raw_lines:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        entry = _json.loads(raw)
                    except _json.JSONDecodeError:
                        continue
                    if skill and entry.get("skill") != skill:
                        continue
                    if passed is not None and bool(entry.get("passed")) != passed:
                        continue
                    out.append(entry)
                    if len(out) >= limit:
                        break
        except Exception as e:
            return {"ok": False, "error": f"audit_log read failed: {e}",
                    "timestamp": _now()}
        return {
            "ok": True,
            "count": len(out),
            "limit": limit,
            "skill": skill,
            "passed": passed,
            "file_path": str(audit),
            "file_size_bytes": file_size,
            "entries": out,
            "timestamp": _now(),
        }


__all__ = ["AuditLog"]
