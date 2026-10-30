param([string]$ConfigPath, [string]$IdentityFile, [string]$PackageDirectory, [switch]$CheckOnly, [switch]$NoPause, [switch]$FunctionsOnly)
. (Join-Path $PSScriptRoot 'common.ps1')

function Invoke-Remote($Node, [string]$Command) {
    $argsList=@(Get-SshOptions $Node $IdentityFile) + @('-T',"$($Node.ssh_user)@$($Node.host)",$Command)
    $result = & ssh.exe @argsList
    if ($LASTEXITCODE -ne 0) { throw "SSH operation failed on $($Node.name). Check the output and receipt before retrying." }
    return ($result -join "`n")
}
function Send-Files($Node, [string[]]$Files, [string]$RemoteDirectory) {
    $argsList=@(Get-SshOptions $Node $IdentityFile -Scp) + $Files + @("$($Node.ssh_user)@$($Node.host):$RemoteDirectory/")
    & scp.exe @argsList
    if ($LASTEXITCODE -ne 0) { throw "SCP upload failed on $($Node.name)." }
}
function Download-File([string]$Url, [string]$Path, [string]$Token) {
    $curl=Get-Command curl.exe -ErrorAction SilentlyContinue
    if ($curl) {
        # Feed the token through stdin, never through process arguments or logs.
        $curlConfig=''
        if ($Token) {
            if ($Token -match '[\r\n"\\]') { throw 'Invalid registry token encoding.' }
            $curlConfig='header = "Authorization: Bearer '+$Token+'"'
        }
        $curlConfig | & $curl.Source --config - --fail --location --silent --show-error --retry 2 --connect-timeout 20 --max-time 900 --ssl-revoke-best-effort --output $Path $Url
        if ($LASTEXITCODE -ne 0) { throw "Download failed (curl $LASTEXITCODE). Use an offline package folder if the network blocks this source." }
        return
    }
    $request=[Net.HttpWebRequest]::Create($Url)
    $request.Timeout=120000; $request.ReadWriteTimeout=120000; $request.UserAgent='AscendMonitorSetup/1'
    if ($Token) { $request.Headers['Authorization']='Bearer '+$Token }
    $response=$null; $inputStream=$null; $outputStream=$null
    try {
        $response=$request.GetResponse(); $inputStream=$response.GetResponseStream()
        $outputStream=[IO.File]::Open($Path,[IO.FileMode]::Create,[IO.FileAccess]::Write,[IO.FileShare]::None)
        $buffer=New-Object byte[] (1024*1024); $total=0L; $last=0L
        while (($count=$inputStream.Read($buffer,0,$buffer.Length)) -gt 0) {
            $outputStream.Write($buffer,0,$count); $total+=$count
            if ($total-$last -ge 32MB) { Write-Host ('Downloaded {0:N0} MiB' -f ($total/1MB)); $last=$total }
        }
    } finally { if ($outputStream) {$outputStream.Dispose()}; if ($inputStream) {$inputStream.Dispose()}; if ($response) {$response.Dispose()} }
}
function Get-Package($Item, [string]$Cache) {
    [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12
    $proxyAddress=if ($env:HTTPS_PROXY) {$env:HTTPS_PROXY} elseif ($env:HTTP_PROXY) {$env:HTTP_PROXY} else {$null}
    if ($proxyAddress) { [Net.WebRequest]::DefaultWebProxy=[Net.WebProxy]::new($proxyAddress) }
    if ($Item.name -notmatch '^[A-Za-z0-9._-]+$' -or $Item.sha256 -cnotmatch '^[0-9a-f]{64}$') { throw 'Invalid package manifest.' }
    if ($Item.url -notmatch '^https://') { throw 'Package downloads must use HTTPS.' }
    if ($PackageDirectory) {
        $candidate=Join-Path $PackageDirectory $Item.name
        if (Test-Path -LiteralPath $candidate) {
            if ((Get-FileHash -LiteralPath $candidate -Algorithm SHA256).Hash.ToLowerInvariant() -ne $Item.sha256) { throw "Offline package checksum mismatch: $($Item.name)" }
            return $candidate
        }
    }
    $path=Join-Path $Cache $Item.name
    if (Test-Path -LiteralPath $path) {
        if ((Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant() -eq $Item.sha256) { return $path }
        throw "Cached file has wrong checksum: $path. Remove this file after checking it."
    }
    Write-Host "Downloading $($Item.name)"
    $token=$null
    if ($Item.registry_auth) {
        $challengeResponse=$null
        try {
            $challengeRequest=[Net.HttpWebRequest]::Create($Item.url)
            $challengeRequest.Method='GET'; $challengeRequest.Timeout=30000
            $challengeResponse=$challengeRequest.GetResponse()
        }
        catch {
            $cause=$_.Exception.GetBaseException()
            if (-not $cause.Response) { throw }
            $challengeResponse=$cause.Response
            $challenge=$challengeResponse.Headers['WWW-Authenticate']
            if (-not $challenge) { throw }
            $realm=[regex]::Match($challenge,'realm="([^"]+)"').Groups[1].Value
            $service=[regex]::Match($challenge,'service="([^"]+)"').Groups[1].Value
            $scope=[regex]::Match($challenge,'scope="([^"]+)"').Groups[1].Value
            $realmUri=[Uri]$realm
            if ($realmUri.Scheme -ne 'https' -or $realmUri.Host -notmatch '(^|\.)(myhuaweicloud\.com|huaweicloud\.com)$') { throw 'Unexpected registry authentication endpoint.' }
            $reply=Invoke-RestMethod -Uri ($realm+'?service='+[Uri]::EscapeDataString($service)+'&scope='+[Uri]::EscapeDataString($scope))
            $token=$reply.token
            if (-not $token) { $token=$reply.access_token }
            if (-not $token) { throw 'Registry did not return a token.' }
        }
        finally { if ($challengeResponse) { $challengeResponse.Close() } }
    }
    $partial=$path+'.partial-'+[Guid]::NewGuid().ToString('N')
    try {
        Download-File $Item.url $partial $token
        if ((Get-FileHash -LiteralPath $partial -Algorithm SHA256).Hash.ToLowerInvariant() -ne $Item.sha256) { throw 'Downloaded checksum mismatch.' }
        Move-Item -LiteralPath $partial -Destination $path
    } finally { $token=$null; if (Test-Path -LiteralPath $partial) { Remove-Item -LiteralPath $partial } }
    return $path
}

if ($FunctionsOnly) { return }
try {
    [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12
    Get-Command ssh.exe, scp.exe -ErrorAction Stop | Out-Null
    $config=Read-JsonFile $ConfigPath; Assert-Deployment $config
    $software=Read-JsonFile (Join-Path $PSScriptRoot 'software.json')
    $runId=[DateTime]::UtcNow.ToString('yyyyMMddTHHmmss')+'-'+[Guid]::NewGuid().ToString('N').Substring(0,8)
    $local=Join-Path (Join-Path $env:LOCALAPPDATA 'AscendMonitor\runs') $runId
    New-Item -ItemType Directory -Path $local -Force | Out-Null
    $safeConfig=Join-Path $local 'deployment.json'; Write-JsonFile $config $safeConfig
    $stages=@{}; $plans=@{}; $receipts=@()
    Write-Host 'SSH passwords, if needed, are entered directly into OpenSSH and are not saved.'
    Write-Host "Run records: $local"
    foreach ($node in $config.nodes) {
        Write-Host "Checking $($node.name) ($($node.host))..."
        $stage="$($config.storage_root.TrimEnd('/'))/$($config.executor_id)/.ascend-monitor-stage-$($config.id)-$runId"
        $stages[$node.name]=$stage
        # Only validated path components enter the shell command.
        $root=$config.storage_root.TrimEnd('/')
        $prepare="umask 077; test `"`$(readlink -f -- '$root')`" = '$root' && test ! -L '$root/$($config.executor_id)' && test ! -L '$stage' && mkdir -p '$stage' && cd '$stage' && command -v python3 >/dev/null"
        Invoke-Remote $node $prepare | Write-Host
        $files=@(Get-ChildItem -LiteralPath (Join-Path $PSScriptRoot 'runtime') -Filter '*.py' | ForEach-Object FullName)
        $files+=@($safeConfig,(Join-Path $PSScriptRoot 'software.json'))
        Send-Files $node $files $stage
        $sudo=if ($node.ssh_user -eq 'root') { '' } else { 'sudo -n ' }
        $command="cd '$stage' && ${sudo}python3 deploy_node.py --config deployment.json --manifest software.json --node '$($node.name)' --probe"
        $output=Invoke-Remote $node $command
        $output | Write-Host
        $plans[$node.name]=($output -split "`n" | Select-Object -Last 1 | ConvertFrom-Json)
        $receipts+=@{node=$node.name; host=$node.host; stage=$stage; action='probe'; completed=$true}
        Write-JsonFile $receipts (Join-Path $local 'receipts.json')
    }
    if ($CheckOnly) { Write-Host 'All node checks passed. No monitoring services were installed.' -ForegroundColor Green }
    else {
        $cache=Join-Path $env:LOCALAPPDATA 'AscendMonitor\packages'
        New-Item -ItemType Directory -Path $cache -Force | Out-Null
        $artifacts=@{}
        $needed=@($plans.Values | ForEach-Object roles | Sort-Object -Unique | Where-Object { $_ -ne 'process' })
        foreach ($item in $software.packages) { if ($item.role -in $needed) { $artifacts[$item.role]=Get-Package $item $cache } }
        foreach ($node in $config.nodes) {
            $stage=$stages[$node.name]; $sudo=if ($node.ssh_user -eq 'root') { '' } else { 'sudo -n ' }
            $upload=@($plans[$node.name].roles | Where-Object {$_ -ne 'process'} | ForEach-Object {$artifacts[$_]})
            Send-Files $node $upload $stage
            # Record the attempted install before sending it. Inspect server receipt after interruption.
            $receipts+=@{node=$node.name; host=$node.host; stage=$stage; action='install'; completed=$false}
            Write-JsonFile $receipts (Join-Path $local 'receipts.json')
            Invoke-Remote $node "cd '$stage' && ${sudo}python3 deploy_node.py --config deployment.json --manifest software.json --node '$($node.name)' --packages . --install" | Write-Host
            $receipts[-1].completed=$true; Write-JsonFile $receipts (Join-Path $local 'receipts.json')
        }
        $central=@($config.nodes | Where-Object name -eq $config.central_node)[0]
        $stage=$stages[$central.name]; $sudo=if ($central.ssh_user -eq 'root') { '' } else { 'sudo -n ' }
        Invoke-Remote $central "cd '$stage' && ${sudo}python3 deploy_node.py --config deployment.json --manifest software.json --verify" | Write-Host
        $zip=New-ClientPackage $config $local $PSScriptRoot
        Write-Host "Deployment verified. Windows client installer: $zip" -ForegroundColor Green
        Start-Process explorer.exe -ArgumentList "/select,`"$zip`""
    }
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    Write-Host 'Stopped. Existing workloads are not stopped. Inspect local receipts and the server install-receipt.json before retrying.'
    if (-not $NoPause) { Read-Host 'Press Enter to close' | Out-Null }; exit 1
}
if (-not $NoPause) { Read-Host 'Press Enter to close' | Out-Null }
