#!/usr/bin/env python3
"""
Runner: CosyVoice3
环境: conda cosyvoice (Python 3.10)
"""
import argparse
import json
import os
import sys
import time

sys.path.append("/root/workspace/CosyVoice/third_party/Matcha-TTS")
sys.path.insert(0, "/root/workspace/CosyVoice")

DEFAULT_MODEL_DIR = "/root/workspace/ckpt/Fun-CosyVoice3-0.5B-2512"


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
        import torchaudio
        from cosyvoice.cli.cosyvoice import AutoModel
    except ImportError as e:
        emit({"event": "error", "msg": f"依赖缺失: {e}"})
        sys.exit(1)

    t_load = time.time()
    cosyvoice = AutoModel(model_dir=args.model_dir)
    emit({"event": "ready", "total": len(items), "elapsed": round(time.time() - t_load, 1)})

    ok = fail = 0

    for item in items:
        key = item.get("key", "")
        text = item.get("text", "")
        out_path = os.path.join(args.output_dir, f"{key}.wav")

        if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            emit({"event": "item", "key": key, "status": "skip"})
            continue

        t0 = time.time()
        try:
            # Fun-CosyVoice3 需要 source_text 带 prompt 前缀，否则 token 化后长度不足报 padding 错误
            prompt_text = f"You are a helpful assistant.<|endofprompt|>{item.get('source_text', '')}"
            result = list(cosyvoice.inference_zero_shot(
                text,
                prompt_text,
                item.get("source_path", ""),
            ))
            torchaudio.save(out_path, result[0]["tts_speech"], cosyvoice.sample_rate)
            ok += 1
            emit({"event": "item", "key": key, "status": "ok", "elapsed": round(time.time() - t0, 2)})
        except Exception as e:
            fail += 1
            emit({"event": "item", "key": key, "status": "fail", "error": str(e), "elapsed": round(time.time() - t0, 2)})

    emit({"event": "done", "total": len(items), "ok": ok, "fail": fail})


if __name__ == "__main__":
    main()
