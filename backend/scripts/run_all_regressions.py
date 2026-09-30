"""Run the complete backend test suite with a single command.

    cd backend
    python scripts/run_all_regressions.py

Phase 16 section 12.3 asks for one obvious way to execute the project's
complete backend suite. This runner executes, in order:

1. the unit/integration tests under ``tests/`` (``pytest.ini`` restricts
   pytest's default collection to that directory), and
2. every Phase 1-15 regression driver under ``scripts/``.

Each regression driver prints its own ``... N checks, N passed, 0 failed``
summary and exits non-zero when one of its checks fails, so a single exit
code is enough to judge the run.

The runner is deliberately not named ``test_*`` so pytest never collects it.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent

REGRESSION_SCRIPTS = [
    "scripts/test_chunker.py",
    "scripts/test_embeddings.py",
    "scripts/test_ingestion.py",
    "scripts/test_phase8_retrieval.py",
    "scripts/test_phase9_rag_context.py",
    "scripts/test_phase10_ollama_answer.py",
    "scripts/test_phase11_agent.py",
    "scripts/test_phase12_frontend_integration.py",
    "scripts/test_phase13_deployment.py",
    "scripts/test_phase15_retrieval_quality.py",
]


def banner(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78, flush=True)


def run(label: str, args: list[str]) -> bool:
    banner(label)
    try:
        completed = subprocess.run(args, cwd=str(BACKEND_DIR))
    except OSError as exc:  # interpreter cannot spawn the child
        print(f"[run] could not start {args!r}: {exc}")
        return False
    return completed.returncode == 0


def main() -> int:
    outcomes: list[tuple[str, bool]] = []

    pytest_available = (
        subprocess.run(
            [sys.executable, "-m", "pytest", "--version"],
            capture_output=True,
            cwd=str(BACKEND_DIR),
        ).returncode
        == 0
    )
    if not pytest_available:
        print(
            "pytest is not installed for this interpreter.\n"
            "Install the development requirements first:\n"
            f"    {sys.executable} -m pip install -r requirements-dev.txt"
        )
        return 1

    outcomes.append(
        ("pytest (backend/tests)", run("pytest", [sys.executable, "-m", "pytest"]))
    )

    for script in REGRESSION_SCRIPTS:
        outcomes.append(
            (script, run(script, [sys.executable, script]))
        )

    banner("REGRESSION SUMMARY")
    for name, ok in outcomes:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")

    failed = [name for name, ok in outcomes if not ok]
    print()
    if failed:
        print(f"{len(failed)} of {len(outcomes)} suites FAILED")
        return 1
    print(f"all {len(outcomes)} suites passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
