param([string]$WindowsAssets)
. (Join-Path $WindowsAssets 'common.ps1')
. (Join-Path $WindowsAssets 'admin-credentials.ps1')
$testParent=[IO.Path]::GetFullPath([IO.Path]::GetTempPath())
$testRoot=Join-Path $testParent ('ascend-credentials-test-'+[Guid]::NewGuid().ToString('N'))
try {
    $config=[pscustomobject]@{id='test';central_node='node';nodes=@([pscustomobject]@{name='node';host='192.0.2.1';ssh_port=22})}
    $first=Get-MonitorAdminCredential $config $false $testRoot
    $second=Get-MonitorAdminCredential $config $true $testRoot
    if ($first.Password.Length -lt 32 -or $first.Password -ne $second.Password) {throw 'Credential generation or reuse failed.'}
    $acl=([IO.FileInfo]$first.Path).GetAccessControl()
    if (-not $acl.AreAccessRulesProtected) {throw 'Credential file must not inherit broader permissions.'}
    $allowed=@([Security.Principal.WindowsIdentity]::GetCurrent().User.Value,'S-1-5-18')
    foreach ($entry in $acl.Access) {
        if ($entry.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value -notin $allowed) {throw 'Unexpected credential file permission.'}
    }
    $config.nodes[0].host='192.0.2.2'
    $rejected=$false
    try {Get-MonitorAdminCredential $config $true $testRoot | Out-Null} catch {$rejected=$true}
    if (-not $rejected) {throw 'Endpoint mismatch did not fail.'}
    Write-Output 'Local credential generation, reuse and ACL checks passed'
} finally {
    $resolved=[IO.Path]::GetFullPath($testRoot)
    if (-not $resolved.StartsWith($testParent,[StringComparison]::OrdinalIgnoreCase) -or [IO.Path]::GetDirectoryName($resolved).TrimEnd('\') -ne $testParent.TrimEnd('\')) {throw 'Unsafe test cleanup path.'}
    if (Test-Path -LiteralPath $resolved) {Remove-Item -LiteralPath $resolved -Recurse -Force}
}
