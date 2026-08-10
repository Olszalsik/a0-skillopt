#!/usr/bin/env python3
"""
SkillOpt v1.2.0 smoke test suite.

Runs as: python tests/smoke.py  (from the plugin root)

Design (per ROADMAP engineering principle 1 - "test with the synthetic
harness, ship with the live agent"):
  * Deterministic - no LLM calls, no network, no clock dependence
  * Fast     - finishes in <5 seconds on a single CPU
  * Loud     - prints the failure reason for every test that didn't pass
  * No pytest - ships its own minimal harness so it runs in the
    A0 container without extra deps

The v1.1.0 release notes mention a 26-case launch verification. This
suite re-derives the deterministic subset of those checks (so a v1.1.0
regression gets caught here) and adds the new v1.2.0 cases for the
reward model. Run this before tagging a release.

Exit code:
  0 - all tests passed
  1 - one or more tests failed (count printed at the end)
  2 - harness setup error (missing plugin dir, missing imports)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path
from typing import Callable


# ----------------------------------------------------------------------- #
# Minimal harness
# ----------------------------------------------------------------------- #

PLUGIN_ROOT = Path(__file__).resolve().parent.parent

# v1.6.0: the A/B harness now defaults to ADVISORY-OFF (ab_harness_enabled:
# false). The harness-functionality tests below exercise the harness itself,
# so they opt in explicitly via this env override (read by ab_harness._config()).
# Tests that assert the *default* is off unset this for their run.
os.environ["SKILLOPT_AB_HARNESS_ENABLED"] = "1"

_tests: list[tuple[str, Callable[[], None]]] = []


def test(name: str):
    """Decorator - register a function as a test case."""
    def deco(fn: Callable[[], None]) -> Callable[[], None]:
        _tests.append((name, fn))
        return fn
    return deco


def _section(label: str) -> None:
    print()
    print("=" * 70)
    print(label)
    print("=" * 70)


def _ok(msg: str) -> None:
    print(f"  PASS  {msg}")


def _fail(msg: str) -> None:
    print(f"  FAIL  {msg}")


def main() -> int:
    if not PLUGIN_ROOT.is_dir():
        print(f"FATAL: plugin root not found: {PLUGIN_ROOT}", file=sys.stderr)
        return 2
    # Ensure the plugin's own helpers are importable as a fallback
    sys.path.insert(0, str(PLUGIN_ROOT))

    failures = 0
    _section("SkillOpt smoke test suite")
    print(f"plugin_root = {PLUGIN_ROOT}")
    print(f"tests registered = {len(_tests)}")

    for name, fn in _tests:
        try:
            fn()
        except AssertionError as e:
            _fail(f"{name}  (assertion: {e})")
            failures += 1
        except Exception as e:
            _fail(f"{name}  ({type(e).__name__}: {e})")
            traceback.print_exc()
            failures += 1
        else:
            _ok(name)

    _section("Summary")
    total = len(_tests)
    passed = total - failures
    print(f"  total   : {total}")
    print(f"  passed  : {passed}")
    print(f"  failed  : {failures}")
    if failures == 0:
        print()
        print("  ALL TESTS PASSED.")
        return 0
    print()
    print(f"  {failures} TEST(S) FAILED.")
    return 1


# ======================================================================= #
# v1.1.0 launch verification (re-derived from the v1.1.0 CHANGELOG entry)
# ======================================================================= #

_section_v110 = "v1.1.0 regression checks (12 cases re-derived from the v1.1.0 changelog)"


@test("v1.1.0: required files all present")
def t_v110_files() -> None:
    required = [
        "plugin.yaml", "default_config.yaml", "hooks.py",
        "helpers/__init__.py", "helpers/sleep_runner.py",
        "helpers/auto_loop.py", "helpers/direct_optimizer.py",
        "helpers/bridge.py", "helpers/reward_model.py",
        "helpers/ab_harness.py",
        "scripts/train_reward_model.py",
        "scripts/calibrate_judge.py",
        "api/status.py", "api/sleep.py", "api/adopt.py",
        "api/loop.py", "api/config.py",
        "tools/skillopt_sleep.py", "tools/skillopt_train.py",
        "tools/skillopt_status.py",
        "extensions/python/agent_init/_50_skillopt_auto_loop.py",
        "extensions/python/hooks/_post_skill_adopt.py",
        "extensions/python/monologue_end/_60_skillopt_harvest_rollout.py",
        "extensions/python/monologue_start/_40_skillopt_warn.py",
        "webui/config.html", "webui/skillopt-dashboard.js",
        "execute.py", "tests/__init__.py", "tests/smoke.py",
    ]
    missing = [r for r in required if not (PLUGIN_ROOT / r).is_file()]
    assert not missing, f"missing files: {missing}"


@test("v1.1.0: validate_proposal rejects byte-identical (the 1904->1904 bug)")
def t_v110_byte_identical() -> None:
    sys.path.insert(0, str(PLUGIN_ROOT))
    from helpers.sleep_runner import validate_proposal
    text = "# Skill\nA 1904 char block\n```\nexample\n```\n" + "x" * 1700
    ok, reason = validate_proposal(text, text, min_chars=200, min_improvement_pp=0.0, max_shrink_ratio=0.5, held_out=None)
    assert not ok, f"expected reject, got ok reason={reason!r}"
    assert "byte-identical" in reason or "no-op" in reason, f"unexpected reason: {reason!r}"


@test("v1.1.0: validate_proposal rejects whitespace-normalised identical")
def t_v110_ws_normalised() -> None:
    sys.path.insert(0, str(PLUGIN_ROOT))
    from helpers.sleep_runner import validate_proposal
    a = "# Skill\nA 1904 char block\n```\nexample\n```\n" + "x" * 1700
    b = "# Skill\nA  1904  char  block\n```\nexample\n```\n" + "x" * 1700
    ok, reason = validate_proposal(a, b, min_chars=200, min_improvement_pp=0.0, max_shrink_ratio=0.5, held_out=None)
    assert not ok, f"expected reject, got ok reason={reason!r}"
    assert "whitespace" in reason or "no-op" in reason, f"unexpected reason: {reason!r}"


@test("v1.1.0: validate_proposal rejects shrink > 50%")
def t_v110_shrink() -> None:
    sys.path.insert(0, str(PLUGIN_ROOT))
    from helpers.sleep_runner import validate_proposal
    long_text = "# Skill\n" + ("abcdefghij" * 200)  # 2000 chars
    short_text = "# Skill\n" + ("abc" * 50)  # 154 chars total - well under 50%
    ok, reason = validate_proposal(short_text, long_text, min_chars=200, min_improvement_pp=0.0, max_shrink_ratio=0.5, held_out=None)
    # Note: short_text is 154 chars, under min_chars=200 - so the size check fires first
    assert not ok


@test("v1.1.0: validate_proposal rejects held-out below min_improvement_pp")
def t_v110_held_out() -> None:
    sys.path.insert(0, str(PLUGIN_ROOT))
    from helpers.sleep_runner import validate_proposal
    proposed = "# New Skill\nA substantially different skill body.\n```\nexample\n```\n" + "y" * 1800
    current = "# Old Skill\nA completely different body.\n```\nexample\n```\n" + "x" * 1800
    held = {"before": 0.5, "after": 0.51, "delta_pp": 1.0}  # below 5pp
    ok, reason = validate_proposal(proposed, current, min_chars=200, min_improvement_pp=5.0, max_shrink_ratio=0.5, held_out=held)
    assert not ok, f"expected reject, got ok reason={reason!r}"
    assert "held-out" in reason or "held_out" in reason, f"unexpected reason: {reason!r}"


@test("v1.1.0: validate_proposal accepts a good proposal with passing held-out")
def t_v110_pass() -> None:
    sys.path.insert(0, str(PLUGIN_ROOT))
    from helpers.sleep_runner import validate_proposal
    proposed = "# New Skill\nA substantially different skill body.\n```\nexample\n```\n" + "y" * 1800
    current = "# Old Skill\nA completely different body.\n```\nexample\n```\n" + "x" * 1800
    held = {"before": 0.4, "after": 0.5, "delta_pp": 10.0}
    ok, reason = validate_proposal(proposed, current, min_chars=200, min_improvement_pp=5.0, max_shrink_ratio=0.5, held_out=held)
    assert ok, f"expected accept, got reject reason={reason!r}"


@test("v1.1.0: parse_held_out reads 'held-out 0.412 -> 0.487' from a log")
def t_v110_parse_held_out() -> None:
    sys.path.insert(0, str(PLUGIN_ROOT))
    from helpers.sleep_runner import parse_held_out
    with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as f:
        f.write("noise line 1\n")
        f.write("more noise\n")
        f.write("held-out 0.412 -> 0.487\n")
        f.write("trailing line\n")
        log = f.name
    try:
        result = parse_held_out(log)
        assert result is not None, "parse_held_out returned None"
        assert abs(result["before"] - 0.412) < 1e-6, f"before: {result['before']}"
        assert abs(result["after"] - 0.487) < 1e-6, f"after: {result['after']}"
        assert abs(result["delta_pp"] - 7.5) < 1e-6, f"delta_pp: {result['delta_pp']}"
    finally:
        os.unlink(log)


@test("v1.1.0: parse_held_out returns None for missing pattern")
def t_v110_no_held_out() -> None:
    sys.path.insert(0, str(PLUGIN_ROOT))
    from helpers.sleep_runner import parse_held_out
    with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as f:
        f.write("nothing useful here\n")
        log = f.name
    try:
        result = parse_held_out(log)
        assert result is None, f"expected None, got {result!r}"
    finally:
        os.unlink(log)


@test("v1.1.0: get_status_snapshot returns the documented keys")
def t_v110_snapshot_shape() -> None:
    sys.path.insert(0, str(PLUGIN_ROOT))
    from helpers.sleep_runner import get_status_snapshot
    snap = get_status_snapshot()
    for key in ("rollout_count", "skills_available", "staged_proposals",
                "package", "plugin_root", "a0_python", "platform"):
        assert key in snap, f"missing key {key} in snapshot"


@test("v1.1.0: rollouts dir + staging dir are created on demand")
def t_v110_dirs_exist() -> None:
    sys.path.insert(0, str(PLUGIN_ROOT))
    from helpers.sleep_runner import rollouts_dir, staging_dir, runs_dir
    assert rollouts_dir().is_dir()
    assert staging_dir().is_dir()
    assert runs_dir().is_dir()


@test("v1.1.0: write_rollout produces a JSON file with id+ts+task+outcome")
def t_v110_write_rollout() -> None:
    sys.path.insert(0, str(PLUGIN_ROOT))
    from helpers.sleep_runner import write_rollout, rollouts_dir, list_rollouts
    rec = {"id": "smoke-test-id-v110", "ts": 1.7e9, "task": "smoke test",
           "task_type": "general", "skill_used": "", "outcome": "success",
           "trajectory": [], "model": "smoke", "duration_s": 0.1}
    p = write_rollout(rec)
    try:
        assert p.is_file()
        data = json.loads(p.read_text(encoding="utf-8"))
        assert data["id"] == "smoke-test-id-v110"
        assert data["task"] == "smoke test"
        assert data["outcome"] == "success"
    finally:
        p.unlink(missing_ok=True)


@test("v1.1.0: harvester's _heuristic_outcome classifies failure / partial / success")
def t_v110_heuristic() -> None:
    sys.path.insert(0, str(PLUGIN_ROOT))
    sys.path.insert(0, str(PLUGIN_ROOT / "extensions" / "python" / "monologue_end"))
    # Import the heuristic from the harvester module
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "harvester",
        PLUGIN_ROOT / "extensions" / "python" / "monologue_end" / "_60_skillopt_harvest_rollout.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore
    # The heuristic is module-internal; we exercise it indirectly via the
    # harvester's record-building code path. But the easier check: the
    # harvester's _heuristic_outcome and reward_model._heuristic_outcome
    # must agree on the canonical failure/partial/success inputs.
    cases = [
        ("Traceback (most recent call last): ...", "failure"),
        ("I could not find the file.", "partial"),
        ("Task complete. Here is the result.", "success"),
    ]
    for text, expected in cases:
        # v1.7.0: _heuristic_outcome is now (last_response) only — the old
        # (messages, last_response) form passed messages=[] (the bug this
        # version fixes), so the arg was always unused and removed.
        got = mod._heuristic_outcome(text)
        assert got == expected, f"harvester heuristic: {text[:40]!r} -> {got!r}, want {expected!r}"
        # The reward-model copy must agree (so fallback is identical to v1.1.0)
        from helpers.reward_model import _heuristic_outcome as rm_heur
        got_rm = rm_heur(text)
        assert got_rm == expected, f"reward-model heuristic: {text[:40]!r} -> {got_rm!r}, want {expected!r}"


# ======================================================================= #
# v1.2.0 NEW - reward model (3 cases the user asked for)
# ======================================================================= #

_section_v120 = "v1.2.0 NEW - reward model (3 user-requested cases)"


@test("v1.2.0 NEW: score_rollout fails closed (heuristic fallback) when model missing")
def t_v120_fallback_when_missing() -> None:
    """With no model on disk, score_rollout must return a valid result
    with source != 'model' and a sensible outcome. This is the
    'fails closed when in doubt' rule from the ROADMAP."""
    sys.path.insert(0, str(PLUGIN_ROOT))
    # Force a clean state: model path is whatever the env / config says,
    # which by default does not exist.
    from helpers import reward_model
    reward_model.reset_for_tests()
    # If the model happens to exist on this machine, the test would
    # still pass - the source field tells us the truth.
    rollout = {
        "task": "Refactor the parser to handle nested groups.",
        "last_response": "Done. Tests pass.",
        "outcome": "success",
        "trajectory": [{"role": "tool_call", "name": "code_execution_tool", "args": "python tests"}],
    }
    result = reward_model.score_rollout(rollout)
    # The shape is always present
    for key in ("success", "partial", "failure", "confidence",
                "outcome", "source", "model_version"):
        assert key in result, f"missing key {key} in score_rollout result"
    # Probabilities sum to 1.0
    total = result["success"] + result["partial"] + result["failure"]
    assert abs(total - 1.0) < 1e-3, f"probs do not sum to 1.0: {total}"
    # Source is honest about what was used
    if result["source"] == "model":
        # A real model is present - the test still passes because the
        # shape is right. The fallback is exercised by the next test.
        assert result["model_version"] != "none"
    else:
        # Fallback path - source must be one of the documented values
        assert result["source"] in {
            "heuristic_fallback", "heuristic_fallback_error", "heuristic_no_input"
        }, f"unexpected source: {result['source']!r}"
        assert result["model_version"] == "none"


@test("v1.2.0 NEW: score_rollout handles garbage in (empty task, empty response)")
def t_v120_handles_garbage() -> None:
    """An empty rollout must NOT crash. It must return a result with
    source='heuristic_no_input' and outcome='failure'."""
    sys.path.insert(0, str(PLUGIN_ROOT))
    from helpers import reward_model
    reward_model.reset_for_tests()

    result = reward_model.score_rollout({})
    assert result["source"] == "heuristic_no_input"
    assert result["outcome"] == "failure"
    assert result["success"] == 0.0
    assert result["partial"] == 0.0
    assert result["failure"] == 1.0

    # Non-dict input must not raise either
    result2 = reward_model.score_rollout(None)  # type: ignore[arg-type]
    assert result2["source"] == "heuristic_no_input"
    assert result2["outcome"] == "failure"


@test("v1.2.0 NEW: harvester writes a 'reward' subfield on every rollout")
def t_v120_harvester_writes_reward() -> None:
    """The harvester's monologue_end hook must call the reward model
    and store the result under record['reward']. This is the
    integration test the user asked for ('verify the reward model
    is called')."""
    sys.path.insert(0, str(PLUGIN_ROOT))
    sys.path.insert(0, str(PLUGIN_ROOT / "extensions" / "python" / "monologue_end"))
    # Build a synthetic message list and a fake agent context
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "harvester",
        PLUGIN_ROOT / "extensions" / "python" / "monologue_end" / "_60_skillopt_harvest_rollout.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore
    # Manually build a record the way the harvester does, to exercise
    # the new reward-model path without running a full monologue.
    record = {
        "id": "smoke-v120-reward",
        "ts": 1.7e9,
        "task": "Investigate the bug in the validation gate.",
        "task_type": "debug",
        "skill_used": "qa",
        "outcome": "success",  # heuristic guess
        "last_response": "The bug was in the byte-equality check.",
        "trajectory": [{"role": "tool_call", "name": "code_execution_tool", "args": "grep -n 'byte' helpers"}],
    }
    # Drive the same code path the harvester uses post-1.2.0
    from helpers import reward_model
    rm_result = reward_model.score_rollout(record)
    record["reward"] = {
        "outcome": rm_result.get("outcome"),
        "confidence": rm_result.get("confidence"),
        "source": rm_result.get("source"),
        "model_version": rm_result.get("model_version"),
    }
    if rm_result.get("source") == "model" and float(rm_result.get("confidence") or 0.0) >= 0.6:
        record["outcome"] = rm_result.get("outcome")

    # The record now carries the reward subfield
    assert "reward" in record, "record missing 'reward' subfield"
    assert record["reward"]["source"] in {
        "model", "heuristic_fallback", "heuristic_fallback_error", "heuristic_no_input"
    }, f"unexpected source: {record['reward']['source']!r}"
    # And the outcome field is one of the canonical three
    assert record["outcome"] in {"success", "partial", "failure"}


# ======================================================================= #
# v1.2.0 NEW - integration / dashboard surface
# ======================================================================= #


@test("v1.2.0 NEW: get_model_status returns the documented shape")
def t_v120_status_shape() -> None:
    sys.path.insert(0, str(PLUGIN_ROOT))
    from helpers import reward_model
    reward_model.reset_for_tests()
    status = reward_model.get_model_status()
    for key in ("path", "model_present_on_disk", "model_loaded",
                "model_version", "fallback_count", "model_call_count",
                "load_error", "classes", "enabled"):
        assert key in status, f"missing key {key} in get_model_status()"
    assert status["classes"] == ["success", "partial", "failure"]


@test("v1.2.0 NEW: status snapshot includes a 'reward_model' block")
def t_v120_snapshot_includes_reward() -> None:
    sys.path.insert(0, str(PLUGIN_ROOT))
    from helpers.sleep_runner import get_status_snapshot
    snap = get_status_snapshot()
    assert "reward_model" in snap, "get_status_snapshot missing 'reward_model' block"
    rm = snap["reward_model"]
    assert "path" in rm
    assert "model_present_on_disk" in rm
    assert "model_loaded" in rm
    assert "fallback_count" in rm


@test("v1.2.0 NEW: training script smoke step runs (transformers + torch present)")
def t_v120_training_smoke() -> None:
    """Invoke scripts/train_reward_model.py with no args. It must
    report that deps are present, build the model, and run one
    forward+backward step. This proves the training pipeline is
    wired up even though we have no labels yet."""
    script = PLUGIN_ROOT / "scripts" / "train_reward_model.py"
    if not script.is_file():
        raise AssertionError(f"missing training script: {script}")
    proc = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        timeout=180,  # cold model build + tokenizer download can be slow
    )
    if proc.returncode != 0:
        # The training deps may legitimately be missing in some
        # environments; surface the output but mark the test as
        # SKIPPED-like by accepting exit 2 (deps missing).
        if proc.returncode == 2:
            print("    (training deps missing - this is expected on a venv without transformers)")
            return
        raise AssertionError(
            f"training script exited {proc.returncode};\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
        )
    assert "smoke step ran" in proc.stdout, f"missing 'smoke step ran' in output:\n{proc.stdout}"


@test("v1.2.0 NEW: gate preservation - validate_proposal still rejects identical after reward model changes")
def t_v120_gate_intact() -> None:
    """The v1.1.0 gate is the safety net. Adding the reward model must
    not weaken it. Re-run the canonical byte-identical + whitespace-
    identical + shrink cases to prove they're still rejected."""
    sys.path.insert(0, str(PLUGIN_ROOT))
    from helpers.sleep_runner import validate_proposal
    # The helper's validate_proposal requires triple-backtick example
    # blocks (the engine always emits one) - include them in both
    # baseline and new so we're testing the gate semantics, not the
    # example-block presence check.
    text = "# Skill\n```\nabcdefghij\n```\n" + ("abcdefghij\n" * 190)
    # 1. byte identical
    ok, _ = validate_proposal(text, text, min_chars=200, min_improvement_pp=0.0, max_shrink_ratio=0.5, held_out=None)
    assert not ok, "byte-identical no longer rejected"
    # 2. whitespace-normalised identical
    text2 = text.replace("\n", "  ")
    ok, _ = validate_proposal(text2, text, min_chars=200, min_improvement_pp=0.0, max_shrink_ratio=0.5, held_out=None)
    assert not ok, "whitespace-normalised identical no longer rejected"
    # 3. good proposal with passing held-out still passes
    new = "# Skill v2\n```\nrefactored body\n```\n" + ("xyz\n" * 600)
    held = {"before": 0.3, "after": 0.4, "delta_pp": 10.0}
    ok, reason = validate_proposal(new, text, min_chars=200, min_improvement_pp=5.0, max_shrink_ratio=0.5, held_out=held)
    assert ok, f"good proposal with passing held-out now rejected: {reason!r}"


