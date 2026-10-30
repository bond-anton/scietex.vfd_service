"""Tests for the ``vfd:*`` data-plane task handler."""

import msgspec
import pytest
from _helpers import FAKE_DRIVER_NAME, _FakeVFD, _make_capabilities, _make_context, _make_task, register_fake_driver
from scietex.hal.vfd.base.capabilities import VFDCapabilities
from scietex.hal.vfd.base.data import VFDParameters, VFDState
from scietex.service import ValkeyWorkerConfig

from scietex.vfd_service.config import VFDServiceSettings, VFDSettings
from scietex.vfd_service.task_handlers import (
    VFD_CLEAR_ERROR_TASK_NAME,
    VFD_READ_TASK_NAME,
    VFD_SET_FREQUENCY_TASK_NAME,
    VFD_START_TASK_NAME,
    VFD_STOP_TASK_NAME,
    VFDCommandResponse,
    VFDControlHandler,
)
from scietex.vfd_service.vfd_worker import VFDWorker

_ALL_TASK_NAMES = [
    VFD_READ_TASK_NAME,
    VFD_START_TASK_NAME,
    VFD_STOP_TASK_NAME,
    VFD_SET_FREQUENCY_TASK_NAME,
    VFD_CLEAR_ERROR_TASK_NAME,
]

#: A capability set with every optional control enabled.
_FULL_CAPABILITIES = VFDCapabilities(
    direction=True,
    jog=True,
    freewheel_stop=True,
    fault_reset=True,
    frequency_setpoint=True,
)


def _make_worker(tmp_path) -> VFDWorker:
    """A worker with a real conf_dir and no remote-config handlers."""
    return VFDWorker(ValkeyWorkerConfig(service_name="test", conf_dir=str(tmp_path), remote_config_enabled=False))


async def _build(worker: VFDWorker, labels: list[str]) -> None:
    """Seed the registry with one fake drive per label."""
    await worker._build_drives(
        VFDServiceSettings(drives=[VFDSettings(label=label, driver=FAKE_DRIVER_NAME) for label in labels])
    )


def _handler(worker: VFDWorker) -> VFDControlHandler:
    return VFDControlHandler("vfd", _make_context(), worker=worker)


def _drive(worker: VFDWorker, label: str = "a") -> _FakeVFD:
    return worker.drives[label]


@pytest.fixture(autouse=True)
def _fake_driver(monkeypatch):
    """Advertise the shared fake driver for every test in this module."""
    register_fake_driver(monkeypatch)


def test_handler_is_data_lane() -> None:
    """The VFD control handler is dispatched on the data lane."""
    assert VFDControlHandler.control is False


def test_supported_tasks_cover_every_task_name() -> None:
    """The handler declares exactly the five ``vfd:*`` task names."""
    handler = VFDControlHandler("vfd", _make_context(), worker=None)  # type: ignore[arg-type]
    assert handler.supported_tasks == _ALL_TASK_NAMES


@pytest.mark.asyncio
async def test_read_returns_parameters_and_fault(tmp_path) -> None:
    """``vfd:read`` returns every parameter field plus the decoded fault."""
    worker = _make_worker(tmp_path)
    await _build(worker, ["a"])
    drive = _drive(worker)
    drive.parameters = VFDParameters(
        frequency_hz=30.0,
        frequency_percent=60.0,
        output_current=2.5,
        output_voltage=380.0,
        output_power=1.2,
        dc_bus_voltage=530.0,
        heatsink_temperature_c=45.0,
        motor_temperature_c=None,
        state=VFDState.RUNNING_FORWARD,
    )
    drive.error_code = 7

    result = await _handler(worker).handle(
        _make_task(VFD_READ_TASK_NAME, {"label": "a"}),
        capabilities=_make_capabilities(),
    )

    assert result.status == "success"
    decoded = msgspec.json.decode(result.payload, type=VFDCommandResponse)
    assert decoded.label == "a"
    assert decoded.task == VFD_READ_TASK_NAME
    assert decoded.frequency_hz == 30.0
    assert decoded.frequency_percent == 60.0
    assert decoded.output_current == 2.5
    assert decoded.output_voltage == 380.0
    assert decoded.output_power == 1.2
    assert decoded.dc_bus_voltage == 530.0
    assert decoded.heatsink_temperature_c == 45.0
    assert decoded.motor_temperature_c is None
    assert decoded.state == "RUNNING_FORWARD"
    assert decoded.error_code == 7
    assert decoded.error_message == "Fault"
    await worker.close_drives()


