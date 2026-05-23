"""Tests for job cancellation (停止轉換)."""
from __future__ import annotations

import time

from fastapi.testclient import TestClient

from app.core.job_manager import JobManager
from app.main import app


def test_cancel_marks_status_and_aborts_at_checkpoint():
    jm = JobManager(workers=2)
    seen = {"started": False, "ran_to_end": False}

    def slow(job):
        seen["started"] = True
        for _ in range(40):
            if job.cancelled:  # checkpoint
                return
            time.sleep(0.02)
        seen["ran_to_end"] = True

    job = jm.submit("test", slow)
    time.sleep(0.1)  # let it start
    assert jm.cancel(job.id) is True
    assert job.status == "cancelled"
    assert job.cancelled is True
    time.sleep(0.4)  # let the worker hit the checkpoint and abort
    assert job.status == "cancelled"
    assert seen["ran_to_end"] is False  # aborted, did not finish


def test_cancel_returns_false_for_finished_job():
    jm = JobManager(workers=2)
    job = jm.submit("test", lambda j: None)
    time.sleep(0.2)
    assert job.status == "done"
    assert jm.cancel(job.id) is False  # already finished → cannot cancel


def test_cancel_endpoint_unknown_job_returns_404():
    client = TestClient(app)
    r = client.post("/api/jobs/nonexistent-id/cancel")
    assert r.status_code == 404


def test_cancel_endpoint_marks_running_job():
    # Submit a real job through the shared manager, then cancel via HTTP.
    from app.core.job_manager import job_manager

    def slow(job):
        for _ in range(40):
            if job.cancelled:
                return
            time.sleep(0.02)

    job = job_manager.submit("test", slow)
    time.sleep(0.1)
    client = TestClient(app)
    r = client.post(f"/api/jobs/{job.id}/cancel")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["status"] == "cancelled"
