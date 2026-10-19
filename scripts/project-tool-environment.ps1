<#
.SYNOPSIS
  Process-scoped environment helper that gathers the pytest launcher's
  quality-gate tool I/O below one runtime root. The default root is ignored
  and git-untracked; an explicit -RuntimeRoot is not checked for either
  property (see docs/agents/quality-gates.md).

.DESCRIPTION
  This is the shared implementation behind the canonical pytest launcher
  (scripts/invoke-pytest.ps1), split into its own dot-sourceable file so its
  functions can also be driven directly by tests/test_project_tool_environment.py
  without going through a full launcher subprocess. It is not a general-purpose
  environment-isolation mechanism ready for an arbitrary second caller: two of
  the seven managed leaves (pytest, ledger-collection) and the lock functions
  (Enter-ProjectPytestLock / Exit-ProjectPytestLock, writing pytest.lock) are
  pytest-specific, with no parameter to rename or skip them. Generalizing this
  for a second consumer would mean parameterizing those pytest-specific leaf
  names and the lock identity, not just calling the functions as they stand
  today.

  The default runtime root is "<repository>/.project-runtime". An explicit
  absolute -RuntimeRoot may override it for a future checkout whose repository
  volume is unsuitable.

  A runtime root whose volume cannot be established at all -- every UNC path,
  or a dangling reparse point whose target does not exist -- is rejected with
  exit 18 before any directory is created or any child process is launched: a
  guard that cannot see the destination must not wave the caller through.
  This also covers the default root, whose `.project-runtime` directory or
  leaves could themselves be a dangling junction.

  Known boundary of -RuntimeRoot: the pytest lock lives on the "locks" leaf
  BELOW the runtime root, so two launches using different runtime roots take
  different locks and can run concurrently. The launcher does move pytest's
  own basetemp with the override (see scripts/invoke-pytest.ps1), so the
  remaining gap is lock scope alone. No canonical route, the release script,
  or any documented workflow passes it; only the test suite does.

  Seven distinct leaves are managed below the runtime root: python-temp,
  pytest, ledger-collection, uv-cache, xdg-cache, pre-commit, and locks. Before
  a child process starts, TEMP/TMP/TMPDIR are pointed at python-temp,
  UV_CACHE_DIR at uv-cache, XDG_CACHE_HOME at xdg-cache, and PRE_COMMIT_HOME at
  pre-commit, and PYTEST_ADDOPTS is removed outright so an inherited value
  cannot append arguments to the launcher's exact pytest argument vector.
  Every value is set with process scope only (`$env:` / `Env:`
  provider assignment) -- this file never calls
  [Environment]::SetEnvironmentVariable with the 'User' or 'Machine' target,
  and never calls setx. The caller's working directory and every managed
  variable are captured before mutation and restored in a `finally` block on
  every exit path, including an absent variable being restored to absence.

  Invoke-InProjectToolEnvironment also acquires the cross-process pytest lock
  (a FileShare.None handle on the "locks" leaf, see Enter-ProjectPytestLock)
  after the leaves exist but before any environment mutation or child launch,
  and releases it in the same `finally` block that restores the snapshot.
  Lock contention exits 17 immediately, without waiting and without starting
  the child process. The lock file may remain on disk after a crashed
  process; ownership is the open handle, not file existence, so a stale file
  never blocks a later run.

  Invoke-InProjectToolEnvironment accepts one optional extension point,
  -PreLaunchValidation (see its own parameter doc), invoked after the lock is
  held and immediately before the child launches. This does not loosen the
  "not general-purpose" warning above: the hook is validation-only (no
  parameters passed in, no return value consumed, nothing it does is used by
  this function), so a second consumer wanting the pytest-specific leaves or
  lock identity renamed or skipped still cannot get that from this parameter
  -- it is the one deliberately narrow seam this file exposes, not a general
  "run arbitrary work inside the lock" facility.
#>

$script:ProjectToolEnvironmentManagedVariables = @(
    'TEMP', 'TMP', 'TMPDIR', 'UV_CACHE_DIR', 'XDG_CACHE_HOME', 'PRE_COMMIT_HOME'
)

# Not pointed at a leaf -- removed outright before the child starts. pytest
# appends PYTEST_ADDOPTS to its command line, so an inherited value could add
# `--basetemp=C:\...` or `-c` and defeat both the isolation contract and the
# launcher's exact-argument-vector contract. Snapshotted and restored on the
# same paths as the managed variables above.
$script:ProjectToolEnvironmentClearedVariables = @('PYTEST_ADDOPTS')

