# Windows 11 部署

准备 WSL 2、Docker Desktop（Linux containers）。在副本根目录执行：

```powershell
.\deploy\scripts\init.ps1
.\.venv\Scripts\python.exe deploy/scripts/init-local-tls.py
.\deploy\scripts\start.ps1
```

`init.ps1` 为每次新部署生成随机数据库密码、邀请码和应用密钥，拒绝覆盖既有配置。请私下读取本仓库 `deploy/.env` 中的邀请码。
本地证书生成脚本不会安装系统信任；需由使用者自行信任 `deploy/certs/local-ca.crt`，或提供适用的受信任证书。不要关闭 TLS 验证。

默认 HTTPS 地址：https://localhost:9443。Compose 项目为 `human-ai-community`，不操作 `human-ai` 容器组；默认只监听回环地址。

## 模型与工具

可在用户界面添加模型连接，API Key 由各使用者自行提供。供应商端点由管理员的 `HAI_PROVIDERS` 配置决定；请参考 `backend/app/config.py` 支持的配置。Ollama 是可选组件，模型权重不随源码分发。
工具行为由 `backend/app/tool_gateway.py` / `mcp_server.py` 实现，并受调用权限和预算控制。真实网络工具与模型调用可能产生费用，不是离线测试的一部分。

## 可选公网隧道

由部署者自行安装、认证 cpolar，并参考 `deploy/cpolar.example.yml` 指向 https://localhost:9443。不要提交真实 cpolar 配置或令牌。
获得实际公网 HTTPS Origin 后执行：

```powershell
.\deploy\scripts\set-public-origin.ps1 -PublicUrl 'https://your-domain.example'
```

此命令更新白名单并重启相关服务。示例域名不可直接使用。未包含自动同步或系统启动任务。

备份与恢复脚本仅操作此 Compose 项目；恢复前阅读脚本，使用空白隔离环境。密钥与数据库必须匹配，且均不得提交 Git。