@pytest.mark.asyncio
async def test_read_works_without_any_capability(tmp_path) -> None:
    """Reading is always supported, even for a drive declaring no capabilities."""
    worker = _make_worker(tmp_path)
    await _build(worker, ["a"])

    result = await _handler(worker).handle(
        _make_task(VFD_READ_TASK_NAME, {"label": "a"}),
        capabilities=_make_capabilities(),
    )

    assert result.status == "success"
    await worker.close_drives()


@pytest.mark.asyncio
async def test_start_passes_direction_and_jog(tmp_path) -> None:
    """``vfd:start`` forwards the direction and jog flags to the driver."""
    worker = _make_worker(tmp_path)
    await _build(worker, ["a"])
    _drive(worker).capabilities = _FULL_CAPABILITIES

    result = await _handler(worker).handle(
        _make_task(VFD_START_TASK_NAME, {"label": "a", "forward": False, "slow": True}),
        capabilities=_make_capabilities(),
    )

    assert result.status == "success"
    assert _drive(worker).start_calls == [(False, True)]
    await worker.close_drives()


@pytest.mark.asyncio
async def test_start_defaults_to_forward(tmp_path) -> None:
    """An omitted direction starts the motor forward."""
    worker = _make_worker(tmp_path)
    await _build(worker, ["a"])
    _drive(worker).capabilities = _FULL_CAPABILITIES

    await _handler(worker).handle(
        _make_task(VFD_START_TASK_NAME, {"label": "a"}),
        capabilities=_make_capabilities(),
    )

    assert _drive(worker).start_calls == [(True, False)]
    await worker.close_drives()


@pytest.mark.asyncio
async def test_stop_passes_freewheel(tmp_path) -> None:
    """``vfd:stop`` forwards the coasting flag to the driver."""
    worker = _make_worker(tmp_path)
    await _build(worker, ["a"])
    _drive(worker).capabilities = _FULL_CAPABILITIES

    result = await _handler(worker).handle(
        _make_task(VFD_STOP_TASK_NAME, {"label": "a", "freewheel": True}),
        capabilities=_make_capabilities(),
    )

    assert result.status == "success"
    assert _drive(worker).stop_calls == [True]
    await worker.close_drives()


@pytest.mark.asyncio
async def test_set_frequency_passes_hz(tmp_path) -> None:
    """``vfd:set-frequency`` forwards the value in Hz, unconverted."""
    worker = _make_worker(tmp_path)
    await _build(worker, ["a"])
    _drive(worker).capabilities = _FULL_CAPABILITIES

    result = await _handler(worker).handle(
        _make_task(VFD_SET_FREQUENCY_TASK_NAME, {"label": "a", "frequency_hz": 42.5}),
        capabilities=_make_capabilities(),
    )

    assert result.status == "success"
    decoded = msgspec.json.decode(result.payload, type=VFDCommandResponse)
    assert decoded.frequency_hz == 42.5
    assert _drive(worker).set_frequency_calls == [42.5]
    await worker.close_drives()


@pytest.mark.asyncio
async def test_set_frequency_requires_a_value(tmp_path) -> None:
    """A missing ``frequency_hz`` is rejected before the driver is called."""
    worker = _make_worker(tmp_path)
    await _build(worker, ["a"])
    _drive(worker).capabilities = _FULL_CAPABILITIES

    result = await _handler(worker).handle(
        _make_task(VFD_SET_FREQUENCY_TASK_NAME, {"label": "a"}),
        capabilities=_make_capabilities(),
    )

    assert result.status == "error"
    assert result.error_code == "INVALID_PARAMETERS"
    assert result.retryable is False
    assert _drive(worker).set_frequency_calls == []
    await worker.close_drives()


@pytest.mark.asyncio
async def test_clear_error_returns_the_code_after_reset(tmp_path) -> None:
    """``vfd:clear-error`` resets the fault and reports the code read back."""
    worker = _make_worker(tmp_path)
    await _build(worker, ["a"])
    drive = _drive(worker)
    drive.capabilities = _FULL_CAPABILITIES
    drive.error_code = 3

    result = await _handler(worker).handle(
        _make_task(VFD_CLEAR_ERROR_TASK_NAME, {"label": "a"}),
        capabilities=_make_capabilities(),
    )

    assert result.status == "success"
    decoded = msgspec.json.decode(result.payload, type=VFDCommandResponse)
    assert decoded.error_code == 0
    assert decoded.error_message == "No error"
    assert drive.clear_error_calls == [False]
    await worker.close_drives()


