"""Behavior tests for the reusable project-tool environment helper.

Every test drives a real ``pwsh`` subprocess that dot-sources
``scripts/project-tool-environment.ps1`` and exercises its functions against a
real child process, asserting on observed child-process state rather than on
in-process mocks.
"""

from __future__ import annotations

import os
import subprocess
import uuid
from pathlib import Path

import pytest

from tests._pwsh_support import ps_quote, run_pwsh_file

REPO_ROOT = Path(__file__).resolve().parent.parent
HELPER_SCRIPT = REPO_ROOT / "scripts" / "project-tool-environment.ps1"

MANAGED_VARIABLES = (
    "TEMP",
    "TMP",
    "TMPDIR",
    "UV_CACHE_DIR",
    "XDG_CACHE_HOME",
    "PRE_COMMIT_HOME",
)

ALL_LEAF_NAMES = (
    "python-temp",
    "pytest",
    "ledger-collection",
    "uv-cache",
    "xdg-cache",
    "pre-commit",
    "locks",
)


def _write_driver(tmp_path: Path, body: str) -> Path:
    driver = tmp_path / f"driver-{uuid.uuid4().hex}.ps1"
    driver.write_text(
        f"$ErrorActionPreference = 'Stop'\n. {ps_quote(HELPER_SCRIPT)}\n{body}\n",
        encoding="utf-8",
    )
    return driver


