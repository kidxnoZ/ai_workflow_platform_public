#!/usr/bin/env python3
"""
Runner: Fish-Audio S2
环境: conda fish-speech (Python 3.12, torch cu128)
"""
import argparse
import json
import os
import sys
import time
from collections import defaultdict

FISH_SPEECH_DIR = "/root/workspace/fish-speech"
os.chdir(FISH_SPEECH_DIR)
if FISH_SPEECH_DIR not in sys.path:
    sys.path.insert(0, FISH_SPEECH_DIR)

CODEC_CKPT = "/root/workspace/ckpt/s2-pro/codec.pth"
DEFAULT_T2S_CKPT = "/root/workspace/ckpt/s2-pro"
MAX_SEQ_LEN = 4096


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
    parser.add_argument("--model-dir", default=DEFAULT_T2S_CKPT)
    args = parser.parse_args()

    params = json.loads(args.params)
    os.makedirs(args.output_dir, exist_ok=True)
    items = load_jsonl(args.jsonl)

    try:
        import torch
        import soundfile as sf
        from fish_speech.models.text2semantic.inference import (
            init_model, load_codec_model, encode_audio, decode_to_audio, generate_long,
        )
    except ImportError as e:
        emit({"event": "error", "msg": f"依赖缺失: {e}"})
        sys.exit(1)

    t_load = time.time()
    device = "cuda" if torch.cuda.is_available() else "cpu"
    precision = torch.bfloat16

    codec = load_codec_model(CODEC_CKPT, device, precision)
    t2s_model, decode_one_token = init_model(args.model_dir, device, precision, compile=False)
    t2s_model.config.max_seq_len = MAX_SEQ_LEN
    with torch.device(device):
        t2s_model.setup_caches(
            max_batch_size=1,
            max_seq_len=MAX_SEQ_LEN,
            dtype=next(t2s_model.parameters()).dtype,
        )

    emit({"event": "ready", "total": len(items), "elapsed": round(time.time() - t_load, 1)})

    # 按 (source_path, source_text) 分组，复用 VQ encoding
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for item in items:
        key = (item.get("source_path", ""), item.get("source_text", ""))
        groups[key].append(item)

    top_p = params.get("top_p", 0.9)
    top_k = params.get("top_k", 30)
    temperature = params.get("temperature", 1.0)
    max_new_tokens = params.get("max_new_tokens", 0)

    ok = fail = 0

    for (ref_audio, ref_text), group_items in groups.items():
        try:
            prompt_tokens = encode_audio(ref_audio, codec, device).cpu()
        except Exception as e:
            for item in group_items:
                fail += 1
                emit({"event": "item", "key": item.get("key", ""), "status": "fail", "error": f"ref encode: {e}"})
            continue

        for item in group_items:
            key = item.get("key", "")
            text = item.get("text", "")
            out_path = os.path.join(args.output_dir, f"{key}.wav")

            if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                emit({"event": "item", "key": key, "status": "skip"})
                continue

            t0 = time.time()
            try:
                codes_list = []
                for resp in generate_long(
                    model=t2s_model,
                    device=device,
                    decode_one_token=decode_one_token,
                    text=text,
                    num_samples=1,
                    max_new_tokens=max_new_tokens,
                    top_p=top_p,
                    top_k=top_k,
                    temperature=temperature,
                    compile=False,
                    iterative_prompt=True,
                    chunk_length=300,
                    prompt_text=[ref_text],
                    prompt_tokens=[prompt_tokens],
                ):
                    if resp.action == "sample":
                        codes_list.append(resp.codes)

                if not codes_list:
                    raise RuntimeError("no codes generated")

                codes = torch.cat(codes_list, dim=1).cpu()
                audio = decode_to_audio(codes.to(device), codec)
                sf.write(out_path, audio.cpu().float().numpy(), codec.sample_rate)
                ok += 1
                emit({"event": "item", "key": key, "status": "ok", "elapsed": round(time.time() - t0, 2)})
            except Exception as e:
                fail += 1
                emit({"event": "item", "key": key, "status": "fail", "error": str(e), "elapsed": round(time.time() - t0, 2)})

    emit({"event": "done", "total": len(items), "ok": ok, "fail": fail})


if __name__ == "__main__":
    main()
