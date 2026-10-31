"""Tests for the VFD telemetry payload structs."""

from datetime import timedelta

import pytest
from _helpers import FAKE_DRIVER_NAME, register_fake_driver
from scietex.hal.vfd.base.data import VFDParameters, VFDState
from scietex.service import ValkeyWorkerConfig

from scietex.vfd_service.config import VFDServiceSettings, VFDSettings
from scietex.vfd_service.telemetry import VFDServiceTelemetry, VFDTelemetry, build_telemetry
from scietex.vfd_service.vfd_worker import VFDWorker


def test_service_telemetry_defaults() -> None:
    """VFDServiceTelemetry defaults schema_version to 1 and timestamp to a tz-aware UTC instant."""
    telemetry = VFDServiceTelemetry(
        service="test",
        instance_id="abc",
        configured=False,
        gateway_host=None,
        gateway_port=None,
        drives=[],
    )

    assert telemetry.schema_version == 1
    assert telemetry.timestamp.tzinfo is not None  # timezone-aware
    assert telemetry.timestamp.utcoffset() == timedelta(0)  # UTC


def test_drive_telemetry_accepts_none_optionals() -> None:
    """VFDTelemetry accepts None for every optional reading field."""
    drive = VFDTelemetry(
        label="pump",
        driver="intek_spe_b_modbus",
        connected=False,
        monitoring=False,
        frequency_hz=None,
        frequency_percent=None,
        output_current=None,
        output_voltage=None,
        output_power=None,
        dc_bus_voltage=None,
        heatsink_temperature_c=None,
        motor_temperature_c=None,
        state=None,
        total_run_hours=None,
        total_power_on_hours=None,
        hours_source=None,
        last_reading_at=None,
        reading_count=0,
        error_count=0,
        last_error=None,
    )

    assert drive.frequency_hz is None
    assert drive.heatsink_temperature_c is None
    assert drive.state is None
    assert drive.total_run_hours is None
    assert drive.hours_source is None
    assert drive.reading_count == 0


@pytest.fixture(autouse=True)
def _fake_driver(monkeypatch):
    """Advertise the shared fake driver for every test in this module."""
    register_fake_driver(monkeypatch)


def _make_worker(tmp_path) -> VFDWorker:
    return VFDWorker(ValkeyWorkerConfig(service_name="test", conf_dir=str(tmp_path), remote_config_enabled=False))


@pytest.mark.asyncio
async def test_telemetry_reports_last_reading(tmp_path) -> None:
    """A drive with a reading reports every parameter field and the run state."""
    worker = _make_worker(tmp_path)
    await worker._build_drives(VFDServiceSettings(drives=[VFDSettings(label="a", driver=FAKE_DRIVER_NAME)]))
    worker.stats["a"].last_parameters = VFDParameters(
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
    worker.stats["a"].reading_count = 3

    telemetry = build_telemetry(worker)

    drive = telemetry.drives[0]
    assert drive.frequency_hz == 25.0
    assert drive.frequency_percent == 50.0
    assert drive.output_current == 1.5
    assert drive.dc_bus_voltage == 540.0
    assert drive.heatsink_temperature_c == 41.0
    assert drive.motor_temperature_c is None
    assert drive.state == "RUNNING_FORWARD"
    assert drive.reading_count == 3
    await worker.close_drives()


@pytest.mark.asyncio
async def test_telemetry_without_reading_reports_none(tmp_path) -> None:
    """A drive that has never been read reports None for every reading field."""
    worker = _make_worker(tmp_path)
    await worker._build_drives(VFDServiceSettings(drives=[VFDSettings(label="a", driver=FAKE_DRIVER_NAME)]))

    telemetry = build_telemetry(worker)

    drive = telemetry.drives[0]
    assert drive.frequency_hz is None
    assert drive.frequency_percent is None
    assert drive.state is None
    assert drive.last_reading_at is None
    assert drive.total_run_hours is None
    assert drive.hours_source is None
    await worker.close_drives()


@pytest.mark.asyncio
async def test_telemetry_prefers_the_drive_counters(tmp_path) -> None:
    """A drive that reports its own counters wins over the software accumulation."""
    worker = _make_worker(tmp_path)
    await worker._build_drives(VFDServiceSettings(drives=[VFDSettings(label="a", driver=FAKE_DRIVER_NAME)]))
    worker.stats["a"].last_parameters = VFDParameters(
        frequency_hz=25.0,
        frequency_percent=50.0,
        total_run_hours=120.5,
        total_power_on_hours=400.0,
    )
    worker.stats["a"].accumulated_run_seconds = 3600.0
    worker.stats["a"].accumulated_power_on_seconds = 7200.0

    drive = build_telemetry(worker).drives[0]

    assert drive.total_run_hours == 120.5
    assert drive.total_power_on_hours == 400.0
    assert drive.hours_source == "drive"
    await worker.close_drives()


@pytest.mark.asyncio
async def test_telemetry_falls_back_to_the_accumulation(tmp_path) -> None:
    """A drive with no counters reports the worker's accumulated hours."""
    worker = _make_worker(tmp_path)
    await worker._build_drives(VFDServiceSettings(drives=[VFDSettings(label="a", driver=FAKE_DRIVER_NAME)]))
    worker.stats["a"].last_parameters = VFDParameters(frequency_hz=25.0, frequency_percent=50.0)
    worker.stats["a"].accumulated_run_seconds = 5400.0
    worker.stats["a"].accumulated_power_on_seconds = 10800.0

    drive = build_telemetry(worker).drives[0]

    assert drive.total_run_hours == pytest.approx(1.5)
    assert drive.total_power_on_hours == pytest.approx(3.0)
    assert drive.hours_source == "accumulated"
    await worker.close_drives()
