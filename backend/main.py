from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pathlib import Path

from backend.database import init_db
from backend.config import CORS_ORIGINS, STORAGE_DIR
from backend.api import tasks, files, tts, dataset, voices, agent, offline_eval, dataset_ingest


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(title="AI Workflow Platform", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(tasks.router, prefix="/api/tasks", tags=["tasks"])
app.include_router(files.router, prefix="/api/files", tags=["files"])
app.include_router(tts.router, prefix="/api/tts", tags=["tts"])
app.include_router(dataset.router, prefix="/api/dataset", tags=["dataset"])
app.include_router(voices.router, prefix="/api/voices", tags=["voices"])
app.include_router(agent.router, prefix="/api/agent", tags=["agent"])
app.include_router(offline_eval.router, prefix="/api/offline-eval", tags=["offline-eval"])
app.include_router(dataset_ingest.router)


@app.get("/api/health")
def health():
    return {"status": "ok"}


app.mount("/storage", StaticFiles(directory=str(STORAGE_DIR)), name="storage")

FRONTEND_DIST = Path(__file__).parent.parent / "frontend" / "dist"
if FRONTEND_DIST.exists():
    app.mount("/assets", StaticFiles(directory=str(FRONTEND_DIST / "assets")), name="assets")

    @app.get("/{full_path:path}")
    def serve_frontend(full_path: str):
        return FileResponse(str(FRONTEND_DIST / "index.html"))
