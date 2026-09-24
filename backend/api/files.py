import uuid
import shutil
from pathlib import Path
from fastapi import APIRouter, UploadFile, File, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.models.task import File as FileModel
from backend.schemas.task import FileOut
from backend.config import UPLOADS_DIR

router = APIRouter()


@router.post("/upload", response_model=FileOut)
async def upload_file(file: UploadFile = File(...), db: Session = Depends(get_db)):
    file_id = str(uuid.uuid4())
    suffix = Path(file.filename or "").suffix
    stored_name = f"{file_id}{suffix}"
    dest = UPLOADS_DIR / stored_name

    with dest.open("wb") as f:
        shutil.copyfileobj(file.file, f)

    size = dest.stat().st_size
    record = FileModel(
        id=file_id,
        original_name=file.filename or stored_name,
        stored_path=str(dest),
        mime_type=file.content_type,
        size_bytes=size,
    )
    db.add(record)
    db.commit()

    return FileOut(file_id=file_id, original_name=record.original_name, size_bytes=size)


@router.get("/{file_id}/download")
def download_file(file_id: str, db: Session = Depends(get_db)):
    f = db.get(FileModel, file_id)
    if not f:
        raise HTTPException(status_code=404, detail="File not found")
    path = Path(f.stored_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="File not found on disk")
    return FileResponse(
        path=str(path),
        media_type=f.mime_type or "audio/wav",
        filename=f.original_name,
    )


@router.get("/{file_id}")
def get_file_info(file_id: str, db: Session = Depends(get_db)):
    f = db.get(FileModel, file_id)
    if not f:
        raise HTTPException(status_code=404, detail="File not found")
    return {"file_id": f.id, "original_name": f.original_name, "size_bytes": f.size_bytes}