def _run_driver(
    driver: Path, *, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return run_pwsh_file(driver, env=env)


def _parse_kv_lines(stdout: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in stdout.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            result[key] = value
    return result


def _run_root_driver(
    tmp_path: Path, runtime_root: str, *, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    """Drive one explicit -RuntimeRoot through the helper under *env*.

    The trailing marker is what distinguishes a run that got past the guard
    from one the guard stopped: rejection tests assert it is absent, success
    tests assert it is present.
    """
    repo_root = tmp_path / "repo"
    repo_root.mkdir(exist_ok=True)

    driver_body = f"""
$repoRoot = {ps_quote(repo_root)}
Invoke-InProjectToolEnvironment `
    -RepositoryRoot $repoRoot `
    -RuntimeRoot {ps_quote(runtime_root)} `
    -ChildFilePath 'pwsh' `
    -ChildArgumentList @('-NoProfile', '-Command', 'exit 0') | Out-Null
Write-Output "COMPLETED=true"
"""
    return _run_driver(_write_driver(tmp_path, driver_body), env=env)


# ---------------------------------------------------------------------------
# Case 1 & 2: managed-variable round trip (initial absence / initial value)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("variable_name", MANAGED_VARIABLES)
@pytest.mark.parametrize("initial_state", ["absent", "present"])
def test_managed_variable_round_trips_through_restoration(
    tmp_path: Path, variable_name: str, initial_state: str
):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    child_env = os.environ.copy()
    # Absolute: TEMP/TMP/TMPDIR are resolved against the caller's CWD, so a
    # relative sentinel makes the child materialize a directory at the repo root.
    sentinel_value = str(tmp_path / f"sentinel-{uuid.uuid4().hex}")
    if initial_state == "absent":
        child_env.pop(variable_name, None)
    else:
        child_env[variable_name] = sentinel_value

    driver_body = f"""
$repoRoot = {ps_quote(repo_root)}
$result = Invoke-InProjectToolEnvironment `
    -RepositoryRoot $repoRoot `
    -ChildFilePath 'pwsh' `
    -ChildArgumentList @('-NoProfile', '-Command', 'exit 0')
$item = Get-Item -LiteralPath 'Env:{variable_name}' -ErrorAction SilentlyContinue
if ($item) {{
    Write-Output "RESTORED_PRESENT=true"
    Write-Output "RESTORED_VALUE=$($item.Value)"
}} else {{
    Write-Output "RESTORED_PRESENT=false"
}}
Write-Output "CHILD_EXIT=$($result.ExitCode)"
exit 0
"""
    driver = _write_driver(tmp_path, driver_body)

    result = _run_driver(driver, env=child_env)

    assert result.returncode == 0, result.stderr
    values = _parse_kv_lines(result.stdout)
    assert values["CHILD_EXIT"] == "0"
    if initial_state == "absent":
        assert values["RESTORED_PRESENT"] == "false"
    else:
        assert values["RESTORED_PRESENT"] == "true"
        assert values["RESTORED_VALUE"] == sentinel_value


# ---------------------------------------------------------------------------
# Case 3: success path -- child observes every managed variable below the root
# ---------------------------------------------------------------------------


def test_success_path_child_observes_every_variable_below_runtime_root(
    tmp_path: Path,
):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    expected_root = repo_root / ".project-runtime"

    probe_lines = "\n".join(
        f'Write-Output "{name}=$env:{name}"' for name in MANAGED_VARIABLES
    )
    driver_body = f"""
$repoRoot = {ps_quote(repo_root)}
$originalCwd = (Get-Location).Path
$childScript = @'
{probe_lines}
exit 0
'@
$result = Invoke-InProjectToolEnvironment `
    -RepositoryRoot $repoRoot `
    -ChildFilePath 'pwsh' `
    -ChildArgumentList @('-NoProfile', '-Command', $childScript)
Write-Output "CHILD_EXIT=$($result.ExitCode)"
Write-Output "RUNTIME_ROOT=$($result.RuntimeRoot)"
Write-Output "POST_CWD=$((Get-Location).Path)"
Write-Output "ORIGINAL_CWD=$originalCwd"
exit 0
"""
    driver = _write_driver(tmp_path, driver_body)

    result = _run_driver(driver, env=os.environ.copy())

    assert result.returncode == 0, result.stderr
    values = _parse_kv_lines(result.stdout)
    assert values["CHILD_EXIT"] == "0"
    assert Path(values["RUNTIME_ROOT"]) == expected_root
    assert values["POST_CWD"] == values["ORIGINAL_CWD"]

    resolved_root = Path(values["RUNTIME_ROOT"]).resolve()
    for name in MANAGED_VARIABLES:
        observed = Path(values[name]).resolve()
        assert resolved_root in observed.parents or resolved_root == observed, (
            f"{name} resolved to {observed}, not below {resolved_root}"
        )


def test_pytest_addopts_is_cleared_for_the_child_and_restored_after(tmp_path: Path):
    """PYTEST_ADDOPTS must not survive into the child.

    pytest appends it to its own command line, so an inherited
    ``--basetemp=C:\\...`` would relocate pytest's temporary tree back onto
    the system drive -- and any other option there would silently extend the
    launcher's exact argument vector.
    """
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    sentinel = f"--basetemp=C:\\smuggled-{uuid.uuid4().hex}"

    child_env = os.environ.copy()
    child_env["PYTEST_ADDOPTS"] = sentinel

    driver_body = f"""
$repoRoot = {ps_quote(repo_root)}
$childScript = 'Write-Output "CHILD_ADDOPTS_PRESENT=$([bool]$env:PYTEST_ADDOPTS)"'
$result = Invoke-InProjectToolEnvironment `
    -RepositoryRoot $repoRoot `
    -ChildFilePath 'pwsh' `
    -ChildArgumentList @('-NoProfile', '-Command', $childScript)
Write-Output "CHILD_EXIT=$($result.ExitCode)"
Write-Output "RESTORED_ADDOPTS=$env:PYTEST_ADDOPTS"
exit 0
"""
    driver = _write_driver(tmp_path, driver_body)

    result = _run_driver(driver, env=child_env)

    assert result.returncode == 0, result.stderr
    values = _parse_kv_lines(result.stdout)
    assert values["CHILD_ADDOPTS_PRESENT"] == "False"
    assert values["RESTORED_ADDOPTS"] == sentinel


# ---------------------------------------------------------------------------
# Case 4: child failure -- environment and CWD still restored, exit propagated
# ---------------------------------------------------------------------------


def test_child_failure_restores_state_and_propagates_exit_code(tmp_path: Path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    sentinel_temp = str(tmp_path / f"sentinel-temp-{uuid.uuid4().hex}")

    child_env = os.environ.copy()
    child_env["TEMP"] = sentinel_temp
    child_env.pop("UV_CACHE_DIR", None)

    driver_body = f"""
$repoRoot = {ps_quote(repo_root)}
$originalCwd = (Get-Location).Path
$result = Invoke-InProjectToolEnvironment `
    -RepositoryRoot $repoRoot `
    -ChildFilePath 'pwsh' `
    -ChildArgumentList @('-NoProfile', '-Command', 'exit 5')
$postCwd = (Get-Location).Path
$tempItem = Get-Item -LiteralPath 'Env:TEMP' -ErrorAction SilentlyContinue
$uvItem = Get-Item -LiteralPath 'Env:UV_CACHE_DIR' -ErrorAction SilentlyContinue
Write-Output "CHILD_EXIT=$($result.ExitCode)"
Write-Output "CWD_MATCH=$($postCwd -eq $originalCwd)"
Write-Output "TEMP_VALUE=$($tempItem.Value)"
Write-Output "UV_CACHE_PRESENT=$([bool]$uvItem)"
exit 0
"""
    driver = _write_driver(tmp_path, driver_body)

    result = _run_driver(driver, env=child_env)

    assert result.returncode == 0, result.stderr
    values = _parse_kv_lines(result.stdout)
    assert values["CHILD_EXIT"] == "5"
    assert values["CWD_MATCH"] == "True"
    assert values["TEMP_VALUE"] == sentinel_temp
    assert values["UV_CACHE_PRESENT"] == "False"


# ---------------------------------------------------------------------------
# Case 5: unsafe runtime root -- exit 18, message names the path, no leaves
# ---------------------------------------------------------------------------


def _run_unsafe_root_driver(tmp_path: Path, unsafe_root: str) -> tuple[int, str]:
    """Drive one explicit -RuntimeRoot expecting rejection, return (code, stderr)."""
    result = _run_root_driver(tmp_path, unsafe_root, env=os.environ.copy())
    assert "COMPLETED" not in result.stdout
    return result.returncode, result.stderr


def test_dangling_junction_runtime_root_exits_18(tmp_path: Path):
    """A junction whose target is missing names no establishable volume.

    Windows reports the same "not found" errors for it as for a path that
    does not exist at all, so a guard that only walks upward would clear the
    safe parent and let the reparse point through -- and its target can come
    into existence later, on any volume.
    """
    system_drive = os.environ.get("SYSTEMDRIVE", "C:").rstrip("\\/")
    junction = tmp_path / "dangling-junction"
    missing_target = f"{system_drive}\\mailattic-test-missing-{uuid.uuid4().hex}"
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), missing_target],
        check=True,
        capture_output=True,
        text=True,
    )
    unsafe_root = str(junction / "runtime")

    try:
        returncode, stderr = _run_unsafe_root_driver(tmp_path, unsafe_root)
    finally:
        junction.rmdir()

    assert returncode == 18, stderr
    assert "volume cannot be established" in stderr


def test_unc_runtime_root_exits_18(tmp_path: Path):
    """A UNC root resolves to a UNC final path, which names no volume.

    It is rejected as unverifiable rather than guessed at -- reading
    `\\\\localhost\\C$` as the system drive would be share-name interpretation,
    not a check.
    """
    unsafe_root = f"\\\\localhost\\C$\\mailattic-test-runtime-{uuid.uuid4().hex}"

    returncode, stderr = _run_unsafe_root_driver(tmp_path, unsafe_root)

    assert returncode == 18, stderr
    assert unsafe_root in stderr
    assert "volume cannot be established" in stderr
    assert not Path(unsafe_root).exists()


# ---------------------------------------------------------------------------
# Case 5b: the surviving integrity refusals, independent of any environment
# variable
#
# These three used to run under the retired system-drive rejection's operator
# waiver, to prove the waiver did not reach an unestablished volume. The
# waiver is gone; the rejection they pin (an unestablished volume) is not, so
# they stay as their own tests, just without the waiver-driving helper.
# ---------------------------------------------------------------------------


def test_unc_runtime_root_is_rejected_as_an_unestablished_volume(tmp_path: Path):
    """A UNC final path names no volume at all, so nothing waves it through."""
    unsafe_root = f"\\\\localhost\\C$\\mailattic-test-runtime-{uuid.uuid4().hex}"

    result = _run_root_driver(tmp_path, unsafe_root, env=os.environ.copy())

    assert result.returncode == 18, (result.stdout, result.stderr)
    assert "volume cannot be established" in result.stderr
    assert "COMPLETED" not in _parse_kv_lines(result.stdout)


def test_dangling_junction_root_is_rejected_as_an_unestablished_volume(
    tmp_path: Path,
):
    """Load-bearing: the target can come into existence later, on any volume."""
    system_drive = os.environ.get("SYSTEMDRIVE", "C:").rstrip("\\/")
    junction = tmp_path / "dangling-junction"
    missing_target = f"{system_drive}\\mailattic-test-missing-{uuid.uuid4().hex}"
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), missing_target],
        check=True,
        capture_output=True,
        text=True,
    )
    unsafe_root = str(junction / "runtime")

    try:
        result = _run_root_driver(tmp_path, unsafe_root, env=os.environ.copy())
    finally:
        junction.rmdir()

    assert result.returncode == 18, (result.stdout, result.stderr)
    assert "volume cannot be established" in result.stderr
    assert "COMPLETED" not in _parse_kv_lines(result.stdout)


