# Production-readiness audit

Audit date: 2026-05-20. Scope: things that would bite **in production** beyond
what `2026-05-20-codebase-audit.md` already covered (past-slot filter,
`IntegrityError` → `slot_taken`, UUID redaction, `hallucinated_id` defence).

Format per finding: **severity** — **fix** — **shipped now / future work**.

---

## Fixed in this pass (blockers + high)

### F1. EHR `httpx.AsyncClient` leak on abrupt disconnect — HIGH

`run_bot` called `dispatcher._ehr.__aenter__()` at start and `__aexit__()`
only inside `on_client_disconnected`. If the browser tab dies, the WebRTC
transport crashes during startup, the process gets SIGTERM mid-call, or
`runner.run` raises, the disconnect handler never fires and the
underlying httpx pool + connections leak. Multiple sequential calls to
the same bot instance would accumulate leaked clients.

Fix: wrap the EHR client in `async with dispatcher._ehr:` around the
pipeline, with a try/finally cancel on the task so cleanup runs in every
exit path. The disconnect handler now only cancels the task (releasing
`runner.run`) — the context manager is what actually closes the
connections. **bot.py:192-235.**

Ship now. **Done.**

### F2. DispatcherProcessor swallows nothing — exception crashed the whole call — HIGH

`await self._dispatcher.handle_user_turn(user_text)` was unguarded. An
OpenAI 5xx after retries exhausted (LLM adapter re-raises), an EHR
timeout, a JSON parse error, or any unexpected exception would bubble up
through `process_frame`, terminate the pipeline, and the caller hears
dead air with no recovery prompt.

Fix: wrap the call in try/except, log with stack trace, speak a short
recovery line ("Sorry, I missed that — could you say it again?") so the
caller can retry. The dispatcher's internal state is unchanged (the user
turn is still in `history`), so the next attempt has full context.
**bot.py:121-127.**

Ship now. **Done.**

### F3. `load_dotenv(override=True)` clobbers prod env — MEDIUM-HIGH

Best practice 2026: dotenv is a dev-only convenience; container / CI /
systemd env vars should be authoritative. `override=True` meant a stale
`.env` lying next to the binary in a deploy would silently overwrite the
`OPENAI_API_KEY` injected by Kubernetes/Railway/etc. — extremely hard
to debug.

Fix: `load_dotenv(override=False)`. **bot.py:56-59.** **Done.**

### F4. Unbounded `history` → token blow-up on long calls — HIGH

`Dispatcher.history` accumulates every user, assistant, system, and tool
message for the entire call. After 50 turns the LLM prompt is the
full transcript on every turn — quadratic cost, linear TTFT regression,
eventual context-window failure.

Fix: sliding window of last 40 messages (~20 turns) in `_messages_for_llm`.
Safe because (a) `CLINIC_PERSONA` + per-state `TASK_MESSAGES` are
re-prepended every turn, and (b) `SessionMemory` carries the structured
facts (`identified_patient`, `last_slots`, `last_upcoming_appointments`)
that the FSM and tool guards depend on — dropping old raw turns only
drops chit-chat. **dispatcher.py:348-365.** **Done.**

### F5. Dockerfile didn't ship `src/prosper` — BLOCKER for deploy

The original Dockerfile only `COPY ./bot.py bot.py` after `uv sync
--no-install-project`. The `prosper` package lives in `src/prosper/` and
was never copied into the image, so `from prosper.bot import bot` would
ImportError at container start. `docker-compose up` cannot have worked.

Fix: copy `src/`, `scripts/`, `pyproject.toml`, `uv.lock`, then run
`uv pip install --no-deps -e .` to install the project so the package is
importable. **Dockerfile.** **Done.**

---

## Medium / low — documented, not changed

### M1. `get_engine(reset=True)` race on module-level singletons — LOW

`_engine` and `_SessionLocal` are module-level globals reassigned inside
`get_engine(reset=True)`. In production `reset=True` is only ever called
by tests (which run sequentially in pytest); in the prod server lifecycle
the engine is initialized once at FastAPI startup and never reset. So no
real race exists today.

Future work if multi-tenancy is added: guard the reset path with
`threading.Lock`, or move the engine onto `FastAPI.state`.

### M2. FastAPI `_session_dep` is correct — LOW (informational)

Confirmed: `_session_dep` calls `get_session()` (which calls
`_SessionLocal()`) per request and closes in `finally`. Each request gets
its own SQLAlchemy `Session`. SQLite's `check_same_thread=False` is
required because Starlette runs sync endpoints on a thread pool.
**No fix needed.** Document upgrade path: replace SQLite with Postgres
when concurrency > ~50 RPS (SQLite write throughput is the limiter, not
the session pattern).

### M3. Observability gaps — MEDIUM (future work)

