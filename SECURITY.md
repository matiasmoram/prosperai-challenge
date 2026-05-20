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
  the scheme is not `http`/`https` or the hostname is missing. This blocks
  the easy SSRF where a tampered `.env` repoints the bot at cloud metadata
  (`169.254.169.254`) and the LLM is phished into exfiltrating the
  response. See `src/prosper/bot.py::_validated_ehr_url`.
- **PII redaction before the LLM.** Tool results never reach the model with
  raw `patient_id` / `appointment_id` / `slot_id` UUIDs; the dispatcher
  compresses them to a human-readable summary so a leaked transcript is
  cheaper to handle. See `src/prosper/dispatcher.py::_redact_for_llm` and
  `src/prosper/observability/redact.py`.
- **Env fail-fast.** When `PROSPER_BOT_ENTRYPOINT=1`, missing API keys
  raise `SystemExit` before the ~17 s pipecat/silero import wall, so a
  misconfigured deploy cannot half-start in an exploitable state. See
  `src/prosper/bot.py`.

Controls explicitly **not** shipped (interview scope): mTLS to the EHR,
HIPAA-grade audit logging, per-tenant rate limiting, signed deploys.
