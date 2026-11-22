"""FastAPI application: planning console + single-card simulator."""

from __future__ import annotations

import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from .batch import BatchError, BatchService
from .defaults import DEFAULT_LAYOUT, default_layout_json
from .models import (
    DriverDelayRequest,
    ExternalIdleSliceRequest,
    ExternalRemoveRequest,
    FaultRequest,
    LayoutImport,
    OrderRequest,
    PlanRequest,
)
from .simulator import SimError, Simulator
from .selftest import run_selftest

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="MIG 切片调整值班台", version="1.0.0")

# Sync endpoints run in a thread pool, so every state-touching request is
# serialised.  The console is a single-operator tool; this keeps the invariant
# checks in the batch service race-free without a full database transaction.
# The batch service shares this lock but sleeps between driver steps *outside*
# it, so reads stay live while a slow card is being changed.
LOCK = threading.RLock()

sim = Simulator(DEFAULT_LAYOUT)
service = BatchService(sim, guard=LOCK)


def state_payload() -> dict:
    # Snapshot under the lock so a read never catches the driver mid-mutation
    # (the lock is only held for one committed step at a time).
    with LOCK:
        return {
            "layout": sim.to_dict(),
            "batch": service.batch.to_dict(sim) if service.batch else None,
            "busy": service.busy,
            "default_layout": default_layout_json(),
        }


def handle(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except (BatchError, SimError) as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=_validation_detail(exc))


def ensure_driver_idle() -> None:
    """Refuse direct simulator writes while the driver is mid-run."""
    if service.busy:
        raise HTTPException(
            status_code=409,
            detail=service.busy_message(),
        )


def _validation_detail(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", ()))
        parts.append(f"{loc}: {err.get('msg')}" if loc else str(err.get("msg")))
    return "；".join(parts) or "输入不合法"


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse((BASE_DIR / "templates" / "index.html").read_text("utf-8"))


@app.get("/api/state")
def get_state() -> dict:
    return state_payload()


@app.post("/api/layout/import")
def import_layout(layout: LayoutImport) -> dict:
    global sim, service
    with LOCK:
        ensure_driver_idle()
        if service.batch and service.batch.state in ("applying", "failed"):
            raise HTTPException(
                status_code=409,
                detail="当前批次尚未结束，先重试或放弃后才能重新导入布局",
            )
        sim = Simulator(layout)
        sim.set_step_delay(_previous_delay())
        service = BatchService(sim, guard=LOCK)
        return state_payload()


def _previous_delay() -> int:
    # The simulated card's knobs are simulator-wide, not per-layout: keep the
    # operator's chosen step delay across a reset/import.
    return getattr(service.sim, "step_delay_ms", 0)


@app.post("/api/layout/reset")
def reset_layout(force: bool = True) -> dict:
    """Load the sample layout.

    Resetting is the operator's escape hatch, so by default it discards any
    in-flight batch (the whole simulator is replaced).  Pass ``force=false``
    to keep the same protection as a normal import.
    """
    global sim, service
    with LOCK:
        ensure_driver_idle()
        if not force:
            return import_layout(DEFAULT_LAYOUT)
        delay = _previous_delay()
        sim = Simulator(DEFAULT_LAYOUT)
        sim.set_step_delay(delay)
        service = BatchService(sim, guard=LOCK)
        return state_payload()


@app.post("/api/plan")
def plan(payload: PlanRequest) -> dict:
    requests = [
        (item.spec.strip(), (item.name or item.spec).strip())
        for item in payload.new_slices
    ]
    # Called outside the console lock: the service takes it for its own short
    # critical sections.  A slow driver must never block /api/state.
    handle(service.plan, requests)
    return state_payload()


@app.post("/api/batch/confirm")
def confirm(hold: bool = False) -> dict:
    handle(service.confirm, hold=hold)
    return state_payload()


@app.post("/api/batch/apply")
def apply_held() -> dict:
    handle(service.apply_held)
    return state_payload()


@app.post("/api/batch/retry")
def retry() -> dict:
    handle(service.retry)
    return state_payload()


@app.post("/api/batch/abandon")
def abandon(rollback_drafts: bool = True) -> dict:
    handle(service.abandon, rollback_drafts=rollback_drafts)
    return state_payload()


@app.post("/api/order")
def order(payload: OrderRequest) -> dict:
    with LOCK:
        ensure_driver_idle()
        handle(sim.place_order, slice_id=payload.slice_id, force=payload.force)
        return state_payload()


@app.post("/api/fault")
def fault(payload: FaultRequest) -> dict:
    with LOCK:
        ensure_driver_idle()
        handle(
            sim.set_fault,
            mode=payload.mode,
            skip=payload.skip,
            message=payload.message,
            count=payload.count,
        )
        return state_payload()


@app.post("/api/fault/clear")
def clear_fault() -> dict:
    with LOCK:
        ensure_driver_idle()
        sim.clear_fault()
        return state_payload()


@app.post("/api/driver/delay")
def set_driver_delay(payload: DriverDelayRequest) -> dict:
    """Set the simulated driver's per-operation delay (slow-card drill)."""
    with LOCK:
        ensure_driver_idle()
        handle(sim.set_step_delay, payload.step_delay_ms)
        return state_payload()


@app.post("/api/external/add-idle")
def external_add(payload: ExternalIdleSliceRequest) -> dict:
    with LOCK:
        ensure_driver_idle()
        handle(sim.external_add_idle, payload.spec, payload.interval, payload.id)
        return state_payload()


@app.post("/api/external/remove")
def external_remove(payload: ExternalRemoveRequest) -> dict:
    with LOCK:
        ensure_driver_idle()
        handle(sim.external_remove, payload.slice_id)
        return state_payload()


@app.post("/api/selftest")
def selftest() -> dict:
    # Runs on throwaway simulators; no lock needed and none taken, so it can
    # never deadlock against a long-running batch operation.
    return run_selftest()


app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
