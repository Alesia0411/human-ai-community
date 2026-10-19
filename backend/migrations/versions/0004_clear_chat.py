# 文件职责：数据库迁移：账号侧聊天清空边界；已发布迁移不应通过修改内容替代新增版本。
"""Per-participant chat visibility and AI history reset boundary."""
import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


# upgrade：按本迁移向前增加或调整账号侧聊天清空边界。
def upgrade():
    for name in ("creator_cleared_seq", "peer_cleared_seq"):
        op.add_column("conversations", sa.Column(name, sa.Integer(), nullable=False, server_default="0"))
    op.add_column("conversations", sa.Column("cleared_at", sa.Float(53), nullable=False, server_default="0"))


# downgrade：逆向撤回本迁移的结构；删除列或表可能丢失对应数据。
def downgrade():
    for name in ("cleared_at", "peer_cleared_seq", "creator_cleared_seq"):
        op.drop_column("conversations", name)