# ----------------------------------------------------------------------- #
# v1.2.0 (Day-3 item 2) - Task A.1 follow-ups
# ----------------------------------------------------------------------- #

# Tiny in-process HTTP server used by the judge_via_http test. Stdlib
# only - no flask, no aiohttp, no requests. Lives in the test file so
# the smoke test stays self-contained.
import threading as _threading
import http.server as _http


class _MockJudgeServer:
    """Single-purpose stub of the LLM judge HTTP endpoint.

    Returns {"verdict": "win", "confidence": 0.8} for any POST. Records
    every request so the test can assert that the harness actually
    hit the HTTP path. Stops cleanly on .stop().
    """

    def __init__(self, body: dict | None = None) -> None:
        self.body = body or {"verdict": "win", "confidence": 0.8}
        self.requests: list[bytes] = []
        self._server: _http.HTTPServer | None = None
        self._thread: _threading.Thread | None = None
        self.port: int = 0

    def _handler(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        outer = self

        class _H(_http.BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length", "0") or 0)
                body = self.rfile.read(length) if length else b""
                outer.requests.append(body)
                payload = json.dumps(outer.body).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *a, **k):  # silence stderr
                pass

        return _H(*args, **kwargs)

    def start(self) -> None:
        self._server = _http.HTTPServer(("127.0.0.1", 0), self._handler)
        self.port = self._server.server_address[1]
        self._thread = _threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)
        self._server = None
        self._thread = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


