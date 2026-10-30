"""Shared helpers for the VFD service handler tests.

This is not a test module: the filename does not match pytest's default
collection patterns (``test_*.py`` / ``*_test.py``), so pytest will not collect
it.
"""

import logging
from importlib.metadata import EntryPoint
from uuid import uuid4

import msgspec
from scietex.hal.vfd.base.capabilities import VFDCapabilities
from scietex.hal.vfd.base.data import VFDError, VFDParameters, VFDState
from scietex.hal.vfd.base.protocol import VFDProtocol
from scietex.service.task_handler import TaskCapabilities, TaskData, TaskHandlerContext

#: Entry point name the shared fake driver is advertised under in tests.
FAKE_DRIVER_NAME = "test_helpers_fake_vfd"


def register_fake_driver(monkeypatch) -> None:
    """Advertise `_FakeVFD` as the only driver entry point.

    The worker resolves drivers from installed distribution metadata, which a
    test module cannot contribute to. Patching ``entry_points`` keeps the
    resolution path under test without installing a throwaway distribution.
    """
    from scietex.vfd_service import vfd_worker as worker_module

    entry_point = EntryPoint(
        name=FAKE_DRIVER_NAME,
        value=f"{__name__}:_FakeVFD",
        group=worker_module.DRIVER_ENTRY_POINT_GROUP,
    )
    monkeypatch.setattr(worker_module, "entry_points", lambda group: [entry_point])


def _make_context() -> TaskHandlerContext:
    """A narrow handler context with fixed service identity and a test logger."""
    return TaskHandlerContext(service_name="test", instance_id="inst", logger=logging.getLogger("test"))


def _make_capabilities() -> TaskCapabilities:
    """Per-call capabilities with a no-op progress writer (mirrors modbus tests)."""

    async def _noop(_task_id, _value) -> None:
        return None

    return TaskCapabilities(task_id=uuid4(), _write_progress=_noop)


def _make_task(task_name: str, payload: dict | None) -> TaskData:
    """A ``TaskData`` whose payload is JSON-encoded from ``payload`` (``None`` -> empty)."""
    return TaskData(
        task_id=str(uuid4()),
        task=task_name,
        payload=msgspec.json.encode(payload) if payload is not None else b"",
    )


class _FakeVFD(VFDProtocol):
    """A `VFDProtocol` fake recording every control call.

    ``capabilities`` defaults to the empty tree (nothing supported) and may be
    replaced per instance. Each control method records its call; ``read_exc``
    makes :meth:`read_parameters` raise, ``control_exc`` makes every control
    method raise.
    """

    capabilities = VFDCapabilities()
    display_name = "Fake VFD"

    def __init__(self, host, port=502, unit_id=1, label="VFD", timeout=1.0, logger=None, **kwargs):
        self.label = label
        self.host = host
        self.port = port
        self.unit_id = unit_id
        self.timeout = timeout
        self.logger = logger
        self.extra_kwargs = kwargs
        self.connect_calls = 0
        self.close_calls = 0
        self.read_calls = 0
        self.parameters = VFDParameters(frequency_hz=25.0, frequency_percent=50.0)
        self.error_code = 0
        self.read_exc = None
        self.control_exc = None
        self.start_calls: list[tuple[bool, bool]] = []
        self.stop_calls: list[bool] = []
        self.set_frequency_calls: list[float] = []
        self.clear_error_calls: list[bool] = []

    async def connect(self):
        self.connect_calls += 1

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
        return self.error_code

    def parse_error_code(self, code: int, comm: bool = False) -> VFDError:
        return VFDError(code=code, message="No error" if code == 0 else "Fault")

    async def clear_error(self, comm: bool = False):
        self.clear_error_calls.append(comm)
        if self.control_exc is not None:
            raise self.control_exc
        self.error_code = 0
        return 0

    async def start(self, forward: bool = True, slow: bool = False):
        self.start_calls.append((forward, slow))
        if self.control_exc is not None:
            raise self.control_exc

    async def stop(self, freewheel: bool = False):
        self.stop_calls.append(freewheel)
        if self.control_exc is not None:
            raise self.control_exc

    async def set_frequency(self, frequency_hz: float):
        self.set_frequency_calls.append(frequency_hz)
        if self.control_exc is not None:
            raise self.control_exc


#: A state value used by tests that need a non-default run state.
RUNNING_FORWARD = VFDState.RUNNING_FORWARD
