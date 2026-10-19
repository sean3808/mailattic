"""The two pre-submission system errors of ``mail_send`` say "not sent" (#307).

Before anything reaches SMTP, ``mail_send`` can hit (a) a draft file that the
OS refuses to read and (b) a failure writing the send-once marker. Both used
to surface as the bare OS error text, with no hint that the mail was not sent
and may be retried. Like the #292 messages, the outcome and next step must sit
at the front, inside the 200-character echo cap. No mail is sent: SMTP is an
in-process double and is asserted untouched.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mailattic.mcp_delivery import ERROR_ECHO_CAP_CHARS
from mailattic.outbound import _marker_path
from tests.test_mcp_send_error_budget import (
    FAILED_PREFIX,
    FRAMEWORK_PREFIX,
    LONG,
    Ctx,
    _call,
    _long_ids,
    ctx,  # noqa: F401 -- pytest fixture
)


def _fail_read(c: Ctx) -> None:
    _long_ids(c)
    target = c.draft_path
    real = Path.read_bytes

    def read_bytes(self: Path) -> bytes:
        if self == target:
            raise PermissionError(13, LONG, str(self))
        return real(self)

    c.monkeypatch.setattr(Path, "read_bytes", read_bytes)


def _fail_marker_write(c: Ctx) -> None:
    _long_ids(c)

    def boom(*_a: object, **_k: object) -> None:
        raise OSError(28, LONG)

    c.monkeypatch.setattr("mailattic.outbound._write_marker", boom)


@pytest.mark.parametrize("fail", [_fail_read, _fail_marker_write])
def test_preflight_system_error_says_not_sent_and_next_step_first(
    fail,
    ctx: Ctx,  # noqa: F811
) -> None:
    fail(ctx)

    res = _call(
        ctx.server,
        "mail_send",
        {"draft_id": ctx.draft_id, "fingerprint": ctx.fingerprint},
    )

    assert res.is_error
    text = res.content[0].text
    assert text.startswith(FRAMEWORK_PREFIX + FAILED_PREFIX)
    detail = text[len(FRAMEWORK_PREFIX) + len(FAILED_PREFIX) :]
    head = detail.split("...(truncated", 1)[0]
    # Outcome ("Not sent") leads; the retry next step follows before the cause.
    assert head.startswith("Not sent"), text
    assert "call mail_send again" in head, text
    assert len(head) <= ERROR_ECHO_CAP_CHARS, text
    ctx.smtp.sendmail.assert_not_called()
    assert not _marker_path(ctx.db_path, ctx.message_id).exists()


def test_os_error_after_smtp_is_never_called_not_sent(
    ctx: Ctx,  # noqa: F811
) -> None:
    """An OSError escaping after the marker was written must not be relabelled
    'Not sent' -- the mail may be out, and a retry would send it twice."""
    _long_ids(ctx)

    def late_failure(**_k: object) -> None:
        raise OSError(5, "late I/O failure after submission")

    ctx.monkeypatch.setattr("mailattic.outbound._append_sent_copy", late_failure)

    res = _call(
        ctx.server,
        "mail_send",
        {"draft_id": ctx.draft_id, "fingerprint": ctx.fingerprint},
    )

    assert res.is_error
    assert "Not sent" not in res.content[0].text
    assert _marker_path(ctx.db_path, ctx.message_id).exists()
