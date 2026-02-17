"""Service Handlers for TickTick Integration."""

from collections.abc import Awaitable, Callable
from datetime import datetime
import logging
from typing import Any, TypeVar
from zoneinfo import ZoneInfo

from custom_components.ticktick.ticktick_api_python.models.task import (
    Task,
    TaskPriority,
)

from homeassistant.core import ServiceCall
from homeassistant.util import dt as dt_util

from .const import PROJECT_ID, TASK_ID
from .ticktick_api_python.ticktick_api import TickTickAPIClient

_LOGGER = logging.getLogger(__name__)


# === Task Scope ===
async def handle_get_task(client: TickTickAPIClient) -> Callable:
    """Return a handler function for the 'get_task' endpoint."""
    return await _create_handler(client.get_task, PROJECT_ID, TASK_ID)


async def handle_create_task(client: TickTickAPIClient) -> Callable:
    """Return a handler function for the 'create_task' endpoint."""
    return await _create_handler(client.create_task, *(Task.get_arg_names()), type=Task)


async def handle_complete_task(client: TickTickAPIClient) -> Callable:
    """Return a handler function for the 'complete_task' endpoint."""
    return await _create_handler(client.complete_task, PROJECT_ID, TASK_ID)


async def handle_delete_task(client: TickTickAPIClient) -> Callable:
    """Return a handler function for the 'delete_task' endpoint."""
    return await _create_handler(client.delete_task, PROJECT_ID, TASK_ID)


async def handle_copy_task(client: TickTickAPIClient) -> Callable:
    """Return a handler function for the 'copy_task' service."""

    async def handler(call: ServiceCall) -> dict[str, Any]:
        """Handle the copy_task service call."""
        source_project_id = call.data.get("source_project_id")
        source_task_id = call.data.get("source_task_id")
        dest_project_id = call.data.get("dest_project_id")
        subtask_updates = call.data.get("subtask_updates", [])

        if not source_project_id or not source_task_id or not dest_project_id:
            return {
                "error": "source_project_id, source_task_id, and dest_project_id are required"
            }

        try:
            # 1. Get all tasks for source project to find descendants
            source_project_data = await client.get_project_with_tasks(source_project_id)
            all_tasks = source_project_data.tasks
            if all_tasks is None:
                return {
                    "error": f"No tasks found in source project {source_project_id}"
                }

            # 2. Find the source task
            source_task = next((t for t in all_tasks if t.id == source_task_id), None)
            if not source_task:
                return {
                    "error": f"Source task {source_task_id} not found in project {source_project_id}"
                }

            # 3. Recursive copy function
            async def do_copy(task: Task, new_parent_id: str | None = None) -> str:
                target_priority = task.priority
                target_due_date = task.dueDate

                # Check for overrides by title
                if subtask_updates:
                    for update in subtask_updates:
                        if update.get("title") == task.title:
                            if "priority" in update:
                                priority_val = update["priority"]
                                try:
                                    if isinstance(priority_val, str):
                                        if priority_val.isdigit():
                                            target_priority = TaskPriority(int(priority_val))
                                        else:
                                            target_priority = TaskPriority[priority_val]
                                    else:
                                        target_priority = TaskPriority(priority_val)
                                except (KeyError, ValueError):
                                    _LOGGER.warning(
                                        "Invalid priority '%s' for subtask '%s'",
                                        priority_val,
                                        task.title,
                                    )
                            if "dueDate" in update:
                                target_due_date = _sanitize_date(
                                    update["dueDate"], update.get("timeZone")
                                )

                # Create the new task object
                new_task = Task(
                    projectId=dest_project_id,
                    title=task.title,
                    content=task.content,
                    desc=task.desc,
                    priority=target_priority,
                    dueDate=target_due_date,
                    parentId=new_parent_id,
                    isAllDay=task.isAllDay,
                    startDate=task.startDate,
                    timeZone=task.timeZone,
                    reminders=task.reminders,
                    repeatFlag=task.repeatFlag,
                    items=task.items,  # Checklist items
                )

                # Create task in TickTick
                created_task_json = await client.create_task(new_task, returnAsJson=True)
                new_task_id = created_task_json.get("id")

                # Find and copy children
                children = [t for t in all_tasks if t.parentId == task.id]
                for child in children:
                    await do_copy(child, new_task_id)

                return new_task_id

            new_root_id = await do_copy(source_task)
            return {"data": {"new_task_id": new_root_id}}

        except Exception as e:
            _LOGGER.error("Error copying task: %s", str(e))
            return {"error": str(e)}

    return handler