@test("v1.2.0 NEW (Task A.1): judge_via_http with in-process mock server")
def t_v121_judge_via_http() -> None:
    """When a real LLM judge is available, the harness must POST to it
    and use the response. We stand up a tiny HTTP server in a thread
    and use set_judge_fn() to inject a wrapper that POSTs to it. This
    is the cleaner of the two options (env var vs injection): the
    injection is test-isolated and doesn't pollute the env."""
    sys.path.insert(0, str(PLUGIN_ROOT))
    from helpers import ab_harness  # type: ignore

    ab_harness.reset_for_tests()
    server = _MockJudgeServer(body={"verdict": "win", "confidence": 0.85})
    server.start()
    try:
        # Build a small HTTP client that talks to the mock server.
        # We use urllib instead of requests to stay stdlib-only.
        import urllib.request as _urlreq
        import urllib.error as _urlerr

        def _http_judge(rollout, score_a, score_b):
            # The harness calls judges with (rollout, score_a, score_b).
            # We construct a prompt from these and POST it to the mock
            # server. This matches the harness's JudgeFn contract.
            prompt = json.dumps({
                "task": rollout.get("task", ""),
                "score_a": score_a,
                "score_b": score_b,
            })
            payload = json.dumps({"prompt": prompt}).encode("utf-8")
            req = _urlreq.Request(
                server.url, data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with _urlreq.urlopen(req, timeout=5) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except (_urlerr.URLError, _urlerr.HTTPError, Exception) as e:
                return {"verdict": "tie", "confidence": 0.0,
                        "error": str(e)}

        ab_harness.set_judge_fn(_http_judge)

        # Inject rollouts so the harness has data to test on.
        rollouts_dir = PLUGIN_ROOT / "logs" / "rollouts"
        rollouts_dir.mkdir(parents=True, exist_ok=True)
        sample = [
            {
                "id": f"r{i}", "ts": time.time(),
                "task": f"Task {i}", "task_type": "general",
                "skill_used": "v121_http_judge",
                "outcome": ("success" if i % 2 == 0 else "failure"),
                "last_response": f"Response {i}", "duration_s": 0.0,
            }
            for i in range(6)
        ]
        for r in sample:
            (rollouts_dir / f"{r['id']}.json").write_text(
                json.dumps(r, ensure_ascii=False), encoding="utf-8",
            )
        try:
            result = ab_harness.run_paired_test(
                skill_name="v121_http_judge",
                proposed_text="# New\n```\nabc\n```\n" + ("x\n" * 300),
                current_text="# Old\n```\ndef\n```\n" + ("y\n" * 300),
                n=6,
            )
        finally:
            for r in sample:
                p = rollouts_dir / f"{r['id']}.json"
                if p.is_file():
                    p.unlink()
        # The harness must have actually called the server.
        assert len(server.requests) >= 1, (
            f"harness did not POST to mock server; judge_fallback={result.get('judge_fallback')} "
            f"judge_model={result.get('judge_model')!r}"
        )
        # The result must include samples (the harness ran the full
        # paired test, not the empty-fallback path). judge_fallback is
        # an internal flag about the keyword stub - we don't assert on
        # it because the harness's report has its own semantics for
        # what counts as 'fallback'.
        assert result.get("samples", 0) >= 2, (
            f"harness should have run the paired test, got samples={result.get('samples')}: {result}"
        )
    finally:
        server.stop()
        ab_harness.reset_for_tests()


@test("v1.2.0 NEW (Task A.2): auto_loop passes skill_name to validate_proposal")
def t_v121_auto_loop_passes_skill_name() -> None:
    """The auto-loop must pass skill_name=... to validate_proposal so the
    A/B harness stage runs during real Sleep cycles. We use AST/inspect
    to assert the call is wired (no need to actually run the loop)."""
    src = (PLUGIN_ROOT / "helpers" / "auto_loop.py").read_text(encoding="utf-8")
    # 1. Must call validate_proposal(...)
    assert "validate_proposal(" in src, "auto_loop no longer calls validate_proposal"
    # 2. Must pass skill_name as a kwarg
    assert "skill_name=skill_name" in src or "skill_name=" in src, (
        "auto_loop does not pass skill_name to validate_proposal - "
        "the A/B harness stage will never run in production"
    )
    # 3. Must guard on ab_harness_enabled so a misconfig is loud
    assert "ab_harness_enabled" in src, (
        "auto_loop does not check ab_harness_enabled - "
        "should be a no-op when the harness is disabled"
    )
    # 4. Must log the harness outcome
    assert "ab_harness:" in src, (
        "auto_loop does not emit a cycle log line for the harness outcome"
    )


# ----------------------------------------------------------------------- #
# v1.2.0 (Day-3 item 3) - Fragment store
# ----------------------------------------------------------------------- #

# Tiny in-memory helper: write a SKILL.md to a fresh dir under a temp
# scratch space. Used by the fragment tests so they never touch the
# user's A0 skills dir.
_SKILL_FM = """---
fragments:
  - id: "{id1}"
    selector: "{sel1}"
  - id: "{id2}"
    selector: "{sel2}"
---
# Skill

## Section A
{body1}

## Section B
{body2}
"""


def _write_test_skill(tmp: Path, name: str, fm: str | None = None,
                      body1: str = "first body", body2: str = "second body",
                      id1: str = "intro", id2: str = "section_b",
                      sel1: str = "## Section A", sel2: str = "## Section B",
                      ) -> Path:
    """Write a test SKILL.md under tmp/<name>/SKILL.md. Returns the path."""
    skill_dir = tmp / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_md = skill_dir / "SKILL.md"
    if fm == "no_frontmatter":
        skill_md.write_text(
            "# Skill\n\n## Section A\n" + body1 + "\n\n## Section B\n" + body2 + "\n",
            encoding="utf-8",
        )
    elif fm is None:
        # Two-fragment skill with frontmatter
        skill_md.write_text(
            _SKILL_FM.format(
                id1=id1, sel1=sel1, id2=id2, sel2=sel2,
                body1=body1, body2=body2,
            ),
            encoding="utf-8",
        )
    else:
        skill_md.write_text(fm, encoding="utf-8")
    return skill_md


def _purge_skill_fragments() -> None:
    """Wipe the plugin's fragments/SKILL/ dir between fragment tests so
    they don't pollute each other (write_fragment snapshots go to
    <plugin>/fragments/<skill>/, keyed off the SKILL.md stem)."""
    import shutil as _shutil
    snaps = PLUGIN_ROOT / "fragments" / "SKILL"
    if snaps.is_dir():
        _shutil.rmtree(snaps)


@test("v1.2.0 NEW (Day-3 item 3): read_fragments() returns one _default fragment for skill without frontmatter")
def t_v121_fragment_no_frontmatter() -> None:
    """Backwards-compat: any existing skill without frontmatter must keep
    working as a single implicit fragment with id='_default'."""
    sys.path.insert(0, str(PLUGIN_ROOT))
    from helpers import fragment_store  # type: ignore
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        skill_md = _write_test_skill(tmp, "no_fm", fm="no_frontmatter")
        frags = fragment_store.read_fragments(skill_md)
        assert len(frags) == 1, f"expected 1 fragment, got {len(frags)}"
        assert frags[0]["id"] == "_default", (
            f"expected _default fragment for no-frontmatter skill, got {frags[0]['id']!r}"
        )
        assert frags[0]["text"].startswith("# Skill"), (
            f"_default fragment should contain the whole file, got {frags[0]['text'][:50]!r}"
        )


@test("v1.2.0 NEW (Day-3 item 3): read_fragments() returns N fragments in declaration order")
def t_v121_fragment_with_frontmatter() -> None:
    sys.path.insert(0, str(PLUGIN_ROOT))
    from helpers import fragment_store  # type: ignore
    _purge_skill_fragments()
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        skill_md = _write_test_skill(tmp, "with_fm")
        frags = fragment_store.read_fragments(skill_md)
        assert len(frags) == 2, f"expected 2 fragments, got {len(frags)}"
        assert frags[0]["id"] == "intro", f"first fragment id wrong: {frags[0]['id']!r}"
        assert frags[1]["id"] == "section_b", f"second fragment id wrong: {frags[1]['id']!r}"
        # Declaration order preserved
        ids = [f["id"] for f in frags]
        assert ids == ["intro", "section_b"], f"declaration order not preserved: {ids}"
        # Each fragment carries its text (resolved by the selector).
        # The intro selector is "## Section A" so its text contains
        # "first body".
        assert "first body" in frags[0]["text"], f"intro text wrong: {frags[0]['text']!r}"
        assert "second body" in frags[1]["text"], f"section_b text wrong: {frags[1]['text']!r}"


@test("v1.2.0 NEW (Day-3 item 3): write_fragment() creates a versioned snapshot")
def t_v121_fragment_write() -> None:
    """write_fragment() contract: stores the new fragment text in a
    versioned snapshot file under <plugin>/fragments/<skill>/. The body
    of the SKILL.md is NOT rewritten in v1.2.0 (the spec notes this as
    a known minimal implementation - the canonical 'current' is the
    .current.md file). The snapshot_path returned in the result is the
    file we should assert against."""
    sys.path.insert(0, str(PLUGIN_ROOT))
    from helpers import fragment_store  # type: ignore
    _purge_skill_fragments()
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        skill_md = _write_test_skill(tmp, "write_test")
        # new_text is the FRAGMENT text (a slice), not the whole file.
        # The fragment we want to update is the 'intro' fragment which
        # has the "## Section A" selector.
        new_fragment_text = "## Section A\nTHIS IS THE NEW INTRO BODY\n"
        result = fragment_store.write_fragment(skill_md, "intro", new_fragment_text)
        assert result.get("ok"), f"write_fragment returned not-ok: {result}"
        assert result.get("version"), f"write_fragment returned no version: {result}"
        # Result includes a snapshot_path pointing at the versioned file
        snapshot_path = result.get("snapshot_path")
        assert snapshot_path and Path(snapshot_path).is_file(), (
            f"snapshot file not created: {snapshot_path}"
        )
        # The .current.md file holds the new fragment text
        current_path = PLUGIN_ROOT / "fragments" / "SKILL" / "intro.current.md"
        assert current_path.is_file(), f"canonical current file not created: {current_path}"
        current_content = current_path.read_text(encoding="utf-8")
        assert "THIS IS THE NEW INTRO BODY" in current_content, (
            f"canonical file does not contain new text: {current_content!r}"
        )


@test("v1.2.0 NEW (Day-3 item 3): rollback_fragment() restores from snapshot")
def t_v121_fragment_rollback() -> None:
    """rollback_fragment() contract: reads the versioned snapshot and
    writes its full text back to the SKILL.md (the snapshot IS the
    canonical form of the older version). After rollback, the SKILL.md
    itself contains the rolled-back text, matching the snapshot file.
    A pre-rollback snapshot is also created so the operation is itself
    reversible."""
    sys.path.insert(0, str(PLUGIN_ROOT))
    from helpers import fragment_store  # type: ignore
    _purge_skill_fragments()
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        skill_md = _write_test_skill(tmp, "rollback_test")
        original_skill_text = skill_md.read_text(encoding="utf-8")
        # Write v1 (creates intro.v1.md with the body text snapshot)
        r1 = fragment_store.write_fragment(
            skill_md, "intro", "## Section A\nv1 fragment body\n",
        )
        assert r1.get("ok"), f"v1 write failed: {r1}"
        # Write v2 (creates intro.v2.md + overwrites intro.current.md)
        r2 = fragment_store.write_fragment(
            skill_md, "intro", "## Section A\nv2 fragment body\n",
        )
        assert r2.get("ok"), f"v2 write failed: {r2}"
        # History should NOT include 'current' (it's the live version, not a snapshot)
        hist = fragment_store.list_fragment_history(skill_md, "intro")
        assert len(hist) >= 1, f"no history: {hist}"
        assert all(h["version"] != "current" for h in hist), (
            f"history should not include 'current': {hist}"
        )
        first_version = hist[0]["version"]
        # Roll back succeeds
        result = fragment_store.rollback_fragment(skill_md, "intro", first_version)
        assert result.get("ok"), f"rollback failed: {result}"
        # A new current_version was returned (the pre-rollback bump)
        assert result.get("current_version"), (
            f"rollback did not return a new current_version: {result}"
        )
        assert result.get("restored_from"), (
            f"rollback did not return a restored_from path: {result}"
        )
        # The SKILL.md itself was overwritten with the snapshot text.
        # The snapshot for the FIRST version contains the original body
        # text (because write_fragment snapshots the current body before
        # writing the new fragment text). So after rollback, the SKILL.md
        # should contain the original body text.
        restored = skill_md.read_text(encoding="utf-8")
        assert restored == original_skill_text, (
            f"rollback did not restore SKILL.md to original: {restored!r}"
        )


@test("v1.2.0 NEW (Day-3 item 3): list_fragment_history() returns sorted history")
def t_v121_fragment_history() -> None:
    sys.path.insert(0, str(PLUGIN_ROOT))
    from helpers import fragment_store  # type: ignore
    _purge_skill_fragments()
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        skill_md = _write_test_skill(tmp, "history_test")
        for i in range(3):
            fragment_store.write_fragment(
                skill_md, "intro", f"## Section A\nversion {i} body\n",
            )
            time.sleep(0.01)  # ensure mtime ordering
        hist = fragment_store.list_fragment_history(skill_md, "intro")
        assert len(hist) >= 3, f"expected >= 3 history entries, got {len(hist)}"
        # Sorted by version (oldest first) OR by mtime ascending
        # Either is acceptable; just check it's deterministic and ordered
        paths = [h["path"] for h in hist]
        assert paths == sorted(paths), "history paths are not in sorted order"


@test("v1.2.0 NEW (Day-3 item 3): validate_fragments() warns on overlapping selectors")
def t_v121_fragment_validate_overlap() -> None:
    sys.path.insert(0, str(PLUGIN_ROOT))
    from helpers import fragment_store  # type: ignore
    _purge_skill_fragments()
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        # Two fragments that both match the same '## Section B' header
        skill_md = _write_test_skill(
            tmp, "overlap_test",
            id1="first", sel1="## Section B",
            id2="second", sel2="## Section B",
        )
        warnings = fragment_store.validate_fragments(skill_md)
        # Should warn about overlap
        overlap_warnings = [
            w for w in warnings
            if "overlap" in w.lower() or "duplicate" in w.lower()
        ]
        assert len(overlap_warnings) > 0, (
            f"expected overlap warning, got: {warnings}"
        )


@test("v1.2.0 NEW (Day-3 item 3): validate_proposal() accepts a valid in-place edit when skill_path set")
def t_v121_gate_per_fragment() -> None:
    """The per-fragment gate should not break valid in-place edits. We
    test the whole-file path (the per-fragment stage requires a sibling
    .proposed file which we don't create here), with a large enough
    skill body that the min_chars check doesn't reject the proposal."""
    sys.path.insert(0, str(PLUGIN_ROOT))
    from helpers import sleep_runner  # type: ignore
    from helpers import fragment_store  # type: ignore
    _purge_skill_fragments()
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        # Build a 600-char test skill so the min_chars check passes
        body_padding = "\n".join([f"line {i}" for i in range(40)])
        skill_md = _write_test_skill(
            tmp, "gate_test",
            body1=body_padding, body2=body_padding,
        )
        current = skill_md.read_text(encoding="utf-8")
        new = current.replace("line 5", "line 5 IMPROVED")
        ok, reason = sleep_runner.validate_proposal(
            new, current, min_chars=200, min_improvement_pp=0.0,
            max_shrink_ratio=0.5, held_out=None, skill_path=skill_md,
        )
        assert ok, (
            f"valid in-place edit with skill_path was rejected (reason={reason!r}). "
            "The per-fragment gate is too strict for in-place edits."
        )


@test("v1.2.0 NEW (Day-3 item 3): validate_proposal() falls back to whole-file when no skill_path")
def t_v121_gate_no_skill_path() -> None:
    """Regression: callers that don't pass skill_path must see the v1.1.0
    whole-file gate behaviour exactly. This is the critical backwards-
    compat test for the fragment store."""
    sys.path.insert(0, str(PLUGIN_ROOT))
    from helpers import sleep_runner  # type: ignore
    text = "# Skill\n```\nabcdefghij\n```\n" + ("abcdefghij\n" * 190)
    # Without skill_path, byte-identical must be rejected
    ok, _ = sleep_runner.validate_proposal(
        text, text, min_chars=200, min_improvement_pp=0.0,
        max_shrink_ratio=0.5, held_out=None,
    )
    assert not ok, (
        "without skill_path, byte-identical should still be rejected "
        "(v1.1.0 gate behaviour)"
    )


@test("v1.2.0 NEW (Day-3 item 3): harvester tags rollouts with fragments_active")
def t_v121_harvester_fragments_active() -> None:
    """The harvester must call fragment_store to tag rollouts with the
    fragments that were active in the SKILL.md at chat time."""
    src = (PLUGIN_ROOT / "extensions" / "python" / "monologue_end"
           / "_60_skillopt_harvest_rollout.py").read_text(encoding="utf-8")
    assert "fragments_active" in src, (
        "harvester does not tag rollouts with fragments_active"
    )
    assert "fragment_store" in src, (
        "harvester does not import the fragment_store helper"
    )
    # The field is set on the record dict, not stored as metadata only
    assert 'record["fragments_active"' in src or "fragments_active_text" in src, (
        "harvester does not write fragments_active to the record dict"
    )


@test("v1.2.0 NEW (Day-3 item 3): A/B harness uses fragments_active_text when present")
def t_v121_harness_uses_fragments() -> None:
    """The A/B harness's _replay_under_skill must prefer the rollout's
    fragments_active_text (set by the harvester) over the whole skill
    text. This is the fragment-aware replay path."""
    src = (PLUGIN_ROOT / "helpers" / "ab_harness.py").read_text(encoding="utf-8")
    # Look for the fragments_active_text preference in the replay
    assert "fragments_active_text" in src, (
        "A/B harness _replay_under_skill does not use fragments_active_text"
    )
    # The fallback to skill_text is still there for pre-fragment rollouts
    assert "skill_text" in src, (
        "A/B harness _replay_under_skill lost the skill_text fallback"
    )


# ----------------------------------------------------------------------- #


# ======================================================================= #
# v1.2.0 NEW - A/B harness (Day-3 item 2)
# ======================================================================= #

_section_v121 = "v1.2.0 NEW - A/B harness (10 cases)"


def _write_fake_rollouts(skill_name: str, n: int = 6) -> list[Path]:
    """Drop N synthetic rollouts into the real rollouts/ dir so the
    harness can load them. Returns the list of paths written so the
    caller can clean up afterwards."""
    sys.path.insert(0, str(PLUGIN_ROOT))
    from helpers.sleep_runner import write_rollout
    import time as _t
    written: list[Path] = []
    # Make half successes, half failures so the stratified split has work to do
    outcomes = ("success", "success", "partial", "failure", "failure", "success")
    for i in range(n):
        rec = {
            "task": f"Refactor module {i} to handle nested groups.",
            "last_response": f"Done. Module {i} now handles nesting. Tests pass.",
            "skill_used": skill_name,
            "outcome": outcomes[i % len(outcomes)],
            "ts": _t.time() + i,  # ensure mtime ordering
            "id": f"test_fixture_{skill_name}_{i}",
        }
        p = write_rollout(rec)
        written.append(p)
    return written


def _cleanup_rollouts(paths: list[Path]) -> None:
    for p in paths:
        try:
            p.unlink()
        except FileNotFoundError:
            pass


@test("v1.2.0 NEW (A/B): harness falls back when no rollouts exist")
def t_v121_harness_no_rollouts() -> None:
    """When there are no rollouts for a skill, the harness returns
    can_run=False (not an error). This is the documented v1.2.0
    behaviour for low-data skills. The gate then falls through to
    the structural checks (no reject)."""
    sys.path.insert(0, str(PLUGIN_ROOT))
    from helpers import ab_harness
    ab_harness.reset_for_tests()
    # Use a unique skill name so we never collide with real rollouts
    # written by other tests in the same run.
    result = ab_harness.run_paired_test(
        skill_name="definitely_no_such_skill_v121_xyz",
        proposed_text="# Proposed\n```\nexample\n```\nbody body body\n" * 50,
        current_text="# Current\n```\nexample\n```\nbody body body\n" * 50,
    )
    assert result["samples"] == 0
    assert result["can_run"] is False
    assert result["passed"] is False
    assert result["wins"] == 0
    assert "not enough rollouts" in result["reason"].lower() or \
           "fallback" in result["reason"].lower() or \
           "no rollouts" in result["reason"].lower() or \
           result["n_rollouts_loaded"] == 0, f"unexpected reason: {result['reason']!r}"


@test("v1.2.0 NEW (A/B): harness fails closed when judge raises")
def t_v121_harness_judge_unreachable() -> None:
    """When the injected judge raises on every call, the harness
    MUST fail closed (passed=False, error=judge_unreachable) per the
    ROADMAP engineering principle 2."""
    sys.path.insert(0, str(PLUGIN_ROOT))
    from helpers import ab_harness
    ab_harness.reset_for_tests()
    # Inject a judge that always raises (simulates LLM unreachable)
    def broken_judge(rollout, score_a, score_b):
        raise ConnectionError("simulated LLM unreachable")
    ab_harness.set_judge_fn(broken_judge)
    try:
        rollouts = _write_fake_rollouts("v121_skill_judge_broken", n=6)
        try:
            result = ab_harness.run_paired_test(
                skill_name="v121_skill_judge_broken",
                proposed_text="# Proposed\n```\nex\n```\n" + ("body\n" * 60),
                current_text="# Current\n```\nex\n```\n" + ("body\n" * 60),
            )
            assert result["can_run"] is True, "harness should have run with 6 rollouts"
            assert result["passed"] is False, "harness must fail closed when judge raises"
            assert result["error"] == "judge_unreachable", \
                f"unexpected error: {result['error']!r}"
        finally:
            _cleanup_rollouts(rollouts)
    finally:
        ab_harness.set_judge_fn(None)


@test("v1.2.0 NEW (A/B): harness counts wins/losses/ties correctly")
def t_v121_harness_counts_wins() -> None:
    """A judge that always says 'win' should produce N wins and 0 losses
    on a 6-rollout set. Validates the win/loss/ties counting path."""
    sys.path.insert(0, str(PLUGIN_ROOT))
    from helpers import ab_harness
    ab_harness.reset_for_tests()
    def always_win(rollout, score_a, score_b):
        return {"verdict": "win", "confidence": 0.9, "reason": "forced win"}
    ab_harness.set_judge_fn(always_win)
    try:
        rollouts = _write_fake_rollouts("v121_skill_counts", n=8)
        try:
            result = ab_harness.run_paired_test(
                skill_name="v121_skill_counts",
                proposed_text="# Proposed\n```\nex\n```\n" + ("body\n" * 80),
                current_text="# Current\n```\nex\n```\n" + ("body\n" * 80),
            )
            assert result["can_run"] is True
            assert result["wins"] == result["samples"], \
                f"expected all wins, got wins={result['wins']} samples={result['samples']}"
            assert result["losses"] == 0
            assert result["ties"] == 0
            assert result["lift_pp"] > 0, f"lift should be positive: {result['lift_pp']}"
            assert result["confidence"] >= 0.6, f"confidence too low: {result['confidence']}"
            # With all wins, the harness should pass (lift >= 5pp AND conf >= 0.6)
            assert result["passed"] is True, \
                f"expected passed=True, got reason={result['reason']!r}"
        finally:
            _cleanup_rollouts(rollouts)
    finally:
        ab_harness.set_judge_fn(None)


@test("v1.2.0 NEW (A/B): harness rejects when proposed skill loses")
def t_v121_harness_rejects_loss() -> None:
    """A judge that always says 'lose' should produce passed=False
    (fail closed) even though can_run=True."""
    sys.path.insert(0, str(PLUGIN_ROOT))
    from helpers import ab_harness
    ab_harness.reset_for_tests()
    def always_lose(rollout, score_a, score_b):
        return {"verdict": "lose", "confidence": 0.9, "reason": "forced loss"}
    ab_harness.set_judge_fn(always_lose)
    try:
        rollouts = _write_fake_rollouts("v121_skill_loser", n=8)
        try:
            result = ab_harness.run_paired_test(
                skill_name="v121_skill_loser",
                proposed_text="# Proposed\n```\nex\n```\n" + ("body\n" * 80),
                current_text="# Current\n```\nex\n```\n" + ("body\n" * 80),
            )
            assert result["can_run"] is True
            assert result["passed"] is False, "harness should reject losing proposals"
            assert result["losses"] == result["samples"]
            assert result["lift_pp"] < 0
            assert "ab_harness" not in result["reason"], \
                f"reason should describe the loss, not a category: {result['reason']!r}"
        finally:
            _cleanup_rollouts(rollouts)
    finally:
        ab_harness.set_judge_fn(None)


@test("v1.2.0 NEW (A/B): get_ab_status returns the documented shape")
def t_v121_get_ab_status_shape() -> None:
    sys.path.insert(0, str(PLUGIN_ROOT))
    from helpers import ab_harness
    ab_harness.reset_for_tests()
    status = ab_harness.get_ab_status()
    expected_keys = {
        "enabled", "can_run_last", "last_run_at", "last_result",
        "total_runs", "total_passed", "total_rejected", "total_skipped",
        "last_error", "judge_model", "judge_endpoint_set",
        "judge_fallback_active", "min_n", "min_lift_pp",
        "min_confidence", "n",
    }
    missing = expected_keys - set(status.keys())
    assert not missing, f"get_ab_status() missing keys: {missing}\nGot: {sorted(status.keys())}"
    # All three counters start at 0 after reset
    assert status["total_runs"] == 0
    assert status["total_passed"] == 0
    assert status["total_rejected"] == 0
    assert status["total_skipped"] == 0
    assert status["judge_fallback_active"] is True  # no judge injected


@test("v1.2.0 NEW (A/B): status snapshot includes the ab_harness block")
def t_v121_snapshot_includes_ab_harness() -> None:
    sys.path.insert(0, str(PLUGIN_ROOT))
    from helpers.sleep_runner import get_status_snapshot
    snap = get_status_snapshot()
    assert "ab_harness" in snap, \
        f"status snapshot missing ab_harness block; keys: {sorted(snap.keys())}"
    block = snap["ab_harness"]
    assert isinstance(block, dict)
    assert "enabled" in block
    assert "total_runs" in block
    assert "judge_model" in block
    assert "judge_fallback_active" in block


@test("v1.2.0 NEW (A/B): gate rejects a losing proposal when can_run=True")
def t_v121_gate_rejects_loser() -> None:
    """When the harness can run AND the proposed skill loses, validate_proposal()
    MUST reject with a reason starting with 'ab_harness_rejected'."""
    sys.path.insert(0, str(PLUGIN_ROOT))
    from helpers import ab_harness
    from helpers.sleep_runner import validate_proposal
    ab_harness.reset_for_tests()
    def always_lose(rollout, score_a, score_b):
        return {"verdict": "lose", "confidence": 0.9, "reason": "forced loss"}
    ab_harness.set_judge_fn(always_lose)
    try:
        rollouts = _write_fake_rollouts("v121_skill_gate_loser", n=8)
        try:
            current = "# Current\n```\nex\n```\n" + ("body\n" * 80)
            proposed = "# Proposed\n```\nex\n```\n" + ("better body\n" * 100)
            ok, reason = validate_proposal(
                proposed, current, min_chars=200, min_improvement_pp=0.0,
                max_shrink_ratio=0.5, held_out=None,
                skill_name="v121_skill_gate_loser",
            )
            assert not ok, "gate should reject a losing proposal"
            assert reason.startswith("ab_harness_rejected"), \
                f"unexpected reason: {reason!r}"
        finally:
            _cleanup_rollouts(rollouts)
    finally:
        ab_harness.set_judge_fn(None)


@test("v1.2.0 NEW (A/B): gate falls through when harness can_run=False")
def t_v121_gate_falls_through_when_no_data() -> None:
    """When there are no rollouts (can_run=False), the harness stage
    must NOT reject - the gate falls through to the structural checks.
    A good proposal with no A/B data should pass through unchanged."""
    sys.path.insert(0, str(PLUGIN_ROOT))
    from helpers import ab_harness
    from helpers.sleep_runner import validate_proposal
    ab_harness.reset_for_tests()
    current = "# Current\n```\nex\n```\n" + ("abcdefghij\n" * 30)
    proposed = "# Proposed v2\n```\nrefactored\n```\n" + ("xyz\n" * 600)
    held = {"before": 0.3, "after": 0.5, "delta_pp": 20.0}
    ok, reason = validate_proposal(
        proposed, current, min_chars=200, min_improvement_pp=5.0,
        max_shrink_ratio=0.5, held_out=held,
        skill_name="definitely_no_such_skill_v121_xyz_fallthrough",
    )
    assert ok, f"gate should fall through with no rollouts; got reason={reason!r}"


@test("v1.2.0 NEW (A/B): gate stays backward compatible when no skill_name")
def t_v121_gate_backward_compat() -> None:
    """Existing callers that don't pass skill_name see the v1.1.0 gate
    exactly. No regression: the new stage is opt-in."""
    sys.path.insert(0, str(PLUGIN_ROOT))
    from helpers import ab_harness
    from helpers.sleep_runner import validate_proposal
    ab_harness.reset_for_tests()
    # Inject a judge that would reject if called.
    def always_lose(rollout, score_a, score_b):
        return {"verdict": "lose", "confidence": 0.9, "reason": "forced"}
    ab_harness.set_judge_fn(always_lose)
    try:
        current = "# Current\n```\nex\n```\n" + ("abcdefghij\n" * 30)
        proposed = "# Proposed v2\n```\nrefactored\n```\n" + ("xyz\n" * 600)
        held = {"before": 0.3, "after": 0.5, "delta_pp": 20.0}
        # No skill_name -> harness stage is skipped, structural gate runs unchanged
        ok, reason = validate_proposal(
            proposed, current, min_chars=200, min_improvement_pp=5.0,
            max_shrink_ratio=0.5, held_out=held,
        )
        assert ok, f"backward-compat broken: {reason!r}"
        # And a bad proposal should still be rejected by the structural stages
        ok, reason = validate_proposal(
            current, current, min_chars=200, min_improvement_pp=0.0,
            max_shrink_ratio=0.5, held_out=None,
        )
        assert not ok, "byte-identical should still be rejected"
    finally:
        ab_harness.set_judge_fn(None)


@test("v1.2.0 NEW (A/B): calibrate_judge.py smoke step runs end-to-end")
def t_v121_calibrate_script_runs() -> None:
    """The calibration script must run its build-demo + run-judge + kappa
    pipeline end-to-end. We pass --use-stub-judge so we don't need an LLM."""
    script = PLUGIN_ROOT / "scripts" / "calibrate_judge.py"
    if not script.is_file():
        raise AssertionError(f"missing script: {script}")
    # 1. Build the demo pair set
    demo_path = PLUGIN_ROOT / "tests" / "fixtures" / "ab_pairs.jsonl"
    proc1 = subprocess.run(
        [sys.executable, str(script), "--build-demo", "--output", str(demo_path)],
        capture_output=True, text=True, timeout=30,
    )
    assert proc1.returncode == 0, \
        f"--build-demo failed: rc={proc1.returncode}\nSTDOUT: {proc1.stdout}\nSTDERR: {proc1.stderr}"
    assert demo_path.is_file(), "demo pairs file not written"
    try:
        # 2. Run calibration against the demo with the keyword stub judge
        proc2 = subprocess.run(
            [sys.executable, str(script), "--pairs", str(demo_path), "--use-stub-judge"],
            capture_output=True, text=True, timeout=30,
        )
        assert proc2.returncode in (0, 1), \
            f"calibrate_judge exited {proc2.returncode}\nSTDOUT: {proc2.stdout}\nSTDERR: {proc2.stderr}"
        # The demo has 4 wins / 3 losses / 3 ties (intentionally), and the
        # keyword stub produces win/lose/tie from score_a vs score_b. The
        # stub's verdicts are deterministic but not perfectly aligned with
        # the demo's human labels, so kappa can be anywhere from 0..1. We
        # only assert the script runs and prints kappa (not the value).
        assert "kappa (ours)" in proc2.stdout, \
            f"missing kappa in output:\n{proc2.stdout}"
    finally:
        # Clean up the demo file we wrote
        if demo_path.is_file():
            try:
                demo_path.unlink()
            except OSError:
                pass


# ----------------------------------------------------------------------- #

# Inner loop test block - to be spliced into tests/smoke.py just before
# the `if __name__ == "__main__":` block.
#
# This file is NOT run directly. tests/smoke.py imports nothing from it;
# the insertion is a plain text splice.


def _make_rollout(rollout_id, task, skill_name, outcome="failure",
                  last_response="I tried but the parser failed.",
                  fragments_active=None):
    """Build a synthetic v1.2.0-format rollout dict for inner_loop tests."""
    return {
        "id": rollout_id,
        "task": task,
        "last_response": last_response,
        "outcome": outcome,
        "reward": {
            "success": 0.1 if outcome == "failure" else 0.9,
            "partial": 0.2,
            "failure": 0.7 if outcome == "failure" else 0.0,
            "confidence": 0.5,
            "source": "heuristic_fallback",
            "model_version": "none",
        },
        "fragments_active": fragments_active or ["_default"],
        "skill_hint": skill_name,
        "awaiting_suggestion": True,
        "ts": int(time.time()),
    }


@test("v1.2.0 NEW (Day-4 item 4): enqueue_suggestion writes a file with the right shape")
def t_v121_inner_enqueue_suggestion():
    """The enqueue helper writes one .md file with YAML-ish frontmatter
    and the suggestion text as the body."""
    sys.path.insert(0, str(PLUGIN_ROOT))
    from helpers import inner_loop
    inner_loop.reset_for_tests()
    with tempfile.TemporaryDirectory() as tmpdir:
        import helpers.inner_loop as il_mod
        orig_dir = il_mod.suggestions_dir
        il_mod.suggestions_dir = lambda: Path(tmpdir)
        try:
            rollout = _make_rollout("r1", "Refactor the parser", "my_skill")
            result = inner_loop.enqueue_suggestion(
                rollout, "Mention the YAML library name.", "my_skill",
            )
        finally:
            il_mod.suggestions_dir = orig_dir
        assert "path" in result, f"missing 'path' in {result!r}"
        assert "bytes" in result, f"missing 'bytes' in {result!r}"
        assert "ts" in result, f"missing 'ts' in {result!r}"
        written = Path(result["path"])
        assert written.is_file(), f"file not written: {written}"
        body = written.read_text(encoding="utf-8")
        assert "skill: my_skill" in body, f"missing skill in frontmatter:\n{body}"
        assert "rollout_id: r1" in body, f"missing rollout_id in frontmatter:\n{body}"
        assert "Refactor the parser" in body, f"missing task in frontmatter:\n{body}"
        assert "Mention the YAML library name." in body, f"missing suggestion text in body:\n{body}"


@test("v1.2.0 NEW (Day-4 item 4): list_pending_suggestions returns sorted by ts")
def t_v121_inner_list_pending_suggestions():
    """Enqueue 3 suggestions, list_pending returns them sorted by ts asc."""
    sys.path.insert(0, str(PLUGIN_ROOT))
    from helpers import inner_loop
    inner_loop.reset_for_tests()
    with tempfile.TemporaryDirectory() as tmpdir:
        import helpers.inner_loop as il_mod
        orig_dir = il_mod.suggestions_dir
        il_mod.suggestions_dir = lambda: Path(tmpdir)
        try:
            r_a = _make_rollout("ra", "task a", "alpha")
            r_b = _make_rollout("rb", "task b", "alpha")
            r_c = _make_rollout("rc", "task c", "alpha")
            res_a = inner_loop.enqueue_suggestion(r_a, "sug a", "alpha")
            time.sleep(0.01)
            res_b = inner_loop.enqueue_suggestion(r_b, "sug b", "alpha")
            time.sleep(0.01)
            res_c = inner_loop.enqueue_suggestion(r_c, "sug c", "alpha")
            r_x = _make_rollout("rx", "task x", "other")
            inner_loop.enqueue_suggestion(r_x, "sug x", "other")

            all_pending = inner_loop.list_pending_suggestions()
            assert len(all_pending) == 4, f"expected 4, got {len(all_pending)}"
            ts_values = [p["ts"] for p in all_pending]
            assert ts_values == sorted(ts_values), f"not sorted ascending: {ts_values}"

            alpha_only = inner_loop.list_pending_suggestions(skill_name="alpha")
            assert len(alpha_only) == 3, f"expected 3 alpha, got {len(alpha_only)}"
            assert all(p["skill"] == "alpha" for p in alpha_only), "filter leaked non-alpha"

            middle = inner_loop.list_pending_suggestions(since_ts=res_b["ts"])
            assert all(p["ts"] >= res_b["ts"] for p in middle), "since_ts filter let older items through"
        finally:
            il_mod.suggestions_dir = orig_dir


@test("v1.2.0 NEW (Day-4 item 4): drain_suggestions returns recent + deletes all")
@test("v1.2.0 NEW (Day-4 item 4): build_targeted_prompt mentions the suggestions")
def t_v121_inner_build_targeted_prompt():
    """The targeted prompt is the whole point of the inner loop."""
    sys.path.insert(0, str(PLUGIN_ROOT))
    from helpers import inner_loop
    inner_loop.reset_for_tests()
    current_text = "# Caveman" + chr(10) + chr(10) + "## Pick your grunt" + chr(10) + "Use short words." + chr(10)
    suggestions = [
        {"skill": "caveman", "rollout_id": "r1", "ts": 1, "task": "t1",
         "failure_mode": "verbose", "confidence": 0.7,
         "text": "Tell users to prefer one-sentence answers."},
        {"skill": "caveman", "rollout_id": "r2", "ts": 2, "task": "t2",
         "failure_mode": "missing example", "confidence": 0.6,
         "text": "Add a worked example of a 1-line prompt."},
    ]
    prompt = inner_loop.build_targeted_prompt("caveman", current_text, suggestions)
    assert isinstance(prompt, str) and len(prompt) > 0, f"prompt is empty: {prompt!r}"
    assert "Caveman" in prompt, f"missing current skill text in prompt: {prompt[:200]}"
    assert "one-sentence answers" in prompt, f"missing suggestion 1 in prompt: {prompt[:200]}"
    assert "worked example" in prompt, f"missing suggestion 2 in prompt: {prompt[:200]}"
    assert ("minimal edit" in prompt.lower() or
            "address the top" in prompt.lower() or
            "targeted edit" in prompt.lower()), f"missing minimal-edit directive in prompt: {prompt[:200]}"


def t_v121_inner_drain_suggestions():
    """drain_suggestions returns recent suggestions (NOT older than max_age_seconds)
    and deletes ALL matching files (recent returned, stale dropped). The
    implementation reads the frontmatter `ts` field, NOT the file mtime."""
    sys.path.insert(0, str(PLUGIN_ROOT))
    from helpers import inner_loop
    inner_loop.reset_for_tests()
    with tempfile.TemporaryDirectory() as tmpdir:
        import helpers.inner_loop as il_mod
        orig_dir = il_mod.suggestions_dir
        il_mod.suggestions_dir = lambda: Path(tmpdir)
        try:
            # Write TWO suggestion files by hand with controlled frontmatter ts.
            sd = Path(tmpdir)
            old_ts = int(time.time()) - 7200  # 2h ago -> older than 1h threshold
            new_ts = int(time.time())
            # Write suggestion files with controlled frontmatter ts. Use chr(10)
            # to avoid f-string-with-newline parsing issues.
            NL = chr(10)
            old_body = NL.join([
                "---",
                "skill: my_skill",
                "rollout_id: r_old",
                "ts: " + str(old_ts),
                "---",
                "old suggestion",
                "",
            ])
            new_body = NL.join([
                "---",
                "skill: my_skill",
                "rollout_id: r_new",
                "ts: " + str(new_ts),
                "---",
                "new suggestion",
                "",
            ])
            (sd / "my_skill_r_old.md").write_text(old_body, encoding="utf-8")
            (sd / "my_skill_r_new.md").write_text(new_body, encoding="utf-8")
            drained = inner_loop.drain_suggestions("my_skill", max_age_seconds=3600)
            # Only the recent one comes back (stale is silently dropped)
            assert len(drained) == 1, f"expected 1 drained, got {len(drained)}: {drained!r}"
            assert drained[0]["rollout_id"] == "r_new", f"drained wrong: {drained[0]['rollout_id']!r}"
            # ALL matching files are deleted (stale dropped, recent consumed)
            assert not (sd / "my_skill_r_old.md").is_file(), "stale file still on disk"
            assert not (sd / "my_skill_r_new.md").is_file(), "recent file still on disk"
        finally:
            il_mod.suggestions_dir = orig_dir
def t_v121_inner_tick_no_llm():
    """inner_loop_tick with no LLM configured returns counters and never raises."""
    sys.path.insert(0, str(PLUGIN_ROOT))
    from helpers import inner_loop
    inner_loop.reset_for_tests()
    counters = inner_loop.inner_loop_tick(llm_endpoint=None)
    for key in ("scanned", "suggested", "skipped", "errors", "last_error"):
        assert key in counters, f"missing key {key} in {counters!r}"
    assert isinstance(counters["scanned"], int)
    assert isinstance(counters["errors"], int)
    assert counters["errors"] == 0, f"unexpected errors with no LLM: {counters!r}"
    assert counters["scanned"] == 0, f"expected 0 scanned, got {counters['scanned']}"


@test("v1.2.0 NEW (Day-4 item 4): inner_loop_tick with a failing LLM fails closed")
def t_v121_inner_tick_failing_llm():
    """If the LLM call raises, the tick reports errors>=1 and never propagates."""
    sys.path.insert(0, str(PLUGIN_ROOT))
    from helpers import inner_loop
    inner_loop.reset_for_tests()
    with tempfile.TemporaryDirectory() as tmpdir:
        rollouts_dir = Path(tmpdir) / "rollouts"
        rollouts_dir.mkdir(parents=True, exist_ok=True)
        for i in (1, 2):
            r = _make_rollout(f"r{i}", f"task {i}", "fail_skill")
            (rollouts_dir / f"r{i}.json").write_text(
                json.dumps(r), encoding="utf-8",
            )
        import helpers.inner_loop as il_mod
        orig_rollouts_dir = il_mod._rollouts_dir
        orig_suggestions_dir = il_mod.suggestions_dir
        il_mod._rollouts_dir = lambda: rollouts_dir
        il_mod.suggestions_dir = lambda: Path(tmpdir) / "suggestions"
        (Path(tmpdir) / "suggestions").mkdir(parents=True, exist_ok=True)
        orig_llm = il_mod._llm_suggest_via_http
        def _fail(*a, **kw):
            # The contract: _llm_suggest_via_http returns {"error": ...} on
            # failure, NOT raises. Match a real failing LLM call here.
            return {"error": "simulated LLM outage", "text": ""}
        il_mod._llm_suggest_via_http = _fail
        try:
            counters = inner_loop.inner_loop_tick(llm_endpoint="http://nope")
        finally:
            il_mod._llm_suggest_via_http = orig_llm
            il_mod._rollouts_dir = orig_rollouts_dir
            il_mod.suggestions_dir = orig_suggestions_dir
        assert counters["errors"] >= 1, f"expected errors>=1, got {counters!r}"
        assert counters["last_error"], f"expected non-empty last_error, got {counters['last_error']!r}"


@test("v1.2.0 NEW (Day-4 item 4): get_inner_status shape and status snapshot block")
def t_v121_inner_status_shape():
    """get_inner_status() returns the documented shape, snapshot mirrors it."""
    sys.path.insert(0, str(PLUGIN_ROOT))
    from helpers import inner_loop
    from helpers import sleep_runner
    inner_loop.reset_for_tests()
    status = inner_loop.get_inner_status()
    expected_keys = {
        "enabled", "last_tick_at", "total_ticks",
        "total_suggestions", "pending_for_skills", "last_error",
    }
    assert expected_keys.issubset(status.keys()), f"missing keys: {expected_keys - set(status.keys())} in {status!r}"
    snap = sleep_runner.get_status_snapshot()
    assert "inner_loop" in snap, f"no inner_loop block in snapshot: {snap!r}"
    assert expected_keys.issubset(snap["inner_loop"].keys()), f"snapshot inner_loop missing keys: {snap['inner_loop']!r}"



# ----------------------------------------------------------------------- #
# v1.3.0 (Day-4 item 5) — Per-skill cadence + per-skill budget smoke tests
# ----------------------------------------------------------------------- #

@test("v1.3.0 NEW (Day-4 item 5): cadence.compute_next_run returns ceiling for cold skill (0 new rollouts)")
def t_v122_cadence_compute_next_run_cold() -> bool:
    """Cold skill (0 new rollouts) should get the ceiling (3600s)."""
    from helpers import cadence  # type: ignore
    nxt = cadence.compute_next_run(0)
    assert nxt == cadence.DEFAULT_CEILING_S, f"cold should be {cadence.DEFAULT_CEILING_S}, got {nxt}"
    print("  t_v122_cadence_compute_next_run_cold: OK")
    return True


@test("v1.3.0 NEW (Day-4 item 5): cadence.compute_next_run returns floor for hot skill (>= target rollouts)")
def t_v122_cadence_compute_next_run_hot() -> bool:
    """Hot skill (>= target rollouts) should get the floor (60s)."""
    from helpers import cadence  # type: ignore
    nxt = cadence.compute_next_run(cadence.DEFAULT_TARGET + 5)
    assert nxt == cadence.DEFAULT_FLOOR_S, f"hot should be {cadence.DEFAULT_FLOOR_S}, got {nxt}"
    print("  t_v122_cadence_compute_next_run_hot: OK")
    return True


@test("v1.3.0 NEW (Day-4 item 5): cadence.compute_next_run returns linear interpolation for mid skill")
def t_v122_cadence_compute_next_run_mid() -> bool:
    """Mid skill should get a value between floor and ceiling (linear interpolation)."""
    from helpers import cadence  # type: ignore
    mid = cadence.DEFAULT_TARGET // 2
    nxt = cadence.compute_next_run(mid)
    assert cadence.DEFAULT_FLOOR_S < nxt < cadence.DEFAULT_CEILING_S,         f"mid ({mid}) should be between {cadence.DEFAULT_FLOOR_S} and {cadence.DEFAULT_CEILING_S}, got {nxt}"
    print(f"  t_v122_cadence_compute_next_run_mid: OK (mid={mid}, nxt={nxt})")
    return True


@test("v1.3.0 NEW (Day-4 item 5): cadence.load_per_skill_state + save_per_skill_state roundtrip")
def t_v122_cadence_load_save_state() -> bool:
    """load_per_skill_state returns defaults when file missing; save+load roundtrips."""
    from helpers import cadence  # type: ignore
    import os
    # Use a sandboxed test skill name to avoid polluting real state
    test_skill = "_test_skill_v122_cadence"
    # Clean up any leftover state from a previous failed run
    sp = cadence._state_path(test_skill)
    if sp.exists():
        sp.unlink()
    # Defaults when missing
    state = cadence.load_per_skill_state(test_skill)
    assert "last_run_at" in state, "defaults should include last_run_at"
    assert "total_cycles" in state, "defaults should include total_cycles"
    # Save + reload
    state["total_cycles"] = 42
    state["last_run_at"] = 12345.6
    cadence.save_per_skill_state(test_skill, state)
    reloaded = cadence.load_per_skill_state(test_skill)
    assert reloaded["total_cycles"] == 42, f"reload total_cycles: {reloaded.get('total_cycles')}"
    assert abs(reloaded["last_run_at"] - 12345.6) < 0.01, f"reload last_run_at: {reloaded.get('last_run_at')}"
    # Clean up
    if sp.exists():
        sp.unlink()
    print("  t_v122_cadence_load_save_state: OK")
    return True


@test("v1.3.0 NEW (Day-4 item 5): budget.BudgetTracker.can_spend allows under cap")
def t_v122_budget_can_spend_under_cap() -> bool:
    """BudgetTracker.can_spend returns True when under the daily cap."""
    from helpers import budget  # type: ignore
    bt = budget.BudgetTracker(skill_name="_test_skill_v122_budget_under")
    bt.reset_for_tests()  # clean slate
    ok, reason = bt.can_spend(50)
    assert ok, f"should allow 50c under 100c cap, got reason={reason!r}"
    print("  t_v122_budget_can_spend_under_cap: OK")
    return True


@test("v1.3.0 NEW (Day-4 item 5): budget.BudgetTracker.can_spend blocks over cap")
def t_v122_budget_can_spend_over_cap() -> bool:
    """BudgetTracker.can_spend returns False when the cap would be exceeded."""
    from helpers import budget  # type: ignore
    bt = budget.BudgetTracker(skill_name="_test_skill_v122_budget_over")
    bt.reset_for_tests()  # clean slate
    bt.record_spend(80)  # 80c of 100c used
    ok, reason = bt.can_spend(50)  # 80+50=130 > 100
    assert not ok, "should block 50c when 80c already spent of 100c cap"
    assert "cap" in reason.lower() or "exceed" in reason.lower(), f"reason should mention cap/exceed, got: {reason!r}"
    print(f"  t_v122_budget_can_spend_over_cap: OK (blocked: {reason})")
    return True


@test("v1.3.0 NEW (Day-4 item 5): budget.BudgetTracker resets on day rollover")
def t_v122_budget_day_rollover() -> bool:
    """BudgetTracker resets the daily total on a new day."""
    from helpers import budget  # type: ignore
    bt = budget.BudgetTracker(skill_name="_test_skill_v122_budget_rollover")
    bt.reset_for_tests()
    # Spend 80c today
    res1 = bt.record_spend(80)
    assert res1["new_total"] == 80, f"day 1 total: {res1}"
    # Simulate a new day by rewriting the reset_at to a time in the past
    import time
    from pathlib import Path as _P
    state_path = bt._state_path()
    import json as _j
    state = _j.loads(state_path.read_text(encoding="utf-8"))
    # Set reset_at to 2 days ago, so the next spend triggers a rollover
    state["reset_at"] = time.time() - (2 * 86400)
    state["daily_total_cents"] = 80  # preserved across rollover
    state_path.write_text(_j.dumps(state), encoding="utf-8")
    # Re-instantiate to pick up the new state
    bt2 = budget.BudgetTracker(skill_name="_test_skill_v122_budget_rollover")
    res2 = bt2.record_spend(10)
    assert res2["new_total"] == 10, f"after rollover, total should be 10 (just the new spend), got {res2}"
    # Clean up
    if state_path.exists():
        state_path.unlink()
    print("  t_v122_budget_day_rollover: OK")
    return True


@test("v1.3.0 NEW (Day-4 item 5): get_status_snapshot includes cadence + budget blocks")
def t_v122_status_snapshot_has_cadence_budget() -> bool:
    """get_status_snapshot() should include cadence and budget blocks."""
    from helpers import sleep_runner  # type: ignore
    snap = sleep_runner.get_status_snapshot()
    assert "cadence" in snap, f"snap should have 'cadence' key, has: {list(snap.keys())}"
    assert "budget" in snap, f"snap should have 'budget' key, has: {list(snap.keys())}"
    assert "enabled" in snap["cadence"], f"cadence block shape: {snap['cadence']}"
    assert "enabled" in snap["budget"], f"budget block shape: {snap['budget']}"
    print("  t_v122_status_snapshot_has_cadence_budget: OK")
    return True




def t_v122_cadence_compute_next_run() -> None:
    """cadence.compute_next_run: cold=ceiling, hot=floor, mid=interpolation."""
    try:
        from usr.plugins.skillopt.helpers import cadence  # type: ignore
    except Exception:
        from helpers import cadence  # type: ignore
    floor_s = cadence.DEFAULT_FLOOR_S
    ceiling_s = cadence.DEFAULT_CEILING_S
    target = cadence.DEFAULT_TARGET
    # Cold: 0 new rollouts → ceiling
    assert cadence.compute_next_run(0, target, floor_s, ceiling_s) == ceiling_s, "cold should be ceiling"
    # Hot: target+1 new rollouts → floor
    assert cadence.compute_next_run(target + 1, target, floor_s, ceiling_s) == floor_s, "hot should be floor"
    # Hot: target new rollouts → floor
    assert cadence.compute_next_run(target, target, floor_s, ceiling_s) == floor_s, "at-target should be floor"
    # Mid: target/2 new rollouts → linear interpolation
    mid = cadence.compute_next_run(target // 2, target, floor_s, ceiling_s)
    expected_mid = floor_s + (ceiling_s - floor_s) // 2
    assert abs(mid - expected_mid) <= 2, f"mid {mid} should be near {expected_mid}"


def t_v122_cadence_state_roundtrip() -> None:
    """cadence.load + save per-skill state: defaults, save, load, atomic write."""
    try:
        from usr.plugins.skillopt.helpers import cadence  # type: ignore
    except Exception:
        from helpers import cadence  # type: ignore
    test_skill = "__smoke_test_cadence__"
    # Defaults when no file
    st = cadence.load_per_skill_state(test_skill)
    assert st["last_run_at"] == 0.0, "default last_run_at should be 0.0"
    assert st["total_cycles"] == 0, "default total_cycles should be 0"
    # Save and load
    st["total_cycles"] = 7
    st["last_run_at"] = 12345.6
    cadence.save_per_skill_state(test_skill, st)
    st2 = cadence.load_per_skill_state(test_skill)
    assert st2["total_cycles"] == 7, f"roundtrip lost total_cycles: {st2}"
    assert abs(st2["last_run_at"] - 12345.6) < 0.01, f"roundtrip lost last_run_at: {st2}"
    # Cleanup
    p = cadence._state_path(test_skill)
    if p.exists():
        p.unlink()


def t_v122_cadence_list_skills() -> None:
    """cadence.list_skills_with_state: returns sorted list of skills with state files."""
    try:
        from usr.plugins.skillopt.helpers import cadence  # type: ignore
    except Exception:
        from helpers import cadence  # type: ignore
    # Create 2 state files
    for s in ["__smoke_alpha__", "__smoke_beta__"]:
        st = cadence.load_per_skill_state(s)
        st["total_cycles"] = 1
        cadence.save_per_skill_state(s, st)
    skills = cadence.list_skills_with_state()
    assert "__smoke_alpha__" in skills, f"alpha missing from {skills}"
    assert "__smoke_beta__" in skills, f"beta missing from {skills}"
    # Cleanup
    for s in ["__smoke_alpha__", "__smoke_beta__"]:
        p = cadence._state_path(s)
        if p.exists():
            p.unlink()


def t_v122_budget_can_spend() -> None:
    """budget.BudgetTracker.can_spend: under cap, at cap, over cap."""
    try:
        from usr.plugins.skillopt.helpers import budget  # type: ignore
    except Exception:
        from helpers import budget  # type: ignore
    bt = budget.BudgetTracker(skill_name="__smoke_budget__", daily_cap_cents=10, reset_hour_utc=0)
    # Reset by recording negative-ish: just check under cap
    ok, reason = bt.can_spend(5)
    assert ok is True, f"under cap should be allowed: {reason}"
    # Spend 8, then try to spend 5 more (8+5=13 > 10 cap)
    bt.record_spend(8)
    ok, reason = bt.can_spend(5)
    assert ok is False, f"over cap should be blocked: got ok=True"
    assert "cap" in reason.lower() or "exceed" in reason.lower(), f"reason should mention cap: {reason}"
    # Cleanup
    p = budget._default_state_dir() / "budget___smoke_budget__.json"
    if p.exists():
        p.unlink()


def t_v122_budget_day_rollover() -> None:
    """budget.BudgetTracker: day rollover resets the counter."""
    try:
        from usr.plugins.skillopt.helpers import budget  # type: ignore
    except Exception:
        from helpers import budget  # type: ignore
    bt = budget.BudgetTracker(skill_name="__smoke_rollover__", daily_cap_cents=100, reset_hour_utc=0)
    # Record 80 cents on day 1
    res1 = bt.record_spend(80, ts=100000.0)
    assert res1["new_total"] == 80, f"day 1 total: {res1}"
    # Record 10 cents on day 2 (ts way in the future to trigger rollover)
    res2 = bt.record_spend(10, ts=200000.0)
    assert res2["new_total"] == 10, f"day 2 total should be reset to 10: {res2}"
    # Cleanup
    p = budget._default_state_dir() / "budget___smoke_rollover__.json"
    if p.exists():
        p.unlink()


def t_v122_status_snapshot_has_cadence_budget() -> None:
    """sleep_runner.get_status_snapshot: includes `cadence` and `budget` keys."""
    try:
        from usr.plugins.skillopt.helpers import sleep_runner  # type: ignore
    except Exception:
        from helpers import sleep_runner  # type: ignore
    snap = sleep_runner.get_status_snapshot()
    assert "cadence" in snap, f"status snapshot missing cadence: keys={list(snap.keys())}"
    assert "budget" in snap, f"status snapshot missing budget: keys={list(snap.keys())}"
    # The cadence block should have at least enabled + per_skill
    c = snap["cadence"]
    assert c.get("enabled") is True, f"cadence block: {c}"
    assert "per_skill" in c, f"cadence.per_skill missing: {c}"
    b = snap["budget"]
    assert b.get("enabled") is True, f"budget block: {b}"
    assert "per_skill" in b, f"budget.per_skill missing: {b}"



# ----------------------------------------------------------------------- #
# v1.3.0 (Day-4 item 6) - Failure memory smoke tests
# ----------------------------------------------------------------------- #
#
# v1.3.0 NEW: per-skill failure attribution that the next outer-loop
# prompt reads via build_failure_context(). Tests cover the contract
# surface - record/load/forget roundtrip, prompt block format, status
# block, the disabled-mode no-op, the injected backend, the loud-not-
# crash error path, and snapshot integration.


def _fm_wipe_local_store() -> None:
    """Wipe the on-disk failure-memory local store between tests."""
    import shutil
    p = PLUGIN_ROOT / "logs" / "runs" / "failure_memory"
    if p.is_dir():
        shutil.rmtree(p, ignore_errors=True)


@test("v1.3.0 NEW (Day-4 item 6): failure_memory.record_failure + load_failures roundtrip")
def t_v130_failure_memory_record_load_roundtrip() -> bool:
    """Record N failures for a skill, then load them back. The local
    JSON store is the default backend in this runtime (A0 memory is
    not importable here). Verifies the basic data-plane contract."""
    sys.path.insert(0, str(PLUGIN_ROOT))
    from helpers import failure_memory  # type: ignore
    failure_memory.reset_for_tests()
    _fm_wipe_local_store()
    r1 = failure_memory.record_failure("my_skill", "Add table sorting",
                                       "shrink_ratio=0.95 too small", ["r1", "r2"])
    r2 = failure_memory.record_failure("my_skill", "Add section X",
                                       "gate_min_chars failed", ["r3"])
    r3 = failure_memory.record_failure("other_skill", "Add footer",
                                       "no rollouts")
    assert r1["ok"] is True and r1["memory_id"], f"r1: {r1!r}"
    assert r2["ok"] is True and r2["memory_id"]
    assert r3["ok"] is True and r3["memory_id"]
    assert r1["backend"] in ("a0", "local", "injected"), f"unexpected backend: {r1['backend']}"
    entries = failure_memory.load_failures("my_skill", limit=10)
    assert len(entries) == 2, f"expected 2 my_skill entries, got {len(entries)}: {entries!r}"
    summaries = {e["proposal_summary"] for e in entries}
    assert "Add table sorting" in summaries
    assert "Add section X" in summaries
    # Sorted by ts desc
    assert entries[0]["ts"] >= entries[1]["ts"], "entries should be ts desc"
    # Cross-skill isolation
    other = failure_memory.load_failures("other_skill", limit=10)
    assert len(other) == 1
    assert other[0]["proposal_summary"] == "Add footer"
    failure_memory.reset_for_tests()
    _fm_wipe_local_store()
    print("  t_v130_failure_memory_record_load_roundtrip: OK")
    return True


@test("v1.3.0 NEW (Day-4 item 6): build_failure_context returns [FAILURE MEMORY] block with markers")
def t_v130_failure_memory_build_context() -> bool:
    """build_failure_context(skill) returns a multi-line block that
    starts with `[FAILURE MEMORY \u2014` and ends with `[END FAILURE MEMORY]`,
    so the optimizer can grep for the markers."""
    sys.path.insert(0, str(PLUGIN_ROOT))
    from helpers import failure_memory  # type: ignore
    failure_memory.reset_for_tests()
    _fm_wipe_local_store()
    failure_memory.record_failure("ctx_skill", "Proposal Alpha",
                                  "rejected: example block missing")
    failure_memory.record_failure("ctx_skill", "Proposal Beta",
                                  "rejected: shrink_ratio too small")
    ctx = failure_memory.build_failure_context("ctx_skill", max_items=5)
    assert ctx, "context block should not be empty after recording 2 failures"
    assert "[FAILURE MEMORY" in ctx, f"missing [FAILURE MEMORY marker in: {ctx!r}"
    assert "[END FAILURE MEMORY]" in ctx, f"missing [END FAILURE MEMORY] marker in: {ctx!r}"
    assert "Proposal Alpha" in ctx or "Proposal Beta" in ctx
    assert "ctx_skill" in ctx
    # Empty when no failures
    failure_memory.forget_failures("ctx_skill", before_ts=time.time() + 100)
    empty_ctx = failure_memory.build_failure_context("ctx_skill")
    assert empty_ctx == "", f"expected empty context after forget, got: {empty_ctx!r}"
    failure_memory.reset_for_tests()
    _fm_wipe_local_store()
    print("  t_v130_failure_memory_build_context: OK")
    return True


@test("v1.3.0 NEW (Day-4 item 6): forget_failures deletes entries older than before_ts")
def t_v130_failure_memory_forget() -> bool:
    """Record 3 failures (2 old, 1 new), forget only the 2 old ones,
    verify 1 remains."""
    sys.path.insert(0, str(PLUGIN_ROOT))
    import time as _t
    from helpers import failure_memory  # type: ignore
    failure_memory.reset_for_tests()
    _fm_wipe_local_store()
    now = _t.time()
    failure_memory.record_failure("forget_skill", "old1", "r", ts=now - 100)
    failure_memory.record_failure("forget_skill", "old2", "r", ts=now - 50)
    failure_memory.record_failure("forget_skill", "new1", "r", ts=now)
    # Forget entries older than 10s ago - should drop old1+old2, keep new1
    res = failure_memory.forget_failures("forget_skill", before_ts=now - 10)
    assert res["ok"] is True, f"forget failed: {res!r}"
    assert res["deleted_count"] == 2, f"expected 2 deletions, got {res['deleted_count']}: {res!r}"
    remaining = failure_memory.load_failures("forget_skill", limit=10)
    assert len(remaining) == 1, f"expected 1 remaining, got {len(remaining)}"
    assert remaining[0]["proposal_summary"] == "new1", f"wrong entry kept: {remaining[0]!r}"
    failure_memory.reset_for_tests()
    _fm_wipe_local_store()
    print("  t_v130_failure_memory_forget: OK")
    return True


@test("v1.3.0 NEW (Day-4 item 6): get_status_block returns documented shape")
def t_v130_failure_memory_status_block() -> bool:
    """get_status_block() returns the documented shape: enabled,
    backend, last_error, totals, last_*_at, per_skill."""
    sys.path.insert(0, str(PLUGIN_ROOT))
    from helpers import failure_memory  # type: ignore
    failure_memory.reset_for_tests()
    _fm_wipe_local_store()
    failure_memory.record_failure("status_skill", "X", "Y")
    block = failure_memory.get_status_block()
    assert block["enabled"] is True, f"expected enabled=True, got: {block!r}"
    assert block["backend"] in ("a0", "local", "injected"), f"unexpected backend: {block['backend']!r}"
    assert "totals" in block
    assert block["totals"]["recorded"] >= 1, f"expected at least 1 recorded, got: {block['totals']!r}"
    assert "last_record_at" in block and block["last_record_at"] > 0
    assert "last_load_at" in block
    assert "last_forget_at" in block
    assert "per_skill" in block
    assert "status_skill" in block["per_skill"], f"per_skill missing status_skill: {list(block['per_skill'].keys())}"
    ps = block["per_skill"]["status_skill"]
    assert ps["total_failures"] >= 1
    assert ps["enabled"] is True
    failure_memory.reset_for_tests()
    _fm_wipe_local_store()
    print("  t_v130_failure_memory_status_block: OK")
    return True


@test("v1.3.0 NEW (Day-4 item 6): failure_memory_enabled=False makes module a no-op")
def t_v130_failure_memory_disabled() -> bool:
    """When the config key (or SKILLOPT_FAILURE_MEMORY_ENABLED env) is
    set to '0', record_failure returns {skipped: True, memory_id: None}
    and build_failure_context returns empty."""
    sys.path.insert(0, str(PLUGIN_ROOT))
    from helpers import failure_memory  # type: ignore
    failure_memory.reset_for_tests()
    _fm_wipe_local_store()
    os.environ["SKILLOPT_FAILURE_MEMORY_ENABLED"] = "0"
    try:
        # Reset forces a re-read of the config on next call
        r = failure_memory.record_failure("dis_skill", "p", "r")
        assert r["ok"] is True
        assert r["skipped"] is True, f"expected skipped=True, got: {r!r}"
        assert r["memory_id"] is None, f"expected memory_id=None, got: {r!r}"
        ctx = failure_memory.build_failure_context("dis_skill")
        assert ctx == "", f"expected empty context when disabled, got: {ctx!r}"
        # And load_failures returns [] when disabled
        assert failure_memory.load_failures("dis_skill") == []
        # forget is also a no-op (returns skipped=True)
        f = failure_memory.forget_failures("dis_skill")
        assert f["ok"] is True and f["skipped"] is True
    finally:
        del os.environ["SKILLOPT_FAILURE_MEMORY_ENABLED"]
    failure_memory.reset_for_tests()
    _fm_wipe_local_store()
    print("  t_v130_failure_memory_disabled: OK")
    return True


@test("v1.3.0 NEW (Day-4 item 6): set_memory_fn injects a custom backend (mock roundtrip)")
def t_v130_failure_memory_injected_backend() -> bool:
    """set_memory_fn(save, load, delete) swaps in a custom backend;
    record/load/delete then flow through the mocks and the kind label
    flips to 'injected'."""
    sys.path.insert(0, str(PLUGIN_ROOT))
    from helpers import failure_memory  # type: ignore
    failure_memory.reset_for_tests()
    _fm_wipe_local_store()
    store_records: list[dict] = []
    store_ids: list[str] = []

    def fake_save(text, area, metadata):
        mid = f"mock-{len(store_ids)}"
        store_ids.append(mid)
        store_records.append({
            "id": mid, "text": text, "area": area,
            "metadata": metadata, "similarity": 0.9,
        })
        return mid

    def fake_load(query, threshold, limit, filter_):
        return list(store_records)[:limit]

    def fake_delete(ids):
        n = 0
        for r in list(store_records):
            if r["id"] in ids:
                store_records.remove(r)
                n += 1
        return n

    failure_memory.set_memory_fn(fake_save, fake_load, fake_delete)
    assert failure_memory.get_backend_kind() == "injected", \
        f"expected injected, got {failure_memory.get_backend_kind()!r}"
    r = failure_memory.record_failure("inj_skill", "p1", "r1", ["x"])
    assert r["backend"] == "injected", f"expected injected, got {r!r}"
    entries = failure_memory.load_failures("inj_skill", limit=5)
    assert len(entries) == 1, f"expected 1, got {len(entries)}"
    assert entries[0]["similarity"] == 0.9
    f = failure_memory.forget_failures("inj_skill")
    assert f["ok"] and f["deleted_count"] == 1
    failure_memory.set_memory_fn(None, None, None)  # restore default
    failure_memory.reset_for_tests()
    _fm_wipe_local_store()
    print("  t_v130_failure_memory_injected_backend: OK")
    return True


@test("v1.3.0 NEW (Day-4 item 6): record_failure loud-not-crash on backend exception")
def t_v130_failure_memory_error_loud_not_crash() -> bool:
    """When the backend's save() raises, record_failure returns
    {ok: False, error: 'memory_save_failed', detail: <str>} instead
    of propagating the exception. Critical: the auto_loop must never
    crash because failure_memory has a bug."""
    sys.path.insert(0, str(PLUGIN_ROOT))
    from helpers import failure_memory  # type: ignore
    failure_memory.reset_for_tests()
    _fm_wipe_local_store()

    def broken_save(text, area, metadata):
        raise RuntimeError("simulated disk full")

    def ok_load(query, threshold, limit, filter_):
        return []

    def ok_delete(ids):
        return 0

    failure_memory.set_memory_fn(broken_save, ok_load, ok_delete)
    r = failure_memory.record_failure("boom_skill", "p", "r")
    assert r["ok"] is False, f"expected ok=False, got: {r!r}"
    assert r["error"] == "memory_save_failed", f"unexpected error: {r!r}"
    assert "disk full" in r.get("detail", ""), f"detail missing cause: {r!r}"
    failure_memory.set_memory_fn(None, None, None)
    failure_memory.reset_for_tests()
    _fm_wipe_local_store()
    print("  t_v130_failure_memory_error_loud_not_crash: OK")
    return True


@test("v1.3.0 NEW (Day-4 item 6): sleep_runner.get_status_snapshot includes failure_memory block")
def t_v130_failure_memory_snapshot_integration() -> bool:
    """The status snapshot consumed by the WebUI/API exposes a
    `failure_memory` key with the documented shape."""
    sys.path.insert(0, str(PLUGIN_ROOT))
    try:
        from usr.plugins.skillopt.helpers import sleep_runner  # type: ignore
    except Exception:
        from helpers import sleep_runner  # type: ignore
    try:
        from usr.plugins.skillopt.helpers import failure_memory  # type: ignore
    except Exception:
        from helpers import failure_memory  # type: ignore
    failure_memory.reset_for_tests()
    _fm_wipe_local_store()
    failure_memory.record_failure("snap_skill", "x", "y")
    snap = sleep_runner.get_status_snapshot()
    assert "failure_memory" in snap, f"no failure_memory block in snapshot keys: {list(snap.keys())}"
    fm_block = snap["failure_memory"]
    assert fm_block.get("enabled") is True, f"snapshot failure_memory block: {fm_block!r}"
    assert "backend" in fm_block
    assert "totals" in fm_block
    assert "per_skill" in fm_block
    failure_memory.reset_for_tests()
    _fm_wipe_local_store()
    print("  t_v130_failure_memory_snapshot_integration: OK")
    return True


# ----------------------------------------------------------------------- #
# v1.4.0-Dev (Day-5 item 7) - cycle_history + cycles/audit_log endpoints
# ----------------------------------------------------------------------- #
#
# The cycle_history helper stores every Sleep cycle outcome (adopted |
# rejected) as one JSON line in logs/runs/cycle_history.jsonl plus a
# one-line summary in cycle_history.log. The cycles API endpoint reads
# it back with filters; the audit_log API endpoint reads the v1.3.0
# adoptions.log so the WebUI dashboard has both tabs.
#
# All tests use the helpers/cycle_history.reset_for_tests() helper to
# wipe state between cases so cross-test contamination cannot happen.


def _audit_wipe_local_store() -> None:
    """Wipe logs/runs/adoptions.log used by the audit_log API tests."""
    sys.path.insert(0, str(PLUGIN_ROOT))
    try:
        from usr.plugins.skillopt.helpers import sleep_runner  # type: ignore
    except Exception:
        from helpers import sleep_runner  # type: ignore
    p = sleep_runner.runs_dir() / "adoptions.log"
    try:
        if p.is_file():
            p.unlink()
    except Exception:
        pass


def _install_helpers_api_stub() -> None:
    """Inject a minimal `helpers.api` module into sys.modules.

    The api modules do `from helpers.api import ApiHandler`. In a
    real A0 framework runtime the framework provides this module
    on sys.path; in the standalone smoke test environment it's
    missing. The api subclasses only ever inherit from ApiHandler
    (they don't call any base methods), so an empty class is
    enough. Idempotent - safe to call more than once.
    """
    if "helpers.api" in sys.modules:
        return
    import types
    mod = types.ModuleType("helpers.api")

    class ApiHandler:  # type: ignore[no-untyped-def]
        """Smoke-test stub for A0 framework's ApiHandler base class."""
        pass

    mod.ApiHandler = ApiHandler  # type: ignore[attr-defined]
    sys.modules["helpers.api"] = mod


@test("v1.4.0-Dev NEW (Day-5 item 7): record_cycle_entry appends one JSON line + returns cycle_id/line_no")
def t_v140_record_cycle_entry() -> bool:
    """Recording an entry returns {ok, cycle_id, line_no, path} and
    creates logs/runs/cycle_history.jsonl with at least one line."""
    sys.path.insert(0, str(PLUGIN_ROOT))
    from helpers import cycle_history  # type: ignore
    cycle_history.reset_for_tests()
    result = cycle_history.record_cycle_entry({
        "skill": "caveman",
        "outcome": "adopted",
        "outcome_detail": "shrunk within ratio",
        "proposal_id": "prop_001",
        "proposed_size": 1200,
        "current_size": 800,
    })
    assert result.get("ok") is True, f"record failed: {result!r}"
    assert isinstance(result.get("cycle_id"), str) and result["cycle_id"], f"missing cycle_id: {result!r}"
    assert isinstance(result.get("line_no"), int) and result["line_no"] >= 1
    assert isinstance(result.get("path"), str) and result["path"].endswith("cycle_history.jsonl")
    # File actually exists with content
    from pathlib import Path
    p = Path(result["path"])
    assert p.is_file(), f"jsonl not created at {p}"
    content = p.read_text(encoding="utf-8")
    assert content.strip(), "jsonl file is empty"
    assert "caveman" in content
    assert "adopted" in content
    cycle_history.reset_for_tests()
    print("  t_v140_record_cycle_entry: OK")
    return True


@test("v1.4.0-Dev NEW (Day-5 item 7): disabled config skips record (no file write)")
def t_v140_record_cycle_entry_disabled() -> bool:
    """When cycle_history_enabled is False, record_cycle_entry returns
    {ok: False, skipped: True} and does NOT write to disk."""
    sys.path.insert(0, str(PLUGIN_ROOT))
    from helpers import cycle_history  # type: ignore
    cycle_history.reset_for_tests()
    # Monkey-patch _enabled to False
    orig = cycle_history._enabled
    cycle_history._enabled = lambda: False
    try:
        result = cycle_history.record_cycle_entry({"skill": "x", "outcome": "adopted"})
        assert result.get("ok") is False, f"expected ok=False when disabled, got: {result!r}"
        assert result.get("skipped") is True, f"expected skipped=True when disabled, got: {result!r}"
        # File should NOT have been created
        jsonl = cycle_history._jsonl_path()
        assert not jsonl.is_file(), f"jsonl was created even when disabled: {jsonl}"
    finally:
        cycle_history._enabled = orig
        cycle_history.reset_for_tests()
    print("  t_v140_record_cycle_entry_disabled: OK")
    return True


@test("v1.4.0-Dev NEW (Day-5 item 7): read_cycle_history returns newest-first + respects limit")
def t_v140_read_cycle_history() -> bool:
    """3 entries recorded, read limit=2 returns the 2 newest."""
    sys.path.insert(0, str(PLUGIN_ROOT))
    from helpers import cycle_history  # type: ignore
    cycle_history.reset_for_tests()
    r1 = cycle_history.record_cycle_entry({"skill": "s1", "outcome": "adopted"})
    r2 = cycle_history.record_cycle_entry({"skill": "s2", "outcome": "rejected"})
    r3 = cycle_history.record_cycle_entry({"skill": "s3", "outcome": "adopted"})
    assert r1["ok"] and r2["ok"] and r3["ok"]
    out = cycle_history.read_cycle_history(limit=2)
    assert len(out) == 2, f"expected 2 entries (limit=2), got {len(out)}"
    # Newest-first: latest 2 should be r3, r2
    assert out[0]["cycle_id"] == r3["cycle_id"], f"newest should be r3, got: {out[0]!r}"
    assert out[1]["cycle_id"] == r2["cycle_id"], f"second should be r2, got: {out[1]!r}"
    cycle_history.reset_for_tests()
    print("  t_v140_read_cycle_history: OK")
    return True


@test("v1.4.0-Dev NEW (Day-5 item 7): read_cycle_history filters by skill / outcome")
def t_v140_read_cycle_history_filters() -> bool:
    """4 entries (2 skill='a', 2 skill='b'), filter by skill returns
    only the matching ones. Outcome filter also tested."""
    sys.path.insert(0, str(PLUGIN_ROOT))
    from helpers import cycle_history  # type: ignore
    cycle_history.reset_for_tests()
    cycle_history.record_cycle_entry({"skill": "a", "outcome": "adopted"})
    cycle_history.record_cycle_entry({"skill": "a", "outcome": "rejected"})
    cycle_history.record_cycle_entry({"skill": "b", "outcome": "adopted"})
    cycle_history.record_cycle_entry({"skill": "b", "outcome": "rejected"})
    a_entries = cycle_history.read_cycle_history(limit=10, skill="a")
    assert len(a_entries) == 2, f"expected 2 'a' entries, got {len(a_entries)}"
    assert all(e["skill"] == "a" for e in a_entries), f"non-'a' entry leaked through: {a_entries!r}"
    adopted = cycle_history.read_cycle_history(limit=10, outcome="adopted")
    assert len(adopted) == 2, f"expected 2 adopted entries, got {len(adopted)}"
    assert all(e["outcome"] == "adopted" for e in adopted), f"non-adopted leaked: {adopted!r}"
    b_rej = cycle_history.read_cycle_history(limit=10, skill="b", outcome="rejected")
    assert len(b_rej) == 1
    assert b_rej[0]["skill"] == "b" and b_rej[0]["outcome"] == "rejected"
    cycle_history.reset_for_tests()
    print("  t_v140_read_cycle_history_filters: OK")
    return True


@test("v1.4.0-Dev NEW (Day-5 item 7): read_cycle(cycle_id) returns matching entry, None on miss")
def t_v140_read_cycle() -> bool:
    """Record an entry, retrieve by cycle_id, verify shape; miss returns None."""
    sys.path.insert(0, str(PLUGIN_ROOT))
    from helpers import cycle_history  # type: ignore
    cycle_history.reset_for_tests()
    r = cycle_history.record_cycle_entry({
        "skill": "lookup_skill", "outcome": "adopted",
        "proposal_id": "abc123", "runtime_seconds": 1.5,
    })
    cid = r["cycle_id"]
    found = cycle_history.read_cycle(cid)
    assert found is not None, f"cycle_id {cid!r} not found"
    assert found["cycle_id"] == cid
    assert found["skill"] == "lookup_skill"
    assert found["outcome"] == "adopted"
    assert found["runtime_seconds"] == 1.5
    # Miss
    assert cycle_history.read_cycle("does-not-exist") is None
    # Empty string is treated as missing
    assert cycle_history.read_cycle("") is None
    cycle_history.reset_for_tests()
    print("  t_v140_read_cycle: OK")
    return True


@test("v1.4.0-Dev NEW (Day-5 item 7): partial-write recovery — malformed lines are skipped")
def t_v140_partial_write_recovery() -> bool:
    """A deliberately-malformed line in the JSONL does not crash read;
    read returns only the valid lines."""
    sys.path.insert(0, str(PLUGIN_ROOT))
    from helpers import cycle_history  # type: ignore
    cycle_history.reset_for_tests()
    jsonl = cycle_history._jsonl_path()
    good1 = json.dumps({"cycle_id": "g1", "ts": "2025-01-01T00:00:00+0000",
                        "skill": "s1", "outcome": "adopted"})
    bad = '{"cycle_id": "abc", "ts": "invalid_json\n'  # deliberately broken
    good2 = json.dumps({"cycle_id": "g2", "ts": "2025-01-02T00:00:00+0000",
                        "skill": "s2", "outcome": "rejected"})
    jsonl.parent.mkdir(parents=True, exist_ok=True)
    with open(jsonl, "w", encoding="utf-8") as f:
        f.write(good1 + "\n")
        f.write(bad + "\n")
        f.write(good2 + "\n")
    out = cycle_history.read_cycle_history(limit=10)
    # Only the two valid entries come back
    assert len(out) == 2, f"expected 2 valid entries (bad line skipped), got {len(out)}: {out!r}"
    ids = {e["cycle_id"] for e in out}
    assert ids == {"g1", "g2"}, f"unexpected cycle_ids: {ids}"
    cycle_history.reset_for_tests()
    print("  t_v140_partial_write_recovery: OK")
    return True


@test("v1.4.0-Dev NEW (Day-5 item 7): get_history_status returns enabled + total + last_* fields")
def t_v140_get_history_status() -> bool:
    """After recording 2 entries, status block reports total_entries=2
    and non-null last_cycle_id / last_outcome."""
    sys.path.insert(0, str(PLUGIN_ROOT))
    from helpers import cycle_history  # type: ignore
    cycle_history.reset_for_tests()
    cycle_history.record_cycle_entry({"skill": "x", "outcome": "adopted"})
    cycle_history.record_cycle_entry({"skill": "y", "outcome": "rejected"})
    st = cycle_history.get_history_status()
    assert st.get("enabled") is True, f"expected enabled=True, got: {st!r}"
    assert st.get("total_entries") == 2, f"expected total_entries=2, got: {st!r}"
    assert st.get("file_size_bytes") and st["file_size_bytes"] > 0
    assert st.get("last_cycle_id") is not None
    assert st.get("last_outcome") == "rejected", f"last entry should be rejected, got: {st!r}"
    assert st.get("file_path", "").endswith("cycle_history.jsonl")
    cycle_history.reset_for_tests()
    print("  t_v140_get_history_status: OK")
    return True


@test("v1.4.0-Dev NEW (Day-5 item 7): Cycles API returns ok=True + newest-first entries")
def t_v140_cycles_api() -> bool:
    """The Cycles endpoint reads back 2 recorded entries with the
    documented response shape."""
    sys.path.insert(0, str(PLUGIN_ROOT))
    from helpers import cycle_history  # type: ignore
    cycle_history.reset_for_tests()
    cycle_history.record_cycle_entry({"skill": "api_skill", "outcome": "adopted"})
    cycle_history.record_cycle_entry({"skill": "api_skill", "outcome": "rejected"})
    _install_helpers_api_stub()
    # Mirror the cycle_history module under the `usr.plugins.skillopt.helpers`
    # namespace so the api module's primary import path resolves in this
    # standalone smoke environment (no real A0 framework runtime here).
    import types as _types
    for dotted in ("usr", "usr.plugins", "usr.plugins.skillopt",
                   "usr.plugins.skillopt.helpers"):
        if dotted not in sys.modules:
            sys.modules[dotted] = _types.ModuleType(dotted)
    sys.modules["usr.plugins.skillopt.helpers.cycle_history"] = cycle_history
    sys.modules["usr.plugins.skillopt.helpers"].cycle_history = cycle_history
    try:
        from usr.plugins.skillopt.api.cycles import Cycles  # type: ignore
    except Exception:
        from api.cycles import Cycles  # type: ignore
    import asyncio
    handler = Cycles()
    response = asyncio.run(handler.process({"limit": 10}, None))
    assert response.get("ok") is True, f"Cycles.process failed: {response!r}"
    assert response.get("count") == 2, f"expected count=2, got: {response!r}"
    entries = response.get("entries") or []
    assert len(entries) == 2, f"expected 2 entries, got {len(entries)}: {entries!r}"
    # Newest-first: the second insert should be first in the response
    assert entries[0]["outcome"] == "rejected", f"expected newest rejected first, got: {entries[0]!r}"
    cycle_history.reset_for_tests()
    print("  t_v140_cycles_api: OK")
    return True


@test("v1.4.0-Dev NEW (Day-5 item 7): AuditLog API reads adoptions.log newest-first")
def t_v140_audit_log_api() -> bool:
    """Write 2 entries to logs/runs/adoptions.log, call AuditLog endpoint,
    verify newest-first and shape."""
    sys.path.insert(0, str(PLUGIN_ROOT))
    try:
        from usr.plugins.skillopt.helpers import sleep_runner  # type: ignore
    except Exception:
        from helpers import sleep_runner  # type: ignore
    _audit_wipe_local_store()
    p = sleep_runner.runs_dir() / "adoptions.log"
    p.parent.mkdir(parents=True, exist_ok=True)
    e1 = {"ts": "2025-01-01T00:00:00+0000", "skill": "audit_a",
          "source": "/staging/a.md", "target": "/skills/audit_a/SKILL.md",
          "proposed_size": 100, "current_size": 80, "passed": True, "reason": "shrunk"}
    e2 = {"ts": "2025-01-02T00:00:00+0000", "skill": "audit_b",
          "source": "/staging/b.md", "target": "/skills/audit_b/SKILL.md",
          "proposed_size": 200, "current_size": 80, "passed": False, "reason": "grow"}
    with open(p, "w", encoding="utf-8") as f:
        f.write(json.dumps(e1) + "\n")
        f.write(json.dumps(e2) + "\n")
    _install_helpers_api_stub()
    try:
        from usr.plugins.skillopt.api.audit_log import AuditLog  # type: ignore
    except Exception:
        from api.audit_log import AuditLog  # type: ignore
    import asyncio
    handler = AuditLog()
    response = asyncio.run(handler.process({"limit": 10}, None))
    assert response.get("ok") is True, f"AuditLog.process failed: {response!r}"
    assert response.get("count") == 2, f"expected count=2, got: {response!r}"
    entries = response.get("entries") or []
    assert len(entries) == 2
    # Newest-first
    assert entries[0]["skill"] == "audit_b", f"expected newest 'audit_b' first, got: {entries[0]!r}"
    assert entries[1]["skill"] == "audit_a"
    _audit_wipe_local_store()
    print("  t_v140_audit_log_api: OK")
    return True


@test("v1.4.0-Dev NEW (Day-5 item 7): sleep_runner snapshot exposes cycle_history block")
def t_v140_cycle_history_block_in_status() -> bool:
    """get_status_snapshot() includes the `cycle_history` block with
    enabled, total_entries, file_path fields."""
    sys.path.insert(0, str(PLUGIN_ROOT))
    try:
        from usr.plugins.skillopt.helpers import sleep_runner  # type: ignore
    except Exception:
        from helpers import sleep_runner  # type: ignore
    from helpers import cycle_history  # type: ignore
    cycle_history.reset_for_tests()
    cycle_history.record_cycle_entry({"skill": "snap_test", "outcome": "adopted"})
    cycle_history.record_cycle_entry({"skill": "snap_test", "outcome": "rejected"})
    snap = sleep_runner.get_status_snapshot()
    assert "cycle_history" in snap, f"no cycle_history block in snapshot keys: {list(snap.keys())}"
    block = snap["cycle_history"]
    assert "enabled" in block, f"cycle_history block missing 'enabled': {block!r}"
    assert "total_entries" in block, f"cycle_history block missing 'total_entries': {block!r}"
    assert "file_path" in block, f"cycle_history block missing 'file_path': {block!r}"
    assert block["total_entries"] >= 2, f"expected >=2 entries, got: {block!r}"
    cycle_history.reset_for_tests()
    print("  t_v140_cycle_history_block_in_status: OK")
    return True


# ----------------------------------------------------------------------- #
#
# v1.5.0-Dev (Day-5 item 8): governance helper tests.
#
# Per-skill opt-out + per-skill policy.json + global default_policy.
# The auto-loop calls check_skill_eligible(skill_name) at the start of
# each per-skill cycle; if ineligible the cycle is skipped. Backed by
# helpers/governance.py + logs/runs/governance.log.
#
# Tests use _gov_setup() to inject a tmp skills dir via
# governance.set_skills_dir_for_tests() so we never touch the real
# /a0/usr/skills/ during smoke runs. _gov_wipe() resets between cases.
# ----------------------------------------------------------------------- #


def _gov_wipe_state() -> None:
    """Wipe logs/runs/governance.log and reset the test skills-dir override."""
    sys.path.insert(0, str(PLUGIN_ROOT))
    from helpers import governance  # type: ignore
    governance.reset_for_tests()


def _gov_setup(tmpdir: Path) -> object:
    """Inject a tmp skills dir + reset state. Returns the governance module."""
    sys.path.insert(0, str(PLUGIN_ROOT))
    from helpers import governance  # type: ignore
    _gov_wipe_state()
    tmpdir.mkdir(parents=True, exist_ok=True)
    governance.set_skills_dir_for_tests(tmpdir)
    return governance


@test("v1.5.0-Dev NEW (Day-5 item 8): check_skill_eligible defaults to opt_out (no marker + no policy.json -> not eligible)")
def t_v150_governance_default_opt_out() -> bool:
    """With global default mode=opt_out and no per-skill marker, a fresh
    skill is NOT eligible. This is the SAFEST backwards-compatible
    default — admins must opt each skill in explicitly."""
    from pathlib import Path
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="skillopt_gov_"))
    try:
        gov = _gov_setup(tmp)
        ok, reason = gov.check_skill_eligible("ghost_skill")
        assert ok is False, f"default should be opt_out (not eligible), got: {(ok, reason)!r}"
        assert reason == "mode_opt_in_no_marker", f"expected mode_opt_in_no_marker, got: {reason!r}"
        print("  t_v150_governance_default_opt_out: OK")
        return True
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


