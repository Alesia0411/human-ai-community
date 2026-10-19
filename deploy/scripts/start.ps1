# 文件职责：校验 Docker 和证书，构建镜像、启动依赖、执行迁移后启动应用并重载 Nginx。
. "$PSScriptRoot\common.ps1"
Assert-Docker
foreach ($name in 'server.crt','server.key') {
    if (-not (Test-Path -LiteralPath (Join-Path $Workspace "deploy\certs\$name"))) { throw "Missing TLS certificate: $name" }
}
Invoke-Compose config --quiet
Invoke-Compose build
Invoke-Compose up -d --wait mysql redis qdrant
Invoke-Compose run --rm api alembic upgrade head
Invoke-Compose up -d --wait
# Nginx resolves its static upstream on startup; API recreation may change its IP.
Invoke-Compose exec -T nginx nginx -s reload
Invoke-Compose ps
