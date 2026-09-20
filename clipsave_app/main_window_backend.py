from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QObject

from .bulk_checkpoint import checkpoint_path
from .bulk_image_controller import BulkImageController
from .database import LibraryDatabase
from .image_task_controller import ImageTaskController
from .library_controller import LibraryController
from .library_metadata_controller import LibraryMetadataController
from .maintenance_controller import LibraryMaintenanceController
from .monitoring_controller import MonitoringController
from .mutation_controller import LibraryMutationController
from .settings import Settings
from . import shutdown_coordinator as _shutdown_coordinator
from .task_supervisor import TaskSupervisor


ShutdownCoordinator = _shutdown_coordinator.ShutdownCoordinator
ShutdownFailure = _shutdown_coordinator.ShutdownFailure


class MainWindowBackend:
    """Own the non-visual controller graph used by MainWindow."""

    def __init__(
        self,
        database: LibraryDatabase,
        settings: Settings,
        *,
        parent: QObject,
        start_regular: Callable | None = None,
        cancel_regular: Callable | None = None,
        start_bounded: Callable | None = None,
    ) -> None:
        self.database = database
        self.settings = settings
        self.tasks = TaskSupervisor()
        self._start_regular_hook = start_regular
        self._cancel_regular_hook = cancel_regular
        self.library = LibraryController(database, self.tasks, parent=parent)
        self.metadata = LibraryMetadataController(database)
        self.maintenance = LibraryMaintenanceController(
            database,
            self.tasks,
            parent=parent,
        )
        self.images = ImageTaskController(
            self.tasks,
            parent=parent,
            database=database,
            start_bounded=start_bounded,
        )
        self.mutations = LibraryMutationController(
            database,
            self.tasks,
            parent=parent,
        )
        self.bulk_images = BulkImageController(
            database,
            checkpoint_path(Path(settings.path)),
            start_task=self.start_regular,
            cancel_task=self.cancel_regular,
            parent=parent,
        )

    def start_regular(self, token: object, target):
        if self._start_regular_hook is not None:
            return self._start_regular_hook(token, target)
        return self.tasks.start_thread(token, target)

    def cancel_regular(self, token: object) -> None:
        if self._cancel_regular_hook is not None:
            self._cancel_regular_hook(token)
            return
        self.tasks.cancel(token)

    def attach_runtime(
        self,
        clipboard_service,
        grid,
        detail,
        *,
        runtime=None,
    ) -> tuple[MonitoringController, ShutdownCoordinator]:
        monitoring = MonitoringController(self.settings, clipboard_service)
        shutdown = ShutdownCoordinator(
            self.database,
            clipboard_service,
            grid,
            detail,
            runtime=runtime,
        )
        self.monitoring = monitoring
        self.shutdown = shutdown
        return monitoring, shutdown

    def cancel_token(self, token: object) -> None:
        self.tasks.cancel(token)

    def finish_bounded(self, token: object) -> None:
        self.tasks.finish_bounded(token)

    def cancel_request(self, request: tuple[object, QObject] | None) -> None:
        if request is None:
            return
        self.tasks.cancel(request[0])

    def cancel_background_requests(self) -> set[object]:
        self.library.cancel_refresh()
        self.library.cancel_search()
        self.library.cancel_page()
        cancelled_tokens = self.images.cancel_for_shutdown()
        cancelled_tokens.update(self.mutations.cancel_for_shutdown())
        bulk_token = self.bulk_images.cancel()
        if bulk_token is not None:
            cancelled_tokens.add(bulk_token)
        return cancelled_tokens

    def clear_background_request_state(self) -> None:
        self.images.clear_state()
        self.mutations.clear_background_state()

    def wait_for_request(
        self,
        request: tuple[object, QObject] | None,
        timeout: float,
        *,
        pump_events: Callable[[], None] | None = None,
    ) -> bool:
        if request is None:
            return True
        return self.tasks.wait_for_token(
            request[0],
            timeout,
            pump_events=pump_events,
        )

    def cancel_all_and_wait(
        self,
        timeout: float,
        *,
        require_bounded: bool = True,
        pump_events: Callable[[], None] | None = None,
    ) -> bool:
        return self.tasks.cancel_all_and_wait(
            timeout,
            require_bounded=require_bounded,
            pump_events=pump_events,
        )
