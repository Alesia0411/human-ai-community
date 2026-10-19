# 文件职责：部署脚本公共路径与 Docker 命令封装；固定 human-ai 项目，命令失败立即终止。
$ErrorActionPreference = 'Stop'
# 从脚本自身位置确定工作区，不依赖调用者当前目录。
$Workspace = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$ComposeFile = Join-Path $Workspace 'deploy\compose.win11.yml'
$EnvFile = Join-Path $Workspace 'deploy\.env'
# Docker Desktop supports both per-user and machine-wide installations.
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    $DockerCandidates = @(
        (Join-Path $env:LOCALAPPDATA 'Programs\DockerDesktop\resources\bin'),
        (Join-Path $env:ProgramFiles 'Docker\Docker\resources\bin')
    )
    foreach ($DockerDirectory in $DockerCandidates) {
        if (Test-Path (Join-Path $DockerDirectory 'docker.exe')) {
            $env:PATH = "$DockerDirectory;$env:PATH"
            break
        }
    }
}
# Invoke-Compose：为全部 Compose 调用固定项目、环境文件与编排文件；检查外部程序退出码并抛出失败。
function Invoke-Compose {
    & docker compose -p human-ai-community --env-file $EnvFile -f $ComposeFile @args
    if ($LASTEXITCODE -ne 0) { throw "Docker Compose failed ($LASTEXITCODE)." }
}
# Assert-Docker：要求 Docker 引擎已启动且处于 Linux 容器模式。
function Assert-Docker {
    $osType = & docker info --format '{{.OSType}}'
    if ($LASTEXITCODE -ne 0 -or $osType -ne 'linux') { throw 'Start Docker Desktop in Linux containers / WSL 2 mode first.' }
}
