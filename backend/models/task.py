import uuid
import json
from datetime import datetime
from sqlalchemy import String, Integer, DateTime, Text, Boolean
from sqlalchemy.orm import Mapped, mapped_column
from backend.database import Base


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    type: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    input: Mapped[str | None] = mapped_column(Text, nullable=True)
    result: Mapped[str | None] = mapped_column(Text, nullable=True)
    logs: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    def append_log(self, msg: str, level: str = "info"):
        logs = json.loads(self.logs or "[]")
        logs.append({"time": datetime.utcnow().isoformat(), "level": level, "msg": msg})
        self.logs = json.dumps(logs, ensure_ascii=False)


class File(Base):
    __tablename__ = "files"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    original_name: Mapped[str] = mapped_column(String, nullable=False)
    stored_path: Mapped[str] = mapped_column(String, nullable=False)
    mime_type: Mapped[str | None] = mapped_column(String, nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)


class ModelRegistry(Base):
    __tablename__ = "model_registry"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    display_name: Mapped[str] = mapped_column(String, nullable=False)
    model_type: Mapped[str] = mapped_column(String, nullable=False)   # direct_python | sglang | api
    conda_env: Mapped[str | None] = mapped_column(String, nullable=True)
    venv_python: Mapped[str | None] = mapped_column(String, nullable=True)
    runner_script: Mapped[str | None] = mapped_column(String, nullable=True)
    model_dir: Mapped[str | None] = mapped_column(String, nullable=True)
    extra_config: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    params_schema: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    status: Mapped[str] = mapped_column(String, nullable=False, default="active")
    status_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    install_log: Mapped[str | None] = mapped_column(Text, nullable=True)
    installed_by: Mapped[str | None] = mapped_column(String, nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)


class BenchmarkRun(Base):
    __tablename__ = "benchmark_run"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str | None] = mapped_column(String, nullable=True)
    jsonl_path: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_audio_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt_map: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    model_ids: Mapped[str] = mapped_column(Text, nullable=False)   # JSON array
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ElevenLabsVoice(Base):
    __tablename__ = "elevenlabs_voices"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    voice_id: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    voice_name: Mapped[str] = mapped_column(String, nullable=False)
    prompt_hash: Mapped[str] = mapped_column(String, nullable=False)
    task_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class MinimaxVoice(Base):
    __tablename__ = "minimax_voices"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    provider_voice_id: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    prompt_hash: Mapped[str] = mapped_column(String, nullable=False)
    task_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class DoubaoVoice(Base):
    __tablename__ = "doubao_voices"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    speaker_id: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    prompt_hash: Mapped[str] = mapped_column(String, nullable=False)
    task_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)


class QwenTtsVoice(Base):
    __tablename__ = "qwen_tts_voices"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    voice_id: Mapped[str] = mapped_column(String, nullable=False)          # enrollment 返回的完整 voice_id
    prompt_hash: Mapped[str] = mapped_column(String, nullable=False)
    target_model: Mapped[str] = mapped_column(String, nullable=False)      # flash 或 plus
    task_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)


class SynthesisRecord(Base):
    __tablename__ = "synthesis_records"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    task_id: Mapped[str] = mapped_column(String, nullable=False)

    scene_type: Mapped[str | None] = mapped_column(String, nullable=True)
    scene_description: Mapped[str | None] = mapped_column(Text, nullable=True)

    round_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    combo_id: Mapped[str] = mapped_column(String, nullable=False)
    combo_type: Mapped[str | None] = mapped_column(String, nullable=True)

    prompt_ids: Mapped[str | None] = mapped_column(String, nullable=True)
    model_id: Mapped[str] = mapped_column(String, nullable=False)
    text_strategy: Mapped[str | None] = mapped_column(String, nullable=True)
    synthesis_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    synthesis_params: Mapped[str | None] = mapped_column(Text, nullable=True)
    audio_url: Mapped[str | None] = mapped_column(String, nullable=True)
    sub_task_id: Mapped[str | None] = mapped_column(String, nullable=True)

    human_rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    vs_target: Mapped[str | None] = mapped_column(String, nullable=True)
    issue_tags: Mapped[str | None] = mapped_column(String, nullable=True)
    human_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_winner: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)


class PromptExperiment(Base):
    __tablename__ = "prompt_experiments"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    task_id: Mapped[str] = mapped_column(String, nullable=False)
    business_context_key: Mapped[str] = mapped_column(String, nullable=False)
    character_type: Mapped[str | None] = mapped_column(String, nullable=True)
    emotion_register: Mapped[str | None] = mapped_column(String, nullable=True)
    content_type: Mapped[str | None] = mapped_column(String, nullable=True)
    language_style: Mapped[str | None] = mapped_column(String, nullable=True)
    special_req: Mapped[str | None] = mapped_column(String, nullable=True)
    prompt_combo: Mapped[str] = mapped_column(String, nullable=False)
    prompt_hash: Mapped[str] = mapped_column(String, nullable=False)
    model_id: Mapped[str] = mapped_column(String, nullable=False)
    params_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    synthesis_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    audio_url: Mapped[str | None] = mapped_column(String, nullable=True)
    human_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    problem_tags: Mapped[str | None] = mapped_column(String, nullable=True)
    human_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