@pytest.mark.asyncio
async def test_clear_error_forwards_comm_flag(tmp_path) -> None:
    """The ``comm`` flag selects the communication fault table."""
    worker = _make_worker(tmp_path)
    await _build(worker, ["a"])
    drive = _drive(worker)
    drive.capabilities = _FULL_CAPABILITIES

    await _handler(worker).handle(
        _make_task(VFD_CLEAR_ERROR_TASK_NAME, {"label": "a", "comm": True}),
        capabilities=_make_capabilities(),
    )

    assert drive.clear_error_calls == [True]
    await worker.close_drives()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("task", "payload", "capability"),
    [
        (VFD_START_TASK_NAME, {"label": "a"}, "direction"),
        (VFD_STOP_TASK_NAME, {"label": "a"}, "freewheel_stop"),
        (VFD_SET_FREQUENCY_TASK_NAME, {"label": "a", "frequency_hz": 10.0}, "frequency_setpoint"),
        (VFD_CLEAR_ERROR_TASK_NAME, {"label": "a"}, "fault_reset"),
    ],
)
async def test_unsupported_capability_is_rejected(tmp_path, task, payload, capability) -> None:
    """A drive missing the task's capability rejects it without reaching the driver.

    The capability is cleared on an otherwise fully capable drive, so the
    rejection is attributable to that one field rather than to the empty tree.
    """
    worker = _make_worker(tmp_path)
    await _build(worker, ["a"])
    drive = _drive(worker)
    drive.capabilities = VFDCapabilities(
        direction=capability != "direction",
        jog=True,
        freewheel_stop=capability != "freewheel_stop",
        fault_reset=capability != "fault_reset",
        frequency_setpoint=capability != "frequency_setpoint",
    )

    result = await _handler(worker).handle(_make_task(task, payload), capabilities=_make_capabilities())

    assert result.status == "error"
    assert result.error_code == "UNSUPPORTED"
    assert result.retryable is False
    assert drive.start_calls == []
    assert drive.stop_calls == []
    assert drive.set_frequency_calls == []
    assert drive.clear_error_calls == []
    await worker.close_drives()


@pytest.mark.asyncio
async def test_unknown_label_is_rejected(tmp_path) -> None:
    """An unknown label yields UNKNOWN_DRIVE and is not retried."""
    worker = _make_worker(tmp_path)
    await _build(worker, ["a"])

    result = await _handler(worker).handle(
        _make_task(VFD_READ_TASK_NAME, {"label": "nope"}),
        capabilities=_make_capabilities(),
    )

    assert result.status == "error"
    assert result.error_code == "UNKNOWN_DRIVE"
    assert result.retryable is False
    await worker.close_drives()


@pytest.mark.asyncio
async def test_invalid_payload_is_rejected(tmp_path) -> None:
    """A payload that is not a VFDCommandRequest is rejected before dispatch."""
    worker = _make_worker(tmp_path)
    await _build(worker, ["a"])

    result = await _handler(worker).handle(
        _make_task(VFD_READ_TASK_NAME, {"bogus": 1}),
        capabilities=_make_capabilities(),
    )

    assert result.status == "error"
    assert result.error_code == "INVALID_PAYLOAD"
    assert result.retryable is False
    await worker.close_drives()


@pytest.mark.asyncio
async def test_driver_failure_is_retryable(tmp_path) -> None:
    """A driver exception is reported as a retryable VFD_COMMAND_FAILED."""
    worker = _make_worker(tmp_path)
    await _build(worker, ["a"])
    drive = _drive(worker)
    drive.capabilities = _FULL_CAPABILITIES
    drive.control_exc = RuntimeError("gateway timeout")

    result = await _handler(worker).handle(
        _make_task(VFD_START_TASK_NAME, {"label": "a"}),
        capabilities=_make_capabilities(),
    )

    assert result.status == "error"
    assert result.error_code == "VFD_COMMAND_FAILED"
    assert result.retryable is True
    assert "gateway timeout" in result.error
    await worker.close_drives()
