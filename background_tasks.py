"""Background task manager to prevent task accumulation."""
import asyncio
import logging
from typing import Dict, Optional

logger = logging.getLogger(__name__)


class BackgroundTaskManager:
    """
    Manages background tasks to prevent accumulation and provide tracking.
    Limits the number of concurrent background tasks and tracks active tasks.
    """
    def __init__(self, max_concurrent_tasks: int = 100):
        self.max_concurrent_tasks = max_concurrent_tasks
        self.active_tasks: Dict[str, asyncio.Task] = {}
        self.task_counter = 0
        self._lock = asyncio.Lock()
        
    async def _safe_task_wrapper(self, task_id: str, coro) -> None:
        """
        Safely executes a background coroutine with error handling.
        Removes the task from active_tasks when done.
        """
        try:
            await coro
        except Exception as e:
            logger.error(f"Background task {task_id} failed: {e}", exc_info=True)
        finally:
            async with self._lock:
                self.active_tasks.pop(task_id, None)
    
    async def create_task(self, coro, task_name: str = None) -> Optional[str]:
        """
        Creates a tracked background task. Returns task_id if created, None if rejected.
        
        Args:
            coro: The coroutine to run in background
            task_name: Optional name for the task (for logging)
            
        Returns:
            task_id if task was created, None if rejected due to max limit
        """
        async with self._lock:
            # Clean up completed tasks first
            self.active_tasks = {
                task_id: task for task_id, task in self.active_tasks.items()
                if not task.done()
            }
            
            # Check if we're at max capacity
            if len(self.active_tasks) >= self.max_concurrent_tasks:
                logger.warning(
                    f"Rejected background task '{task_name or 'unnamed'}': "
                    f"{len(self.active_tasks)}/{self.max_concurrent_tasks} tasks active"
                )
                return None
            
            # Create new task
            self.task_counter += 1
            task_id = f"task_{self.task_counter}_{task_name or 'unnamed'}"
            task = asyncio.create_task(self._safe_task_wrapper(task_id, coro))
            self.active_tasks[task_id] = task
            
            if task_name:
                logger.debug(f"Created background task: {task_id} ({len(self.active_tasks)}/{self.max_concurrent_tasks} active)")
            
            return task_id
    
    def get_active_task_count(self) -> int:
        """Returns the current number of active tasks."""
        return len(self.active_tasks)


# Global task manager instance
_task_manager = BackgroundTaskManager(max_concurrent_tasks=100)


async def safe_background_task(coro) -> None:
    """
    Safely executes a background coroutine with error handling.
    Prevents unhandled exceptions in fire-and-forget tasks.
    Uses the global task manager to prevent task accumulation.
    """
    await _task_manager.create_task(coro, task_name="safe_background_task")


def get_task_manager() -> BackgroundTaskManager:
    """Get the global task manager instance."""
    return _task_manager

