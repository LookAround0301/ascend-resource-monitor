param([string]$ConfigPath=(Join-Path $PSScriptRoot 'client.json'), [switch]$ValidateOnly)
. (Join-Path $PSScriptRoot 'common.ps1')
try {
    $config=Read-JsonFile $ConfigPath
    if (-not $config.shared_url) { throw 'No shared HTTPS entry is configured. Use Open-Monitor.cmd.' }
    Assert-SharedUrl ([string]$config.shared_url)
    if ($ValidateOnly) { Write-Output 'Shared HTTPS entry valid'; exit 0 }
    Write-Host 'Optional browser entry. If blocked by proxy, port or certificate policy, use Open-Monitor.cmd.'
    Start-Process -FilePath ([string]$config.shared_url)
} catch { Write-Host $_.Exception.Message -ForegroundColor Red; exit 1 }