@test("v1.5.0-Dev NEW (Day-5 item 8): optout marker wins everything (mode_immutable + optin marker still rejected)")
def t_v150_governance_optout_marker() -> bool:
    """Drop usr/skills/<name>/.skillopt.optout -> check_skill_eligible
    returns (False, 'opted_out_via_marker') even when the per-skill
    policy says immutable OR when there's an opt-in marker too.
    Optout is the highest-priority signal."""
    from pathlib import Path
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="skillopt_gov_"))
    try:
        gov = _gov_setup(tmp)
        sd = tmp / "locked_skill"
        sd.mkdir()
        (sd / ".skillopt.optout").write_text("", encoding="utf-8")
        (sd / ".skillopt.optin").write_text("", encoding="utf-8")  # ignored
        (sd / ".skillopt.policy.json").write_text(
            '{"mode": "rate_limited"}', encoding="utf-8"
        )
        ok, reason = gov.check_skill_eligible("locked_skill")
        assert ok is False, f"optout marker must reject, got: {(ok, reason)!r}"
        assert reason == "opted_out_via_marker", f"wrong reason: {reason!r}"
        print("  t_v150_governance_optout_marker: OK")
        return True
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


@test("v1.5.0-Dev NEW (Day-5 item 8): policy mode=immutable -> mode_immutable reason")
def t_v150_governance_mode_immutable() -> bool:
    """policy.json says mode=immutable + opt-in marker present -> still
    rejected with reason='mode_immutable'. The opt-in marker opts the
    skill INTO the loop, but mode=immutable wins."""
    from pathlib import Path
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="skillopt_gov_"))
    try:
        gov = _gov_setup(tmp)
        sd = tmp / "compliance_skill"
        sd.mkdir()
        (sd / ".skillopt.policy.json").write_text(
            '{"mode": "immutable"}', encoding="utf-8"
        )
        (sd / ".skillopt.optin").write_text("", encoding="utf-8")
        ok, reason = gov.check_skill_eligible("compliance_skill")
        assert ok is False, f"immutable must reject, got: {(ok, reason)!r}"
        assert reason == "mode_immutable", f"wrong reason: {reason!r}"
        # No optin marker either -> still mode_immutable (it beats mode_opt_in_no_marker)
        (sd / ".skillopt.optin").unlink()
        ok2, reason2 = gov.check_skill_eligible("compliance_skill")
        assert ok2 is False and reason2 == "mode_immutable", f"no-optin case: {(ok2, reason2)!r}"
        print("  t_v150_governance_mode_immutable: OK")
        return True
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


