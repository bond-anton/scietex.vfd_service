"""VFD worker: a Valkey-backed service that polls and controls variable frequency drives.

`VFDWorker` extends `ValkeyWorker` and drives one or more Modbus/TCP variable
frequency drives reached through the Scietex Modbus gateway. Each drive is
driven by a `scietex.hal.vfd` class selected by entry point name; the worker
seeds its in-memory drive registry from the service-owned ``vfd.yml`` at
startup and reconciles it against the framework's merged remote-config section
(the L3 ``vfd`` section replaces the YAML list wholesale, it is never merged by
label). A ``Telemetry`` manager publishes a service snapshot every 2 s so the UI
can observe the configured/live state, and one long-lived poll task per drive
publishes readings to a per-drive Valkey stream.
"""

import asyncio
import logging
from datetime import datetime, timezone
from importlib.metadata import entry_points
from typing import Any, cast

import msgspec
from glide import (
    ConnectionError as GlideConnectionError,
)
from glide import (
    ExpirySet,
    ExpiryType,
    RequestError,
)
from glide import (
    TimeoutError as GlideTimeoutError,
)
from pymodbus.exceptions import ConnectionException, ModbusIOException
from scietex.hal.vfd.base.data import VFDParameters, VFDState
from scietex.hal.vfd.base.protocol import VFDProtocol
from scietex.service import Manager, ValkeyWorker, ValkeyWorkerConfig
from scietex.service.valkey._glide import StreamAddOptions, TrimByMaxLen

from .config import (
    VFD_SECTION,
    VFD_SETTINGS_DEFAULTS,
    VFDServiceSettings,
    VFDSettings,
    read_vfd_config,
)
from .task_handlers import MonitoringHandler, VFDControlHandler
from .telemetry import TELEMETRY_INTERVAL, TELEMETRY_TTL, build_telemetry

#: Entry point group under which ``scietex.hal.vfd`` advertises its drivers. A
#: drive configuration stores the entry point name.
DRIVER_ENTRY_POINT_GROUP: str = "scietex.hal.vfd.drivers"

#: Default cap on the number of entries retained per drive's readings stream.
#: The effective value comes from ``VFDServiceSettings.stream_maxlen``.
STREAM_MAXLEN: int = 10000

#: Default number of consecutive failed reads before a drive is reported as
#: disconnected. The effective value comes from
#: ``VFDServiceSettings.failure_threshold``.
CONNECTED_FAILURE_THRESHOLD: int = 3

#: Longest gap between two successful readings that the software hours
#: accumulator credits to a drive. A worker that was stopped, or a drive that
#: was unreachable for hours, must not have that downtime counted as run time;
#: the cap is generous enough that a normal poll interval never hits it.
MAX_ACCUMULATION_GAP: float = 60.0


class VFDStats(msgspec.Struct):
    """Per-drive counters accumulated by the poll loop.

    Mutable (no ``frozen=True``): the poll loop updates the fields in place.
    """

    reading_count: int = 0
    error_count: int = 0
    last_parameters: VFDParameters | None = None
    last_reading_at: datetime | None = None
    last_error: str | None = None
    # Consecutive failed reads since the last success. A single bus glitch must
    # not flip the drive to disconnected, so the poll loop only clears
    # `_connected` once this reaches the configured failure threshold.
    consecutive_failures: int = 0
    # Software hours for a drive that keeps no run-time counter of its own.
    # The poll loop credits the elapsed time between two successful readings,
    # capped at `MAX_ACCUMULATION_GAP` so a worker that was stopped for a day
    # does not credit that day to the drive. Both stay 0 for a drive that
    # reports its own counters; the telemetry prefers the drive's value.
    accumulated_run_seconds: float = 0.0
    accumulated_power_on_seconds: float = 0.0


