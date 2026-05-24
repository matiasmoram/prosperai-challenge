# src/prosper/console/static/call/ — Front F4: Call frontend (WebRTC caller UI)

The phone-call page the caller opens. **Static assets only** — html/js/css.
No backend lives here. Owner front: **F4**. Ownership map: `../../../../../FRONTS.md`.

Parallel-safe with F1 F2 F3 F5 — **as long as you stay in this directory.**
Shares seam **S2** with F5 (the serving plumbing that mounts these assets).

## What lives here (F4-owned, the ONLY files you edit)
- `index.html` — the call page markup
- `call.js`    — WebRTC client; dials the bot directly at `BOT_ORIGIN` → `:7860/api/offer`
- `call.css`   — styling

## How it's wired (so you don't go looking for a backend that isn't here)
- Served as static files by the console app on `:7861` under `/call/static`
  (mounted by `../../sse.py::mount_static_on_app` — that's **F5** code, not yours).
- `call.js` does WebRTC signaling **straight to the Pipecat bot** at
  `:7860/api/offer` (`BOT_ORIGIN`). It does **not** call the console backend.
- So two ports are in play: `:7861` serves the page, `:7860` runs the call. Don't confuse them.

## Contracts you MUST NOT break
- **Static only.** You edit `index.html` / `call.js` / `call.css` and nothing else.
- **A new route or mount path is NOT yours.** If the page needs a new served path,
  that edit to `server.py` / `sse.py` is a **Front F5** change (seam S2) — request it,
  don't reach into F5 Python.
- **The bot pipeline (`:7860`, `bot.py`) is Front F2.** `call.js` signals into it;
  it never edits it.
- Keep `BOT_ORIGIN` pointing at the bot's offer endpoint — hard-coding a different
  origin breaks the call in deployment.

## Verify gate
No Python test owns this front — it's browser-driven. Manual smoke:
```
make seed        # once
make ehr         # terminal 1  (:8000)
make bot         # terminal 2  (:7860)
# start the console app (:7861), then open the /call page and Connect — confirm audio both ways
```
Still run `make verify` before committing (it won't test the page, but keeps the repo gate green).

## Open work — derive fresh each cycle (manager's job)
No `FUTURE.md` items target this front today. Likely work = caller-UX polish
(connection states, mic-permission prompts, reconnect handling). The bot's
*audible* behaviour is **F3** (`prompts.py`), not here — this front is the visual/transport shell only.
