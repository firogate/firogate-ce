from pathlib import Path
from sqlalchemy import event
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from app.core.config import get_settings

settings = get_settings()
Path("data").mkdir(exist_ok=True)

# Build engine kwargs based on the database backend so the same code runs on
# SQLite (local dev) and PostgreSQL (production) without changes.
_db_url = settings.DATABASE_URL
_is_sqlite = _db_url.startswith("sqlite")

_engine_kwargs: dict = {"echo": False}
if _is_sqlite:
    # timeout is sqlite3's busy-wait in seconds: when a second writer hits a
    # locked DB (SQLite only allows one writer at a time), it retries for up
    # to this long instead of raising "database is locked" immediately. This
    # matters here because the Spark scanner (triggered both by its poll
    # loop and by blocknotify, see app/api/internal.py) and a normal request
    # handler (e.g. payment_links.py's checkout) can each open a write
    # transaction at the same moment.
    _engine_kwargs["connect_args"] = {"check_same_thread": False, "timeout": 15}
else:
    # PostgreSQL (asyncpg): real connection pooling for concurrency under load.
    _engine_kwargs.update(
        pool_size=getattr(settings, "DB_POOL_SIZE", 20),
        max_overflow=getattr(settings, "DB_MAX_OVERFLOW", 10),
        pool_timeout=30,
        pool_recycle=1800,      # recycle connections every 30 min
        pool_pre_ping=True,     # detect dropped connections
    )

engine = create_async_engine(_db_url, **_engine_kwargs)

