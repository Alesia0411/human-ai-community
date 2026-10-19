# 文件职责：查询容器和 HTTPS 就绪接口；不绕过证书校验，不代表真实模型与手机端已验证。
param([string]$BaseUrl = 'https://localhost:9443')
. "$PSScriptRoot\common.ps1"
Assert-Docker
Invoke-Compose ps
Invoke-RestMethod -Uri "$BaseUrl/api/v1/health/ready"
Write-Host 'TLS verification was not bypassed. Mobile/LAN and real-provider checks are manual.'
