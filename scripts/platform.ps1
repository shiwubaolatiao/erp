# LMG ERP v2 - unified platform orchestration (platform.ps1)
# ASCII only. Compatible with Windows PowerShell 5.1.
#
# Single PowerShell entry point for the four batch wrappers:
#   platform.ps1 -Action Start|Stop|Status|Restart -Root <project root>
#
# Lock model: Start/Stop/Restart hold logs/platform.operation.lock for the whole
# operation; Restart holds it once and reuses the same Core functions (no re-lock).
# Status never takes a lock and never changes the system.
#
# Exit codes: 0 ok/already-in-target-state | 1 generic/partial failure
#             2 dependency/config/DB precheck | 3 port conflict
#             4 operation busy | 5 identity unknown | 6 ready timeout

param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('Start', 'Stop', 'Status', 'Restart')]
    [string]$Action,

    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$Root
)

$ErrorActionPreference = 'Continue'
. (Join-Path $PSScriptRoot '_common.ps1')

# ------------------------------------------------------------- prechecks ----

function Test-RuntimeDependencies {
    param([Parameter(Mandatory = $true)]$Context)
    $missing = @()
    if (-not (Get-Command python.exe -ErrorAction SilentlyContinue)) { $missing += 'python.exe not found in PATH' }
    else {
        $null = & python.exe -c "import fastapi, uvicorn, sqlalchemy, pymysql, openpyxl, jose, bcrypt, dotenv" 2>&1
        if ($LASTEXITCODE -ne 0) { $missing += 'python packages missing (pip install -r backend/requirements.txt)' }
    }
    if (-not (Get-Command node.exe -ErrorAction SilentlyContinue)) { $missing += 'node.exe not found in PATH' }
    if (-not (Test-Path (Join-Path $Context.FrontendDirectory 'node_modules\vite'))) {
        $missing += 'frontend node_modules missing (npm install in frontend/)'
    }
    return $missing
}

function Get-BackendConfigState {
    param([Parameter(Mandatory = $true)]$Context)
    $code = "from app.core.config import settings; print('JWT=' + ('1' if settings.JWT_SECRET.strip() else '0')); print('CORS=' + str(len(settings.CORS_ORIGIN_LIST)))"
    Push-Location $Context.BackendDirectory
    try {
        $out = & python.exe -c $code 2>&1
    } finally {
        Pop-Location
    }
    $text = ($out | Out-String)
    $state = [PSCustomObject]@{ JwtConfigured = $false; CorsCount = 0; Readable = $false }
    if ($text -match 'JWT=1') { $state.JwtConfigured = $true }
    if ($text -match 'CORS=(\d+)') { $state.CorsCount = [int]$Matches[1]; $state.Readable = $true }
    return $state
}

# --------------------------------------------------------------- start core -