def test_dangling_junction_leaf_is_rejected_as_an_unestablished_volume(
    tmp_path: Path,
):
    """Leaves are checked on their own, independently of the root.

    This is the leaf-level half of the unestablished-volume rejection, and
    the only test in this suite exercising a dangling reparse point at leaf
    granularity rather than at the root.
    """
    system_drive = os.environ.get("SYSTEMDRIVE", "C:").rstrip("\\/")
    runtime_root = tmp_path / "runtime"
    leaf = runtime_root / "uv-cache"
    leaf.parent.mkdir(parents=True)
    missing_target = f"{system_drive}\\mailattic-test-missing-{uuid.uuid4().hex}"
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(leaf), missing_target],
        check=True,
        capture_output=True,
        text=True,
    )

    try:
        result = _run_root_driver(tmp_path, str(runtime_root), env=os.environ.copy())
    finally:
        leaf.rmdir()

    assert result.returncode == 18, (result.stdout, result.stderr)
    assert "uv-cache" in result.stderr
    assert "volume cannot be established" in result.stderr
    created = [
        name
        for name in ALL_LEAF_NAMES
        if name != "uv-cache" and (runtime_root / name).exists()
    ]
    assert not created, f"leaves were created despite an unsafe leaf: {created}"


# ---------------------------------------------------------------------------
# Case 6: directory-creation failure -- visible and non-zero
# ---------------------------------------------------------------------------


