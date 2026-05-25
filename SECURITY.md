# Security Policy

## Reporting a vulnerability

This repo is an interview submission, not a production deployment, but if
you spot something that would be exploitable in a real clinic context please
report it privately rather than opening a public issue.

Email: epsilonboys67@gmail.com — please include:

- A description of the issue and its impact
- Steps to reproduce (a failing test or eval scenario is ideal)
- The commit SHA you observed it on

I'll acknowledge within a few days. Please give me a reasonable window to
patch before public disclosure.

## Shipped security controls

The threat model and full audit live in
[`docs/research/2026-05-20-security-audit.md`](docs/research/2026-05-20-security-audit.md).
The controls actually in the codebase today:

- **SSRF guard at startup.** `PROSPER_EHR_URL` is parsed and rejected if
  the scheme is not `http`/`https` or the hostname is missing — closing the
  scheme-based exfil vectors (`file://`, `gopher://`, schemeless) at startup.
  It does **not** block dangerous IP ranges (e.g. the cloud-metadata address
  `169.254.169.254`): the legit default is loopback, and a parse-time IP check
  is bypassable (DNS rebinding, IPv6, httpx connecting by hostname), so it would
  be false security. Blocking egress to metadata/internal ranges is the deploy
  environment's job (network policy / egress firewall). See
  `src/prosper/bot.py::_validated_ehr_url`.
- **PII redaction before the LLM.** Tool results never reach the model with
  raw `patient_id` / `appointment_id` / `slot_id` UUIDs; the dispatcher
  compresses them to a human-readable summary so a leaked transcript is
  cheaper to handle. See `src/prosper/dispatcher.py::_redact_for_llm` and
  `src/prosper/observability/redact.py`.
- **Env fail-fast.** When `PROSPER_BOT_ENTRYPOINT=1`, missing API keys
  raise `SystemExit` before the ~17 s pipecat/silero import wall, so a
  misconfigured deploy cannot half-start in an exploitable state. See
  `src/prosper/bot.py`.

- **`/frontdesk` full-PII surface (F6).** The `/frontdesk` router
  (`src/prosper/integrations/router.py`) reads the full-PII `MailStore` (patient
  name, phone, and call summary). This is a deliberate higher-trust tier than the
  masked operator-console bus — a clinic receptionist legitimately needs real
  contact details to return a callback. **In the demo it is loopback-only
  (console uvicorn binds to `127.0.0.1` by default)**; no inbound internet path
  exists. In production, this endpoint must be behind authentication (e.g.,
  clinic SSO or a shared secret header) before exposing it beyond localhost.
  The `MailStore` root defaults to `data/mail/` and must not be served as a
  static file directory.
- **Mail filename sanitisation (path-traversal guard).** `MailStore.write`
  derives the per-session file as `<root>/<session_id>.jsonl`. Real session ids
  are server-generated UUIDs, but a malformed id containing `/` or `..` would let
  an append escape the mail root. `_safe_session_stem` reduces the id to
  `[A-Za-z0-9_-]` for the filename only (the canonical id is preserved inside each
  record), so a write can never address a path outside `data/mail/`. Defense in
  depth at the filesystem boundary for a PII store.

Controls explicitly **not** shipped (interview scope): mTLS to the EHR,
HIPAA-grade audit logging, per-tenant rate limiting, signed deploys.
