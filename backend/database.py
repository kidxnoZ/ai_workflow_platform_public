from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker, DeclarativeBase
from backend.config import DATABASE_URL

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})


@event.listens_for(engine, "connect")
def _set_wal_mode(dbapi_conn, _):
    dbapi_conn.execute("PRAGMA journal_mode=WAL")
    dbapi_conn.execute("PRAGMA busy_timeout=30000")
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine,
                            expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _safe_add_columns():
    """幂等地给现有表补新列（SQLAlchemy create_all 不会给已有表加列）。"""
    migrations = [
        "ALTER TABLE model_registry ADD COLUMN install_log TEXT",
        "ALTER TABLE model_registry ADD COLUMN installed_by TEXT",
        "ALTER TABLE eval_ratings ADD COLUMN nmos_score INTEGER",
        "ALTER TABLE eval_samples ADD COLUMN age TEXT DEFAULT ''",
        "ALTER TABLE eval_samples ADD COLUMN gender TEXT DEFAULT ''",
    ]
    with engine.connect() as conn:
        for stmt in migrations:
            try:
                conn.execute(text(stmt))
                conn.commit()
            except Exception:
                pass  # 列已存在时 SQLite 报错，忽略即可


def init_db():
    from backend.models import task  # noqa: F401  (registers ModelRegistry, BenchmarkRun too)
    from backend.models import offline_eval  # noqa: F401
    Base.metadata.create_all(bind=engine)
    _safe_add_columns()
