"""``vfd:*`` data-plane tasks: read and control a single variable frequency drive.

Each task targets one drive by label. Every control operation is gated on the
drive's declared capabilities before dispatch, so an unsupported command is
rejected as a structured error rather than silently ignored by the driver.
"""

from typing import TYPE_CHECKING, ClassVar, cast

import msgspec
from scietex.hal.vfd.base.data import VFDError
from scietex.service.task_handler import (
    TaskCapabilities,
    TaskData,
    TaskHandler,
    TaskHandlerContext,
    TaskResult,
)

if TYPE_CHECKING:
    from ..vfd_worker import VFDWorker

#: Task name that reads the drive's operating parameters and fault code.
VFD_READ_TASK_NAME = "vfd:read"

#: Task name that starts the motor.
VFD_START_TASK_NAME = "vfd:start"

#: Task name that stops the motor.
VFD_STOP_TASK_NAME = "vfd:stop"

#: Task name that writes the frequency setpoint.
VFD_SET_FREQUENCY_TASK_NAME = "vfd:set-frequency"

#: Task name that resets the latched fault.
VFD_CLEAR_ERROR_TASK_NAME = "vfd:clear-error"

#: Maps a control task name to the capability field that gates it. A field that
#: is ``False`` rejects the task. ``vfd:read`` is absent — reading is always
#: supported.
_CAPABILITY_BY_TASK: dict[str, str] = {
    VFD_START_TASK_NAME: "direction",
    VFD_STOP_TASK_NAME: "freewheel_stop",
    VFD_SET_FREQUENCY_TASK_NAME: "frequency_setpoint",
    VFD_CLEAR_ERROR_TASK_NAME: "fault_reset",
}


