"""
tests/test_skill_doc_cli_flags.py
==================================
機械防線：偵測 skills/mailattic/**/*.md 裡的 ``bes`` 範例呼叫
是否使用了實際 CLI 不存在的 flag。

背景
-----
`capability-b-find.md` 曾出現「bes search --format json」，但 ``search``
子命令只有 ``--json``，沒有 ``--format``。這個 test 在 CI / pre-commit 時
自動抓出此類漂移，防止復發。

做法
-----
1. 直接 introspect ``build_parser()``（不解析 --help 文字），取得每個
   （巢狀）子命令的合法 long option set。
2. 遞迴掃 skills/mailattic/**/*.md，從 bash/sh fenced code block
   與 prose inline code span 中抽取以 ``bes `` 開頭的命令行。
3. 對每個 ``--flag`` 斷言它出現在該子命令的合法 set 內。
4. 包含 self-check（red case）：餵含 ``bes search --format json`` 的假 markdown
   給抽取函式，確認它會產生違規，證明 detector 非空殼。

純函式設計：``collect_valid_flags``、``extract_bes_lines``、
``check_flags_in_lines`` 可獨立單元測試。
"""

from __future__ import annotations

import argparse
import re
import shlex
import textwrap
from pathlib import Path

# ── 1. parser introspection ───────────────────────────────────────────────────
from mailattic.cli import build_parser

_SKILL_ROOT = Path(__file__).resolve().parent.parent / "skills" / "mailattic"


def collect_valid_flags() -> dict[tuple[str, ...], set[str]]:
    """
    走訪 build_parser() 回傳的 ArgumentParser，收集每個（巢狀）子命令
    的合法 ``--flag`` set。

    回傳 dict：
      key   = 子命令路徑 tuple，例如 ``("search",)``、``("db", "backup")``
      value = 合法 ``--flag`` 字串 set（含 ``--help``）
    """
    parser = build_parser()

    def _long_opts(p: argparse.ArgumentParser) -> set[str]:
        result: set[str] = set()
        for action in p._actions:
            for opt in action.option_strings:
                if opt.startswith("--"):
                    result.add(opt)
        return result

    valid: dict[tuple[str, ...], set[str]] = {}

    # 取頂層 subparsers
    top_subparsers_actions = [
        a for a in parser._actions if isinstance(a, argparse._SubParsersAction)
    ]
    if not top_subparsers_actions:
        return valid

    top_choices: dict[str, argparse.ArgumentParser] = (
        top_subparsers_actions[0].choices or {}
    )

    for cmd_name, cmd_parser in top_choices.items():
        key = (cmd_name,)
        valid[key] = _long_opts(cmd_parser)

        # 若此子命令下還有 sub-subparsers（例：db → backup/doctor/…）
        nested_actions = [
            a for a in cmd_parser._actions if isinstance(a, argparse._SubParsersAction)
        ]
        for nested_action in nested_actions:
            for sub_name, sub_parser in (nested_action.choices or {}).items():
                valid[(cmd_name, sub_name)] = _long_opts(sub_parser)

    return valid


# ── 2. markdown 掃描 / 命令行抽取 ─────────────────────────────────────────────

_FENCE_PATTERN = re.compile(
    r"```(?:bash|sh)\n(.*?)```",
    re.DOTALL,
)
_INLINE_CODE_PATTERN = re.compile(r"`([^`]+)`")


def extract_bes_lines(markdown_text: str) -> list[str]:
    """
    從 markdown 文字裡抽取所有以 ``bes `` 開頭的邏輯命令行。

    規則：
    - 從 ```bash 或 ```sh fenced block 取內容（不掃 ```python block）。
    - 額外掃 prose 裡的 inline code span（單反引號）。
    - 跳過 ``#`` 開頭的註解行。
    - 處理反斜線行接續（以 ``\\`` 結尾的行合併下一行）。
    - 結果是「去掉前導/尾隨空白後以 ``bes `` 開頭」的邏輯行。
    """
    raw_lines: list[str] = []

    # (a) fenced code blocks（bash / sh）
    for block in _FENCE_PATTERN.findall(markdown_text):
        raw_lines.extend(block.splitlines())

    # (b) prose inline code spans
    for span in _INLINE_CODE_PATTERN.findall(markdown_text):
        # inline span 可能是多行（反引號跨行罕見，但簡單 split 即可）
        raw_lines.extend(span.splitlines())

    # 合併反斜線行接續，再篩選出 bes 命令行
    logical_lines: list[str] = []
    buf: list[str] = []

    def _flush(parts: list[str]) -> None:
        joined = " ".join(p.rstrip("\\").strip() for p in parts if p.strip())
        stripped = joined.strip()
        if stripped.startswith("bes "):
            logical_lines.append(stripped)

    for raw in raw_lines:
        stripped = raw.strip()
        if stripped.startswith("#"):
            # 遇到注釋行：先 flush 暫存（防止上一 continuation 未收尾），然後跳過
            if buf:
                _flush(buf)
                buf = []
            continue

        if buf:
            # 前一行有 backslash continuation
            buf.append(stripped)
            if not stripped.endswith("\\"):
                _flush(buf)
                buf = []
        else:
            if stripped.endswith("\\"):
                buf.append(stripped)
            else:
                if stripped.startswith("bes "):
                    logical_lines.append(stripped)

    if buf:
        _flush(buf)

    return logical_lines


