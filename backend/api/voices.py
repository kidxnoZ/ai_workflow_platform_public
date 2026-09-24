"""音色管理 API：列出 / 删除各 provider 克隆音色。"""
import json
import os
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.models.task import DoubaoVoice, ElevenLabsVoice, MinimaxVoice, ModelRegistry
from backend.schemas.task import (
    DoubaoVoiceOut,
    ElevenLabsVoiceListOut,
    ElevenLabsVoiceOut,
    MinimaxVoiceOut,
    VoiceListOut,
)

router = APIRouter()


# ── 统一列表（所有 provider）────────────────────────────────────────────────────

@router.get("", response_model=VoiceListOut)
def list_all_voices(include_deleted: bool = False, db: Session = Depends(get_db)):
    el_q = db.query(ElevenLabsVoice)
    mm_q = db.query(MinimaxVoice)
    db_q = db.query(DoubaoVoice)

    if not include_deleted:
        el_q = el_q.filter(ElevenLabsVoice.deleted_at.is_(None))
        mm_q = mm_q.filter(MinimaxVoice.deleted_at.is_(None))

    return VoiceListOut(
        elevenlabs=[ElevenLabsVoiceOut.model_validate(v)
                    for v in el_q.order_by(ElevenLabsVoice.created_at.desc()).all()],
        minimax=[MinimaxVoiceOut.model_validate(v)
                 for v in mm_q.order_by(MinimaxVoice.created_at.desc()).all()],
        doubao=[DoubaoVoiceOut.model_validate(v)
                for v in db_q.order_by(DoubaoVoice.created_at.desc()).all()],
    )


# ── ElevenLabs ─────────────────────────────────────────────────────────────────

@router.get("/elevenlabs", response_model=ElevenLabsVoiceListOut)
def list_elevenlabs_voices(include_deleted: bool = False, db: Session = Depends(get_db)):
    q = db.query(ElevenLabsVoice)
    if not include_deleted:
        q = q.filter(ElevenLabsVoice.deleted_at.is_(None))
    voices = q.order_by(ElevenLabsVoice.created_at.desc()).all()
    return ElevenLabsVoiceListOut(items=[ElevenLabsVoiceOut.model_validate(v) for v in voices])


@router.delete("/elevenlabs/{voice_id}")
def delete_elevenlabs_voice(voice_id: str, db: Session = Depends(get_db)):
    return _do_delete_elevenlabs(voice_id, db)


# 旧路由兼容：DELETE /api/voices/{voice_id} → ElevenLabs 删除
@router.delete("/{voice_id}")
def delete_voice_compat(voice_id: str, db: Session = Depends(get_db)):
    return _do_delete_elevenlabs(voice_id, db)


def _do_delete_elevenlabs(voice_id: str, db: Session):
    record = db.query(ElevenLabsVoice).filter(
        ElevenLabsVoice.voice_id == voice_id
    ).first()
    if not record:
        raise HTTPException(status_code=404, detail="Voice not found")
    if record.deleted_at:
        raise HTTPException(status_code=400, detail="Voice already deleted")

    server_deleted = False
    elmodel = db.query(ModelRegistry).filter(ModelRegistry.id == "elevenlabs").first()
    if elmodel:
        extra = json.loads(elmodel.extra_config or "{}")
        api_key = os.environ.get(extra.get("api_key_env", "ELEVENLABS_API_KEY"), "")
        if api_key:
            try:
                from elevenlabs.client import ElevenLabs as _EL
                _EL(api_key=api_key).voices.delete(voice_id)
                server_deleted = True
            except Exception:
                pass

    if not server_deleted:
        raise HTTPException(status_code=502,
            detail="ElevenLabs 服务端删除失败，voice_id 仍在服务器上，请稍后重试")

    record.deleted_at = datetime.utcnow()
    db.commit()
    return {"ok": True}


# ── Minimax 删除────────────────────────────────────────────────────────────────

@router.delete("/minimax/{provider_voice_id}")
def delete_minimax_voice(provider_voice_id: str, db: Session = Depends(get_db)):
    record = db.query(MinimaxVoice).filter(
        MinimaxVoice.provider_voice_id == provider_voice_id
    ).first()
    if not record:
        raise HTTPException(status_code=404, detail="Voice not found")
    if record.deleted_at:
        raise HTTPException(status_code=400, detail="Voice already deleted")

    server_deleted = False
    mmmodel = db.query(ModelRegistry).filter(ModelRegistry.id == "minimax").first()
    if mmmodel:
        extra = json.loads(mmmodel.extra_config or "{}")
        auth    = os.environ.get(extra.get("auth_env", "TTS_AUTH"), "")
        api_url = extra.get("api_url", "")
        if auth and api_url:
            import requests
            headers = {"Authorization": f"Bearer {auth}", "Content-Type": "application/json"}
            for voice_type in ("voice_cloning", "voice_generation"):
                payload = {
                    "model":      "minimax-voice-delete",
                    "voice_type": voice_type,
                    "voice_id":   provider_voice_id,
                }
                try:
                    resp = requests.post(api_url, json=payload, headers=headers, timeout=60)
                    if resp.ok and resp.json().get("base_resp", {}).get("status_code") == 0:
                        server_deleted = True
                        break
                except Exception:
                    pass

    if not server_deleted:
        # 服务端删除失败，voice_id 仍存在于 minimax 服务器，不标记为已删除
        # 避免下次任务用同一 voice_id 重建时被服务端拒绝
        raise HTTPException(status_code=502,
            detail="minimax 服务端删除失败，voice_id 仍在服务器上，请稍后重试")

    record.deleted_at = datetime.utcnow()
    db.commit()
    return {"ok": True}


# ── Doubao（只读，暂无删除）────────────────────────────────────────────────────

@router.get("/doubao")
def list_doubao_voices(db: Session = Depends(get_db)):
    voices = db.query(DoubaoVoice).order_by(DoubaoVoice.created_at.desc()).all()
    return {"items": [DoubaoVoiceOut.model_validate(v) for v in voices]}
