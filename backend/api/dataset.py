import uuid
import json
import shutil
from pathlib import Path
from fastapi import APIRouter, UploadFile, File, Depends
from sqlalchemy.orm import Session
from typing import List

from backend.database import get_db
from backend.models.task import Task, File as FileModel
from backend.schemas.task import DatasetProcessResponse
from backend.config import UPLOADS_DIR

router = APIRouter()


@router.post("/process", response_model=DatasetProcessResponse)
async def process_dataset(
    files: List[UploadFile] = File(...),
    db: Session = Depends(get_db),
):
    task_id = str(uuid.uuid4())
    saved_paths = []

    for upload in files:
        file_id = str(uuid.uuid4())
        suffix = Path(upload.filename or "").suffix
        stored_name = f"{file_id}{suffix}"
        dest = UPLOADS_DIR / stored_name
        with dest.open("wb") as f:
            shutil.copyfileobj(upload.file, f)

        record = FileModel(
            id=file_id,
            original_name=upload.filename or stored_name,
            stored_path=str(dest),
            mime_type=upload.content_type,
            size_bytes=dest.stat().st_size,
        )
        db.add(record)
        saved_paths.append(str(dest))

    task_input = {"audio_paths": saved_paths}
    task = Task(
        id=task_id,
        type="dataset",
        input=json.dumps(task_input, ensure_ascii=False),
    )
    db.add(task)
    db.commit()
    return DatasetProcessResponse(task_id=task_id)
