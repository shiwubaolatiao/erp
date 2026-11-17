# LMG ERP v2 watchdog - hidden supervisor for backend(8020) + frontend(5180)
# ASCII only. Compatible with Windows PowerShell 5.1.
#
# Lifetime single instance: holds logs/watchdog.lock for the whole process life.
# Recovery state machine per component:
#   Healthy -> Suspect(1st/2nd failure, no changes) -> Recovering(3rd failure)
#   Recovering -> Healthy (verified by a later probe) | RecoveryFailed (>60s)
#   Blocked: external/unknown port owner, or DB unreachable (backend only)
# Recovery never kills external processes and never trusts a CIM return code.

param(
    [int]$IntervalSeconds = 15,
    [string]$Root = ""
)

$ErrorActionPreference = 'Continue'
. (Join-Path $PSScriptRoot '_common.ps1')

if (-not $Root) {
    $Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
}
$Context = New-PlatformContext -Root $Root
$RecoveryVerifySeconds = 60
$RecoveryBackoffSeconds = 60

function Write-WatchdogLog([string]$Message) {
    try {
        $line = '[{0}] {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), (Protect-SensitiveText $Message)
        Add-Content -LiteralPath $Context.WatchdogLogPath -Value $line -Encoding UTF8 -ErrorAction Stop
        $all = Get-Content -LiteralPath $Context.WatchdogLogPath -ErrorAction SilentlyContinue
        if ($all -and $all.Count -gt $Context.WatchdogLogLines) {
            $all | Select-Object -Last $Context.WatchdogLogLines |
                Set-Content -LiteralPath $Context.WatchdogLogPath -Encoding UTF8
        }
    } catch { }
}

function New-ComponentState {
    return @{
        Failures = 0; Status = 'Healthy'; RecoveryStartedAt = $null
        BackoffUntil = $null; Note = ''
    }
}

function Test-ComponentHealth([string]$Role) {
    if ($Role -eq 'Backend') { return Test-BackendHealth -Context $Context -TimeoutSeconds 5 }
    return Test-FrontendHealth -Context $Context -TimeoutSeconds 5
}

function Get-RecoveryBlockReason([string]$Role) {
    # returns '' when recovery is allowed, otherwise the blocking reason
    if ($Role -eq 'Backend') {
        $db = Test-DatabaseDependency -Context $Context -TimeoutSeconds 5
        if (-not $db.Healthy) {
            return "database unreachable ($($db.Category)); remote DB server may need manual power-on"
        }
    }
    $port = $(if ($Role -eq 'Backend') { $Context.BackendPort } else { $Context.FrontendPort })
    $own = Get-PortOwnership -Port $port -ExpectedRole $Role -Context $Context
    if ($own.Status -in @('Conflict', 'Unknown')) {
        return "port $port held by external/unknown process ($($own.Status)); refusing to kill it"
    }
    return ''
}

function Invoke-ComponentRecovery([string]$Role, $State) {
    $opLock = Enter-ExclusiveLock -Path $Context.OperationLockPath -WaitTimeoutSeconds 0 -Purpose 'watchdog-recovery'
    if (-not $opLock.Acquired) {
        Write-WatchdogLog "$Role recovery skipped: a platform operation is in progress"
        return
    }
    try {
        # re-verify inside the lock before changing anything
        $health = Test-ComponentHealth $Role
        if ($health.Healthy) {
            $State.Status = 'Healthy'
            $State.Failures = 0
            return
        }
        $block = Get-RecoveryBlockReason $Role
        if ($block) {
            $State.Status = 'Blocked'
            $State.Note = $block
            Write-WatchdogLog "$Role recovery blocked: $block"
            return
        }
        $port = $(if ($Role -eq 'Backend') { $Context.BackendPort } else { $Context.FrontendPort })
        $own = Get-PortOwnership -Port $port -ExpectedRole $Role -Context $Context
        if ($own.Status -eq 'OwnedListening') {
            Write-WatchdogLog "$Role stopping owned but unhealthy component before recovery"
            $null = Stop-PlatformComponent -Role $Role -Context $Context -Source 'Watchdog'
        }
        $start = Start-PlatformComponent -Role $Role -Context $Context -Source 'Watchdog'
        if ($start.Accepted) {
            $State.Status = 'Recovering'
            $State.RecoveryStartedAt = Get-Date
            Write-WatchdogLog "$Role recovery started (launcher pid $($start.ProcessId)); awaiting health confirmation"
        } else {
            $State.Status = 'RecoveryFailed'
            $State.BackoffUntil = (Get-Date).AddSeconds($RecoveryBackoffSeconds)
            Write-WatchdogLog "$Role RECOVERY_FAILED: CIM create not accepted ($($start.Message)); logs $($start.LogPaths -join ',')"
        }
    } finally {
        Release-ExclusiveLock $opLock
    }
}

