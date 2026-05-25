"""Seed the local SQLite EHR with demo data.

Inserts ten providers across five specialties (two per specialty: Therapist,
Psychiatrist, General Practice, Dermatologist, Physiotherapist) and a five-week
slot grid (one past week + four future weeks, 9am-5pm UTC, every 30 min, lunch
noon-1pm skipped) plus 16 demo patients with deliberate variety:

- At least one patient with ≥4 upcoming appointments seeded (Ada Lovelace).
- A bulk fill of ~40-55 booked appointments spread deterministically across
  business days so the front-desk calendar has content every week (past week,
  this week, and the next three) rather than a near-empty grid.
- Two pairs of similar names (same last name, different DOB) to exercise
  the name+DOB lookup path.
- DOB spread: elderly (1939), young-adult (2002), and mid-career.
- Non-ASCII / apostrophe names to stress-test ``normalize_name``.

Idempotent: skips inserts if matching rows already exist; the per-slot
existing-appointment check means re-running never double-books.

Run: ``uv run python scripts/seed.py``
"""

from __future__ import annotations

import random
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from prosper.ehr.db import get_engine, init_db
from prosper.ehr.models import Appointment, Patient, Provider, Slot
from prosper.ehr.repository import normalize_name

# ---------------------------------------------------------------------------
# Providers — two per specialty so the specialty-filter path is exercised.
# ---------------------------------------------------------------------------
PROVIDERS: list[tuple[str, str, str]] = [
    # Therapist
    ("Dr. Aisha Patel", "America/New_York", "Therapist"),
    ("Dr. Priya Sharma", "America/New_York", "Therapist"),
    # Psychiatrist
    ("Dr. Marcus Chen", "America/New_York", "Psychiatrist"),
    ("Dr. David Kim", "America/New_York", "Psychiatrist"),
    # General Practice
    ("Dr. Sofia Romero", "America/New_York", "General Practice"),
    ("Dr. Elena Vasquez", "America/New_York", "General Practice"),
    # Dermatologist
    ("Dr. Liam Okonkwo", "America/New_York", "Dermatologist"),
    ("Dr. Nadia Kowalski", "America/New_York", "Dermatologist"),
    # Physiotherapist
    ("Dr. Yuki Tanaka", "America/New_York", "Physiotherapist"),
    ("Dr. Ben Osei", "America/New_York", "Physiotherapist"),
]


def _patient(first: str, last: str, dob: date, phone: str) -> dict[str, object]:
    return {
        "first_name": first,
        "last_name": last,
        # Use the full normalisation pipeline (diacritics, honorifics, bidi
        # marks) rather than a bare .lower() so the stored value matches what
        # repo.find_patient_by_name_dob compares against at lookup time.
        "name_normalized": normalize_name(f"{first} {last}"),
        "dob": dob,
        "phone": phone,
    }


