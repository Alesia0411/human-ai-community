# 文件职责：仅向空数据库导入备份，随后撤销旧授权并重建向量；恢复后保持业务停止等待审核。
param([Parameter(Mandatory=$true)][string]$Source)
. "$PSScriptRoot\common.ps1"
Assert-Docker
$InputDir = (Resolve-Path -LiteralPath $Source).Path
foreach ($name in 'database.sql','media.tar.gz') {
    if (-not (Test-Path -LiteralPath (Join-Path $InputDir $name))) { throw "Missing backup file $name" }
}
$Manifest = Join-Path $InputDir 'checksums.json'
if (-not (Test-Path -LiteralPath $Manifest)) { throw 'Missing checksums.json; verify the backup before restoring.' }
$Entries = @(Get-Content -LiteralPath $Manifest -Raw | ConvertFrom-Json)
foreach ($name in 'database.sql','media.tar.gz','checkpoints.tar.gz') {
    $File = Join-Path $InputDir $name
    if (-not (Test-Path -LiteralPath $File)) { continue }
    $Match = @($Entries | Where-Object { [IO.Path]::GetFileName($_.Path) -eq $name })
    if ($Match.Count -ne 1 -or (Get-FileHash -LiteralPath $File).Hash -ne $Match[0].Hash) {
        throw "Backup checksum mismatch: $name. No services stopped."
    }
}
Invoke-Compose stop -t 120 nginx beat agent-worker api
Invoke-Compose up -d --wait mysql redis
$tables = & docker compose -p human-ai-community --env-file $EnvFile -f $ComposeFile exec -T mysql sh -c 'MYSQL_PWD="$MYSQL_PASSWORD" mysql -u hai -N -e "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=\"human_ai\""'
if ($LASTEXITCODE -ne 0 -or [int]$tables -ne 0) { throw 'Restore only supports an empty database. Existing data was not overwritten. Services remain stopped.' }
Invoke-Compose cp (Join-Path $InputDir 'database.sql') mysql:/tmp/hai-restore.sql
Invoke-Compose exec -T mysql sh -c 'MYSQL_PWD="$MYSQL_PASSWORD" mysql -u hai human_ai < /tmp/hai-restore.sql'
Invoke-Compose run --rm --no-deps --user root -v "${InputDir}:/backup:ro" api tar -xzf /backup/media.tar.gz -C /data/media
if (Test-Path -LiteralPath (Join-Path $InputDir 'checkpoints.tar.gz')) {
    Invoke-Compose run --rm --no-deps --user root -v "${InputDir}:/backup:ro" api tar -xzf /backup/checkpoints.tar.gz -C /data/checkpoints
}
Invoke-Compose up -d qdrant
Invoke-Compose run --rm --no-deps api alembic upgrade head
# 旧备份不知道之后发生的撤权；先关闭全部旧授权，不能直接恢复在线服务。
Invoke-Compose run --rm --no-deps api python -m app.restore_guard --apply-to-isolated-restore
Invoke-Compose run --rm --no-deps api python -m app.rebuild_vectors
Write-Host 'Restored offline with all roles private, AI grants disabled, credentials cleared and pending runs cancelled/unknown. Review deleted historical content before start.ps1; owners must explicitly reauthorize and re-upload knowledge. Never treat an old backup as the latest consent record.'