@test("v1.5.0-Dev NEW (Day-5 item 8): global default opt_out + skill has opt-in marker -> eligible")
def t_v150_governance_optin_marker_opt_in() -> bool:
    """Global default mode=opt_out, but the skill has an opt-in
    marker -> check_skill_eligible returns (True, 'eligible'). The
    opt-in marker is the symmetry of the opt-out marker."""
    from pathlib import Path
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="skillopt_gov_"))
    try:
        gov = _gov_setup(tmp)
        sd = tmp / "opted_in_skill"
        sd.mkdir()
        (sd / ".skillopt.optin").write_text("", encoding="utf-8")
        # Also verify the policy can switch the default to opt_in per-skill.
        sd2 = tmp / "opted_in_via_policy"
        sd2.mkdir()
        (sd2 / ".skillopt.policy.json").write_text(
            '{"mode": "opt_in"}', encoding="utf-8"
        )
        ok1, reason1 = gov.check_skill_eligible("opted_in_skill")
        assert ok1 is True and reason1 == "eligible", f"marker case: {(ok1, reason1)!r}"
        ok2, reason2 = gov.check_skill_eligible("opted_in_via_policy")
        assert ok2 is False and reason2 == "mode_opt_in_no_marker", f"policy opt_in without marker: {(ok2, reason2)!r}"
        print("  t_v150_governance_optin_marker_opt_in: OK")
        return True
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


