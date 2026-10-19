# 文件职责：首次发布前冻结初始迁移的开发工具；不得重跑来改写已发布数据库历史。
"""Development utility: freeze the initial schema BEFORE shipping a database.

Never rerun to change a released database. Add a new Alembic revision instead.
"""

from pathlib import Path

from alembic.autogenerate import produce_migrations, render_python_code
from alembic.migration import MigrationContext
from sqlalchemy import create_engine

from app import models  # noqa: F401
from app.db import Base

with create_engine("sqlite://").connect() as connection:
    ctx = MigrationContext.configure(connection)
    migration = produce_migrations(ctx, Base.metadata)
    upgrade = render_python_code(migration.upgrade_ops)
    downgrade = render_python_code(migration.downgrade_ops)
content = (
    '''"""Frozen R1 schema. Future changes require a new revision."""
import sqlalchemy as sa
from alembic import op

revision = "0001_r1"
down_revision = None
branch_labels = None
depends_on = None

def upgrade():
'''
    + upgrade
    + "\n\ndef downgrade():\n"
    + downgrade
    + "\n"
)
Path(__file__).resolve().parents[1].joinpath("migrations/versions/0001_r1.py").write_text(
    content, encoding="utf-8"
)
