#!/usr/bin/env python3
"""Top-level release verification for the Agentic Document AI project.

    python scripts/verify_release.py             # full local release gate
    python scripts/verify_release.py --offline   # no live services (CI subset)
    python scripts/verify_release.py --base-url http://localhost:8000

Stages, in order (spec PHASE_17_SPEC.md §9):

     deterministic — these are exactly what CI runs

     1  backend syntax check         python -m compileall -q app scripts
     2  backend import validation    python -c "import app.main; ..."
     3  backend unit tests           pytest (-m "not integration" --strict-markers
                                       when --offline, i.e. no live services;
                                       the full suite otherwise)
     4  chunking driver              scripts/test_chunker.py (17 checks)
     5  deployment contract driver   scripts/test_phase13_deployment.py (19 checks)
     7  frontend lint                npm run lint                needs: Node 22+
     8  frontend tests               npm test
     9  frontend production build    npm run build

     live — need the services documented in DEPLOYMENT.md

     6  backend regression suite     scripts/run_all_regressions.py
                                          needs: database + Ollama
     10 API contract/security probe  scripts/api_contract_probe.py (26 checks)
                                          needs: running backend
     11 end-to-end smoke probe       scripts/e2e_smoke.py (24 checks)
     12 golden evaluation            evaluate.py + release gate
                                          (21/21 cases, 100/100/100)

Every stage runs as a child process with its own exit code. The script stops
at the first failing stage, names that stage in the summary, and exits
non-zero. Nothing is retried and no failure is swallowed.

`--offline` runs the deterministic stages only and reports every live gate as
NOT RUN instead of pretending it passed. It is the local equivalent of
.github/workflows/ci.yml.

Docker release verification is documented in DEPLOYMENT.md (build -> up ->
health -> probes -> evaluation -> down) because it runs against containers
rather than this interpreter.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND = REPO_ROOT / "backend"
FRONTEND = REPO_ROOT / "frontend"
GOLDEN_SET = REPO_ROOT / "eval" / "golden_sets" / "sample_insurance_policy.json"
REPORT_DIR = REPO_ROOT / "eval" / "reports"


def resolve(command: str) -> str:
    """Resolve an executable for subprocess use (npm is npm.cmd on Windows)."""
    found = shutil.which(command)
    return found if found else command


def banner(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78, flush=True)


def run(label: str, command: list[str], cwd: Path) -> bool:
    banner(label)
    print(f"$ {' '.join(command)}   (cwd: {cwd.relative_to(REPO_ROOT)})")
    started = time.time()
    try:
        completed = subprocess.run(command, cwd=str(cwd))
    except OSError as exc:
        print(f"[verify] could not start {command!r}: {exc}")
        return False
    duration = time.time() - started
    if completed.returncode != 0:
        print(
            f"[verify] stage failed: {label} (exit {completed.returncode}, {duration:.0f}s)")
        return False
    print(f"[verify] stage passed: {label} ({duration:.0f}s)")
    return True


def check_evaluation_gate(since: float) -> tuple[bool, str]:
    """The evaluation only counts if the release gates are actually met."""
    reports = sorted(
        (p for p in REPORT_DIR.glob("*.json") if p.stat().st_mtime >= since - 1),
        key=lambda p: p.stat().st_mtime,
    )
    if not reports:
        return False, "no evaluation report was written"
    report = json.loads(reports[-1].read_text(encoding="utf-8"))

    expected_cases = len(json.loads(
        GOLDEN_SET.read_text(encoding="utf-8"))["cases"])
    gates = {
        "total_cases": expected_cases,
        "failed_cases": 0,
        "retrieval_hit_rate": 100.0,
        "citation_validity_rate": 100.0,
        "correct_rejection_rate": 100.0,
    }
    failures = [
        f"{key}: expected {want}, got {report.get(key)!r}"
        for key, want in gates.items()
        if report.get(key) != want
    ]
    detail = (
        f"{report.get('passed_cases')}/{report.get('total_cases')} cases, "
        f"retrieval {report.get('retrieval_hit_rate')}%, "
        f"citation {report.get('citation_validity_rate')}%, "
        f"rejection {report.get('correct_rejection_rate')}% "
        f"(report: {reports[-1].name})"
    )
    if failures:
        return False, f"{detail}; gate failures: {'; '.join(failures)}"
    return True, detail


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the release verification stages and print a summary."
    )
    parser.add_argument(
        "--base-url",
        default="http://localhost:8000",
        help="backend base URL for the live probe/evaluation stages",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="run only the deterministic stages (the CI subset); the live "
        "gates are reported as NOT RUN rather than as passed",
    )
    args = parser.parse_args()

    py = sys.executable
    npm = resolve("npm")

    # CI runs without live services, so it deselects the integration tests and
    # adds --strict-markers. The full gate runs the complete suite.
    pytest_args = ["--strict-markers"]
    if args.offline:
        pytest_args += ["-m", "not integration"]

    # `app.database` builds the engine at import time, so importing the app
    # needs a syntactically valid URL. CI provides a placeholder pointing at a
    # closed port (nothing in the offline stages connects). Mirror that only
    # when this checkout has no URL at all, so a fresh clone behaves like CI.
    if args.offline and not os.environ.get("DATABASE_URL") and not (
        BACKEND / ".env"
    ).exists():
        os.environ["DATABASE_URL"] = "postgresql+psycopg://ci:ci@127.0.0.1:5432/ci"
        print(
            "[verify] no backend/.env and no DATABASE_URL: using the CI "
            "placeholder (offline stages open no connections)"
        )

    stages: list[tuple[str, str, list[str], Path]] = [
        (
            "backend syntax check (compileall app scripts)",
            "deterministic",
            [py, "-m", "compileall", "-q", "app", "scripts"],
            BACKEND,
        ),
        (
            "backend import validation",
            "deterministic",
            [py, "-c",
                "import app.main; import app.config; print('imports ok')"],
            BACKEND,
        ),
        (
            "backend unit tests (pytest)",
            "deterministic",
            [py, "-m", "pytest", *pytest_args],
            BACKEND,
        ),
        (
            "backend chunking driver (17 checks)",
            "deterministic",
            [py, "scripts/test_chunker.py"],
            BACKEND,
        ),
        (
            "backend deployment contract driver (19 checks)",
            "deterministic",
            [py, "scripts/test_phase13_deployment.py"],
            BACKEND,
        ),
        (
            "backend regression suite (Phase 1-15 drivers)",
            "live: database + Ollama",
            [py, "scripts/run_all_regressions.py"],
            BACKEND,
        ),
        ("frontend lint", "deterministic", [npm, "run", "lint"], FRONTEND),
        ("frontend tests", "deterministic", [npm, "test"], FRONTEND),
        ("frontend production build", "deterministic",
         [npm, "run", "build"], FRONTEND),
        (
            f"API contract & security probe ({args.base_url})",
            "live: running backend",
            [py, "scripts/api_contract_probe.py", "--base-url", args.base_url],
            BACKEND,
        ),
        (
            "end-to-end smoke probe (24 checks)",
            "live: running backend",
            [py, "scripts/e2e_smoke.py", "--base-url", args.base_url],
            BACKEND,
        ),
        (
            "golden evaluation (release gate 21/21, 100/100/100)",
            "live: running backend",
            [py, "evaluate.py", "--base-url", args.base_url],
            BACKEND,
        ),
    ]

    results: list[tuple[str, str, str]] = []
    failed_at: str | None = None

    for label, requirement, command, cwd in stages:
        if args.offline and requirement.startswith("live"):
            results.append((label, "NOT RUN", f"--offline; {requirement}"))
            continue

        if label.startswith("golden evaluation"):
            since = time.time()
            ok = run(label, command, cwd)
            if ok:
                ok, detail = check_evaluation_gate(since)
                print(
                    f"[verify] evaluation gate: {'PASS' if ok else 'FAIL'} — {detail}")
            else:
                detail = "evaluate.py exited non-zero"
            results.append((label, "PASS" if ok else "FAIL", detail))
            if not ok:
                failed_at = label
                break
            continue

        ok = run(label, command, cwd)
        results.append((label, "PASS" if ok else "FAIL", ""))
        if not ok:
            failed_at = label
            break

    # Stages never reached because of an earlier failure.
    reached = {label for label, _, _ in results}
    for label, requirement, _, _ in stages:
        if label not in reached:
            results.append((label, "NOT RUN", "earlier stage failed"))

    banner("RELEASE VERIFICATION SUMMARY")
    for label, status, detail in results:
        suffix = f"  ({detail})" if detail else ""
        print(f"  {status:<8} {label}{suffix}")

    not_run = [label for label, status, _ in results if status == "NOT RUN"]

    print()
    if failed_at:
        print(f"RELEASE VERIFICATION: FAILED at stage -> {failed_at}")
        return 1
    if not_run:
        print(
            "RELEASE VERIFICATION: deterministic stages passed; "
            f"{len(not_run)} live stage(s) NOT RUN (--offline)."
        )
        print("Run without --offline (with database + Ollama + backend) for the full gate.")
        return 0
    print("RELEASE VERIFICATION: all stages passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
