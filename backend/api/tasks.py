import json
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import select, func

from backend.database import get_db
from backend.models.task import Task
from backend.schemas.task import TaskOut, TaskListOut

router = APIRouter()


@router.get("/{task_id}", response_model=TaskOut)
def get_task(task_id: str, db: Session = Depends(get_db)):
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return TaskOut.from_orm_task(task)


@router.get("", response_model=TaskListOut)
def list_tasks(
    type: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    q = select(Task)
    if type:
        q = q.where(Task.type == type)
    total = db.scalar(select(func.count()).select_from(q.subquery()))
    tasks = db.scalars(q.order_by(Task.created_at.desc()).limit(limit).offset(offset)).all()
    return TaskListOut(items=[TaskOut.from_orm_task(t) for t in tasks], total=total or 0)