function Invoke-StartCore {
    param([Parameter(Mandatory = $true)]$Context)

    $result = New-OperationResult -Stage 'precheck'
    $launched = @{ Backend = 0; Frontend = 0 }

    Write-Host '[1/5] checking dependencies and configuration ...'
    $missing = Test-RuntimeDependencies -Context $Context
    $cfg = Get-BackendConfigState -Context $Context
    if (-not $cfg.JwtConfigured) { $missing += 'JWT_SECRET is not configured (set it in backend/.env)' }
    if ($cfg.CorsCount -le 0) { $missing += 'CORS_ORIGINS resolved empty (set an explicit whitelist in backend/.env)' }
    if ($missing.Count -gt 0) {
        foreach ($m in $missing) { Write-Host ("  [FAIL] {0}" -f $m); Add-ResultDetail $result $m }
        $category = 'DEPENDENCY_MISSING'
        if (-not $cfg.JwtConfigured) { $category = 'JWT_SECRET_MISSING' }
        elseif ($cfg.CorsCount -le 0) { $category = 'CORS_ORIGINS_EMPTY' }
        $result.Succeeded = $false
        $result.FailureCategory = $category
        $result.ExitCode = 2
        $result.Stage = 'precheck'
        $result.NextAction = 'fix the items above, then run start.bat again'
        $result.LogPaths = @((Get-DiagnosticLogExcerpt -Context $Context).Path)
        Write-PlatformLog -Context $Context -Action 'start' -Component 'platform' -Result 'precheck_failed' -Message ($missing -join ' | ')
        return $result
    }
    Write-Host '  runtime and configuration OK'

    Write-Host '[2/5] checking database and ports ...'
    $db = Test-DatabaseDependency -Context $Context -TimeoutSeconds 5
    if (-not $db.Healthy) {
        $result.Succeeded = $false
        $result.Stage = 'database'
        $result.ExitCode = 2
        $result.FailureCategory = $db.Category
        if ($db.Category -eq 'DB_NETWORK_UNREACHABLE') {
            $result.Message = 'remote MySQL is unreachable; the DB server may need to be powered on manually (scripts cannot recover it)'
        } else {
            $result.Message = 'database check failed; verify DB_USER/DB_PASSWORD/DB_NAME in backend/.env'
        }
        $result.NextAction = 'restore the database, then run start.bat again'
        Write-Host ("  [FAIL] {0} ({1})" -f $result.FailureCategory, $db.Target)
        Write-Host ("  {0}" -f $result.Message)
        Write-PlatformLog -Context $Context -Action 'start' -Component 'database' -Result $db.Category -Message $db.Target
        return $result
    }
    Write-Host ("  database OK ({0})" -f $db.Target)

    $portBackend = Get-PortOwnership -Port $Context.BackendPort -ExpectedRole Backend -Context $Context
    $portFrontend = Get-PortOwnership -Port $Context.FrontendPort -ExpectedRole Frontend -Context $Context
    foreach ($p in @($portBackend, $portFrontend)) {
        if ($p.Status -in @('Conflict', 'Unknown')) {
            $result.Succeeded = $false
            $result.Stage = 'ports'
            $result.FailureCategory = $(if ($p.Status -eq 'Conflict') { 'PORT_CONFLICT' } else { 'IDENTITY_UNKNOWN' })
            $result.ExitCode = $(if ($p.Status -eq 'Conflict') { 3 } else { 5 })
            $result.Message = "port $($p.Port): $($p.Detail)"
            $result.NextAction = 'identify the process manually; this script will not kill it'
            Write-Host ("  [FAIL] port {0}: {1}" -f $p.Port, $p.Detail)
            foreach ($o in $p.Owners) {
                Write-Host ("         pid {0} {1} -> {2} {3}" -f $o.ProcessId, $o.Name, $o.Verdict, $o.Reason)
                Add-ResultDetail $result ("port {0} pid {1} verdict {2}" -f $p.Port, $o.ProcessId, $o.Verdict)
            }
            Write-Host '  no process was terminated. Please confirm the owner yourself.'
            Write-PlatformLog -Context $Context -Action 'start' -Component 'ports' -Result $result.FailureCategory -Message $result.Message
            return $result
        }
    }
    Write-Host '  ports usable (free or owned by this project)'

    # database schema/seed initialization is allowed only on explicit Start
    $null = Test-DatabaseDependency -Context $Context -TimeoutSeconds 5 -EnsureDatabase

    Write-Host '[3/5] reconciling services and watchdog ...'
    $bh = Test-BackendHealth -Context $Context -TimeoutSeconds 3
    $fh = Test-FrontendHealth -Context $Context -TimeoutSeconds 3

    if ($bh.Healthy) {
        Write-Host '  backend already healthy, reusing it'
    } else {
        if ($portBackend.Status -eq 'OwnedListening') {
            Write-Host '  backend owned but unhealthy, rebuilding it'
            $null = Stop-PlatformComponent -Role Backend -Context $Context -Source 'User'
        }
        $start = Start-PlatformComponent -Role Backend -Context $Context -Source 'User'
        if (-not $start.Accepted) {
            $result.Succeeded = $false; $result.Stage = 'start-backend'
            $result.FailureCategory = 'COMPONENT_START_FAILED'; $result.ExitCode = 1
            $result.Message = $start.Message; $result.LogPaths = $start.LogPaths
            $result.NextAction = 'check the backend error log, then run restart.bat'
            return $result
        }
        $launched.Backend = $start.ProcessId
        Write-Host ("  backend start accepted (launcher pid {0})" -f $start.ProcessId)
    }

    if ($fh.Healthy) {
        Write-Host '  frontend already healthy, reusing it'
    } else {
        if ($portFrontend.Status -eq 'OwnedListening') {
            Write-Host '  frontend owned but unhealthy, rebuilding it'
            $null = Stop-PlatformComponent -Role Frontend -Context $Context -Source 'User'
        }
        $start = Start-PlatformComponent -Role Frontend -Context $Context -Source 'User'
        if (-not $start.Accepted) {
            $result.Succeeded = $false; $result.Stage = 'start-frontend'
            $result.FailureCategory = 'COMPONENT_START_FAILED'; $result.ExitCode = 1
            $result.Message = $start.Message; $result.LogPaths = $start.LogPaths
            $result.NextAction = 'check the frontend error log, then run restart.bat'
            return $result
        }
        $launched.Frontend = $start.ProcessId
        Write-Host ("  frontend start accepted (launcher pid {0})" -f $start.ProcessId)
    }

    $wd = Get-WatchdogState -Context $Context
    if ($wd.ValidCount -eq 1) {
        Write-Host ("  watchdog already running (pid {0})" -f ($wd.Instances -join ','))
    } elseif ($wd.ValidCount -gt 1) {
        # keep the instance recorded in the pid file, stop the other owned ones
        $keep = $wd.ProcessId
        if ($keep -gt 0 -and $wd.Instances -contains $keep) {
            foreach ($other in $wd.Instances) {
                if ($other -ne $keep) {
                    Write-Host ("  stopping duplicate watchdog pid {0}" -f $other)
                    Stop-Process -Id $other -Force -ErrorAction SilentlyContinue
                }
            }
        } else {
            $result.Succeeded = $false; $result.Stage = 'watchdog'
            $result.FailureCategory = 'IDENTITY_UNKNOWN'; $result.ExitCode = 5
            $result.Message = 'multiple watchdog instances and no trustworthy record to keep'
            $result.NextAction = 'run status.bat, then stop duplicates manually'
            return $result
        }
    } else {
        if ($wd.RecordState -in @('Stale', 'IdentityMismatch')) {
            Remove-Item -LiteralPath $Context.WatchdogPidPath -Force -ErrorAction SilentlyContinue
        }
        $start = Start-PlatformComponent -Role Watchdog -Context $Context -Source 'User'
        if (-not $start.Accepted) {
            $result.Succeeded = $false; $result.Stage = 'start-watchdog'
            $result.FailureCategory = 'COMPONENT_START_FAILED'; $result.ExitCode = 1
            $result.Message = $start.Message; $result.LogPaths = $start.LogPaths
            return $result
        }
        Write-Host ("  watchdog start accepted (pid {0})" -f $start.ProcessId)
    }

    Write-Host '[4/5] waiting for readiness (budget 180s) ...'
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $ready = $false
    $lastNote = ''
    while ($sw.Elapsed.TotalSeconds -lt $Context.ReadyBudgetSeconds) {
        $bh = Test-BackendHealth -Context $Context -TimeoutSeconds 3
        $fh = Test-FrontendHealth -Context $Context -TimeoutSeconds 3

        if (-not $bh.Healthy -and $launched.Backend -gt 0) {
            $ev = Get-ProcessEvidence -ProcessId $launched.Backend
            if ($ev.NotRunning -and $portBackend.Status -ne 'OwnedListening') {
                $portNow = Get-PortOwnership -Port $Context.BackendPort -ExpectedRole Backend -Context $Context
                if ($portNow.Status -eq 'Free') {
                    $result.Succeeded = $false; $result.Stage = 'backend-exited'
                    $result.FailureCategory = 'COMPONENT_START_FAILED'; $result.ExitCode = 1
                    $result.Message = 'backend launcher exited before becoming healthy'
                    $result.LogPaths = @((Get-DiagnosticLogExcerpt -Context $Context -Role Backend).Path)
                    $result.NextAction = 'read the backend error log below, fix it, then run restart.bat'
                    $sw.Stop()
                    return $result
                }
            }
        }

        if ($bh.Healthy -and $fh.Healthy) {
            $dbNow = Test-DatabaseDependency -Context $Context -TimeoutSeconds 3
            $wdNow = Get-WatchdogState -Context $Context
            if ($dbNow.Healthy -and $wdNow.ValidCount -eq 1) { $ready = $true; break }
            $lastNote = $(if (-not $dbNow.Healthy) { 'database' } else { 'watchdog' })
        } else {
            $lastNote = (@($(if (-not $bh.Healthy) { 'backend' }), $(if (-not $fh.Healthy) { 'frontend' })) -join '+')
        }
        Write-Host ("  {0,3}s waiting: {1}" -f [int]$sw.Elapsed.TotalSeconds, $lastNote)
        Start-Sleep -Seconds $Context.ReadyPollSeconds
    }
    $sw.Stop()

    if (-not $ready) {
        $result.Succeeded = $false
        $result.Stage = 'readiness'
        $result.FailureCategory = 'READY_TIMEOUT'
        $result.ExitCode = 6
        $result.Message = "not ready within $($Context.ReadyBudgetSeconds)s (last pending: $lastNote)"
        $result.NextAction = 'run status.bat first, then decide whether to restart.bat (services were left running)'
        $result.LogPaths = @(
            (Get-DiagnosticLogExcerpt -Context $Context -Role Backend).Path,
            (Get-DiagnosticLogExcerpt -Context $Context -Role Frontend).Path,
            (Get-DiagnosticLogExcerpt -Context $Context -Role Watchdog).Path)
        Write-Host ("  [TIMEOUT] {0}" -f $result.Message)
        Write-PlatformLog -Context $Context -Action 'start' -Component 'platform' -Result 'ready_timeout' -Message $lastNote
        return $result
    }

    Write-Host '[5/5] services ready.'
    $result.Succeeded = $true
    $result.Stage = 'ready'
    $result.ExitCode = 0
    $result.Message = 'platform is ready'
    Write-PlatformLog -Context $Context -Action 'start' -Component 'platform' -Result 'ready' -Message ''
    return $result
}

