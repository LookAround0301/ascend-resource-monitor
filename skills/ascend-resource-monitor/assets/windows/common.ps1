$ErrorActionPreference = 'Stop'

function Read-JsonFile([string]$Path) {
    Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json
}

function Write-JsonFile($Value, [string]$Path) {
    $Value | ConvertTo-Json -Depth 16 | Set-Content -LiteralPath $Path -Encoding UTF8
}

function Assert-Deployment($Config) {
    if ($Config.id -cnotmatch '^[a-z][a-z0-9-]{0,27}$') { throw 'Invalid deployment ID.' }
    if ($Config.executor_id -notmatch '^[A-Za-z0-9_][A-Za-z0-9_.-]{0,39}$' -or $Config.executor_id -in @('.', '..')) { throw 'Invalid executor ID.' }
    if ($Config.storage_root -notmatch '^/[A-Za-z0-9_./-]+$' -or $Config.storage_root -eq '/' -or ($Config.storage_root -split '/') -contains '..') { throw 'Invalid data directory.' }
    if (@($Config.nodes).Count -lt 1 -or @($Config.nodes).Count -gt 32) { throw 'Provide 1 to 32 nodes.' }
    foreach ($node in $Config.nodes) {
        if ($node.name -notmatch '^[A-Za-z0-9_][A-Za-z0-9_.-]{0,39}$') { throw 'Invalid node name.' }
        if ($node.host -notmatch '^[A-Za-z0-9][A-Za-z0-9.-]{0,252}$') { throw 'Invalid SSH host.' }
        if ($node.ssh_user -notmatch '^[A-Za-z0-9_][A-Za-z0-9_.-]{0,39}$') { throw 'Invalid SSH user.' }
        $ip = $null
        if (-not [Net.IPAddress]::TryParse($node.monitor_ip, [ref]$ip) -or $ip.AddressFamily -ne 'InterNetwork') { throw 'Monitoring address must be IPv4.' }
        foreach ($port in @($node.ssh_port, $node.node_port, $node.npu_port, $node.process_port)) {
            if ([int]$port -lt 1 -or [int]$port -gt 65535) { throw 'Invalid port.' }
        }
    }
    if ($Config.central_node -notin @($Config.nodes.name)) { throw 'Select a central node.' }
    foreach ($port in @($Config.grafana_port, $Config.prometheus_port, $Config.local_port)) {
        if ([int]$port -lt 1024 -or [int]$port -gt 65535) { throw 'Invalid panel port.' }
    }
}

function Assert-Client($Config) {
    if ($Config.id -cnotmatch '^[a-z][a-z0-9-]{0,27}$' -or $Config.dashboard_uid -cnotmatch '^[a-z][a-z0-9-]{0,39}$') { throw 'Invalid dashboard ID.' }
    if ($Config.host -notmatch '^[A-Za-z0-9][A-Za-z0-9.-]{0,252}$' -or $Config.ssh_user -notmatch '^[A-Za-z0-9_][A-Za-z0-9_.-]{0,39}$') { throw 'Invalid SSH target.' }
    foreach ($port in @($Config.ssh_port, $Config.remote_port, $Config.local_port)) {
        if ([int]$port -lt 1 -or [int]$port -gt 65535) { throw 'Invalid client port.' }
    }
}

function Get-SshOptions($Node, [string]$IdentityFile, [switch]$Scp) {
    $result = @('-o', 'ConnectTimeout=15', '-o', 'StrictHostKeyChecking=accept-new', '-o', 'ServerAliveInterval=30', '-o', 'ServerAliveCountMax=3')
    if ($Scp) { $result += @('-P', [string]$Node.ssh_port) } else { $result += @('-p', [string]$Node.ssh_port) }
    if ($IdentityFile) {
        if (-not (Test-Path -LiteralPath $IdentityFile -PathType Leaf)) { throw 'Selected identity file does not exist.' }
        $result += @('-i', $IdentityFile)
    }
    return $result
}

function New-ClientPackage($Config, [string]$Destination, [string]$Templates) {
    Assert-Deployment $Config
    $node = @($Config.nodes | Where-Object name -eq $Config.central_node)[0]
    $client = [ordered]@{ id=$Config.id; title=$Config.title; host=$node.host; ssh_user=$node.ssh_user; ssh_port=[int]$node.ssh_port; remote_port=[int]$Config.grafana_port; local_port=[int]$Config.local_port; dashboard_uid=$Config.id; anonymous_viewer=[bool]$Config.anonymous_viewer }
    $folder = Join-Path $Destination ('client-' + $Config.id)
    New-Item -ItemType Directory -Force -Path $folder | Out-Null
    foreach ($name in @('common.ps1','open-monitor.ps1','install-client.ps1','Install.cmd','Open-Monitor.cmd')) {
        Copy-Item -LiteralPath (Join-Path $Templates $name) -Destination $folder -Force
    }
    Write-JsonFile $client (Join-Path $folder 'client.json')
    $zip = Join-Path $Destination ('client-' + $Config.id + '.zip')
    Compress-Archive -Path (Join-Path $folder '*') -DestinationPath $zip -Force
    return $zip
}
