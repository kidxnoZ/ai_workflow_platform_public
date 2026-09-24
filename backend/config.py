import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env", override=True)

ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_AUTH_TOKEN: str = os.getenv("ANTHROPIC_AUTH_TOKEN", "")
ANTHROPIC_BASE_URL: str = os.getenv("ANTHROPIC_BASE_URL", "")
ANTHROPIC_MODEL: str = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5")

CORS_ORIGINS = [
    origin.strip()
    for origin in os.getenv(
        "CORS_ORIGINS",
        "http://localhost:5173,http://127.0.0.1:5173",
    ).split(",")
    if origin.strip()
]

MINIMAX_API_URL = os.getenv("MINIMAX_API_URL", "")
MINIMAX_API_KEY = os.getenv("MINIMAX_API_KEY", "")
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "base")

_storage = os.getenv("STORAGE_DIR", "storage")
STORAGE_DIR = Path(__file__).parent.parent / _storage
UPLOADS_DIR = STORAGE_DIR / "uploads"
OUTPUTS_DIR = STORAGE_DIR / "outputs"
DATASETS_DIR = STORAGE_DIR / "datasets"

for d in (UPLOADS_DIR, OUTPUTS_DIR, DATASETS_DIR):
    d.mkdir(parents=True, exist_ok=True)

DB_PATH = STORAGE_DIR / "data.db"
DATABASE_URL = f"sqlite:///{DB_PATH}"