if _is_sqlite:
    # WAL lets readers proceed while a writer is mid-transaction (default
    # "DELETE" journal mode blocks readers too), so most of the concurrent
    # read/write traffic this app generates (dashboard polling, webhook
    # retries, the Spark scanner) never reaches the writer-vs-writer
    # contention that `timeout` above handles as a fallback.
    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragma(dbapi_conn, _):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()

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
    import app.models.spark as _spk
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_ensure_user_columns)
        await conn.run_sync(_fix_payments_schema)
        await conn.run_sync(_ensure_payment_spark_columns)
        await conn.run_sync(_encrypt_spark_view_keys)
        await conn.run_sync(_drop_legacy_view_key_hex)
        await conn.run_sync(_ensure_spark_project_column)
        await conn.run_sync(_normalize_operator_role)
        await conn.run_sync(_normalize_operator_notes_columns)
        await conn.run_sync(_drop_orphaned_tables)


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
        ("app_name_change_count",   "INTEGER DEFAULT 0 NOT NULL"),
        ("app_name_last_changed_at","DATETIME"),
        ("app_logo",                "TEXT"),
        ("theme_qr_position",       "VARCHAR(8)"),
        ("theme_cancel_position",   "VARCHAR(8)"),
        ("theme_bg_image",          "TEXT"),
        ("theme_bg_overlay",        "VARCHAR(4)"),
        ("theme_v2_colors_json",    "TEXT"),
        ("wallet_address",                "VARCHAR(128)"),
        ("auth_method",                   "VARCHAR(16) DEFAULT 'password'"),
        ("lifetime_gross_sales_firo",     "FLOAT DEFAULT 0.0"),
        ("lifetime_received_firo",        "FLOAT DEFAULT 0.0"),
        ("lifetime_confirmed_payments",   "INTEGER DEFAULT 0"),
        ("lifetime_completed_orders",     "INTEGER DEFAULT 0"),
        ("rollover_requests",             "INTEGER DEFAULT 0"),
        ("rollover_expires_at",           "DATETIME"),
        ("cycle_start_at",                "DATETIME"),
        ("checkout_layout",               "VARCHAR(16) DEFAULT 'stripe'"),
        ("totp_last_step",                "INTEGER"),
        ("telegram_id",                   "VARCHAR(32)"),
        ("last_login_ip",                 "VARCHAR(64)"),
        ("last_login_device",             "VARCHAR(256)"),
        ("telegram_chat_id",              "VARCHAR(32)"),
        ("notify_telegram",               "BOOLEAN DEFAULT 0"),
        ("blocked_reason",                "TEXT"),
        ("blocked_at",                    "DATETIME"),
        ("blocked_by",                    "VARCHAR(36)"),
        ("spark_connect_enabled",         "BOOLEAN"),
        ("show_market_price",             "BOOLEAN DEFAULT 0 NOT NULL"),
        # Merchant Setup checklist: existing accounts are unlocked by
        # default same as new registrations (auth.py /register sets this
        # explicitly too) operators can still lock individual accounts
        # via the panel if needed, gated separately by the global Wallet
        # Connection Access switch (EmergencyControl "disable_wallet_connections").
        ("merchant_setup_unlocked",       "BOOLEAN DEFAULT 1 NOT NULL"),
        ("setup_learned_basics",          "BOOLEAN DEFAULT 0 NOT NULL"),
        ("has_seen_onboarding",           "BOOLEAN DEFAULT 0 NOT NULL"),
        ("account_number_hash",           "VARCHAR(128)"),
        ("account_number_lookup",         "VARCHAR(64)"),
        ("account_number_enc",            "VARCHAR(512)"),
        ("required_confirmations_policy", "INTEGER"),
        ("payment_tolerance_firo",        "FLOAT"),
    ]
    for col, ddl in additions:
        if col not in existing:
            sync_conn.execute(text(f"ALTER TABLE users ADD COLUMN {col} {ddl}"))

    # One-time backfill: installs that already had merchant_setup_unlocked
    # from an earlier deploy (default was locked/0 then) get every existing
    # account unlocked once, same as the DEFAULT 1 new installs get from
    # the ALTER TABLE above. Gated by a marker row so it runs exactly once
    # ever an operator's later "Lock" on a specific merchant must never be
    # silently undone by a subsequent server restart.
    _MIGRATION_KEY = "merchant_setup_unlocked_backfilled_v1"
    sync_conn.execute(text(
        "CREATE TABLE IF NOT EXISTS _migrations_applied (key VARCHAR PRIMARY KEY, applied_at DATETIME DEFAULT CURRENT_TIMESTAMP)"
    ))
    already_ran = sync_conn.execute(text(
        "SELECT 1 FROM _migrations_applied WHERE key = :k"
    ), {"k": _MIGRATION_KEY}).first()
    if not already_ran:
        sync_conn.execute(text(
            "UPDATE users SET merchant_setup_unlocked = 1 WHERE merchant_setup_unlocked = 0"
        ))
        sync_conn.execute(text(
            "INSERT INTO _migrations_applied (key) VALUES (:k)"
        ), {"k": _MIGRATION_KEY})
    try:
        sync_conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_users_firebase_uid "
            "ON users(firebase_uid) WHERE firebase_uid IS NOT NULL"
        ))
        sync_conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_users_wallet_address "
            "ON users(wallet_address) WHERE wallet_address IS NOT NULL"
        ))
        sync_conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_users_telegram_id "
            "ON users(telegram_id) WHERE telegram_id IS NOT NULL"
        ))
        sync_conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_users_account_number_lookup "
            "ON users(account_number_lookup) WHERE account_number_lookup IS NOT NULL"
        ))
    except Exception:
        pass

    # plan_configs: add new columns on existing installs
    try:
        pc_rows = sync_conn.execute(text("PRAGMA table_info(plan_configs)")).fetchall()
        pc_existing = {r[1] for r in pc_rows}
        if pc_rows:
            if "currency" not in pc_existing:
                sync_conn.execute(text(
                    "ALTER TABLE plan_configs ADD COLUMN currency VARCHAR DEFAULT 'USD'"
                ))
            if "max_rollover_balance" not in pc_existing:
                sync_conn.execute(text(
                    "ALTER TABLE plan_configs ADD COLUMN max_rollover_balance INTEGER DEFAULT 0"
                ))
            if "features" not in pc_existing:
                sync_conn.execute(text(
                    "ALTER TABLE plan_configs ADD COLUMN features JSON"
                ))
    except Exception:
        pass

    # daily_stats: rename total_revenue → total_volume_firo, drop platform_fees
    try:
        ds_rows = sync_conn.execute(text("PRAGMA table_info(daily_stats)")).fetchall()
        ds_existing = {r[1] for r in ds_rows}
        if ds_rows:
            if "total_volume_firo" not in ds_existing:
                sync_conn.execute(text(
                    "ALTER TABLE daily_stats ADD COLUMN total_volume_firo FLOAT DEFAULT 0.0"
                ))
                # Copy existing data from the old column if it exists
                if "total_revenue" in ds_existing:
                    sync_conn.execute(text(
                        "UPDATE daily_stats SET total_volume_firo = total_revenue WHERE total_volume_firo IS NULL OR total_volume_firo = 0.0"
                    ))
    except Exception:
        pass

    # user_daily_stats: add gross_sales_firo + received_firo
    try:
        uds_rows = sync_conn.execute(text("PRAGMA table_info(user_daily_stats)")).fetchall()
        uds_existing = {r[1] for r in uds_rows}
        if uds_rows:
            if "gross_sales_firo" not in uds_existing:
                sync_conn.execute(text(
                    "ALTER TABLE user_daily_stats ADD COLUMN gross_sales_firo FLOAT DEFAULT 0.0"
                ))
                # Copy from old revenue column if it exists
                if "revenue" in uds_existing:
                    sync_conn.execute(text(
                        "UPDATE user_daily_stats SET gross_sales_firo = revenue WHERE gross_sales_firo IS NULL OR gross_sales_firo = 0.0"
                    ))
            if "received_firo" not in uds_existing:
                sync_conn.execute(text(
                    "ALTER TABLE user_daily_stats ADD COLUMN received_firo FLOAT DEFAULT 0.0"
                ))
                # Best-effort: seed received_firo from gross_sales_firo (close enough for historical data)
                sync_conn.execute(text(
                    "UPDATE user_daily_stats SET received_firo = gross_sales_firo WHERE received_firo IS NULL OR received_firo = 0.0"
                ))
    except Exception:
        pass