# ---------------------------------------------------------------- stop core -

function Invoke-StopCore {
    param([Parameter(Mandatory = $true)]$Context)

    $result = New-OperationResult -Stage 'stop'
    $problems = @()

    $wd = Get-WatchdogState -Context $Context
    if ($wd.ValidCount -eq 0 -and $wd.RecordState -in @('NoRecord', 'Stale')) {
        # nothing to stop on the watchdog side
    }
    foreach ($procId in $wd.Instances) {
        $ev = Get-ProcessEvidence -ProcessId $procId
        $v = Test-ProjectProcess -Evidence $ev -Role Watchdog -Context $Context
        if ($v.Verdict -eq 'Owned') {
            Write-Host ("  stopping watchdog pid {0}" -f $procId)
            Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
        } else {
            $problems += "watchdog pid $procId not confirmed ($($v.Verdict))"
        }
    }
    if ($wd.RecordState -in @('Stale', 'IdentityMismatch')) {
        Remove-Item -LiteralPath $Context.WatchdogPidPath -Force -ErrorAction SilentlyContinue
    }

    foreach ($role in @('Backend', 'Frontend')) {
        $r = Stop-PlatformComponent -Role $role -Context $Context -Source 'User'
        if ($r.Stopped.Count -gt 0) { Write-Host ("  {0} stopped: {1}" -f $role, ($r.Stopped -join ',')) }
        foreach ($s in $r.Skipped) { $problems += "$role pid $($s.ProcessId): $($s.Reason)" }
    }

    $wdAfter = Get-WatchdogState -Context $Context
    if ($wdAfter.ValidCount -gt 0) { $problems += 'watchdog instance still present' }

    $pb = Get-PortOwnership -Port $Context.BackendPort -ExpectedRole Backend -Context $Context
    $pf = Get-PortOwnership -Port $Context.FrontendPort -ExpectedRole Frontend -Context $Context
    foreach ($p in @($pb, $pf)) {
        if ($p.Status -in @('Conflict', 'Unknown')) {
            $problems += "port $($p.Port) still held by non-project or unknown process"
        } elseif ($p.Status -eq 'OwnedListening') {
            $problems += "port $($p.Port) still held by this project"
        }
    }

    Write-Host '  note: remote MySQL was NOT stopped.'
    if ($problems.Count -eq 0) {
        $result.Succeeded = $true
        $result.ExitCode = 0
        $result.Message = 'platform stopped'
        Write-PlatformLog -Context $Context -Action 'stop' -Component 'platform' -Result 'ok' -Message ''
    } else {
        $result.Succeeded = $false
        $result.PartialFailure = $true
        $result.ExitCode = 1
        $result.FailureCategory = 'STOP_INCOMPLETE'
        $result.Message = ($problems -join ' | ')
        $result.NextAction = 'inspect the listed processes manually; nothing external was killed'
        foreach ($p in $problems) { Write-Host ("  [WARN] {0}" -f $p) }
        Write-PlatformLog -Context $Context -Action 'stop' -Component 'platform' -Result 'partial' -Message $result.Message
    }
    return $result
}

