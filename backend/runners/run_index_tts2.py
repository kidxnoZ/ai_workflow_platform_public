#!/usr/bin/env python3
"""
Runner: Index-TTS2
环境: /root/workspace/index-tts/.venv/bin/python (Python 3.10)
注意：直接用 .venv/bin/python 调用，勿用 uv run（会触发 sync 下载）
"""
import argparse
import json
import os
import sys
import time
from collections import defaultdict

sys.path.insert(0, os.path.expanduser("~/workspace/index-tts"))

DEFAULT_MODEL_DIR = "/root/workspace/ckpt/IndexTTS-2"


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
    parser.add_argument("--model-dir", default=DEFAULT_MODEL_DIR)
    args = parser.parse_args()

    params = json.loads(args.params)
    os.makedirs(args.output_dir, exist_ok=True)
    items = load_jsonl(args.jsonl)

    try:
        from indextts.infer_v2 import IndexTTS2
    except ImportError as e:
        emit({"event": "error", "msg": f"依赖缺失: {e}"})
        sys.exit(1)

    t_load = time.time()
    tts = IndexTTS2(
        cfg_path=os.path.join(args.model_dir, "config.yaml"),
        model_dir=args.model_dir,
        use_fp16=False,
        use_cuda_kernel=False,
        use_deepspeed=False,
    )
    emit({"event": "ready", "total": len(items), "elapsed": round(time.time() - t_load, 1)})

    # 按 source_path 分组，复用 speaker cache
    groups: dict[str, list[dict]] = defaultdict(list)
    for item in items:
        groups[item.get("source_path", "")].append(item)

    ok = fail = 0

    for ref_audio, group_items in groups.items():
        for item in group_items:
            key = item.get("key", "")
            out_path = os.path.join(args.output_dir, f"{key}.wav")

            if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                emit({"event": "item", "key": key, "status": "skip"})
                continue

            t0 = time.time()
            try:
                tts.infer(
                    spk_audio_prompt=ref_audio,
                    text=item.get("text", ""),
                    output_path=out_path,
                    verbose=False,
                )
                ok += 1
                emit({"event": "item", "key": key, "status": "ok", "elapsed": round(time.time() - t0, 2)})
            except Exception as e:
                fail += 1
                emit({"event": "item", "key": key, "status": "fail", "error": str(e), "elapsed": round(time.time() - t0, 2)})

    emit({"event": "done", "total": len(items), "ok": ok, "fail": fail})


if __name__ == "__main__":
    main()