async def handle_update_task(client: TickTickAPIClient) -> Callable:
    """Return a handler function for the 'update_task' endpoint."""
    async def handler(call: ServiceCall) -> dict[str, Any]:
        """Handle the update_task service call."""
        project_id = call.data.get(PROJECT_ID)
        task_id = call.data.get(TASK_ID)
        
        if not project_id or not task_id:
            return {"error": f"Both {PROJECT_ID} and {TASK_ID} are required"}
        
        try:
            # First, get the existing task
            existing_task_response = await client.get_task(project_id, task_id, returnAsJson=True)
            
            # Create a Task object from the existing task data
            existing_task = Task.from_dict(existing_task_response)
            _LOGGER.debug("Retrieved existing task: %s", existing_task.title)
            
            # Update only the fields that are provided in the service call
            if "title" in call.data:
                existing_task.title = call.data.get("title")
                _LOGGER.debug("Updating task title to: %s", existing_task.title)
            
            # Handle both desc and content fields
            if "content" in call.data and "desc" in call.data:
                _LOGGER.warning("Both 'content' and 'desc' fields provided. Using 'content' field.")
                existing_task.content = call.data.get("content")
                existing_task.desc = call.data.get("desc")
                _LOGGER.debug("Updating task content to: %s", existing_task.content)
            elif "content" in call.data:
                existing_task.content = call.data.get("content")
                _LOGGER.debug("Updating task content to: %s", existing_task.content)
            elif "desc" in call.data:
                existing_task.content = call.data.get("desc")
                existing_task.desc = call.data.get("desc")
                _LOGGER.debug("Updating task content and desc to: %s", existing_task.content)
            
            if "dueDate" in call.data:
                due_date = call.data.get("dueDate")
                due_date_time_zone = call.data.get("timeZone")
                
                if isinstance(due_date, str):
                    existing_task.dueDate = _sanitize_date(due_date, due_date_time_zone)
                else:
                    existing_task.dueDate = due_date
                _LOGGER.debug("Updated task due date to: %s", existing_task.dueDate)
            
            # Handle additional fields
            if "isAllDay" in call.data:
                existing_task.isAllDay = call.data.get("isAllDay")
                _LOGGER.debug("Updating task isAllDay to: %s", existing_task.isAllDay)
            
            if "startDate" in call.data:
                start_date = call.data.get("startDate")
                start_date_time_zone = call.data.get("timeZone")
                
                if isinstance(start_date, str):
                    existing_task.startDate = _sanitize_date(start_date, start_date_time_zone)
                else:
                    existing_task.startDate = start_date
                _LOGGER.debug("Updated task start date to: %s", existing_task.startDate)
            
            if "repeatFlag" in call.data:
                existing_task.repeatFlag = call.data.get("repeatFlag")
                _LOGGER.debug("Updating task repeat flag to: %s", existing_task.repeatFlag)
            
            if "reminders" in call.data:
                reminders = call.data.get("reminders")
                # Ensure reminders is a list of strings
                if reminders is not None:
                    if isinstance(reminders, list):
                        existing_task.reminders = reminders
                    else:
                        # If a single string is provided, convert it to a list
                        existing_task.reminders = [reminders]
                    _LOGGER.debug("Updating task reminders to: %s", existing_task.reminders)
                else:
                    existing_task.reminders = []
                    _LOGGER.debug("Clearing task reminders")
            
            if "priority" in call.data:
                priority_val = call.data.get("priority")
                try:
                    if isinstance(priority_val, str):
                        if priority_val.isdigit():
                            existing_task.priority = TaskPriority(int(priority_val))
                        else:
                            existing_task.priority = TaskPriority[priority_val]
                    else:
                        existing_task.priority = TaskPriority(priority_val)
                    _LOGGER.debug("Updating task priority to: %s", existing_task.priority)
                except (KeyError, ValueError):
                    _LOGGER.warning("Invalid priority value: %s. Ignoring.", priority_val)
            
            if "sortOrder" in call.data:
                existing_task.sortOrder = call.data.get("sortOrder")
                _LOGGER.debug("Updating task sort order to: %s", existing_task.sortOrder)

            if "parentId" in call.data:
                existing_task.parentId = call.data.get("parentId")
                _LOGGER.debug("Updating task parentId to: %s", existing_task.parentId)
            
            # Update the task in TickTick
            response = await client.update_task(existing_task, returnAsJson=True)
            
            return {"data": response}
        except Exception as e:
            _LOGGER.error("Error updating task: %s", str(e))
            return {"error": str(e)}
    
    return handler


# === Project Scope ===
async def handle_get_projects(client: TickTickAPIClient) -> Callable:
    """Return a handler function for the 'get_projects' endpoint."""
    return await _create_handler(client.get_projects)


T = TypeVar("T")


async def _create_handler(
    client_method: Callable[..., Awaitable[Any]],
    *arg_names: str,
    type: type[T] | None = None,
) -> Callable:
    """Create a reusable handler function for TickTick API endpoints."""

    async def handler(call: ServiceCall) -> dict[str, Any]:
        """Return a generic handler for TickTick API endpoints."""

        args = {arg: call.data.get(arg) for arg in arg_names}
        try:
            response = None
            if type == Task:
                if "dueDate" in args and isinstance(args["dueDate"], str):
                    args["dueDate"] = _sanitize_date(args["dueDate"], args["timeZone"])
                if "startDate" in args and isinstance(args["startDate"], str):
                    args["startDate"] = _sanitize_date(
                        args["startDate"], args["timeZone"]
                    )
                if "priority" in args and args["priority"] is not None:
                    priority_val = args["priority"]
                    try:
                        if isinstance(priority_val, str):
                            if priority_val.isdigit():
                                args["priority"] = TaskPriority(int(priority_val))
                            else:
                                args["priority"] = TaskPriority[priority_val]
                        else:
                            args["priority"] = TaskPriority(priority_val)
                    except (KeyError, ValueError):
                        args["priority"] = None
                instance = type(**args)
                response = await client_method(instance, returnAsJson=True)
            else:
                response = await client_method(**args, returnAsJson=True)

            return {"data": response}  # noqa: TRY300
        except Exception as e:  # noqa: BLE001
            return {"error": str(e)}

    return handler


def _sanitize_date(date: str, timeZone: str | None) -> str:
    """Sanitize a date string to the format expected by TickTick API."""
    naive_dt = datetime.strptime(date, "%Y-%m-%d %H:%M:%S")
    
    if timeZone:
        zone_info = ZoneInfo(timeZone)
    else:
        zone_info = dt_util.get_default_time_zone()

    aware_dt = naive_dt.replace(tzinfo=zone_info)

    return aware_dt.strftime("%Y-%m-%dT%H:%M:%S%z")
