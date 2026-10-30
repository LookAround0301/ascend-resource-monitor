param([Parameter(Mandatory=$true)][string]$Directory)
$ErrorActionPreference='Stop'
$failures=@()
Get-ChildItem -LiteralPath $Directory -Filter '*.ps1' | ForEach-Object {
    $tokens=$null; $parseErrors=$null
    [System.Management.Automation.Language.Parser]::ParseFile($_.FullName,[ref]$tokens,[ref]$parseErrors) | Out-Null
    if ($parseErrors) { $failures += $parseErrors.Message }
}
if ($failures) { $failures | Write-Error; exit 1 }
Write-Output 'PowerShell syntax passed'
