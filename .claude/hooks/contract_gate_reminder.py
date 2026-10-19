"""行為契約 gate 非強制提醒 hook（UserPromptSubmit + PostToolUse 共用）。

含 Bash-blindness 修正。
觸發 = prompt 含契約關鍵詞，或編輯 AGENTS.md（內容含契約詞）/ anchor test 檔。
Bash 通道只看命令字串有沒有提到那些檔名——不分辨讀或寫，寧可多響一次：漏響的代價是
gate 被繞過（試點於 2026-08-19 實際發生），多響的代價是一行提醒。
提醒走 additionalContext（只達 AI），每 session 每通道最多一次，永不 block。
"""

import json
import os
import re
import sys
from pathlib import Path

KEYWORDS = re.compile(
    r"契約|anchor\s*test|reconcile|behaviou?r\s+contract", re.IGNORECASE
)
ANCHOR_TESTS = {
    "test_mcp_server.py",
    "test_outbound_send.py",
    "test_imap_append.py",
    "test_date_bounds.py",
    "test_imap_sync_ingest_accounting.py",
    "test_rebuild.py",
    "test_mcp_send.py",
}
CANONICAL_DOC = "AGENTS.md"  # `## 行為契約` 段所在的檔
MSG = (
    "本操作觸及行為契約領域（非強制提醒）。本專案規則：動到行為契約條目或其 anchor test"
    "的變更必須走 PR + /spec-reconcile；anchor test 檔案改動另須跨模型語意審查"
    "（由 /closeout 的 semantic-review 格履行，收尾定 Medium 以上）。"
    f"正典：{CANONICAL_DOC} ## Behaviour contracts (行為契約) 段（D-20260820-3）。"
    "與契約無關的操作可忽略本提醒。"
)


def detect_channel(data: dict, event: str) -> str | None:
    if event == "UserPromptSubmit":
        return "prompt" if KEYWORDS.search(data.get("prompt", "")) else None
    if event == "PostToolUse":
        tool_input = data.get("tool_input") or {}
        fp = tool_input.get("file_path", "")
        if not fp:
            # Bash 通道：Edit/Write 以外的改檔管道（heredoc、sed -i、腳本）走這裡
            cmd = str(tool_input.get("command", ""))
            watched = (*ANCHOR_TESTS, CANONICAL_DOC)
            return "file" if any(n in cmd for n in watched) else None
        p = Path(fp)
        if p.name in ANCHOR_TESTS:
            return "file"
        # 不比對 cwd：cwd 非 repo root 時比對會靜默不響，與 Bash 通道的寧可多響相矛盾。
        # 關鍵詞條件仍在，與契約無關的 AGENTS.md 編輯照樣安靜。
        if p.name == CANONICAL_DOC:
            edited = " ".join(
                str(tool_input.get(k, ""))
                for k in ("old_string", "new_string", "content")
            )
            return "file" if KEYWORDS.search(edited) else None
    return None


def main() -> None:
    data = json.loads(sys.stdin.buffer.read() or b"{}")
    event = data.get("hook_event_name", "")
    channel = detect_channel(data, event)
    if not channel:
        return

    # ponytail: 節流狀態放 %TEMP% 純文字、無限增長（~2 行/session）；真要收拾再加輪替
    state = (
        Path(os.environ.get("TEMP", "/tmp")) / "mailattic-contract-gate-reminded.txt"
    )
    tag = f"{data.get('session_id', 'unknown')}:{channel}"
    seen = state.read_text(encoding="utf-8").splitlines() if state.exists() else []
    if tag in seen:
        return
    state.write_text("\n".join([*seen, tag]) + "\n", encoding="utf-8")

    sys.stdout.reconfigure(encoding="utf-8")
    if event == "UserPromptSubmit":
        print(MSG)
    else:
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PostToolUse",
                        "additionalContext": MSG,
                    }
                },
                ensure_ascii=False,
            )
        )


if __name__ == "__main__":
    try:
        main()
    except (
        Exception
    ) as exc:  # 提醒 hook 永不擋工具，但壞掉要看得見（靜默 = gate 靜悄悄被繞過）
        print(f"[contract-gate-reminder] {exc!r}", file=sys.stderr)
    sys.exit(0)