class VFDCommandRequest(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """Request payload for a ``vfd:*`` task.

    Args:
        label: Drive label to target (required).
        forward: Run direction for ``vfd:start``; ``True`` is forward.
        slow: Use the jog command for ``vfd:start``.
        freewheel: Coast to a stop for ``vfd:stop``.
        frequency_hz: Frequency setpoint for ``vfd:set-frequency``, in Hz.
        comm: Reset the communication-protocol fault for ``vfd:clear-error``.
    """

    label: str
    forward: bool = True
    slow: bool = False
    freewheel: bool = False
    frequency_hz: float | None = None
    comm: bool = False


class VFDCommandResponse(msgspec.Struct, frozen=True):
    """Result payload of a ``vfd:*`` task.

    Args:
        label: Drive label the command targeted.
        task: Task name that was executed.
        frequency_hz: Output frequency (``vfd:read`` only), in Hz.
        frequency_percent: Output frequency as a percentage of the configured
            maximum (``vfd:read`` only).
        output_current: Output current in Amps (``vfd:read`` only).
        output_voltage: Output voltage in Volts (``vfd:read`` only).
        output_power: Output power in kW (``vfd:read`` only).
        dc_bus_voltage: DC bus voltage in Volts, or ``None`` when the drive does
            not report it (``vfd:read`` only).
        heatsink_temperature_c: Power-module temperature in degrees Celsius, or
            ``None`` when the drive does not report it (``vfd:read`` only).
        motor_temperature_c: Motor temperature in degrees Celsius, or ``None``
            when the drive does not report it (``vfd:read`` only).
        state: Drive run state name (``vfd:read`` only).
        error_code: Latched fault code (``vfd:read`` and ``vfd:clear-error``).
        error_message: Human-readable fault description.
    """

    label: str
    task: str
    frequency_hz: float | None = None
    frequency_percent: float | None = None
    output_current: float | None = None
    output_voltage: float | None = None
    output_power: float | None = None
    dc_bus_voltage: float | None = None
    heatsink_temperature_c: float | None = None
    motor_temperature_c: float | None = None
    state: str | None = None
    error_code: int | None = None
    error_message: str | None = None


class VFDControlHandler(TaskHandler):
    """Handle ``vfd:*`` tasks on the data lane.

    The worker is injected as a constructor keyword argument (``worker=``) via
    ``add_task_handler`` — the handler context carries no worker reference.
    """

    control: ClassVar[bool] = False

    def __init__(self, name: str, context: TaskHandlerContext, *, worker: "VFDWorker") -> None:
        """Initialize the handler.

        Args:
            name: Human-readable name for this handler instance.
            context: Narrow context provided by the owning processor.
            worker: The VFD worker whose drive registry is controlled.
        """
        super().__init__(name, context)
        self._worker: VFDWorker = worker

    @property
    def supported_tasks(self) -> list[str]:
        """Task types this handler serves."""
        return [
            VFD_READ_TASK_NAME,
            VFD_START_TASK_NAME,
            VFD_STOP_TASK_NAME,
            VFD_SET_FREQUENCY_TASK_NAME,
            VFD_CLEAR_ERROR_TASK_NAME,
        ]

    async def handle(self, task_data: TaskData, *, capabilities: TaskCapabilities) -> TaskResult:
        """Execute one ``vfd:*`` command against the labelled drive.

        Checks, in order: payload decode, drive lookup, capability gate,
        parameter validation, then dispatch. A transport failure from the driver
        is transient and marked retryable; every other failure is terminal.
        """
        task = task_data.task
        try:
            request = msgspec.json.decode(task_data.payload, type=VFDCommandRequest)
        except msgspec.DecodeError as exc:
            return TaskResult(status="error", error=str(exc), error_code="INVALID_PAYLOAD", retryable=False)

        label = request.label
        drive = self._worker.drives.get(label)
        if drive is None:
            return TaskResult(
                status="error", error=f"Unknown drive label {label!r}", error_code="UNKNOWN_DRIVE", retryable=False
            )

        capability_path = _CAPABILITY_BY_TASK.get(task)
        if capability_path is not None and not _supports(drive, capability_path):
            return TaskResult(
                status="error",
                error=f"Drive {label!r} does not support {task}",
                error_code="UNSUPPORTED",
                retryable=False,
            )

        if task == VFD_SET_FREQUENCY_TASK_NAME and request.frequency_hz is None:
            return self._invalid_parameters(f"{task} requires frequency_hz")

        try:
            if task == VFD_READ_TASK_NAME:
                parameters = await drive.read_parameters()
                code = await drive.read_error_code()
                error = _parse_error(drive, code)
                response = VFDCommandResponse(
                    label=label,
                    task=task,
                    frequency_hz=parameters.frequency_hz,
                    frequency_percent=parameters.frequency_percent,
                    output_current=parameters.output_current,
                    output_voltage=parameters.output_voltage,
                    output_power=parameters.output_power,
                    dc_bus_voltage=parameters.dc_bus_voltage,
                    heatsink_temperature_c=parameters.heatsink_temperature_c,
                    motor_temperature_c=parameters.motor_temperature_c,
                    state=parameters.state.name,
                    error_code=code,
                    error_message=error.message if error is not None else None,
                )
            elif task == VFD_START_TASK_NAME:
                await drive.start(forward=request.forward, slow=request.slow)
                response = VFDCommandResponse(label=label, task=task)
            elif task == VFD_STOP_TASK_NAME:
                await drive.stop(freewheel=request.freewheel)
                response = VFDCommandResponse(label=label, task=task)
            elif task == VFD_SET_FREQUENCY_TASK_NAME:
                # Validated above: the task is rejected when the value is absent.
                await drive.set_frequency(cast(float, request.frequency_hz))
                response = VFDCommandResponse(label=label, task=task, frequency_hz=request.frequency_hz)
            else:  # VFD_CLEAR_ERROR_TASK_NAME (last enumerated task)
                code = await drive.clear_error(comm=request.comm)
                error = _parse_error(drive, code, comm=request.comm)
                response = VFDCommandResponse(
                    label=label,
                    task=task,
                    error_code=code,
                    error_message=error.message if error is not None else None,
                )
        except Exception as exc:
            return TaskResult(status="error", error=str(exc), error_code="VFD_COMMAND_FAILED", retryable=True)

        return TaskResult(status="success", payload=msgspec.json.encode(response))

    @staticmethod
    def _invalid_parameters(message: str) -> TaskResult:
        """Build the non-retryable result for a parameter-validation failure."""
        return TaskResult(status="error", error=message, error_code="INVALID_PARAMETERS", retryable=False)


def _parse_error(drive: object, code: int, *, comm: bool = False) -> VFDError | None:
    """Decode a fault code through the driver's own table, when it has one.

    ``parse_error_code`` is not part of `VFDProtocol` — it is a driver-level
    convenience — so the call is guarded with ``hasattr``. A driver without it
    yields ``None`` and the caller reports the bare code.

    Args:
        drive: The drive driver instance.
        code: The fault code to decode.
        comm: Look the code up in the communication fault table.

    Returns:
        The decoded `VFDError`, or ``None`` when the driver cannot decode codes.
    """
    parse = getattr(drive, "parse_error_code", None)
    if not callable(parse):
        return None
    return cast(VFDError, parse(code, comm=comm))


def _supports(drive: object, capability_path: str) -> bool:
    """Resolve a dotted capability path against a drive's capability tree.

    Args:
        drive: The drive driver instance.
        capability_path: Dotted path such as ``"direction"`` or
            ``"frequency_setpoint"``.

    Returns:
        bool: Whether the drive declares that capability. A missing capability
        tree or a missing field counts as unsupported.
    """
    node: object = getattr(drive, "capabilities", None)
    for part in capability_path.split("."):
        if node is None:
            return False
        node = getattr(node, part, None)
    return bool(node)
