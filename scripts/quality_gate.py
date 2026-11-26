"""Deterministic VANTA release gate.

Runs test modules as isolated processes so audio, UI, network, or background
threads in one module cannot hang the complete quality report.  The default
suite covers the human-visible request lifecycle; --full includes every test
module.  No pytest dependency is required.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
from pathlib import Path
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"
REPORT = ROOT / "runtime" / "quality_gate.json"

CRITICAL = (
    "test_action_registry.py",
    "test_action_contract_matrix.py",
    "test_action_speech.py",
    "test_action_wiring.py",
    "test_action_visibility.py",
    "test_authentication_gate.py",
    "test_capability_equivalence.py",
    "test_capability_integrity.py",
    "test_cognitive_pipeline.py",
    "test_conversation_output.py",
    "test_conversation_runtime.py",
    "test_evidence_quality.py",
    "test_identity_gate_ui.py",
    "test_interface.py",
    "test_listening_and_rest.py",
    "test_modes.py",
    "test_proactivity_policy.py",
    "test_quiet.py",
    "test_release_scenarios.py",
    "test_rest_wakes_on_voice.py",
    "test_result_presenter.py",
    "test_router_resilience.py",
    "test_speech_stream.py",
    "test_sleep_screen.py",
    "test_task_concurrency.py",
    "test_tool_syntax.py",
    "test_turn_ingress.py",
    "test_wake_long_and_quiet.py",
    "test_wake_busy_and_browsers.py",
    "test_window_identity.py",
)


def _run(path: Path, timeout: int) -> dict:
    started = time.monotonic()
    env = dict(os.environ)
    env.setdefault("PYTHONUTF8", "1")
    # Running a test file directly puts tests/ rather than the project root at
    # sys.path[0].  Some older modules add the root themselves and others rely
    # on the runner, so make the isolation strategy consistent for both.
    existing_path = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(ROOT) + (os.pathsep + existing_path if existing_path else "")
    try:
        proc = subprocess.run(
            [sys.executable, str(path)], cwd=str(ROOT), env=env,
            text=True, capture_output=True, timeout=timeout,
        )
        status = "passed" if proc.returncode == 0 else "failed"
        output = (proc.stdout + proc.stderr).strip()
        return {"test": path.name, "status": status,
                "seconds": round(time.monotonic() - started, 2),
                "returncode": proc.returncode, "output": output[-6000:]}
    except subprocess.TimeoutExpired as exc:
        tail = "".join(filter(None, (exc.stdout or "", exc.stderr or "")))
        return {"test": path.name, "status": "timed_out",
                "seconds": round(time.monotonic() - started, 2),
                "returncode": None, "output": tail[-6000:]}
    except Exception as exc:
        return {"test": path.name, "status": "error",
                "seconds": round(time.monotonic() - started, 2),
                "returncode": None, "output": str(exc)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Run VANTA's isolated release gate")
    parser.add_argument("--full", action="store_true", help="run every test module")
    parser.add_argument("--timeout", type=int, default=90,
                        help="seconds allowed per module")
    parser.add_argument("--workers", type=int, default=3,
                        help="parallel isolated test processes")
    args = parser.parse_args()

    names = ([p.name for p in sorted(TESTS.glob("test_*.py"))]
             if args.full else list(CRITICAL))
    paths = [TESTS / name for name in names]
    missing = [str(path) for path in paths if not path.exists()]
    paths = [path for path in paths if path.exists()]

    started = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {pool.submit(_run, path, args.timeout): path for path in paths}
        results = []
        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            results.append(result)
            mark = "PASS" if result["status"] == "passed" else "FAIL"
            print(f"[{mark}] {result['test']} ({result['seconds']}s)", flush=True)

    results.sort(key=lambda item: item["test"])
    failed = [item for item in results if item["status"] != "passed"]
    payload = {
        "suite": "full" if args.full else "critical",
        "started_at": started,
        "duration_seconds": round(time.time() - started, 2),
        "passed": len(results) - len(failed),
        "failed": len(failed),
        "missing": missing,
        "status": "passed" if not failed and not missing else "failed",
        "results": results,
    }
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\n{payload['status'].upper()}: {payload['passed']}/{len(results)} modules passed")
    print(f"Report: {REPORT}")
    if failed:
        for item in failed:
            print(f"\n--- {item['test']} ({item['status']}) ---\n{item['output']}")
    return 0 if payload["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
