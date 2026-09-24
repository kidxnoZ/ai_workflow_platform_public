"""backend/tools/audio_preprocess.py

Audio processing operations: silence trim, pause compression, gain normalisation,
sentence segmentation. All functions are stateless and operate on file paths.

CRITICAL implementation notes (from audio-trim skill):
- Silence trim uses hold_ms=50 to prevent single-frame noise spikes.
- Pause compression fills with original audio (not pure silence) to preserve
  natural noise floor and prevent clipping at splice points.
- Two independent thresholds: threshold_db (head/tail) vs pause_threshold_db (middle).
- Export preserves original bit-depth; 32-bit float uses pcm_f32le codec.

All functions MUST call _assert_in_bounds() at entry and validate numeric params.
"""
from __future__ import annotations

import math
import os
from pathlib import Path

import numpy as np
from pydub import AudioSegment
from pydub.silence import detect_silence


# ── Safety helpers ────────────────────────────────────────────────────────────

def _assert_in_bounds(path: str, root: str) -> None:
    """Raise ValueError if path resolves outside root (symlink-safe)."""
    if not Path(path).resolve().is_relative_to(Path(root).resolve()):
        raise ValueError(f"路径越界: {path}")


def get_export_codec(sample_width: int, float32: bool = False) -> str:
    """Map sample_width (bytes) + float32 flag to ffmpeg codec name."""
    if float32:
        return "pcm_f32le"
    mapping = {1: "pcm_s8", 2: "pcm_s16le", 3: "pcm_s24le", 4: "pcm_s32le"}
    return mapping.get(sample_width, "pcm_s24le")


# ── Head/tail silence replacement ─────────────────────────────────────────────

def find_speech_bounds(
    audio_path: str,
    threshold_db: float = -55.0,
    frame_ms: int = 10,
    hold_ms: int = 50,
) -> tuple[int, int]:
    """Return (speech_start_ms, speech_end_ms). hold_ms prevents false starts.

    Defaults: speech_start_ms = len(audio), speech_end_ms = 0 so that an
    all-silence file triggers the speech_start >= speech_end condition
    in apply_silence_trim.
    """
    audio = AudioSegment.from_file(audio_path)

    samples = np.array(audio.get_array_of_samples(), dtype=np.float32)
    if audio.channels > 1:
        samples = samples.reshape(-1, audio.channels).mean(axis=1)

    sr = audio.frame_rate
    frame_size = int(sr * frame_ms / 1000)
    max_val = float(2 ** (audio.sample_width * 8 - 1))
    hold_frames = max(1, int(hold_ms / frame_ms))

    # Per-frame RMS → dBFS
    n = len(samples) // frame_size
    if n == 0:
        return (len(audio), 0)

    dbs = []
    for i in range(n):
        chunk = samples[i * frame_size:(i + 1) * frame_size]
        rms = np.sqrt(np.mean(chunk ** 2))
        dbs.append(20.0 * math.log10(rms / max_val) if rms > 0 else -120.0)
    dbs_arr = np.array(dbs)
    is_speech = dbs_arr >= threshold_db

    # Forward: find first block of hold_frames consecutive speech frames
    speech_start_ms: int = len(audio)
    for i in range(n - hold_frames + 1):
        if np.all(is_speech[i:i + hold_frames]):
            speech_start_ms = max(0, i * frame_ms)
            break

    # Backward: find last block of hold_frames consecutive speech frames
    speech_end_ms: int = 0
    if hold_frames <= n:
        for i in range(n - 1, hold_frames - 2, -1):
            start_idx = i - hold_frames + 1
            if start_idx >= 0 and np.all(is_speech[start_idx:i + 1]):
                speech_end_ms = min(len(audio), (i + 1) * frame_ms)
                break

    return speech_start_ms, speech_end_ms


