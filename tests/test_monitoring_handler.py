"""Tests for the ``monitoring:*`` control-plane task handler."""

import asyncio

import msgspec
import pytest
from _helpers import FAKE_DRIVER_NAME, _make_capabilities, _make_context, _make_task, register_fake_driver
from scietex.service import ValkeyWorkerConfig

from scietex.vfd_service.config import VFDServiceSettings, VFDSettings
from scietex.vfd_service.task_handlers import (
    MONITORING_START_TASK_NAME,
    MONITORING_STOP_TASK_NAME,
    MonitoringHandler,
    MonitoringResponse,
)
from scietex.vfd_service.vfd_worker import VFDWorker


def _make_worker(tmp_path) -> VFDWorker:
    """A worker with a real conf_dir and no remote-config handlers."""
    return VFDWorker(ValkeyWorkerConfig(service_name="test", conf_dir=str(tmp_path), remote_config_enabled=False))


async def _build(worker: VFDWorker, labels: list[str]) -> None:
    """Seed the registry with one fake drive per label."""
    await worker._build_drives(
        VFDServiceSettings(drives=[VFDSettings(label=label, driver=FAKE_DRIVER_NAME) for label in labels])
    )


def _handler(worker: VFDWorker) -> MonitoringHandler:
    return MonitoringHandler("monitoring", _make_context(), worker=worker)


async def _teardown(worker: VFDWorker) -> None:
    """Cancel and await any lingering poll tasks, then close drives."""
    tasks = list(worker._poll_tasks.values())
    await worker.close_drives()
    await asyncio.gather(*tasks, return_exceptions=True)


@pytest.fixture(autouse=True)
def _fake_driver(monkeypatch):
    """Advertise the shared fake driver for every test in this module."""
    register_fake_driver(monkeypatch)


@pytest.mark.asyncio
async def test_start_no_payload_starts_all(tmp_path) -> None:
    """An empty payload selects every drive; all labels are returned."""
    worker = _make_worker(tmp_path)
    await _build(worker, ["a", "b"])

    result = await _handler(worker).handle(
        _make_task(MONITORING_START_TASK_NAME, None),
        capabilities=_make_capabilities(),
    )

    assert result.status == "success"
    decoded = msgspec.json.decode(result.payload, type=MonitoringResponse)
    assert decoded.action == "start"
    assert decoded.labels == ["a", "b"]
    assert decoded.monitoring == {"a": True, "b": True}
    await _teardown(worker)


@pytest.mark.asyncio
async def test_start_with_label_starts_one(tmp_path) -> None:
    """A label payload starts only that drive."""
    worker = _make_worker(tmp_path)
    await _build(worker, ["a", "b"])

    result = await _handler(worker).handle(
        _make_task(MONITORING_START_TASK_NAME, {"label": "b"}),
        capabilities=_make_capabilities(),
    )

    assert result.status == "success"
    decoded = msgspec.json.decode(result.payload, type=MonitoringResponse)
    assert decoded.labels == ["b"]
    assert decoded.monitoring == {"a": False, "b": True}
    await _teardown(worker)


@pytest.mark.asyncio
async def test_stop_returns_affected_labels(tmp_path) -> None:
    """Stopping reports the labels that were actually monitoring."""
    worker = _make_worker(tmp_path)
    await _build(worker, ["a", "b"])
    worker.start_monitoring("a")

    result = await _handler(worker).handle(
        _make_task(MONITORING_STOP_TASK_NAME, None),
        capabilities=_make_capabilities(),
    )

    assert result.status == "success"
    decoded = msgspec.json.decode(result.payload, type=MonitoringResponse)
    assert decoded.action == "stop"
    assert decoded.labels == ["a"]
    assert decoded.monitoring == {"a": False, "b": False}
    await _teardown(worker)


@pytest.mark.asyncio
async def test_unknown_label_is_not_retryable(tmp_path) -> None:
    """An unknown label yields UNKNOWN_DRIVE and is not retried."""
    worker = _make_worker(tmp_path)
    await _build(worker, ["a"])

    result = await _handler(worker).handle(
        _make_task(MONITORING_START_TASK_NAME, {"label": "nope"}),
        capabilities=_make_capabilities(),
    )

    assert result.status == "error"
    assert result.error_code == "UNKNOWN_DRIVE"
    assert result.retryable is False
    await _teardown(worker)


@pytest.mark.asyncio
async def test_invalid_payload_is_rejected(tmp_path) -> None:
    """A payload that is not a MonitoringRequest is rejected before dispatch."""
    worker = _make_worker(tmp_path)
    await _build(worker, ["a"])

    result = await _handler(worker).handle(
        _make_task(MONITORING_START_TASK_NAME, {"bogus": 1}),
        capabilities=_make_capabilities(),
    )

    assert result.status == "error"
    assert result.error_code == "INVALID_PAYLOAD"
    assert result.retryable is False
    await _teardown(worker)


def test_handler_is_control_lane() -> None:
    """The monitoring handler is dispatched on the control lane."""
    assert MonitoringHandler.control is True
