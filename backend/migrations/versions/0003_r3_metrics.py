# 文件职责：数据库迁移：任务运行计时指标；已发布迁移不应通过修改内容替代新增版本。
"""R3 payer-private latency and usage provenance. Historical values stay unknown."""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


# upgrade：按本迁移向前增加或调整任务运行计时指标。
def upgrade():
    for column in ("started_at", "finished_at", "first_token_at"):
        op.add_column("ai_runs", sa.Column(column, sa.Float(53), nullable=True))
    op.add_column("call_ledger", sa.Column("finished_at", sa.Float(53), nullable=True))
    op.add_column(
        "call_ledger", sa.Column("usage_known", sa.Boolean(), nullable=False, server_default=sa.false())
    )


# downgrade：逆向撤回本迁移的结构；删除列或表可能丢失对应数据。
def downgrade():
    op.drop_column("call_ledger", "usage_known")
    op.drop_column("call_ledger", "finished_at")
    for column in ("first_token_at", "finished_at", "started_at"):
        op.drop_column("ai_runs", column)