# -------------------------------------------------------------- status core -

function Get-PlatformStatus {
    param([Parameter(Mandatory = $true)]$Context)

    $result = New-OperationResult -Stage 'status'
    $issues = @()

    Write-Host '============ LMG ERP v2  status  ============'
    Write-Host ("Root: {0}" -f $Context.RootPath)

    $wd = Get-WatchdogState -Context $Context
    Write-Host '[watchdog]'
    Write-Host ("  record={0} instances={1} valid={2} {3}" -f $wd.RecordState, ($wd.Instances -join ','), $wd.ValidCount, $wd.Reason)
    if ($wd.ValidCount -ne 1) { $issues += 'watchdog is not exactly one valid instance (use start.bat to reconcile)' }

    $bh = Test-BackendHealth -Context $Context -TimeoutSeconds 4
    $fh = Test-FrontendHealth -Context $Context -TimeoutSeconds 4
    Write-Host '[backend]'
    Write-Host ("  healthy={0} identity={1} {2}" -f $bh.Healthy, $bh.IdentityMatched, $bh.Error)
    if (-not $bh.Healthy) { $issues += 'backend not healthy' }
    Write-Host '[frontend]'
    Write-Host ("  healthy={0} identity={1} {2}" -f $fh.Healthy, $fh.IdentityMatched, $fh.Error)
    if (-not $fh.Healthy) { $issues += 'frontend not healthy' }

    foreach ($p in @((Get-PortOwnership -Port $Context.BackendPort -ExpectedRole Backend -Context $Context),
                     (Get-PortOwnership -Port $Context.FrontendPort -ExpectedRole Frontend -Context $Context))) {
        Write-Host ("  port {0}: {1} {2}" -f $p.Port, $p.Status, $p.Detail)
        if ($p.Status -in @('Conflict', 'Unknown')) { $issues += "port $($p.Port) ownership conflict/unknown" }
    }

    $db = Test-DatabaseDependency -Context $Context -TimeoutSeconds 5
    Write-Host '[database]'
    Write-Host ("  target={0} status={1}" -f $db.Target, $db.Category)
    if (-not $db.Healthy) {
        if ($db.Category -eq 'DB_NETWORK_UNREACHABLE') {
            $issues += 'remote MySQL unreachable (server may need manual power-on; scripts cannot recover it)'
        } else {
            $issues += 'database check failed (verify credentials/configuration in backend/.env)'
        }
    }

    if (-not $bh.Healthy) {
        $log = Get-DiagnosticLogExcerpt -Context $Context -Role Backend
        Write-Host ("[backend log] {0}" -f $(if ($log.Available) { $log.Path } else { $log.Note }))
        foreach ($l in $log.Lines) { Write-Host ("   {0}" -f $l) }
    }
    if (-not $fh.Healthy) {
        $log = Get-DiagnosticLogExcerpt -Context $Context -Role Frontend
        Write-Host ("[frontend log] {0}" -f $(if ($log.Available) { $log.Path } else { $log.Note }))
        foreach ($l in $log.Lines) { Write-Host ("   {0}" -f $l) }
    }

    $overall = 'Healthy'
    if ($issues.Count -gt 0) {
        $overall = 'Degraded'
        if (-not $bh.Healthy -and -not $fh.Healthy -and $wd.ValidCount -eq 0) { $overall = 'Stopped' }
    }
    Write-Host ("Overall: {0}" -f $overall)
    foreach ($i in $issues) { Write-Host ("  - {0}" -f $i); Add-ResultDetail $result $i }

    $result.Succeeded = ($issues.Count -eq 0)
    $result.ExitCode = $(if ($issues.Count -eq 0) { 0 } else { 1 })
    $result.Message = $overall
    return $result
}