# ── 3. 驗證函式 ───────────────────────────────────────────────────────────────


class FlagViolation:
    """單一違規記錄。"""

    def __init__(
        self,
        filepath: Path,
        line_content: str,
        subcmd_key: tuple[str, ...],
        illegal_flag: str,
        valid_flags: set[str],
    ) -> None:
        self.filepath = filepath
        self.line_content = line_content
        self.subcmd_key = subcmd_key
        self.illegal_flag = illegal_flag
        self.valid_flags = valid_flags

    def __repr__(self) -> str:
        valid_sorted = sorted(self.valid_flags)
        return (
            f"\n  File : {self.filepath}\n"
            f"  Line : {self.line_content!r}\n"
            f"  Cmd  : bes {' '.join(self.subcmd_key)}\n"
            f"  Bad  : {self.illegal_flag}\n"
            f"  Valid: {valid_sorted}"
        )


def check_flags_in_lines(
    lines: list[str],
    valid_flags: dict[tuple[str, ...], set[str]],
    filepath: Path,
) -> list[FlagViolation]:
    """
    對 ``lines``（已是邏輯行，以 ``bes `` 開頭）中的每個 ``--flag``
    檢查是否在 ``valid_flags`` 對應子命令的合法 set 中。

    回傳所有違規的 FlagViolation list（空 list 代表無違規）。
    """
    violations: list[FlagViolation] = []

    for line in lines:
        # tokenize（posix=True 讓 shlex 理解單/雙引號）
        try:
            tokens = shlex.split(line, posix=True)
        except ValueError:
            # shlex 解析失敗（未配對引號等）→ 跳過此行，不誤判
            continue

        if not tokens or tokens[0] != "bes":
            continue
        if len(tokens) < 2:
            continue

        cmd = tokens[1]

        # 判斷是否為巢狀子命令（db backup / db doctor / …）
        if cmd == "db" and len(tokens) >= 3 and not tokens[2].startswith("-"):
            subcmd_key: tuple[str, ...] = ("db", tokens[2])
        else:
            subcmd_key = (cmd,)

        allowed = valid_flags.get(subcmd_key)
        if allowed is None:
            # 未知子命令：無法驗證，跳過（避免誤報）
            continue

        # 收集此行所有 --flag token
        for token in tokens:
            if not token.startswith("--"):
                continue
            # 處理 --flag=value 形式
            flag = token.split("=", 1)[0]
            if flag not in allowed:
                violations.append(
                    FlagViolation(
                        filepath=filepath,
                        line_content=line,
                        subcmd_key=subcmd_key,
                        illegal_flag=flag,
                        valid_flags=allowed,
                    )
                )

    return violations


# ── 4. 主測試：掃真實 skill 文件 ─────────────────────────────────────────────


def test_skill_doc_bes_flags_match_cli() -> None:
    """
    遞迴掃 skills/mailattic/**/*.md，確認所有 ``bes`` 範例呼叫
    使用的 ``--flag`` 在對應子命令的 argparse parser 中都合法。

    失敗時一次列出所有違規（含檔案 / 命令行 / 非法 flag / 合法 flag 列表），
    讓人一眼定位要修哪裡。
    """
    assert _SKILL_ROOT.is_dir(), (
        f"skill 目錄不存在：{_SKILL_ROOT}。"
        "請確認 repo 結構正確，或更新此測試的 _SKILL_ROOT 路徑。"
    )

    valid = collect_valid_flags()
    all_violations: list[FlagViolation] = []

    md_files = sorted(_SKILL_ROOT.rglob("*.md"))
    assert md_files, f"在 {_SKILL_ROOT} 下找不到任何 .md 檔案"

    for md_path in md_files:
        text = md_path.read_text(encoding="utf-8")
        bes_lines = extract_bes_lines(text)
        violations = check_flags_in_lines(bes_lines, valid, filepath=md_path)
        all_violations.extend(violations)

    assert not all_violations, (
        f"skill 文件包含 {len(all_violations)} 個非法 CLI flag（"
        "請同步修正文件或 CLI parser）：\n" + "".join(repr(v) for v in all_violations)
    )


