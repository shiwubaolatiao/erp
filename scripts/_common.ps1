# LMG ERP v2 - shared platform operations primitives (_common.ps1)
# ASCII only. Compatible with Windows PowerShell 5.1.
#
# Single owner of: context, results, locks, process identity, port ownership,
# CIM detached creation, component start/stop, health probes, diagnostics.
# platform.ps1 and watchdog.ps1 MUST reuse these functions instead of
# re-implementing identity / kill / probe rules.
#
# Hard rules:
#   * only ports 8020 / 5180 are managed; legacy ports are never touched
#   * PID / process name / port alone never proves ownership
#   * long-lived processes are created through CIM Win32_Process.Create only
#   * insufficient evidence always yields Unknown and never a kill

# ---------------------------------------------------------------- context ---

function New-PlatformContext {
    param([Parameter(Mandatory = $true)][string]$Root)

    $full = [System.IO.Path]::GetFullPath($Root)
    if ($full.EndsWith('\')) { $full = $full.TrimEnd('\') }
    foreach ($dir in @('backend', 'frontend', 'scripts')) {
        if (-not (Test-Path (Join-Path $full $dir))) {
            throw "invalid project root (missing '$dir'): $full"
        }
    }
    $logDir = Join-Path $full 'logs'
    if (-not (Test-Path $logDir)) {
        New-Item -ItemType Directory -Force -Path $logDir | Out-Null
    }

    return [PSCustomObject]@{
        RootPath            = $full
        BackendDirectory    = Join-Path $full 'backend'
        FrontendDirectory   = Join-Path $full 'frontend'
        ScriptsDirectory    = Join-Path $full 'scripts'
        LogDirectory        = $logDir
        BackendPort         = 8020
        FrontendPort        = 5180
        BackendScriptPath   = Join-Path $full 'backend\run.py'
        FrontendScriptPath  = Join-Path $full 'frontend\node_modules\vite\bin\vite.js'
        WatchdogScriptPath  = Join-Path $full 'scripts\watchdog.ps1'
        WatchdogPidPath     = Join-Path $logDir 'watchdog.pid'
        BackendPidPath      = Join-Path $logDir 'backend.pid'
        FrontendPidPath     = Join-Path $logDir 'frontend.pid'
        OperationLockPath   = Join-Path $logDir 'platform.operation.lock'
        WatchdogLockPath    = Join-Path $logDir 'watchdog.lock'
        PlatformLogPath     = Join-Path $logDir 'platform.log'
        WatchdogLogPath     = Join-Path $logDir 'watchdog.log'
        ReadyBudgetSeconds  = 180
        StopBudgetSeconds   = 15
        StatusBudgetSeconds = 30
        ProbeTimeoutSeconds = 5
        ReadyPollSeconds    = 2
        DiagnosticLogLines  = 10
        WatchdogLogLines    = 400
    }
}

function New-OperationResult {
    param(
        [string]$Stage = '',
        [string]$FailureCategory = '',
        [int]$ExitCode = 0,
        [bool]$Succeeded = $true,
        [bool]$AlreadyInTargetState = $false,
        [bool]$PartialFailure = $false,
        [string]$Message = '',
        [string]$NextAction = '',
        [string[]]$LogPaths = @()
    )
    return [PSCustomObject]@{
        Succeeded            = $Succeeded
        AlreadyInTargetState = $AlreadyInTargetState
        PartialFailure       = $PartialFailure
        Stage                = $Stage
        FailureCategory      = $FailureCategory
        ExitCode             = $ExitCode
        Message              = $Message
        NextAction           = $NextAction
        LogPaths             = $LogPaths
        Details              = @()
    }
}

function Add-ResultDetail {
    param([Parameter(Mandatory = $true)]$Result, [string]$Line)
    if ($null -ne $Line -and $Line -ne '') { $Result.Details += $Line }
}

# -------------------------------------------------------------- redaction ---

function Protect-SensitiveText {
    param([string]$Text)
    if ($null -eq $Text) { return '' }
    $t = $Text
    # key=value style secrets (JWT_SECRET / DB_PASSWORD / *TOKEN* / *SECRET* ...)
    $t = [regex]::Replace($t, '(?i)\b([A-Z0-9_]*(?:PASSWORD|SECRET|TOKEN|API_KEY)[A-Z0-9_]*)\s*[=:]\s*("[^"]*"|''[^'']*''|[^\s,;]+)', '$1=[REDACTED]')
    # credentials inside URLs  scheme://user:password@host
    $t = [regex]::Replace($t, '(?i)([a-z][a-z0-9+.\-]*://[^/\s:@]+:)[^@/\s]+(@)', '$1[REDACTED]$2')
    # bearer tokens
    $t = [regex]::Replace($t, '(?i)\bBearer\s+[A-Za-z0-9_\-\.=\+/]{8,}', 'Bearer [REDACTED]')
    return $t
}

# ------------------------------------------------------------------ logging -

function Write-PlatformLog {
    param(
        [Parameter(Mandatory = $true)]$Context,
        [string]$Source = 'User',
        [string]$Action = '',
        [string]$Component = '',
        [string]$Result = '',
        [string]$Message = ''
    )
    try {
        $line = '[{0}] [{1}] action={2} component={3} result={4} {5}' -f `
            (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Source, $Action, $Component, $Result,
            (Protect-SensitiveText $Message)
        Add-Content -LiteralPath $Context.PlatformLogPath -Value $line -Encoding UTF8 -ErrorAction Stop
        $all = Get-Content -LiteralPath $Context.PlatformLogPath -ErrorAction SilentlyContinue
        if ($all -and $all.Count -gt $Context.WatchdogLogLines) {
            $all | Select-Object -Last $Context.WatchdogLogLines |
                Set-Content -LiteralPath $Context.PlatformLogPath -Encoding UTF8
        }
    } catch { }
}

function Get-DiagnosticLogExcerpt {
    param(
        [Parameter(Mandatory = $true)]$Context,
        [string]$Role = '',
        [int]$LineCount = 10
    )
    if ($LineCount -gt $Context.DiagnosticLogLines) { $LineCount = $Context.DiagnosticLogLines }
    $candidates = @()
    switch ($Role) {
        'Backend'  { $candidates = @('backend.*.err.log', 'backend.*.out.log') }
        'Frontend' { $candidates = @('frontend.*.err.log', 'frontend.*.out.log') }
        'Watchdog' { $candidates = @('watchdog.log') }
        default    { $candidates = @('platform.log') }
    }
    $files = @()
    foreach ($pat in $candidates) {
        $files += Get-ChildItem -LiteralPath $Context.LogDirectory -Filter $pat -File -ErrorAction SilentlyContinue
    }
    if ($Role -eq 'Watchdog') {
        $files = @(Get-Item -LiteralPath $Context.WatchdogLogPath -ErrorAction SilentlyContinue)
    }
    if ($Role -eq '') {
        $files = @(Get-Item -LiteralPath $Context.PlatformLogPath -ErrorAction SilentlyContinue)
    }
    $files = @($files | Where-Object { $null -ne $_ } | Sort-Object LastWriteTime -Descending)
    if ($files.Count -eq 0) {
        return [PSCustomObject]@{ Path = ''; Lines = @(); Available = $false; Note = 'no log file found for this component' }
    }
    $target = $files[0]
    $lines = @()
    try {
        $raw = Get-Content -LiteralPath $target.FullName -Tail $LineCount -ErrorAction Stop
        foreach ($l in $raw) { $lines += (Protect-SensitiveText $l) }
        return [PSCustomObject]@{ Path = $target.FullName; Lines = $lines; Available = $true; Note = '' }
    } catch {
        return [PSCustomObject]@{ Path = $target.FullName; Lines = @(); Available = $false; Note = 'log not readable' }
    }
}

# -------------------------------------------------------------------- locks -

function Enter-ExclusiveLock {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [int]$WaitTimeoutSeconds = 0,
        [string]$Purpose = 'operation'
    )
    $grant = [PSCustomObject]@{
        Acquired = $false; Status = 'Error'; Path = $Path; Stream = $null; Purpose = $Purpose; Message = ''
    }
    $deadline = (Get-Date).AddSeconds($WaitTimeoutSeconds)
    while ($true) {
        try {
            $stream = [System.IO.File]::Open($Path, [System.IO.FileMode]::OpenOrCreate,
                [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
            $grant.Stream = $stream
            $grant.Acquired = $true
            $grant.Status = 'Acquired'
            return $grant
        } catch [System.IO.IOException] {
            if ((Get-Date) -ge $deadline) {
                $grant.Status = 'Busy'
                $grant.Message = "another operation holds $Purpose lock"
                return $grant
            }
            Start-Sleep -Milliseconds 400
        } catch {
            $grant.Status = 'Error'
            $grant.Message = (Protect-SensitiveText $_.Exception.Message)
            return $grant
        }
    }
}

function Release-ExclusiveLock {
    param($Lock)
    try {
        if ($null -ne $Lock -and $null -ne $Lock.Stream) { $Lock.Stream.Dispose() }
    } catch { }
    if ($null -ne $Lock) { $Lock.Acquired = $false }
}

# -------------------------------------------------------- process identity --

function Get-ProcessEvidence {
    param([Parameter(Mandatory = $true)][int]$ProcessId)
    $ev = [PSCustomObject]@{
        ProcessId = $ProcessId; ParentProcessId = 0; CreationDate = $null
        ExecutablePath = ''; CommandLine = ''; Available = $false; NotRunning = $false; Reason = ''
    }
    try {
        $p = Get-CimInstance -ClassName Win32_Process -Filter ("ProcessId={0}" -f $ProcessId) -ErrorAction Stop
        if ($null -eq $p) {
            $ev.NotRunning = $true
            $ev.Reason = 'process not found'
            return $ev
        }
        $ev.Available = $true
        $ev.ParentProcessId = [int]$p.ParentProcessId
        $ev.CreationDate = $p.CreationDate
        $ev.ExecutablePath = [string]$p.ExecutablePath
        $ev.CommandLine = [string]$p.CommandLine
        if ([string]::IsNullOrWhiteSpace($ev.CommandLine)) {
            $ev.Available = $false
            $ev.Reason = 'command line not readable'
        }
        return $ev
    } catch {
        $ev.Reason = (Protect-SensitiveText $_.Exception.Message)
        return $ev
    }
}

function Resolve-PathText {
    param([string]$Text)
    if (-not $Text) { return $Text }
    $t = $Text.Replace('/', '\')
    # npm launches vite through "node_modules\.bin\\..\vite\bin\vite.js";
    # normalize separators and relative segments before comparing identities.
    $t = [regex]::Replace($t, '\\{2,}', '\')
    $t = [regex]::Replace($t, '\\\.\\', '\')
    $pattern = '\\[^\\"]+\\\.\.\\'
    $guard = 0
    while ($guard -lt 20 -and [regex]::IsMatch($t, $pattern)) {
        $next = [regex]::Replace($t, $pattern, '\')
        if ($next -eq $t) { break }
        $t = $next
        $guard++
    }
    return $t
}

function Test-CommandLineContainsPath {
    param([string]$CommandLine, [string]$Path)
    if ([string]::IsNullOrWhiteSpace($CommandLine) -or [string]::IsNullOrWhiteSpace($Path)) { return $false }
    $cmd = Resolve-PathText $CommandLine
    $p = Resolve-PathText $Path
    $idx = $cmd.IndexOf($p, [System.StringComparison]::OrdinalIgnoreCase)
    while ($idx -ge 0) {
        $after = $idx + $p.Length
        if ($after -ge $cmd.Length) { return $true }
        $c = $cmd[$after]
        if ($c -eq '"' -or $c -eq "'" -or [char]::IsWhiteSpace($c)) { return $true }
        $idx = $cmd.IndexOf($p, $after, [System.StringComparison]::OrdinalIgnoreCase)
    }
    return $false
}

function Test-ProjectProcess {
    param(
        $Evidence,
        [Parameter(Mandatory = $true)][ValidateSet('Watchdog', 'Backend', 'Frontend')][string]$Role,
        [Parameter(Mandatory = $true)]$Context
    )
    $verdict = [PSCustomObject]@{ Verdict = 'Unknown'; Reason = ''; ProcessId = 0; Name = '' }
    if ($null -eq $Evidence) { $verdict.Reason = 'no evidence'; return $verdict }
    $verdict.ProcessId = $Evidence.ProcessId
    if ($Evidence.NotRunning) { $verdict.Verdict = 'NotRunning'; $verdict.Reason = 'process not found'; return $verdict }
    if (-not $Evidence.Available) { $verdict.Verdict = 'Unknown'; $verdict.Reason = $Evidence.Reason; return $verdict }

    $exe = ''
    try { $exe = [System.IO.Path]::GetFileName([string]$Evidence.ExecutablePath).ToLowerInvariant() } catch { }
    $verdict.Name = $exe
    $cmd = [string]$Evidence.CommandLine

    switch ($Role) {
        'Watchdog' {
            if ($exe -notin @('powershell.exe', 'pwsh.exe')) {
                $verdict.Verdict = 'External'; $verdict.Reason = "not a PowerShell host ($exe)"; return $verdict
            }
            if (Test-CommandLineContainsPath $cmd $Context.WatchdogScriptPath) {
                $verdict.Verdict = 'Owned'; return $verdict
            }
        }
        'Backend' {
            if ($exe -notlike 'python*') {
                $verdict.Verdict = 'External'; $verdict.Reason = "not a Python host ($exe)"; return $verdict
            }
            if (Test-CommandLineContainsPath $cmd $Context.BackendScriptPath) {
                $verdict.Verdict = 'Owned'; return $verdict
            }
        }
        'Frontend' {
            if ($exe -ne 'node.exe') {
                $verdict.Verdict = 'External'; $verdict.Reason = "not a Node host ($exe)"; return $verdict
            }
            if (Test-CommandLineContainsPath $cmd $Context.FrontendScriptPath) {
                $verdict.Verdict = 'Owned'; return $verdict
            }
        }
    }
    $verdict.Verdict = 'External'
    $verdict.Reason = "command line does not reference this project's $Role entry point"
    return $verdict
}

function Get-PortOwnership {
    param(
        [Parameter(Mandatory = $true)][int]$Port,
        [Parameter(Mandatory = $true)][ValidateSet('Backend', 'Frontend')][string]$ExpectedRole,
        [Parameter(Mandatory = $true)]$Context
    )
    $result = [PSCustomObject]@{ Port = $Port; Status = 'Unknown'; Owners = @(); Detail = '' }
    $listeners = @()
    try {
        # no listening socket is a normal state, not an error: do NOT use Stop here
        $listeners = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
    } catch {
        $result.Detail = 'port query failed'
        return $result
    }
    if ($listeners.Count -eq 0) { $result.Status = 'Free'; return $result }

    $pids = @($listeners | Select-Object -ExpandProperty OwningProcess -Unique)
    $conflict = $false
    $unknown = $false
    foreach ($procId in $pids) {
        $ev = Get-ProcessEvidence -ProcessId $procId
        $v = Test-ProjectProcess -Evidence $ev -Role $ExpectedRole -Context $Context
        $result.Owners += [PSCustomObject]@{
            ProcessId = $procId; Verdict = $v.Verdict; Reason = $v.Reason; Name = $v.Name
        }
        if ($v.Verdict -eq 'Unknown') { $unknown = $true }
        elseif ($v.Verdict -ne 'Owned') { $conflict = $true }
    }
    if ($conflict) {
        $result.Status = 'Conflict'
        $result.Detail = 'port is held by process(es) not owned by this project'
    } elseif ($unknown) {
        $result.Status = 'Unknown'
        $result.Detail = 'insufficient evidence to confirm port ownership'
    } else {
        $result.Status = 'OwnedListening'
    }
    return $result
}

function Get-WatchdogState {
    param([Parameter(Mandatory = $true)]$Context)
    $state = [PSCustomObject]@{
        RecordState = 'NoRecord'; ProcessId = 0; Reason = ''
        Instances = @(); ValidCount = 0
    }
    if (Test-Path $Context.WatchdogPidPath) {
        $raw = (Get-Content -LiteralPath $Context.WatchdogPidPath -ErrorAction SilentlyContinue | Select-Object -First 1)
        $pidValue = 0
        if ([int]::TryParse([string]$raw, [ref]$pidValue) -and $pidValue -gt 0) {
            $state.ProcessId = $pidValue
            $ev = Get-ProcessEvidence -ProcessId $pidValue
            $v = Test-ProjectProcess -Evidence $ev -Role 'Watchdog' -Context $Context
            switch ($v.Verdict) {
                'Owned'      { $state.RecordState = 'Valid' }
                'NotRunning' { $state.RecordState = 'Stale'; $state.Reason = 'recorded pid is not running' }
                'Unknown'    { $state.RecordState = 'Unknown'; $state.Reason = $v.Reason }
                default      { $state.RecordState = 'IdentityMismatch'; $state.Reason = $v.Reason }
            }
        } else {
            $state.RecordState = 'Stale'
            $state.Reason = 'pid file is not a valid pid'
        }
    }
    $hosts = @(Get-CimInstance -ClassName Win32_Process -Filter "Name='powershell.exe' OR Name='pwsh.exe'" -ErrorAction SilentlyContinue)
    foreach ($h in $hosts) {
        if (Test-CommandLineContainsPath ([string]$h.CommandLine) $Context.WatchdogScriptPath) {
            $state.Instances += [int]$h.ProcessId
        }
    }
    $state.ValidCount = $state.Instances.Count
    return $state
}

# --------------------------------------------------------- CIM and changes --

function Start-DetachedProcess {
    param(
        [Parameter(Mandatory = $true)][string]$CommandLine,
        [Parameter(Mandatory = $true)][string]$WorkingDirectory
    )
    $out = [PSCustomObject]@{ Accepted = $false; ReturnValue = -1; ProcessId = 0; Message = '' }
    try {
        # some CIM providers do not materialize ShowWindow on a blank instance,
        # so pass it at creation time instead of assigning afterwards.
        $startup = New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly `
            -Property @{ ShowWindow = [uint16]0 } -ErrorAction Stop
        $r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
            CommandLine = $CommandLine; CurrentDirectory = $WorkingDirectory; ProcessStartupInformation = $startup
        } -ErrorAction Stop
        $out.ReturnValue = [int]$r.ReturnValue
        $out.ProcessId = [int]$r.ProcessId
        $out.Accepted = ($r.ReturnValue -eq 0)
        if (-not $out.Accepted) { $out.Message = "CIM create returned $($r.ReturnValue)" }
        return $out
    } catch {
        $out.Message = (Protect-SensitiveText $_.Exception.Message)
        return $out
    }
}

function Get-ComponentLaunchSpec {
    param(
        [Parameter(Mandatory = $true)][ValidateSet('Watchdog', 'Backend', 'Frontend')][string]$Role,
        [Parameter(Mandatory = $true)]$Context
    )
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
    switch ($Role) {
        'Watchdog' {
            $cmd = 'powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}" -Root "{1}"' -f `
                $Context.WatchdogScriptPath, $Context.RootPath
            return [PSCustomObject]@{ CommandLine = $cmd; WorkingDirectory = $Context.RootPath; Logs = @($Context.WatchdogLogPath) }
        }
        'Backend' {
            $py = (Get-Command python.exe -ErrorAction SilentlyContinue).Source
            if (-not $py) { $py = 'python.exe' }
            $outLog = Join-Path $Context.LogDirectory ("backend.{0}.out.log" -f $stamp)
            $errLog = Join-Path $Context.LogDirectory ("backend.{0}.err.log" -f $stamp)
            $inner = 'set ERP_PORT={0}&& "{1}" "{2}" >>"{3}" 2>>"{4}"' -f `
                $Context.BackendPort, $py, $Context.BackendScriptPath, $outLog, $errLog
            $cmd = 'cmd.exe /d /s /c "{0}"' -f $inner
            return [PSCustomObject]@{ CommandLine = $cmd; WorkingDirectory = $Context.BackendDirectory; Logs = @($outLog, $errLog) }
        }
        'Frontend' {
            $npm = (Get-Command npm.cmd -ErrorAction SilentlyContinue).Source
            if (-not $npm) { $npm = 'npm.cmd' }
            $outLog = Join-Path $Context.LogDirectory ("frontend.{0}.out.log" -f $stamp)
            $errLog = Join-Path $Context.LogDirectory ("frontend.{0}.err.log" -f $stamp)
            $inner = '"{0}" run dev -- --host 0.0.0.0 --port {1} --strictPort >>"{2}" 2>>"{3}"' -f `
                $npm, $Context.FrontendPort, $outLog, $errLog
            $cmd = 'cmd.exe /d /s /c "{0}"' -f $inner
            return [PSCustomObject]@{ CommandLine = $cmd; WorkingDirectory = $Context.FrontendDirectory; Logs = @($outLog, $errLog) }
        }
    }
    return $null
}

function Start-PlatformComponent {
    param(
        [Parameter(Mandatory = $true)][ValidateSet('Watchdog', 'Backend', 'Frontend')][string]$Role,
        [Parameter(Mandatory = $true)]$Context,
        [string]$Source = 'User'
    )
    $spec = Get-ComponentLaunchSpec -Role $Role -Context $Context
    $r = Start-DetachedProcess -CommandLine $spec.CommandLine -WorkingDirectory $spec.WorkingDirectory
    Write-PlatformLog -Context $Context -Source $Source -Action 'start' -Component $Role `
        -Result ($(if ($r.Accepted) { 'accepted' } else { 'failed' })) -Message $r.Message
    return [PSCustomObject]@{
        Role = $Role; Accepted = $r.Accepted; ProcessId = $r.ProcessId
        LogPaths = $spec.Logs; Message = $r.Message
        Category = $(if ($r.Accepted) { '' } else { 'COMPONENT_START_FAILED' })
    }
}

function Get-ComponentCandidates {
    param(
        [Parameter(Mandatory = $true)][ValidateSet('Backend', 'Frontend')][string]$Role,
        [Parameter(Mandatory = $true)]$Context
    )
    $found = @{}
    $anchor = $(if ($Role -eq 'Backend') { $Context.BackendScriptPath } else { $Context.FrontendScriptPath })
    $names = $(if ($Role -eq 'Backend') { @('python.exe', 'pythonw.exe', 'cmd.exe') } else { @('node.exe', 'cmd.exe', 'npm.cmd') })
    foreach ($n in $names) {
        $procs = @(Get-CimInstance -ClassName Win32_Process -Filter ("Name='{0}'" -f $n) -ErrorAction SilentlyContinue)
        foreach ($p in $procs) {
            if (Test-CommandLineContainsPath ([string]$p.CommandLine) $anchor) {
                $found[[int]$p.ProcessId] = $true
            }
        }
    }
    $port = $(if ($Role -eq 'Backend') { $Context.BackendPort } else { $Context.FrontendPort })
    $listeners = @(Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)
    foreach ($l in $listeners) { $found[[int]$l.OwningProcess] = $true }
    return @($found.Keys)
}

function Stop-PlatformComponent {
    param(
        [Parameter(Mandatory = $true)][ValidateSet('Backend', 'Frontend')][string]$Role,
        [Parameter(Mandatory = $true)]$Context,
        [string]$Source = 'User'
    )
    $res = [PSCustomObject]@{
        Role = $Role; Stopped = @(); Skipped = @(); Succeeded = $true; Message = ''
    }
    $candidates = Get-ComponentCandidates -Role $Role -Context $Context
    foreach ($procId in $candidates) {
        $ev = Get-ProcessEvidence -ProcessId $procId
        if ($ev.NotRunning) { continue }
        # wrapper processes (cmd/npm) are matched by project path too, but the
        # role verdict is decided against the real component entry point.
        $v = Test-ProjectProcess -Evidence $ev -Role $Role -Context $Context
        $isWrapper = $false
        if ($v.Verdict -ne 'Owned') {
            $exe = ''
            try { $exe = [System.IO.Path]::GetFileName([string]$ev.ExecutablePath).ToLowerInvariant() } catch { }
            $anchor = $(if ($Role -eq 'Backend') { $Context.BackendScriptPath } else { $Context.FrontendScriptPath })
            if ($exe -in @('cmd.exe', 'npm.cmd') -and (Test-CommandLineContainsPath $ev.CommandLine $anchor)) {
                $isWrapper = $true
            }
        }
        if ($v.Verdict -eq 'Owned' -or $isWrapper) {
            try {
                Stop-Process -Id $procId -Force -ErrorAction Stop
                $res.Stopped += $procId
            } catch {
                $res.Succeeded = $false
                $res.Skipped += [PSCustomObject]@{ ProcessId = $procId; Reason = (Protect-SensitiveText $_.Exception.Message) }
            }
        } else {
            $res.Succeeded = $false
            $res.Skipped += [PSCustomObject]@{ ProcessId = $procId; Reason = "$($v.Verdict): $($v.Reason)" }
        }
    }
    Start-Sleep -Milliseconds 800
    Write-PlatformLog -Context $Context -Source $Source -Action 'stop' -Component $Role `
        -Result ($(if ($res.Succeeded) { 'ok' } else { 'partial' })) `
        -Message ("stopped=" + ($res.Stopped -join ',') + " skipped=" + ($res.Skipped.Count))
    return $res
}

# ------------------------------------------------------------- health probes -

function Test-BackendHealth {
    param([Parameter(Mandatory = $true)]$Context, [int]$TimeoutSeconds = 5)
    if ($TimeoutSeconds -lt 1) { $TimeoutSeconds = 1 }
    if ($TimeoutSeconds -gt 5) { $TimeoutSeconds = 5 }
    $r = [PSCustomObject]@{ Component = 'Backend'; Healthy = $false; IdentityMatched = $false; Stage = 'http'; Error = ''; ElapsedMs = 0 }
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    try {
        $resp = Invoke-WebRequest -Uri ("http://127.0.0.1:{0}/health" -f $Context.BackendPort) `
            -TimeoutSec $TimeoutSeconds -UseBasicParsing -ErrorAction Stop
        if ($resp.StatusCode -ne 200) {
            $r.Error = "http $($resp.StatusCode)"
        } else {
            $json = $resp.Content | ConvertFrom-Json
            $r.IdentityMatched = ($json.app -eq 'LMG-ERP' -and $json.status -eq 'ok')
            $r.Healthy = $r.IdentityMatched
            if (-not $r.Healthy) { $r.Error = 'http 200 but application identity mismatch'; $r.Stage = 'identity' }
        }
    } catch {
        $r.Error = (Protect-SensitiveText $_.Exception.Message)
    }
    $sw.Stop()
    $r.ElapsedMs = [int]$sw.ElapsedMilliseconds
    return $r
}

function Test-FrontendHealth {
    param([Parameter(Mandatory = $true)]$Context, [int]$TimeoutSeconds = 5)
    if ($TimeoutSeconds -lt 1) { $TimeoutSeconds = 1 }
    if ($TimeoutSeconds -gt 5) { $TimeoutSeconds = 5 }
    $r = [PSCustomObject]@{ Component = 'Frontend'; Healthy = $false; IdentityMatched = $false; Stage = 'http'; Error = ''; ElapsedMs = 0 }
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    try {
        $resp = Invoke-WebRequest -Uri ("http://127.0.0.1:{0}/" -f $Context.FrontendPort) `
            -TimeoutSec $TimeoutSeconds -UseBasicParsing -ErrorAction Stop
        if ($resp.StatusCode -ne 200) {
            $r.Error = "http $($resp.StatusCode)"
        } elseif ($resp.Content -match 'id\s*=\s*"root"') {
            $r.IdentityMatched = $true
            $r.Healthy = $true
        } else {
            $r.Error = 'http 200 but no id="root" element'
            $r.Stage = 'identity'
        }
    } catch {
        $r.Error = (Protect-SensitiveText $_.Exception.Message)
    }
    $sw.Stop()
    $r.ElapsedMs = [int]$sw.ElapsedMilliseconds
    return $r
}

function Test-DatabaseDependency {
    param(
        [Parameter(Mandatory = $true)]$Context,
        [int]$TimeoutSeconds = 5,
        [switch]$EnsureDatabase
    )
    if ($TimeoutSeconds -lt 1) { $TimeoutSeconds = 1 }
    if ($TimeoutSeconds -gt 5) { $TimeoutSeconds = 5 }
    $r = [PSCustomObject]@{ Healthy = $false; Category = 'DB_UNKNOWN'; Target = ''; Message = ''; ElapsedMs = 0; RawText = '' }
    $py = @'
import sys
from app.core.config import settings
target = '%s:%s/%s' % (settings.DB_HOST, settings.DB_PORT, settings.DB_NAME)
print('TARGET=' + target)
try:
    import pymysql
    conn = pymysql.connect(host=settings.DB_HOST, port=settings.DB_PORT,
                           user=settings.DB_USER, password=settings.DB_PASSWORD,
                           database=settings.DB_NAME, charset='utf8mb4',
                           connect_timeout=TMO, read_timeout=TMO, write_timeout=TMO)
    with conn.cursor() as cur:
        cur.execute('SELECT 1')
        cur.fetchone()
    conn.close()
    print('RESULT=OK')
except Exception as exc:
    code = exc.args[0] if getattr(exc, 'args', None) else None
    print('RESULT=FAIL')
    print('CODE=%s' % code)
'@
    $py = $py.Replace('TMO', [string]$TimeoutSeconds)
    $ensure = @'
from app.core.database import ensure_database
ensure_database()
'@
    if ($EnsureDatabase) { $py = $ensure + $py }
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    # one retry: a transient read-timeout / lost-connection must not be reported
    # as a hard dependency failure. Each single attempt stays within TimeoutSeconds.
    for ($attempt = 1; $attempt -le 2; $attempt++) {
        try {
            # settings import requires the backend directory on sys.path
            Push-Location $Context.BackendDirectory
            try {
                $out = & python.exe -c $py 2>&1
            } finally {
                Pop-Location
            }
            $text = ($out | Out-String)
            $r.RawText = $text
            foreach ($line in ($text -split "`r?`n")) {
                if ($line.StartsWith('TARGET=')) { $r.Target = $line.Substring(7) }
            }
            if ($text -match 'RESULT=OK') {
                $r.Healthy = $true
                $r.Category = 'OK'
                $r.Message = ''
            } else {
                $code = ''
                if ($text -match 'CODE=(\d+)') { $code = $Matches[1] }
                if ($code -in @('1044', '1045', '1049')) {
                    $r.Category = 'DB_AUTH_OR_CONFIG'
                    $r.Message = 'database authentication/permission/configuration error'
                } elseif ($code -in @('2002', '2003', '2005', '2006', '2013') -or
                          $text -match '10060|10061|timed out|Name or service') {
                    $r.Category = 'DB_NETWORK_UNREACHABLE'
                    $r.Message = 'database network unreachable'
                } else {
                    $r.Category = 'DB_UNKNOWN'
                    $r.Message = 'database probe failed'
                }
            }
        } catch {
            $r.Category = 'DB_UNKNOWN'
            $r.Message = (Protect-SensitiveText $_.Exception.Message)
        }
        if ($r.Healthy) { break }
        # authentication/configuration errors are deterministic: retrying is pointless
        if ($r.Category -eq 'DB_AUTH_OR_CONFIG') { break }
        if ($attempt -eq 1) { Start-Sleep -Milliseconds 500 }
    }
    $sw.Stop()
    $r.ElapsedMs = [int]$sw.ElapsedMilliseconds
    return $r
}