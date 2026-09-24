import uuid
import json
from datetime import datetime
from sqlalchemy import String, Integer, Float, DateTime, Text
from sqlalchemy.orm import Mapped, mapped_column
from backend.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


class EvalTask(Base):
    __tablename__ = "eval_tasks"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    jsonl_path: Mapped[str | None] = mapped_column(String, nullable=True)
    audio_dir: Mapped[str | None] = mapped_column(String, nullable=True)
    metrics: Mapped[str] = mapped_column(Text, nullable=False, default='["mos"]')
    wer_model: Mapped[str | None] = mapped_column(String, nullable=True)
    sim_model: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default="ready")
    total_groups: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_samples: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)

    def get_metrics(self) -> list:
        return json.loads(self.metrics or '["mos"]')


class EvalGroup(Base):
    __tablename__ = "eval_groups"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    eval_task_id: Mapped[str] = mapped_column(String, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    eval_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    checked_out_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    checked_out_session: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)


class EvalSample(Base):
    __tablename__ = "eval_samples"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    eval_task_id: Mapped[str] = mapped_column(String, nullable=False)
    eval_group_id: Mapped[str] = mapped_column(String, nullable=False)
    key: Mapped[str] = mapped_column(String, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    speaker: Mapped[str] = mapped_column(String, nullable=False, default="")
    speaker_type: Mapped[str] = mapped_column(String, nullable=False, default="")
    age: Mapped[str] = mapped_column(String, nullable=False, default="")
    gender: Mapped[str] = mapped_column(String, nullable=False, default="")
    test_set: Mapped[str] = mapped_column(String, nullable=False, default="")
    model: Mapped[str] = mapped_column(String, nullable=False, default="")
    audio_path: Mapped[str | None] = mapped_column(String, nullable=True)
    ref_audio_path: Mapped[str | None] = mapped_column(String, nullable=True)
    wer_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    sim_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)


class EvalRating(Base):
    __tablename__ = "eval_ratings"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    eval_task_id: Mapped[str] = mapped_column(String, nullable=False)
    eval_group_id: Mapped[str] = mapped_column(String, nullable=False)
    eval_sample_id: Mapped[str] = mapped_column(String, nullable=False)
    session_id: Mapped[str] = mapped_column(String, nullable=False, default="")
    mos_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    nmos_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    smos_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    issue_tags: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    notes: Mapped[str] = mapped_column(String, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)

    def get_issue_tags(self) -> list:
        return json.loads(self.issue_tags or "[]")
