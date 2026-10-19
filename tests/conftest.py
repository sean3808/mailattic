"""Shared pytest fixtures and mailbox access security guards."""

from __future__ import annotations

import builtins
import ipaddress
import os
import socket
import sys
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest

import mailattic.imap_runtime as imap_runtime
from mailattic import archive_scrape, imap_sync, outbound


class MailboxGuardError(Exception):
    """Base exception for unauthorized mailbox access in test runs."""

    __test__ = False


class TestConnectionGuardError(MailboxGuardError):
    """Raised when test code attempts a socket connection to a non-loopback address."""

    __test__ = False


class TestCredentialsGuardError(MailboxGuardError):
    """Raised when test code attempts to load real mailbox credentials."""

    __test__ = False


_REPO_ROOT = Path(__file__).resolve().parents[1]
# Derived from the production defaults, so the guard follows them if they move.
_PROTECTED_CREDENTIALS_PATHS = {
    Path(default).resolve()
    for default in (
        imap_sync.DEFAULT_ENV_FILE,
        outbound.DEFAULT_SMTP_ENV_FILE,
        archive_scrape.CREDENTIALS_ENV_PATH,
        _REPO_ROOT / ".env.sean3808",
        _REPO_ROOT / ".env",
    )
}
_PROTECTED_BASENAMES = {path.name for path in _PROTECTED_CREDENTIALS_PATHS}

_current_test_nodeid: str | None = None


def get_current_test_nodeid() -> str:
    """Return the node ID of the currently executing pytest item, or fallback."""
    return _current_test_nodeid or "<unknown test>"


def is_loopback_host(host: str | None) -> bool:
    """Return True if host is localhost or a valid loopback IP address."""
    if not host:
        return False
    host_clean = host.strip().lower()
    if host_clean == "localhost" or host_clean.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(host_clean)
        return ip.is_loopback
    except ValueError:
        return False


def is_protected_credentials_path(path: str | Path | os.PathLike[str]) -> bool:
    """Return True if path resolves to a production credentials file."""
    try:
        p = Path(path)
        if p.name not in _PROTECTED_BASENAMES:
            return False
        return p.expanduser().resolve() in _PROTECTED_CREDENTIALS_PATHS
    except Exception:
        return False


# --- Original functions ---
_orig_create_connection = socket.create_connection
_orig_socket_connect = socket.socket.connect
_orig_path_open = Path.open
_orig_builtin_open = builtins.open
_orig_load_env_file = imap_runtime.load_env_file


def _assert_allowed_destination(host: Any, port: Any) -> None:
    if isinstance(host, str) and not is_loopback_host(host):
        violating = get_current_test_nodeid()
        raise TestConnectionGuardError(
            f"[MailboxGuard: Connection] Outbound connection blocked: "
            f"attempted connection to non-loopback host {host}:{port} "
            f"(violating test: {violating})"
        )


def _raise_credentials_violation(
    target_path: str | Path | os.PathLike[str],
    action: str = "read",
) -> None:
    violating = get_current_test_nodeid()
    resolved = Path(target_path).expanduser().resolve()
    raise TestCredentialsGuardError(
        f"[MailboxGuard: Credentials] Test attempted to {action} real credentials "
        f"file: {resolved} (violating test: {violating})"
    )


def _guarded_create_connection(
    address: tuple[Any, ...] | list[Any] | Any,
    *args: Any,
    **kwargs: Any,
) -> socket.socket:
    host = None
    port = None
    if isinstance(address, tuple | list) and len(address) > 0:
        host = address[0]
        if len(address) > 1:
            port = address[1]
    _assert_allowed_destination(host, port)
    return _orig_create_connection(address, *args, **kwargs)


def _guarded_socket_connect(self: socket.socket, address: Any) -> None:
    if isinstance(address, tuple | list) and len(address) >= 2:
        host = address[0]
        port = address[1]
        _assert_allowed_destination(host, port)
    return _orig_socket_connect(self, address)


def _guarded_path_open(self: Path, *args: Any, **kwargs: Any) -> Any:
    if self.name in _PROTECTED_BASENAMES and is_protected_credentials_path(self):
        _raise_credentials_violation(self, action="read")
    return _orig_path_open(self, *args, **kwargs)


def _guarded_builtin_open(file: Any, *args: Any, **kwargs: Any) -> Any:
    if isinstance(file, str | Path | os.PathLike):
        basename = os.path.basename(os.fspath(file))
        if basename in _PROTECTED_BASENAMES and is_protected_credentials_path(file):
            _raise_credentials_violation(file, action="read")
    return _orig_builtin_open(file, *args, **kwargs)


def _guarded_load_env_file(env_file: str | Path) -> dict[str, str]:
    if is_protected_credentials_path(env_file):
        _raise_credentials_violation(env_file, action="load")
    return _orig_load_env_file(env_file)


def _sync_modules_load_env_file() -> None:
    imap_runtime.load_env_file = _guarded_load_env_file
    for mod_name in (
        "mailattic.imap_sync",
        "mailattic.outbound",
        "mailattic.cli",
        "mailattic.mcp_writer",
    ):
        if mod_name in sys.modules:
            mod = sys.modules[mod_name]
            if hasattr(mod, "load_env_file"):
                mod.load_env_file = _guarded_load_env_file


# Install global hooks on session start
socket.create_connection = _guarded_create_connection
socket.socket.connect = _guarded_socket_connect
Path.open = _guarded_path_open
builtins.open = _guarded_builtin_open
imap_runtime.load_env_file = _guarded_load_env_file


@pytest.fixture(autouse=True)
def guard_test_mailbox(request: pytest.FixtureRequest) -> Generator[None]:
    """Track current test node ID and ensure guards remain active."""
    global _current_test_nodeid
    previous = _current_test_nodeid
    _current_test_nodeid = request.node.nodeid
    _sync_modules_load_env_file()
    try:
        yield
    finally:
        _current_test_nodeid = previous


@pytest.fixture
def blind_host_mime_map(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the outbound host extension map answer nothing."""
    monkeypatch.setattr(
        "mailattic.outbound.mimetypes.guess_type",
        lambda *_args, **_kwargs: (None, None),
    )