Today: `TimingCollector` prints one JSON line per span to stdout, plus a
session-end p50/p95 table. No `request_id` propagation bot → EHR → DB,
no aggregation across calls, no structured logger config.

Minimum prod observability without overengineering (in order of ROI):

1. **`session_id` per call.** Generate `uuid4()` in `run_bot`, pass to
   `Dispatcher` constructor, store on `TimingCollector`, include in
   every span line. Then add `EHRClient` middleware that sends
   `X-Prosper-Session-Id` header on every request; FastAPI logs it on
   every access log line. ~30 lines total.
2. **stdout → log aggregator.** If deploying on Railway / Fly / Vercel /
   anywhere with `journald`, the JSON spans already aggregate. For
   self-hosted, ship Vector / Promtail → Loki and grep by
   `session_id`. No code change.
3. **Sentry for the bot.** `sentry-sdk` + `sentry_sdk.init()` in
   `run_bot`, breadcrumb every state transition. Crash-only; do not
   send PII (transcript). 10 lines.
4. **Per-state TTFT histogram.** Already recorded; just export to
   Prometheus via `prometheus_client` Histogram. 15 lines.

OpenTelemetry is overengineering for a single-call short-lived process
until you have ≥3 services to correlate across.

### M4. SQLite backup / restore — MEDIUM (future work)

`data/ehr.db` is gitignored and contains all patient + appointment
state. For a real clinic this is the entire chart record. Minimum
backup story:

1. `make backup` target: `sqlite3 data/ehr.db ".backup data/ehr-$(date
   +%Y%m%d-%H%M%S).db"` (online backup, no app downtime).
2. Cron / systemd timer the same command nightly, rotate 30 days.
3. For HIPAA: ship backups to encrypted S3 (or equivalent), retain ≥6
   years per Security Rule.
4. Periodic restore drill — backup that's never been restored is a
   prayer, not a backup.

Not added now because (a) it's deployment-environment-specific and (b)
the SQLite → Postgres migration in M2 changes the answer entirely.
Document in SOLUTION.md "Future work".

### M5. Pipecat-specific gotchas — LOW

- **`SileroVADAnalyzer(stop_secs=0.2)`** — aggressive but justified by
  the latency research doc (`2026-05-20-latency-advanced-research.md`).
  Risk: cuts off speakers who pause mid-sentence; recommend 0.3 for
  callers with slower speech (older patients). Configurable via env in
  v2.
- **Smart Turn analyzer removed.** Intentional: the docstring at the
  top of `bot.py` says "We don't use OpenAILLMService directly" — Smart
  Turn was redundant with the FSM-driven dispatcher and added ~80ms per
  turn. Re-adding it only makes sense if we move to barge-in / overlap
  speech, which is out of scope for the challenge. **Document, don't
  re-add.**
- **Pipecat 0.0.100 pinned.** v1.0.0 (2026-04-14) is a breaking change;
  upgrade is listed as future work in SOLUTION.md and there is a
  defensive `logger.warning` at module load if a non-0.x is detected.
  Good as-is.

### M6. `docker-compose` end-to-end — MEDIUM (untestable here, fix applied blind)

Docker is not installed on this audit host, so I cannot literally run
`docker-compose up`. Reasoning from the Dockerfile + compose:

- After **F5** (above) the `prosper` package is now installed inside
  the image, so `uv run uvicorn prosper.ehr.api:app` and
  `uv run bot.py` should both resolve their imports.
- `volumes: ["./data:/app/data"]` mounts the host's SQLite over the
  container's — good for dev, terrible for prod (host crash = data
  loss). For prod use a managed volume or migrate to Postgres.
- The `bot` container has no `OPENAI_API_KEY` default — compose will
  pass it through from the host env via `${OPENAI_API_KEY}`. Document
  in SOLUTION.md that callers must `export` these or use `--env-file`.

Reviewer should `docker compose up --build` locally as the next step
to confirm the F5 fix works end-to-end.

---

## Summary

| # | Severity | Status |
|---|---|---|
| F1 EHR client leak | high | **Fixed** (bot.py) |
| F2 Dispatcher crash kills call | high | **Fixed** (bot.py) |
| F3 dotenv override=True | med-high | **Fixed** (bot.py) |
| F4 Unbounded history | high | **Fixed** (dispatcher.py) |
| F5 Dockerfile missing src/ | blocker (for deploy) | **Fixed** (Dockerfile) |
| M1 `get_engine(reset=True)` race | low | Documented; tests-only today |
| M2 Per-request `Session` correctness | informational | Confirmed correct |
| M3 Observability | medium | Documented (4-step minimum plan) |
| M4 SQLite backup | medium | Documented (`make backup`) |
| M5 Pipecat VAD / Smart Turn | low | Documented (decisions ratified) |
| M6 `docker-compose up` E2E | medium | Fix applied blind; reviewer to confirm |

All 46 unit tests stay green after edits.
