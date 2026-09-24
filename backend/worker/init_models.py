"""初始化 10 个 TTS 模型注册数据（幂等，已存在则跳过）。"""
import json
import os
from sqlalchemy.orm import Session
from backend.models.task import ModelRegistry

RUNNERS_DIR = "/root/workspace/ai_workflow_platform/backend/runners"

INITIAL_MODELS = [
    {
        "id": "fish-audio",
        "display_name": "Fish-Audio S2",
        "model_type": "direct_python",
        "conda_env": "fish-speech",
        "runner_script": f"{RUNNERS_DIR}/run_fish_audio.py",
        "model_dir": "/root/workspace/ckpt/s2-pro",
        "params_schema": "{}",
        "status": "active",
        "sort_order": 1,
    },
    {
        "id": "qwen3-tts",
        "display_name": "Qwen3-TTS",
        "model_type": "direct_python",
        "conda_env": "tts_clone",
        "runner_script": f"{RUNNERS_DIR}/run_qwen3_tts.py",
        "model_dir": "/root/workspace/ckpt/Qwen3-TTS-12Hz-1.7B-Base",
        "params_schema": json.dumps({
            "properties": {
                "temperature": {
                    "type": "number", "default": 0.9, "minimum": 0.0, "maximum": 2.0,
                    "title": "随机性", "ui:widget": "slider",
                },
                "top_k": {
                    "type": "integer", "default": 50, "minimum": 1, "maximum": 200,
                    "title": "Top K", "ui:widget": "input",
                },
            }
        }),
        "status": "active",
        "sort_order": 2,
    },
    {
        "id": "voxcpm2",
        "display_name": "VoxCPM2",
        "model_type": "api",
        "extra_config": json.dumps({
            "triton_server":        os.getenv("TRITON_SERVER", ""),
            "keepalive_time_ms":    600000,
            "keepalive_timeout_ms": 60000,
            "service_id":           "5000",
            "sub_service_id":       "5",
        }),
        "params_schema": "{}",
        "status": "active",
        "sort_order": 3,
    },
    {
        "id": "index-tts2",
        "display_name": "Index-TTS2",
        "model_type": "direct_python",
        "venv_python": "/root/workspace/index-tts/.venv/bin/python",
        "runner_script": f"{RUNNERS_DIR}/run_index_tts2.py",
        "model_dir": "/root/workspace/ckpt/IndexTTS-2",
        "params_schema": "{}",
        "status": "active",
        "sort_order": 4,
    },
    {
        "id": "cosyvoice2",
        "display_name": "CosyVoice2",
        "model_type": "direct_python",
        "conda_env": "cosyvoice",
        "runner_script": f"{RUNNERS_DIR}/run_cosyvoice2.py",
        "model_dir": "/root/workspace/ckpt/CosyVoice2-0.5B",
        "params_schema": "{}",
        "status": "active",
        "sort_order": 5,
    },
    {
        "id": "cosyvoice3",
        "display_name": "CosyVoice3",
        "model_type": "direct_python",
        "conda_env": "cosyvoice",
        "runner_script": f"{RUNNERS_DIR}/run_cosyvoice3.py",
        "model_dir": "/root/workspace/ckpt/Fun-CosyVoice3-0.5B-2512",
        "params_schema": "{}",
        "status": "active",
        "sort_order": 6,
    },
    {
        "id": "higgs",
        "display_name": "Higgs Audio V3",
        "model_type": "sglang",
        "extra_config": json.dumps({
            "model_path": "/root/workspace/ckpt/higgs-audio-v3",
            "request_format": "higgs",
        }),
        "params_schema": json.dumps({
            "properties": {
                "temperature": {
                    "type": "number", "default": 0.8, "minimum": 0.0, "maximum": 2.0,
                    "title": "随机性", "ui:widget": "slider",
                },
                "top_k": {
                    "type": "integer", "default": 50, "minimum": 1, "maximum": 200,
                    "title": "Top K", "ui:widget": "input",
                },
                "max_new_tokens": {
                    "type": "integer", "default": 1024, "minimum": 128, "maximum": 4096,
                    "title": "最大 Token 数", "ui:widget": "input",
                },
            }
        }),
        "status": "active",
        "sort_order": 7,
    },
    {
        "id": "moss",
        "display_name": "MOSS-TTS v1.5",
        "model_type": "sglang",
        "extra_config": json.dumps({
            "model_path": "/root/workspace/ckpt/MOSS-TTS-v1.5",
            "yaml_config": "/root/workspace/sglang-omni/examples/configs/moss_tts.yaml",
            "request_format": "moss",
        }),
        "params_schema": json.dumps({
            "properties": {
                "max_new_tokens": {
                    "type": "integer", "default": 1024, "minimum": 128, "maximum": 4096,
                    "title": "最大 Token 数", "ui:widget": "input",
                },
            }
        }),
        "status": "active",
        "sort_order": 8,
    },
    {
        "id": "elevenlabs",
        "display_name": "ElevenLabs IVC",
        "model_type": "api",
        "extra_config": json.dumps({
            "api_base_url": "https://api.elevenlabs.io",
            "api_key_env": "ELEVENLABS_API_KEY",
        }),
        "params_schema": json.dumps({
            "properties": {
                "stability": {
                    "type": "number", "default": 0.5, "minimum": 0.0, "maximum": 1.0,
                    "title": "稳定性", "ui:widget": "slider",
                },
                "similarity_boost": {
                    "type": "number", "default": 0.75, "minimum": 0.0, "maximum": 1.0,
                    "title": "相似度", "ui:widget": "slider",
                },
                "delete_after_task": {
                    "type": "boolean", "default": False,
                    "title": "完成后删除克隆音色", "ui:widget": "checkbox",
                },
            }
        }),
        "status": "active",
        "sort_order": 9,
    },
    {
        "id": "mimo",
        "display_name": "MIMO v2.5",
        "model_type": "api",
        "extra_config": json.dumps({
            "api_base_url": "https://api.xiaomimimo.com/v1",
            "api_key_env": "MIMO_API_KEY",
            "model_name": "mimo-v2.5-tts-voiceclone",
        }),
        "params_schema": json.dumps({
            "properties": {
                "instruction": {
                    "type": "string", "default": "",
                    "title": "自然语言风格指令", "ui:widget": "textarea",
                },
            }
        }),
        "status": "active",
        "sort_order": 10,
    },
    {
        "id": "minimax",
        "display_name": "Minimax TTS 2.8",
        "model_type": "api",
        "extra_config": json.dumps({
            "api_url":    os.getenv("AIGC_GATEWAY_URL", ""),
            "upload_url": os.getenv("MINIMAX_UPLOAD_URL", ""),
            "auth_env":   "TTS_AUTH",
        }),
        "params_schema": json.dumps({
            "properties": {
                "delete_after_task": {
                    "type": "boolean", "default": False,
                    "title": "完成后删除克隆音色", "ui:widget": "checkbox",
                },
            }
        }),
        "status": "active",
        "sort_order": 11,
    },
    {
        "id": "doubao",
        "display_name": "Doubao 语音克隆",
        "model_type": "api",
        "extra_config": json.dumps({
            "api_url":          os.getenv("AIGC_GATEWAY_URL", ""),
            "apply_url":        os.getenv("DOUBAO_APPLY_URL", ""),
            "auth_env":         "TTS_AUTH",
            "apply_app_id":     os.getenv("DOUBAO_APPLY_APP_ID", ""),
            "apply_project_id": os.getenv("DOUBAO_APPLY_PROJECT_ID", ""),
            "user":             os.getenv("DOUBAO_USER", ""),
        }),
        "params_schema": "{}",
        "status": "active",
        "sort_order": 12,
    },
    {
        "id": "qwen-tts-flash",
        "display_name": "Qwen-TTS Flash",
        "model_type": "api",
        "extra_config": json.dumps({
            "api_key_env":  "TTS_AUTH",
            "model_name":   "qwen-audio-3.0-tts-flash",
        }),
        "params_schema": json.dumps({
            "properties": {
                "instructions": {
                    "type": "string", "default": "",
                    "title": "风格指令（自然语言）", "ui:widget": "textarea",
                },
            }
        }),
        "status": "active",
        "sort_order": 13,
    },
    {
        "id": "qwen-tts-plus",
        "display_name": "Qwen-TTS Plus",
        "model_type": "api",
        "extra_config": json.dumps({
            "api_key_env":  "TTS_AUTH",
            "model_name":   "qwen-audio-3.0-tts-plus",
        }),
        "params_schema": json.dumps({
            "properties": {
                "instructions": {
                    "type": "string", "default": "",
                    "title": "风格指令（自然语言）", "ui:widget": "textarea",
                },
            }
        }),
        "status": "active",
        "sort_order": 14,
    },
]


def init_models(db: Session) -> None:
    for data in INITIAL_MODELS:
        existing = db.get(ModelRegistry, data["id"])
        if existing:
            existing.model_type   = data["model_type"]
            existing.params_schema = data.get("params_schema", "{}")
            existing.extra_config = data.get("extra_config", "{}")
            existing.venv_python  = data.get("venv_python")
            existing.runner_script = data.get("runner_script")
            db.add(existing)
        else:
            row = ModelRegistry(
                id=data["id"],
                display_name=data["display_name"],
                model_type=data["model_type"],
                conda_env=data.get("conda_env"),
                venv_python=data.get("venv_python"),
                runner_script=data.get("runner_script"),
                model_dir=data.get("model_dir"),
                extra_config=data.get("extra_config", "{}"),
                params_schema=data.get("params_schema", "{}"),
                status=data.get("status", "active"),
                sort_order=data.get("sort_order", 0),
            )
            db.add(row)
    db.commit()