def _encrypt_spark_view_keys(sync_conn) -> None:
    if not str(sync_conn.engine.url).startswith("sqlite"):
        return
    from sqlalchemy import text
    rows = sync_conn.execute(text("PRAGMA table_info(spark_wallet_connections)")).fetchall()
    existing = {r[1] for r in rows}
    if "view_key_enc" not in existing:
        sync_conn.execute(text("ALTER TABLE spark_wallet_connections ADD COLUMN view_key_enc VARCHAR(512)"))

    if "view_key_hex" not in existing:
        return

    from app.core.security import encrypt_field
    plaintext_rows = sync_conn.execute(text(
        "SELECT id, view_key_hex FROM spark_wallet_connections "
        "WHERE view_key_hex IS NOT NULL AND view_key_hex != '' "
        "AND (view_key_enc IS NULL OR view_key_enc = '')"
    )).fetchall()
    for row_id, plaintext in plaintext_rows:
        enc = encrypt_field(plaintext)
        sync_conn.execute(
            text("UPDATE spark_wallet_connections SET view_key_enc = :enc, view_key_hex = '' WHERE id = :id"),
            {"enc": enc, "id": row_id},
        )


def _drop_legacy_view_key_hex(sync_conn) -> None:
    """view_key_hex was superseded by the encrypted view_key_enc column, but
    its NOT NULL constraint can't be altered in place in SQLite new inserts
    (which no longer set view_key_hex at all) fail until the column itself is
    dropped."""
    if not str(sync_conn.engine.url).startswith("sqlite"):
        return
    from sqlalchemy import text
    rows = sync_conn.execute(text("PRAGMA table_info(spark_wallet_connections)")).fetchall()
    existing = {r[1] for r in rows}
    if "view_key_hex" not in existing:
        return
    sync_conn.execute(text("ALTER TABLE spark_wallet_connections DROP COLUMN view_key_hex"))


def _ensure_spark_project_column(sync_conn) -> None:
    if not str(sync_conn.engine.url).startswith("sqlite"):
        return
    from sqlalchemy import text
    rows = sync_conn.execute(text("PRAGMA table_info(spark_wallet_connections)")).fetchall()
    existing = {r[1] for r in rows}
    if "project_id" not in existing:
        sync_conn.execute(text("ALTER TABLE spark_wallet_connections ADD COLUMN project_id VARCHAR"))


def _normalize_operator_role(sync_conn) -> None:
    """Keep the operator role value normalized on existing installs."""
    from sqlalchemy import text
    sync_conn.execute(text("UPDATE users SET role = 'operator' WHERE role = 'admin'"))


def _normalize_operator_notes_columns(sync_conn) -> None:
    """Keep the operator-notes column name normalized on existing installs."""
    if not str(sync_conn.engine.url).startswith("sqlite"):
        return
    from sqlalchemy import text
    for table in ("reports", "merchant_applications"):
        rows = sync_conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
        existing = {r[1] for r in rows}
        if "admin_notes" in existing and "operator_notes" not in existing:
            sync_conn.execute(text(f"ALTER TABLE {table} RENAME COLUMN admin_notes TO operator_notes"))


def _drop_orphaned_tables(sync_conn) -> None:
    """Drop tables left behind by removed features (privacy-routing, license system)."""
    if not str(sync_conn.engine.url).startswith("sqlite"):
        return
    from sqlalchemy import text
    for table in ("routing_hops", "routing_chains", "licenses",
                  "license_audit_log", "license_client_state", "license_challenges"):
        sync_conn.execute(text(f"DROP TABLE IF EXISTS {table}"))


