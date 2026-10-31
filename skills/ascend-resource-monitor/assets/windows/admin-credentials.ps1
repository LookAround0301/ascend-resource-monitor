# Only the operator's PC stores the password; this file is never sent to Linux.
function Set-PrivateCredentialDirectory([string]$Path) {
    New-Item -ItemType Directory -Path $Path -Force | Out-Null
    $sid=[Security.Principal.WindowsIdentity]::GetCurrent().User
    $systemSid=New-Object Security.Principal.SecurityIdentifier('S-1-5-18')
    $acl=New-Object Security.AccessControl.DirectorySecurity
    $acl.SetOwner($sid)
    $acl.SetAccessRuleProtection($true,$false)
    foreach ($identity in @($sid,$systemSid)) {
        $rule=New-Object Security.AccessControl.FileSystemAccessRule($identity,'FullControl','ContainerInherit,ObjectInherit','None','Allow')
        $acl.AddAccessRule($rule)
    }
    ([IO.DirectoryInfo]$Path).SetAccessControl($acl)
}

function Set-PrivateCredentialFile([string]$Path) {
    $acl=New-Object Security.AccessControl.FileSecurity
    $sid=[Security.Principal.WindowsIdentity]::GetCurrent().User
    $acl.SetOwner($sid)
    $acl.SetAccessRuleProtection($true,$false)
    foreach ($identity in @($sid,(New-Object Security.Principal.SecurityIdentifier('S-1-5-18')))) {
        $acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($identity,'FullControl','Allow')))
    }
    ([IO.FileInfo]$Path).SetAccessControl($acl)
}

function Get-MonitorAdminCredential($Config, [bool]$Initialized, [string]$CredentialRoot) {
    $folder=if ($CredentialRoot) {$CredentialRoot} else {Join-Path $env:LOCALAPPDATA 'AscendMonitor\credentials'}
    Set-PrivateCredentialDirectory $folder
    $path=Join-Path $folder ($Config.id+'.json')
    $central=@($Config.nodes | Where-Object name -eq $Config.central_node)[0]
    if (Test-Path -LiteralPath $path) {
        Set-PrivateCredentialFile $path
        $saved=Read-JsonFile $path
        if ($saved.host -ne $central.host -or [int]$saved.ssh_port -ne [int]$central.ssh_port) {
            throw "Saved credential belongs to another endpoint: $path"
        }
        if (-not $saved.password) { throw "Saved credential is empty: $path" }
    } else {
        if ($Initialized) {
            $secure=Read-Host 'Enter the existing Grafana admin password (hidden; account will not be reset)' -AsSecureString
            $pointer=[Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
            try { $password=[Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer) }
            finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer) }
            if (-not $password) { throw 'The existing administrator password is required.' }
        } else {
            $bytes=New-Object byte[] 32
            $rng=[Security.Cryptography.RandomNumberGenerator]::Create()
            try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
            $password=[Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+','-').Replace('/','_')
        }
        $saved=[ordered]@{version=1;host=$central.host;ssh_port=[int]$central.ssh_port;username='admin';password=$password}
        # The parent already has a protected operator+SYSTEM ACL before creation.
        Write-JsonFile $saved $path
        Set-PrivateCredentialFile $path
    }
    Write-Host "Administrator credential is kept only on this PC: $path"
    return [pscustomobject]@{Password=[string]$saved.password;Path=$path}
}
