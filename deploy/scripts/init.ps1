# 文件职责：首次生成部署环境变量与随机应用密钥；已有配置时拒绝覆盖，避免旧加密凭证无法解密。
. "$PSScriptRoot\common.ps1"
# New-RandomHex：用密码学随机数生成 32 字节值并转为十六进制，用于首次初始化凭证。
function New-RandomHex {
    $bytes = New-Object byte[] 32
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    return ([BitConverter]::ToString($bytes)).Replace('-', '').ToLowerInvariant()
}
# 环境文件与 app.key 不能覆盖：已有数据库中的加密 Key 依赖原应用密钥。
if (Test-Path -LiteralPath $EnvFile) { throw 'deploy/.env already exists. Existing credentials were not changed.' }
$SecretDir = Join-Path $Workspace 'deploy\secrets'
$CertDir = Join-Path $Workspace 'deploy\certs'
[void](New-Item -ItemType Directory -Force -Path $SecretDir,$CertDir)
$SecretFile = Join-Path $SecretDir 'app.key'
if (Test-Path -LiteralPath $SecretFile) { throw 'app.key exists; keep the original key for existing encrypted API keys.' }
$utf8 = New-Object System.Text.UTF8Encoding($false)
[IO.File]::WriteAllText($SecretFile, (New-RandomHex), $utf8)
$lines = @(
    "MYSQL_PASSWORD=$(New-RandomHex)",
    "MYSQL_ROOT_PASSWORD=$(New-RandomHex)",
    "HAI_INVITE_CODE=$(New-RandomHex)",
    'HAI_DEMO_MODE=false', 'HAI_BIND=127.0.0.1', 'HAI_HTTPS_PORT=9443',
    'HAI_ALLOWED_ORIGINS=https://localhost:9443'
)
[IO.File]::WriteAllLines($EnvFile, $lines, $utf8)
Write-Host 'Created private configuration. Find your invite code in deploy/.env.'
Write-Host 'Supply trusted TLS certs as deploy/certs/server.crt and server.key before start.ps1.'