# ------------------------------------------------------------------- start --

$wdLock = Enter-ExclusiveLock -Path $Context.WatchdogLockPath -WaitTimeoutSeconds 0 -Purpose 'watchdog-lifetime'
if (-not $wdLock.Acquired) {
    Write-WatchdogLog "another watchdog already holds the lifetime lock ($($wdLock.Status)); exiting without starting components"
    exit 0
}

try {
    Set-Content -LiteralPath $Context.WatchdogPidPath -Value $PID -Encoding ASCII
    Write-WatchdogLog "watchdog started (pid $PID), interval ${IntervalSeconds}s, root $($Context.RootPath)"
    Write-PlatformLog -Context $Context -Source 'Watchdog' -Action 'watchdog-start' -Component 'Watchdog' -Result 'ok' -Message "pid $PID"

    $states = @{ Backend = (New-ComponentState); Frontend = (New-ComponentState) }

    while ($true) {
        foreach ($role in @('Backend', 'Frontend')) {
            $state = $states[$role]
            try {
                $health = Test-ComponentHealth $role
                if ($health.Healthy) {
                    if ($state.Status -ne 'Healthy') {
                        Write-WatchdogLog "$role healthy again (previous state $($state.Status))"
                        if ($state.Status -eq 'Recovering') {
                            Write-PlatformLog -Context $Context -Source 'Watchdog' -Action 'recover' -Component $role -Result 'ok' -Message 'health confirmed after recovery'
                        }
                    }
                    $state.Status = 'Healthy'
                    $state.Failures = 0
                    $state.Note = ''
                    continue
                }

                $state.Failures++

                if ($state.Status -eq 'Recovering') {
                    $elapsed = ((Get-Date) - $state.RecoveryStartedAt).TotalSeconds
                    if ($elapsed -ge $RecoveryVerifySeconds) {
                        $state.Status = 'RecoveryFailed'
                        $state.BackoffUntil = (Get-Date).AddSeconds($RecoveryBackoffSeconds)
                        Write-WatchdogLog "$role RECOVERY_FAILED: still unhealthy ${elapsed}s after recovery start"
                        Write-PlatformLog -Context $Context -Source 'Watchdog' -Action 'recover' -Component $role -Result 'RECOVERY_FAILED' -Message "unhealthy after ${elapsed}s"
                    }
                    continue
                }

                if ($state.Status -eq 'RecoveryFailed') {
                    if ((Get-Date) -lt $state.BackoffUntil) { continue }
                    Write-WatchdogLog "$role backoff finished, re-evaluating"
                    $state.Status = 'Suspect'
                    $state.Failures = 3
                }

                if ($state.Status -eq 'Blocked') {
                    $block = Get-RecoveryBlockReason $role
                    if ($block) {
                        if ($block -ne $state.Note) {
                            Write-WatchdogLog "$role still blocked: $block"
                            $state.Note = $block
                        }
                        continue
                    }
                    Write-WatchdogLog "$role blocking condition cleared, re-evaluating recovery"
                    $state.Status = 'Suspect'
                    $state.Failures = 3
                }

                if ($state.Failures -lt 3) {
                    Write-WatchdogLog "$role health failed ($($state.Failures)/3)"
                    $state.Status = 'Suspect'
                    continue
                }

                Write-WatchdogLog "$role health failed 3 times, evaluating recovery"
                Invoke-ComponentRecovery -Role $role -State $state
            } catch {
                # a single probe/recovery error must never kill the supervisor loop
                Write-WatchdogLog "$role loop error: $($_.Exception.Message)"
            }
        }
        Start-Sleep -Seconds $IntervalSeconds
    }
} finally {
    Release-ExclusiveLock $wdLock
}
