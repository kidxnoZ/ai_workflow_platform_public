import os
from pathlib import Path


def get_metadata(audio_paths: list[str]) -> dict:
    """Collect basic metadata for a list of audio files."""
    from pydub import AudioSegment

    total_duration = 0.0
    sample_rates = []
    valid = 0

    for path in audio_paths:
        try:
            audio = AudioSegment.from_file(path)
            total_duration += len(audio) / 1000.0
            sample_rates.append(audio.frame_rate)
            valid += 1
        except Exception:
            pass

    dominant_sr = max(set(sample_rates), key=sample_rates.count) if sample_rates else 0

    return {
        "total_files": len(audio_paths),
        "valid_files": valid,
        "total_duration_sec": round(total_duration, 2),
        "sample_rate": dominant_sr,
    }
