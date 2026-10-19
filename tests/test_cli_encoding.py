"""Tests for UTF-8 stdout/stderr reconfigure in bes CLI main()."""

from __future__ import annotations

import contextlib
import io
import sys
from unittest.mock import MagicMock, patch

from mailattic.cli import main


def test_reconfigure_called_on_stdout_and_stderr() -> None:
    """main() calls reconfigure(encoding='utf-8', errors='replace') on both streams."""
    mock_stdout = MagicMock(spec=io.TextIOWrapper)
    mock_stderr = MagicMock(spec=io.TextIOWrapper)

    # --help exits via SystemExit; suppress it so we only test reconfigure
    with (
        patch.object(sys, "stdout", mock_stdout),
        patch.object(sys, "stderr", mock_stderr),
        contextlib.suppress(SystemExit),
    ):
        main(["--help"])

    mock_stdout.reconfigure.assert_called_once_with(encoding="utf-8", errors="replace")
    mock_stderr.reconfigure.assert_called_once_with(encoding="utf-8", errors="replace")


def test_reconfigure_unsupported_operation_is_silenced() -> None:
    """If reconfigure raises io.UnsupportedOperation, main() continues without error."""
    mock_stdout = MagicMock(spec=io.TextIOWrapper)
    mock_stderr = MagicMock(spec=io.TextIOWrapper)
    mock_stdout.reconfigure.side_effect = io.UnsupportedOperation("not supported")
    mock_stderr.reconfigure.side_effect = io.UnsupportedOperation("not supported")

    # No exception raised — degraded to no-op as specified
    with (
        patch.object(sys, "stdout", mock_stdout),
        patch.object(sys, "stderr", mock_stderr),
        contextlib.suppress(SystemExit),
    ):
        main(["--help"])
