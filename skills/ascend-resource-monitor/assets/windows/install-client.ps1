param([switch]$NoLaunch, [switch]$NoShortcut, [string]$DestinationRoot, [switch]$ValidateOnly)
. (Join-Path $PSScriptRoot 'common.ps1')
try {
    $config = Read-JsonFile (Join-Path $PSScriptRoot 'client.json')
    Assert-Client $config
    if ($ValidateOnly) { Write-Output 'Installer configuration valid'; exit 0 }
    Get-Command ssh.exe -ErrorAction Stop | Out-Null
    if (-not $DestinationRoot) { $DestinationRoot=Join-Path $env:LOCALAPPDATA 'AscendMonitor' }
    $destination=Join-Path $DestinationRoot $config.id
    New-Item -ItemType Directory -Force -Path $destination | Out-Null
    foreach ($name in @('common.ps1','client.json','open-monitor.ps1','Open-Monitor.cmd')) {
        $source=Join-Path $PSScriptRoot $name; $target=Join-Path $destination $name
        if ([IO.Path]::GetFullPath($source) -ne [IO.Path]::GetFullPath($target)) { Copy-Item -LiteralPath $source -Destination $target -Force }
    }
    foreach ($name in @('open-web.ps1','Open-Web.cmd','使用说明.txt')) {
        $source=Join-Path $PSScriptRoot $name; $target=Join-Path $destination $name
        if ((Test-Path -LiteralPath $source) -and [IO.Path]::GetFullPath($source) -ne [IO.Path]::GetFullPath($target)) { Copy-Item -LiteralPath $source -Destination $target -Force }
    }
    if (-not $NoShortcut) {
        $shell = New-Object -ComObject WScript.Shell
        $shortcut=$shell.CreateShortcut((Join-Path ([Environment]::GetFolderPath('Desktop')) ('Ascend Monitor - '+$config.id+'.lnk')))
        $shortcut.TargetPath=Join-Path $destination 'Open-Monitor.cmd'
        $shortcut.WorkingDirectory=$destination; $shortcut.Description=$config.title; $shortcut.Save()
    }
    Write-Host "Installed: $destination"
    if (-not $NoLaunch) { Start-Process -FilePath (Join-Path $destination 'Open-Monitor.cmd') -WorkingDirectory $destination }
} catch { Write-Host $_.Exception.Message -ForegroundColor Red; exit 1 }
