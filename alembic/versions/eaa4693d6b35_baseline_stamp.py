"""baseline_stamp

Revision ID: eaa4693d6b35
Revises:
Create Date: 2026-08-06

INTENTIONAL NO-OP. Do not fill in upgrade()/downgrade() to "build" CE's
schema — that is app/core/database.py's create_tables() job, unchanged and
still run on every boot. This revision exists purely so `alembic stamp head`
can mark an existing, already-built CE database as "up to date" without
Alembic ever executing DDL against it.

Running `alembic upgrade head` against a fresh empty database will create
ZERO tables here — that's correct. Use `alembic stamp head` against a
database that create_tables() has already built, then write real Alembic
migrations for any *future* schema change instead of a new `_ensure_*`
function in database.py.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'eaa4693d6b35'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
