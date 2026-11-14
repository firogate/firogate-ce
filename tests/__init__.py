import os
import tempfile

_DB_DIR = tempfile.mkdtemp(prefix="fg-tests-")

os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_DB_DIR}/test.db"
os.environ["SECRET_KEY"] = "test-secret-key-not-used-in-production-000000000000000"
os.environ["FIELD_ENCRYPTION_KEY"] = "test-field-encryption-key-not-used-in-production"
os.environ["FIRO_RPC_USER"] = "rt"
os.environ["FIRO_RPC_PASSWORD"] = "rtpass"
os.environ["FIRO_RPC_HOST"] = "127.0.0.1"
os.environ["FIRO_RPC_PORT"] = os.environ.get("FIROGATE_REGTEST_RPC_PORT", "28931")
os.environ.setdefault("TOR_ENABLED", "false")
