from pydub import AudioSegment


def concat_wavs(paths: list[str], output_path: str, silence_between_ms: int = 100) -> str:
    result = AudioSegment.empty()
    for i, p in enumerate(paths):
        seg = AudioSegment.from_file(p)
        result += seg
        if i < len(paths) - 1 and silence_between_ms > 0:
            result += AudioSegment.silent(duration=silence_between_ms, frame_rate=seg.frame_rate)
    result.export(output_path, format="wav")
    return output_path
