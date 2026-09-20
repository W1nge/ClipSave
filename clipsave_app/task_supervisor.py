from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol


class CancellableHandle(Protocol):
    done_event: threading.Event

    def cancel(self) -> None: ...

    def wait(self, timeout: float | None = None) -> bool: ...


@dataclass(frozen=True)
class ThreadTask:
    cancel_event: threading.Event
    thread: threading.Thread


class TaskSupervisor:
    """Own and coordinate background work started by one application component.

    The supervisor intentionally has no Qt dependency. Callers can supply an
    optional event-pump callback while synchronously waiting during shutdown.
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.regular_tasks: dict[object, tuple[threading.Event, threading.Thread]] = {}
        self.bounded_tasks: dict[object, CancellableHandle] = {}

    def start_thread(
        self,
        token: object,
        target: Callable[[threading.Event], None],
        *,
        name: str = "ClipSaveAsync",
        daemon: bool = True,
    ) -> threading.Event:
        cancel_event = threading.Event()

        def run() -> None:
            try:
                target(cancel_event)
            finally:
                with self.lock:
                    current = self.regular_tasks.get(token)
                    if current is not None and current[1] is threading.current_thread():
                        self.regular_tasks.pop(token, None)

        thread = threading.Thread(target=run, name=name, daemon=daemon)
        with self.lock:
            if token in self.regular_tasks or token in self.bounded_tasks:
                raise ValueError("Task token is already active")
            self.regular_tasks[token] = (cancel_event, thread)
        thread.start()
        return cancel_event

    def track_bounded(self, token: object, handle: CancellableHandle) -> None:
        with self.lock:
            self._prune_bounded_locked()
            if token in self.regular_tasks or token in self.bounded_tasks:
                raise ValueError("Task token is already active")
            self.bounded_tasks[token] = handle

    def finish_bounded(self, token: object) -> None:
        with self.lock:
            self.bounded_tasks.pop(token, None)

    def cancel(self, token: object) -> None:
        with self.lock:
            regular = self.regular_tasks.get(token)
            bounded = self.bounded_tasks.get(token)
        if regular is not None:
            regular[0].set()
        if bounded is not None:
            bounded.cancel()

    def token_done(self, token: object) -> bool:
        with self.lock:
            regular = self.regular_tasks.get(token)
            bounded = self.bounded_tasks.get(token)
        return (
            (regular is None or not regular[1].is_alive())
            and (bounded is None or bounded.done_event.is_set())
        )

    def wait_for_token(
        self,
        token: object,
        timeout: float,
        *,
        pump_events: Callable[[], None] | None = None,
    ) -> bool:
        self.cancel(token)
        deadline = time.monotonic() + max(timeout, 0.0)
        while True:
            with self.lock:
                regular = self.regular_tasks.get(token)
                bounded = self.bounded_tasks.get(token)
            regular_done = regular is None or not regular[1].is_alive()
            bounded_done = bounded is None or bounded.done_event.is_set()
            if regular_done and bounded_done:
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            if pump_events is not None:
                pump_events()
            wait_time = min(0.01, remaining)
            if regular is not None and regular[1].is_alive():
                regular[1].join(wait_time)
            elif bounded is not None and not bounded.done_event.is_set():
                bounded.wait(wait_time)

    def cancel_all_and_wait(
        self,
        timeout: float,
        *,
        require_bounded: bool = True,
        pump_events: Callable[[], None] | None = None,
    ) -> bool:
        deadline = time.monotonic() + max(timeout, 0.0)
        while True:
            with self.lock:
                regular_tasks = list(self.regular_tasks.values())
                bounded_tasks = list(self.bounded_tasks.values())
            for cancel_event, _thread in regular_tasks:
                cancel_event.set()
            for handle in bounded_tasks:
                handle.cancel()
            regular_done = all(not thread.is_alive() for _cancel_event, thread in regular_tasks)
            bounded_done = all(handle.done_event.is_set() for handle in bounded_tasks)
            if regular_done and (bounded_done or not require_bounded):
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            if pump_events is not None:
                pump_events()
            wait_time = min(0.005, remaining)
            for _cancel_event, thread in regular_tasks:
                if thread.is_alive():
                    thread.join(wait_time)
            if require_bounded:
                for handle in bounded_tasks:
                    if not handle.done_event.is_set():
                        handle.wait(wait_time)

    def _prune_bounded_locked(self) -> None:
        finished = [token for token, handle in self.bounded_tasks.items() if handle.done_event.is_set()]
        for token in finished:
            self.bounded_tasks.pop(token, None)
