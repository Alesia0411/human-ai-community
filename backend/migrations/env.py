# 文件职责：Alembic 迁移环境：将当前数据库连接和 ORM 元数据交给迁移上下文。
from alembic import context

from app import models  # noqa: F401
from app.db import Base, engine


# migrate：在给定数据库连接中配置 Alembic 上下文并执行迁移。
def migrate(connection):
    context.configure(connection=connection, target_metadata=Base.metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


with engine.connect() as connection:
    migrate(connection)
