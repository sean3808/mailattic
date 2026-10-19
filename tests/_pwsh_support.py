"""Shared subprocess helpers for the PowerShell- and Git-driving test suites.

Every test file in this repo that exercises a PowerShell script does so by
spawning a real ``pwsh`` subprocess -- never an in-process mock. This module
centralizes the small pieces of ceremony every one of those spawns repeats:
escaping a value for embedding inside a single-quoted PowerShell string
literal, and building the ``pwsh -NoProfile -ExecutionPolicy Bypass ...``
argument vector together with the common ``subprocess.run`` keyword arguments
(``capture_output=True, text=True, encoding="utf-8"``).

It also owns the child-environment shaping those same suites need --
suppressing Git's automatic maintenance in disposable fixture repositories.
That does not spawn ``pwsh``, but exists for the same reason the ``pwsh``
helpers do: the setting has to be identical everywhere it is applied, and a
bare literal repeated across modules would drift. Every helper here is
opt-in -- importing this module changes no behavior on its own.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

GIT_AUTO_MAINTENANCE_SETTING = "maintenance.auto"
"""Git config key that gates the automatic ``git maintenance run --auto`` fork.

Named rather than spelled inline because it is set through two different
mechanisms -- the inherited environment for repositories the tests drive
directly, and repository-local config for bare remotes Git refuses to let the
environment reach (see :func:`git_env_without_auto_maintenance`).
"""


def git_env_without_auto_maintenance(
    base: dict[str, str] | None = None,
) -> dict[str, str]:
    """Child environment that suppresses Git's automatic maintenance.

    The two Git-heavy suites build hundreds of tiny repositories that are
    thrown away at the end of the test, and Git forks ``git maintenance run
    --auto`` after commits in every one of them. No test asserts maintenance
    behavior, so those children are pure process-creation cost -- amplified on
    Windows by endpoint-security scanning.

    Delivered through ``GIT_CONFIG_COUNT``/``KEY``/``VALUE`` rather than a
    per-invocation ``git -c`` precisely because it is inherited: the suites
    drive Git two and three processes down through ``pwsh`` and Python
    children, and those descendants must see the same setting. It stays
    test-local by being opt-in -- callers pass the returned mapping as an
    explicit child ``env``, so nothing is written to any Git config file and
    no unrelated test module, operator shell, or production process inherits
    it.

    An inherited ``GIT_CONFIG_COUNT`` is appended to rather than overwritten,
    so an existing environment-supplied setting survives instead of being
    silently dropped. A malformed inherited count raises here rather than being
    coerced to zero: coercing would overwrite slot 0 -- the exact clobber this
    counting exists to avoid -- and Git would reject the value anyway, so the
    loud failure names the real problem instead of one derived from it.

    One hop this deliberately does NOT reach: Git clears ``GIT_CONFIG_COUNT``
    (and its siblings) before entering a *different* repository, so the
    ``receive-pack`` a local ``git push`` starts inside a bare fixture remote
    runs without it and forks maintenance of its own. Bare remotes therefore
    need :data:`GIT_AUTO_MAINTENANCE_SETTING` written into their own config at
    creation time; the environment alone is not enough.
    """
    env = dict(os.environ if base is None else base)
    count = int(env.get("GIT_CONFIG_COUNT", "0"))
    env[f"GIT_CONFIG_KEY_{count}"] = GIT_AUTO_MAINTENANCE_SETTING
    env[f"GIT_CONFIG_VALUE_{count}"] = "false"
    env["GIT_CONFIG_COUNT"] = str(count + 1)
    return env


def disable_auto_maintenance_in_bare_repo(bare_repo: Path) -> None:
    """Persist the maintenance override inside a freshly initialized bare remote.

    Written straight into the repository's config file rather than shelled out
    through ``git config`` because a fixture remote is created once per test:
    a subprocess here would spend roughly as many Git processes as suppressing
    maintenance saves, which is the opposite of the point.

    The leading newline is not cosmetic: appending to a config file that does
    not end in one would splice this section onto the last existing line.
    ``git init --bare`` does end in one, so the blank line is what the reader
    sees and a silently corrupted config is what they do not.
    """
    section, _, key = GIT_AUTO_MAINTENANCE_SETTING.partition(".")
    with (bare_repo / "config").open("a", encoding="utf-8") as handle:
        handle.write(f"\n[{section}]\n\t{key} = false\n")


def git_config_origin_and_value(stdout: str) -> tuple[str, str]:
    """Split ``git config --show-origin --get`` output into (origin, value).

    Asserting on the origin rather than the value alone is what keeps a
    maintenance-override check honest. ``--get`` resolves across every scope,
    so a workstation whose global gitconfig happens to set the same key would
    satisfy a value-only assertion even with every override in this repo
    deleted -- a green test measuring the host instead of the change. The
    environment-delivered override reports the origin ``command line:``.
    """
    origin, _, value = stdout.strip().partition("\t")
    return origin, value


def ps_quote(value: str | Path) -> str:
    """Escape ``value`` for embedding inside a single-quoted PowerShell literal."""
    return "'" + str(value).replace("'", "''") + "'"


def run_pwsh_command(
    command: str,
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run ``command`` via ``pwsh -Command``."""
    return subprocess.run(
        ["pwsh", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=cwd,
        env=env,
        timeout=timeout,
        check=check,
    )


def pwsh_file_argv(script_path: Path, args: list[str] | None = None) -> list[str]:
    """Build the ``pwsh -File`` argument vector for ``script_path``.

    Exposed separately from :func:`run_pwsh_file` for callers that need a
    live ``subprocess.Popen`` handle (e.g. to observe an in-progress process)
    rather than a completed ``subprocess.run`` result.
    """
    return [
        "pwsh",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script_path),
        *(args or []),
    ]


def run_pwsh_file(
    script_path: Path,
    args: list[str] | None = None,
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run ``script_path`` via ``pwsh -File``, with optional trailing ``args``."""
    return subprocess.run(
        pwsh_file_argv(script_path, args),
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=cwd,
        env=env,
        timeout=timeout,
        check=check,
    )