def resolve_driver(name: str) -> type[VFDProtocol]:
    """Resolve a driver entry point name to its drive class.

    Drivers are advertised by ``scietex.hal.vfd`` under the
    ``scietex.hal.vfd.drivers`` entry point group. The stored identifier is the
    entry point name, so a module move inside HAL does not invalidate a drive
    configuration.

    Only a class is accepted — an unknown name, an import failure, or a
    non-class object each raise `ValueError`. The `VFDProtocol` check is
    deliberately deferred: it runs after instantiation, where ``isinstance`` on
    an instance is meaningful.

    Args:
        name: Entry point name, e.g. ``intek_spe_b``.

    Returns:
        The resolved drive driver class.

    Raises:
        ValueError: If no advertised driver carries that name, its module cannot
            be imported, or the resolved object is not a class.
    """
    entry_point = next(
        (ep for ep in entry_points(group=DRIVER_ENTRY_POINT_GROUP) if ep.name == name),
        None,
    )
    if entry_point is None:
        raise ValueError(f"Unknown driver {name!r}: not advertised under {DRIVER_ENTRY_POINT_GROUP!r}")
    try:
        obj = entry_point.load()
    except ImportError as exc:
        raise ValueError(f"Cannot import driver {name!r}") from exc
    if not isinstance(obj, type):
        raise ValueError(f"Driver {name!r} resolved to {type(obj).__name__}, not a class")
    return cast(type[VFDProtocol], obj)


def _is_transport_failure(exc: Exception) -> bool:
    """Whether ``exc`` means the socket is gone rather than the drive answering.

    ``VFDProtocol`` exposes no ``is_connected``, so the poll loop has to infer
    transport death from the exception. The driver raises
    ``ModbusOperationError`` for an exception *response* — the gateway answered,
    so the socket is still open and reconnecting would be wrong. pymodbus raises
    ``ConnectionException`` when the transport is already gone and
    ``ModbusIOException`` when a request timed out and it closed the connection
    itself; both leave a socket that cannot be reused.
    """
    return isinstance(exc, (ConnectionException, ModbusIOException, OSError))


