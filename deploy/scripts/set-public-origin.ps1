# 文件职责：验证公网 HTTPS 来源后更新允许来源，按参数决定是否重启相关服务。
param(
    [Parameter(Mandatory = $true)][string]$PublicUrl,
    [switch]$SkipRestart
)
. "$PSScriptRoot\common.ps1"

# 只接受不含路径、查询、片段或用户信息的 HTTPS 来源，避免错误放宽写请求来源校验。
$PublicUri = $null
if (-not [Uri]::TryCreate($PublicUrl, [UriKind]::Absolute, [ref]$PublicUri) -or
    $PublicUri.Scheme -ne 'https' -or $PublicUri.UserInfo -or
    $PublicUri.Query -or $PublicUri.Fragment -or $PublicUri.AbsolutePath -ne '/' -or
    $PublicUri.Host -notmatch '^[a-zA-Z0-9.-]+$') {
    throw 'Supply only the HTTPS site origin, for example https://your-site.cpolar.top (no path or credentials).'
}
$Origin = $PublicUri.GetLeftPart([UriPartial]::Authority)
$StateFile = Join-Path $Workspace 'data\public-origin.txt'
$PreviousOrigin = if (Test-Path $StateFile) { [IO.File]::ReadAllText($StateFile).Trim() } else { '' }
$EnvText = [IO.File]::ReadAllText($EnvFile)
$Match = [regex]::Match($EnvText, '(?m)^HAI_ALLOWED_ORIGINS=([^\r\n]*)')
$Existing = if ($Match.Success) { $Match.Groups[1].Value.Trim().Trim('"').Trim("'") -split ',' } else { @() }
$Origins = @($Existing | ForEach-Object { $_.Trim() } | Where-Object { $_ -and $_ -ne $PreviousOrigin })
$Origins = @($Origins + 'https://localhost:9443' + 'https://127.0.0.1:9443' + $Origin | Select-Object -Unique)
$NewLine = 'HAI_ALLOWED_ORIGINS=' + ($Origins -join ',')
if ($Match.Success) {
    $EnvText = $EnvText.Substring(0, $Match.Index) + $NewLine + $EnvText.Substring($Match.Index + $Match.Length)
} else {
    $EnvText = $EnvText.TrimEnd() + "`r`n" + $NewLine + "`r`n"
}
[IO.File]::WriteAllText($EnvFile, $EnvText, (New-Object Text.UTF8Encoding($false)))
New-Item -ItemType Directory -Force (Split-Path $StateFile) | Out-Null
[IO.File]::WriteAllText($StateFile, $Origin, (New-Object Text.UTF8Encoding($false)))
if (-not $SkipRestart) {
    Assert-Docker
    Invoke-Compose up -d --wait --no-build api agent-worker beat
    Invoke-Compose exec -T nginx nginx -s reload
}
Write-Output "Public origin: $Origin"
