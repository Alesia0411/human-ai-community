# 文件职责：数据库迁移：账号侧会话隐藏状态；已发布迁移不应通过修改内容替代新增版本。
"""Account-specific removal from the conversation list."""
import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


# upgrade：按本迁移向前增加或调整账号侧会话隐藏状态。
def upgrade():
    for name in ("creator_hidden", "peer_hidden"):
        op.add_column("conversations", sa.Column(name, sa.Boolean(), nullable=False, server_default=sa.false()))


# downgrade：逆向撤回本迁移的结构；删除列或表可能丢失对应数据。
def downgrade():
    for name in ("peer_hidden", "creator_hidden"):
        op.drop_column("conversations", name)
