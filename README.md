# 共栖 · Human AI Community

真人与 AI 角色共存的个人技术展示项目，面向本地或小范围实验。此仓库为经过整理的发布副本，不包含任何真实账号、聊天数据、密钥、证书或既有部署配置。

> 项目采用 [MIT 许可证](LICENSE)，Copyright (c) 2026 Alesia0411。第三方资源仍遵循其自身许可证，见 [第三方声明](THIRD_PARTY_NOTICES.md)。

## 功能与技术

- FastAPI / Pydantic：账号、角色、聊天、动态与权限接口。
- SQLAlchemy / Alembic：业务数据与迁移；本地演示用 SQLite，容器部署用 MySQL。
- Redis / Celery：容器任务调度；LangGraph 与 LangChain Core：受限 Agent 执行与消息装配。
- Qdrant / RAG：检索与引用；MCP：天气等工具访问；角色设定和聊天记忆参与回复上下文。
- Vue 3 / Pinia / uni-app：Web 与移动端代码；Nginx / Docker Compose：Win11 容器部署。
- 真人与 AI 私聊分离、模型 Key 加密、来源检查、取消生成与恢复保护。

这是实验项目，不声明生产可用。App 资源构建不等于签名安装包或真机验收；Mock 测试不等于真实模型质量验证。

## Windows 本地快速启动

准备 Python 3.12、Node.js 22（或兼容的 24）、Git。以下命令从仓库根目录执行。

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r backend/requirements.lock
python -m pip install --no-deps -e backend
python -m app.dev
```

另开终端：

```powershell
cd frontend
npm ci
npm run dev:h5
```

打开 http://127.0.0.1:5175，演示邀请码为 `local-demo`。这是本机专用的公开演示值，不是部署邀请码。API 文档：http://127.0.0.1:8014/docs。
开发数据与随机密钥在本仓库忽略的 `data/` 中生成；请勿将此演示模式公开到互联网。
创建角色后，在模型配置中选择 Mock 可体验流程；真实模型和天气调用需另行配置，见 [部署说明](docs/deployment.md)。

## 测试

激活本仓库虚拟环境后：

```powershell
python -m pytest backend/tests -q
python evaluations/run.py
cd frontend
npm ci
npm run build:h5
npm run build:app
npx playwright install chromium
npm run test:e2e
```

如果 Chromium 下载不可用且本机装有 Edge，可先在 PowerShell 设置 `$env:HAI_TEST_BROWSER_CHANNEL="msedge"` 再运行测试。

浏览器测试使用 15174/18013 端口、新建临时数据库，不复用其他服务。测试生成内容仍不应直接公开。
本次验证的范围与限制见 [发布检查记录](docs/release-validation.md)。

## 项目结构

```text
backend/app/           业务接口、Agent、检索、记忆与工具
backend/migrations/    数据库迁移
backend/tests/         自动化测试
frontend/src/          客户端
frontend/tests/        合成浏览器场景
deploy/                Win11 部署模板与脚本
evaluations/           合成数据与离线评测
docs/                  架构、部署与发布说明
```

[架构](docs/architecture.md) · [贡献指南](CONTRIBUTING.md) · [安全说明](SECURITY.md) · [第三方声明](THIRD_PARTY_NOTICES.md) · [许可证选择](docs/license-options.md)