def test_directory_creation_failure_is_visible_and_nonzero(tmp_path: Path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    blocking_file = repo_root / ".project-runtime" / "uv-cache"
    blocking_file.parent.mkdir(parents=True)
    blocking_file.write_text("blocking file, not a directory", encoding="utf-8")

    driver_body = f"""
$repoRoot = {ps_quote(repo_root)}
try {{
    Invoke-InProjectToolEnvironment `
        -RepositoryRoot $repoRoot `
        -ChildFilePath 'pwsh' `
        -ChildArgumentList @('-NoProfile', '-Command', 'exit 0') | Out-Null
    Write-Output "UNEXPECTED_SUCCESS=true"
}} catch {{
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 1
}}
"""
    driver = _write_driver(tmp_path, driver_body)

    result = _run_driver(driver, env=os.environ.copy())

    assert result.returncode != 0
    assert "UNEXPECTED_SUCCESS" not in result.stdout
    assert str(blocking_file) in result.stderr
    assert blocking_file.is_file()


# ---------------------------------------------------------------------------
# Bonus coverage: leaf distinctness and explicit RuntimeRoot validation
# ---------------------------------------------------------------------------


def test_default_root_and_leaves_are_repo_relative_and_distinct(tmp_path: Path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    driver_body = f"""
$repoRoot = {ps_quote(repo_root)}
$root = Get-ProjectRuntimeRoot -RepositoryRoot $repoRoot
Write-Output "ROOT=$root"
$leaves = Get-ProjectRuntimeLeaves -RuntimeRoot $root
foreach ($key in $leaves.Keys) {{
    Write-Output "LEAF_$key=$($leaves[$key])"
}}
exit 0
"""
    driver = _write_driver(tmp_path, driver_body)

    result = _run_driver(driver, env=os.environ.copy())

    assert result.returncode == 0, result.stderr
    values = _parse_kv_lines(result.stdout)
    assert Path(values["ROOT"]) == repo_root / ".project-runtime"

    leaf_paths = [Path(values[f"LEAF_{name}"]) for name in ALL_LEAF_NAMES]
    assert len(set(leaf_paths)) == len(leaf_paths)
    for path in leaf_paths:
        assert path.parent == Path(values["ROOT"])


@pytest.mark.parametrize(
    "override",
    [
        "relative\\path",
        # Drive-relative and root-relative forms: IsPathRooted accepts both,
        # yet their GetFullPath expansion depends on the caller's current
        # directory, so neither is an absolute path.
        "C:runtime",
        "\\runtime",
    ],
    ids=["relative", "drive-relative", "root-relative"],
)
def test_explicit_relative_runtime_root_override_is_rejected(
    tmp_path: Path, override: str
):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    driver_body = f"""
$repoRoot = {ps_quote(repo_root)}
try {{
    Get-ProjectRuntimeRoot `
        -RepositoryRoot $repoRoot `
        -RuntimeRoot {ps_quote(override)} | Out-Null
    Write-Output "UNEXPECTED_SUCCESS=true"
}} catch {{
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 1
}}
"""
    driver = _write_driver(tmp_path, driver_body)

    result = _run_driver(driver, env=os.environ.copy())

    assert result.returncode != 0
    assert "UNEXPECTED_SUCCESS" not in result.stdout
    assert "absolute" in result.stderr.lower()


# ---------------------------------------------------------------------------
# PreLaunchValidation hook -- runs inside the lock, immediately before the
# child launches (#75)
# ---------------------------------------------------------------------------


def test_prelaunch_validation_hook_failure_releases_the_lock_within_the_same_process(
    tmp_path: Path,
):
    """The regression guard for "a hook that can strand the lock or leave the
    caller's environment mutated would be worse than the race it closes" --
    the criterion this test cares about most.

    This deliberately does NOT use a hook that calls `exit`: doing so
    terminates the whole process, and Windows releases every handle a
    process still holds the instant it exits, regardless of whether
    Exit-ProjectPytestLock's own Dispose() ever ran. A test built that way
    (spawn a process whose hook calls `exit`, then check the lock from a
    second, separate process) would stay green even with
    Exit-ProjectPytestLock deleted from the `finally` block entirely --
    confirmed directly: doing exactly that still passed. That is the false-
    confidence trap this test exists to avoid.

    Instead, the hook throws a terminating error, caught by an ordinary
    try/catch in the SAME still-running process (`exit` cannot be caught
    this way -- confirmed separately -- but a `throw` can), and immediately
    afterward that same process tries to acquire the identical lock again.
    With no process boundary for the OS to clean up behind, this can only
    succeed if Exit-ProjectPytestLock in the `finally` block actually ran.

    Lock re-acquisition alone does not pin environment/CWD restoration --
    those are a separate, unconditional statement in the same `finally`
    block, and nothing stops an implementation from releasing the lock while
    skipping them. So this test also snapshots a managed variable and the
    CWD before the call, mutates both away from that snapshot inside the
    driver process itself (the call under test does this as part of its own
    contract), and asserts both are back to the pre-call snapshot after the
    hook fails -- directly, not by inference from the lock check.
    """
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    sibling_dir = tmp_path / "sibling-cwd"
    sibling_dir.mkdir()
    sentinel_temp = str(tmp_path / f"sentinel-temp-{uuid.uuid4().hex}")

    driver_body = f"""
$repoRoot = {ps_quote(repo_root)}
Set-Location -LiteralPath {ps_quote(sibling_dir)}
$originalCwd = (Get-Location).Path
$env:TEMP = {ps_quote(sentinel_temp)}
$hook = {{ throw "hook failure -- the process must NOT terminate here" }}
try {{
    Invoke-InProjectToolEnvironment `
        -RepositoryRoot $repoRoot `
        -ChildFilePath 'pwsh' `
        -ChildArgumentList @('-NoProfile', '-Command', 'exit 0') `
        -PreLaunchValidation $hook | Out-Null
    Write-Output "UNEXPECTED_SUCCESS=true"
}} catch {{
    Write-Output "HOOK_EXCEPTION_CAUGHT=true"
}}

$postCwd = (Get-Location).Path
$tempItem = Get-Item -LiteralPath 'Env:TEMP' -ErrorAction SilentlyContinue
Write-Output "CWD_RESTORED=$($postCwd -eq $originalCwd)"
Write-Output "TEMP_RESTORED=$($tempItem.Value -eq {ps_quote(sentinel_temp)})"

# Still the SAME process -- no process boundary for the OS to have cleaned
# up behind. This can only succeed if the `finally` block above actually
# released the lock.
$root = Get-ProjectRuntimeRoot -RepositoryRoot $repoRoot
$leaves = Get-ProjectRuntimeLeaves -RuntimeRoot $root
Initialize-ProjectRuntimeLeaves -Leaves $leaves
$lockHandle = Enter-ProjectPytestLock -LocksLeaf $leaves['locks']
Write-Output "LOCK_REACQUIRED_SAME_PROCESS=true"
Exit-ProjectPytestLock -LockHandle $lockHandle
exit 0
"""
    driver = _write_driver(tmp_path, driver_body)
    result = _run_driver(driver, env=os.environ.copy())

    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "UNEXPECTED_SUCCESS" not in result.stdout, result.stdout
    assert "HOOK_EXCEPTION_CAUGHT=true" in result.stdout, result.stdout
    values = _parse_kv_lines(result.stdout)
    assert values["CWD_RESTORED"] == "True", (
        "the caller's working directory was not restored after a "
        "-PreLaunchValidation hook failed"
    )
    assert values["TEMP_RESTORED"] == "True", (
        "the caller's managed environment variables were not restored "
        "after a -PreLaunchValidation hook failed"
    )
    assert "LOCK_REACQUIRED_SAME_PROCESS=true" in result.stdout, (
        "the pytest lock was not released after a -PreLaunchValidation hook "
        "failed -- the finally block did not run to completion, or "
        "Exit-ProjectPytestLock was skipped"
    )


def test_prelaunch_validation_hook_calling_exit_leaves_the_lock_available_after(
    tmp_path: Path,
):
    """Realistic-usage contract check: scripts/invoke-pytest.ps1's actual
    hook calls `exit`, so this confirms that shape end to end too.

    NOTE: this alone is not a discriminating regression guard for lock
    release -- see the docstring on the test above. Windows releases every
    handle a process still holds the instant it exits, `exit` here
    terminates the whole process, so the lock becomes available to the next
    caller either way. It is kept as a smoke/contract check of the actual
    caller shape.
    """
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    first_driver_body = f"""
$repoRoot = {ps_quote(repo_root)}
$hook = {{ exit 19 }}
try {{
    Invoke-InProjectToolEnvironment `
        -RepositoryRoot $repoRoot `
        -ChildFilePath 'pwsh' `
        -ChildArgumentList @('-NoProfile', '-Command', 'exit 0') `
        -PreLaunchValidation $hook | Out-Null
    Write-Output "UNREACHABLE=true"
}} catch {{
    Write-Output "UNREACHABLE_CAUGHT=true"
}}
"""
    first_driver = _write_driver(tmp_path, first_driver_body)
    first_result = _run_driver(first_driver, env=os.environ.copy())

    assert first_result.returncode == 19, (first_result.stdout, first_result.stderr)
    assert "UNREACHABLE" not in first_result.stdout, first_result.stdout

    second_driver_body = f"""
$repoRoot = {ps_quote(repo_root)}
$root = Get-ProjectRuntimeRoot -RepositoryRoot $repoRoot
$leaves = Get-ProjectRuntimeLeaves -RuntimeRoot $root
Initialize-ProjectRuntimeLeaves -Leaves $leaves
$lockHandle = Enter-ProjectPytestLock -LocksLeaf $leaves['locks']
Write-Output "LOCK_ACQUIRED=true"
Exit-ProjectPytestLock -LockHandle $lockHandle
exit 0
"""
    second_driver = _write_driver(tmp_path, second_driver_body)
    second_result = _run_driver(second_driver, env=os.environ.copy())

    assert second_result.returncode == 0, (second_result.stdout, second_result.stderr)
    assert "LOCK_ACQUIRED=true" in second_result.stdout


def test_prelaunch_validation_hook_output_does_not_leak_into_the_return_value(
    tmp_path: Path,
):
    """A hook's success-stream output must not corrupt the function's own
    return value.

    A PowerShell function's return value is the union of every unredirected
    pipeline write during its execution, not just its final `return`
    statement -- so a hook that writes an object (as the "return value is
    ignored" contract permits it to, since nothing stops a caller-supplied
    scriptblock from doing so) must not silently join $result. A regression
    here would still let $result.ExitCode resolve correctly (member
    enumeration skips array elements lacking the property), so this checks
    $result's own shape directly rather than only one property of it.
    """
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    driver_body = f"""
$repoRoot = {ps_quote(repo_root)}
$hook = {{ Write-Output "hook output that must not leak" }}
$result = Invoke-InProjectToolEnvironment `
    -RepositoryRoot $repoRoot `
    -ChildFilePath 'pwsh' `
    -ChildArgumentList @('-NoProfile', '-Command', 'exit 0') `
    -PreLaunchValidation $hook
Write-Output "RESULT_TYPE=$($result.GetType().Name)"
Write-Output "RESULT_COUNT=$(@($result).Count)"
Write-Output "CHILD_EXIT=$($result.ExitCode)"
"""
    driver = _write_driver(tmp_path, driver_body)
    result = _run_driver(driver, env=os.environ.copy())

    assert result.returncode == 0, (result.stdout, result.stderr)
    values = _parse_kv_lines(result.stdout)
    assert values["RESULT_TYPE"] == "PSCustomObject", (
        "the hook's own pipeline output leaked into the function's return "
        f"value, changing its type: {result.stdout}"
    )
    assert values["RESULT_COUNT"] == "1"
    assert values["CHILD_EXIT"] == "0"
    assert "hook output that must not leak" not in result.stdout


def test_prelaunch_validation_hook_runs_before_the_child_and_can_block_it(
    tmp_path: Path,
):
    """The hook sits inside the lock, immediately before the child launches:
    when it does not exit, the child still launches normally afterward; when
    it does exit (the case above), the child never launches at all.

    Occurrence of both markers is not enough to pin ordering -- a swap that
    launched the child before running the hook would leave both markers on
    disk too, silently degrading pre-launch validation into post-launch
    validation (the entire reason this hook exists). So the hook and the
    child both append their own line to one SHARED events file instead of
    writing separate markers, and this asserts the recorded sequence is
    exactly [hook, child] -- proving the hook observably runs first, not
    merely that both eventually ran.
    """
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    events_file = tmp_path / "hook-child-order.log"

    driver_body = f"""
$repoRoot = {ps_quote(repo_root)}
$eventsFile = {ps_quote(events_file)}
$hook = {{ Add-Content -LiteralPath $eventsFile -Value 'hook' }}.GetNewClosure()
$childCommand = "Add-Content -LiteralPath '$eventsFile' -Value 'child'; exit 0"
$result = Invoke-InProjectToolEnvironment `
    -RepositoryRoot $repoRoot `
    -ChildFilePath 'pwsh' `
    -ChildArgumentList @('-NoProfile', '-Command', $childCommand) `
    -PreLaunchValidation $hook
Write-Output "CHILD_EXIT=$($result.ExitCode)"
"""
    driver = _write_driver(tmp_path, driver_body)
    result = _run_driver(driver, env=os.environ.copy())

    assert result.returncode == 0, (result.stdout, result.stderr)
    values = _parse_kv_lines(result.stdout)
    assert values["CHILD_EXIT"] == "0"
    assert events_file.exists(), "neither the hook nor the child recorded an event"
    recorded_events = events_file.read_text(encoding="utf-8").split()
    assert recorded_events == ["hook", "child"], (
        "the hook did not observably run before the child -- recorded "
        f"event order was {recorded_events!r}, expected ['hook', 'child']"
    )


def test_omitted_prelaunch_validation_hook_leaves_behavior_unchanged(
    tmp_path: Path,
):
    """Every existing caller omits -PreLaunchValidation. Adding the parameter
    must not change behavior on that path: the child still launches and the
    returned ExitCode/RuntimeRoot are unaffected, exactly as before this
    parameter existed.
    """
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    driver_body = f"""
$repoRoot = {ps_quote(repo_root)}
$result = Invoke-InProjectToolEnvironment `
    -RepositoryRoot $repoRoot `
    -ChildFilePath 'pwsh' `
    -ChildArgumentList @('-NoProfile', '-Command', 'exit 0')
Write-Output "CHILD_EXIT=$($result.ExitCode)"
Write-Output "ROOT=$($result.RuntimeRoot)"
"""
    driver = _write_driver(tmp_path, driver_body)
    result = _run_driver(driver, env=os.environ.copy())

    assert result.returncode == 0, (result.stdout, result.stderr)
    values = _parse_kv_lines(result.stdout)
    assert values["CHILD_EXIT"] == "0"
    assert Path(values["ROOT"]) == repo_root / ".project-runtime"
