# 文件职责：优雅停止项目容器并保留数据卷；不是卸载或清空数据库。
. "$PSScriptRoot\common.ps1"
Assert-Docker
Invoke-Compose stop -t 120
Write-Host 'Services stopped. Named volumes are retained.'