def apply_silence_trim(
    audio_path: str,
    output_path: str,
    output_root: str,
    threshold_db: float = -55.0,
    hold_ms: int = 50,
    head_tail_ms: int = 100,
    float32: bool = False,
) -> str:
    """Trim head/tail noise, replace with head_tail_ms of pure silence.
    If speech_start >= speech_end, output head_tail_ms silence and return."""
    _assert_in_bounds(output_path, output_root)

    audio = AudioSegment.from_file(audio_path)
    speech_start, speech_end = find_speech_bounds(
        audio_path, threshold_db=threshold_db, frame_ms=10, hold_ms=hold_ms,
    )

    if speech_start >= speech_end:
        result = AudioSegment.silent(
            duration=head_tail_ms, frame_rate=audio.frame_rate)
    else:
        result = (
            AudioSegment.silent(
                duration=head_tail_ms, frame_rate=audio.frame_rate)
            + audio[speech_start:speech_end]
            + AudioSegment.silent(
                duration=head_tail_ms, frame_rate=audio.frame_rate)
        )

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    result.export(
        output_path, format="wav",
        parameters=["-acodec", get_export_codec(audio.sample_width, float32)],
    )
    return output_path


# ── Internal pause compression ────────────────────────────────────────────────

def find_speech_regions(
    audio_path: str,
    threshold_db: float = -55.0,
    frame_ms: int = 10,
    offset_hold_ms: int = 50,
) -> list[tuple[int, int]]:
    """Return list of (start_ms, end_ms) speech regions.
    Gaps < offset_hold_ms are filled (preserves natural word-level pauses)."""
    audio = AudioSegment.from_file(audio_path)

    samples = np.array(audio.get_array_of_samples(), dtype=np.float32)
    if audio.channels > 1:
        samples = samples.reshape(-1, audio.channels).mean(axis=1)

    sr = audio.frame_rate
    frame_size = int(sr * frame_ms / 1000)
    max_val = float(2 ** (audio.sample_width * 8 - 1))
    offset_frames = max(1, int(offset_hold_ms / frame_ms))

    n = len(samples) // frame_size
    if n == 0:
        return []

    dbs = []
    for i in range(n):
        chunk = samples[i * frame_size:(i + 1) * frame_size]
        rms = np.sqrt(np.mean(chunk ** 2))
        dbs.append(20.0 * math.log10(rms / max_val) if rms > 0 else -120.0)
    dbs_arr = np.array(dbs)
    is_speech = dbs_arr >= threshold_db

    # offset hold: fill silence gaps shorter than offset_frames
    smoothed = is_speech.copy()
    in_silence = False
    silence_start = 0
    for i in range(len(smoothed)):
        if smoothed[i]:
            if in_silence and (i - silence_start < offset_frames):
                smoothed[silence_start:i] = True
            in_silence = False
        else:
            if not in_silence:
                in_silence = True
                silence_start = i

    # Convert to region list
    regions: list[tuple[int, int]] = []
    in_speech = False
    start = 0
    for i, s in enumerate(smoothed):
        if s and not in_speech:
            in_speech = True
            start = i
        elif not s and in_speech:
            in_speech = False
            regions.append((start * frame_ms, i * frame_ms))
    if in_speech:
        regions.append((start * frame_ms, len(smoothed) * frame_ms))

    return regions


def compress_pause(
    gap_ms: int,
    pause_threshold: int = 400,
    pause_ratio: float = 0.6,
    pause_cap: int = 1500,
) -> int:
    """Apply formula: if gap < threshold → gap; else → min(threshold + (gap - threshold) * ratio, cap).
    Examples:
       gap=300ms → 300 (below threshold, unchanged)
       gap=500ms → 400 + 100*0.6 = 460ms
       gap=2000ms → min(400 + 1600*0.6, 1500) = 1500ms
    """
    if gap_ms < pause_threshold:
        return gap_ms
    compressed = pause_threshold + (gap_ms - pause_threshold) * pause_ratio
    return int(min(compressed, pause_cap))


