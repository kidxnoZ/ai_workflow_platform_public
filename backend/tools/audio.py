import numpy as np
from pydub import AudioSegment


def find_speech_bounds(audio, threshold_db=-55, frame_ms=10, hold_ms=50):
    samples = np.array(audio.get_array_of_samples(), dtype=np.float32)
    if audio.channels > 1:
        samples = samples.reshape(-1, audio.channels).mean(axis=1)

    sr = audio.frame_rate
    frame_size = int(sr * frame_ms / 1000)
    max_val = 2 ** (audio.sample_width * 8 - 1)
    hold_frames = max(1, int(hold_ms / frame_ms))

    n = len(samples) // frame_size
    dbs = []
    for i in range(n):
        chunk = samples[i * frame_size:(i + 1) * frame_size]
        rms = np.sqrt(np.mean(chunk ** 2))
        dbs.append(20 * np.log10(rms / max_val) if rms > 0 else -120)
    dbs = np.array(dbs)
    is_speech = dbs >= threshold_db

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

    return speech_start_ms, speech_end_ms


def segment_by_silence(audio, min_silence_ms=2000, silence_thresh_db=-45, keep_silence_ms=100):
    """
    Split audio into segments wherever silence exceeds min_silence_ms.
    Returns list of (start_ms, end_ms) tuples relative to the original audio.
    """
    frame_ms = 10
    samples = np.array(audio.get_array_of_samples(), dtype=np.float32)
    if audio.channels > 1:
        samples = samples.reshape(-1, audio.channels).mean(axis=1)

    frame_size = int(audio.frame_rate * frame_ms / 1000)
    max_val = 2 ** (audio.sample_width * 8 - 1)

    n = len(samples) // frame_size
    is_silent = []
    for i in range(n):
        chunk = samples[i * frame_size:(i + 1) * frame_size]
        rms = np.sqrt(np.mean(chunk ** 2))
        db = 20 * np.log10(rms / max_val) if rms > 0 else -120
        is_silent.append(db < silence_thresh_db)

    min_silence_frames = min_silence_ms // frame_ms
    segments = []
    in_speech = False
    seg_start = 0
    silence_count = 0

    for i, silent in enumerate(is_silent):
        if not silent:
            if not in_speech:
                in_speech = True
                seg_start = i
            silence_count = 0
        else:
            if in_speech:
                silence_count += 1
                if silence_count >= min_silence_frames:
                    seg_end = (i - silence_count + 1) * frame_ms
                    segments.append((
                        max(0, seg_start * frame_ms - keep_silence_ms),
                        min(len(audio), seg_end + keep_silence_ms),
                    ))
                    in_speech = False
                    silence_count = 0

    if in_speech:
        segments.append((
            max(0, seg_start * frame_ms - keep_silence_ms),
            len(audio),
        ))

    if not segments:
        segments = [(0, len(audio))]

    return segments
