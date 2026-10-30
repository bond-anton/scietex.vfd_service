"""Telemetry payloads the VFD worker publishes to Valkey.

The structs mirror the API read side field-for-field so the monitoring endpoint
can decode the payload unchanged, with one addition: this producer carries a
``configured`` flag the API struct omits (the API decoder tolerates unknown
fields, so it is ignored there). The key shape, push interval, and TTL are the
frozen contract: key ``scietex:{service}:{instance_id}:telemetry`` (the
``telemetry`` suffix never matches the heartbeat discovery glob
``scietex:*:*:status``), pushed every 2 s with a 6 s TTL.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

import msgspec

if TYPE_CHECKING:
    from .vfd_worker import VFDWorker

#: Push cadence and key TTL: 2 s interval, 6 s TTL so a single missed beat does
#: not expire the key.
TELEMETRY_INTERVAL: float = 2.0
TELEMETRY_TTL: int = 6


class VFDTelemetry(msgspec.Struct, frozen=True):
    """Per-drive monitoring status and last reading.

    The reading fields mirror `scietex.hal.vfd.base.data.VFDParameters`; the
    optional ones stay ``None`` when the drive does not report them.
    """

    label: str
    driver: str
    connected: bool
    monitoring: bool
    frequency_hz: float | None
    frequency_percent: float | None
    output_current: float | None
    output_voltage: float | None
    output_power: float | None
    dc_bus_voltage: float | None
    heatsink_temperature_c: float | None
    motor_temperature_c: float | None
    state: str | None
    last_reading_at: datetime | None
    reading_count: int
    error_count: int
    last_error: str | None


class VFDServiceTelemetry(msgspec.Struct, frozen=True):
    """Service telemetry snapshot pushed by the worker.

    Mirrors the API read struct field-for-field and adds ``configured`` (whether
    the drive list is populated). ``forbid_unknown_fields`` is deliberately left
    off so the API side can decode this payload with its own (slightly smaller)
    struct.
    """

    service: str
    instance_id: str
    configured: bool
    gateway_host: str | None
    gateway_port: int | None
    drives: list[VFDTelemetry]
    timestamp: datetime = msgspec.field(default_factory=lambda: datetime.now(timezone.utc))
    schema_version: int = 1


def build_telemetry(worker: VFDWorker) -> VFDServiceTelemetry:
    """Snapshot the worker's drive registry into a telemetry payload.

    An unconfigured worker (empty drive list) reports ``configured=False`` with
    no drives. A configured worker reads the per-label stats and monitoring
    flags; the gateway host/port come from the effective settings (``None``
    before startup), and drives are sorted by label.
    """
    settings = worker.vfd_settings
    drives = [
        _drive_telemetry(worker, label, drive_settings.driver)
        for label, drive_settings in sorted(worker.drive_settings.items())
    ]
    return VFDServiceTelemetry(
        service=worker.service_name,
        instance_id=worker.instance_id,
        configured=bool(worker.drive_settings),
        gateway_host=settings.gateway.host if settings is not None else None,
        gateway_port=settings.gateway.port if settings is not None else None,
        drives=drives,
    )


def _drive_telemetry(worker: VFDWorker, label: str, driver: str) -> VFDTelemetry:
    """Build one drive's telemetry entry from its stats and last reading."""
    stats = worker.stats[label]
    parameters = stats.last_parameters
    return VFDTelemetry(
        label=label,
        driver=driver,
        connected=worker.connected.get(label, False),
        monitoring=worker.monitoring.get(label, False),
        frequency_hz=parameters.frequency_hz if parameters is not None else None,
        frequency_percent=parameters.frequency_percent if parameters is not None else None,
        output_current=parameters.output_current if parameters is not None else None,
        output_voltage=parameters.output_voltage if parameters is not None else None,
        output_power=parameters.output_power if parameters is not None else None,
        dc_bus_voltage=parameters.dc_bus_voltage if parameters is not None else None,
        heatsink_temperature_c=parameters.heatsink_temperature_c if parameters is not None else None,
        motor_temperature_c=parameters.motor_temperature_c if parameters is not None else None,
        state=parameters.state.name if parameters is not None else None,
        last_reading_at=stats.last_reading_at,
        reading_count=stats.reading_count,
        error_count=stats.error_count,
        last_error=stats.last_error,
    )