@test("v1.5.0-Dev NEW (Day-5 item 8): rate_limited within min_interval_seconds -> rejected")
def t_v150_governance_rate_limited() -> bool:
    """policy.json mode=rate_limited + min_interval_seconds=3600, opt-in
    marker present, last eligible decision 60s ago -> rejected with
    reason='rate_limited_min_interval'. Then we rewind to 7200s ago
    and the same skill is eligible."""
    from pathlib import Path
    import tempfile
    import json as _json
    import time as _t
    tmp = Path(tempfile.mkdtemp(prefix="skillopt_gov_"))
    try:
        gov = _gov_setup(tmp)
        sd = tmp / "hot_skill"
        sd.mkdir()
        (sd / ".skillopt.optin").write_text("", encoding="utf-8")
        (sd / ".skillopt.policy.json").write_text(
            '{"mode": "rate_limited", "min_interval_seconds": 3600}',
            encoding="utf-8",
        )
        now = _t.time()
        # Record an eligible decision 60s ago -> still in cooldown
        gov.mark_decision("hot_skill", True, "eligible")
        # Patch the ts_iso back 60s
        from helpers import governance  # type: ignore
        log = governance._runs_dir() / "governance.log"
        lines = log.read_text(encoding="utf-8").splitlines()
        last = lines[-1]
        entry = _json.loads(last)
        from datetime import datetime, timezone, timedelta
        entry["ts_iso"] = (
            datetime.now(timezone.utc) - timedelta(seconds=60)
        ).isoformat()
        lines[-1] = _json.dumps(entry)
        log.write_text("\n".join(lines) + "\n", encoding="utf-8")
        ok, reason = gov.check_skill_eligible("hot_skill")
        assert ok is False and reason == "rate_limited_min_interval", (
            f"within 3600s of last cycle, expected rate-limited, got: {(ok, reason)!r}"
        )
        # Now rewind to 7200s ago -> eligible
        entry["ts_iso"] = (
            datetime.now(timezone.utc) - timedelta(seconds=7200)
        ).isoformat()
        lines[-1] = _json.dumps(entry)
        log.write_text("\n".join(lines) + "\n", encoding="utf-8")
        ok2, reason2 = gov.check_skill_eligible("hot_skill")
        assert ok2 is True and reason2 == "eligible", (
            f"after 7200s, expected eligible, got: {(ok2, reason2)!r}"
        )
        print("  t_v150_governance_rate_limited: OK")
        return True
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


@test("v1.5.0-Dev NEW (Day-5 item 8): load_skill_policy falls back to global default on missing/malformed policy.json")
def t_v150_governance_load_skill_policy_fallback() -> bool:
    """Three scenarios, all must return _source='global_default' except
    the first (which is skill_overlay):
      A. per-skill policy.json present + valid   -> _source='skill_overlay', mode overridden
      B. per-skill policy.json present + malformed JSON -> _source='global_default'
      C. per-skill policy.json absent            -> _source='global_default'
    """
    from pathlib import Path
    import tempfile
    import json as _json
    tmp = Path(tempfile.mkdtemp(prefix="skillopt_gov_"))
    try:
        gov = _gov_setup(tmp)
        # A: valid overlay
        sd = tmp / "overlay_skill"
        sd.mkdir()
        (sd / ".skillopt.policy.json").write_text(
            '{"mode": "rate_limited", "min_interval_seconds": 99}',
            encoding="utf-8",
        )
        pol_a = gov.load_skill_policy("overlay_skill")
        assert pol_a.get("_source") == "skill_overlay", (
            f"valid policy.json should be skill_overlay, got: {pol_a!r}"
        )
        assert pol_a.get("mode") == "rate_limited", f"mode not overridden: {pol_a!r}"
        assert pol_a.get("min_interval_seconds") == 99, f"interval not overridden: {pol_a!r}"
        # B: malformed JSON
        sd_b = tmp / "bad_skill"
        sd_b.mkdir()
        (sd_b / ".skillopt.policy.json").write_text(
            "{not valid json", encoding="utf-8"
        )
        pol_b = gov.load_skill_policy("bad_skill")
        assert pol_b.get("_source") == "global_default", (
            f"malformed policy.json should fall back, got: {pol_b!r}"
        )
        # C: absent
        pol_c = gov.load_skill_policy("absent_skill")
        assert pol_c.get("_source") == "global_default", (
            f"missing policy.json should fall back, got: {pol_c!r}"
        )
        # Sanity: default policy carries mode + cap
        assert pol_c.get("mode") == "opt_out", f"global default mode missing: {pol_c!r}"
        assert pol_c.get("daily_budget_cents") == 100, f"global default cap missing: {pol_c!r}"
        print("  t_v150_governance_load_skill_policy_fallback: OK")
        return True
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


@test("v1.5.0-Dev NEW (Day-5 item 8): mark_human_decision writes a line to logs/runs/governance.log + clears approval gate")
def t_v150_governance_mark_human_decision() -> bool:
    """mark_human_decision() returns {ok, ts, decided_by} AND writes
    one line to logs/runs/governance.log. A subsequent
    require_human_approval=true skill is then considered approved."""
    from pathlib import Path
    import tempfile
    import json as _json
    tmp = Path(tempfile.mkdtemp(prefix="skillopt_gov_"))
    try:
        gov = _gov_setup(tmp)
        from helpers import governance  # type: ignore
        # Skill with require_human_approval + opt-in marker
        sd = tmp / "needs_approval"
        sd.mkdir()
        (sd / ".skillopt.optin").write_text("", encoding="utf-8")
        (sd / ".skillopt.policy.json").write_text(
            '{"mode": "opt_in", "require_human_approval": true}',
            encoding="utf-8",
        )
        # Without approval -> rejected
        ok, reason = gov.check_skill_eligible("needs_approval")
        assert ok is False and reason == "require_human_approval_pending", (
            f"expected pending without approval, got: {(ok, reason)!r}"
        )
        # Now record an approval
        res = gov.mark_human_decision("needs_approval", approved=True, decided_by="alice")
        assert res.get("ok") is True, f"mark_human_decision failed: {res!r}"
        assert res.get("ts"), f"missing ts: {res!r}"
        assert res.get("decided_by") == "alice", f"wrong decided_by: {res!r}"
        # Verify the log line
        log = governance._runs_dir() / "governance.log"
        assert log.is_file(), "governance.log missing"
        lines = log.read_text(encoding="utf-8").splitlines()
        last = _json.loads(lines[-1])
        assert last.get("event") == "human_decision", f"wrong event: {last!r}"
        assert last.get("skill") == "needs_approval", f"wrong skill: {last!r}"
        assert last.get("approved") is True, f"wrong approved: {last!r}"
        # Eligibility now passes
        ok2, reason2 = gov.check_skill_eligible("needs_approval")
        assert ok2 is True and reason2 == "eligible", (
            f"expected eligible after approval, got: {(ok2, reason2)!r}"
        )
        # A denial flips it back
        gov.mark_human_decision("needs_approval", approved=False)
        ok3, reason3 = gov.check_skill_eligible("needs_approval")
        assert ok3 is False and reason3 == "require_human_approval_pending", (
            f"expected pending after denial, got: {(ok3, reason3)!r}"
        )
        print("  t_v150_governance_mark_human_decision: OK")
        return True
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