def _fix_payments_schema(sync_conn) -> None:
    """
    Remove legacy fee columns from the payments table.

    SQLite does not support ALTER COLUMN or DROP COLUMN (pre-3.35), so we
    use the standard SQLite table-rebuild pattern:
      1. Create payments_new with the clean schema
      2. Copy all rows, defaulting removed columns to 0 / NULL
      3. Drop payments
      4. Rename payments_new → payments
      5. Recreate indexes

    Idempotent: skips if platform_fee_pct is already gone.
    """
    if not str(sync_conn.engine.url).startswith("sqlite"):
        return
    from sqlalchemy import text
    rows = sync_conn.execute(text("PRAGMA table_info(payments)")).fetchall()
    existing_cols = {r[1] for r in rows}
    if "platform_fee_pct" not in existing_cols:
        return

    keep_cols = [
        "id", "merchant_id", "receiving_address", "receiving_address_label",
        "amount_firo", "amount_received", "txid", "vout", "confirmations",
        "required_confirmations", "block_height", "status", "order_id",
        "order_description", "customer_email", "metadata_json", "success_url",
        "cancel_url", "collect_email", "email_collected_at", "extend_count",
        "created_at", "expires_at", "confirmed_at", "webhook_sent",
        "webhook_sent_at", "webhook_response", "webhook_attempts",
        "webhook_next_retry_at", "manual_txhash", "manual_check_result",
        "customer_ip",
    ]
    copy_cols = [c for c in keep_cols if c in existing_cols]
    col_list  = ", ".join(copy_cols)

    sync_conn.execute(text("PRAGMA foreign_keys = OFF"))
    try:
        sync_conn.execute(text("""
            CREATE TABLE payments_new (
                id                      VARCHAR      NOT NULL PRIMARY KEY,
                merchant_id             VARCHAR      NOT NULL,
                receiving_address       VARCHAR(256) NOT NULL,
                receiving_address_label VARCHAR(128),
                amount_firo             FLOAT        NOT NULL,
                amount_received         FLOAT,
                txid                    VARCHAR(64),
                vout                    INTEGER,
                confirmations           INTEGER,
                required_confirmations  INTEGER,
                block_height            INTEGER,
                status                  VARCHAR(10),
                order_id                VARCHAR(256),
                order_description       VARCHAR(512),
                customer_email          VARCHAR(254),
                metadata_json           TEXT,
                success_url             VARCHAR(2048),
                cancel_url              VARCHAR(2048),
                collect_email           BOOLEAN,
                email_collected_at      DATETIME,
                extend_count            INTEGER,
                created_at              DATETIME,
                expires_at              DATETIME,
                confirmed_at            DATETIME,
                webhook_sent            BOOLEAN,
                webhook_sent_at         DATETIME,
                webhook_response        VARCHAR(512),
                webhook_attempts        INTEGER,
                webhook_next_retry_at   DATETIME,
                manual_txhash           VARCHAR(64),
                manual_check_result     VARCHAR(64),
                customer_ip             VARCHAR(45),
                FOREIGN KEY(merchant_id) REFERENCES users(id),
                UNIQUE(txid, vout)
            )
        """))
        sync_conn.execute(text(
            f"INSERT INTO payments_new ({col_list}) SELECT {col_list} FROM payments"
        ))
        sync_conn.execute(text("DROP TABLE payments"))
        sync_conn.execute(text("ALTER TABLE payments_new RENAME TO payments"))
        for stmt in [
            "CREATE INDEX IF NOT EXISTS ix_payments_merchant_id    ON payments (merchant_id)",
            "CREATE INDEX IF NOT EXISTS ix_payments_receiving_address ON payments (receiving_address)",
            "CREATE INDEX IF NOT EXISTS ix_payment_address         ON payments (receiving_address)",
            "CREATE INDEX IF NOT EXISTS ix_payments_status         ON payments (status)",
            "CREATE INDEX IF NOT EXISTS ix_payments_txid           ON payments (txid)",
        ]:
            try:
                sync_conn.execute(text(stmt))
            except Exception:
                pass
    finally:
        sync_conn.execute(text("PRAGMA foreign_keys = ON"))


def _ensure_payment_spark_columns(sync_conn) -> None:
    """Idempotent column additions for Spark support on existing installs."""
    if not str(sync_conn.engine.url).startswith("sqlite"):
        return
    from sqlalchemy import text
    rows = sync_conn.execute(text("PRAGMA table_info(payments)")).fetchall()
    existing = {r[1] for r in rows}
    additions = [
        ("address_type",      "VARCHAR(16) DEFAULT 'spark' NOT NULL"),
        ("spark_diversifier", "INTEGER"),
        ("spark_coin_tag",    "VARCHAR(128)"),
        ("spark_owner_id",    "VARCHAR"),
        ("spark_coin_tags_json", "TEXT"),
        ("start_block_height", "INTEGER"),
    ]
    for col, ddl in additions:
        if col not in existing:
            sync_conn.execute(text(f"ALTER TABLE payments ADD COLUMN {col} {ddl}"))
    try:
        sync_conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_payments_spark_coin_tag "
            "ON payments(spark_coin_tag) WHERE spark_coin_tag IS NOT NULL"
        ))
    except Exception:
        pass

