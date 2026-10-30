"""``monitoring:*`` control-plane tasks: start and stop drive poll loops.

Each drive is polled by one long-lived asyncio task started and cancelled by
:meth:`VFDWorker.start_monitoring` and :meth:`VFDWorker.stop_monitoring`. The UI
submits these tasks on the control lane to switch monitoring on or off for one
drive or for every drive.
"""

from typing import TYPE_CHECKING, ClassVar

import msgspec
from scietex.service.task_handler import (
    TaskCapabilities,
    TaskData,
    TaskHandler,
    TaskHandlerContext,
    TaskResult,
)

if TYPE_CHECKING:
    from ..vfd_worker import VFDWorker

#: Task name that starts the poll loop for one drive or every drive.
MONITORING_START_TASK_NAME = "monitoring:start"

#: Task name that stops the poll loop for one drive or every drive.
MONITORING_STOP_TASK_NAME = "monitoring:stop"


class MonitoringRequest(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """Request payload for a ``monitoring:*`` task.

    Args:
        label: Drive label to act on; ``None`` (or an empty payload) selects
            every drive.
    """

    label: str | None = None


class MonitoringResponse(msgspec.Struct, frozen=True):
    """Result payload of a ``monitoring:*`` task.

    Args:
        action: ``"start"`` or ``"stop"``.
        labels: Labels actually affected by the call.
        monitoring: Full per-label monitoring state after the call.
    """

    action: str
    labels: list[str]
    monitoring: dict[str, bool]


class MonitoringHandler(TaskHandler):
    """Handle ``monitoring:start`` and ``monitoring:stop`` on the control lane.

    The worker is injected as a constructor keyword argument (``worker=``) via
    ``add_task_handler`` — the handler context carries no worker reference.
    """

    control: ClassVar[bool] = True

    def __init__(self, name: str, context: TaskHandlerContext, *, worker: "VFDWorker") -> None:
        """Initialize the handler.

        Args:
            name: Human-readable name for this handler instance.
            context: Narrow context provided by the owning processor.
            worker: The VFD worker whose poll loops are controlled.
        """
        super().__init__(name, context)
        self._worker: VFDWorker = worker

    @property
    def supported_tasks(self) -> list[str]:
        """Task types this handler serves."""
        return [MONITORING_START_TASK_NAME, MONITORING_STOP_TASK_NAME]

    async def handle(self, task_data: TaskData, *, capabilities: TaskCapabilities) -> TaskResult:
        """Start or stop the poll loop for the selected drive(s).

        An empty payload selects every drive. An unknown label yields
        ``UNKNOWN_DRIVE``; any other failure yields ``MONITORING_FAILED``. Both
        are non-retryable: a bad label or a worker-level failure will not
        succeed on retry.
        """
        if task_data.payload:
            try:
                request = msgspec.json.decode(task_data.payload, type=MonitoringRequest)
            except msgspec.DecodeError as exc:
                return TaskResult(status="error", error=str(exc), error_code="INVALID_PAYLOAD", retryable=False)
        else:
            request = MonitoringRequest()

        action = "start" if task_data.task == MONITORING_START_TASK_NAME else "stop"
        try:
            if action == "start":
                labels = self._worker.start_monitoring(request.label)
            else:
                labels = self._worker.stop_monitoring(request.label)
        except KeyError as exc:
            return TaskResult(status="error", error=str(exc), error_code="UNKNOWN_DRIVE", retryable=False)
        except Exception as exc:
            return TaskResult(status="error", error=str(exc), error_code="MONITORING_FAILED", retryable=False)

        response = MonitoringResponse(action=action, labels=labels, monitoring=dict(self._worker.monitoring))
        return TaskResult(status="success", payload=msgspec.json.encode(response))
