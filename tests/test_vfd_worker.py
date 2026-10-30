"""Tests for the VFDWorker registry lifecycle and poll loop."""

import asyncio
import logging
from importlib.metadata import EntryPoint

import msgspec
import pytest
from pymodbus.exceptions import ConnectionException
from scietex.hal.serial import ModbusOperationError
from scietex.hal.vfd.base.capabilities import VFDCapabilities
from scietex.hal.vfd.base.data import VFDParameters, VFDState
from scietex.hal.vfd.base.protocol import VFDProtocol
from scietex.service import ValkeyWorker, ValkeyWorkerConfig

from scietex.vfd_service import vfd_worker as worker_module
from scietex.vfd_service.config import (
    VFD_CONFIG_FILE,
    VFD_CONFIG_SUBDIR,
    ModbusGatewaySettings,
    VFDServiceSettings,
    VFDSettings,
)
from scietex.vfd_service.telemetry import build_telemetry
from scietex.vfd_service.vfd_worker import (
    CONNECTED_FAILURE_THRESHOLD,
    DRIVER_ENTRY_POINT_GROUP,
    STREAM_MAXLEN,
    VFDWorker,
    resolve_driver,
)

#: Entry point name the fake driver is registered under for the test session.
_DRIVER_NAME = "test_fake_vfd"

#: A module-level non-class, used to exercise the "resolved to a non-class" path.
_NOT_A_CLASS = "just a string, not a class"


@pytest.fixture(autouse=True)
def _register_fake_driver(monkeypatch):
    """Advertise the in-module fake driver as the only entry point.

    The worker resolves drivers from installed distribution metadata, which a
    test module cannot contribute to. Patching ``entry_points`` keeps the
    resolution path under test without installing a throwaway distribution.
    """
    entry_point = EntryPoint(
        name=_DRIVER_NAME,
        value=f"{__name__}:_FakeVFD",
        group=DRIVER_ENTRY_POINT_GROUP,
    )
    monkeypatch.setattr(worker_module, "entry_points", lambda group: [entry_point])


class _FakeVFD(VFDProtocol):
    """A `VFDProtocol` fake with configurable behaviour."""

    capabilities = VFDCapabilities()
    display_name = "Fake VFD"

    def __init__(self, host, port=502, unit_id=1, label="VFD", timeout=1.0, logger=None, **kwargs):
        self.label = label
        self.unit_id = unit_id
        self.host = host
        self.port = port
        self.timeout = timeout
        self.logger = logger
        self.extra_kwargs = kwargs
        self.connect_calls = 0
        self.close_calls = 0
        self.read_calls = 0
        self.parameters = VFDParameters(frequency_hz=25.0, frequency_percent=50.0)
        self.read_exc = None
        self.connect_exc = None

    async def connect(self):
        self.connect_calls += 1
        if self.connect_exc is not None:
            raise self.connect_exc

    async def close(self):
        self.close_calls += 1

    async def read_data(self):
        return {field: getattr(self.parameters, field) for field in self.parameters.__struct_fields__}

    async def read_parameters(self):
        self.read_calls += 1
        if self.read_exc is not None:
            raise self.read_exc
        return self.parameters

    async def read_state(self):
        return self.parameters.state

    async def read_error_code(self, comm: bool = False):
        return 0

    async def clear_error(self, comm: bool = False):
        return 0

    async def start(self, forward: bool = True, slow: bool = False):
        return None

    async def stop(self, freewheel: bool = False):
        return None

    async def set_frequency(self, frequency_hz: float):
        return None


def _make_worker(tmp_path) -> VFDWorker:
    """Build a worker with a real conf_dir and no remote-config handlers."""
    return VFDWorker(
        ValkeyWorkerConfig(
            service_name="test",
            conf_dir=str(tmp_path),
            remote_config_enabled=False,
        )
    )


def _write_config(tmp_path, drives, gateway=None) -> None:
    """Write a vfd.yml with the given drives and gateway."""
    path = tmp_path / VFD_CONFIG_SUBDIR / VFD_CONFIG_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        msgspec.yaml.encode(
            VFDServiceSettings(
                gateway=gateway if gateway is not None else ModbusGatewaySettings(),
                drives=drives,
            )
        )
    )


