# 文件职责：自动化测试：临时 SQLite 库的迁移升级/回退及旧数据保留；具体保证以各测试断言为准。
import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import create_engine, inspect


# test_fresh_database_can_upgrade_downgrade_and_upgrade：验证：新库可以升级、回退后再次升级且保留外键结构；范围以本函数断言为准。
def test_fresh_database_can_upgrade_downgrade_and_upgrade(tmp_path):
    backend = Path(__file__).resolve().parents[1]
    url = "sqlite:///" + (tmp_path / "migration.db").as_posix()
    env = {**os.environ, "HAI_DATABASE_URL": url}
    for direction, target in [("upgrade", "head"), ("downgrade", "base"), ("upgrade", "head")]:
        subprocess.run(
            [sys.executable, "-m", "alembic", "-c", str(backend / "alembic.ini"), direction, target],
            cwd=backend,
            env=env,
            check=True,
            capture_output=True,
        )
    engine = create_engine(url)
    assert {"accounts", "roles", "conversations", "messages", "ai_runs"} <= set(
        inspect(engine).get_table_names()
    )
    assert inspect(engine).get_foreign_keys("messages")
    engine.dispose()


# test_upgrade_r1_existing_data_is_preserved：验证：旧版本数据库升级后已有记录不丢失；范围以本函数断言为准。
def test_upgrade_r1_existing_data_is_preserved(tmp_path):
    import sqlite3

    backend = Path(__file__).resolve().parents[1]
    path = tmp_path / "existing.db"
    env = {**os.environ, "HAI_DATABASE_URL": "sqlite:///" + path.as_posix()}

    # migrate：对独立临时数据库执行指定 Alembic 版本迁移。
    def migrate(target):
        subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", target],
            cwd=backend,
            env=env,
            check=True,
            capture_output=True,
        )

    migrate("0001_r1")
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO accounts VALUES ('demo','password','Demo','',1,100000,0,0,'a',1)")
        db.execute(
            "INSERT INTO ai_runs VALUES ('c','a','key','role',1,'SUCCEEDED','',100,0,0,'',1,'old-run',1)"
        )
    migrate("head")
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT username FROM accounts").fetchone()[0] == "demo"
        assert db.execute("SELECT purpose,context,result FROM ai_runs").fetchone() == ("CHAT", "{}", "{}")
        assert db.execute("SELECT started_at,finished_at,first_token_at FROM ai_runs").fetchone() == (
            None,
            None,
            None,
        )