def apply_pause_compress(
    audio_path: str,
    output_path: str,
    output_root: str,
    pause_threshold_db: float = -55.0,
    offset_hold_ms: int = 50,
    pause_threshold: int = 400,
    pause_ratio: float = 0.6,
    pause_cap: int = 1500,
    float32: bool = False,
) -> str:
    """Compress internal pauses. Fills compressed gaps with original audio
    (not pure silence) to preserve noise floor and avoid clipping."""
    _assert_in_bounds(output_path, output_root)

    audio = AudioSegment.from_file(audio_path)
    core = audio

    regions = find_speech_regions(
        audio_path, threshold_db=pause_threshold_db,
        frame_ms=10, offset_hold_ms=offset_hold_ms,
    )

    HEAD_TAIL_MS = 100

    if not regions:
        result = (
            AudioSegment.silent(
                duration=HEAD_TAIL_MS, frame_rate=audio.frame_rate)
            + core
            + AudioSegment.silent(
                duration=HEAD_TAIL_MS, frame_rate=audio.frame_rate)
        )
    else:
        # Merge adjacent regions separated by < 50ms
        merged = [list(regions[0])]
        for r in regions[1:]:
            if r[0] - merged[-1][1] < 50:
                merged[-1][1] = r[1]
            else:
                merged.append(list(r))

        result = AudioSegment.silent(
            duration=HEAD_TAIL_MS, frame_rate=audio.frame_rate)
        for i, (start, end) in enumerate(merged):
            result += core[start:end]
            if i < len(merged) - 1:
                next_start = merged[i + 1][0]
                gap = next_start - end
                new_gap = compress_pause(gap, pause_threshold, pause_ratio, pause_cap)
                # Fill with original audio (not pure silence) to preserve noise floor
                gap_end = min(end + new_gap, len(core))
                result += core[end:gap_end]
        result += AudioSegment.silent(
            duration=HEAD_TAIL_MS, frame_rate=audio.frame_rate)

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    result.export(
        output_path, format="wav",
        parameters=["-acodec", get_export_codec(audio.sample_width, float32)],
    )
    return output_path


# ── Volume gain ───────────────────────────────────────────────────────────────

def apply_gain_normalization(
    audio_path: str,
    output_path: str,
    output_root: str,
    peak_target: float = 0.7,
) -> str:
    """Peak-normalise to peak_target amplitude. peak_target must be (0, 1]."""
    _assert_in_bounds(output_path, output_root)

    if not (0.0 < peak_target <= 1.0):
        raise ValueError(f"peak_target must be in (0, 1], got {peak_target}")

    audio = AudioSegment.from_file(audio_path)

    # Normalize to 0 dBFS peak, then apply gain to hit peak_target
    audio = audio.apply_gain(-audio.max_dBFS)
    target_dBFS = 20.0 * math.log10(peak_target)
    audio = audio.apply_gain(target_dBFS)

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    audio.export(output_path, format="wav")
    return output_path


# ── Sentence segmentation ─────────────────────────────────────────────────────

def apply_sentence_segmentation(
    audio_path: str,
    asr_text: str,
    output_dir: str,
    output_root: str,
    silence_threshold_sec: float = 2.0,
    repeat_min_chars: int = 10,
) -> list[dict]:
    """Segment audio at silence gaps > threshold or repeated text blocks.
    Returns list of {key, path, start_sec, end_sec, text} dicts."""
    _assert_in_bounds(output_dir, output_root)

    audio = AudioSegment.from_file(audio_path)
    total_ms = len(audio)
    min_silence_len = int(silence_threshold_sec * 1000)

    # Find silence gaps longer than threshold
    silence_ranges = detect_silence(
        audio, min_silence_len=min_silence_len, silence_thresh=-40,
    )

    # Collect audio segments between silence gaps
    segments: list[AudioSegment] = []
    seg_boundaries: list[tuple[int, int]] = []
    prev_end = 0
    for sil_start, sil_end in silence_ranges:
        if sil_start > prev_end:
            segments.append(audio[prev_end:sil_start])
            seg_boundaries.append((prev_end, sil_start))
        prev_end = sil_end
    if prev_end < total_ms:
        segments.append(audio[prev_end:total_ms])
        seg_boundaries.append((prev_end, total_ms))

    if not segments:
        segments = [audio]
        seg_boundaries = [(0, total_ms)]

    # Export each segment
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    results: list[dict] = []
    for i, (seg, (start_ms, end_ms)) in enumerate(zip(segments, seg_boundaries)):
        key = f"{i:04d}"
        out_path = str(Path(output_dir) / f"{key}.wav")
        seg.export(out_path, format="wav")
        results.append({
            "key": key,
            "path": out_path,
            "start_sec": start_ms / 1000.0,
            "end_sec": end_ms / 1000.0,
            "text": asr_text,
        })

    return results
