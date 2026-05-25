# Prosper Health voice agent — solution

> **The one-page overview.** This is the non-technical tour: what was built, why
> it matters, and why you can trust it on a real phone call. The full
> engineering reference + operations manual (how every piece is wired, how to
> run, evaluate, and operate it) lives in
> **[`ARCHITECTURE.md`](./ARCHITECTURE.md)**.

---

## What it is

A **voice agent that answers a clinic's phone** and handles the front-desk
basics end to end: it greets the caller, works out who they are (or registers
them as a new patient), and then **books, cancels, or reschedules** their
appointment — all by voice, in a natural back-and-forth conversation.

It is a complete, self-contained system: the voice agent, a clinic database
("EHR") it reads and writes, a **live operator dashboard** so staff can watch
calls in real time, and a **front-desk inbox + calendar** that fills in as calls
happen. Built for the Prosper Technologies challenge.

## What a call looks like

> *"Hi, you've reached Prosper Health — how can I help?"*
> — caller: *"I need to see someone, I've been really anxious lately."*
> The agent recognises a mental-health need, finds the caller in the system by
> phone, offers a couple of therapist times that actually fit a longer first
> visit, confirms the one they pick, and books it. The moment it's booked, a
> note lands in the front-desk inbox and the appointment appears on the clinic
> calendar.

The agent handles the messy reality of real callers: people who interrupt, who
mis-say their date of birth and correct themselves, who change their mind
mid-call ("actually, can we move it instead?"), who ramble, or whose phone line
garbles a word. It asks again instead of guessing, and it never pretends to have
done something it didn't.

## What it can do

- **Have a real conversation** — natural voice in and out, warm (not robotic)
  tone, and it **stops talking the moment the caller cuts in**, the way a person
  would. Crucially, when you talk over it, it remembers *only the part of its
  sentence you actually heard* — so if it gets cut off halfway through "I have
  Monday, Tuesday, or…", it won't later assume it already offered you Wednesday.
  That keeps the conversation honest after an interruption instead of drifting.
- **Know who it's talking to** — looks the caller up by phone or by name + date
  of birth, sorts out look-alikes by reading the options back, and registers new
  patients. It will not touch anyone's appointments until it's sure who's on the
  line.
- **Book the right kind of visit** — it reads the caller's complaint and routes
  them to the right specialty and the right visit length (a first psychiatry
  visit needs more time than a routine check-up), then offers a few fitting times
  rather than reading out a long list.
- **Cancel and reschedule safely** — moving an appointment is a single, all-or-
  nothing operation: if the new time is taken, the original is never lost.
- **Know its limits** — if someone describes a medical emergency it stops and
  redirects to 911; if it gets stuck or can't help, it takes a message and hands
  off to a human instead of flailing.
- **Keep staff in the loop** — every booking, cancellation, reschedule, and
  hand-off leaves a note in the front-desk inbox, and the clinic calendar stays
  current.

## Why you can trust it on a live call

A voice agent at a clinic fails in one catastrophic way: **telling a patient
"you're all booked" when nothing was actually booked.** The entire system is
designed around making that impossible.

- **It can't go off-script.** Instead of letting the AI freely decide what to do,
  the agent runs on a small, fixed map of conversation stages. At each stage only
  a specific set of actions is even *available* to it — it physically cannot book
  before it has confirmed who the caller is, and cannot confirm a booking the
  database didn't accept.
- **The database is the only source of truth.** The agent confirms an
  appointment only after the database has actually accepted the write. It can't
  invent or read out an appointment that doesn't exist.
- **Privacy by default.** Phone numbers, dates of birth, and emails are masked in
  the operator view and in the logs; the agent never even sees the database's
  internal record IDs.
- **It degrades gracefully.** If the AI provider hiccups, the system retries,
  falls back to a second model, and — worst case — speaks a calm canned line and
  leaves a message for staff, rather than going silent or crashing the call.

These aren't aspirations bolted on at the end — they're enforced by how the
system is structured, and every one of them is checked automatically (below).

## What's genuinely hard here — and how it's handled

The challenge calls out a few things that separate a demo from something you'd
trust. Where each stands today:

