from pathlib import Path
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from app.core.config import get_settings

settings = get_settings()
Path("data").mkdir(exist_ok=True)

engine = create_async_engine(
    settings.DATABASE_URL,
    echo=False,
    connect_args={"check_same_thread": False},
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=True,
)


class Base(DeclarativeBase):
    pass


async def get_db():
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def create_tables():
    import app.models.models as _
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_ensure_user_columns)


def _ensure_user_columns(sync_conn) -> None:
    """Idempotent column additions for existing SQLite installs (safe no-op otherwise)."""
    if not str(sync_conn.engine.url).startswith("sqlite"):
        return
    from sqlalchemy import text
    rows = sync_conn.execute(text("PRAGMA table_info(users)")).fetchall()
    existing = {r[1] for r in rows}
    additions = [
        ("firebase_uid",            "VARCHAR(128)"),
        ("email_verified",          "BOOLEAN DEFAULT 0"),
        ("password_changed_at",     "DATETIME"),
        ("app_name",                "VARCHAR(64)"),
        ("app_name_locked",         "BOOLEAN DEFAULT 0 NOT NULL"),
        ("app_name_change_allowed", "BOOLEAN DEFAULT 0 NOT NULL"),
    ]
    for col, ddl in additions:
        if col not in existing:
            sync_conn.execute(text(f"ALTER TABLE users ADD COLUMN {col} {ddl}"))
    # Best-effort unique index for firebase_uid
    try:
        sync_conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_users_firebase_uid "
            "ON users(firebase_uid) WHERE firebase_uid IS NOT NULL"
        ))
    except Exception:
        pass