$script:ProjectToolEnvironmentLeafNames = @(
    'python-temp', 'pytest', 'ledger-collection', 'uv-cache', 'xdg-cache', 'pre-commit', 'locks'
)

function Get-ProjectRuntimeRoot {
    <#
    .SYNOPSIS
      Resolve the normalized, absolute project runtime root.

    .DESCRIPTION
      Returns "<RepositoryRoot>/.project-runtime" unless an explicit
      -RuntimeRoot override is supplied, in which case that override must
      already be an absolute path and is returned normalized instead. This
      function performs no drive-safety check and no I/O.
    #>
    param(
        [Parameter(Mandatory)]
        [string]$RepositoryRoot,

        [string]$RuntimeRoot
    )

    $resolvedRepositoryRoot = (Resolve-Path -LiteralPath $RepositoryRoot).Path

    if ($RuntimeRoot) {
        # IsPathFullyQualified, not IsPathRooted: the latter accepts the
        # drive-relative "C:runtime" and the root-relative "\runtime", whose
        # GetFullPath expansion depends on the caller's current directory --
        # exactly what an absolute-path requirement must exclude.
        if (-not [IO.Path]::IsPathFullyQualified($RuntimeRoot)) {
            throw "RuntimeRoot override must be an absolute path: $RuntimeRoot"
        }
        return [IO.Path]::GetFullPath($RuntimeRoot)
    }

    return [IO.Path]::GetFullPath((Join-Path $resolvedRepositoryRoot '.project-runtime'))
}

