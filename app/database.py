"""Database engine and session management."""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import settings

# Engine kwargs differ between SQLite (tests) and PostgreSQL (production).
# SQLite ignores pool_size/max_overflow and the SingletonThreadPool rejects them.
_engine_kwargs: dict = {"pool_pre_ping": True}
if not settings.DATABASE_URL.startswith("sqlite"):
    _engine_kwargs["pool_size"] = 10
    _engine_kwargs["max_overflow"] = 20

engine = create_engine(settings.DATABASE_URL, **_engine_kwargs)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
