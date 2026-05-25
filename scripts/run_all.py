"""Run the whole application at once — every process, every surface.

One command brings up all parts of the app and prints their URLs, then streams
each process's logs with a prefix so you see everything in one terminal:

    uv run python scripts/run_all.py        (or: make run-all)

Processes launched:
  * EHR API        — FastAPI + SQLite, the source of truth      :8000
  * bot            — Pipecat voice runner (WebRTC signaling)     :7860
  * console + web  — operator console + caller call UI +
                     persistent front-desk Mail/Calendar         :7861

The bot runs with PROSPER_CONSOLE_ENABLED=0 so it does NOT also bind :7861
(its per-connection console would race the standing one). The standing console
server owns :7861 and serves /console, /call and /frontdesk persistently,
reading the real data/mail/ store (where the bot appends mail mid-call) and
proxying the live EHR for the calendar.

Needs OPENAI_API_KEY + ELEVENLABS_API_KEY in .env for the bot to start; the EHR
and the web surfaces run regardless, so /frontdesk + the calendar are usable even
without the voice keys. Ctrl-C stops everything.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# (label, command, extra env). Order matters: EHR first so the others find it.
_PROCS: list[tuple[str, list[str], dict[str, str]]] = [
    (
        "ehr",
        ["uv", "run", "uvicorn", "prosper.ehr.api:app", "--host", "127.0.0.1", "--port", "8000"],
        {},
    ),
    # Console disabled on the bot so it does not bind :7861 (the standing server does).
    ("bot", ["uv", "run", "bot.py"], {"PROSPER_CONSOLE_ENABLED": "0"}),
    # Standing console + call UI + front-desk; reads real data/mail/ + live EHR.
    (
        "web",
        ["uv", "run", "python", "scripts/frontdesk_server.py"],
        {"PROSPER_CONSOLE_PORT": "7861", "PROSPER_EHR_URL": "http://127.0.0.1:8000"},
    ),
]

_URLS = """
────────────────────────────────────────────────────────────────────
  All parts up. Open:
    Caller call UI ....... http://127.0.0.1:7861/call        (dials the bot)
    Front desk (mail+cal)  http://127.0.0.1:7861/frontdesk
    Operator console ..... http://127.0.0.1:7861/console
    Bot connect UI ....... http://127.0.0.1:7860
    EHR API docs ......... http://127.0.0.1:8000/docs
  Ctrl-C to stop everything.
────────────────────────────────────────────────────────────────────
"""


def _seed_if_needed() -> None:
    """Seed the EHR DB once if it does not exist yet (auto-seed is empty-only)."""
    if (ROOT / "data" / "ehr.db").exists():
        return
    print("[run-all] no data/ehr.db — seeding …", flush=True)
    subprocess.run(["uv", "run", "python", "scripts/seed.py"], cwd=ROOT, check=True)  # noqa: S607


def _pump(label: str, proc: subprocess.Popen[str]) -> None:
    """Stream one child's combined output with a [label] prefix.

    Must NEVER raise: if this thread dies the child's stdout pipe stops being
    drained, its buffer fills, and the child blocks on its next print (which is
    how the bot used to freeze on :7860). So every write is guarded.
    """
    assert proc.stdout is not None  # noqa: S101 — type narrowing, stdout=PIPE guarantees it
    for line in proc.stdout:
        try:
            sys.stdout.write(f"[{label}] {line}")
            sys.stdout.flush()
        except Exception:  # noqa: S110 — draining must never die; a failed log line is fine
            pass


def main() -> int:
    # Our own stdout is cp1252 on a default Windows console; printing the
    # children's UTF-8 lines (emoji, JSON spans) would crash. Force UTF-8.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            with contextlib.suppress(Exception):
                reconfigure(encoding="utf-8", errors="replace")
    _seed_if_needed()
    children: list[tuple[str, subprocess.Popen[str]]] = []
    for label, cmd, extra_env in _PROCS:
        env = {**os.environ, **extra_env}
        proc = subprocess.Popen(  # noqa: S603 — fixed commands, uv on PATH, no shell
            cmd,
            cwd=ROOT,
            env={**env, "PYTHONIOENCODING": "utf-8"},
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            # Decode child output as UTF-8 and never die on a stray byte. On
            # Windows the default was cp1252, so pipecat's emoji / our JSON span
            # logs raised UnicodeDecodeError in the pump thread — which then
            # stopped draining the pipe, the child's stdout buffer filled, and
            # the child BLOCKED on its next print (bot froze, :7860 hung).
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        children.append((label, proc))
        threading.Thread(target=_pump, args=(label, proc), daemon=True).start()
        print(f"[run-all] started {label} (pid {proc.pid})", flush=True)
        time.sleep(1.0)  # small stagger so logs interleave readably

    print(_URLS, flush=True)

    try:
        while True:
            time.sleep(1.0)
            for label, proc in children:
                code = proc.poll()
                if code is not None:
                    # One died (commonly the bot when voice keys are missing).
                    # Report it but keep the rest up — the EHR + web surfaces
                    # are still useful on their own.
                    print(
                        f"[run-all] '{label}' exited (code {code}); leaving the rest up.",
                        flush=True,
                    )
                    children.remove((label, proc))
                    break
            if not children:
                print("[run-all] all processes exited.", flush=True)
                return 1
    except KeyboardInterrupt:
        print("\n[run-all] stopping all processes …", flush=True)
    finally:
        for _label, proc in children:
            if proc.poll() is None:
                proc.terminate()
        for _label, proc in children:
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()
    return 0


if __name__ == "__main__":
    # Make Ctrl-C land as KeyboardInterrupt on Windows too.
    with contextlib.suppress(Exception):
        signal.signal(signal.SIGINT, signal.default_int_handler)
    raise SystemExit(main())
