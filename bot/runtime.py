import asyncio
import logging
from collections.abc import Awaitable
from typing import TypeVar

logger = logging.getLogger(__name__)

# Track all background tasks for proper cleanup on shutdown.
_background_tasks: set[asyncio.Future] = set()
T = TypeVar("T")


def _task_done(task: asyncio.Future) -> None:
    _background_tasks.discard(task)
    if task.cancelled():
        return
    try:
        error = task.exception()
    except (asyncio.CancelledError, asyncio.InvalidStateError):
        return
    if error is not None:
        logger.error(
            "Background task %s failed",
            task.get_name() if isinstance(task, asyncio.Task) else type(task).__name__,
            exc_info=(type(error), error, error.__traceback__),
        )


def _track_task(task: asyncio.Future[T]) -> asyncio.Future[T]:
    """Track a task/future, retrieve failures, and remove it when finished."""
    _background_tasks.add(task)
    task.add_done_callback(_task_done)
    return task


def create_background_task(awaitable: Awaitable[T], *, name: str) -> asyncio.Task[T]:
    """Create a named task whose errors and shutdown are centrally managed."""
    return _track_task(asyncio.create_task(awaitable, name=name))  # type: ignore[return-value]

async def cleanup_background_tasks() -> None:
    """Cancel tracked tasks and await their cleanup in application shutdown."""
    tasks = tuple(_background_tasks)
    if not tasks:
        return
    logger.info("Cancelling %d background tasks...", len(tasks))
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def _cleanup_background_tasks() -> None:
    """Synchronous compatibility wrapper; use cleanup_background_tasks in PTB."""
    for task in tuple(_background_tasks):
        task.cancel()
