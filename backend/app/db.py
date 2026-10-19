# 文件职责：创建 SQLAlchemy 引擎和会话工厂；请求结束关闭会话，事务提交由业务代码明确控制。
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import settings


# Base：ORM 声明基类；模型元数据和迁移据此识别表结构。
class Base(DeclarativeBase):
    pass


url = settings().database_url
if url.startswith("sqlite"):
    Path("data").mkdir(exist_ok=True)
engine = create_engine(
    url, pool_pre_ping=True, connect_args={"check_same_thread": False} if url.startswith("sqlite") else {}
)
if url.startswith("sqlite"):

    # configure_sqlite：为每个 SQLite 连接启用外键并配置锁等待，不代表拥有 MySQL 的行锁行为。
    @event.listens_for(engine, "connect")
    def configure_sqlite(conn, _):
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")


SessionLocal = sessionmaker(engine, expire_on_commit=False)


# get_db：向 FastAPI 依赖提供数据库会话；yield 结束后释放连接，不自动提交事务。
def get_db():
    with SessionLocal() as db:
        yield db