# ---------------------------------------------------------------------------
# Patients — deliberate variety for richer demo scenarios.
#
# Phones are distinct +1-202-555-01xx / +1-202-555-02xx NANP 555-reserved
# test numbers; each phone is unique across the set.
#
# Variety goals:
#   - Same last name pairs (Smith x2, Johnson x2) with different DOBs to
#     exercise the name+DOB disambiguation path.
#   - Apostrophe (O'Brien) and non-ASCII diacritic (Müller, Al-Sayed) names
#     to exercise normalize_name across the full pipeline.
#   - DOB spread: 1939 (elderly), 2002 (young-adult), mix of decades.
#   - "Power user" patient (Ada Lovelace) will receive ≥4 seeded appointments.
# ---------------------------------------------------------------------------
DEMO_PATIENTS: list[dict[str, object]] = [
    # ── legacy demo patients (names preserved for mock-eval compatibility) ──
    _patient("Ada", "Lovelace", date(1990, 12, 10), "+12025550100"),
    _patient("Grace", "Hopper", date(1906, 12, 9), "+12025550111"),
    _patient("John", "Smith", date(1985, 3, 12), "+12025550102"),
    _patient("Maria", "Garcia", date(1972, 11, 4), "+12025550103"),
    _patient("James", "Williams", date(1968, 7, 21), "+12025550104"),
    _patient("Patricia", "Brown", date(1995, 2, 28), "+12025550105"),
    _patient("Robert", "Jones", date(1959, 9, 15), "+12025550106"),
    _patient("Linda", "Nguyen", date(2001, 6, 3), "+12025550107"),
    _patient("Michael", "O'Brien", date(1980, 1, 30), "+12025550108"),
    _patient("Fatima", "Al-Sayed", date(1993, 10, 18), "+12025550109"),
    # ── new patients ────────────────────────────────────────────────────────
    # Same-last-name pair (Smith) — different DOB to exercise disambiguation.
    _patient("Sarah", "Smith", date(1990, 6, 15), "+12025550110"),
    # Same-last-name pair (Johnson) — two different patients, different DOBs.
    _patient("Thomas", "Johnson", date(1955, 4, 22), "+12025550112"),
    _patient("Rachel", "Johnson", date(1988, 9, 7), "+12025550113"),
    # Elderly patient — DOB spread.
    _patient("Eleanor", "Whitfield", date(1939, 3, 8), "+12025550114"),
    # Young adult — DOB spread.
    _patient("Lena", "Müller", date(2002, 11, 25), "+12025550115"),
    # Additional variety.
    _patient("Carlos", "Reyes", date(1976, 8, 3), "+12025550116"),
]

# ---------------------------------------------------------------------------
# Slot generation
# ---------------------------------------------------------------------------

# Slot grid spans one past week + four future weeks so the front-desk calendar
# has content to browse in either direction (not just "this week").
_SLOT_WINDOW_START_OFFSET = -7  # days before today
_SLOT_WINDOW_DAYS = 35  # total calendar days generated (≈ 5 weeks)


def _make_slots_for_provider(
    provider_id: str, start: date, days: int = _SLOT_WINDOW_DAYS
) -> list[Slot]:
    """Generate 30-min slots Mon-Fri 09:00-17:00 naive-UTC across ``days`` days."""
    slots: list[Slot] = []
    for day_offset in range(days):
        d = start + timedelta(days=day_offset)
        # Skip weekends (Mon=0 … Sun=6).
        if d.weekday() >= 5:
            continue
        for hour in range(9, 17):
            if hour == 12:
                continue  # lunch
            for minute in (0, 30):
                start_dt = datetime.combine(d, time(hour, minute), tzinfo=timezone.utc)
                # Strip tzinfo → naive UTC (seed convention).
                start_dt = start_dt.replace(tzinfo=None)
                slots.append(
                    Slot(
                        provider_id=provider_id,
                        start_at=start_dt,
                        end_at=start_dt + timedelta(minutes=30),
                    )
                )
    return slots


# ---------------------------------------------------------------------------
# Demo appointments for "Ada Lovelace" — power-user with ≥4 bookings.
# We book her into future slots (days 2-5) so get_upcoming_appointments picks
# them up; these are deliberately spread across different providers.
# ---------------------------------------------------------------------------


