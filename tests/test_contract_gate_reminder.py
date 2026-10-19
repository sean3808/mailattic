"""
tests/test_contract_gate_reminder.py
====================================
Runnable check for .claude/hooks/contract_gate_reminder.py

跑真正的 hook 入口（subprocess + stdin JSON），因為要驗的三件事都住 `__main__`：
stdout 形狀、節流狀態檔、以及「壞掉時 stderr 看得見」（不再 silent suppress）。

隔離：節流狀態檔路徑取自 `%TEMP%`，所以每個 test 把 TEMP/TMP 指向自己的 tmp_path，
絕不碰真實 %TEMP% 的 mailattic-contract-gate-reminded.txt。
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_HOOK = _REPO_ROOT / ".claude" / "hooks" / "contract_gate_reminder.py"


def _load_hook_module():
    """.claude/hooks/ 不在 sys.path，用 importlib 直接載入檔案取常數。"""
    spec = importlib.util.spec_from_file_location("contract_gate_reminder", _HOOK)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _run(payload: object, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    """跑 hook 一次；payload 是 dict（會 json dump）或 raw str（malformed 用）。"""
    env = {
        **os.environ,
        "TEMP": str(tmp_path),
        "TMP": str(tmp_path),
        "PYTHONIOENCODING": "utf-8",
    }
    stdin = payload if isinstance(payload, str) else json.dumps(payload)
    return subprocess.run(
        [sys.executable, str(_HOOK)],
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        check=False,
    )


def _post_edit(file_path: str, **extra: str) -> dict:
    return {
        "hook_event_name": "PostToolUse",
        "session_id": "sess-1",
        "tool_name": "Edit",
        "tool_input": {"file_path": file_path, **extra},
    }


def _assert_reminder(proc: subprocess.CompletedProcess[str]) -> None:
    assert proc.returncode == 0, proc.stderr
    parsed = json.loads(proc.stdout)
    out = parsed["hookSpecificOutput"]
    assert out["hookEventName"] == "PostToolUse"
    assert "行為契約" in out["additionalContext"]


def test_edit_of_anchor_test_reminds(tmp_path):
    """anchor test 檔被 Edit → additionalContext 提醒，exit 0。"""
    _assert_reminder(_run(_post_edit("tests/test_mcp_server.py"), tmp_path))


def test_bash_command_mentioning_anchor_test_reminds(tmp_path):
    """Bash 通道：命令字串提到 anchor test 檔名就響（刻意寧可多響）。"""
    payload = {
        "hook_event_name": "PostToolUse",
        "session_id": "sess-1",
        "tool_name": "Bash",
        "tool_input": {"command": "sed -i s/x/y/ tests/test_outbound_send.py"},
    }
    _assert_reminder(_run(payload, tmp_path))


def test_agents_md_outside_cwd_still_reminds(tmp_path):
    """AGENTS.md 不在 cwd 底下也要響（原本的 cwd 比對會靜默漏響）。"""
    far_away = tmp_path / "elsewhere" / "AGENTS.md"
    payload = _post_edit(str(far_away), new_string="改了 行為契約 那段")
    _assert_reminder(_run(payload, tmp_path))


def test_agents_md_without_contract_keyword_is_quiet(tmp_path):
    """AGENTS.md 被改但內容與契約無關 → 不響（關鍵詞條件仍在）。"""
    payload = _post_edit(str(tmp_path / "AGENTS.md"), new_string="改了常用指令那段")
    proc = _run(payload, tmp_path)
    assert proc.returncode == 0
    assert proc.stdout == ""


def test_second_identical_payload_is_throttled(tmp_path):
    """同 session 同通道第二次 → 無輸出（節流），exit 仍 0。"""
    payload = _post_edit("tests/test_date_bounds.py")
    _assert_reminder(_run(payload, tmp_path))
    second = _run(payload, tmp_path)
    assert second.returncode == 0
    assert second.stdout == ""


def test_unrelated_file_is_quiet(tmp_path):
    """與契約無關的檔案 → 無輸出，exit 0。"""
    proc = _run(_post_edit("src/mailattic/query.py"), tmp_path)
    assert proc.returncode == 0
    assert proc.stdout == ""


def test_malformed_stdin_reports_to_stderr_and_exits_zero(tmp_path):
    """壞掉不 block、但也不靜默：stderr 印出例外，exit 0。"""
    proc = _run("this is not json", tmp_path)
    assert proc.returncode == 0
    assert "[contract-gate-reminder]" in proc.stderr


def test_anchor_tests_match_agents_md_contract_section():
    """
    hook 的 ANCHOR_TESTS 是 AGENTS.md `## 行為契約` 表的第二份拷貝，兩邊沒有東西綁住。
    改名或新增 anchor test 只動一邊，gate 就靜默瞎掉（= 本 hook 存在要修的失效類型）。
    """
    lines = (_REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8").splitlines()
    start = lines.index("## Behaviour contracts (行為契約)")
    end = next(
        i for i, ln in enumerate(lines[start + 1 :], start + 1) if ln.startswith("## ")
    )
    section = "\n".join(lines[start:end])

    in_agents_md = set(re.findall(r"tests/(test_\w+\.py)", section))
    assert in_agents_md, "AGENTS.md `## 行為契約` 段裡找不到任何 anchor test 路徑"
    assert in_agents_md == _load_hook_module().ANCHOR_TESTS