if (-not ('Mailattic.VolumeIdentity' -as [type])) {
    # GetFinalPathNameByHandle is the only resolution that sees through every
    # indirect spelling of a volume at once -- a subst mapping, a junction, a
    # symlink, and an extended-length prefix all come back as the real
    # \\?\<drive>\... path. .NET's managed ResolveLinkTarget is not enough --
    # it does not see a subst mapping at all. The type test above keeps a
    # second dot-source within one process from recompiling; the one compile
    # per launcher process is an accepted cost, small beside the uv start-up
    # that follows it.
    Add-Type -Namespace Mailattic -Name VolumeIdentity -MemberDefinition @'
[DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
public static extern IntPtr CreateFileW(
    string lpFileName, uint dwDesiredAccess, uint dwShareMode, IntPtr lpSecurityAttributes,
    uint dwCreationDisposition, uint dwFlagsAndAttributes, IntPtr hTemplateFile);

[DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
public static extern uint GetFinalPathNameByHandleW(
    IntPtr hFile, System.Text.StringBuilder lpszFilePath, uint cchFilePath, uint dwFlags);

[DllImport("kernel32.dll", SetLastError = true)]
public static extern bool CloseHandle(IntPtr hObject);
'@
}

function Resolve-ProjectFinalPathName {
    <#
    .SYNOPSIS
      Return the real path Windows resolves a path to, or $null with the
      Win32 error code when it cannot be opened.

    .OUTPUTS
      [pscustomobject] with Path (the resolved `\\?\...` path, or $null) and
      ErrorCode (0 on success; 2 or 3 when the path simply does not exist).
    #>
    param(
        [Parameter(Mandatory)]
        [string]$Path
    )

    # FILE_READ_ATTRIBUTES only (0), full sharing (7), OPEN_EXISTING (3),
    # FILE_FLAG_BACKUP_SEMANTICS (0x02000000) so a directory may be opened.
    $handle = [Mailattic.VolumeIdentity]::CreateFileW(
        $Path, 0, 7, [IntPtr]::Zero, 3, 0x02000000, [IntPtr]::Zero)
    if ($handle -eq [IntPtr](-1)) {
        return [pscustomobject]@{
            Path = $null
            ErrorCode = [Runtime.InteropServices.Marshal]::GetLastWin32Error()
        }
    }
    try {
        $buffer = New-Object System.Text.StringBuilder 32768
        $length = [Mailattic.VolumeIdentity]::GetFinalPathNameByHandleW(
            $handle, $buffer, 32767, 0)
        if ($length -eq 0) {
            return [pscustomobject]@{
                Path = $null
                ErrorCode = [Runtime.InteropServices.Marshal]::GetLastWin32Error()
            }
        }
        return [pscustomobject]@{ Path = $buffer.ToString(); ErrorCode = 0 }
    } finally {
        [Mailattic.VolumeIdentity]::CloseHandle($handle) | Out-Null
    }
}

function Get-ProjectRuntimeRootRejectionReason {
    <#
    .SYNOPSIS
      Return $null when a normalized runtime root is safe to use, or a short
      reason phrase naming why it must be rejected.

    .DESCRIPTION
      The root itself usually does not exist yet at guard time and Windows
      resolves only existing paths, so this walks up to the nearest existing
      ancestor -- a directory that does not exist cannot itself be a reparse
      point, and any junction above it is resolved by the same call.

      Anything whose volume cannot be established is rejected, including
      every UNC path: a UNC final path name carries no drive letter, and
      reading an administrative share name as a drive would be interpretation
      rather than a check. A guard that cannot see does not wave the caller
      through.
    #>
    param(
        [Parameter(Mandatory)]
        [string]$RuntimeRoot
    )

    $unverifiable = 'its volume cannot be established'

    $candidate = $RuntimeRoot
    $resolved = $null
    while ($true) {
        $attempt = Resolve-ProjectFinalPathName -Path $candidate
        if ($attempt.Path) {
            $resolved = $attempt.Path
            break
        }
        # ERROR_FILE_NOT_FOUND / ERROR_PATH_NOT_FOUND are the only failures
        # that mean "keep walking up"; anything else (a permission denial, a
        # disconnected volume) is a guard that cannot see.
        if ($attempt.ErrorCode -ne 2 -and $attempt.ErrorCode -ne 3) {
            return $unverifiable
        }
        # A dangling junction reports the same two codes -- the entry exists,
        # only its target does not -- so "not found" alone would walk past it
        # to a safe parent and allow a reparse point whose volume nobody can
        # establish, and which may resolve later.
        try {
            $attributes = [IO.File]::GetAttributes($candidate)
            if ($attributes -band [IO.FileAttributes]::ReparsePoint) {
                return $unverifiable
            }
        } catch {
            # The entry genuinely does not exist; keep walking up.
        }
        $parent = [IO.Path]::GetDirectoryName($candidate)
        if (-not $parent -or $parent -eq $candidate) {
            return $unverifiable
        }
        $candidate = $parent
    }

    # Drop the extended-length prefix ahead of a drive letter; the lookahead
    # leaves '\\?\UNC\server\share' intact so it still reads as UNC below.
    $volumePath = $resolved -replace '^\\\\[?.]\\(?=[A-Za-z]:)', ''
    $rootDrive = [IO.Path]::GetPathRoot($volumePath)
    if (-not $rootDrive) {
        return $unverifiable
    }
    $rootDrive = $rootDrive.TrimEnd('\', '/').ToLowerInvariant()
    if ($rootDrive -notmatch '^[a-z]:$') {
        return $unverifiable
    }
    return $null
}

function Get-ProjectRuntimeLeaves {
    <#
    .SYNOPSIS
      Return the seven managed leaf paths below a normalized runtime root.
    #>
    param(
        [Parameter(Mandatory)]
        [string]$RuntimeRoot
    )

    $leaves = [ordered]@{}
    foreach ($name in $script:ProjectToolEnvironmentLeafNames) {
        $leaves[$name] = Join-Path $RuntimeRoot $name
    }
    return $leaves
}

function Initialize-ProjectRuntimeLeaves {
    <#
    .SYNOPSIS
      Create every managed leaf directory. Throws with the failing path on
      any creation failure; never falls back to a different location.
    #>
    param(
        [Parameter(Mandatory)]
        [System.Collections.Specialized.OrderedDictionary]$Leaves
    )

    foreach ($path in $Leaves.Values) {
        if (Test-Path -LiteralPath $path -PathType Leaf) {
            throw "Unable to create project runtime directory '$path': a file already exists at that path."
        }
        try {
            New-Item -ItemType Directory -Force -Path $path -ErrorAction Stop | Out-Null
        } catch {
            throw "Unable to create project runtime directory '$path': $($_.Exception.Message)"
        }
        if (-not (Test-Path -LiteralPath $path -PathType Container)) {
            throw "Unable to create project runtime directory '$path': the path is not a directory after creation."
        }
    }
}

function Get-ManagedEnvironmentSnapshot {
    <#
    .SYNOPSIS
      Capture the caller's working directory and the current state (present
      or absent, with value) of every managed environment variable.
    #>

    $variables = [ordered]@{}
    $allNames = $script:ProjectToolEnvironmentManagedVariables +
        $script:ProjectToolEnvironmentClearedVariables
    foreach ($name in $allNames) {
        $item = Get-Item -LiteralPath "Env:$name" -ErrorAction SilentlyContinue
        $variables[$name] = [pscustomobject]@{
            Present = [bool]$item
            Value = if ($item) { $item.Value } else { $null }
        }
    }

    return [pscustomobject]@{
        WorkingDirectory = (Get-Location).Path
        Variables = $variables
    }
}

function Set-ManagedEnvironment {
    <#
    .SYNOPSIS
      Point every managed environment variable at its runtime leaf. Process
      scope only -- never writes User- or Machine-scoped state.
    #>
    param(
        [Parameter(Mandatory)]
        [System.Collections.Specialized.OrderedDictionary]$Leaves
    )

    $pythonTemp = $Leaves['python-temp']
    $env:TEMP = $pythonTemp
    $env:TMP = $pythonTemp
    $env:TMPDIR = $pythonTemp
    $env:UV_CACHE_DIR = $Leaves['uv-cache']
    $env:XDG_CACHE_HOME = $Leaves['xdg-cache']
    $env:PRE_COMMIT_HOME = $Leaves['pre-commit']

    foreach ($name in $script:ProjectToolEnvironmentClearedVariables) {
        Remove-Item -LiteralPath "Env:$name" -ErrorAction SilentlyContinue
    }
}

function Restore-ManagedEnvironmentSnapshot {
    <#
    .SYNOPSIS
      Restore the caller's working directory and every managed environment
      variable from a snapshot produced by Get-ManagedEnvironmentSnapshot. A
      variable that was absent before is restored to absence, not empty string.
    #>
    param(
        [Parameter(Mandatory)]
        [pscustomobject]$Snapshot
    )

    Set-Location -LiteralPath $Snapshot.WorkingDirectory

    $allNames = $script:ProjectToolEnvironmentManagedVariables +
        $script:ProjectToolEnvironmentClearedVariables
    foreach ($name in $allNames) {
        $entry = $Snapshot.Variables[$name]
        if ($entry.Present) {
            Set-Item -LiteralPath "Env:$name" -Value $entry.Value
        } else {
            Remove-Item -LiteralPath "Env:$name" -ErrorAction SilentlyContinue
        }
    }
}

function Enter-ProjectPytestLock {
    <#
    .SYNOPSIS
      Acquire the cross-process pytest lock via a FileShare.None handle.

    .DESCRIPTION
      Opens (creating if absent) "<LocksLeaf>/pytest.lock" with
      [System.IO.FileShare]::None. Lock ownership is the open handle, not
      file existence: a lock file left behind by a crashed process does not
      block this call, because no live process still holds an exclusive
      handle on it.

      On contention (another live process already holds the handle), this
      writes a diagnostic to stderr naming the lock path and exits the
      process with code 17 immediately -- it never waits, and it never lets
      the caller reach uv or pytest.
    .OUTPUTS
      [System.IO.FileStream] the open lock handle. The caller MUST dispose
      it (e.g. via Exit-ProjectPytestLock) only after the child process
      guarded by the lock has exited.
    #>
    param(
        [Parameter(Mandatory)]
        [string]$LocksLeaf
    )

    $lockPath = Join-Path $LocksLeaf 'pytest.lock'
    try {
        return [System.IO.File]::Open(
            $lockPath,
            [System.IO.FileMode]::OpenOrCreate,
            [System.IO.FileAccess]::ReadWrite,
            [System.IO.FileShare]::None
        )
    } catch [System.IO.IOException] {
        [Console]::Error.WriteLine(
            "[project-tool-environment] Pytest lock is held by another process: $lockPath"
        )
        exit 17
    }
}

function Exit-ProjectPytestLock {
    <#
    .SYNOPSIS
      Release a lock handle obtained from Enter-ProjectPytestLock.
    #>
    param(
        [System.IO.FileStream]$LockHandle
    )

    if ($LockHandle) {
        $LockHandle.Dispose()
    }
}

function Invoke-InProjectToolEnvironment {
    <#
    .SYNOPSIS
      Run one child process with process-scoped project-runtime environment
      variables, restoring the caller's CWD and managed variables afterward.

    .DESCRIPTION
      Resolution order: normalize the runtime root, reject it with exit 18 if
      its volume cannot be established (before any directory is created or
      environment mutated), create the seven managed leaves, acquire the
      pytest lock, snapshot the caller's CWD/environment, mutate the
      environment and CWD, run -PreLaunchValidation if supplied, launch the
      child, then restore the snapshot in `finally` regardless of outcome.

    .PARAMETER PreLaunchValidation
      Optional. A validation-only scriptblock, invoked with no arguments,
      after the pytest lock is held and immediately before the child process
      launches -- the one deliberately narrow extension point this function
      accepts (see the file-level doc's "not general-purpose" warning: this
      does not loosen it). It receives no leaves, paths, or other state from
      this function -- any values the check needs must already be closed
      over by the caller (e.g. via `.GetNewClosure()`) -- and its return
      value is ignored: a validation failure must terminate the process
      itself (e.g. `exit <code>`). That is honored correctly, because
      PowerShell propagates `exit` up through enclosing `try/finally` blocks
      within the same process, so the `finally` below still restores the
      environment and releases the lock before the process actually exits --
      pinned by a test in tests/test_project_tool_environment.py that a hook
      calling `exit` still releases the lock and restores the environment.
      Omitted (the default for every existing caller), behavior is
      byte-for-byte identical to before this parameter existed.

    .OUTPUTS
      [pscustomobject] with ExitCode (the child process's exit code),
      RuntimeRoot (the normalized root actually used), and Leaves (the seven
      resolved leaf paths).
    #>
    param(
        [Parameter(Mandatory)]
        [string]$RepositoryRoot,

        [string]$RuntimeRoot,

        [Parameter(Mandatory)]
        [string]$ChildFilePath,

        [string[]]$ChildArgumentList = @(),

        [scriptblock]$PreLaunchValidation
    )

    $resolvedRepositoryRoot = (Resolve-Path -LiteralPath $RepositoryRoot).Path
    $normalizedRuntimeRoot = Get-ProjectRuntimeRoot -RepositoryRoot $resolvedRepositoryRoot -RuntimeRoot $RuntimeRoot

    $rejectionReason = Get-ProjectRuntimeRootRejectionReason -RuntimeRoot $normalizedRuntimeRoot
    if ($rejectionReason) {
        [Console]::Error.WriteLine(
            "[project-tool-environment] Unsafe runtime root, $($rejectionReason): $normalizedRuntimeRoot"
        )
        exit 18
    }

    $leaves = Get-ProjectRuntimeLeaves -RuntimeRoot $normalizedRuntimeRoot

    # A cleared root is not enough: Initialize-ProjectRuntimeLeaves reuses an
    # existing leaf as-is, so a leaf that is itself an unresolvable reparse
    # point would go unnoticed while the root above it looks safe.
    # Checked before creation -- a leaf that does not exist yet cannot be a
    # reparse point, and resolves to the root already cleared above.
    foreach ($leafName in $leaves.Keys) {
        $leafReason = Get-ProjectRuntimeRootRejectionReason -RuntimeRoot $leaves[$leafName]
        if ($leafReason) {
            [Console]::Error.WriteLine(
                "[project-tool-environment] Unsafe runtime leaf '$leafName', $($leafReason): $($leaves[$leafName])"
            )
            exit 18
        }
    }

    Initialize-ProjectRuntimeLeaves -Leaves $leaves

    # Acquire the cross-process pytest lock after the leaves exist but before
    # any environment mutation or child launch; released in the `finally`
    # block below alongside the environment/CWD restoration, only after the
    # child process has exited.
    $lockHandle = Enter-ProjectPytestLock -LocksLeaf $leaves['locks']

    $snapshot = Get-ManagedEnvironmentSnapshot
    try {
        Set-Location -LiteralPath $resolvedRepositoryRoot
        Set-ManagedEnvironment -Leaves $leaves

        # Runs inside the lock, immediately before the child launches -- a
        # reader can confirm the ordering directly: this call is the last
        # statement before & $ChildFilePath below, and both are inside the
        # same try block guarded by $lockHandle acquired above and released
        # only in the `finally` block. See -PreLaunchValidation's own
        # parameter doc for why a hook calling `exit` here still restores
        # the environment and releases the lock rather than stranding them.
        #
        # Piped through Out-Null: a PowerShell function's return value is the
        # union of every unredirected pipeline write during its execution,
        # not just its final `return` statement, so any object the hook
        # writes to its success stream would otherwise silently join this
        # function's own ExitCode/RuntimeRoot/Leaves return value -- the same
        # leak the child launch below is piped through Out-Host to avoid.
        # This is also the concrete meaning of "its return value is ignored"
        # in the parameter doc above.
        if ($PreLaunchValidation) {
            & $PreLaunchValidation | Out-Null
        }

        # Piped through Out-Host so the child's own stdout/stderr still reach
        # the console live, without those output objects leaking into this
        # function's own return pipeline (which would otherwise corrupt the
        # returned ExitCode/RuntimeRoot/Leaves object whenever the caller
        # captures this function's result, e.g. `$result = Invoke-...`).
        & $ChildFilePath @ChildArgumentList | Out-Host
        $childExitCode = $LASTEXITCODE
    } finally {
        Restore-ManagedEnvironmentSnapshot -Snapshot $snapshot
        Exit-ProjectPytestLock -LockHandle $lockHandle
    }

    return [pscustomobject]@{
        ExitCode = $childExitCode
        RuntimeRoot = $normalizedRuntimeRoot
        Leaves = $leaves
    }
}