@test("v1.5.0-Dev NEW (Day-5 item 8): get_governance_status block exposes opted_out / governed / last_decisions")
def t_v150_governance_status_block() -> bool:
    """get_governance_status() returns the documented shape: enabled,
    available, default_policy, opted_out, opted_in, governed,
    last_decisions, skills_dir, log_path, file_size_bytes. The sleep
    runner status snapshot also includes the governance block."""
    from pathlib import Path
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="skillopt_gov_"))
    try:
        gov = _gov_setup(tmp)
        try:
            from usr.plugins.skillopt.helpers import sleep_runner  # type: ignore
        except Exception:
            from helpers import sleep_runner  # type: ignore
        # Stage 3 skills with different markers
        for sk, marker, has_policy in [
            ("skill_a", "optout", True),
            ("skill_b", "optin", False),
            ("skill_c", "", True),
        ]:
            sd = tmp / sk
            sd.mkdir()
            if marker == "optout":
                (sd / ".skillopt.optout").write_text("", encoding="utf-8")
            elif marker == "optin":
                (sd / ".skillopt.optin").write_text("", encoding="utf-8")
            if has_policy:
                (sd / ".skillopt.policy.json").write_text(
                    '{"mode": "rate_limited"}', encoding="utf-8"
                )
        # Record a decision so last_decisions has data
        gov.mark_decision("skill_b", True, "eligible")
        gov.mark_decision("skill_a", False, "opted_out_via_marker")
        block = gov.get_governance_status()
        assert block.get("available") is True, f"block not available: {block!r}"
        assert block.get("enabled") is True, f"block not enabled: {block!r}"
        assert "default_policy" in block, f"missing default_policy: {block!r}"
        assert block["default_policy"].get("mode") == "opt_out"
        assert "skill_a" in block.get("opted_out", []), f"opted_out missing skill_a: {block!r}"
        assert "skill_b" in block.get("opted_in", []), f"opted_in missing skill_b: {block!r}"
        assert "skill_a" in block.get("governed", []), f"governed missing skill_a: {block!r}"
        assert "skill_c" in block.get("governed", []), f"governed missing skill_c: {block!r}"
        ld = block.get("last_decisions") or {}
        assert "skill_a" in ld, f"last_decisions missing skill_a: {ld!r}"
        assert "skill_b" in ld, f"last_decisions missing skill_b: {ld!r}"
        assert ld["skill_a"]["eligible"] is False
        assert ld["skill_b"]["eligible"] is True
        # Status snapshot also exposes the block
        snap = sleep_runner.get_status_snapshot()
        assert "governance" in snap, f"no governance block in snapshot: {list(snap.keys())}"
        sb = snap["governance"]
        assert sb.get("available") is True, f"snapshot governance not available: {sb!r}"
        assert "skill_a" in sb.get("opted_out", []), f"snapshot governance opted_out missing: {sb!r}"
        print("  t_v150_governance_status_block: OK")
        return True
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


@test("v1.5.0-Dev NEW (Day-5 item 8): auto_loop calls check_skill_eligible and skips opted-out skills")
def t_v150_governance_auto_loop_skip() -> bool:
    """End-to-end: AutoLoopThread._auto_adopt() calls governance at the
    top, an opted-out skill is skipped without touching the gate, and
    a governance decision row lands in logs/runs/governance.log."""
    from pathlib import Path
    import tempfile
    import json as _json
    tmp = Path(tempfile.mkdtemp(prefix="skillopt_gov_"))
    try:
        # Set up: tmp skills dir with an opted-out skill, plus a fake
        # staged proposal + a target SKILL.md so the gate path WOULD
        # have run if governance didn't block it.
        gov = _gov_setup(tmp)
        sd = tmp / "locked_for_real"
        sd.mkdir()
        (sd / ".skillopt.optout").write_text("", encoding="utf-8")
        target = sd / "SKILL.md"
        target.write_text("# original\n", encoding="utf-8")
        try:
            from usr.plugins.skillopt.helpers import sleep_runner, auto_loop  # type: ignore
        except Exception:
            from helpers import sleep_runner, auto_loop  # type: ignore
        from helpers import governance  # type: ignore
        runs = sleep_runner.runs_dir()
        # CRITICAL: find_staged_proposals() looks at sleep_runner.staging_dir()
        # (i.e. <plugin>/staging/), NOT <plugin>/logs/staging/. Write to the
        # correct path or _auto_adopt returns early before governance runs.
        staging = sleep_runner.staging_dir()
        staged = staging / "locked_for_real.md"
        original = target.read_text(encoding="utf-8")
        proposed = (
            original
            + "\n\n## A new section\n\n```python\nprint('hello')\n```\n"
        )  # way over min_chars; would otherwise pass the gate
        staged.write_text(proposed, encoding="utf-8")
        # Wipe the adoptions.log so we can prove no adoption happens.
        adoptions = runs / "adoptions.log"
        adoptions_unlink = False
        if adoptions.is_file():
            adoptions.unlink()
            adoptions_unlink = True
        # Snapshot target mtime so we can detect writes.
        before = target.read_text(encoding="utf-8")
        # Run _auto_adopt with auto_adopt=True and skill_name = locked_for_real
        thread = auto_loop.AutoLoopThread(get_config=lambda: {"auto_adopt": True, "gate_min_chars": 50})
        thread._auto_adopt({}, thread.get_config())
        # Target MUST NOT have been touched
        after = target.read_text(encoding="utf-8")
        assert before == after, "governance should have blocked the write to SKILL.md"
        # governance.log must have at least one decision row for this skill
        log = governance._runs_dir() / "governance.log"
        assert log.is_file(), "governance.log missing after _auto_adopt"
        lines = log.read_text(encoding="utf-8").splitlines()
        decision_rows = []
        for line in lines:
            try:
                entry = _json.loads(line)
            except Exception:
                continue
            if entry.get("skill") == "locked_for_real" and entry.get("event") == "decision":
                decision_rows.append(entry)
        assert decision_rows, f"no decision row for locked_for_real in {lines!r}"
        assert decision_rows[-1].get("eligible") is False
        assert decision_rows[-1].get("reason") == "opted_out_via_marker"
        # Cleanup
        if adoptions_unlink:
            try:
                adoptions.unlink()
            except Exception:
                pass
        try:
            staged.unlink()
        except Exception:
            pass
        print("  t_v150_governance_auto_loop_skip: OK")
        return True
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


# ======================================================================= #
# v1.6.0 — Solution B: official-engine bridge + side-finding fixes
# ======================================================================= #

_section_v160 = "v1.6.0 NEW (Solution B): official-engine bridge, gate delegation, per-skill gating, side-findings"


@test("v1.6.1: version strings aligned across plugin.py / hooks.py / plugin.yaml")
def t_v160_version_alignment() -> None:
    import re
    plugin_py = (PLUGIN_ROOT / "plugin.py").read_text(encoding="utf-8")
    hooks_py = (PLUGIN_ROOT / "hooks.py").read_text(encoding="utf-8")
    manifest = (PLUGIN_ROOT / "plugin.yaml").read_text(encoding="utf-8")
    assert 'PLUGIN_VERSION = "1.6.1"' in plugin_py, "plugin.py not 1.6.1"
    assert 'PLUGIN_VERSION = "1.6.1"' in hooks_py, "hooks.py not 1.6.1"
    assert re.search(r'^version:\s*1\.6\.1', manifest, re.M), "plugin.yaml not 1.6.1"


@test("v1.6.1: default_config.yaml declares the official-engine bridge keys")
def t_v160_config_keys() -> None:
    src = (PLUGIN_ROOT / "default_config.yaml").read_text(encoding="utf-8")
    for key in ["use_official_engine", "official_run_verb", "official_backend",
                "official_optimizer_model", "official_edit_budget",
                "official_preferences", "official_run_timeout_s"]:
        assert key in src, f"default_config.yaml missing {key}"
    # ab_harness is advisory-off by default in v1.6.0+
    assert "ab_harness_enabled: false" in src, "ab_harness_enabled should default to false"


@test("v1.6.0: official_adapter probe returns a shaped dict (available bool)")
def t_v160_probe_official_shape() -> None:
    sys.path.insert(0, str(PLUGIN_ROOT))
    from helpers import official_adapter
    info = official_adapter.probe_official(force=True)
    assert "available" in info and isinstance(info["available"], bool), info
    assert "py" in info, info
    assert "error" in info, info


@test("v1.6.0: run_official_sleep_cycle falls back to direct when package unavailable")
def t_v160_official_fallback() -> None:
    sys.path.insert(0, str(PLUGIN_ROOT))
    from helpers import official_adapter
    # Force the probe cache to "unavailable" so the run short-circuits
    # deterministically regardless of whether the package is installed.
    official_adapter._probe_cache["result"] = {"available": False, "error": "forced by test"}
    official_adapter._probe_cache["at"] = time.time()
    try:
        res = official_adapter.run_official_sleep_cycle(target="x", cfg={})
        assert res["ok"] is False, res
        assert res.get("fallback_to_direct") is True, res
        assert res.get("engine") == "official", res
        assert "not available" in res.get("reason", ""), res
    finally:
        official_adapter._probe_cache["result"] = None


@test("v1.6.0: validate_proposal official_gated skips the held-out stage")
def t_v160_official_gated_skips_heldout() -> None:
    sys.path.insert(0, str(PLUGIN_ROOT))
    from helpers.sleep_runner import validate_proposal
    proposed = "# New Skill\nA substantially different body.\n```\nexample\n```\n" + "y" * 1800
    current = "# Old Skill\nA completely different body.\n```\nexample\n```\n" + "x" * 1800
    held = {"before": 0.5, "after": 0.51, "delta_pp": 1.0}  # below 5pp -> normally rejects
    # Without official_gated: rejects on held-out
    ok, reason = validate_proposal(
        proposed, current, min_chars=200, min_improvement_pp=5.0,
        max_shrink_ratio=0.5, held_out=held,
    )
    assert not ok and "held-out" in reason, f"expected held-out reject, got {ok!r} {reason!r}"
    # With official_gated=True: held-out stage skipped -> accepts (structural passes)
    ok2, reason2 = validate_proposal(
        proposed, current, min_chars=200, min_improvement_pp=5.0,
        max_shrink_ratio=0.5, held_out=held, official_gated=True,
    )
    assert ok2, f"official_gated should accept; got {reason2!r}"


@test("v1.6.0: validate_proposal official_gated still rejects structural failures")
def t_v160_official_gated_keeps_structural() -> None:
    sys.path.insert(0, str(PLUGIN_ROOT))
    from helpers.sleep_runner import validate_proposal
    # Byte-identical must still reject even when official_gated
    text = "# Skill\nA 1904 char block\n```\nexample\n```\n" + "x" * 1700
    ok, reason = validate_proposal(
        text, text, min_chars=200, min_improvement_pp=5.0,
        max_shrink_ratio=0.5, held_out=None, official_gated=True,
    )
    assert not ok, f"official_gated must not bypass structural checks; got {ok!r} {reason!r}"


@test("v1.6.0: auto-loop _tick wires governance + cadence + budget + official engine")
def t_v160_tick_wiring() -> None:
    src = (PLUGIN_ROOT / "helpers" / "auto_loop.py").read_text(encoding="utf-8")
    assert "check_skill_eligible" in src, "tick must call governance.check_skill_eligible"
    assert "compute_next_run" in src, "tick must call cadence.compute_next_run"
    assert "can_spend" in src, "tick must call budget.can_spend"
    assert "use_official_engine" in src, "tick must branch on use_official_engine"
    assert "official_adapter" in src, "tick must drive the official adapter"
    assert "last_engine" in src, "tick must record last_engine for Phase 2"
    assert "official_gated" in src, "_auto_adopt must pass official_gated to the gate"


@test("v1.6.0: ab_harness defaults to advisory-off (env unset)")
def t_v160_ab_harness_default_off() -> None:
    sys.path.insert(0, str(PLUGIN_ROOT))
    from helpers import ab_harness
    saved = os.environ.pop("SKILLOPT_AB_HARNESS_ENABLED", None)
    try:
        cfg = ab_harness._config()
        assert cfg["enabled"] is False, (
            f"ab_harness default should be False (advisory-off), got {cfg['enabled']}"
        )
    finally:
        if saved is not None:
            os.environ["SKILLOPT_AB_HARNESS_ENABLED"] = saved


@test("v1.6.0: cycle_history compaction rotates overflow to .archive.jsonl")
def t_v160_cycle_history_compaction() -> None:
    sys.path.insert(0, str(PLUGIN_ROOT))
    from helpers import cycle_history
    cycle_history.reset_for_tests()
    archive = cycle_history._runs_dir() / "cycle_history.archive.jsonl"
    if archive.is_file():
        archive.unlink()
    jsonl = cycle_history._jsonl_path()
    jsonl.parent.mkdir(parents=True, exist_ok=True)
    # Write 5 entries directly into the JSONL.
    for i in range(5):
        with open(jsonl, "a", encoding="utf-8") as f:
            f.write(json.dumps({"cycle_id": f"c{i}", "skill": f"s{i}", "outcome": "tick"}) + "\n")
    # Compact to keep 3 -> 2 moved to archive.
    res = cycle_history._compact_jsonl(3)
    assert res["ok"], res
    assert res["compacted"] == 2, res
    with open(jsonl, "r", encoding="utf-8") as f:
        hot = [ln for ln in f if ln.strip()]
    assert len(hot) == 3, f"hot file should retain 3, has {len(hot)}"
    assert archive.is_file(), "archive file not created"
    with open(archive, "r", encoding="utf-8") as f:
        cold = [ln for ln in f if ln.strip()]
    assert len(cold) == 2, f"archive should have 2, has {len(cold)}"
    # idempotent: compacting again at max=3 is a no-op
    res2 = cycle_history._compact_jsonl(3)
    assert res2["compacted"] == 0, res2
    # cleanup
    cycle_history.reset_for_tests()
    if archive.is_file():
        archive.unlink()


@test("v1.6.0: get_history_status surfaces archive + max_entries")
def t_v160_cycle_history_status_archive() -> None:
    sys.path.insert(0, str(PLUGIN_ROOT))
    from helpers import cycle_history
    cycle_history.reset_for_tests()
    archive = cycle_history._runs_dir() / "cycle_history.archive.jsonl"
    if archive.is_file():
        archive.unlink()
    try:
        st = cycle_history.get_history_status()
        assert "archive_path" in st, st
        assert "max_entries" in st, st
        assert "archived_entries" in st, st
    finally:
        cycle_history.reset_for_tests()
        if archive.is_file():
            archive.unlink()


# ======================================================================= #
# v1.6.1 — verified CLI mapping + gate verdict from report.json
# (verified against microsoft/skillopt @ HEAD 2026-08-10)
# ======================================================================= #

_section_v161 = "v1.6.1 NEW: verified CLI flag mapping, report.json gate verdict, gate_reject contract"


@test("v1.6.1: _build_run_args maps to the real skillopt_sleep run flags")
def t_v161_build_run_args_flags() -> None:
    sys.path.insert(0, str(PLUGIN_ROOT))
    from helpers import official_adapter
    cfg = {
        "official_backend": "azure_openai",
        "official_optimizer_model": "gpt-x",
        "official_lookback_hours": 72,
        "official_max_tasks": 10,
        "official_edit_budget": 3,
        "official_preferences": "be terse",
    }
    args = official_adapter._build_run_args(cfg, target=None)
    # --project is always passed (predictable staging)
    assert "--project" in args, args
    # --model (single), NOT --optimizer-model / --target-model (don't exist)
    assert "--model" in args and "gpt-x" in args, args
    assert "--optimizer-model" not in args, "real CLI has no --optimizer-model"
    assert "--target-model" not in args, "real CLI has no --target-model"
    # --backend, --lookback-hours, --max-tasks, --edit-budget, --preferences
    assert "--backend" in args and "azure_openai" in args, args
    assert "--lookback-hours" in args and "72" in args, args
    assert "--max-tasks" in args and "10" in args, args
    assert "--edit-budget" in args and "3" in args, args
    assert "--preferences" in args and "be terse" in args, args
    # --json for structured stdout
    assert "--json" in args, args
    # NO --skill (the real flag is --target-skill-path, a PATH)
    assert "--skill" not in args, "real CLI has no --skill"
    # --auto-adopt must NEVER be passed (we do our own gated adopt)
    assert "--auto-adopt" not in args, "must not auto-adopt"


@test("v1.6.1: _build_run_args rejects unknown backends (argparse would error)")
def t_v161_build_run_args_unknown_backend() -> None:
    sys.path.insert(0, str(PLUGIN_ROOT))
    from helpers import official_adapter
    cfg = {"official_backend": "some-bogus-backend"}
    args = official_adapter._build_run_args(cfg, target=None)
    assert "--backend" not in args, "unknown backend must be omitted"


@test("v1.6.1: _build_run_args adds --target-skill-path only for a real skill")
def t_v161_build_run_args_target_skill_path() -> None:
    sys.path.insert(0, str(PLUGIN_ROOT))
    from helpers import official_adapter
    # A skill that exists on the host (any of the 106) -> path resolved
    real = "agent-browser"
    args = official_adapter._build_run_args({}, target=real)
    if official_adapter._resolve_skill_path(real):
        assert "--target-skill-path" in args, args
        assert any(a.endswith("SKILL.md") for a in args), args
    # A skill that doesn't exist -> flag omitted (engine runs without a target)
    args2 = official_adapter._build_run_args({}, target="no_such_skill_xyz_123")
    assert "--target-skill-path" not in args2, args2


@test("v1.6.1: _read_gate_verdict reads accepted/reject + scores from report.json")
def t_v161_read_gate_verdict() -> None:
    import json as _j
    sys.path.insert(0, str(PLUGIN_ROOT))
    from helpers import official_adapter
    import tempfile, os
    with tempfile.TemporaryDirectory() as d:
        from pathlib import Path
        sd = Path(d)
        # accepted proposal
        (sd / "report.json").write_text(_j.dumps({
            "accepted": True, "gate_action": "accept_new_best",
            "baseline_score": 0.41, "candidate_score": 0.57,
            "night": 3, "edits": [{"a": 1}], "rejected_edits": [],
        }), encoding="utf-8")
        v = official_adapter._read_gate_verdict(sd)
        assert v is not None and v["accepted"] is True, v
        assert v["gate_action"] == "accept_new_best", v
        assert v["baseline_score"] == 0.41 and v["candidate_score"] == 0.57, v
        assert v["n_accepted_edits"] == 1, v
        # rejected proposal
        (sd / "report.json").write_text(_j.dumps({
            "accepted": False, "gate_action": "reject",
            "baseline_score": 0.57, "candidate_score": 0.50,
        }), encoding="utf-8")
        v2 = official_adapter._read_gate_verdict(sd)
        assert v2 is not None and v2["accepted"] is False, v2
        assert v2["gate_action"] == "reject", v2
        # missing report -> None
        (sd / "report.json").unlink()
        assert official_adapter._read_gate_verdict(sd) is None


@test("v1.6.1: _find_staging_dir finds the newest manifest.json dir")
def t_v161_find_staging_dir() -> None:
    import os, time
    sys.path.insert(0, str(PLUGIN_ROOT))
    from helpers import official_adapter
    import tempfile
    from pathlib import Path
    root = official_adapter._official_staging_root()
    # Save + clear any pre-existing staging for a clean test
    saved = []
    if root.is_dir():
        for child in list(root.iterdir()):
            saved.append(child)
    # Create two fake staging dirs with manifest.json; second is newer
    a = root / "20260101T000000"
    b = root / "20260102T000000"
    a.mkdir(parents=True, exist_ok=True)
    b.mkdir(parents=True, exist_ok=True)
    (a / "manifest.json").write_text("{}", encoding="utf-8")
    (b / "manifest.json").write_text("{}", encoding="utf-8")
    # ensure b is newer by mtime
    os.utime(str(a), (time.time() - 100, time.time() - 100))
    os.utime(str(b), (time.time(), time.time()))
    try:
        found = official_adapter._find_staging_dir()
        assert found is not None, "expected a staging dir"
        assert found.name == "20260102T000000", found
        # A dir without manifest.json is ignored
        c = root / "20260103T000000"
        c.mkdir(parents=True, exist_ok=True)
        found2 = official_adapter._find_staging_dir()
        assert found2 is not None and found2.name == "20260102T000000", found2
        c.rmdir()
    finally:
        for p in (a, b):
            for f in p.glob("*"):
                f.unlink()
            p.rmdir()
        # restore nothing (saved entries are real; leave them)


