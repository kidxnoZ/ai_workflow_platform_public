"""backend/tools/audio_analysis.py

Per-speaker audio parameter sampling and grouping.
Reads a small sample of files per speaker to determine processing parameters,
then clusters speakers with similar parameters into groups for batch processing.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from pydub import AudioSegment


@dataclass
class SpeakerParams:
    speaker_id: str
    speaker_dir: str
    threshold_db: float       # head/tail silence detection threshold
    pause_threshold_db: float  # internal pause detection threshold (may differ)
    sample_width: int         # bytes per sample: 2=16bit, 3=24bit, 4=32bit
    float32: bool             # True if 32-bit float PCM
    sample_rate: int
    peak_db: float
    mean_db: float
    std_db: float


@dataclass
class ParamGroup:
    group_id: str
    speaker_ids: list[str]
    representative_params: SpeakerParams
    # paths of all audio files belonging to this group
    audio_paths: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _compute_frame_dbs(audio: AudioSegment, frame_ms: int = 10) -> np.ndarray:
    """Compute per-frame dBFS values for an AudioSegment.

    Adapted from reference_code/audio-trim/audio_trim.py handling of
    frame-level RMS-to-dBFS conversion.
    """
    samples = np.array(audio.get_array_of_samples(), dtype=np.float64)
    if audio.channels > 1:
        samples = samples.reshape(-1, audio.channels).mean(axis=1)

    sr = audio.frame_rate
    frame_size = int(sr * frame_ms / 1000)
    max_val = 2 ** (audio.sample_width * 8 - 1)

    n = len(samples) // frame_size
    if n == 0:
        return np.array([-120.0])

    dbs = np.empty(n, dtype=np.float64)
    for i in range(n):
        chunk = samples[i * frame_size:(i + 1) * frame_size]
        rms = np.sqrt(np.mean(chunk ** 2))
        dbs[i] = 20.0 * np.log10(rms / max_val) if rms > 0 else -120.0

    return dbs


def _list_audio_files(directory: Path) -> list[Path]:
    """Return sorted list of audio files (.wav/.mp3/.flac) in *directory*."""
    extensions = {".wav", ".mp3", ".flac"}
    return sorted(
        p for p in directory.iterdir()
        if p.is_file() and p.suffix.lower() in extensions
    )


def _sample_evenly(items: list, count: int) -> list:
    """Sample up to *count* items, evenly spaced across the list."""
    n = len(items)
    if n == 0 or count <= 0:
        return []
    if count >= n:
        return list(items)
    # evenly-spaced indices: first item, last item, and intermediate steps
    indices = [int(i * (n - 1) / max(1, count - 1)) for i in range(count)]
    return [items[idx] for idx in indices]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def measure_loudness(audio_path: str) -> dict[str, float]:
    """Return peak_db, mean_db, std_db for a single file.

    Computes frame-level dBFS distribution (10 ms frames) and derives
    summary statistics.

    Edge case: if the audio is all silence (peak near -120 dBFS) the
    return value is ``{"peak_db": -120.0, "mean_db": -120.0, "std_db": 0.0}``.
    """
    audio = AudioSegment.from_file(audio_path)
    dbs = _compute_frame_dbs(audio)

    peak = float(np.max(dbs))
    mean = float(np.mean(dbs))
    std = float(np.std(dbs))

    # All-silence guard: all frames report the floor value.
    if peak <= -119.0:
        return {"peak_db": -120.0, "mean_db": -120.0, "std_db": 0.0}

    return {"peak_db": peak, "mean_db": mean, "std_db": std}


def detect_silence_threshold(audio_path: str) -> float:
    """Estimate an appropriate silence-detection threshold (dBFS) from the
    noise-floor of *audio_path*.

    Heuristic: ``mean_db - 2 * std_db``, clamped to **[-80.0, -20.0]**.
    """
    stats = measure_loudness(audio_path)
    noise_floor = stats["mean_db"] - 2.0 * stats["std_db"]
    return float(max(-80.0, min(-20.0, noise_floor)))


def analyze_speaker_params(
    speaker_dir: str,
    sample_count: int = 2,
) -> SpeakerParams:
    """Sample up to *sample_count* audio files from ``{speaker_dir}/audio/``,
    measure loudness distribution, detect bit-depth and float32 flag.

    Raises
    ------
    ValueError
        If ``{speaker_dir}/audio/`` does not exist or contains no audio files.
    """
    speaker_path = Path(speaker_dir)
    audio_dir = speaker_path / "audio"

    if not audio_dir.is_dir():
        raise ValueError(f"No audio files in {speaker_dir}/audio/")

    audio_files = _list_audio_files(audio_dir)
    if not audio_files:
        raise ValueError(f"No audio files in {speaker_dir}/audio/")

    sampled = _sample_evenly(audio_files, sample_count)

    # Format info from the first sampled file
    first = AudioSegment.from_file(str(sampled[0]))
    sample_width = first.sample_width
    sample_rate = first.frame_rate

    # pcm_s32le and pcm_f32le both have sample_width=4 -- cannot
    # distinguish without deeper inspection; safe default is False.
    float32 = False

    # Aggregate loudness statistics across sampled files
    peaks: list[float] = []
    means: list[float] = []
    stds: list[float] = []
    thresholds: list[float] = []

    for f in sampled:
        stats = measure_loudness(str(f))
        peaks.append(stats["peak_db"])
        means.append(stats["mean_db"])
        stds.append(stats["std_db"])
        thresholds.append(detect_silence_threshold(str(f)))

    avg_peak = float(np.mean(peaks))
    avg_mean = float(np.mean(means))
    avg_std = float(np.mean(stds))
    avg_threshold = float(np.mean(thresholds))

    return SpeakerParams(
        speaker_id=speaker_path.name,
        speaker_dir=str(speaker_dir),
        threshold_db=avg_threshold,
        pause_threshold_db=avg_threshold,  # same by default
        sample_width=sample_width,
        float32=float32,
        sample_rate=sample_rate,
        peak_db=avg_peak,
        mean_db=avg_mean,
        std_db=avg_std,
    )


def compute_threshold_preview(
    audio_path: str,
    estimated_threshold_db: float = -55.0,  # kept for signature compat, not used internally
) -> dict:
    """Two-window silence-threshold visualisation.

    Fixed detection threshold: -55 dBFS.
      - head_idx: first frame > -55 dBFS (speech start)
      - tail_idx: last  frame > -55 dBFS (speech end)

    Returns two 500ms windows concatenated (~1000ms total):
      Window 1 (head): [head_idx - 250ms … head_idx + 250ms]
      Window 2 (tail): [tail_idx - 250ms … tail_idx + 250ms]

    Canvas shows: [silence → speech onset] gap [speech offset → silence]
    Two white vertical markers at head_transition_pos and tail_transition_pos.

    auto_threshold_db = p80 of (head silence ∪ tail silence) + 2 dB, capped at -45.
    """
    SPEECH_DB = -55.0          # fixed detection threshold
    HALF_WIN_MS = 250          # ±250ms around each transition point

    audio = AudioSegment.from_file(audio_path)
    samples = np.array(audio.get_array_of_samples(), dtype=np.float64)
    if audio.channels > 1:
        samples = samples.reshape(-1, audio.channels).mean(axis=1)

    sr = audio.frame_rate
    max_val = float(2 ** (audio.sample_width * 8 - 1))

    # ── coarse 10ms envelope (finer than before for ±250ms precision) ─────────
    coarse_ms = 10
    cw = max(1, int(sr * coarse_ms / 1000))
    n_c = len(samples) // cw
    coarse_dbs: list[float] = []
    for i in range(n_c):
        chunk = samples[i * cw:(i + 1) * cw]
        peak = float(np.max(np.abs(chunk))) if len(chunk) else 0.0
        coarse_dbs.append(20.0 * np.log10(peak / max_val) if peak > 0 else -120.0)

    # ── head_idx: first frame > SPEECH_DB ─────────────────────────────────────
    head_idx = next((i for i, d in enumerate(coarse_dbs) if d > SPEECH_DB), None)

    # ── tail_idx: last frame > SPEECH_DB ──────────────────────────────────────
    tail_idx = None
    for i in range(n_c - 1, -1, -1):
        if coarse_dbs[i] > SPEECH_DB:
            tail_idx = i
            break

    # ── build two windows ─────────────────────────────────────────────────────
    half_c = HALF_WIN_MS // coarse_ms   # = 25 coarse frames

    def _extract(center: int) -> tuple[np.ndarray, int, int]:
        """Return (segment_samples, start_sample, center_offset_in_samples)."""
        s_c = max(0, center - half_c)
        e_c = min(n_c, center + half_c)
        s_s = s_c * cw
        e_s = min(len(samples), e_c * cw)
        seg = samples[s_s:e_s]
        center_off = (center - s_c) * cw   # sample offset of transition in seg
        return seg, s_s, center_off

    if head_idx is None and tail_idx is None:
        coarse_env = [round(float(max(-96.0, d)), 1) for d in coarse_dbs]
        half = len(coarse_env) // 2
        return {
            "head_envelope":        coarse_env[:half],
            "head_transition_pos":  half // 2,
            "tail_envelope":        coarse_env[half:],
            "tail_transition_pos":  half // 2,
            "auto_threshold_db":    -55.0,
            "window_start_ms":      0,
            "window_duration_ms":   int(len(samples) / sr * 1000),
        }

    if head_idx is None:
        head_idx = tail_idx
    if tail_idx is None:
        tail_idx = head_idx

    head_seg, head_start_s, head_center_off = _extract(head_idx)
    tail_seg, tail_start_s, tail_center_off = _extract(tail_idx)

    # ── fine 1ms envelope ─────────────────────────────────────────────────────
    fine_ms = 1
    fw = max(1, int(sr * fine_ms / 1000))

    def _fine_envelope(seg: np.ndarray) -> list[float]:
        result = []
        n = len(seg) // fw
        for i in range(n):
            chunk = seg[i * fw:(i + 1) * fw]
            peak = float(np.max(np.abs(chunk))) if len(chunk) else 0.0
            db = 20.0 * np.log10(peak / max_val) if peak > 0 else -96.0
            result.append(round(float(max(-96.0, db)), 1))
        return result

    head_env = _fine_envelope(head_seg)
    tail_env = _fine_envelope(tail_seg)

    # Positions are each relative to their own envelope
    head_fine_pos = max(0, min(len(head_env) - 1, head_center_off // fw))
    tail_fine_pos = max(0, min(len(tail_env) - 1, tail_center_off // fw))

    # If windows overlap (very short file), use head envelope for both
    if tail_start_s < head_start_s + len(head_seg):
        tail_env = head_env
        tail_fine_pos = head_fine_pos

    # ── noise floor: head silence (before speech) + tail silence (after speech) ─
    head_silence = np.array(head_env[:max(1, head_fine_pos - 20)])
    tail_silence = np.array(tail_env[min(len(tail_env) - 1, tail_fine_pos + 20):])
    noise_samples = np.concatenate([head_silence, tail_silence]) if len(tail_silence) else head_silence
    if len(noise_samples) >= 3:
        noise_p80 = float(np.percentile(noise_samples, 80))
        auto_db = min(-45.0, max(-80.0, noise_p80 + 2.0))
    else:
        auto_db = -55.0

    return {
        "head_envelope":        head_env,
        "head_transition_pos":  head_fine_pos,
        "tail_envelope":        tail_env,
        "tail_transition_pos":  tail_fine_pos,
        "auto_threshold_db":    round(auto_db, 1),
        "window_start_ms":      int(head_start_s / sr * 1000),
        "window_duration_ms":   int(len(head_seg) / sr * 1000),
        "tail_window_start_ms": int(tail_start_s / sr * 1000),
        "tail_window_duration_ms": int(len(tail_seg) / sr * 1000),
    }

def compute_waveform_envelope(
    audio_path: str,
    resolution_ms: int = 100,
    max_points: int = 600,
) -> list[float]:
    """Return peak dBFS per time window for waveform visualization.

    Uses dBFS-linear coordinates so the threshold line is visible at
    typical game-audio levels (-55 dBFS maps to ~76% of half-height).
    """
    audio = AudioSegment.from_file(audio_path)
    samples = np.array(audio.get_array_of_samples(), dtype=np.float64)
    if audio.channels > 1:
        samples = samples.reshape(-1, audio.channels).mean(axis=1)

    sr = audio.frame_rate
    max_val = float(2 ** (audio.sample_width * 8 - 1))
    window_size = int(sr * resolution_ms / 1000)
    if window_size == 0:
        return []

    n_windows = len(samples) // window_size
    # Cap to max_points by increasing window size if needed
    if n_windows > max_points:
        window_size = len(samples) // max_points
        n_windows = len(samples) // window_size

    peaks: list[float] = []
    for i in range(n_windows):
        chunk = samples[i * window_size:(i + 1) * window_size]
        peak = float(np.max(np.abs(chunk))) if len(chunk) > 0 else 0.0
        db = 20.0 * np.log10(peak / max_val) if peak > 0 else -96.0
        peaks.append(round(max(-96.0, db), 1))

    return peaks


def cluster_speakers_by_params(
    speaker_params: list[SpeakerParams],
    threshold_db_tolerance: float = 5.0,
    loudness_tolerance: float = 3.0,
) -> list[ParamGroup]:
    """Group speakers whose *threshold_db*, loudness, and *sample_rate* are similar.

    Speakers with different sample_rate are always placed in separate groups —
    different sample rates require separate export pipelines.
    """
    groups: list[ParamGroup] = []

    for sp in speaker_params:
        assigned = False
        for grp in groups:
            rep = grp.representative_params
            if (
                rep.sample_rate == sp.sample_rate
                and abs(sp.threshold_db - rep.threshold_db) <= threshold_db_tolerance
                and abs(sp.mean_db - rep.mean_db) <= loudness_tolerance
            ):
                grp.speaker_ids.append(sp.speaker_id)
                assigned = True
                break

        if not assigned:
            groups.append(
                ParamGroup(
                    group_id=f"group_{len(groups)}",
                    speaker_ids=[sp.speaker_id],
                    representative_params=sp,
                )
            )

    return groups


def sample_preview_files(
    group: ParamGroup,
    output_path: str,
    count: int = 5,
) -> list[str]:
    """Return up to *count* representative audio paths from *group* for preview.

    Collects every audio file from ``{output_path}/{spk}/audio/`` for each
    speaker in the group, then returns evenly-spaced samples (or all files
    if fewer than *count* exist).
    """
    output = Path(output_path)
    extensions = {".wav", ".mp3", ".flac"}
    all_files: list[str] = []

    for spk_id in group.speaker_ids:
        audio_dir = output / spk_id / "audio"
        if audio_dir.is_dir():
            for f in sorted(audio_dir.iterdir()):
                if f.is_file() and f.suffix.lower() in extensions:
                    all_files.append(str(f))

    sampled = _sample_evenly(all_files, count)
    return sampled
