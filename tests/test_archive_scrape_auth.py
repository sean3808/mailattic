"""Unit tests for archive_scrape Group 2: credentials loader, session builder,
and failure classifier (tasks 2.1 / 2.2 / 2.3).
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests

from mailattic.archive_scrape import (
    CREDENTIALS_ENV_PATH,
    CREDENTIALS_ENV_VAR,
    EnvMissingFieldError,
    EnvNotFoundError,
    MlsAuthFailedError,
    MlsCredentials,
    MlsSessionInvalidError,
    MlsUnreachableError,
    _default_credentials_env_path,
    build_session,
    classify_request_failure,
    load_credentials,
)


class TestLoadCredentials(unittest.TestCase):
    """task 2.1 — load_credentials() unit tests."""

    def _write_env(self, tmp_path: Path, content: str) -> Path:
        env_file = tmp_path / ".env"
        env_file.write_text(content, encoding="utf-8")
        return env_file

    def test_all_keys_present_returns_mlscredentials(
        self,
        tmp_path: Path | None = None,
    ) -> None:
        """env 檔齊全三 key → 回 MlsCredentials 三 field 正確。"""
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            env_file = self._write_env(
                Path(td),
                "MLS_URL=https://192.0.2.10:9443\nMLS_USER=admin\nMLS_PASS=s3cr3t\n",
            )
            creds = load_credentials(env_file)

        self.assertIsInstance(creds, MlsCredentials)
        self.assertEqual(creds.url, "https://192.0.2.10:9443")
        self.assertEqual(creds.user, "admin")
        self.assertEqual(creds.password, "s3cr3t")

    def test_missing_env_file_raises_env_not_found_error(self) -> None:
        """env 檔缺檔（指向不存在路徑）→ raise EnvNotFoundError。"""
        nonexistent = Path("/nonexistent/path/.env")
        with self.assertRaises(EnvNotFoundError) as ctx:
            load_credentials(nonexistent)

        self.assertIn("MAIL_SERVER_ENV_NOT_FOUND", str(ctx.exception))
        self.assertEqual(ctx.exception.exit_code, 1)

    def test_missing_mls_pass_raises_env_missing_field_error(self) -> None:
        """env 檔存在但缺 MLS_PASS → raise EnvMissingFieldError。"""
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            env_file = self._write_env(
                Path(td),
                "MLS_URL=https://192.0.2.10:9443\nMLS_USER=admin\n",
            )
            with self.assertRaises(EnvMissingFieldError) as ctx:
                load_credentials(env_file)

        self.assertIn("MAIL_SERVER_ENV_MISSING_FIELD", str(ctx.exception))
        self.assertIn("MLS_PASS", str(ctx.exception))
        self.assertEqual(ctx.exception.exit_code, 1)

    def test_password_not_in_exception_message(self) -> None:
        """密碼值不得出現在任何 exception message 中。"""
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            env_file = self._write_env(
                Path(td),
                "MLS_URL=https://192.0.2.10:9443\n"
                "MLS_USER=admin\n"
                "MLS_PASS=super_secret_password\n",
            )
            creds = load_credentials(env_file)

        # repr of MlsCredentials should not expose password
        self.assertNotIn("super_secret_password", repr(creds))

    def test_value_with_equals_sign_parsed_correctly(self) -> None:
        """值含 '=' 時（如 base64 密碼）應用 split('=', 1) 正確解析。"""
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            env_file = self._write_env(
                Path(td),
                "MLS_URL=https://192.0.2.10:9443\nMLS_USER=admin\nMLS_PASS=abc=xyz==\n",
            )
            creds = load_credentials(env_file)

        self.assertEqual(creds.password, "abc=xyz==")


class TestBuildSession(unittest.TestCase):
    """task 2.2 — build_session() unit tests（不連真實郵件伺服器）。"""

    def _make_creds(self) -> MlsCredentials:

        return MlsCredentials(
            url="https://192.0.2.10:9443",
            user="admin",
            password="secret",
        )

    def test_session_auth_set_to_user_and_password(self) -> None:
        """session.auth 應設為 (user, password) tuple。"""
        creds = self._make_creds()
        session = build_session(creds)
        self.assertEqual(session.auth, ("admin", "secret"))

    def test_session_verify_false_by_default(self) -> None:
        """預設 verify=False（郵件伺服器自簽憑證）。"""
        creds = self._make_creds()
        session = build_session(creds)
        self.assertFalse(session.verify)

    def test_session_verify_can_be_overridden(self) -> None:
        """verify=True 時 session.verify 應為 True。"""
        creds = self._make_creds()
        session = build_session(creds, verify=True)
        self.assertTrue(session.verify)

    def test_returns_requests_session_instance(self) -> None:
        """回傳值應為 requests.Session 實例。"""
        creds = self._make_creds()
        session = build_session(creds)
        self.assertIsInstance(session, requests.Session)


class TestClassifyRequestFailure(unittest.TestCase):
    """task 2.3 — classify_request_failure() unit tests。"""

    def test_connection_error_maps_to_mls_unreachable(self) -> None:
        """requests.ConnectionError → MlsUnreachableError（exit 2）。"""
        exc = requests.exceptions.ConnectionError("Name resolution failed")
        result = classify_request_failure(exc, after_login=False)
        self.assertIs(result, MlsUnreachableError)
        self.assertEqual(result.exit_code, 2)

    def test_timeout_maps_to_mls_unreachable(self) -> None:
        """requests.Timeout → MlsUnreachableError。"""
        exc = requests.exceptions.Timeout("timed out")
        result = classify_request_failure(exc, after_login=False)
        self.assertIs(result, MlsUnreachableError)

    def test_401_at_login_maps_to_mls_auth_failed(self) -> None:
        # after_login=False + 401 → MlsAuthFailedError (exit 3)
        exc = MagicMock()
        exc.status_code = 401
        # Not a ConnectionError / Timeout, has status_code attribute
        result = classify_request_failure(exc, after_login=False)
        self.assertIs(result, MlsAuthFailedError)
        self.assertEqual(result.exit_code, 3)

    def test_401_after_login_maps_to_mls_session_invalid(self) -> None:
        # after_login=True + 401 → MlsSessionInvalidError (exit 4)
        exc = MagicMock()
        exc.status_code = 401
        result = classify_request_failure(exc, after_login=True)
        self.assertIs(result, MlsSessionInvalidError)
        self.assertEqual(result.exit_code, 4)

    def test_exception_classes_have_correct_exit_codes(self) -> None:
        """各 exception class 的 exit_code 屬性對應常數。"""
        self.assertEqual(EnvNotFoundError.exit_code, 1)
        self.assertEqual(EnvMissingFieldError.exit_code, 1)
        self.assertEqual(MlsUnreachableError.exit_code, 2)
        self.assertEqual(MlsAuthFailedError.exit_code, 3)
        self.assertEqual(MlsSessionInvalidError.exit_code, 4)

    def test_exception_classes_inherit_runtime_error(self) -> None:
        """各 exception class 應繼承 RuntimeError。"""
        for cls in (
            EnvNotFoundError,
            EnvMissingFieldError,
            MlsUnreachableError,
            MlsAuthFailedError,
            MlsSessionInvalidError,
        ):
            self.assertTrue(
                issubclass(cls, RuntimeError),
                f"{cls.__name__} not RuntimeError",
            )


class TestDefaultCredentialsEnvPath(unittest.TestCase):
    """fix-credentials-path-wsl-portability — env-var override unit tests."""

    def test_default_path_uses_hardcoded_when_env_var_unset(self) -> None:
        """未設 MAIL_SERVER_ENV_PATH → 回 hardcoded 預設 CREDENTIALS_ENV_PATH。"""
        env_without = {k: v for k, v in os.environ.items() if k != CREDENTIALS_ENV_VAR}
        with patch.dict(os.environ, env_without, clear=True):
            result = _default_credentials_env_path()
        self.assertEqual(result, CREDENTIALS_ENV_PATH)

    def test_default_path_uses_env_var_when_set(self) -> None:
        """設定 MAIL_SERVER_ENV_PATH → 回 Path(override)。"""
        override = "/mnt/x/example/.env"
        with patch.dict(os.environ, {CREDENTIALS_ENV_VAR: override}):
            result = _default_credentials_env_path()
        self.assertEqual(result, Path(override))

    def test_default_path_empty_env_var_falls_back(self) -> None:
        """設定空字串 → 回 hardcoded 預設（spec: Empty treated as unset）。"""
        with patch.dict(os.environ, {CREDENTIALS_ENV_VAR: ""}):
            result = _default_credentials_env_path()
        self.assertEqual(result, CREDENTIALS_ENV_PATH)

    def test_load_credentials_raises_for_invalid_env_var_path(self) -> None:
        """MAIL_SERVER_ENV_PATH 指向不存在檔案 → raise EnvNotFoundError 含路徑。

        路徑 stringify 在不同 OS 上 separator 不同（Windows 用 \\，POSIX 用 /），
        故 assertion 用 Path object 直接比對而非字串 substring。
        """
        nonexistent = "/nonexistent/path/.env"
        with (
            patch.dict(os.environ, {CREDENTIALS_ENV_VAR: nonexistent}),
            self.assertRaises(EnvNotFoundError) as ctx,
        ):
            load_credentials()
        self.assertIn(str(Path(nonexistent)), str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