def _write_raw(tmp_path, text: str) -> None:
    """Write raw text to vfd.yml (for malformed-file tests)."""
    path = tmp_path / VFD_CONFIG_SUBDIR / VFD_CONFIG_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _drive(worker: VFDWorker, label: str) -> _FakeVFD:
    """The live fake drive instance for ``label`` (the registry is live)."""
    return worker.drives[label]


def test_resolve_driver_resolves_class() -> None:
    """An advertised entry point name resolves to the driver class."""
    assert resolve_driver(_DRIVER_NAME) is _FakeVFD


def test_resolve_driver_unknown_name() -> None:
    """A name no entry point advertises raises ValueError."""
    with pytest.raises(ValueError, match="Unknown driver"):
        resolve_driver("no_such_driver")


def test_resolve_driver_not_a_class(monkeypatch) -> None:
    """An entry point resolving to a non-class raises ValueError."""
    entry_point = EntryPoint(
        name="not_a_class",
        value=f"{__name__}:_NOT_A_CLASS",
        group=DRIVER_ENTRY_POINT_GROUP,
    )
    monkeypatch.setattr(
        worker_module,
        "entry_points",
        lambda group: [entry_point],
    )
    with pytest.raises(ValueError, match="not a class"):
        resolve_driver("not_a_class")


