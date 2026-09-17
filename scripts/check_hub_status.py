#!/usr/bin/env python3
"""
SkillOpt hub-status watchdog - ROADMAP item 10 (PR merge monitoring +
post-hub indexing verification).

Queries the GitHub REST API for Plugin-Hub PR #512 (agent0ai/a0-plugins,
"feat: add skillopt plugin") and, once the PR is merged, verifies that the
generated plugin catalog indexes `skillopt`.

Catalog sources (probed 2026-09-17):
  - https://www.agent-zero.ai/p/plugins/ is an SPA shell (HTML, no plugin
    data inline; data loads client-side), so a missing literal there is NOT
    evidence of non-indexing. Probed for reachability + literal occurrence,
    recorded as supplemental evidence only.
  - The canonical generated catalog is the release asset
    https://github.com/agent0ai/a0-plugins/releases/download/generated-index/index.json
    ({"authors": {...}, "plugins": {name: entry, ...}, "version": 1}) -
    the same asset scripts/download_index_release.py in the hub repo
    consumes. Indexing verdict is based on this asset.

Statuses (stdout + logs/hub_status.json):
  OPEN_PENDING     - PR #512 is not merged yet (open or closed-unmerged)
  MERGED_INDEXED   - PR merged AND skillopt present in the generated index
  MERGED_UNINDEXED - PR merged BUT skillopt absent from the generated index
                     (index regeneration lag; re-run later)
Exceptional: {"status": "ERROR", "error": ...} when a query cannot be
completed (HTTP/network/JSON failure) - the watchdog never guesses.

No blocking network calls on any agent execution path: this is a
run-standalone / scheduled utility; nothing in hooks.py, plugin.py, the
API routes, tools, or extensions imports it.

Usage:
  python scripts/check_hub_status.py              # real run
  python scripts/check_hub_status.py --selftest   # offline status-mapping checks

Exit codes:
  0 - status determined (OPEN_PENDING / MERGED_INDEXED / MERGED_UNINDEXED)
  1 - --selftest failed (logic regression)
  2 - real-run query failed (see the JSON "error" field)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

PR_API_URL = "https://api.github.com/repos/agent0ai/a0-plugins/pulls/512"
CATALOG_PAGE_URL = "https://www.agent-zero.ai/p/plugins/"
INDEX_ASSET_URL = (
    "https://github.com/agent0ai/a0-plugins/releases/"
    "download/generated-index/index.json"
)
PLUGIN_ROOT = Path(__file__).resolve().parents[1]
LOG_PATH = PLUGIN_ROOT / "logs" / "hub_status.json"
HISTORY_KEEP = 50
HTTP_TIMEOUT = 30
USER_AGENT = "skillopt-hub-status-watchdog"


def _fetch_bytes(url: str, token: str | None = None,
                 accept: str = "application/json") -> bytes:
    req = urllib.request.Request(url, method="GET", headers={
        "User-Agent": USER_AGENT,
        "Accept": accept,
    })
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return resp.read()


def _fetch_json(url: str, token: str | None = None) -> Any:
    return json.loads(_fetch_bytes(url, token).decode("utf-8", errors="replace"))


def check_pr(token: str | None) -> dict[str, Any]:
    """PR #512 state from the REST API (never raises past here)."""
    p = _fetch_json(PR_API_URL, token)
    return {
        "state": p.get("state"),
        "merged": bool(p.get("merged")),
        "merged_at": p.get("merged_at"),
        "merged_by": (p.get("merged_by") or {}).get("login"),
        "merge_commit_sha": p.get("merge_commit_sha"),
        "review_decision": p.get("review_decision"),
        "head_sha": (p.get("head") or {}).get("sha"),
        "head_label": (p.get("head") or {}).get("label"),
        "mergeable_state": p.get("mergeable_state"),
    }


