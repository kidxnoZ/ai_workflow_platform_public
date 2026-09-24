#!/usr/bin/env python3
"""
Runner: Qwen3-TTS
环境: conda tts_clone (Python 3.10, torch cu124)
"""
import argparse
import json
import os
import sys
import time
import types
from collections import defaultdict

# mock gradio（qwen_tts 内部 import gradio，不需要 UI）
_mock = types.ModuleType("gradio")
_mock.Progress = type("Progress", (), {"__call__": lambda s, v=0, d="": s})()
sys.modules["gradio"] = _mock

# qwen_tts 包在 /root/workspace/qwen_tts/
sys.path.insert(0, "/root/workspace")

DEFAULT_MODEL_PATH = "/root/workspace/ckpt/Qwen3-TTS-12Hz-1.7B-Base"

COMMON_GEN_KWARGS = dict(
    max_new_tokens=2048,
    do_sample=True,
    top_k=50,
    top_p=1.0,
    temperature=0.9,
    repetition_penalty=1.05,
    subtalker_dosample=True,
    subtalker_top_k=50,
    subtalker_top_p=1.0,
    subtalker_temperature=0.9,
)


def emit(obj: dict):
    print(json.dumps(obj, ensure_ascii=False), flush=True)


def load_jsonl(path: str) -> list[dict]:
    items = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                items.append(json.loads(line))
    return items


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--jsonl", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--task-id", default="")
    parser.add_argument("--params", default="{}")
    parser.add_argument("--model-dir", default=DEFAULT_MODEL_PATH)
    args = parser.parse_args()

    params = json.loads(args.params)
    os.makedirs(args.output_dir, exist_ok=True)
    items = load_jsonl(args.jsonl)

    try:
        import torch
        import soundfile as sf
        from qwen_tts import Qwen3TTSModel
    except ImportError as e:
        emit({"event": "error", "msg": f"依赖缺失: {e}"})
        sys.exit(1)

    t_load = time.time()
    model = Qwen3TTSModel.from_pretrained(
        args.model_dir,
        device_map="cuda:0",
        dtype=torch.bfloat16,
        attn_implementation="sdpa",
    )

    emit({"event": "ready", "total": len(items), "elapsed": round(time.time() - t_load, 1)})

    # 更新 gen kwargs（允许外部覆盖 temperature/top_k）
    gen_kwargs = dict(COMMON_GEN_KWARGS)
    for k in ("temperature", "top_k", "top_p", "max_new_tokens"):
        if k in params:
            gen_kwargs[k] = params[k]

    # 按 (source_path, source_text) 分组，每组一次批量推理
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for item in items:
        key = (item.get("source_path", ""), item.get("source_text", ""))
        groups[key].append(item)

    ok = fail = 0

    for (ref_audio, ref_text), group_items in groups.items():
        # 检查所有条目是否都已存在（断点续跑）
        pending = []
        for item in group_items:
            key = item.get("key", "")
            out_path = os.path.join(args.output_dir, f"{key}.wav")
            if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                emit({"event": "item", "key": key, "status": "skip"})
            else:
                pending.append(item)

        if not pending:
            continue

        t0 = time.time()
        try:
            prompt_items = model.create_voice_clone_prompt(
                ref_audio=ref_audio,
                ref_text=ref_text,
                x_vector_only_mode=False,
            )
            texts = [it["text"] for it in pending]
            wavs, sr = model.generate_voice_clone(
                text=texts,
                language=["Chinese"] * len(texts),
                voice_clone_prompt=prompt_items,
                **gen_kwargs,
            )
            batch_elapsed = round(time.time() - t0, 2)
            for i, (wav, item) in enumerate(zip(wavs, pending)):
                key = item.get("key", "")
                out_path = os.path.join(args.output_dir, f"{key}.wav")
                sf.write(out_path, wav, sr)
                ok += 1
                # 批量推理：第一条显示总耗时，后续标注同批
                label = f"{batch_elapsed}s" if i == 0 else f"{batch_elapsed}s (批×{len(pending)})"
                emit({"event": "item", "key": key, "status": "ok", "elapsed": label})
        except Exception as e:
            for item in pending:
                key = item.get("key", "")
                fail += 1
                emit({"event": "item", "key": key, "status": "fail", "error": str(e)})

    emit({"event": "done", "total": len(items), "ok": ok, "fail": fail})


if __name__ == "__main__":
    main()