def _seed_ada_appointments(session: Session, ada_id: str, base: date) -> None:
    """Book Ada Lovelace into four slots spread across different providers.

    Uses days 2-5 (relative to base) at 09:00 UTC.  Skips weekends so the
    chosen days always land inside the seeded slot grid.
    """
    # Collect business days starting at base+1.
    business_days: list[date] = []
    d = base + timedelta(days=1)
    while len(business_days) < 4:
        if d.weekday() < 5:
            business_days.append(d)
        d += timedelta(days=1)

    # Fetch providers in insertion order (we want a spread of specialties).
    providers = session.execute(select(Provider)).scalars().all()
    if len(providers) < 4:
        return  # shouldn't happen; guard only

    for i, appt_date in enumerate(business_days):
        provider = providers[i % len(providers)]
        slot_start = datetime(appt_date.year, appt_date.month, appt_date.day, 9, 0)
        slot = session.execute(
            select(Slot).where(
                Slot.provider_id == provider.id,
                Slot.start_at == slot_start,
            )
        ).scalar_one_or_none()
        if slot is None:
            continue
        # Skip if a scheduled appointment already exists for this slot.
        existing = session.execute(
            select(Appointment).where(
                Appointment.slot_id == slot.id,
                Appointment.status == "scheduled",
            )
        ).scalar_one_or_none()
        if existing is not None:
            continue
        # Make Ada's first visit a 60-min therapy intake so the calendar has a
        # longer (two-slot) appointment to render; the rest stay 30 min.
        appt = Appointment(
            patient_id=ada_id,
            slot_id=slot.id,
            duration_minutes=60 if i == 0 else 30,
            status="scheduled",
        )
        session.add(appt)

    session.commit()


# ---------------------------------------------------------------------------
# Bulk demo appointments — fill the front-desk calendar so it isn't sparse.
#
# Deterministic (seeded RNG) so the candidate set is *fixed*: the same
# (day, hour, minute) triples are chosen every run, mapped to the same
# (provider, patient) by candidate index.  Idempotency is structural — a
# re-run picks the identical fixed candidates, finds their slots already
# booked, and adds nothing.  A taken candidate is *dropped*, never substituted
# with the next free slot (substitution is what would re-fill on every run).
#
# We walk every business day from (today-5) to (today+20).  Patients rotate
# (Ada excluded so we don't clash with her four upcoming demo bookings) and
# providers rotate so specialties stay spread across the week.
# ---------------------------------------------------------------------------

_BULK_WINDOW_START_OFFSET = -5  # first business-day candidate (days from today)
# +26 reaches into the fourth future week so every visible week (past, this,
# +1, +2, +3) gets content. Stays inside the 35-day slot grid (today-7..+27).
_BULK_WINDOW_END_OFFSET = 26  # last business-day candidate (days from today)
_BULK_HOURS: tuple[int, ...] = (9, 10, 11, 14, 15, 16)
_BULK_MINUTES: tuple[int, ...] = (0, 30)
_BULK_PER_DAY = 3  # appointments booked per business day