def check_catalog() -> dict[str, Any]:
    """Indexing evidence: canonical index.json asset + SPA page probe.

    Both probes are fail-soft (recorded as false on error) so one flaky
    endpoint never masks the other.
    """
    out: dict[str, Any] = {
        "index_asset_present": False,
        "skillopt_in_index": False,
        "index_plugin_count": None,
        "index_source": INDEX_ASSET_URL,
        "catalog_page_reachable": False,
        "skillopt_literal_on_page": False,
        "page_source": CATALOG_PAGE_URL,
    }
    try:
        data = _fetch_json(INDEX_ASSET_URL)
        plugins = data.get("plugins") if isinstance(data, dict) else None
        if isinstance(plugins, dict):
            out["index_asset_present"] = True
            out["index_plugin_count"] = len(plugins)
            out["skillopt_in_index"] = "skillopt" in plugins
    except Exception:
        pass
    try:
        html = _fetch_bytes(CATALOG_PAGE_URL, accept="text/html,*/*").decode(
            "utf-8", errors="replace")
        out["catalog_page_reachable"] = True
        out["skillopt_literal_on_page"] = "skillopt" in html
    except Exception:
        pass
    return out


def determine_status(pr: dict[str, Any] | None,
                     catalog: dict[str, Any] | None) -> str:
    """Pure status mapping - the part --selftest pins down."""
    if not pr or not pr.get("merged"):
        return "OPEN_PENDING"
    catalog = catalog or {}
    if catalog.get("skillopt_in_index"):
        return "MERGED_INDEXED"
    return "MERGED_UNINDEXED"


def build_status(pr: dict[str, Any] | None, catalog: dict[str, Any] | None,
                 error: str | None = None) -> dict[str, Any]:
    st: dict[str, Any] = {
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "status": "ERROR" if error else determine_status(pr, catalog),
        "pr": pr or {},
        "catalog": catalog or {},
    }
    if error:
        st["error"] = error
    return st


def write_log(st: dict[str, Any]) -> None:
    """logs/hub_status.json = {latest, history[-50:]}, atomic replace."""
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    doc: dict[str, Any] = {}
    if LOG_PATH.exists():
        try:
            old = json.loads(LOG_PATH.read_text(encoding="utf-8"))
            if isinstance(old, dict):
                doc = old
        except Exception:
            doc = {}
    history = doc.get("history") if isinstance(doc.get("history"), list) else []
    doc["latest"] = st
    doc["history"] = (history + [st])[-HISTORY_KEEP:]
    tmp = LOG_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, LOG_PATH)


def _selftest() -> int:
    cases = [
        ("open PR -> OPEN_PENDING",
         {"merged": False, "state": "open"}, {}, "OPEN_PENDING"),
        ("closed-unmerged -> OPEN_PENDING",
         {"merged": False, "state": "closed"}, {}, "OPEN_PENDING"),
        ("merged + indexed -> MERGED_INDEXED",
         {"merged": True, "state": "closed"},
         {"skillopt_in_index": True, "index_plugin_count": 186},
         "MERGED_INDEXED"),
        ("merged + unindexed -> MERGED_UNINDEXED",
         {"merged": True, "state": "closed"},
         {"skillopt_in_index": False, "index_plugin_count": 185},
         "MERGED_UNINDEXED"),
    ]
    ok = True
    for name, pr, catalog, expected in cases:
        got = determine_status(pr, catalog)
        good = got == expected
        ok = ok and good
        print(f"{'PASS' if good else 'FAIL'} {name}: {got}")
    st = build_status(None, None, error="simulated")
    good = st["status"] == "ERROR" and "error" in st
    ok = ok and good
    print(f"{'PASS' if good else 'FAIL'} ERROR path carries error field")
    print("selftest " + ("OK" if ok else "FAILED"))
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--selftest", action="store_true",
                    help="offline status-mapping checks, no network")
    args = ap.parse_args()
    if args.selftest:
        return _selftest()
    token = os.environ.get("GITHUB_TOKEN") or None
    try:
        pr = check_pr(token)
    except Exception as e:
        st = build_status(None, None, error=f"PR query failed: {e}")
        write_log(st)
        print(json.dumps(st, indent=2, ensure_ascii=False))
        return 2
    catalog = check_catalog()
    st = build_status(pr, catalog)
    write_log(st)
    print(json.dumps(st, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
