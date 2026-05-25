"""CLI: simulate live calls with autonomous adversarial callers and audit them.

    uv run python -m tester.simulate                 # all curated personas
    uv run python -m tester.simulate --only hallucination_bait --only rude_but_completes
    uv run python -m tester.simulate --generate 5    # LLM invents fresh personas
    uv run python -m tester.simulate -v              # also print redacted transcripts

This is the automatic call simulator: it generates the caller side with the LLM
(no scripted turns, no human dialing), drives the real bot, and fails (exit 1)
if any call breaks an invariant in :mod:`tester.invariants` — i.e. the bot
confirmed something it didn't actually do. Needs ``OPENAI_API_KEY`` (loaded from
``.env``).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import sys

from dotenv import load_dotenv

from prosper.observability.redact import redact_pii
from tester.invariants import InvariantViolation, check_call
from tester.live_sim import CallResult, simulate_call
from tester.personas import CURATED, Persona, generate_personas


def _print_transcript(result: CallResult) -> None:
    """Print a PII-redacted user/bot transcript for one call."""
    for ev in result.transcript:
        kind = ev.get("kind")
        if kind == "user":
            print(f"      caller: {redact_pii(ev.get('text', ''))}")
        elif kind == "assistant":
            print(f"      bot   : {redact_pii(ev.get('text', ''))}")
        elif kind == "tool_ok":
            print(f"      [tool ok  {ev.get('name', '?')}]")
        elif kind == "tool_err":
            print(f"      [tool ERR {ev.get('name', '?')} code={ev.get('code', '?')}]")


async def _run(
    personas: list[Persona], *, concurrency: int, model: str | None, verbose: bool
) -> int:
    """Run every persona, audit each call, print a report; return process exit code."""
    sem = asyncio.Semaphore(max(1, concurrency))

    from openai import AsyncOpenAI

    client = AsyncOpenAI()

    async def _bounded(p: Persona) -> tuple[CallResult, list[InvariantViolation]]:
        async with sem:
            result = await simulate_call(p, client=client, model=model)
            violations = check_call(result.events, result.transcript)
            # Messy-human personas add a clarification audit: a write that landed
            # on a garbled value with no re-prompt is a plowed_ahead_on_garble.
            violations.extend(
                InvariantViolation("plowed_ahead_on_garble", detail)
                for detail in result.clarification_violations
            )
            return result, violations

    pairs = await asyncio.gather(*[_bounded(p) for p in personas])

    total_violations = 0
    errored = 0
    corrupted = 0
    print(f"\nSimulated {len(pairs)} autonomous call(s):\n")
    for result, violations in pairs:
        if result.error:
            errored += 1
            mark = "ERR "
        elif result.corrupt:
            corrupted += 1
            mark = "CRPT"
        elif violations:
            mark = "FAIL"
        else:
            mark = "PASS"
        print(
            f"  [{mark}] {result.persona:34s} world={result.world:18s} "
            f"outcome={result.outcome or '-':11s} turns={result.turns:2d} "
            f"({result.duration_ms:5.0f}ms)"
        )
        if result.error:
            print(f"         error: {result.error}")
        if result.corrupt:
            print("         ! corrupt: caller LLM emitted unfilled [Placeholder] text")
        for v in violations:
            total_violations += 1
            print(f"         ✗ {v}")
        if verbose:
            _print_transcript(result)

    # Corrupt runs are excluded from clean/violation tallies — they tested
    # garbage input (e.g. phone="[PHONE]") and their results are meaningless.
    usable = len(pairs) - errored - corrupted
    clean = usable - sum(1 for r, v in pairs if v and not r.error and not r.corrupt)
    print(
        f"\n{clean}/{usable} clean"
        + (f" ({corrupted} corrupt/skipped)" if corrupted else "")
        + f", {sum(1 for r, v in pairs if v and not r.corrupt)} with violations"
        + f", {errored} errored."
    )
    if total_violations:
        print("HALLUCINATION/MISTAKE CAUGHT — see violations above.", file=sys.stderr)
    # Exit non-zero on any invariant breach OR any crashed call.
    # Corrupt runs do NOT trigger exit 1 — they are a data-quality warning only.
    return 1 if (total_violations or errored) else 0


def _select(args: argparse.Namespace) -> list[Persona]:
    """Resolve the persona list from CLI args (curated, filtered, or generated)."""
    if args.generate:
        from openai import AsyncOpenAI

        model = args.model or os.environ.get("PROSPER_EVAL_MODEL", "gpt-4o-mini")
        personas = asyncio.run(
            generate_personas(client=AsyncOpenAI(), n=args.generate, model=model)
        )
        if not personas:
            print("ERROR: persona generation returned nothing parseable", file=sys.stderr)
        return personas
    personas = list(CURATED)
    if args.only:
        wanted = set(args.only)
        personas = [p for p in personas if p.name in wanted]
    if args.n:
        personas = personas[: args.n]
    return personas


def main() -> int:
    """Parse args, ensure credentials, run the simulator. Returns the exit code."""
    load_dotenv()
    # The report prints "✗"/"—" glyphs; on a default Windows console (cp1252)
    # that raises UnicodeEncodeError mid-report and the run dies while printing a
    # violation (so a real finding never surfaces). Force UTF-8 with a safe
    # fallback so the harness never crashes on the very output it exists to show.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            with contextlib.suppress(Exception):
                reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Autonomous adversarial call simulator.")
    parser.add_argument("--only", action="append", help="Run only these curated persona names.")
    parser.add_argument("--n", type=int, help="Cap the number of personas run.")
    parser.add_argument("--generate", type=int, help="LLM-invent N fresh adversarial personas.")
    parser.add_argument(
        "--concurrency", type=int, default=3, help="Max calls in flight (default 3)."
    )
    parser.add_argument("--model", help="Override model for bot + caller (default gpt-4o-mini).")
    parser.add_argument("-v", "--verbose", action="store_true", help="Print redacted transcripts.")
    args = parser.parse_args()

    if not os.environ.get("OPENAI_API_KEY"):
        print("ERROR: OPENAI_API_KEY not set (looked in env + .env)", file=sys.stderr)
        return 2

    personas = _select(args)
    if not personas:
        print("no personas selected", file=sys.stderr)
        return 2

    return asyncio.run(
        _run(personas, concurrency=args.concurrency, model=args.model, verbose=args.verbose)
    )


if __name__ == "__main__":
    raise SystemExit(main())