# ------------------------------------------------------------- restart core -

function Invoke-RestartCore {
    param([Parameter(Mandatory = $true)]$Context)
    Write-Host '=== restart: stop phase ==='
    $stop = Invoke-StopCore -Context $Context
    if (-not $stop.Succeeded) {
        $stop.Stage = 'Stop'
        $stop.Message = 'restart aborted during stop phase: ' + $stop.Message
        return $stop
    }
    Write-Host '=== restart: start phase ==='
    $start = Invoke-StartCore -Context $Context
    $start.Stage = 'Start/' + $start.Stage
    return $start
}

# ------------------------------------------------------------------- output -

function Write-OperationSummary {
    param([Parameter(Mandatory = $true)]$Result, [Parameter(Mandatory = $true)]$Context)
    Write-Host ''
    if ($Result.Succeeded) {
        Write-Host ("RESULT: OK ({0})" -f $Result.Message)
    } else {
        Write-Host ("RESULT: FAILED stage={0} category={1}" -f $Result.Stage, $Result.FailureCategory)
        Write-Host ("  {0}" -f (Protect-SensitiveText $Result.Message))
        foreach ($p in @($Result.LogPaths)) {
            if ($p) {
                Write-Host ("  log: {0}" -f $p)
                try {
                    Get-Content -LiteralPath $p -Tail $Context.DiagnosticLogLines -ErrorAction Stop |
                        ForEach-Object { Write-Host ("    {0}" -f (Protect-SensitiveText $_)) }
                } catch { }
            }
        }
    }
    if ($Result.NextAction) { Write-Host ("  next: {0}" -f $Result.NextAction) }
}

