from pydantic import BaseModel
from datetime import datetime
from typing import Any


class TaskOut(BaseModel):
    id: str
    type: str
    status: str
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None = None
    input: Any = None
    result: Any = None
    logs: list[dict] = []
    error: str | None = None

    model_config = {"from_attributes": True}

    @classmethod
    def from_orm_task(cls, task):
        import json
        return cls(
            id=task.id,
            type=task.type,
            status=task.status,
            created_at=task.created_at,
            updated_at=task.updated_at,
            finished_at=task.finished_at,
            input=json.loads(task.input) if task.input else None,
            result=json.loads(task.result) if task.result else None,
            logs=json.loads(task.logs) if task.logs else [],
            error=task.error,
        )


class TaskListOut(BaseModel):
    items: list[TaskOut]
    total: int


class FileOut(BaseModel):
    file_id: str
    original_name: str
    size_bytes: int | None = None


class ModelInfo(BaseModel):
    id: str
    display_name: str
    status: str
    model_type: str
    params_schema: dict = {}


class ModelsListOut(BaseModel):
    models: list[ModelInfo]


class TTSRunRequest(BaseModel):
    jsonl_file_id: str
    prompt_audio_file_id: str | None = None
    model_id: str = "fish-audio"
    prompt_map: dict[str, str] = {}   # basename -> file_id
    params: dict = {}


class TTSRunResponse(BaseModel):
    task_id: str


class AnalyzeJsonlRequest(BaseModel):
    jsonl_file_id: str


class PromptInfo(BaseModel):
    basename: str
    source_path: str
    exists_on_server: bool
    item_count: int


class AnalyzeJsonlResponse(BaseModel):
    total_items: int
    prompts: list[PromptInfo]
    missing_prompts: list[str]
    has_any_prompt: bool = False      # JSONL 里至少有一条 source_path 非空
    empty_source_path_count: int = 0  # source_path 为空的条目数


class BenchmarkRunListItem(BaseModel):
    id: str
    name: str | None
    status: str
    model_ids: list[str]
    created_at: datetime
    finished_at: datetime | None = None
    task_count: int


class BenchmarkRunListOut(BaseModel):
    items: list[BenchmarkRunListItem]
    total: int


class BenchmarkRunRequest(BaseModel):
    jsonl_file_id: str
    prompt_audio_file_id: str | None = None
    prompt_map: dict[str, str] = {}
    model_ids: list[str]
    params_per_model: dict[str, dict] = {}
    name: str | None = None


class BenchmarkRunResponse(BaseModel):
    benchmark_run_id: str
    task_ids: list[str]
    status: str


class BenchmarkTaskSummary(BaseModel):
    task_id: str
    model_id: str
    status: str
    total: int | None = None
    ok: int | None = None


class BenchmarkRunOut(BaseModel):
    id: str
    name: str | None
    status: str
    tasks: list[BenchmarkTaskSummary]


class ElevenLabsVoiceOut(BaseModel):
    voice_id: str
    voice_name: str
    prompt_hash: str
    task_id: str | None = None
    created_at: datetime
    deleted_at: datetime | None = None

    model_config = {"from_attributes": True}


class ElevenLabsVoiceListOut(BaseModel):
    items: list[ElevenLabsVoiceOut]


class MinimaxVoiceOut(BaseModel):
    provider_voice_id: str
    prompt_hash: str
    task_id: str | None = None
    created_at: datetime
    deleted_at: datetime | None = None

    model_config = {"from_attributes": True}


class DoubaoVoiceOut(BaseModel):
    speaker_id: str
    prompt_hash: str
    task_id: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class VoiceListOut(BaseModel):
    """统一多 provider 音色列表。"""
    elevenlabs: list[ElevenLabsVoiceOut] = []
    minimax: list[MinimaxVoiceOut] = []
    doubao: list[DoubaoVoiceOut] = []


class DatasetProcessResponse(BaseModel):
    task_id: str


class PromptExperimentRequest(BaseModel):
    prompt_file_ids: list[str]
    prompt_texts: dict[str, str] = {}
    business_context: dict
    model_ids: list[str]
    params_map: dict[str, dict] = {}
    test_text_count: int = 10
    synthesis_scene: str = ""


class ConfirmAsrRequest(BaseModel):
    asr_texts: list[str]   # 与 prompts 顺序一一对应


class ConfirmTextsRequest(BaseModel):
    confirmed_texts: list[str]


class ConfirmPlanRequest(BaseModel):
    confirmed_plan: list[dict]


class ReviewSubmission(BaseModel):
    human_selections: dict[str, str]
    evaluations: dict[str, dict]


class ExperimentHistoryItem(BaseModel):
    business_context_key: str
    prompt_combo: str
    model_id: str
    avg_score: float | None
    n_samples: int


class ExperimentHistoryResponse(BaseModel):
    items: list[ExperimentHistoryItem]
    total: int


# ── v3 克隆实验室 Schemas ────────────────────────────────────────────────────

class PromptExperimentV3Request(BaseModel):
    prompt_file_ids: list[str]
    prompt_annotations: dict[str, dict] = {}   # {file_id: {recording_env, speaking_pace, style, paralanguage}}
    scene_type: str
    scene_description: str = ""
    scene_form: dict = {}
    model_ids: list[str]
    params_map: dict[str, dict] = {}
    user_texts: list[str] = []
    user_texts_file_id: str | None = None
    max_synthesis_per_round: int = 30
    auto_text_count: int = 0   # 0=不限制(Agent自行决定), >0=Agent自动生成指定条数


class ConfirmAsrV3Request(BaseModel):
    prompts: list[dict]  # [{file_id, asr_text, annotation: {...}}]


class ConfirmPlanV3Request(BaseModel):
    approved: bool = True
    user_direction: str = ""


class ConfirmRoundRequest(BaseModel):
    confirmed_combo_ids: list[str]


class SubmitEvaluationRequest(BaseModel):
    round: int
    rankings_by_text: dict[str, list[dict]]  # text_idx → [{combo_id, rank}]
    per_combo: dict[str, dict] = {}           # eval_key → {vs_target, issues[], notes}
    winner: str | None = None                 # audio_label of winner, or null
    next_round_direction: str = ""
    finish_requested: bool = False
