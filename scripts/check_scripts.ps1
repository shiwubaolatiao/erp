# LMG ERP v2 - read-only static guard for ops scripts (P0)
# ASCII only. Compatible with Windows PowerShell 5.1.
# Checks:
#   1) every .bat in project root is pure ASCII and uses CRLF line endings
#   2) every .ps1 in scripts/ parses under the current PowerShell parser
#   3) no PS7-only operators, no hardcoded project root, no Start-Process for
#      long-lived processes, no legacy-port (8000/5173) operations targets
# Exit code = number of failed checks (0 means all guards passed).

param(
    [string]$Root = ""
)

$ErrorActionPreference = "Continue"
if (-not $Root) {
    $Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
}
$Root = [System.IO.Path]::GetFullPath($Root)

$script:Failures = 0

function Add-Failure([string]$File, [string]$Rule, [string]$Detail) {
    $script:Failures++
    Write-Host ("  [FAIL] {0} | {1} | {2}" -f $File, $Rule, $Detail)
}

function Add-Pass([string]$File, [string]$Rule) {
    Write-Host ("  [ok]   {0} | {1}" -f $File, $Rule)
}

function Test-BatchFile([string]$Path) {
    $name = Split-Path -Leaf $Path
    $bytes = [System.IO.File]::ReadAllBytes($Path)
    $nonAscii = 0
    foreach ($b in $bytes) { if ($b -gt 127) { $nonAscii++ } }
    if ($nonAscii -gt 0) {
        Add-Failure $name "bat-ascii" "$nonAscii non-ASCII byte(s); batch files must stay ASCII"
    } else {
        Add-Pass $name "bat-ascii"
    }
    $text = [System.Text.Encoding]::ASCII.GetString($bytes)
    $lfOnly = ([regex]::Matches($text, "(?<!\r)\n")).Count
    if ($lfOnly -gt 0) {
        Add-Failure $name "bat-crlf" "$lfOnly LF-only line ending(s); convert to CRLF"
    } else {
        Add-Pass $name "bat-crlf"
    }
}

function Test-PowerShellFile([string]$Path) {
    $name = Split-Path -Leaf $Path
    $raw = Get-Content -LiteralPath $Path -Raw -ErrorAction SilentlyContinue
    if ($null -eq $raw) {
        Add-Failure $name "ps-read" "cannot read file"
        return
    }

    $parseErrors = $null
    [System.Management.Automation.PSParser]::Tokenize($raw, [ref]$parseErrors) | Out-Null
    if ($parseErrors -and $parseErrors.Count -gt 0) {
        Add-Failure $name "ps-parse" ($parseErrors[0].Message)
    } else {
        Add-Pass $name "ps-parse"
    }

    $lines = $raw -split "`r?`n"
    $badOps = @()
    $hardRoot = @()
    $startProcess = @()
    $legacyPort = @()
    for ($i = 0; $i -lt $lines.Count; $i++) {
        $line = $lines[$i]
        $trim = $line.TrimStart()
        if ($trim.StartsWith("#")) { continue }
        # strip string literals before operator detection: cmd.exe connectors
        # such as 'set X=1&& ...' live inside PowerShell strings and are legal.
        $code = [regex]::Replace($line, "'[^']*'", "''")
        $code = [regex]::Replace($code, '"[^"]*"', '""')
        if ($code -match '\?\?|\?\.|&&|\|\|') { $badOps += ($i + 1) }
        if ($line -match '[A-Za-z]:\\[^"'']*erp' -and $line -match 'erp重构|项目') { $hardRoot += ($i + 1) }
        if ($code -match '\bStart-Process\b') { $startProcess += ($i + 1) }
        if ($code -match '\b(8000|5173)\b') { $legacyPort += ($i + 1) }
    }
    if ($badOps.Count -gt 0) {
        Add-Failure $name "ps51-syntax" ("PS7-only operator candidate at line(s): " + ($badOps -join ','))
    } else {
        Add-Pass $name "ps51-syntax"
    }
    if ($hardRoot.Count -gt 0) {
        Add-Failure $name "no-hardcoded-root" ("hardcoded project path at line(s): " + ($hardRoot -join ','))
    } else {
        Add-Pass $name "no-hardcoded-root"
    }
    if ($startProcess.Count -gt 0) {
        Add-Failure $name "no-start-process" ("Start-Process at line(s): " + ($startProcess -join ','))
    } else {
        Add-Pass $name "no-start-process"
    }
    if ($legacyPort.Count -gt 0) {
        Add-Failure $name "no-legacy-port-ops" ("legacy port reference at line(s): " + ($legacyPort -join ','))
    } else {
        Add-Pass $name "no-legacy-port-ops"
    }
}

Write-Host "=== LMG ERP v2 script static guard (read-only) ==="
Write-Host ("Root: {0}" -f $Root)
Write-Host ""

$batFiles = Get-ChildItem -LiteralPath $Root -Filter *.bat -File -ErrorAction SilentlyContinue
if (-not $batFiles) { Add-Failure "(root)" "bat-present" "no .bat entry point found" }
foreach ($f in $batFiles) { Test-BatchFile $f.FullName }

$psFiles = Get-ChildItem -LiteralPath (Join-Path $Root "scripts") -Filter *.ps1 -File -ErrorAction SilentlyContinue
foreach ($f in $psFiles) {
    if ($f.Name -eq "check_scripts.ps1") { continue }
    Test-PowerShellFile $f.FullName
}

Write-Host ""
if ($script:Failures -eq 0) {
    Write-Host "RESULT: all guards passed"
} else {
    Write-Host ("RESULT: {0} guard failure(s)" -f $script:Failures)
}
exit $script:Failures