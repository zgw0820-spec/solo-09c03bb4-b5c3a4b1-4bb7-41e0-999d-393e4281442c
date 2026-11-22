"""Slow-driver drill: per-step delay, live reads, busy gate, late failure.

These tests use a fake ``sleep`` so they are deterministic and fast.  What is
asserted mirrors the operator drill described in the README:

* while the driver is stepping, /api/state (or the service's read view) shows
  the changes that already landed and the steps still pending;
* a second page's mutating call (repeat run, abandon, external add/remove,
  import, reset) is refused with a busy error;
* a driver that errors *after* changing the card exposes the change plus the
  failure reason, and retry only fills in what is missing.
"""

import threading
import time

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.batch import BatchError, BatchService
from app.defaults import DEFAULT_LAYOUT, SELFTEST_LAYOUT
from app.simulator import Simulator


class GatedSleep:
    """Replacement for time.sleep that parks the driver on a chosen step.

    The driver sleeps *before* each operation, so blocking on call N means
    operations 1..N-1 have already been committed to the card.
    """

    def __init__(self, block_on: int = 1):
        self.block_on = block_on
        self.calls = 0
        self.entered = threading.Event()
        self.release = threading.Event()

    def __call__(self, seconds: float) -> None:
        self.calls += 1
        if self.calls == self.block_on:
            self.entered.set()
            assert self.release.wait(5), "test did not release the driver"


def _service(gated: GatedSleep | None = None):
    sim = Simulator(SELFTEST_LAYOUT)
    if gated is None:
        return sim, BatchService(sim)
    sim.set_step_delay(100)  # make the driver call the fake sleep
    return sim, BatchService(sim, driver_sleep=gated)


