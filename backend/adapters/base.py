from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class TTSItem:
    key: str
    text: str
    prompt_audio_path: str
    prompt_text: str = ""
    emotion: str = ""


@dataclass
class TTSResult:
    key: str
    audio_bytes: bytes | None
    error: str | None = None


class TTSModel(ABC):
    @abstractmethod
    def synthesize(self, item: TTSItem, params: dict) -> TTSResult: ...

    @abstractmethod
    def validate(self) -> bool: ...
