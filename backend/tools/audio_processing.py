"""音频处理工具：时长检测、静音检测、裁剪、拼接、按静音切分。

用于克隆实验室 Phase 6 合成阶段的 prompt 音频预处理。
"""
import os
import uuid
from pathlib import Path

import numpy as np
from pydub import AudioSegment

TEMP_DIR = Path("/tmp/audio_processing")
TEMP_DIR.mkdir(parents=True, exist_ok=True)


def _get_export_codec(sample_width: int) -> str:
    mapping = {1: "pcm_s8", 2: "pcm_s16le", 3: "pcm_s24le", 4: "pcm_s32le"}
    return mapping.get(sample_width, "pcm_s16le")


def _frame_dbs(audio: AudioSegment, frame_ms: int = 10) -> np.ndarray:
    samples = np.array(audio.get_array_of_samples(), dtype=np.float32)
    if audio.channels > 1:
        samples = samples.reshape(-1, audio.channels).mean(axis=1)

    sr = audio.frame_rate
    frame_size = int(sr * frame_ms / 1000)
    max_val = 2 ** (audio.sample_width * 8 - 1)

    n = len(samples) // frame_size
    dbs = np.empty(n)
    for i in range(n):
        chunk = samples[i * frame_size:(i + 1) * frame_size]
        rms = np.sqrt(np.mean(chunk ** 2))
        dbs[i] = 20 * np.log10(rms / max_val) if rms > 0 else -120.0
    return dbs


def detect_total_duration(path: str) -> float:
    """返回音频总时长（秒）"""
    audio = AudioSegment.from_file(path)
    return len(audio) / 1000.0


def detect_silence_segments(path: str, threshold_db: float = -40.0, min_silence_ms: int = 500) -> list[dict]:
    """
    检测静音段。
    返回: [{"start_ms": 3200, "end_ms": 4800, "duration_ms": 1600}]
    """
    audio = AudioSegment.from_file(path)
    frame_ms = 10
    dbs = _frame_dbs(audio, frame_ms)
    is_silent = dbs < threshold_db

    min_frames = min_silence_ms // frame_ms
    segments = []
    in_silence = False
    start_frame = 0

    for i, silent in enumerate(is_silent):
        if silent:
            if not in_silence:
                in_silence = True
                start_frame = i
        else:
            if in_silence:
                duration_frames = i - start_frame
                if duration_frames >= min_frames:
                    start = start_frame * frame_ms
                    end = i * frame_ms
                    segments.append({
                        "start_ms": start,
                        "end_ms": end,
                        "duration_ms": end - start,
                    })
                in_silence = False

    if in_silence:
        duration_frames = len(is_silent) - start_frame
        if duration_frames >= min_frames:
            start = start_frame * frame_ms
            end = len(is_silent) * frame_ms
            segments.append({
                "start_ms": start,
                "end_ms": end,
                "duration_ms": end - start,
            })

    return segments


def trim_silence(path: str, padding_ms: int = 100, threshold_db: float = -55.0) -> str:
    """裁剪首尾静音，保留 padding_ms 的静音 padding，返回处理后路径。

    使用 find_speech_bounds 算法（与 reference_code/audio-trim 一致）：
    连续多帧超过阈值才算语音起止点，避免瞬态噪声干扰。
    """
    audio = AudioSegment.from_file(path)
    frame_ms = 10
    hold_ms = 50

    dbs = _frame_dbs(audio, frame_ms)
    is_speech = dbs >= threshold_db
    n = len(dbs)
    hold_frames = max(1, int(hold_ms / frame_ms))

    speech_start_ms = 0
    for i in range(n - hold_frames + 1):
        if np.all(is_speech[i:i + hold_frames]):
            speech_start_ms = max(0, i * frame_ms)
            break

    speech_end_ms = len(audio)
    for i in range(n - 1, hold_frames - 2, -1):
        if np.all(is_speech[i - hold_frames + 1:i + 1]):
            speech_end_ms = min(len(audio), (i + 1) * frame_ms)
            break

    if speech_start_ms >= speech_end_ms:
        out_path = str(TEMP_DIR / f"{uuid.uuid4().hex}.wav")
        AudioSegment.silent(duration=padding_ms * 2).export(out_path, format="wav")
        return out_path

    core = audio[speech_start_ms:speech_end_ms]
    result = AudioSegment.silent(duration=padding_ms) + core + AudioSegment.silent(duration=padding_ms)

    out_path = str(TEMP_DIR / f"{uuid.uuid4().hex}.wav")
    result.export(out_path, format="wav",
                  parameters=["-acodec", _get_export_codec(audio.sample_width)])
    return out_path


def concat_audio(paths: list[str], gap_ms: int = 100) -> str:
    """拼接多条音频，中间插入 gap_ms 静音，返回拼接后路径。

    每条音频先 trim 首尾静音再拼接。
    """
    if not paths:
        raise ValueError("paths cannot be empty")
    if len(paths) == 1:
        return trim_silence(paths[0])

    segments = []
    sample_width = None
    for p in paths:
        trimmed_path = trim_silence(p)
        seg = AudioSegment.from_file(trimmed_path)
        segments.append(seg)
        if sample_width is None:
            sample_width = seg.sample_width

    result = segments[0]
    for seg in segments[1:]:
        if gap_ms > 0:
            result += AudioSegment.silent(duration=gap_ms, frame_rate=result.frame_rate)
        result += seg

    out_path = str(TEMP_DIR / f"{uuid.uuid4().hex}.wav")
    result.export(out_path, format="wav",
                  parameters=["-acodec", _get_export_codec(sample_width or 2)])
    return out_path


def split_by_silence(path: str, min_silence_ms: int = 3000, threshold_db: float = -40.0) -> list[str]:
    """按长静音切分音频为多段，每段 trim 首尾静音后输出。返回各段路径列表。"""
    audio = AudioSegment.from_file(path)
    silence_segs = detect_silence_segments(path, threshold_db=threshold_db, min_silence_ms=min_silence_ms)

    if not silence_segs:
        trimmed = trim_silence(path)
        return [trimmed]

    split_points = [0]
    for seg in silence_segs:
        mid = (seg["start_ms"] + seg["end_ms"]) // 2
        split_points.append(mid)
    split_points.append(len(audio))

    out_paths = []
    for i in range(len(split_points) - 1):
        start = split_points[i]
        end = split_points[i + 1]
        if end - start < 200:
            continue
        chunk = audio[start:end]
        chunk_path = str(TEMP_DIR / f"{uuid.uuid4().hex}_chunk.wav")
        chunk.export(chunk_path, format="wav",
                     parameters=["-acodec", _get_export_codec(audio.sample_width)])
        trimmed = trim_silence(chunk_path)
        out_paths.append(trimmed)
        os.unlink(chunk_path)

    return out_paths if out_paths else [trim_silence(path)]