def _run_in_thread(svc, fn, *args, **kwargs):
    out = {}

    def target():
        try:
            out["result"] = fn(*args, **kwargs)
        except Exception as exc:  # pragma: no cover - surfaced by assertion
            out["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread, out


# ------------------------------------------------------------------- driver
def test_step_delay_setting_is_exposed_and_validated():
    sim, _ = _service()
    assert sim.to_dict()["driver"]["step_delay_ms"] == 0
    sim.set_step_delay(350)
    assert sim.to_dict()["driver"]["step_delay_ms"] == 350
    with pytest.raises(Exception):
        sim.set_step_delay(-1)


def test_delay_is_slept_per_operation_not_before_the_batch():
    """Each op costs one delay; the last op is followed only by verification."""
    sim = Simulator(SELFTEST_LAYOUT)
    slept: list[float] = []
    service = BatchService(sim, driver_sleep=slept.append)
    sim.set_step_delay(250)
    batch = service.plan([("1g.10gb", "a"), ("1g.10gb", "b")])
    service.confirm()
    expected_ops = len(batch.plan.removals) + 2
    assert len(slept) == expected_ops
    assert slept == [0.25] * expected_ops


def test_zero_delay_still_drives_to_delivery():
    sim, service = _service()
    service.plan([("1g.10gb", "a"), ("1g.10gb", "b")])
    service.confirm()
    assert service.batch.state == "delivered"
    assert service.batch.progress["done"] == service.batch.progress["total"]


# ------------------------------------------------- live view during a run
def test_second_page_sees_partial_layout_and_pending_steps_mid_run():
    # Block on the *second* step: step 1 (the removal) is already on the card.
    gated = GatedSleep(block_on=2)
    sim, service = _service(gated)
    batch = service.plan([("1g.10gb", "a"), ("1g.10gb", "b")])
    expected_ops = len(batch.plan.removals) + 2
    assert expected_ops == 3

    thread, out = _run_in_thread(service, service.confirm)
    try:
        assert gated.entered.wait(5), "driver never reached step 2"
        assert service.busy is True
        # A read while the driver is parked must return immediately.
        view = service.batch.to_dict(sim)
        assert view["driver"]["running"] is True
        assert view["driver"]["total"] == expected_ops
        assert view["driver"]["done"] == 1
        assert view["driver"]["ops"]
        assert view["driver"]["step_delay_ms"] == 100
        # The removal has taken effect, the two creates are still missing.
        assert view["actual"]["remaining_removals"][0]["state"] == "removed"
        assert [c["state"] for c in view["actual"]["created"]] == ["missing", "missing"]
        assert view["actual"]["layout_matches_plan"] is False
        # Delivered slices are the only ones that accept orders; before that
        # the batch owns them.
        assert all(slc.can_accept is False for slc in sim.slices.values()
                   if slc.batch_id == batch.id)
    finally:
        gated.release.set()
        thread.join(5)
    assert out.get("result") is not None
    assert service.busy is False
    assert service.batch.state == "delivered"
    assert service.batch.progress["done"] == expected_ops


def test_finished_progress_reports_last_step_and_no_pending_work():
    sim, service = _service()
    service.plan([("1g.10gb", "a"), ("1g.10gb", "b")])
    service.confirm()
    progress = service.batch.progress
    assert progress["running"] is False
    assert progress["done"] == progress["total"]
    assert progress["finished_at"]
    assert progress["current"]  # last applied step is described


# --------------------------------------------------------------- busy gate
def test_busy_refuses_repeat_abandon_and_plan():
    gated = GatedSleep()
    sim, service = _service(gated)
    service.plan([("1g.10gb", "a"), ("1g.10gb", "b")])
    thread, out = _run_in_thread(service, service.confirm)
    try:
        assert gated.entered.wait(5)
        assert service.busy is True
        for fn in (
            lambda: service.apply_held(),
            lambda: service.retry(),
            lambda: service.abandon(),
            lambda: service.plan([("2g.20gb", "x"), ("2g.20gb", "y")]),
            lambda: service.confirm(),
        ):
            with pytest.raises(BatchError, match="驱动正在执行"):
                fn()
        # The failed attempts must not have disturbed the live batch.
        assert service.batch.state == "applying"
        assert service.batch.progress["running"] is True
    finally:
        gated.release.set()
        thread.join(5)
    assert out.get("result") is not None
    assert service.batch.state == "delivered"


# ------------------------------------------------------ late driver failure
def test_failure_after_partial_apply_exposes_change_and_reason_then_retry():
    sim, service = _service()
    batch = service.plan([("1g.10gb", "a"), ("1g.10gb", "b")])
    planned = [p.slice_id for p in batch.plan.placements]
    removal = batch.plan.removals[0].slice_id
    sim.set_fault(mode="after_apply", skip=1, count=1, message="NVML: device busy")

    service.confirm()
    assert service.batch.state == "failed"
    assert "device busy" in service.batch.error
    view = service.batch.to_dict(sim)
    # Refresh view: step 1 (removal) and step 2 (first create) took effect
    # before the error, so exactly the last create is still missing.
    assert view["actual"]["remaining_removals"][0]["state"] == "removed"
    missing = [c["slice_id"] for c in view["actual"]["created"] if c["state"] == "missing"]
    assert len(missing) == 1
    assert view["actual"]["layout_matches_plan"] is False
    assert view["driver"]["running"] is False
    assert view["driver"]["done"] == 2
    assert view["driver"]["total"] == 3

    service.retry()
    assert service.batch.state == "delivered"
    assert sim.get(removal) is None
    assert all(sim.get(i) is not None for i in planned)
    # Retry only re-ran the one missing step: not the removal, nor the create
    # that had already landed.
    assert service.batch.progress["total"] == 1


# ----------------------------------------------------------------- HTTP API
@pytest.fixture()
def client():
    main_module.sim = main_module.Simulator(DEFAULT_LAYOUT)
    main_module.service = main_module.BatchService(main_module.sim)
    with TestClient(main_module.app) as c:
        yield c


def plan_body():
    return {"new_slices": [{"spec": "1g.10gb"}, {"spec": "1g.10gb"}]}


def test_delay_endpoint_round_trip(client):
    data = client.post("/api/driver/delay", json={"step_delay_ms": 300}).json()
    assert data["layout"]["driver"]["step_delay_ms"] == 300
    data = client.post("/api/driver/delay", json={"step_delay_ms": 0}).json()
    assert data["layout"]["driver"]["step_delay_ms"] == 0
    assert client.post("/api/driver/delay", json={"step_delay_ms": -5}).status_code == 422


def test_http_busy_conflict_from_second_page(client, monkeypatch):
    """A second page's mutations are 409 while the driver is mid-run."""
    client.post("/api/plan", json=plan_body())
    main_module.sim.set_step_delay(300)

    holder = main_module.service

    # Simulate the busy flag from a concurrent request (the real one would be
    # inside a live driver thread).
    original = holder._busy
    holder._busy = True
    try:
        for path, body in (
            ("/api/batch/confirm", None),
            ("/api/batch/retry", None),
            ("/api/batch/abandon", None),
            ("/api/external/add-idle", {"spec": "1g.10gb", "interval": {"start": 7, "end": 7}}),
            ("/api/external/remove", {"slice_id": "idle-2"}),
            ("/api/order", {"slice_id": "idle-2"}),
            ("/api/plan", plan_body()),
            ("/api/layout/reset", None),
            ("/api/layout/import", main_module.default_layout_json()),
            ("/api/driver/delay", {"step_delay_ms": 0}),
            ("/api/fault", {"mode": "before_apply", "skip": 0, "count": 1}),
        ):
            res = client.post(path, json=body) if body is not None else client.post(path)
            assert res.status_code == 409, f"{path} -> {res.status_code}"
            assert "驱动正在执行" in res.json()["detail"]
        # Reads stay available.
        assert client.get("/api/state").status_code == 200
        assert client.get("/api/state").json()["busy"] is True
    finally:
        holder._busy = original
    assert client.get("/api/state").json()["busy"] is False


def test_http_slow_driver_live_state_then_failure_and_retry(client):
    """End-to-end over HTTP with a real (short) delay and an after_apply fault."""
    client.post("/api/plan", json=plan_body())
    client.post("/api/driver/delay", json={"step_delay_ms": 20})
    client.post(
        "/api/fault",
        json={"mode": "after_apply", "skip": 1, "count": 1, "message": "NVML: busy"},
    )

    seen_busy = []
    with TestClient(main_module.app, raise_server_exceptions=True) as live:
        import threading as _t

        out = {}

        def drive():
            # Use the same client's sync endpoint from a worker thread.
            out["res"] = client.post("/api/batch/confirm")

        worker = _t.Thread(target=drive, daemon=True)
        worker.start()
        while worker.is_alive():
            payload = live.get("/api/state").json()
            if payload["busy"]:
                seen_busy.append(payload["batch"]["driver"]["done"])
            time.sleep(0.01)
        worker.join(5)

    assert seen_busy, "never observed the busy state from another request"
    assert max(seen_busy) >= 1, f"progress never advanced mid-run: {seen_busy}"
    data = out["res"].json()
    assert data["batch"]["state"] == "failed"
    assert "NVML: busy" in data["batch"]["error"]
    assert data["batch"]["driver"]["running"] is False
    assert data["batch"]["actual"]["layout_matches_plan"] is False

    retried = client.post("/api/batch/retry").json()
    assert retried["batch"]["state"] == "delivered"
    assert retried["batch"]["actual"]["layout_matches_plan"] is True