@pytest.mark.asyncio
async def test_initialize_fails_on_unparseable_yaml(tmp_path, caplog) -> None:
    """An unparseable vfd.yml fails startup with an error log."""
    _write_raw(tmp_path, "unterminated: [flow\n")
    worker = _make_worker(tmp_path)

    with caplog.at_level(logging.ERROR):
        assert await worker.initialize() is False

    assert any("Failed to read VFD configuration" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_initialize_fails_on_bad_driver(tmp_path, caplog) -> None:
    """A drive whose driver name cannot be resolved fails startup."""
    _write_config(tmp_path, [VFDSettings(label="x", driver="nonexistent_driver")])
    worker = _make_worker(tmp_path)

    with caplog.at_level(logging.ERROR):
        assert await worker.initialize() is False

    assert any("Failed to build VFD registry" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_initialize_fails_on_duplicate_labels(tmp_path, caplog) -> None:
    """Two drives sharing a label fail startup (labels are the registry key)."""
    _write_config(
        tmp_path,
        [
            VFDSettings(label="dup", driver=_DRIVER_NAME),
            VFDSettings(label="dup", driver=_DRIVER_NAME),
        ],
    )
    worker = _make_worker(tmp_path)

    with caplog.at_level(logging.ERROR):
        assert await worker.initialize() is False

    assert any("Duplicate drive label" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_initialize_succeeds_with_empty_drives(tmp_path, monkeypatch) -> None:
    """An empty drive list is a valid state and initializes successfully."""

    async def fake_initialize(self) -> bool:
        return True

    monkeypatch.setattr(ValkeyWorker, "initialize", fake_initialize)
    _write_config(tmp_path, [])
    worker = _make_worker(tmp_path)

    assert await worker.initialize() is True
    assert worker.drives == {}
    assert worker.drive_settings == {}


@pytest.mark.asyncio
async def test_initialize_builds_registry_from_yaml(tmp_path, monkeypatch) -> None:
    """With no remote config, the registry is seeded from vfd.yml."""

    async def fake_initialize(self) -> bool:
        return True

    monkeypatch.setattr(ValkeyWorker, "initialize", fake_initialize)
    _write_config(tmp_path, [VFDSettings(label="pump", driver=_DRIVER_NAME, unit_id=7)])
    worker = _make_worker(tmp_path)

    assert await worker.initialize() is True
    assert set(worker.drives) == {"pump"}
    assert worker.drive_settings["pump"].unit_id == 7
    assert worker.vfd_settings is not None
    assert worker.vfd_settings.drives[0].label == "pump"
    await worker.cleanup()


@pytest.mark.asyncio
async def test_initialize_rebuilds_from_merged_settings(tmp_path, monkeypatch) -> None:
    """The merged section replaces the YAML list wholesale, not by label.

    Approach: monkeypatch ``ValkeyWorker.initialize`` to skip the live connect,
    and monkeypatch the worker's ``current_config_settings`` accessor to return
    a struct that differs from the YAML — the authoritative merged config.
    """

    async def fake_initialize(self) -> bool:
        return True

    monkeypatch.setattr(ValkeyWorker, "initialize", fake_initialize)
    _write_config(tmp_path, [VFDSettings(label="yaml", driver=_DRIVER_NAME)])
    worker = _make_worker(tmp_path)

    merged = VFDServiceSettings(
        gateway=ModbusGatewaySettings(host="remote", port=5020),
        drives=[VFDSettings(label="remote", driver=_DRIVER_NAME)],
    )
    monkeypatch.setattr(worker, "current_config_settings", lambda name: merged)

    assert await worker.initialize() is True
    assert set(worker.drives) == {"remote"}
    assert "yaml" not in worker.drives
    assert worker.vfd_settings.gateway.host == "remote"
    await worker.cleanup()


@pytest.mark.asyncio
async def test_max_frequency_passed_only_when_configured(tmp_path) -> None:
    """``max_frequency_hz`` reaches the driver only when the config sets it.

    A driver that reads its maximum from the drive itself must keep its own
    default, so the worker must not pass ``None`` explicitly.
    """
    worker = _make_worker(tmp_path)
    await worker._build_drives(
        VFDServiceSettings(
            drives=[
                VFDSettings(label="default", driver=_DRIVER_NAME),
                VFDSettings(label="explicit", driver=_DRIVER_NAME, max_frequency_hz=60.0),
            ]
        )
    )

    assert "max_frequency_hz" not in _drive(worker, "default").extra_kwargs
    assert _drive(worker, "explicit").extra_kwargs["max_frequency_hz"] == 60.0
    await worker.close_drives()


@pytest.mark.asyncio
async def test_start_monitoring_selects_drives(tmp_path) -> None:
    """start_monitoring starts one, all, and rejects unknown labels; no double-starts."""
    worker = _make_worker(tmp_path)
    await worker._build_drives(
        VFDServiceSettings(
            drives=[
                VFDSettings(label="a", driver=_DRIVER_NAME),
                VFDSettings(label="b", driver=_DRIVER_NAME),
            ]
        )
    )

    assert worker.start_monitoring("a") == ["a"]
    task_a = worker._poll_tasks["a"]
    assert worker.monitoring["a"] is True
    assert worker.monitoring["b"] is False

    # Already monitoring: not returned again.
    assert worker.start_monitoring("a") == []

    with pytest.raises(KeyError):
        worker.start_monitoring("unknown")

    # Start all: only the not-yet-monitoring labels are affected.
    assert worker.start_monitoring() == ["b"]
    task_b = worker._poll_tasks["b"]

    assert worker.stop_monitoring() == ["a", "b"]
    assert worker._poll_tasks == {}
    assert worker.monitoring == {"a": False, "b": False}
    await asyncio.gather(task_a, task_b, return_exceptions=True)
    await worker.close_drives()


@pytest.mark.asyncio
async def test_poll_loop_publishes_reading(tmp_path, monkeypatch) -> None:
    """One successful poll iteration writes the expected stream entry.

    Approach: monkeypatch ``_sleep`` to raise `asyncio.CancelledError` on the
    first call, so exactly one connect+read+publish cycle runs before the loop
    is stopped.
    """
    worker = _make_worker(tmp_path)
    await worker._build_drives(VFDServiceSettings(drives=[VFDSettings(label="a", driver=_DRIVER_NAME)]))
    _drive(worker, "a").parameters = VFDParameters(
        frequency_hz=25.0,
        frequency_percent=50.0,
        output_current=1.5,
        output_voltage=230.0,
        output_power=0.4,
        dc_bus_voltage=540.0,
        heatsink_temperature_c=41.0,
        motor_temperature_c=None,
        state=VFDState.RUNNING_FORWARD,
    )

    class FakeClient:
        def __init__(self):
            self.calls = []

        async def xadd(self, key, values, options=None):
            self.calls.append((key, values, options))

    fake = FakeClient()
    monkeypatch.setattr(worker, "_client", fake)

    async def cancel_after_one(_delay):
        raise asyncio.CancelledError

    monkeypatch.setattr(worker, "_sleep", cancel_after_one)

    with pytest.raises(asyncio.CancelledError):
        await worker._poll_drive("a")

    assert len(fake.calls) == 1
    key, values, options = fake.calls[0]
    assert key == f"scietex:{worker.service_name}:{worker.instance_id}:vfd:a:readings"
    fields = dict(values)
    assert list(fields) == [
        b"ts",
        b"frequency_hz",
        b"frequency_percent",
        b"output_current",
        b"output_voltage",
        b"output_power",
        b"dc_bus_voltage",
        b"heatsink_temperature_c",
        b"motor_temperature_c",
        b"state",
    ]
    assert fields[b"frequency_hz"] == b"25.0"
    assert fields[b"frequency_percent"] == b"50.0"
    assert fields[b"dc_bus_voltage"] == b"540.0"
    assert fields[b"heatsink_temperature_c"] == b"41.0"
    # A drive that does not report motor temperature encodes an empty value,
    # which is distinguishable from a reported 0.0.
    assert fields[b"motor_temperature_c"] == b""
    assert fields[b"state"] == b"RUNNING_FORWARD"
    assert options.trim.threshold == STREAM_MAXLEN
    assert options.trim.exact is False

    assert worker.stats["a"].reading_count == 1
    assert worker.stats["a"].last_parameters is not None
    assert worker.stats["a"].last_error is None
    await worker.close_drives()


@pytest.mark.asyncio
async def test_poll_loop_uses_configured_stream_maxlen(tmp_path, monkeypatch) -> None:
    """The readings stream retention comes from settings, not the module default."""
    worker = _make_worker(tmp_path)
    await worker._build_drives(
        VFDServiceSettings(
            drives=[VFDSettings(label="a", driver=_DRIVER_NAME)],
            stream_maxlen=250,
        )
    )

    class FakeClient:
        def __init__(self):
            self.calls = []

        async def xadd(self, key, values, options=None):
            self.calls.append((key, values, options))

    fake = FakeClient()
    monkeypatch.setattr(worker, "_client", fake)

    async def cancel_after_one(_delay):
        raise asyncio.CancelledError

    monkeypatch.setattr(worker, "_sleep", cancel_after_one)

    with pytest.raises(asyncio.CancelledError):
        await worker._poll_drive("a")

    assert fake.calls[0][2].trim.threshold == 250
    await worker.close_drives()


@pytest.mark.asyncio
async def test_poll_loop_survives_read_error(tmp_path, monkeypatch) -> None:
    """A read_parameters() exception increments error_count without killing the loop.

    Approach: monkeypatch ``_sleep`` to raise `asyncio.CancelledError` on the
    second call, so two read attempts run (each failing) before the loop is
    stopped — proving it survived the first failure.
    """
    worker = _make_worker(tmp_path)
    await worker._build_drives(VFDServiceSettings(drives=[VFDSettings(label="a", driver=_DRIVER_NAME)]))
    _drive(worker, "a").read_exc = RuntimeError("boom")

    calls = {"n": 0}

    async def cancel_after_two(_delay):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(worker, "_sleep", cancel_after_two)

    with pytest.raises(asyncio.CancelledError):
        await worker._poll_drive("a")

    assert worker.stats["a"].error_count == 2
    assert worker.stats["a"].last_error is not None
    assert worker.stats["a"].reading_count == 0
    await worker.close_drives()


@pytest.mark.asyncio
async def test_cleanup_cancels_tasks_and_closes_drives(tmp_path) -> None:
    """cleanup() cancels poll tasks and closes every drive."""
    worker = _make_worker(tmp_path)
    await worker._build_drives(VFDServiceSettings(drives=[VFDSettings(label="a", driver=_DRIVER_NAME)]))
    drive = _drive(worker, "a")
    worker.start_monitoring("a")
    task = worker._poll_tasks["a"]
    assert worker.monitoring["a"] is True

    await worker.cleanup()

    assert worker._poll_tasks == {}
    assert worker.monitoring["a"] is False
    assert drive.close_calls == 1
    await asyncio.gather(task, return_exceptions=True)


def test_build_telemetry_unconfigured(tmp_path) -> None:
    """An unconfigured worker reports configured=False with no drives and no gateway."""
    worker = _make_worker(tmp_path)

    telemetry = build_telemetry(worker)

    assert telemetry.service == "test"
    assert telemetry.instance_id == worker.instance_id
    assert telemetry.configured is False
    assert telemetry.gateway_host is None
    assert telemetry.gateway_port is None
    assert telemetry.drives == []


@pytest.mark.asyncio
async def test_build_telemetry_configured_sorted(tmp_path) -> None:
    """A configured worker reports drives sorted by label and the gateway endpoint."""
    worker = _make_worker(tmp_path)
    settings = VFDServiceSettings(
        gateway=ModbusGatewaySettings(host="gw", port=5020),
        drives=[
            VFDSettings(label="z", driver=_DRIVER_NAME),
            VFDSettings(label="a", driver=_DRIVER_NAME),
        ],
    )
    await worker._build_drives(settings)
    worker._settings = settings

    telemetry = build_telemetry(worker)

    assert telemetry.configured is True
    assert telemetry.gateway_host == "gw"
    assert telemetry.gateway_port == 5020
    assert [d.label for d in telemetry.drives] == ["a", "z"]
    await worker.close_drives()


def test_restart_required_transitions(tmp_path) -> None:
    """restart_required is False at startup and True after a post-startup apply."""
    worker = _make_worker(tmp_path)
    assert worker.restart_required is False

    # Pre-startup apply only stores; it does not flag a restart.
    worker._apply_vfd_settings(VFDServiceSettings())
    assert worker.restart_required is False

    worker._startup_complete = True
    worker._apply_vfd_settings(VFDServiceSettings())
    assert worker.restart_required is True


def test_handlers_registered_on_declared_lanes(tmp_path) -> None:
    """Both handlers register at construction, on their declared lanes.

    ``_handler_registry`` returns ``(registration_map, active_map)``; the first
    element is the registered-but-not-started map. ``MonitoringHandler`` declares
    ``control = True``, so it shares the control registry with the built-in
    ``CancelTaskHandler``; ``VFDControlHandler`` declares ``control = False``
    and lands in the data registry.
    """
    worker = _make_worker(tmp_path)

    monitoring_map, monitoring_active = worker._handler_registry("MonitoringHandler")
    assert "MonitoringHandler" in monitoring_map
    assert not monitoring_active

    vfd_map, vfd_active = worker._handler_registry("VFDControlHandler")
    assert "VFDControlHandler" in vfd_map
    assert not vfd_active

    control_map, _ = worker._handler_registry("CancelTaskHandler")
    assert monitoring_map is control_map
    assert vfd_map is not control_map


@pytest.mark.asyncio
async def test_poll_loop_clears_connected_after_failure_threshold(tmp_path, monkeypatch) -> None:
    """A drive that connects but cannot be read is reported as disconnected.

    ``connect()`` only opens the TCP socket to the gateway, so it succeeds for
    a unit id that no device answers. ``connected`` therefore tracks whether
    the drive answers, not the socket: setting it on connect would report such
    a drive as connected for the whole poll interval, between the successful
    connect and the failing read.
    """
    worker = _make_worker(tmp_path)
    await worker._build_drives(VFDServiceSettings(drives=[VFDSettings(label="a", driver=_DRIVER_NAME)]))
    drive = _drive(worker, "a")

    # One good read puts the drive in the connected state, then every read
    # fails. Record the flag as the loop sees it after each failed read, so the
    # threshold boundary is asserted rather than only the settled end state.
    observed: list[bool] = []
    reads = {"n": 0}

    async def read_parameters():
        reads["n"] += 1
        if reads["n"] == 1:
            return VFDParameters(frequency_hz=25.0, frequency_percent=50.0)
        raise RuntimeError("Read of register 0 failed: exception code 11")

    drive.read_parameters = read_parameters

    async def record_and_cancel(_delay):
        # The loop sleeps after a success too; only failed reads are of interest.
        if reads["n"] > 1:
            observed.append(worker.connected["a"])
        if len(observed) > CONNECTED_FAILURE_THRESHOLD:
            raise asyncio.CancelledError

    monkeypatch.setattr(worker, "_sleep", record_and_cancel)

    with pytest.raises(asyncio.CancelledError):
        await worker._poll_drive("a")

    # Below the threshold the drive stays connected; at the threshold it clears.
    assert observed[: CONNECTED_FAILURE_THRESHOLD - 1] == [True] * (CONNECTED_FAILURE_THRESHOLD - 1)
    assert observed[CONNECTED_FAILURE_THRESHOLD - 1] is False
    # One extra failed read runs before the cancel takes effect.
    assert worker.stats["a"].error_count == CONNECTED_FAILURE_THRESHOLD + 1
    assert worker.stats["a"].reading_count == 1
    # An exception response means the gateway answered, so the socket is still
    # open and the loop does not reconnect.
    assert drive.connect_calls == 1
    await worker.close_drives()


@pytest.mark.asyncio
async def test_poll_loop_reconnects_after_transport_failure(tmp_path, monkeypatch) -> None:
    """A read that fails because the socket died makes the loop reconnect.

    pymodbus closes the transport when a request times out, so the socket is
    unusable afterwards and every later read fails the same way. Without
    clearing the transport flag the loop retried a dead socket forever and only
    a service restart recovered it.
    """
    worker = _make_worker(tmp_path)
    await worker._build_drives(VFDServiceSettings(drives=[VFDSettings(label="a", driver=_DRIVER_NAME)]))
    drive = _drive(worker, "a")

    reads = {"n": 0}

    async def read_parameters():
        reads["n"] += 1
        if reads["n"] == 1:
            raise ConnectionException("Not connected")
        return VFDParameters(frequency_hz=25.0, frequency_percent=50.0)

    drive.read_parameters = read_parameters

    sleeps = {"n": 0}

    async def cancel_after_bound(_delay):
        sleeps["n"] += 1
        if worker.stats["a"].reading_count or sleeps["n"] > 5:
            raise asyncio.CancelledError

    monkeypatch.setattr(worker, "_sleep", cancel_after_bound)

    with pytest.raises(asyncio.CancelledError):
        await worker._poll_drive("a")

    # The failed read cleared the flag, so the next iteration reconnected.
    assert drive.connect_calls == 2
    assert worker.stats["a"].reading_count == 1
    assert worker.connected["a"] is True
    await worker.close_drives()


@pytest.mark.asyncio
async def test_poll_loop_keeps_transport_on_exception_response(tmp_path, monkeypatch) -> None:
    """A Modbus exception response does not trigger a reconnect.

    The gateway answered, so the socket is healthy; reconnecting on every
    exception response would tear down a working connection.
    """
    worker = _make_worker(tmp_path)
    await worker._build_drives(VFDServiceSettings(drives=[VFDSettings(label="a", driver=_DRIVER_NAME)]))
    drive = _drive(worker, "a")
    drive.read_exc = ModbusOperationError("Read of register 0 failed: exception code 11")

    sleeps = {"n": 0}

    async def cancel_after_bound(_delay):
        sleeps["n"] += 1
        if sleeps["n"] > 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(worker, "_sleep", cancel_after_bound)

    with pytest.raises(asyncio.CancelledError):
        await worker._poll_drive("a")

    assert drive.connect_calls == 1
    assert worker.stats["a"].error_count == 4
    await worker.close_drives()


@pytest.mark.asyncio
async def test_poll_loop_keeps_connected_below_failure_threshold(tmp_path, monkeypatch) -> None:
    """A single failed read does not flip the drive to disconnected.

    An RS485 segment drops the occasional frame, so one failure must not make
    the status icon flicker. The drive stays connected until the failures are
    consecutive and reach the threshold.
    """
    worker = _make_worker(tmp_path)
    await worker._build_drives(VFDServiceSettings(drives=[VFDSettings(label="a", driver=_DRIVER_NAME)]))
    drive = _drive(worker, "a")

    calls = {"n": 0}

    async def read_parameters():
        calls["n"] += 1
        # One good read, then a single failure, then good reads again.
        if calls["n"] == 2:
            raise RuntimeError("transient bus error")
        return VFDParameters(frequency_hz=25.0, frequency_percent=50.0)

    drive.read_parameters = read_parameters

    sleeps = {"n": 0}

    async def cancel_after_bound(_delay):
        sleeps["n"] += 1
        if sleeps["n"] > 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(worker, "_sleep", cancel_after_bound)

    with pytest.raises(asyncio.CancelledError):
        await worker._poll_drive("a")

    assert worker.connected["a"] is True
    await worker.close_drives()