| What matters | How it's addressed |
|---|---|
| **Speed** — phone calls can't have awkward pauses | The agent does the minimum work per turn, speaks a brief "one moment" only when a step is actually slow, and is tuned so the database is never the bottleneck. Real timing is measured, not guessed. |
| **Reliability** — AI providers fail | Automatic retries, a fallback model, a calm spoken recovery line, and a message to staff so a failure is never silent. |
| **Talking over the agent (barge-in)** — the most-noticed call bug | It stops on a dime and keeps an honest record of only what the caller heard, so an interruption never leaves it confused. Covered by 30+ automated interruption tests (cut off at every point, rapid repeats, hang-ups mid-sentence) plus a real synthesise-and-listen-back audio check. |
| **No false confirmations** — the cardinal sin | Enforced structurally (above) *and* caught automatically by tests that flag any "it's booked" with no matching database write. |
| **Testing without dialing in by hand** | A simulated caller (driven by AI, with a goal and a personality) calls the agent automatically, and the results are checked by hard rules. 100+ scripted scenarios run in ~5 seconds with zero cost; adversarial and "messy caller" suites probe the edges. |
| **Privacy / security** | PII masking, input size limits, and a guard that stops the agent from being pointed at an internal/cloud address. |

## Proof it works

- **108 conversation scenarios** pass end to end — happy paths plus the hard
  cases: prompt-injection attempts, callers demanding someone else's
  appointment, mid-call changes of mind, garbled speech, hang-ups at every point.
- **~715 automated tests** green, including a standing check that the agent can
  never confirm something it didn't actually do.
- **Strict type-checking** across the codebase; one command (`make verify`) gates
  every change.

## See it run

```bash
cp env.example .env        # add your ElevenLabs + OpenAI keys
make install               # one-time setup
make seed                  # create the demo clinic database
make run-all               # launch everything: agent, EHR, console + front desk
# then open http://127.0.0.1:7861/call and click Connect to talk to it
```

**On Windows / no `make`?** Run the exact same steps with `uv` directly:

```bash
cp env.example .env                     # (PowerShell: copy env.example .env)
uv sync                                 # = make install
uv run python scripts/seed.py           # = make seed
uv run python scripts/run_all.py        # = make run-all
# then open http://127.0.0.1:7861/call and click Connect to talk to it
```

Want proof without making a call or spending a cent?

```bash
make mock-eval                          # all 108 scenarios offline in ~5 s
uv run python -m evals --mock-llm       # same thing, without make
```

(Full command list, environment variables, and operations manual: see
`ARCHITECTURE.md` §2–§3 and `README.md`.)

## What we'd build next (and why it isn't here yet)

A few things were deliberately left for later — not forgotten, decided. Two worth
naming for a reviewer:

- **Caching the "what's free?" lookups.** When the agent checks availability, it
  asks the database every time. A short-lived cache could skip repeat lookups —
  but on the current setup that lookup already takes ~10 ms, so caching would save
  noise, not time. It only becomes worthwhile if the medical-records system moves
  to a remote server (where each lookup costs 100–300 ms). The catch: the version
  that *would* speed up a remote system is also the one that can quietly show a
  slot as "free" after someone else just took it — so doing it correctly means
  the cache must instantly forget a day's availability whenever an appointment on
  that day is booked, cancelled, or moved. The full, correct design is written up
  ready to build (`FUTURE.md` §1.3); we just don't pay its complexity for a
  saving we can't yet measure.
- **A real audio test loop.** The conversation logic is tested exhaustively in
  text; testing the *spoken* round-trip (synthesised voice → agent → voice back)
  is the natural next layer — see the testing notes below.

The complete, honest "what's deferred and why" list lives in `ARCHITECTURE.md`
§16 / §16.1 and the ranked roadmap in `FUTURE.md`.

## Where the detail lives

| If you want… | Read |
|---|---|
| The engineering deep-dive + how-to manual | **`ARCHITECTURE.md`** (start at §0) |
| Process & conversation-flow diagrams | `docs/architecture.md` |
| Why each load-bearing decision was made | `docs/adr/001..006` |
| The exhaustive capability list | `docs/FEATURES.md` |
| How the testing machinery works | `docs/tester.md` |
| What's deliberately deferred, and why | `ARCHITECTURE.md` §16 / §16.1 |

**Status:** 12 conversation stages, 11 actions the agent can take, 108 scenarios
+ ~715 tests green, strict types. What is *not* built and why →
`ARCHITECTURE.md` §16.1.
