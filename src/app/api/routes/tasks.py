from fastapi import APIRouter, HTTPException, status
from urllib.parse import unquote_plus

from src.app.schemas.voice import Task, TaskCreate, TaskReplace, TaskUpdate

router = APIRouter(prefix="/tasks", tags=["tasks"])
tasks: list[Task] = []
next_task_id = 1


@router.get("", response_model=list[Task])
def get_tasks() -> list[Task]:
    return tasks


@router.post("", response_model=Task, status_code=status.HTTP_201_CREATED)
def create_task(payload: TaskCreate) -> Task:
    global next_task_id
    task = Task(id=next_task_id, title=payload.title, done=payload.done)
    tasks.append(task)
    next_task_id += 1
    return task


@router.get("/title/{title}", response_model=Task)
def get_task_by_title(title: str) -> Task:
    idx = find_task_index_by_title(title)
    return tasks[idx]


@router.put("/{task_id}", response_model=Task)
def replace_task(
    task_id: int,
    payload: TaskReplace,
) -> Task:
    idx = find_task_index(task_id)
    updated = Task(id=task_id, title=payload.title, done=payload.done)
    tasks[idx] = updated
    return updated


@router.put("/title/{title}", response_model=Task)
def replace_task_by_title(
    title: str,
    payload: TaskReplace,
) -> Task:
    idx = find_task_index_by_title(title)
    current = tasks[idx]
    updated = Task(id=current.id, title=payload.title, done=payload.done)
    tasks[idx] = updated
    return updated


@router.patch("/{task_id}", response_model=Task)
def update_task(
    task_id: int,
    payload: TaskUpdate,
) -> Task:
    idx = find_task_index(task_id)
    current = tasks[idx]
    updated = Task(
        id=current.id,
        title=payload.title if payload.title is not None else current.title,
        done=payload.done if payload.done is not None else current.done,
    )
    tasks[idx] = updated
    return updated


@router.patch("/title/{title}", response_model=Task)
def update_task_by_title(
    title: str,
    payload: TaskUpdate,
) -> Task:
    idx = find_task_index_by_title(title)
    current = tasks[idx]
    updated = Task(
        id=current.id,
        title=payload.title if payload.title is not None else current.title,
        done=payload.done if payload.done is not None else current.done,
    )
    tasks[idx] = updated
    return updated


@router.delete("/{task_id}")
def delete_task(task_id: int) -> dict[str, str]:
    idx = find_task_index(task_id)
    del tasks[idx]
    return {"message": f"Task {task_id} deleted."}


@router.delete("/title/{title}")
def delete_task_by_title(title: str) -> dict[str, str]:
    idx = find_task_index_by_title(title)
    deleted = tasks[idx]
    del tasks[idx]
    return {"message": f"Task '{deleted.title}' deleted."}


@router.delete("")
def delete_all_tasks() -> dict[str, str]:
    global next_task_id
    deleted_count = len(tasks)
    tasks.clear()
    next_task_id = 1
    return {"message": f"Deleted {deleted_count} task(s)."}


def find_task_index(task_id: int) -> int:
    for idx, task in enumerate(tasks):
        if task.id == task_id:
            return idx

    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"Task {task_id} not found.",
    )


def find_task_index_by_title(title: str) -> int:
    wanted = unquote_plus(title).strip().lower()
    for idx, task in enumerate(tasks):
        if task.title.strip().lower() == wanted:
            return idx

    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"Task '{title}' not found.",
    )