def test_reply_docs_publish_safe_anchor_and_paced_restart_contract() -> None:
    """跨專案 reply 指引必須保留兩個 production safety contract。"""
    skill_text = (_SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
    reply_text = (_SKILL_ROOT / "references" / "capability-c-reply.md").read_text(
        encoding="utf-8"
    )

    assert "archive-scrape" in skill_text
    assert "inbound" in skill_text
    assert "To/Cc" in skill_text or "To / Cc" in skill_text
    assert "--allow-archive-anchor" in skill_text

    assert "archive-scrape" in reply_text
    assert "same thread" in reply_text
    assert "To/Cc" in reply_text or "To / Cc" in reply_text
    assert "--allow-archive-anchor" in reply_text
    assert "--replace-recipients" in reply_text
    assert "90 seconds" in reply_text
    assert "list-drafts" in reply_text
    assert "sending" in reply_text
    assert "never resend blindly" in reply_text


def test_reply_docs_require_rfc822_anchor_and_publish_parser_boundary() -> None:
    """Reply recipes must use the RFC822 Message-ID and state parser limits."""
    skill_text = (_SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
    reply_text = (_SKILL_ROOT / "references" / "capability-c-reply.md").read_text(
        encoding="utf-8"
    )

    draft_recipe_lines = [
        line.strip()
        for line in reply_text.splitlines()
        if line.strip().startswith("bes draft-reply ")
    ]
    assert draft_recipe_lines
    assert all("<rfc822_message_id>" in line for line in draft_recipe_lines[:-1])
    assert "<archive_rfc822_message_id>" in draft_recipe_lines[-1]
    assert all("db_id" not in line for line in draft_recipe_lines)

    for text in (skill_text, reply_text):
        assert "--full-body --json" in text
        assert "message_id" in text
        assert "malformed raw syntax" in text
        assert "ASCII addr-spec" in text
        assert "Unicode display name" in text
        assert "exit 2 before env / DB I/O" in text


# ── 5. Self-check（red case）：確認 detector 能抓到已知壞範例 ─────────────────


def test_self_check_detect_search_format_flag() -> None:
    """
    Self-check：餵一段含 ``bes search --format json`` 的假 markdown，
    確認 check_flags_in_lines 偵測到違規。

    這證明 green case（test_skill_doc_bes_flags_match_cli）不是永遠 pass 的空殼。
    若 search 子命令真的加了 ``--format``，這個 test 也會失敗（提醒更新）。
    """
    valid = collect_valid_flags()

    # 確認 ("search",) 合法 flag 集不含 --format（前提驗證）
    assert ("search",) in valid, "parser 應包含 search 子命令"
    assert "--format" not in valid[("search",)], (
        "search 子命令不應有 --format flag。"
        "若 CLI 真的加了這個 flag，請同步更新此 self-check 的期望。"
    )

    fake_md = textwrap.dedent("""\
        ## 範例

        ```bash
        bes search --keyword 乙商行 --format json --limit 20
        ```
    """)

    lines = extract_bes_lines(fake_md)
    assert lines, "extract_bes_lines 應從 fake_md 中抽出至少一個命令行"

    violations = check_flags_in_lines(
        lines,
        valid,
        filepath=Path("(fake_markdown)"),
    )
    assert violations, (
        "self-check 失敗：check_flags_in_lines 沒有偵測到 ``bes search --format json`` "
        "的違規，表示 detector 有 bug。"
    )
    illegal_flags = {v.illegal_flag for v in violations}
    assert "--format" in illegal_flags, (
        f"期望違規 flag 為 --format，實際違規：{illegal_flags}"
    )


# ── 6. Self-check（green case）：合法命令行不應產生違規 ──────────────────────


def test_self_check_no_false_positive_for_valid_flags() -> None:
    """
    Self-check（green side）：確認合法命令行不會被誤報為違規。

    覆蓋幾個邊界情境：
    - ``--json`` 在 search（合法）
    - ``--full-body --json`` 在 search（合法）
    - ``--format json`` 在 contact-history（合法）
    - ``db backup --output`` 巢狀子命令（合法）
    - ``bes sync-imap --pre-task``（合法）
    - ``bes show <id> --format text``（合法）
    - 反斜線行接續的多行 search（合法）
    """
    valid = collect_valid_flags()

    fake_md = textwrap.dedent("""\
        ```bash
        bes search --keyword 乙商行 --json --limit 50
        bes search --keyword 乙商行 --full-body --json --from-date 2025-01-01
        bes contact-history --contact alice@client.com --format json
        bes db backup --output /tmp/backup.db
        bes sync-imap --pre-task
        bes show 12345 --format text
        bes search \\
          --keyword 乙商行 \\
          --participants-domain client.com \\
          --limit 50 --json
        ```
    """)

    lines = extract_bes_lines(fake_md)
    assert lines, "extract_bes_lines 應從 fake_md 中抽出命令行"

    violations = check_flags_in_lines(
        lines,
        valid,
        filepath=Path("(fake_markdown)"),
    )
    assert not violations, (
        "self-check（green）產生誤報（false positive），"
        "以下合法命令被誤判為違規：\n" + "".join(repr(v) for v in violations)
    )
