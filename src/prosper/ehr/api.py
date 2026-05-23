"""FastAPI app: 5 challenge endpoints plus a helper for upcoming appointments."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator
from datetime import date as date_t

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from loguru import logger
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session
from starlette.responses import PlainTextResponse

from prosper.ehr import repository as repo
from prosper.ehr.db import (
    get_engine,
    get_session,
    init_db,
    make_session_factory,
)
from prosper.ehr.models import Appointment, AppointmentStatus, Patient, Slot
from prosper.ehr.schemas import (
    AppointmentCancel,
    AppointmentCreate,
    AppointmentList,
    AppointmentOut,
    AppointmentReschedule,
    PatientCreate,
    PatientFuzzyList,
    PatientList,
    PatientOut,
    PatientWithSimilarity,
    SlotList,
    SlotOut,
)


def _session_dep() -> Iterator[Session]:
    """Module-level dep used only by the module-bottom ``app = create_app()``
    fallback (which binds the global engine). Apps built via
    ``create_app(engine=...)`` install their own per-app dep that closes
    over a private session factory — see :func:`create_app`.
    """
    s = get_session()
    try:
        yield s
    finally:
        s.close()


def _appt_to_out(a: Appointment) -> AppointmentOut:
    return AppointmentOut(
        id=a.id,
        patient_id=a.patient_id,
        slot_id=a.slot_id,
        status=a.status.value,
        start_at=a.slot.start_at,
        end_at=a.slot.end_at,
        provider_id=a.slot.provider_id,
        provider_name=a.slot.provider.name,
        notes=a.notes,
    )


def _slot_to_out(s: Slot) -> SlotOut:
    return SlotOut(
        id=s.id,
        provider_id=s.provider_id,
        provider_name=s.provider.name,
        start_at=s.start_at,
        end_at=s.end_at,
    )


def create_app(engine: Engine | None = None) -> FastAPI:
    """Build a FastAPI app.

    If ``engine`` is provided, the app is fully self-contained: it owns
    its own ``Session`` factory bound to that engine, has zero shared
    state with the module-level singleton, and is safe to instantiate
    in parallel for concurrent eval scenarios. Tables are created on
    the explicit engine.

    If ``engine`` is None, falls back to the module-level singleton
    engine (see :func:`prosper.ehr.db.get_engine`), preserving the
    existing ``app = create_app()`` deployment-time behaviour.
    """
    app = FastAPI(title="Prosper EHR", version="0.1.0")
    session_dep = _session_dep
    if engine is not None:
        init_db(engine)
        session_factory = make_session_factory(engine)
        app.state.engine = engine

        def session_dep() -> Iterator[Session]:
            s = session_factory()
            try:
                yield s
            finally:
                s.close()
    else:
        get_engine()
        init_db()

    # Per-app request counters fed by the middleware below; surfaced via
    # the /metrics endpoint. Counter is path-scoped so /metrics can break
    # down "what's getting hit": e.g. http_requests_total{path="/patients"}.
    app.state.request_counts = Counter()

    @app.middleware("http")
    async def _request_id_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
        # Bot sends ``X-Request-Id: <session>-<turn>-<n>``; mint one when
        # absent so any direct curl / TestClient call is still greppable.
        req_id = request.headers.get("x-request-id") or "anon"
        app.state.request_counts[request.url.path] += 1
        # Bind the id into loguru so every log line emitted inside this
        # request handler carries the same field — that's the join key
        # against the bot's per-span JSON logs.
        with logger.contextualize(request_id=req_id, path=request.url.path):
            response = await call_next(request)
        response.headers["X-Request-Id"] = req_id
        return response

    @app.post("/patients", response_model=PatientOut, status_code=201)
    def create_patient(
        payload: PatientCreate,
        session: Session = Depends(session_dep),
    ) -> PatientOut:
        # Normalise once at the boundary and reuse the canonical form for both
        # the uniqueness lookup and the insert. Passing the raw ``payload.phone``
        # through to ``repo.create_patient`` while the lookup uses the normalised
        # form can desync the two whenever ``normalize_phone`` is not strictly
        # idempotent on the raw input — letting two equivalent phone strings
        # slip past the 409 guard and surface as a 500 ``IntegrityError`` from
        # the underlying ``UNIQUE`` constraint.
        normalized_phone = repo.normalize_phone(payload.phone)
        existing = repo.find_patient_by_phone(session, normalized_phone)
        if existing:
            raise HTTPException(
                status_code=409, detail={"code": "patient_exists", "id": existing[0].id}
            )
        p = repo.create_patient(
            session,
            first_name=payload.first_name,
            last_name=payload.last_name,
            dob=payload.dob,
            phone=normalized_phone,
            email=payload.email,
        )
        return PatientOut.model_validate(p)

    @app.get("/patients/by-phone", response_model=PatientList)
    def find_by_phone(
        phone: str = Query(min_length=7),
        session: Session = Depends(session_dep),
    ) -> PatientList:
        return PatientList(
            patients=[
                PatientOut.model_validate(p) for p in repo.find_patient_by_phone(session, phone)
            ]
        )

    @app.get("/patients/by-name-dob", response_model=PatientFuzzyList)
    def find_by_name_dob(
        name: str = Query(min_length=1),
        dob: date_t = Query(...),
        min_similarity: float = Query(0.85),
        session: Session = Depends(session_dep),
    ) -> PatientFuzzyList:
        rows = repo.find_patient_by_name_dob(session, name, dob, min_similarity=min_similarity)
        return PatientFuzzyList(
            patients=[
                PatientWithSimilarity(
                    id=p.id,
                    first_name=p.first_name,
                    last_name=p.last_name,
                    dob=p.dob,
                    phone=p.phone,
                    email=p.email,
                    similarity=sim,
                )
                for p, sim in rows
            ]
        )

    @app.get("/patients/{patient_id}/appointments", response_model=AppointmentList)
    def patient_appointments(
        patient_id: str,
        session: Session = Depends(session_dep),
    ) -> AppointmentList:
        if session.get(Patient, patient_id) is None:
            raise HTTPException(status_code=404, detail={"code": "patient_not_found"})
        appts = repo.get_upcoming_appointments(session, patient_id=patient_id)
        return AppointmentList(appointments=[_appt_to_out(a) for a in appts])

    @app.get("/availability", response_model=SlotList)
    def availability(
        date: date_t = Query(...),
        provider_id: str | None = Query(default=None),
        specialty: str | None = Query(default=None),
        session: Session = Depends(session_dep),
    ) -> SlotList:
        slots = repo.list_available_slots(
            session, date_=date, provider_id=provider_id, specialty=specialty
        )
        return SlotList(slots=[_slot_to_out(s) for s in slots])

    @app.post("/appointments", response_model=AppointmentOut, status_code=201)
    def create_appointment(
        payload: AppointmentCreate,
        session: Session = Depends(session_dep),
    ) -> AppointmentOut:
        if session.get(Patient, payload.patient_id) is None:
            raise HTTPException(status_code=404, detail={"code": "patient_not_found"})
        if session.get(Slot, payload.slot_id) is None:
            raise HTTPException(status_code=404, detail={"code": "slot_not_found"})
        try:
            appt = repo.create_appointment(
                session,
                patient_id=payload.patient_id,
                slot_id=payload.slot_id,
                notes=payload.notes,
            )
        except repo.SlotTakenError as e:
            raise HTTPException(
                status_code=409,
                detail={"code": "slot_taken", "owner_patient_id": e.owner_patient_id},
            ) from e
        return _appt_to_out(appt)

    @app.post("/appointments/{appointment_id}/cancel", response_model=AppointmentOut)
    def cancel_appointment(
        appointment_id: str,
        payload: AppointmentCancel,
        session: Session = Depends(session_dep),
    ) -> AppointmentOut:
        try:
            appt = repo.cancel_appointment(
                session, appointment_id=appointment_id, reason=payload.reason
            )
        except repo.AppointmentNotFoundError as e:
            raise HTTPException(status_code=404, detail={"code": "appointment_not_found"}) from e
        return _appt_to_out(appt)

    @app.patch("/appointments/{appointment_id}", response_model=AppointmentOut)
    def reschedule_appointment(
        appointment_id: str,
        payload: AppointmentReschedule,
        session: Session = Depends(session_dep),
    ) -> AppointmentOut:
        # Pre-check the new slot exists so the 404 is distinguishable from the
        # "appointment not found" 404. The repository raises ``SlotTakenError``
        # only when the slot exists but is held by someone else; a missing
        # slot id would otherwise bubble up as the repository's generic
        # ``IntegrityError`` path on commit.
        if session.get(Slot, payload.new_slot_id) is None:
            raise HTTPException(status_code=404, detail={"code": "slot_not_found"})
        try:
            appt = repo.reschedule_appointment(
                session,
                appointment_id=appointment_id,
                new_slot_id=payload.new_slot_id,
            )
        except repo.AppointmentNotFoundError as e:
            raise HTTPException(status_code=404, detail={"code": "appointment_not_found"}) from e
        except repo.SlotTakenError as e:
            raise HTTPException(
                status_code=409,
                detail={"code": "slot_taken", "owner_patient_id": e.owner_patient_id},
            ) from e
        return _appt_to_out(appt)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/metrics", response_class=PlainTextResponse)
    def metrics(session: Session = Depends(session_dep)) -> str:
        """Hand-rolled Prometheus text exposition. ~20 lines, no extra deps."""
        patient_total = session.scalar(select(func.count()).select_from(Patient)) or 0
        slot_total = session.scalar(select(func.count()).select_from(Slot)) or 0
        appt_scheduled = (
            session.scalar(
                select(func.count())
                .select_from(Appointment)
                .where(Appointment.status == AppointmentStatus.SCHEDULED)
            )
            or 0
        )
        appt_cancelled = (
            session.scalar(
                select(func.count())
                .select_from(Appointment)
                .where(Appointment.status == AppointmentStatus.CANCELLED)
            )
            or 0
        )
        lines = [
            "# HELP ehr_patients_total Patient rows in the EHR.",
            "# TYPE ehr_patients_total gauge",
            f"ehr_patients_total {patient_total}",
            "# HELP ehr_slots_total Slot rows in the EHR.",
            "# TYPE ehr_slots_total gauge",
            f"ehr_slots_total {slot_total}",
            "# HELP ehr_appointments_total Appointment rows by status.",
            "# TYPE ehr_appointments_total gauge",
            f'ehr_appointments_total{{status="scheduled"}} {appt_scheduled}',
            f'ehr_appointments_total{{status="cancelled"}} {appt_cancelled}',
            "# HELP http_requests_total HTTP requests served, by path.",
            "# TYPE http_requests_total counter",
        ]
        for path, count in sorted(app.state.request_counts.items()):
            lines.append(f'http_requests_total{{path="{path}"}} {count}')
        return "\n".join(lines) + "\n"

    return app


app = create_app()
