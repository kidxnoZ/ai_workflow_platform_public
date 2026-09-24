"""backend/schemas/dataset_ingest.py

Pydantic request/response models for the dataset ingest API.
"""
from __future__ import annotations
from typing import Any, Literal
from pydantic import BaseModel


class IngestRequest(BaseModel):
    source_type: Literal["server_path", "upload"]
    source_path: str | None = None
    upload_file_id: str | None = None
    output_path: str
    project_code: str
    source_label: str
    copyright: str
    domain: str
    language: str
    steps: list[str]


class IngestResponse(BaseModel):
    task_id: str


class ActionRequest(BaseModel):
    action: Literal["approve", "reject", "submit_review", "manual_move",
                    "resubmit_script", "sandbox_override", "cancel"]
    step: str
    feedback: str | None = None
    merge_tasks: list[dict[str, Any]] | None = None
    review_items: list[dict[str, Any]] | None = None
    move_src: str | None = None
    move_dst: str | None = None
    save_experience: bool = False  # explicit gate: only write to experience lib if True


class ActionResponse(BaseModel):
    ok: bool
    message: str | None = None


class DirTreeNode(BaseModel):
    name: str
    type: Literal["dir", "file"]
    file_count: int | None = None
    children: list["DirTreeNode"] = []


class DirTreeResponse(BaseModel):
    tree: DirTreeNode
    role_count: int
    special_count: int
    integrity_ok: bool
    integrity_detail: str


class PreviewSample(BaseModel):
    key: str
    speaker_id: str
    before_url: str
    after_url: str
    duration_sec: float


class ParamGroupPreview(BaseModel):
    group_id: str
    speaker_ids: list[str]
    params: dict[str, Any]
    samples: list[PreviewSample]


class PreviewResponse(BaseModel):
    step: str
    groups: list[ParamGroupPreview]


class ReviewItem(BaseModel):
    key: str
    path: str
    audio_url: str
    text: str
    speaker: str
    duration: float
    flags: list[str]
    # human review output fields
    text_edited: str | None = None
    quality_poor: int = 0
    paralanguage_heavy: int = 0
    hardcode_error: int = 0
    too_short: int = 0


class ReviewItemsResponse(BaseModel):
    items: list[ReviewItem]
    total: int
    page: int
    size: int


class IntegrityResult(BaseModel):
    ok: bool
    initial_count: int
    role_audio_count: int
    special_count: int
    current_total: int
    detail: str
