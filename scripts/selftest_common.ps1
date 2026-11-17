# temporary self-test for _common.ps1 primitives (read-only, no service start)
$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
. (Join-Path $root 'scripts\_common.ps1')

$fail = 0
function Check($name, $cond) {
    if ($cond) { Write-Host "  [ok]   $name" } else { Write-Host "  [FAIL] $name"; $script:fail++ }
}

Write-Host '--- context ---'
$ctx = New-PlatformContext -Root $root
Check 'ports 8020/5180' ($ctx.BackendPort -eq 8020 -and $ctx.FrontendPort -eq 5180)
Check 'budgets 180/15/30' ($ctx.ReadyBudgetSeconds -eq 180 -and $ctx.StopBudgetSeconds -eq 15 -and $ctx.StatusBudgetSeconds -eq 30)
Check 'watchdog path exists' (Test-Path $ctx.WatchdogScriptPath)
Check 'no hardcoded root in _common' (-not ((Get-Content (Join-Path $root 'scripts\_common.ps1') -Raw) -match [regex]::Escape($root)))

Write-Host '--- redaction ---'
Check 'jwt redacted' ((Protect-SensitiveText 'JWT_SECRET=abc123') -notmatch 'abc123')
Check 'url credential redacted' ((Protect-SensitiveText 'mysql+pymysql://root:pw123@10.0.0.1/db') -notmatch 'pw123')
Check 'bearer redacted' ((Protect-SensitiveText 'Authorization: Bearer abcdef.ghijkl.mnopqr') -notmatch 'abcdef')
Check 'normal text untouched' ((Protect-SensitiveText 'backend health ok') -eq 'backend health ok')

Write-Host '--- locks ---'
$l1 = Enter-ExclusiveLock -Path $ctx.OperationLockPath -WaitTimeoutSeconds 0 -Purpose 'selftest'
Check 'first lock acquired' $l1.Acquired
$l2 = Enter-ExclusiveLock -Path $ctx.OperationLockPath -WaitTimeoutSeconds 1 -Purpose 'selftest2'
Check 'second lock busy' ((-not $l2.Acquired) -and $l2.Status -eq 'Busy')
Release-ExclusiveLock $l1
$l3 = Enter-ExclusiveLock -Path $ctx.OperationLockPath -WaitTimeoutSeconds 1 -Purpose 'selftest3'
Check 'lock reacquirable after release' $l3.Acquired
Release-ExclusiveLock $l3

Write-Host '--- identity ---'
$ev = Get-ProcessEvidence -ProcessId 999999
Check 'missing process NotRunning' $ev.NotRunning
$v = Test-ProjectProcess -Evidence $ev -Role Backend -Context $ctx
Check 'missing process verdict NotRunning' ($v.Verdict -eq 'NotRunning')
$self = Get-ProcessEvidence -ProcessId $PID
Check 'self evidence available' $self.Available
$v2 = Test-ProjectProcess -Evidence $self -Role Watchdog -Context $ctx
Check 'self is not watchdog (External/Unknown)' ($v2.Verdict -in @('External', 'Unknown'))
$fake = [PSCustomObject]@{ ProcessId = 1; ParentProcessId = 0; CreationDate = $null; ExecutablePath = 'C:\Python310\python.exe'; CommandLine = ('python.exe "{0}"' -f $ctx.BackendScriptPath); Available = $true; NotRunning = $false; Reason = '' }
Check 'synthetic backend Owned' ((Test-ProjectProcess -Evidence $fake -Role Backend -Context $ctx).Verdict -eq 'Owned')
$fakeOther = [PSCustomObject]@{ ProcessId = 2; ParentProcessId = 0; CreationDate = $null; ExecutablePath = 'C:\Python310\python.exe'; CommandLine = 'python.exe "D:\other\backend\run.py"'; Available = $true; NotRunning = $false; Reason = '' }
Check 'other project backend External' ((Test-ProjectProcess -Evidence $fakeOther -Role Backend -Context $ctx).Verdict -eq 'External')
$fakeBoundary = [PSCustomObject]@{ ProcessId = 3; ParentProcessId = 0; CreationDate = $null; ExecutablePath = 'C:\Python310\python.exe'; CommandLine = ('python.exe "{0}-other\run.py"' -f $ctx.RootPath); Available = $true; NotRunning = $false; Reason = '' }
Check 'similar directory not matched' ((Test-ProjectProcess -Evidence $fakeBoundary -Role Backend -Context $ctx).Verdict -ne 'Owned')

Write-Host '--- ports / health / watchdog ---'
$p = Get-PortOwnership -Port 8020 -ExpectedRole Backend -Context $ctx
Check 'port 8020 free or conflict (no service running)' ($p.Status -in @('Free', 'Conflict', 'Unknown'))
Check 'port status string present' ($null -ne $p.Status)
$wd = Get-WatchdogState -Context $ctx
Check 'watchdog state has ValidCount' ($null -ne $wd.ValidCount)
$hb = Test-BackendHealth -Context $ctx -TimeoutSeconds 2
Check 'backend unhealthy while stopped' (-not $hb.Healthy)
$hf = Test-FrontendHealth -Context $ctx -TimeoutSeconds 2
Check 'frontend unhealthy while stopped' (-not $hf.Healthy)

Write-Host '--- launch specs (string only, not executed) ---'
$sb = Get-ComponentLaunchSpec -Role Backend -Context $ctx
Check 'backend cmd uses cmd.exe' ($sb.CommandLine -like 'cmd.exe*')
Check 'backend cmd sets ERP_PORT' ($sb.CommandLine -match 'set ERP_PORT=8020')
Check 'backend cmd has run.py path' ($sb.CommandLine.Contains($ctx.BackendScriptPath))
Check 'backend log names' ($sb.Logs.Count -eq 2)
$sf = Get-ComponentLaunchSpec -Role Frontend -Context $ctx
Check 'frontend strictPort' ($sf.CommandLine -match '--strictPort')
Check 'frontend port 5180' ($sf.CommandLine -match '--port 5180')
$swd = Get-ComponentLaunchSpec -Role Watchdog -Context $ctx
Check 'watchdog uses powershell -File' ($swd.CommandLine -match 'powershell\.exe.*-File')

Write-Host ''
if ($fail -eq 0) { Write-Host 'SELFTEST: all passed' } else { Write-Host "SELFTEST: $fail failure(s)" }
exit $fail