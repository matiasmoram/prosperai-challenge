"""LLM-as-judge: scores a transcript against natural-language criteria."""

from __future__ import annotations

import os
from typing import Any

_JUDGE_SYSTEM = """\
You are an evaluator for a voice-agent transcript. Given a list of pass
criteria and a transcript, answer strictly with one of:

PASS — <one short reason>
FAIL — <one short reason>

Be strict. If even one criterion is clearly not met, FAIL.
"""


async def judge_transcript(
    *,
    client: Any,
    transcript: list[dict],
    criteria: list[str],
    model: str | None = None,
) -> tuple[bool, str]:
    resolved_model = model or os.environ.get("PROSPER_EVAL_MODEL", "gpt-4o-mini")
    formatted = _format_transcript(transcript)
    crit_block = "\n".join(f"- {c}" for c in criteria)
    resp = await client.chat.completions.create(
        model=resolved_model,
        temperature=0,
        messages=[
            {"role": "system", "content": _JUDGE_SYSTEM},
            {
                "role": "user",
                "content": f"CRITERIA:\n{crit_block}\n\nTRANSCRIPT:\n{formatted}",
            },
        ],
    )
    text = (resp.choices[0].message.content or "").strip()
    head = text.split("\n", 1)[0]
    is_pass = head.upper().startswith("PASS")
    return is_pass, text


def _format_transcript(transcript: list[dict]) -> str:
    lines: list[str] = []
    for ev in transcript:
        kind = ev.get("kind")
        if kind == "user":
            lines.append(f"USER: {ev['text']}")
        elif kind == "assistant":
            lines.append(f"BOT[{ev.get('state', '?')}]: {ev['text']}")
        elif kind == "tool_ok":
            lines.append(f"TOOL_OK {ev['name']}")
        elif kind == "tool_err":
            lines.append(f"TOOL_ERR {ev['name']} code={ev['code']}")
        elif kind == "tool_rejected":
            lines.append(f"TOOL_REJECTED {ev['name']}")
        elif kind == "transition":
            lines.append(f"STATE {ev['from']} -> {ev['to']} ({ev['label']})")
    return "\n".join(lines)