def _seed_bulk_appointments(session: Session, today: date) -> None:
    """Book ~40-55 appointments deterministically across the slot window.

    Spreads bookings over business days from ``today-5`` to ``today+20`` so
    every visible week of the calendar has content.  Excludes Ada (she has her
    own four demo bookings).  The candidate set is fixed per run, so skipping a
    missing or already-booked slot leaves it dropped (not substituted) and the
    seed stays idempotent on re-run.
    """
    rng = random.Random(20260525)  # noqa: S311 — demo-data spread, not crypto

    providers = session.execute(select(Provider)).scalars().all()
    # Exclude Ada (phone +12025550100) so her four-appointment demo is untouched.
    patients = [
        p for p in session.execute(select(Patient)).scalars().all() if p.phone != "+12025550100"
    ]
    if not providers or not patients:
        return  # nothing to book against; guard only

    # ci = global candidate index → drives provider/patient rotation. It
    # advances once per *candidate considered* (not per successful add) so a
    # given candidate always maps to the same provider+patient across runs.
    ci = 0
    for day_offset in range(_BULK_WINDOW_START_OFFSET, _BULK_WINDOW_END_OFFSET + 1):
        d = today + timedelta(days=day_offset)
        if d.weekday() >= 5:
            continue  # weekends have no slots
        # Fix the per-day candidate times deterministically: shuffle the full
        # grid with the seeded RNG, then take a stable slice. Same seed → same
        # slice every run.
        time_choices = [(h, m) for h in _BULK_HOURS for m in _BULK_MINUTES]
        rng.shuffle(time_choices)
        for hour, minute in time_choices[:_BULK_PER_DAY]:
            provider = providers[ci % len(providers)]
            patient = patients[ci % len(patients)]
            ci += 1
            slot_start = datetime(d.year, d.month, d.day, hour, minute)
            slot = session.execute(
                select(Slot).where(
                    Slot.provider_id == provider.id,
                    Slot.start_at == slot_start,
                )
            ).scalar_one_or_none()
            if slot is None:
                continue  # dropped, not substituted
            existing = session.execute(
                select(Appointment).where(
                    Appointment.slot_id == slot.id,
                    Appointment.status == "scheduled",
                )
            ).scalar_one_or_none()
            if existing is not None:
                continue  # already booked (re-run, or Ada holds it) → drop
            session.add(
                Appointment(
                    patient_id=patient.id,
                    slot_id=slot.id,
                    duration_minutes=30,
                    status="scheduled",
                )
            )

    session.commit()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    engine = get_engine()
    init_db()
    with Session(engine) as session:
        # -- providers -------------------------------------------------------
        for name, tz, specialty in PROVIDERS:
            if not session.execute(select(Provider).where(Provider.name == name)).first():
                session.add(Provider(name=name, timezone=tz, specialty=specialty))
        session.commit()

        today = datetime.now(timezone.utc).date()

        # -- slots -----------------------------------------------------------
        # Grid spans one past week + four future weeks (see _SLOT_WINDOW_*),
        # so the calendar has browsable content in both directions. Ada's and
        # the bulk bookings anchor on ``today`` (not the window start) so the
        # upcoming-appointment demo always lands in the future.
        if session.execute(select(Slot).limit(1)).first() is None:
            slot_start = today + timedelta(days=_SLOT_WINDOW_START_OFFSET)
            for prov in session.execute(select(Provider)).scalars():
                session.add_all(_make_slots_for_provider(prov.id, slot_start, _SLOT_WINDOW_DAYS))
            session.commit()

        # -- patients --------------------------------------------------------
        for p in DEMO_PATIENTS:
            if not session.execute(select(Patient).where(Patient.phone == p["phone"])).first():
                session.add(Patient(**p))
        session.commit()

        # -- Ada's demo appointments -----------------------------------------
        ada_row = session.execute(
            select(Patient).where(Patient.phone == "+12025550100")
        ).scalar_one_or_none()
        if ada_row is not None:
            _seed_ada_appointments(session, ada_row.id, today)

        # -- bulk calendar fill ----------------------------------------------
        _seed_bulk_appointments(session, today)

        # -- summary ---------------------------------------------------------
        prov_count = session.execute(select(Provider)).scalars().all()
        slot_count = session.execute(select(Slot)).scalars().all()
        pat_count = session.execute(select(Patient)).scalars().all()
        appt_count = session.execute(select(Appointment)).scalars().all()

        # Per-specialty breakdown.
        specialty_counts: dict[str, int] = {}
        for prov in prov_count:
            specialty_counts[prov.specialty] = specialty_counts.get(prov.specialty, 0) + 1
        breakdown = ", ".join(f"{sp}={n}" for sp, n in sorted(specialty_counts.items()))
        scheduled = [a for a in appt_count if a.status == "scheduled"]
        print(
            f"seeded: providers={len(prov_count)} slots={len(slot_count)} "
            f"patients={len(pat_count)} appointments={len(appt_count)} "
            f"(scheduled={len(scheduled)})"
        )
        print(f"  specialty breakdown: {breakdown}")
        print(
            "  calendar fill: Ada (4 upcoming) + bulk spread across "
            f"{_BULK_WINDOW_START_OFFSET}..+{_BULK_WINDOW_END_OFFSET} days"
        )


if __name__ == "__main__":
    main()
