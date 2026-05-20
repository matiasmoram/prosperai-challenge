"""Entrypoint — delegates to ``prosper.bot``.

Run: ``uv run bot.py`` (Pipecat CLI discovers ``bot`` coroutine).
"""
from prosper.bot import bot  # noqa: F401 — re-exported for pipecat-ai-cli

if __name__ == "__main__":
    from pipecat.runner.run import main

    main()
