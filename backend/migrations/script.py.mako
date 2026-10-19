# 文件职责：Alembic 新迁移模板；模板变量由生成器替换，不应作为普通 Python 直接执行。
"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
${imports if imports else ""}

# revision identifiers, used by Alembic.
revision: str = ${repr(up_revision)}
down_revision: Union[str, Sequence[str], None] = ${repr(down_revision)}
branch_labels: Union[str, Sequence[str], None] = ${repr(branch_labels)}
depends_on: Union[str, Sequence[str], None] = ${repr(depends_on)}


# upgrade：应用新版本结构；生成后需审核变更及数据影响。
def upgrade() -> None:
    """Upgrade schema."""
    ${upgrades if upgrades else "pass"}


# downgrade：撤回本版本结构；删列/删表可能丢失数据。
def downgrade() -> None:
    """Downgrade schema."""
    ${downgrades if downgrades else "pass"}
