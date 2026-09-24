import struct
import wave
import io
from backend.adapters.base import TTSModel, TTSItem, TTSResult


def _make_silent_wav(duration_ms: int = 500, sample_rate: int = 22050) -> bytes:
    """Generate a silent WAV file for stub testing."""
    num_samples = int(sample_rate * duration_ms / 1000)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(struct.pack("<" + "h" * num_samples, *([0] * num_samples)))
    return buf.getvalue()


class MiniMaxAdapter(TTSModel):
    """
    MiniMax internal TTS adapter.
    TODO: Fill in endpoint, auth headers, request/response schema
          once internal API documentation is available.
    Currently returns a stub silent WAV so the pipeline can be tested end-to-end.
    """

    def __init__(self, api_url: str, api_key: str):
        self.api_url = api_url
        self.api_key = api_key

    def validate(self) -> bool:
        # TODO: ping endpoint when real API is available
        return True

    def synthesize(self, item: TTSItem, params: dict) -> TTSResult:
        if not self.api_url or self.api_url.startswith("http://placeholder"):
            # Stub mode: return silent audio
            return TTSResult(key=item.key, audio_bytes=_make_silent_wav())

        # TODO: implement real MiniMax HTTP call
        # Example skeleton (fill after receiving internal API docs):
        #
        # import requests, base64
        # with open(item.prompt_audio_path, "rb") as f:
        #     prompt_b64 = base64.b64encode(f.read()).decode()
        # payload = {
        #     "text": item.text,
        #     "prompt_audio": prompt_b64,
        #     "prompt_text": item.prompt_text,
        #     "emotion": item.emotion,
        #     **params,
        # }
        # resp = requests.post(
        #     self.api_url,
        #     json=payload,
        #     headers={"Authorization": f"Bearer {self.api_key}"},
        #     timeout=60,
        # )
        # resp.raise_for_status()
        # audio_bytes = resp.content  # or parse JSON for base64 field
        # return TTSResult(key=item.key, audio_bytes=audio_bytes)

        return TTSResult(key=item.key, audio_bytes=None, error="MiniMax API not configured")