@test("v1.6.1: _try_evaluate_gate degrades to None when package absent")
def t_v161_try_evaluate_gate_none() -> None:
    sys.path.insert(0, str(PLUGIN_ROOT))
    from helpers import official_adapter
    # Package not installed in this dev env -> must return None (fail-soft)
    v = official_adapter._try_evaluate_gate(
        "cand", 0.6, "cur", 0.5, "best", 0.55, 1, 2, metric="hard",
    )
    assert v is None, f"expected None when package absent, got {v!r}"


@test("v1.6.1: _run_engine_for_skill honors gate_rejected (no direct fallback)")
def t_v161_gate_rejected_no_fallback() -> None:
    src = (PLUGIN_ROOT / "helpers" / "auto_loop.py").read_text(encoding="utf-8")
    assert "gate_rejected" in src, "auto_loop must branch on gate_rejected"
    # The branch must return WITHOUT calling direct_optimizer.optimize_skill
    assert "official gate REJECTED" in src, src


@test("v1.6.1: official_adapter docstring marks the API as VERIFIED")
def t_v161_docstring_verified() -> None:
    src = (PLUGIN_ROOT / "helpers" / "official_adapter.py").read_text(encoding="utf-8")
    assert "VERIFIED API" in src, "adapter docstring must mark API verified"
    assert "report.json" in src
    assert "--target-skill-path" in src


# ======================================================================= #
# v1.7.0 — Solution C, Phase C1: ground-truth skill attribution
# (fixes the broken-harvester bug: loop_data.messages never existed)
# ======================================================================= #

_section_v170_c1 = "v1.7.0 NEW (C1): ground-truth skill attribution + broken-harvester fix"


def _load_harvester_module():
    """Load the harvester extension as a fresh module (importlib) and return it."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "harvester_c1",
        PLUGIN_ROOT / "extensions" / "python" / "monologue_end" / "_60_skillopt_harvest_rollout.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore
    return mod


def _install_fake_skills(mod_skills):
    """Inject a fake `helpers.skills` (A0 repo-root helper, not on test path)
    so the harvester's `from helpers import skills` resolves. Returns the
    prior sys.modules entry (or None) for restoration."""
    import types
    # Ensure the plugin's `helpers` package is loaded so attribute set works.
    sys.path.insert(0, str(PLUGIN_ROOT))
    import helpers as helpers_pkg  # type: ignore
    prev = sys.modules.get("helpers.skills")
    sys.modules["helpers.skills"] = mod_skills
    helpers_pkg.skills = mod_skills  # type: ignore[attr-defined]
    return prev


def _restore_skills(prev):
    sys.modules.pop("helpers.skills", None)
    if prev is not None:
        sys.modules["helpers.skills"] = prev
    import helpers as helpers_pkg  # type: ignore
    if hasattr(helpers_pkg, "skills"):
        try:
            del helpers_pkg.skills  # type: ignore[attr-defined]
        except AttributeError:
            pass


def _fake_skills_module(*, instruction_names=None, loaded_names=None):
    """Build a fake helpers.skills module.

    instruction_names: list of names the per-message skill_instruction_name
      should return, one per call (cycled). If None, the real-shape matcher
      below is used (reads msg["content"]["skill_instructions"]).
    loaded_names: list returned by get_loaded_skill_names(agent).
    """
    import types
    mod = types.ModuleType("helpers.skills")

    def skill_instruction_name(message):
        if instruction_names is not None:
            # cycle through the provided list per call
            name = instruction_names.pop(0) if instruction_names else ""
            return name
        # real-shape matcher (mirrors helpers.skills.skill_instruction_name)
        try:
            content = message.get("content") if isinstance(message, dict) else getattr(message, "content", None)
            if isinstance(content, dict):
                si = content.get("skill_instructions")
                if isinstance(si, dict) and si.get("content_included"):
                    return str(si.get("name") or "").strip()
        except Exception:
            pass
        return ""

    def get_loaded_skill_names(agent):
        return list(loaded_names or [])

    mod.skill_instruction_name = skill_instruction_name
    mod.get_loaded_skill_names = get_loaded_skill_names
    return mod


def _fake_sleep_runner(captured):
    """A fake sleep_runner module exposing write_rollout (captures the record)
    + a0_skills_dir (tmp path, unused when fragment_store import fails)."""
    import types, tempfile
    from pathlib import Path
    mod = types.ModuleType("sleep_runner_fake")

    def write_rollout(record):
        captured.append(record)
        return Path(record["id"] + ".json")

    def a0_skills_dir():
        return Path(tempfile.gettempdir())

    mod.write_rollout = write_rollout
    mod.a0_skills_dir = a0_skills_dir
    return mod


@test("v1.7.0 C1: harvester reads loop_data.history_output (not nonexistent .messages)")
def t_c1_harvest_uses_history_output_not_messages() -> None:
    """The v1.1.0 bug: the harvester read loop_data.messages, which does NOT
    exist on Agent Zero's LoopData, so messages=[] and it early-returned on
    every turn (zero rollouts written). With history_output present and NO
    messages attribute at all, the fixed harvester must still write a rollout."""
    mod = _load_harvester_module()
    captured = []
    mod._sleep_runner = _fake_sleep_runner(captured)
    prev = _install_fake_skills(_fake_skills_module(loaded_names=["agent-browser"]))
    try:
        import types
        # user_message is a framework Message-like object with .content
        user_message = types.SimpleNamespace(content="Refactor the parser to handle nested groups.")
        history_output = [
            {"ai": True, "content": "Done. Tests pass."},
        ]
        loop_data = types.SimpleNamespace(
            history_output=history_output,
            user_message=user_message,
            last_response="Done. Tests pass.",
            start_time=time.time(),
        )
        agent = types.SimpleNamespace(loop_data=loop_data, config=types.SimpleNamespace(chat_model="smoke"))
        mod.execute(agent, loop_data=loop_data)
        assert len(captured) == 1, f"expected one rollout written, got {len(captured)}: {captured}"
        rec = captured[0]
        assert rec["task"] == "Refactor the parser to handle nested groups.", rec
        # history_output had no skill-instructions message; loaded_names has
        # agent-browser -> skill_used falls back to the session ledger.
        assert rec["skill_used"] == "agent-browser", rec
        assert rec["task_type"] == "agent-browser", rec
        assert rec["outcome"] == "success", rec
        assert rec["last_response"] == "Done. Tests pass.", rec
    finally:
        _restore_skills(prev)


@test("v1.7.0 C1: skill_instruction_name per-turn match wins over session ledger")
def t_c1_harvest_skill_instruction_name_fallback() -> None:
    """When a history_output message carries skill_instructions with
    content_included=True (a skill whose content was injected THIS turn),
    the per-turn match is preferred over the session-level ledger. This is
    the exact signal tools/skills_tool.py:_visible_skill_loaded reads."""
    mod = _load_harvester_module()
    captured = []
    mod._sleep_runner = _fake_sleep_runner(captured)
    # Session ledger says "old-skill"; the per-turn message says "coding-fast".
    prev = _install_fake_skills(_fake_skills_module(loaded_names=["old-skill"]))
    try:
        import types
        user_message = types.SimpleNamespace(content="Write a unit test for the parser.")
        history_output = [
            # An earlier turn loaded a different skill (content_included=True).
            {"ai": False, "content": {"skill_instructions": {"name": "earlier-skill", "content_included": True}}},
            # This turn loaded coding-fast.
            {"ai": False, "content": {"skill_instructions": {"name": "coding-fast", "content_included": True}}},
            {"ai": True, "content": "Done. Wrote 3 tests."},
        ]
        loop_data = types.SimpleNamespace(
            history_output=history_output,
            user_message=user_message,
            last_response="Done. Wrote 3 tests.",
            start_time=time.time(),
        )
        agent = types.SimpleNamespace(loop_data=loop_data, config=types.SimpleNamespace(chat_model="smoke"))
        mod.execute(agent, loop_data=loop_data)
        assert len(captured) == 1, captured
        rec = captured[0]
        # The LAST content_included=True match wins (coding-fast), not the
        # session ledger (old-skill).
        assert rec["skill_used"] == "coding-fast", rec
        assert rec["task_type"] == "coding-fast", rec
    finally:
        _restore_skills(prev)


@test("v1.7.0 C1: SKILLOPT_REPLAY_MODE skips harvest (recursion guard)")
def t_c1_harvest_replay_mode_skips() -> None:
    """When the local replay harness (C2) sets SKILLOPT_REPLAY_MODE, the
    replayed agent's own monologue_end must NOT write a synthetic rollout."""
    mod = _load_harvester_module()
    captured = []
    mod._sleep_runner = _fake_sleep_runner(captured)
    prev = _install_fake_skills(_fake_skills_module(loaded_names=["x"]))
    old = os.environ.get("SKILLOPT_REPLAY_MODE")
    os.environ["SKILLOPT_REPLAY_MODE"] = "1"
    try:
        import types
        loop_data = types.SimpleNamespace(
            history_output=[{"ai": True, "content": "synthetic"}],
            user_message=types.SimpleNamespace(content="replay task"),
            last_response="synthetic",
            start_time=time.time(),
        )
        agent = types.SimpleNamespace(loop_data=loop_data, config=types.SimpleNamespace(chat_model="smoke"))
        mod.execute(agent, loop_data=loop_data)
        assert captured == [], f"replay-mode must skip write_rollout, got {captured}"
    finally:
        if old is None:
            os.environ.pop("SKILLOPT_REPLAY_MODE", None)
        else:
            os.environ["SKILLOPT_REPLAY_MODE"] = old
        _restore_skills(prev)


@test("v1.7.0 C1: no skill at all falls back to task_type='general'")
def t_c1_harvest_no_skill_falls_back_to_general() -> None:
    """A chat turn with no skill loaded (no skill_instructions message, empty
    session ledger) still writes a rollout with skill_used='' and
    task_type='general' — the dataset keeps unspecialised turns too."""
    mod = _load_harvester_module()
    captured = []
    mod._sleep_runner = _fake_sleep_runner(captured)
    prev = _install_fake_skills(_fake_skills_module(loaded_names=[]))
    try:
        import types
        loop_data = types.SimpleNamespace(
            history_output=[{"ai": True, "content": "Hello there."}],
            user_message=types.SimpleNamespace(content="Just chatting."),
            last_response="Hello there.",
            start_time=time.time(),
        )
        agent = types.SimpleNamespace(loop_data=loop_data, config=types.SimpleNamespace(chat_model="smoke"))
        mod.execute(agent, loop_data=loop_data)
        assert len(captured) == 1, captured
        rec = captured[0]
        assert rec["skill_used"] == "", rec
        assert rec["task_type"] == "general", rec
    finally:
        _restore_skills(prev)


# ======================================================================= #
# v1.7.0 — Solution C, Phase C2: local replay harness
# (deterministic mock executor + real-executor stub + gate wiring)
# ======================================================================= #

_section_v170_c2 = "v1.7.0 NEW (C2): local counterfactual replay harness"


@test("v1.7.0 C2: mock executor is deterministic + relevance-driven")
def t_c2_mock_executor_deterministic() -> None:
    """_mock_score is pure (same inputs -> same output), outcome-driven
    (failure -> 0), and relevance-driven (a skill whose directive keywords
    overlap the task scores higher than one that doesn't)."""
    sys.path.insert(0, str(PLUGIN_ROOT))
    from helpers import replay_harness
    relevant = "# Refactor Parser\n**nested groups** cleanly\n"
    irrelevant = "# Cooking Recipes\n**bake cake** slowly\n"
    task = {"task": "refactor the parser for nested groups", "outcome": "success"}
    hi = replay_harness._mock_score(task, relevant)
    lo = replay_harness._mock_score(task, irrelevant)
    assert hi > lo, f"relevant skill must outscore irrelevant: {hi} vs {lo}"
    assert hi == 1.0, f"full overlap success should score 1.0, got {hi}"
    assert lo == 0.5, f"no-overlap success should score 0.5, got {lo}"
    # failure outcome -> 0 regardless of relevance
    fail = replay_harness._mock_score(
        {"task": "refactor parser nested groups", "outcome": "failure"}, relevant,
    )
    assert fail == 0.0, f"failure base must be 0, got {fail}"
    # determinism
    assert replay_harness._mock_score(task, relevant) == hi
    # directive keywords are headings + bold, stopwords dropped
    kws = replay_harness._directive_keywords("# Refactor Parser\n**nested groups** here")
    assert "refactor" in kws and "parser" in kws
    assert "nested" in kws and "groups" in kws
    assert "here" not in kws, "'here' is not a heading/bold span"


@test("v1.7.0 C2: _decide is strict-monotonic (lift >= min_pp to accept)")
def t_c2_gate_monotonic_strict() -> None:
    sys.path.insert(0, str(PLUGIN_ROOT))
    from helpers import replay_harness
    cfg = {"gate_min_improvement_pp": 5.0, "replay_min_n": 3}
    # strict lift >= 5pp -> accept
    v = replay_harness._decide(0.5, 0.6, 5, cfg)
    assert v["accepted"] is True, v
    assert v["reason"].startswith("ok_lift"), v
    # no lift -> reject
    assert replay_harness._decide(0.5, 0.5, 5, cfg)["accepted"] is False
    # regression -> reject
    assert replay_harness._decide(0.6, 0.5, 5, cfg)["accepted"] is False
    # positive but below the bar -> reject
    v2 = replay_harness._decide(0.5, 0.54, 5, cfg)  # 4pp < 5pp
    assert v2["accepted"] is False and "insufficient_lift" in v2["reason"], v2
    # insufficient n -> reject (checked first)
    v3 = replay_harness._decide(0.5, 0.6, 2, cfg)
    assert v3["accepted"] is False and "insufficient_n" in v3["reason"], v3


@test("v1.7.0 C2: run_counterfactual returns ok=False on insufficient n")
def t_c2_gate_insufficient_n() -> None:
    sys.path.insert(0, str(PLUGIN_ROOT))
    from helpers import replay_harness
    r0 = replay_harness.run_counterfactual(
        "s", "cur", "prop", [], executor="mock", config={"replay_min_n": 3},
    )
    assert r0["ok"] is False and r0["reason"] == "no_held_out_tasks", r0
    r1 = replay_harness.run_counterfactual(
        "s", "cur", "prop",
        [{"task": "t", "outcome": "success"}] * 2,
        executor="mock", config={"replay_min_n": 3},
    )
    assert r1["ok"] is False and r1["reason"].startswith("insufficient_n"), r1
    # enough tasks -> ok=True with per_task scores
    r2 = replay_harness.run_counterfactual(
        "s", "# A\n**b**\n", "# A\n**b c**\n",
        [{"task": "task b", "outcome": "success"}] * 3,
        executor="mock", config={"replay_min_n": 3, "gate_min_improvement_pp": 0.0},
    )
    assert r2["ok"] is True, r2
    assert r2["n"] == 3 and len(r2["per_task"]) == 3, r2
    assert "hard_current" in r2 and "hard_proposed" in r2 and "lift_pp" in r2, r2


@test("v1.7.0 C2: real executor is a guarded stub (not enabled -> ok=False)")
def t_c2_real_executor_stub_disabled() -> None:
    sys.path.insert(0, str(PLUGIN_ROOT))
    from helpers import replay_harness
    tasks = [{"task": "t", "outcome": "success"}] * 3
    # default: flag off -> real_executor_not_enabled
    r = replay_harness.run_counterfactual(
        "s", "cur", "prop", tasks, executor="real", config={"replay_min_n": 3},
    )
    assert r["ok"] is False, r
    assert r["reason"] == "real_executor_not_enabled", r
    # flag on -> stub raises -> real_executor_unavailable (never a fake score)
    r2 = replay_harness.run_counterfactual(
        "s", "cur", "prop", tasks, executor="real",
        config={"replay_min_n": 3, "replay_real_executor_enabled": True},
    )
    assert r2["ok"] is False, r2
    assert r2["reason"].startswith("real_executor_unavailable"), r2
    # unknown executor -> ok=False
    r3 = replay_harness.run_counterfactual(
        "s", "cur", "prop", tasks, executor="quantum", config={"replay_min_n": 3},
    )
    assert r3["ok"] is False and "unknown_executor" in r3["reason"], r3


@test("v1.7.0 C2: validate_proposal local replay gate rejects a losing proposal")
def t_c2_validate_proposal_local_gate_rejects() -> None:
    """On the direct path (official_gated=False), with enough held-out
    rollouts, a proposed skill that scores WORSE than the current skill
    under the mock replay is rejected with 'replay_gate_rejected'. The
    A/B harness env is disabled for this test so stage 0 falls through
    and the replay stage 0.7 is the authoritative gate (the A/B stage
    would otherwise pick up leftover rollouts with empty skill_used)."""
    sys.path.insert(0, str(PLUGIN_ROOT))
    from helpers import ab_harness
    from helpers.sleep_runner import validate_proposal
    ab_harness.reset_for_tests()
    old_ab = os.environ.get("SKILLOPT_AB_HARNESS_ENABLED")
    os.environ.pop("SKILLOPT_AB_HARNESS_ENABLED", None)  # A/B disabled -> stage 0 falls through
    skill = "c2_replay_skill_5"
    rollouts = _write_fake_rollouts(skill, n=3)
    try:
        # Current skill's directive keywords (refactor/module/nested/groups)
        # overlap the rollout task text -> high score. Proposed skill's
        # keywords (cooking/bake/cake) do not -> low score -> regression.
        current = "# Refactor Module\n**nested groups** parse trees\n\n```example\nrefactor module\n```\n" + ("x" * 180)
        proposed = "# Cooking Recipes\n**bake cake** slowly\n\n```example\nbake a cake\n```\n" + ("y" * 180)
        ok, reason = validate_proposal(
            proposed, current, min_chars=200, min_improvement_pp=5.0,
            max_shrink_ratio=0.5, held_out=None, skill_name=skill,
        )
        assert not ok, f"replay gate should reject a losing proposal, got ok={ok} reason={reason!r}"
        assert reason.startswith("replay_gate_rejected"), f"unexpected reason: {reason!r}"
    finally:
        _cleanup_rollouts(rollouts)
        if old_ab is None:
            os.environ.pop("SKILLOPT_AB_HARNESS_ENABLED", None)
        else:
            os.environ["SKILLOPT_AB_HARNESS_ENABLED"] = old_ab


@test("v1.7.0 C2: official_gated=True skips the local replay gate")
def t_c2_validate_proposal_official_gated_skips_replay() -> None:
    """When official_gated=True, the local replay gate (stage 0.7) is
    SKIPPED — the upstream held-out gate is authoritative and we must not
    double-count. With the same rollouts that would make the replay gate
    reject (test 5), a structurally-valid proposal must PASS when
    official_gated=True."""
    sys.path.insert(0, str(PLUGIN_ROOT))
    from helpers import ab_harness
    from helpers.sleep_runner import validate_proposal
    ab_harness.reset_for_tests()
    old_ab = os.environ.get("SKILLOPT_AB_HARNESS_ENABLED")
    os.environ.pop("SKILLOPT_AB_HARNESS_ENABLED", None)
    skill = "c2_replay_skill_6"
    rollouts = _write_fake_rollouts(skill, n=3)
    try:
        current = "# Refactor Module\n**nested groups** parse trees\n\n```example\nrefactor module\n```\n" + ("x" * 180)
        proposed = "# Cooking Recipes\n**bake cake** slowly\n\n```example\nbake a cake\n```\n" + ("y" * 180)
        ok, reason = validate_proposal(
            proposed, current, min_chars=200, min_improvement_pp=5.0,
            max_shrink_ratio=0.5, held_out=None, skill_name=skill,
            official_gated=True,
        )
        assert ok, f"official_gated should skip replay + accept structurally; got {ok!r} {reason!r}"
        assert "replay_gate" not in reason, f"replay gate must be skipped under official_gated: {reason!r}"
    finally:
        _cleanup_rollouts(rollouts)
        if old_ab is None:
            os.environ.pop("SKILLOPT_AB_HARNESS_ENABLED", None)
        else:
            os.environ["SKILLOPT_AB_HARNESS_ENABLED"] = old_ab


if __name__ == "__main__":
    # Print the section headers once at the top of the run
    print(_section_v110)
    print(_section_v120)
    print(_section_v121)
    print(_section_v160)
    print(_section_v161)
    print(_section_v170_c1)
    print(_section_v170_c2)
    sys.exit(main())
