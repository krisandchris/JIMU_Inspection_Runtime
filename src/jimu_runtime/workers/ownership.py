"""Transfer ownership acquired by blocking resources without leaking on cancellation."""

import asyncio
from collections.abc import Callable
from concurrent.futures import Future
from contextlib import suppress
from threading import Lock, Thread

from jimu_runtime.workers.resources import ResourceUnavailable


async def acquire_with_compensation[T](
    operation: Callable[[], T],
    abandon: Callable[[T], object],
) -> T:
    """Transfer the result or compensate it without relying on a live event loop."""
    lock = Lock()
    owned: list[T] = []
    cancelled = False

    def run() -> T:
        value = operation()
        with lock:
            if not cancelled:
                owned.append(value)
                return value
        with suppress(OSError, ResourceUnavailable):
            abandon(value)
        return value

    try:
        return await asyncio.to_thread(run)
    except asyncio.CancelledError:
        with lock:
            cancelled = True
            done = tuple(owned)
        if done:
            cleanup: Future[None] = Future()

            def release() -> None:
                try:
                    abandon(done[0])
                except Exception as exc:
                    cleanup.set_exception(exc)
                else:
                    cleanup.set_result(None)

            Thread(target=release, name="jimu-resource-compensate").start()
            with suppress(OSError, ResourceUnavailable):
                await asyncio.shield(asyncio.wrap_future(cleanup))
        raise
