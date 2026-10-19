# 实现结构

浏览器通过 Vite 开发代理或 Nginx 连接 FastAPI。账号路由负责认证，角色与聊天路由检查权限，并将持久业务状态写入 SQLAlchemy 管理的数据库。

容器环境由 Celery / Redis 调度后台工作；数据库中的运行记录承担持久状态。Worker 通过 Agent、模型网关和工具网关执行任务。模型供应商与付款方选择受服务端策略约束，不能由模型自行选择他人 Key。

`chat_memory.py` / `context_capacity.py` 负责历史上下文与容量控制；`rag.py` / `retrieval_rank.py` 负责检索；`learning.py` 负责授权后的角色设定学习。`tool_gateway.py` / `mcp_server.py` 管理工具调用。

API Key 加密材料由部署者首次生成。数据库、私有媒体、聊天记忆和密钥必须独立备份与保护，不能加入源码仓库。

源码阅读入口：`backend/app/main.py` → `routes_auth.py` → `routes_chat.py` → `worker.py` → `agents.py` → `gateways.py`。测试说明可参考 `backend/tests/conftest.py`。
