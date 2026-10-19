# 文件职责：登录后等待 Linux Docker 就绪，用已有镜像恢复固定服务，不重新构建或另建角色进程。
. "$PSScriptRoot\common.ps1"
# Login recovery uses already-built images and the fixed Compose project.
# Docker Desktop must independently be configured to start on user login.
# 最多等待约两分钟；未就绪就退出，不在后台无限循环或创建新环境。
$Ready = $false
for ($Attempt = 0; $Attempt -lt 12; $Attempt++) {
    if (Get-Command docker -ErrorAction SilentlyContinue) {
        $Kind = & docker info --format '{{.OSType}}' 2>$null
        if ($LASTEXITCODE -eq 0 -and $Kind -eq 'linux') { $Ready = $true; break }
    }
    Start-Sleep -Seconds 10
}
if (-not $Ready) { throw 'Docker Linux engine is not ready; no services were launched.' }
Invoke-Compose up -d --wait --no-build
Invoke-Compose exec -T nginx nginx -s reload
