# 文件职责：短暂停止写服务后导出数据库与文件归档，生成校验清单；不会删除数据卷。
param([Parameter(Mandatory=$true)][string]$Destination)
. "$PSScriptRoot\common.ps1"
Assert-Docker
$Target = [IO.Path]::GetFullPath($Destination)
if (Test-Path -LiteralPath $Target) { throw 'Choose a new, empty backup directory.' }
[void](New-Item -ItemType Directory -Path $Target)
# A short write pause keeps MySQL and media aligned. No volume deletion is performed.
# 暂停写入窗口，尽量使数据库、媒体和任务检查点处于一致时点。
Invoke-Compose stop -t 120 nginx beat agent-worker api
try {
    Invoke-Compose exec -T mysql sh -c 'MYSQL_PWD="$MYSQL_PASSWORD" mysqldump -u hai --single-transaction --no-tablespaces human_ai > /tmp/hai-backup.sql'
    Invoke-Compose cp mysql:/tmp/hai-backup.sql (Join-Path $Target 'database.sql')
    Invoke-Compose run --rm --no-deps --user root -v "${Target}:/backup" api tar -czf /backup/media.tar.gz -C /data/media .
    Invoke-Compose run --rm --no-deps --user root -v "${Target}:/backup" api tar -czf /backup/checkpoints.tar.gz -C /data/checkpoints .
    Invoke-Compose run --rm --no-deps --user root -v "${Target}:/backup" api tar -czf /backup/chat-records.tar.gz -C /data/chat-records .
    [IO.File]::WriteAllText((Join-Path $Target 'README.txt'), 'R2 backup. Qdrant is rebuilt from authorized SQL chunks. Keep app.key separately. Restore into an isolated empty environment first.')
    Get-ChildItem -LiteralPath $Target -File | Get-FileHash | Select-Object Path,Hash | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $Target 'checksums.json')
} finally { Invoke-Compose up -d }
