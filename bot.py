"""Entrypoint — delegates to ``prosper.bot``.

Run: ``uv run bot.py`` (Pipecat CLI discovers ``bot`` coroutine).
"""
import sys

# Windows PowerShell defaults to cp1252; pipecat's runner prints "🚀 Bot
# ready!" which crashes the boot before Uvicorn binds. Force utf-8 on the
# stdio streams so the emoji print is harmless.
for _stream in (sys.stdout, sys.stderr):
    reconfigure = getattr(_stream, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(encoding="utf-8")

from prosper.bot import bot  # noqa: E402, F401 — re-exported for pipecat-ai-cli

if __name__ == "__main__":
    from pipecat.runner.run import main

    main()
