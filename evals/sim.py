"""Persona-driven simulator: an LLM playing the caller side of the call.

The persona prompt (per scenario) tells the model exactly what to say,
including any scripted mistake (e.g. "spell your last name 'Smyth' on first
try, then correct it to 'Smith' when read back"). Determinism is achieved by
scripting the persona, not by trying to constrain the bot.
"""

from __future__ import annotations

import os
from typing import Any


class PersonaSimulator:
    """Wraps an LLM to generate caller utterances given the bot's last reply."""

    def __init__(self, *, client: Any, persona: str, model: str | None = None) -> None:
        self._client = client
        self._persona = persona
        self._model = model or os.environ.get("PROSPER_EVAL_MODEL", "gpt-4o-mini")
        self._history: list[dict[str, Any]] = []

    async def reply_to(self, bot_text: str) -> str:
        if bot_text:
            self._history.append({"role": "user", "content": f"BOT: {bot_text}"})
        messages = [
            {"role": "system", "content": self._persona},
            {
                "role": "system",
                "content": (
                    "You are the CALLER. Reply with a short, natural sentence — "
                    "what the caller would say next. Don't narrate, don't explain, "
                    "just speak. Stop the call when satisfied by saying "
                    "'okay, thanks, bye'."
                ),
            },
            *self._history,
        ]
        resp = await self._client.chat.completions.create(
            model=self._model, messages=messages, temperature=0.3
        )
        text = resp.choices[0].message.content or ""
        self._history.append({"role": "assistant", "content": text})
        return text