class VFDWorker(ValkeyWorker):
    """A `ValkeyWorker` that polls and controls one or more variable frequency drives.

    Settings resolve through the framework's four-layer merge: constructor
    default (L0) < ``vfd.yml`` bootstrap patch (L1, read directly here) <
    framework ``config.yml`` snapshot (L2) < remote ``vfd`` section (L3). The
    section is registered with ``bootstrap=None``, so L1 never enters the
    framework's layer stack; the worker reads the YAML itself to seed the
    initial registry and then reconciles against the merged section after
    ``super().initialize()``. The merged ``drives`` list replaces the YAML list
    entirely — it is not merged by label.

    A drive is polled by one long-lived task; it is never connected during
    startup, so an offline drive does not block the service. A reconfig at
    runtime is restart-required (the registry is not live-rebuilt).
    """

    def __init__(self, config: ValkeyWorkerConfig | None = None, *, client_factory=None, theme=None) -> None:
        super().__init__(config, client_factory=client_factory, theme=theme)
        self._settings: VFDServiceSettings | None = None
        self._drives: dict[str, VFDProtocol] = {}
        self._drive_settings: dict[str, VFDSettings] = {}
        self._monitoring: dict[str, bool] = {}
        self._poll_tasks: dict[str, asyncio.Task[None]] = {}
        self._stats: dict[str, VFDStats] = {}
        # The protocol exposes no connection accessor, so the worker tracks
        # two per-label flags: `_transport_open` is the socket state the poll
        # loop needs to decide whether to reconnect, and `_connected` is what
        # telemetry reports — "the last read succeeded". They differ because
        # `connect()` only opens the TCP connection to the gateway, which
        # succeeds for a unit id that no device answers.
        self._transport_open: dict[str, bool] = {}
        self._connected: dict[str, bool] = {}
        # Startup guard: the config apply hook runs both during
        # ``super().initialize()`` (L2/L3 delivery, where ``initialize`` itself
        # builds the registry) and at runtime (where a change is restart-
        # required). False until ``initialize`` returns.
        self._startup_complete: bool = False
        self._restart_required: bool = False
        # Telemetry encoding is done with an encoder owned here (the framework's
        # ``__encoder`` is private), and the key is resolved once at
        # construction.
        self._telemetry_encoder = msgspec.msgpack.Encoder()
        self._telemetry_key = f"scietex:{self.service_name}:{self.instance_id}:telemetry"
        self.register_config_settings(
            VFD_SECTION,
            VFDServiceSettings,
            apply=self._apply_vfd_settings,
            defaults=VFD_SETTINGS_DEFAULTS,
            # bootstrap=None is deliberate: the worker reads the YAML itself and
            # seeds L0 from it, so L1 never enters the layer stack and the
            # remote L3 config cleanly replaces it (see config.py module
            # docstring).
            bootstrap=None,
        )
        # Control-lane monitoring switch and data-lane drive commands. Both
        # handlers receive the worker by constructor injection: the handler
        # context carries no worker reference.
        self.add_task_handler(MonitoringHandler, worker=self)
        self.add_task_handler(VFDControlHandler, worker=self)

    @property
    def drives(self) -> dict[str, VFDProtocol]:
        """The live drive registry keyed by label (the dict itself, not a copy).

        Mutating callers share the worker's registry; read-only consumers should
        iterate without changing it.
        """
        return self._drives

    @property
    def drive_settings(self) -> dict[str, VFDSettings]:
        """The per-label drive settings the registry was built from."""
        return self._drive_settings

    @property
    def monitoring(self) -> dict[str, bool]:
        """Per-label poll-loop activity: True while a poll task is scheduled."""
        return self._monitoring

    @property
    def connected(self) -> dict[str, bool]:
        """Per-label transport state tracked by the poll loop."""
        return self._connected

    @property
    def stats(self) -> dict[str, VFDStats]:
        """Per-label counters accumulated by the poll loop."""
        return self._stats

    @property
    def vfd_settings(self) -> VFDServiceSettings | None:
        """The effective VFD settings, or `None` before startup."""
        return self._settings

    def _failure_threshold(self) -> int:
        """Consecutive failed reads before a drive is reported as disconnected.

        Falls back to the module default before settings are applied, so a poll
        task that starts early still has a usable bound.
        """
        settings = self._settings
        return settings.failure_threshold if settings is not None else CONNECTED_FAILURE_THRESHOLD

    def _stream_maxlen(self) -> int:
        """Entries retained per drive's readings stream.

        Falls back to the module default before settings are applied.
        """
        settings = self._settings
        return settings.stream_maxlen if settings is not None else STREAM_MAXLEN

    @property
    def restart_required(self) -> bool:
        """Whether a post-startup settings apply requires a restart to rebuild the registry."""
        return self._restart_required

    async def initialize(self) -> bool:
        """Seed the registry from the YAML, then reconcile against the merged settings.

        The service-owned YAML is read before ``super().initialize()`` so the
        registry is populated even when no remote config exists; the merged
        section read back afterwards replaces it wholesale when it differs. A
        bad YAML or a drive that fails to resolve/construct fails startup: a
        half-working registry must not run.

        Returns:
            `True` if the framework initialized and the registry is built;
            `False` on any configuration or startup failure.
        """
        self._startup_complete = False
        try:
            raw = read_vfd_config(self.conf_dir)
        except RuntimeError as exc:
            self.logger.error("Failed to read VFD configuration: %s", exc)
            return False
        try:
            settings = msgspec.convert(raw, VFDServiceSettings)
        except msgspec.ValidationError as exc:
            self.logger.error("Invalid VFD configuration: %s", exc)
            return False
        self._settings = settings
        try:
            await self._build_drives(settings)
        except ValueError as exc:
            self.logger.error("Failed to build VFD registry: %s", exc)
            return False

        if not await super().initialize():
            return False

        resolved = self.current_config_settings(VFD_SECTION)
        if resolved is not None:
            resolved_settings = cast(VFDServiceSettings, resolved)
            if resolved_settings != settings:
                # L3 wins wholesale: the merged ``drives`` list replaces the
                # YAML-seeded list entirely, it is not merged by label.
                self._apply_vfd_settings(resolved_settings)
                try:
                    await self._build_drives(resolved_settings)
                except ValueError as exc:
                    self.logger.error("Failed to rebuild VFD registry from merged settings: %s", exc)
                    return False

        self._startup_complete = True
        self.logger.info(
            "VFD service started: %d drive(s) via gateway %s:%d",
            len(self._drives),
            self._settings.gateway.host if self._settings is not None else "?",
            self._settings.gateway.port if self._settings is not None else 0,
        )
        return True

    async def cleanup(self) -> None:
        """Cancel poll tasks and close drives, then the framework resources."""
        await self.close_drives()
        await super().cleanup()

    async def _build_drives(self, settings: VFDServiceSettings) -> None:
        """(Re)build the drive registry from the given settings.

        Idempotent: existing poll tasks are cancelled and existing drives closed
        before the registry is rebuilt. A drive is instantiated but not
        connected here — connection happens in the poll loop, so an offline
        drive does not block startup.

        Args:
            settings: The authoritative settings to build from.

        Raises:
            ValueError: On a duplicate label, an unresolvable driver, or an
                instance that does not satisfy `VFDProtocol`.
        """
        await self.close_drives()
        # The registry is built from these settings, so they are the effective
        # ones: the poll loop reads the failure threshold and the stream
        # retention from here rather than from the module defaults.
        self._settings = settings
        self._drives = {}
        self._drive_settings = {}
        self._monitoring = {}
        self._transport_open = {}
        self._connected = {}
        self._stats = {}

        seen: set[str] = set()
        for drive_settings in settings.drives:
            label = drive_settings.label
            if label in seen:
                self.logger.error("Duplicate drive label %r in configuration", label)
                raise ValueError(f"Duplicate drive label {label!r}")
            seen.add(label)

            driver = resolve_driver(drive_settings.driver)
            # The protocol declares no constructor, so cast to `Any` before the
            # dynamic instantiation — the concrete driver's `__init__` signature
            # is unknown statically and validated by the isinstance check below.
            kwargs: dict[str, Any] = {
                "host": settings.gateway.host,
                "port": settings.gateway.port,
                "unit_id": drive_settings.unit_id,
                "label": label,
                "timeout": settings.gateway.timeout,
                "logger": self.logger,
            }
            # Only pass the maximum frequency when configured: a driver that
            # reads it from the drive itself must keep its own default.
            if drive_settings.max_frequency_hz is not None:
                kwargs["max_frequency_hz"] = drive_settings.max_frequency_hz
            drive = cast(Any, driver)(**kwargs)
            if not isinstance(drive, VFDProtocol):
                self.logger.error("Driver %r for drive %r is not a VFDProtocol", drive_settings.driver, label)
                raise ValueError(f"Driver {drive_settings.driver!r} for drive {label!r} is not a VFDProtocol")

            self._drives[label] = drive
            self._drive_settings[label] = drive_settings
            self._monitoring[label] = False
            self._transport_open[label] = False
            self._connected[label] = False
            self._stats[label] = VFDStats()

    async def close_drives(self) -> None:
        """Cancel all poll tasks and close every drive, swallowing close errors.

        Used by both ``cleanup`` and the idempotent rebuild in ``_build_drives``.
        A close error is logged and swallowed so a teardown failure never aborts
        shutdown or a rebuild.
        """
        for label, task in list(self._poll_tasks.items()):
            task.cancel()
            self._monitoring[label] = False
            self._transport_open[label] = False
            self._connected[label] = False
        self._poll_tasks.clear()
        for label, drive in list(self._drives.items()):
            try:
                await drive.close()
            except Exception as exc:
                self.logger.warning("Failed to close drive %r: %s", label, exc)

    def _apply_vfd_settings(self, settings: VFDServiceSettings) -> None:
        """Apply merged VFD settings (sync hook, invoked inline).

        During startup the hook only stores: `initialize` builds the registry
        from the final merged settings. At runtime a change is only stored and
        flagged restart-required — the registry is not live-rebuilt because the
        framework's ``_restart_required_fields`` only inspects core settings, so
        this service tracks its own restart-required state.
        """
        self._settings = settings
        if not self._startup_complete:
            self.logger.info("Stored VFD settings for startup")
            return
        self.logger.warning(
            "VFD settings applied; restart required to rebuild the drive registry (drives are not live-rebuilt)"
        )
        self._restart_required = True

    def _labels(self, label: str | None) -> list[str]:
        """Resolve a label selector to the list of affected labels.

        ``None`` selects all drives; an unknown label raises `KeyError`.
        """
        if label is None:
            return list(self._drives)
        if label not in self._drives:
            raise KeyError(f"Unknown drive label {label!r}")
        return [label]

    def start_monitoring(self, label: str | None = None) -> list[str]:
        """Start a poll task for the selected drive(s), returning affected labels.

        ``label=None`` starts every drive. A label that is already monitoring is
        a no-op and is not returned. Each started drive gets one long-lived poll
        task scheduled on the running loop.
        """
        affected: list[str] = []
        for candidate in self._labels(label):
            if self._monitoring[candidate]:
                continue
            self._monitoring[candidate] = True
            self._poll_tasks[candidate] = asyncio.get_running_loop().create_task(self._poll_drive(candidate))
            affected.append(candidate)
        return affected

    def stop_monitoring(self, label: str | None = None) -> list[str]:
        """Cancel the poll task for the selected drive(s), returning affected labels.

        ``label=None`` stops every drive. The task is cancelled (sync — the loop
        is not awaited), removed from ``_poll_tasks``, and the label marked not
        monitoring.
        """
        affected: list[str] = []
        for candidate in self._labels(label):
            task = self._poll_tasks.pop(candidate, None)
            if task is not None:
                task.cancel()
            if self._monitoring.get(candidate):
                self._monitoring[candidate] = False
                self._transport_open[candidate] = False
                self._connected[candidate] = False
                affected.append(candidate)
        return affected

    async def _sleep(self, delay: float) -> None:
        """Sleep for ``delay``; a separate method so tests can short-circuit the poll loop."""
        await asyncio.sleep(delay)

    async def _poll_drive(self, label: str) -> None:
        """Poll one drive forever: connect, read, publish, repeat.

        The loop never dies on a drive error: a connect or read failure
        increments the drive's error counters, logs at WARNING, and continues
        after the poll interval. A successful reading updates the stats and
        publishes a stream entry. The task is expected to be stopped by
        cancellation (``stop_monitoring``/``cleanup``), not by returning.
        """
        drive = self._drives.get(label)
        drive_settings = self._drive_settings.get(label)
        if drive is None or drive_settings is None:
            return
        while True:
            if not self._transport_open.get(label, False):
                try:
                    await drive.connect()
                    self._transport_open[label] = True
                except Exception as exc:
                    self._record_error(label, exc, "connect")
                    await self._sleep(drive_settings.poll_interval)
                    continue

            try:
                parameters = await drive.read_parameters()
            except Exception as exc:
                # `connected` means "the drive is answering", not "the socket
                # is open": `connect()` only opens the TCP connection to the
                # gateway, so it succeeds for a unit id that no device answers.
                # A single failed read is not enough to report the drive as
                # gone — an RS485 segment drops the occasional frame — so the
                # flag clears only after the configured number of failures in
                # a row.
                stats = self._stats[label]
                stats.consecutive_failures += 1
                if stats.consecutive_failures >= self._failure_threshold():
                    self._connected[label] = False
                # A dead socket never recovers on its own: pymodbus closed the
                # transport, so every later read fails the same way. Clear the
                # flag so the next iteration reconnects. An exception response
                # leaves the socket open and must not trigger a reconnect.
                if _is_transport_failure(exc):
                    self._transport_open[label] = False
                self._record_error(label, exc, "read")
                await self._sleep(drive_settings.poll_interval)
                continue

            self._connected[label] = True
            stats = self._stats[label]
            stats.consecutive_failures = 0
            stats.reading_count += 1
            now = datetime.now(timezone.utc)
            self._accumulate_hours(stats, parameters, now)
            stats.last_parameters = parameters
            stats.last_reading_at = now
            stats.last_error = None
            await self._publish_reading(label, parameters, stats.last_reading_at)

            await self._sleep(drive_settings.poll_interval)

    def _accumulate_hours(self, stats: VFDStats, parameters: VFDParameters, now: datetime) -> None:
        """Credit the elapsed time since the previous reading to the software hours.

        Only a drive that reports no run-time counter of its own is
        accumulated: a drive that does report one is authoritative, and adding
        to a parallel software count would give two disagreeing totals.

        The elapsed time is capped at `MAX_ACCUMULATION_GAP`, so a worker that
        was stopped for a day does not credit that day to the drive. The
        powered-on counter is credited on every successful reading; the run
        counter only while the drive reports a running state.
        """
        if parameters.total_run_hours is not None:
            return
        previous = stats.last_reading_at
        if previous is None:
            return
        elapsed = min((now - previous).total_seconds(), MAX_ACCUMULATION_GAP)
        if elapsed <= 0:
            return
        stats.accumulated_power_on_seconds += elapsed
        if parameters.state in (VFDState.RUNNING_FORWARD, VFDState.RUNNING_BACKWARD):
            stats.accumulated_run_seconds += elapsed

    def _record_error(self, label: str, exc: Exception, stage: str) -> None:
        """Increment a drive's error counters and log a WARNING."""
        stats = self._stats[label]
        stats.error_count += 1
        stats.last_error = str(exc)
        self.logger.log(logging.WARNING, "Failed to %s drive %r: %s", stage, label, exc)

    async def _publish_reading(self, label: str, parameters: VFDParameters, now: datetime) -> None:
        """Publish one reading to the drive's stream.

        The write is guarded on `self.client`; a glide error is logged at
        WARNING and reported to the transport health, mirroring the telemetry
        loop's error handling.
        """
        client = self.client
        if client is None:
            return
        key = f"scietex:{self.service_name}:{self.instance_id}:vfd:{label}:readings"
        try:
            await client.xadd(
                key,
                [
                    (b"ts", now.isoformat().encode()),
                    (b"frequency_hz", encode_value(parameters.frequency_hz)),
                    (b"frequency_percent", encode_value(parameters.frequency_percent)),
                    (b"output_current", encode_value(parameters.output_current)),
                    (b"output_voltage", encode_value(parameters.output_voltage)),
                    (b"output_power", encode_value(parameters.output_power)),
                    (b"dc_bus_voltage", encode_value(parameters.dc_bus_voltage)),
                    (b"heatsink_temperature_c", encode_value(parameters.heatsink_temperature_c)),
                    (b"motor_temperature_c", encode_value(parameters.motor_temperature_c)),
                    (b"state", parameters.state.name.encode()),
                    (b"total_run_hours", encode_value(parameters.total_run_hours)),
                    (b"total_power_on_hours", encode_value(parameters.total_power_on_hours)),
                ],
                StreamAddOptions(trim=TrimByMaxLen(exact=False, threshold=self._stream_maxlen())),
            )
        except (GlideConnectionError, RequestError, GlideTimeoutError) as exc:
            self.logger.log(logging.WARNING, "Failed to publish reading for drive %r: %s", label, exc)
            self._health.report_failure(exc)

    @Manager(name="Telemetry")
    async def telemetry_loop(self) -> None:
        """Publish service telemetry to Valkey once per iteration.

        The manager runtime repeats this body forever with auto-restart on error.
        The write is guarded on `self.client` and runs even when the worker is
        unconfigured, so the UI observes ``configured=false``. Glide errors are
        logged at WARNING and reported to the transport health, matching the
        heartbeat's error handling.
        """
        client = self.client
        if client is not None:
            payload = build_telemetry(self)
            try:
                await client.set(
                    self._telemetry_key,
                    value=self._telemetry_encoder.encode(payload),
                    expiry=ExpirySet(ExpiryType.SEC, TELEMETRY_TTL),
                )
            except (GlideConnectionError, RequestError, GlideTimeoutError) as exc:
                self.logger.log(logging.WARNING, "Failed to publish VFD telemetry: %s", exc)
                self._health.report_failure(exc)
        await asyncio.sleep(TELEMETRY_INTERVAL)


def encode_value(value: float | None) -> bytes:
    """Encode a reading value for the stream: ``b""`` for `None`, otherwise ``repr``.

    ``repr(float)`` round-trips exactly; the API re-parses and computes min/max,
    so truncation (e.g. ``.12g``) would bias every bucket. ``None`` encodes as an
    empty string so a drive that does not report a field is distinguishable from
    one reporting zero.
    """
    return b"" if value is None else repr(value).encode()
