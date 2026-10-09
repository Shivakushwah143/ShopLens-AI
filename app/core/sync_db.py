from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from sqlmodel import SQLModel
from app.core.config import get_settings

settings = get_settings()

# Sync engine for stable SQLite operations on Windows
sync_database_url = settings.database_url.replace("sqlite+aiosqlite", "sqlite")
sync_engine = create_engine(sync_database_url, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=sync_engine)

def init_sync_db():
    SQLModel.metadata.create_all(sync_engine)

def get_sync_session():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
