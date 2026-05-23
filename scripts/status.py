"""Print a one-page status of the running Prosper bot + EHR.

Usage: ``uv run python scripts/status.py``

Hits EHR /health, lists patient + slot + appointment counts, prints last
~5 commits, total tests + coverage % (if `coverage.xml` exists), eval
scenario count, and a confirmation that both processes respond on their
expected ports.

Designed so a reviewer can run one command and see whether everything is
wired up before opening the browser to talk to the bot.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from xml.etree import ElementTree as ET

import httpx

EHR_URL = "http://127.0.0.1:8000"
BOT_URL = "http://127.0.0.1:7860"
REPO_ROOT = Path(__file__).resolve().parent.parent


def _check(label: str, fn) -> str:
    try:
        return fn()
    except Exception as e:
        return f"ERR ({type(e).__name__}: {e})"


def _ehr_status() -> str:
    r = httpx.get(f"{EHR_URL}/health", timeout=2.0)
    return f"OK ({r.status_code})" if r.status_code == 200 else f"BAD ({r.status_code})"


def _bot_status() -> str:
    r = httpx.get(f"{BOT_URL}/client", timeout=2.0, follow_redirects=False)
    # Bot client redirects (307) — that's a live signal.
    return f"OK ({r.status_code})" if r.status_code in (200, 307) else f"BAD ({r.status_code})"


def _ehr_counts() -> dict[str, int]:
    out: dict[str, int] = {}
    # We can derive patient and slot counts via the public endpoints.
    out["available_slots_tomorrow"] = len(
        httpx.get(
            f"{EHR_URL}/availability",
            params={"date": "2026-05-21"},
            timeout=2.0,
        ).json()["slots"]
    )
    out["seeded_patients_phone_match"] = len(
        httpx.get(
            f"{EHR_URL}/patients/by-phone",
            params={"phone": "2025550100"},
            timeout=2.0,
        ).json()["patients"]
    )
    return out


def _last_commits(n: int = 5) -> list[str]:
    out = subprocess.run(
        ["git", "log", f"-{n}", "--oneline"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return out.stdout.strip().splitlines()


def _total_commits() -> str:
    out = subprocess.run(
        ["git", "rev-list", "--count", "HEAD"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return out.stdout.strip()


def _test_count() -> int:
    out = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q",
         "tests/", "evals/test_types.py", "evals/test_runner_checks.py"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    last = [line for line in out.stdout.splitlines() if "tests collected" in line or "test collected" in line]
    if not last:
        return -1
    return int(last[-1].split()[0])


def _scenario_count() -> int:
    # Side-import via subprocess to avoid heavy pipecat imports.
    out = subprocess.run(
        [sys.executable, "-c",
         "from evals.scenarios import SCENARIOS; print(len(SCENARIOS))"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return int(out.stdout.strip() or 0)


def _coverage_pct() -> str:
    path = REPO_ROOT / "coverage.xml"
    if not path.exists():
        return "n/a (run `uv run pytest --cov`)"
    root = ET.parse(path).getroot()
    line_rate = float(root.attrib.get("line-rate", "0"))
    return f"{line_rate * 100:.0f}%"


def main() -> int:
    print("# Prosper Health voice agent — status\n")
    print(f"## Services")
    print(f"- EHR ({EHR_URL}):  {_check('ehr', _ehr_status)}")
    print(f"- Bot ({BOT_URL}):  {_check('bot', _bot_status)}")
    try:
        for k, v in _ehr_counts().items():
            print(f"- {k}: {v}")
    except Exception as e:
        print(f"- counts: ERR ({e})")

    print(f"\n## Code")
    print(f"- Total git commits: {_total_commits()}")
    print(f"- Eval scenarios:    {_scenario_count()}")
    print(f"- Tests collected:   {_test_count()}")
    print(f"- Coverage:          {_coverage_pct()}")

    print(f"\n## Last 5 commits")
    for line in _last_commits():
        print(f"- {line}")

    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
