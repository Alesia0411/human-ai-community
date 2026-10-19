# 文件职责：数据库迁移：长期聊天摘要与文件同步状态；已发布迁移不应通过修改内容替代新增版本。
"""Durable five-message summaries and retryable transcript files."""
import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


# upgrade：按本迁移向前增加或调整长期聊天摘要与文件同步状态。
def upgrade():
    op.add_column("conversations", sa.Column("files_dirty", sa.Boolean(), nullable=False, server_default=sa.true()))
    op.create_table(
        "chat_memories",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("created_at", sa.Float(53), nullable=False),
        sa.Column("conversation_id", sa.String(32), sa.ForeignKey("conversations.id"), nullable=False),
        sa.Column("batch_key", sa.String(64), nullable=False),
        sa.Column("message_ids", sa.JSON(), nullable=False),
        sa.Column("end_seq", sa.Integer(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.UniqueConstraint("conversation_id", "batch_key"),
    )
    op.create_index("ix_chat_memories_conversation_id", "chat_memories", ["conversation_id"])
    op.create_index("ix_chat_memories_created_at", "chat_memories", ["created_at"])


# downgrade：逆向撤回本迁移的结构；删除列或表可能丢失对应数据。
def downgrade():
    op.drop_table("chat_memories")
    op.drop_column("conversations", "files_dirty")
