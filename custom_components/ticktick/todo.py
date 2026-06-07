import asyncio
import json
import logging
from typing import Any
from datetime import date, datetime

from custom_components.ticktick.coordinator import TickTickCoordinator
from custom_components.ticktick.ticktick_api_python.models.task import Task, TaskStatus, TaskPriority

from homeassistant.components.todo import (
    TodoItem,
    TodoItemStatus,
    TodoListEntity,
    TodoListEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# JSON metadata separator
METADATA_SEPARATOR = " | _META_:"


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the TickTick todo platform config entry."""
    coordinator: TickTickCoordinator = hass.data[DOMAIN][entry.entry_id]
    projects = await coordinator.async_get_projects()
    async_add_entities(
        TickTickTodoListEntity(coordinator, entry.entry_id, project.id, project.name)
        for project in projects
    )


def _format_date_for_comparison(date_value) -> str:
    """Format a date value for comparison, handling different types."""
    if date_value is None:
        return ""
    if isinstance(date_value, datetime):
        # Convert datetime to string in a consistent format
        return date_value.isoformat()
    if isinstance(date_value, str):
        return date_value.strip()
    # For any other type, convert to string
    return str(date_value).strip()


def _extract_metadata_from_description(description: str) -> tuple[str, dict]:
    """Extract content and metadata from description.
    
    Format: "content | _META_:{\"priority\":\"HIGH\",\"tags\":[...],\"parent_task_id\":\"...\"}"
    
    Returns:
        Tuple of (clean_content, metadata_dict)
    """
    if not description:
        return "", {}
    
    if METADATA_SEPARATOR not in description:
        return description, {}
    
    try:
        content, meta_str = description.rsplit(METADATA_SEPARATOR, 1)
        metadata = json.loads(meta_str)
        content = content.strip()
        if content == "None":
            content = ""
        return content, metadata
    except (json.JSONDecodeError, ValueError):
        # If JSON parsing fails, return the whole description as content
        return description, {}


def _format_description_with_metadata(content: str, metadata: dict) -> str:
    """Format content and metadata into a description string.
    
    Returns:
        Formatted description with JSON metadata
    """
    if not metadata:
        return content or ""
    
    # Filter out empty/None values
    filtered_metadata = {k: v for k, v in metadata.items() if v is not None and v != ""}
    
    if not filtered_metadata:
        return content or ""
    
    return f"{content or ''}{METADATA_SEPARATOR}{json.dumps(filtered_metadata)}"


def _map_task(
    item: TodoItem, projectId: str, api_task: Task | None = None
) -> tuple[Task, bool]:
    """Convert a TodoItem to Task."""
    modified = False
    
    # Extract metadata from item description
    item_content, item_metadata = _extract_metadata_from_description(item.description or "")
    
    if api_task:
        if (item.summary or "").strip() != (api_task.title or "").strip():
            api_task.title = item.summary
            modified = True
        if (item_content or "").strip() != (api_task.content or "").strip():
            api_task.content = item_content or None
            modified = True

        # Handle isAllDay based on due date type
        is_all_day = (
            item.due is not None
            and not isinstance(item.due, datetime)
            and isinstance(item.due, date)
        )
        if api_task.isAllDay != is_all_day:
            api_task.isAllDay = is_all_day
            modified = True

        # Handle priority from metadata
        if "priority" in item_metadata:
            try:
                new_priority = TaskPriority[item_metadata["priority"]]
                if api_task.priority != new_priority:
                    api_task.priority = new_priority
                    modified = True
            except KeyError:
                pass  # Invalid priority, ignore
        
        # Handle tags from metadata
        if "tags" in item_metadata:
            if api_task.tags != item_metadata["tags"]:
                api_task.tags = item_metadata["tags"]
                modified = True
        
        # Handle parent_task_id from metadata
        if "parent_task_id" in item_metadata:
            if api_task.parentId != item_metadata["parent_task_id"]:
                api_task.parentId = item_metadata["parent_task_id"]
                modified = True
        
        # Handle due date comparison with proper type checking
        item_due_str = _format_date_for_comparison(item.due)
        api_due_str = _format_date_for_comparison(api_task.dueDate)

        if item_due_str != api_due_str:
            # If start date matches due date or is after new due date, update it too
            # to avoid API error (startDate cannot be after dueDate)
            api_start_str = _format_date_for_comparison(api_task.startDate)
            if api_task.startDate and (
                api_start_str == api_due_str or api_start_str > item_due_str
            ):
                api_task.startDate = item.due

            api_task.dueDate = item.due
            modified = True

        return api_task, modified

    # Create new task
    is_all_day = (
        item.due is not None
        and not isinstance(item.due, datetime)
        and isinstance(item.due, date)
    )

    metadata = {}
    if "priority" in item_metadata:
        try:
            metadata["priority"] = item_metadata["priority"]
        except (KeyError, ValueError):
            pass
    
    if "tags" in item_metadata:
        metadata["tags"] = item_metadata["tags"]
    
    if "parent_task_id" in item_metadata:
        metadata["parent_task_id"] = item_metadata["parent_task_id"]
    
    return Task(
        projectId=projectId,
        title=item.summary,
        content=item_content or None,
        dueDate=item.due,
        isAllDay=is_all_day,
        priority=TaskPriority[metadata.get("priority", "NONE")]
        if "priority" in metadata
        else None,
        parentId=metadata.get("parent_task_id"),
    ), modified


class TickTickTodoListEntity(CoordinatorEntity[TickTickCoordinator], TodoListEntity):
    """A TickTick TodoListEntity."""

    _attr_supported_features = (
        TodoListEntityFeature.CREATE_TODO_ITEM
        | TodoListEntityFeature.UPDATE_TODO_ITEM
        | TodoListEntityFeature.DELETE_TODO_ITEM
        | TodoListEntityFeature.SET_DUE_DATE_ON_ITEM
        | TodoListEntityFeature.SET_DUE_DATETIME_ON_ITEM
        | TodoListEntityFeature.SET_DESCRIPTION_ON_ITEM
    )

    def __init__(
        self,
        coordinator: TickTickCoordinator,
        config_entry_id: str,
        project_id: str,
        project_name: str,
    ) -> None:
        """Initialize TickTickTodoListEntity."""
        super().__init__(coordinator=coordinator)
        self._project_id = project_id
        self._attr_unique_id = f"{config_entry_id}-{project_id}"
        self._attr_name = project_name
        self._attr_todo_items = []

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""

        projects_with_tasks = self.coordinator.data

        if projects_with_tasks is None:
            self._attr_todo_items = None
        else:
            tasks_to_add = []
            for project_with_tasks in projects_with_tasks:
                if (
                    project_with_tasks.project.id != self._project_id
                    or not project_with_tasks.tasks
                ):
                    continue

                for task in project_with_tasks.tasks:
                    # Build metadata from task fields
                    metadata = {}
                    
                    if task.priority and task.priority != TaskPriority.NONE:
                        metadata["priority"] = task.priority.name
                    
                    if hasattr(task, "tags") and task.tags:
                        metadata["tags"] = task.tags
                    
                    if task.parentId:
                        metadata["parent_task_id"] = task.parentId
                    
                    # Format description with metadata
                    formatted_description = _format_description_with_metadata(
                        task.content or None, metadata
                    )
                    
                    tasks_to_add.insert(0,  # noqa: PERF401
                        TodoItem(
                            uid=task.id,
                            summary=task.title,
                            status=TodoItemStatus.COMPLETED
                            if task.status == TaskStatus.COMPLETED
                            else TodoItemStatus.NEEDS_ACTION,
                            due=task.dueDate,
                            description=formatted_description or None,
                        )
                    )

            self._attr_todo_items = tasks_to_add

        super()._handle_coordinator_update()

    async def async_create_todo_item(self, item: TodoItem) -> None:
        """Create a To-do item."""
        if item.status != TodoItemStatus.NEEDS_ACTION:
            raise ValueError("Only active tasks may be created.")
        mapped_task, _ = _map_task(item, self._project_id)
        try:
            created_task = await self.coordinator.api.create_task(mapped_task)
        except Exception as e:
            _LOGGER.error("Error creating TickTick task: %s", str(e))
            self.coordinator.async_request_refresh()
            return

        # Update local state optimistically
        if self.coordinator.data:
            for project_with_tasks in self.coordinator.data:
                if project_with_tasks.project.id == self._project_id:
                    if project_with_tasks.tasks is None:
                        project_with_tasks.tasks = []
                    project_with_tasks.tasks.append(created_task)
                    self.coordinator.async_set_updated_data(self.coordinator.data)
                    break

        self.coordinator.async_request_refresh()

    async def async_update_todo_item(self, item: TodoItem) -> None:
        """Update a To-do item."""

        async def process_status_change() -> bool:
            if item.status is not None:
                # Only update status if changed
                for existing_item in self._attr_todo_items or ():
                    if existing_item.uid != item.uid:
                        continue

                    if item.status != existing_item.status:
                        if item.status == TodoItemStatus.COMPLETED:
                            try:
                                await self.coordinator.api.complete_task(
                                    projectId=self._project_id, taskId=item.uid
                                )
                            except Exception as e:
                                _LOGGER.error(
                                    "Error completing TickTick task %s: %s",
                                    item.uid,
                                    str(e),
                                )
                                return False
                            # Update local state optimistically by removing the completed task
                            if self.coordinator.data:
                                for project_with_tasks in self.coordinator.data:
                                    if (
                                        project_with_tasks.project.id
                                        == self._project_id
                                        and project_with_tasks.tasks is not None
                                    ):
                                        project_with_tasks.tasks = [
                                            t
                                            for t in project_with_tasks.tasks
                                            if t.id != item.uid
                                        ]
                                        self.coordinator.async_set_updated_data(
                                            self.coordinator.data
                                        )
                                        break
                            return True
                        # else:
                        # Not supported by TickTick as they don't return completed tasks
            return False

        projects_with_tasks = self.coordinator.data
        api_task = next(
            (
                task
                for project_with_tasks in projects_with_tasks
                if project_with_tasks.tasks
                for task in project_with_tasks.tasks
                if task.id == item.uid
            ),
            None,
        )

        if await process_status_change():  # This should be changed if completing the task will support also changing description etc.
            self.coordinator.async_request_refresh()
            return

        mapped_task, is_modified = _map_task(item, self._project_id, api_task)

        if is_modified:
            try:
                updated_task = await self.coordinator.api.update_task(mapped_task)
            except Exception as e:
                _LOGGER.error("Error updating TickTick task %s: %s", item.uid, str(e))
                self.coordinator.async_request_refresh()
                return

            # Update local state optimistically
            if self.coordinator.data:
                for project_with_tasks in self.coordinator.data:
                    if (
                        project_with_tasks.project.id == self._project_id
                        and project_with_tasks.tasks is not None
                    ):
                        for i, task in enumerate(project_with_tasks.tasks):
                            if task.id == updated_task.id:
                                project_with_tasks.tasks[i] = updated_task
                                self.coordinator.async_set_updated_data(
                                    self.coordinator.data
                                )
                                break
                        break

        self.coordinator.async_request_refresh()

    async def async_delete_todo_items(self, uids: list[str]) -> None:
        """Delete a To-do item."""
        try:
            await asyncio.gather(
                *[
                    self.coordinator.api.delete_task(
                        projectId=self._project_id, taskId=uid
                    )
                    for uid in uids
                ]
            )
        except Exception as e:
            _LOGGER.error("Error deleting TickTick tasks %s: %s", uids, str(e))
        # Update local state optimistically
        if self.coordinator.data:
            for project_with_tasks in self.coordinator.data:
                if (
                    project_with_tasks.project.id == self._project_id
                    and project_with_tasks.tasks is not None
                ):
                    project_with_tasks.tasks = [
                        t for t in project_with_tasks.tasks if t.id not in uids
                    ]
                    self.coordinator.async_set_updated_data(self.coordinator.data)
                    break
        self.coordinator.async_request_refresh()

    async def async_added_to_hass(self) -> None:
        """When entity is added to hass update state from existing coordinator data."""
        await super().async_added_to_hass()
        self._handle_coordinator_update()
