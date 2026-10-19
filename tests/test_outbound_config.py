"""Tests for outbound SMTP config reader and legacy TLS context (tasks 1.2 and 1.3).

These tests are unit-level and require no live SMTP server.  The real-server
TLS handshake smoke test (connect to mail.mailattic.test:465) is a manual
operator step to be run before the first production send.
"""

from __future__ import annotations

import ssl
from pathlib import Path

import pytest

from mailattic.outbound import (
    SmtpConfig,
    build_smtp_ssl_context,
    load_smtp_config,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_VALID_ENV: dict[str, str] = {
    "SMTP_HOST": "mail.mailattic.test",
    "SMTP_PORT": "465",
    "SMTP_USER": "sean3808@mailattic.test",
    "SMTP_PASSWORD": "test-secret",
}


# ---------------------------------------------------------------------------
# Task 1.2 — SMTP config reader
# ---------------------------------------------------------------------------


class TestLoadSmtpConfig:
    def test_parses_valid_env_dict(self) -> None:
        cfg = load_smtp_config(_VALID_ENV)
        assert isinstance(cfg, SmtpConfig)
        assert cfg.host == "mail.mailattic.test"
        assert cfg.port == 465
        assert cfg.user == "sean3808@mailattic.test"
        assert cfg.password == "test-secret"

    def test_port_is_int(self) -> None:
        cfg = load_smtp_config(_VALID_ENV)
        assert isinstance(cfg.port, int)

    def test_missing_smtp_password_raises(self) -> None:
        env = {k: v for k, v in _VALID_ENV.items() if k != "SMTP_PASSWORD"}
        with pytest.raises(ValueError, match="SMTP_PASSWORD"):
            load_smtp_config(env)

    def test_empty_smtp_password_raises(self) -> None:
        env = {**_VALID_ENV, "SMTP_PASSWORD": ""}
        with pytest.raises(ValueError, match="SMTP_PASSWORD"):
            load_smtp_config(env)

    def test_whitespace_only_smtp_password_raises(self) -> None:
        env = {**_VALID_ENV, "SMTP_PASSWORD": "   "}
        with pytest.raises(ValueError, match="SMTP_PASSWORD"):
            load_smtp_config(env)

    def test_missing_smtp_host_raises(self) -> None:
        env = {k: v for k, v in _VALID_ENV.items() if k != "SMTP_HOST"}
        with pytest.raises(ValueError, match="SMTP_HOST"):
            load_smtp_config(env)

    def test_missing_smtp_user_raises(self) -> None:
        env = {k: v for k, v in _VALID_ENV.items() if k != "SMTP_USER"}
        with pytest.raises(ValueError, match="SMTP_USER"):
            load_smtp_config(env)

    def test_non_integer_port_raises(self) -> None:
        env = {**_VALID_ENV, "SMTP_PORT": "not-a-port"}
        with pytest.raises(ValueError, match="SMTP_PORT"):
            load_smtp_config(env)

    def test_loads_from_env_file(self, tmp_path: Path) -> None:
        env_file = tmp_path / ".env.test"
        env_file.write_text(
            "SMTP_HOST=mail.example.com\n"
            "SMTP_PORT=465\n"
            "SMTP_USER=user@example.com\n"
            "SMTP_PASSWORD=secret123\n",
            encoding="utf-8",
        )
        cfg = load_smtp_config(env_file)
        assert cfg.host == "mail.example.com"
        assert cfg.port == 465
        assert cfg.user == "user@example.com"
        assert cfg.password == "secret123"

    def test_loads_from_env_file_path_str(self, tmp_path: Path) -> None:
        env_file = tmp_path / ".env.test"
        env_file.write_text(
            "SMTP_HOST=mail.example.com\n"
            "SMTP_PORT=465\n"
            "SMTP_USER=user@example.com\n"
            "SMTP_PASSWORD=secret123\n",
            encoding="utf-8",
        )
        cfg = load_smtp_config(str(env_file))
        assert cfg.port == 465

    def test_env_file_not_found_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            load_smtp_config(tmp_path / "nonexistent.env")

    def test_strips_whitespace_and_quotes_from_values(self) -> None:
        env = {
            "SMTP_HOST": "  mail.mailattic.test  ",
            "SMTP_PORT": " 465 ",
            "SMTP_USER": "  sean3808@mailattic.test  ",
            "SMTP_PASSWORD": "  secret  ",
        }
        cfg = load_smtp_config(env)
        assert cfg.host == "mail.mailattic.test"
        assert cfg.user == "sean3808@mailattic.test"
        assert cfg.password == "secret"


# ---------------------------------------------------------------------------
# Task 1.3 — SMTP TLS context aligned with the legacy mail server
# ---------------------------------------------------------------------------


class TestBuildSmtpSslContext:
    def test_check_hostname_is_false(self) -> None:
        ctx = build_smtp_ssl_context()
        assert ctx.check_hostname is False

    def test_verify_mode_is_cert_none(self) -> None:
        ctx = build_smtp_ssl_context()
        assert ctx.verify_mode == ssl.CERT_NONE

    def test_returns_ssl_context(self) -> None:
        ctx = build_smtp_ssl_context()
        assert isinstance(ctx, ssl.SSLContext)

    def test_each_call_returns_independent_context(self) -> None:
        ctx1 = build_smtp_ssl_context()
        ctx2 = build_smtp_ssl_context()
        assert ctx1 is not ctx2
