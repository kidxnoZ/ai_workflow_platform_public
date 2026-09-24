import json
from pathlib import Path
from sqlalchemy.orm import Session
from pydub import AudioSegment

from backend.models.task import Task
from backend.tools.metadata import get_metadata
from backend.tools.audio import segment_by_silence
from backend.tools.asr import transcribe
from backend.config import DATASETS_DIR


def _is_high_laugh(text: str, threshold: float = 0.7) -> bool:
    laugh_chars = sum(1 for c in text if c in "哈嗯呵嘻")
    return len(text) > 0 and laugh_chars / len(text) >= threshold


def run_dataset_pipeline(task: Task, db: Session) -> dict:
    input_data = json.loads(task.input)
    audio_paths = input_data["audio_paths"]
    out_dir = DATASETS_DIR / task.id
    out_dir.mkdir(parents=True, exist_ok=True)

    # Step 1: Metadata
    task.append_log("Step 1/4: 统计元信息")
    db.commit()
    metadata = get_metadata(audio_paths)

    # Step 2: ASR
    task.append_log(f"Step 2/4: ASR 转录 ({len(audio_paths)} 个文件)")
    db.commit()
    transcriptions: dict[str, str] = {}
    for p in audio_paths:
        try:
            transcriptions[p] = transcribe(p)
            task.append_log(f"  ASR: {Path(p).name}")
            db.commit()
        except Exception as e:
            transcriptions[p] = ""
            task.append_log(f"  ASR ERROR {Path(p).name}: {e}", "error")
            db.commit()

    # Step 3: Segmentation
    task.append_log("Step 3/4: 切句")
    db.commit()
    all_segments = []
    for path in audio_paths:
        src_name = Path(path).stem
        try:
            audio = AudioSegment.from_file(path)
            segs = segment_by_silence(audio)
            for idx, (start_ms, end_ms) in enumerate(segs):
                seg_audio = audio[start_ms:end_ms]
                seg_key = f"{src_name}_seg_{idx:03d}"
                seg_path = out_dir / f"{seg_key}.wav"
                seg_audio.export(str(seg_path), format="wav")
                asr_text = transcriptions.get(path, "")
                all_segments.append({
                    "key": seg_key,
                    "source_file": Path(path).name,
                    "start_sec": round(start_ms / 1000, 3),
                    "end_sec": round(end_ms / 1000, 3),
                    "asr_text": asr_text,
                    "flags": [],
                    "seg_path": str(seg_path),
                })
        except Exception as e:
            task.append_log(f"  SEG ERROR {Path(path).name}: {e}", "error")
            db.commit()

    # Step 4: Cleanup
    task.append_log(f"Step 4/4: 清洗 ({len(all_segments)} 段)")
    db.commit()
    for seg in all_segments:
        text = seg["asr_text"]
        if len(text) < 3:
            seg["flags"].append("too_short")
        if _is_high_laugh(text):
            seg["flags"].append("high_laugh_ratio")

    # Export data.json
    data_json = {
        "metadata": metadata,
        "segments": [{k: v for k, v in s.items() if k != "seg_path"} for s in all_segments],
    }
    data_path = out_dir / "data.json"
    with open(data_path, "w", encoding="utf-8") as f:
        json.dump(data_json, f, ensure_ascii=False, indent=2)

    task.append_log(f"完成：{len(all_segments)} 段，data.json 已生成")
    db.commit()

    return {
        "data_json_url": f"/storage/datasets/{task.id}/data.json",
        "total_segments": len(all_segments),
        "metadata": metadata,
    }
