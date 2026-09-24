#!/usr/bin/env python3
"""
Runner: VoxCPM2
环境: venv /root/workspace/venvs/voxcpm (Python 3.12)
注意：必须用专用 venv，不能混入其他环境
"""
import argparse
import json
import os
import sys
import time

DEFAULT_MODEL_DIR = "/root/workspace/ckpt/voxcpm_pretrained"


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
        import soundfile as sf
        from voxcpm import VoxCPM
    except ImportError as e:
        emit({"event": "error", "msg": f"依赖缺失: {e}"})
        sys.exit(1)

    t_load = time.time()
    model = VoxCPM.from_pretrained(args.model_dir, load_denoiser=False)
    sample_rate = model.tts_model.sample_rate
    emit({"event": "ready", "total": len(items), "elapsed": round(time.time() - t_load, 1)})

    clone_mode = params.get("clone_mode", "hifi_clone")
    _KNOWN_GEN_KWARGS = {"cfg_value", "inference_timesteps", "min_len", "max_len", "normalize"}
    gen_kwargs = {k: v for k, v in params.items() if k in _KNOWN_GEN_KWARGS}

    ok = fail = 0

    for item in items:
        key = item.get("key", "")
        out_path = os.path.join(args.output_dir, f"{key}.wav")

        if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            emit({"event": "item", "key": key, "status": "skip"})
            continue

        # JSONL 字段映射：source_path/source_text → prompt_speech/prompt_text
        vox_item = dict(item)
        if "source_path" in item:
            vox_item["prompt_speech"] = item["source_path"]
        if "source_text" in item:
            vox_item["prompt_text"] = item["source_text"]
        if "clone_mode" not in vox_item:
            vox_item["clone_mode"] = clone_mode

        t0 = time.time()
        try:
            actual_mode = _infer_clone_mode(vox_item)
            wav = _run_inference(model, vox_item, actual_mode, gen_kwargs)
            sf.write(out_path, wav, samplerate=sample_rate)
            ok += 1
            emit({"event": "item", "key": key, "status": "ok", "elapsed": round(time.time() - t0, 2)})
        except Exception as e:
            fail += 1
            emit({"event": "item", "key": key, "status": "fail", "error": str(e), "elapsed": round(time.time() - t0, 2)})

    emit({"event": "done", "total": len(items), "ok": ok, "fail": fail})


def _infer_clone_mode(item: dict) -> str:
    if item.get("clone_mode"):
        return item["clone_mode"]
    has_ref = bool(item.get("prompt_speech", "").strip())
    has_prompt_text = bool(item.get("prompt_text", "").strip())
    if not has_ref:
        return "voice_design"
    if has_prompt_text:
        return "hifi_clone"
    return "controllable_clone"


def _run_inference(model, item: dict, clone_mode: str, gen_kwargs: dict):
    text = item["text"]
    prompt_speech = item.get("prompt_speech", "")
    prompt_text = item.get("prompt_text", "")

    if clone_mode == "voice_design":
        return model.generate(text=text, **gen_kwargs)
    elif clone_mode == "controllable_clone":
        return model.generate(text=text, reference_wav_path=prompt_speech, **gen_kwargs)
    elif clone_mode == "hifi_clone":
        return model.generate(
            text=text,
            prompt_wav_path=prompt_speech,
            prompt_text=prompt_text,
            reference_wav_path=prompt_speech,
            **gen_kwargs,
        )
    else:
        raise ValueError(f"未知 clone_mode: {clone_mode}")


if __name__ == "__main__":
    main()