# -------------------------------------------------------------------- main --

$ctx = $null
try {
    $ctx = New-PlatformContext -Root $Root
} catch {
    Write-Host ("RESULT: FAILED {0}" -f (Protect-SensitiveText $_.Exception.Message))
    exit 1
}

$final = $null
if ($Action -eq 'Status') {
    $final = Get-PlatformStatus -Context $ctx
    Write-OperationSummary -Result $final -Context $ctx
    exit $final.ExitCode
}

$budget = $(if ($Action -eq 'Stop') { $ctx.StopBudgetSeconds } else { $ctx.ReadyBudgetSeconds })
$lock = Enter-ExclusiveLock -Path $ctx.OperationLockPath -WaitTimeoutSeconds $budget -Purpose 'platform-operation'
if (-not $lock.Acquired) {
    $final = New-OperationResult -Stage 'lock' -FailureCategory 'OPERATION_BUSY' -ExitCode 4 `
        -Succeeded $false -Message $lock.Message -NextAction 'run status.bat; retry after the other operation finishes'
    Write-OperationSummary -Result $final -Context $ctx
    exit 4
}

try {
    switch ($Action) {
        'Start'   { $final = Invoke-StartCore -Context $ctx }
        'Stop'    { $final = Invoke-StopCore -Context $ctx }
        'Restart' { $final = Invoke-RestartCore -Context $ctx }
    }
} catch {
    $final = New-OperationResult -Stage 'unhandled' -FailureCategory 'UNHANDLED_ERROR' -ExitCode 1 `
        -Succeeded $false -Message (Protect-SensitiveText $_.Exception.Message)
} finally {
    Release-ExclusiveLock $lock
}

if ($Action -eq 'Start' -and $final.Succeeded) {
    Write-Host ''
    Write-Host ("  Frontend : http://localhost:{0}/" -f $ctx.FrontendPort)
    Write-Host ("  API docs : http://localhost:{0}/docs" -f $ctx.BackendPort)
    Write-Host '  Ops      : status.bat / stop.bat / restart.bat'
    Write-Host ("  Logs     : {0}" -f $ctx.LogDirectory)
    Write-Host '  Account  : admin / 123456 applies ONLY to a fresh seed account whose password'
    Write-Host '             was never changed; the first login forces a password change.'
}

Write-OperationSummary -Result $final -Context $ctx
exit $final.ExitCode