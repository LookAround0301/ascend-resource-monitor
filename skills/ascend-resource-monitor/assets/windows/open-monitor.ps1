param([string]$ConfigPath = (Join-Path $PSScriptRoot 'client.json'), [string]$IdentityFile, [switch]$NoBrowser, [switch]$ValidateOnly)
. (Join-Path $PSScriptRoot 'common.ps1')
try {
    $config = Read-JsonFile $ConfigPath
    Assert-Client $config
    if ($ValidateOnly) { Write-Output 'Client configuration valid'; exit 0 }
    $ssh = Get-Command ssh.exe -ErrorAction Stop
    $url = "http://127.0.0.1:$($config.local_port)/d/$($config.dashboard_uid)"
    $api = "http://127.0.0.1:$($config.local_port)/api/dashboards/uid/$($config.dashboard_uid)"
    function Test-Dashboard([string]$Api, [string]$Uid) {
        $response = $null; $reader = $null
        try {
            $request = [Net.HttpWebRequest]::Create($Api); $request.Proxy = $null; $request.Timeout = 1500; $request.ReadWriteTimeout = 1500
            $response = $request.GetResponse(); $reader = [IO.StreamReader]::new($response.GetResponseStream())
            $value = $reader.ReadToEnd() | ConvertFrom-Json
            return $value.dashboard.uid -eq $Uid
        } catch { return $false } finally { if ($reader) { $reader.Dispose() }; if ($response) { $response.Dispose() } }
    }
    if ((Test-Dashboard $api $config.dashboard_uid)) {
        Write-Host "Dashboard already reachable: $url"
        if (-not $NoBrowser) { Start-Process $url }
        exit 0
    }
    $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, [int]$config.local_port)
    try { $listener.Start() } catch { throw 'The local port is in use by an unverified service. Choose another local_port in client.json.' } finally { $listener.Stop() }
    Write-Host 'Connect your VPN first. SSH uses this PC''s key or asks for a password in this window.'
    Write-Host "Dashboard: $url"
    Write-Host 'Keep this window open. Ctrl+C closes the tunnel.'
    $job = $null
    if (-not $NoBrowser) {
        $job = Start-Job -ArgumentList $api, $url, $config.dashboard_uid, ([bool]$config.anonymous_viewer) -ScriptBlock {
            param($Api, $Url, $Uid, $Anonymous)
            for ($i = 0; $i -lt 90; $i++) {
                $response = $null; $reader = $null
                try {
                    $request = [Net.HttpWebRequest]::Create($Api); $request.Proxy=$null; $request.Timeout=1500; $request.ReadWriteTimeout=1500
                    $response=$request.GetResponse(); $reader=[IO.StreamReader]::new($response.GetResponseStream())
                    $value=$reader.ReadToEnd() | ConvertFrom-Json
                    if ($value.dashboard.uid -eq $Uid) { Start-Process $Url; return }
                } catch {
                    if (-not $Anonymous -and $_.Exception.Response -and [int]$_.Exception.Response.StatusCode -eq 401) { Start-Process $Url; return }
                } finally { if ($reader) {$reader.Dispose()}; if ($response) {$response.Dispose()} }
                Start-Sleep -Seconds 1
            }
        }
    }
    $options = @(Get-SshOptions $config $IdentityFile)
    $options += @('-N','-T','-o','ExitOnForwardFailure=yes','-L',"127.0.0.1:$($config.local_port):127.0.0.1:$($config.remote_port)","$($config.ssh_user)@$($config.host)")
    try { & $ssh.Source @options; $code=$LASTEXITCODE }
    finally { if ($job) { Stop-Job $job -ErrorAction SilentlyContinue; Remove-Job $job -Force -ErrorAction SilentlyContinue } }
    exit $code
} catch { Write-Host $_.Exception.Message -ForegroundColor Red; exit 1 }
