"""
deploy-skill-to-ssot.py
========================
Git post-commit / post-merge hook：main 上的 commit 落地後，把 repo
dev-canonical skill 自動部署到 SSOT working tree（只 cp，不在 SSOT 跑任何
git 指令）。

三道閘：
  1. main gate：當前分支是否為 main？（新流程語意：main = 已完成；
     feature branch 的 WIP commit 不部署，半成品不外流。原 archive-commit
     觸發隨 Spectra 退場失效，見 D-20260820-4 / issue #172）
     否 → sys.exit(0) 靜默 no-op
  1.5 clean gate：skill 目錄的 working tree 是否乾淨？部署源是磁碟上的
     目錄（可變），不是 HEAD 快照 —— skill 目錄有未 commit 改動時，任何
     無關 commit 都會把半成品掃出去。dirty → 跳過，等 WIP 自己 commit
     後的下一次 main commit 自癒（#172 round-2 review 發現）
  2. 內容 diff：repo skill 與 SSOT 是否已一致？
     是 → sys.exit(0) 靜默 no-op
     否 → 逐檔複製部署，失敗 fail-loud + sys.exit(非0)

部署與比對的邊界 = `git ls-files` 的 tracked 清單（#172 round-3 review）：
porcelain 看不見 ignored 檔，但整目錄複製會把 __pycache__ / .env 這類
未提交產物甚至秘密搬進 SSOT —— 以 tracked 清單為界，兩者天然出局。

SSOT 路徑由環境變數 MAILATTIC_SKILL_SSOT 取；未設時 fallback 絕對預設路徑。
正式 release 加 --verify-required：來源 gate 跳過即失敗，部署後再逐檔比對。
"""

from __future__ import annotations

import argparse
import filecmp
import os
import shutil
import subprocess
import sys
from pathlib import Path

# ── 路徑解析 ─────────────────────────────────────────────────────────────────


def get_repo_root() -> Path:
    """從本 script 所在目錄往上找 repo root（含 .git 的目錄）。"""
    candidate = Path(__file__).resolve().parent
    while True:
        if (candidate / ".git").exists():
            return candidate
        parent = candidate.parent
        if parent == candidate:
            script = Path(__file__).resolve()
            raise RuntimeError(
                f"無法找到 git repo root（從 {script} 往上搜尋皆無 .git）"
            )
        candidate = parent


def get_ssot_path() -> Path:
    """SSOT 目標路徑：優先讀 MAILATTIC_SKILL_SSOT env var，否則用絕對預設。"""
    raw = os.environ.get("MAILATTIC_SKILL_SSOT", "").strip()
    if raw:
        return Path(raw)
    return Path(r"C:\IT_Projects\ai-agent-setting-backup\skills\shared\mailattic")


# ── 核心邏輯（拆成可單元測試的函式）─────────────────────────────────────────


def is_on_main(repo_root: Path | None = None) -> bool:
    """
    跑 git branch --show-current 判斷當前分支是否為 main。
    回傳 True → 在 main（進入內容 diff 閘）；False → 不是（no-op）。
    detached HEAD（輸出空字串）或 git 指令失敗 → 保守地回 False。
    """
    result = subprocess.run(
        ["git", "branch", "--show-current"],
        capture_output=True,
        text=True,
        cwd=str(repo_root) if repo_root else None,
    )
    if result.returncode != 0:
        return False
    return result.stdout.strip() == "main"


def skill_worktree_clean(repo_root: Path | None = None) -> bool:
    """
    跑 git status --porcelain 限定 skill 目錄，判斷部署源是否等於已提交內容。
    乾淨 → True（可部署）；有未 commit 改動或 git 失敗 → 保守回 False（跳過，
    半成品不外流；下一次 main commit 自癒）。
    """
    result = subprocess.run(
        ["git", "status", "--porcelain", "--", "skills/mailattic"],
        capture_output=True,
        text=True,
        cwd=str(repo_root) if repo_root else None,
    )
    if result.returncode != 0:
        return False
    return result.stdout.strip() == ""


def tracked_skill_files(repo_root: Path | None = None) -> list[str] | None:
    """
    跑 git ls-files 取得 skill 目錄的 tracked 檔案清單（repo-relative，
    forward-slash）。這份清單是部署與比對的**唯一邊界** —— ignored /
    untracked 檔（__pycache__、.env…）不在清單上，永遠不會被比對或複製。
    git 失敗 → 回 None（呼叫端保守跳過部署）。
    """
    result = subprocess.run(
        [
            "git",
            "-c",
            "core.quotepath=false",
            "ls-files",
            "--",
            "skills/mailattic",
        ],
        capture_output=True,
        text=True,
        cwd=str(repo_root) if repo_root else None,
    )
    if result.returncode != 0:
        return None
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def dirs_differ(src: Path, dst: Path, rel_files: list[str]) -> bool:
    """
    以 tracked 相對路徑清單為界，逐檔比對 src 與 dst 是否有差異。
    任一檔在 dst 缺失或內容不同 → True（需要部署）；全部一致 → False。
    dst 側多出的檔案不比對 —— 部署本來就只增補覆蓋、不刪除，且清單外的
    檔案（ignored / untracked）明確不屬於部署範圍。
    """
    if not dst.exists():
        return True
    # 部署正確性閘門：必須讀內容比對，不靠 (size, mtime) 近似（見 issue #20）。
    # 兩層防護，缺一不可：
    #   1. shallow=False：走內容比對而非 stat 簽章。
    #   2. clear_cache()：filecmp 的模組級 _cache 以 (size, mtime) stat 簽章
    #      為 key，同 tick 改寫且 size 不變時簽章碰撞會回傳 stale 結果，
    #      同一 process 內多次呼叫會踩到（deploy 前後各比一次即是）。
    filecmp.clear_cache()
    for rel in rel_files:
        sp = src / rel
        dp = dst / rel
        if not dp.exists():
            return True
        if not filecmp.cmp(str(sp), str(dp), shallow=False):
            return True
    return False


def deploy(src: Path, dst: Path, rel_files: list[str]) -> None:
    """
    把清單內的檔案逐一從 src 複製到 dst（自動建父目錄；只增補覆蓋，不刪除）。
    失敗時 print 到 stderr 並 raise，呼叫端負責 sys.exit(非0)。
    """
    try:
        for rel in rel_files:
            dp = dst / rel
            dp.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(src / rel), str(dp))
    except Exception as exc:
        print(
            f"[deploy-skill-to-ssot] ERROR：skill 部署失敗。\n"
            f"  來源（repo dev-canonical）：{src}\n"
            f"  目標（SSOT working tree）：{dst}\n"
            f"  錯誤：{exc}\n"
            f"  請手動逐檔複製 git ls-files 清單內的檔案，"
            f"不要整目錄 copytree（會把 ignored 檔一起搬進 SSOT）",
            file=sys.stderr,
        )
        raise


# ── 主流程 ───────────────────────────────────────────────────────────────────


def main(*, verify_required: bool = False) -> None:
    repo_root = get_repo_root()
    src = repo_root / "skills" / "mailattic"
    dst = get_ssot_path()

    # 閘 1：main gate
    if not is_on_main(repo_root):
        if verify_required:
            raise RuntimeError("skill SSOT sync skipped: source is not on main")
        # 非 main 分支 → 靜默 no-op
        sys.exit(0)

    # 閘 1.5：clean gate — skill 目錄有未 commit 改動就不部署（半成品不外流）
    if not skill_worktree_clean(repo_root):
        if verify_required:
            raise RuntimeError("skill SSOT sync skipped: source is dirty or unreadable")
        sys.exit(0)

    # 部署邊界：tracked 檔案清單（None = git 失敗、空 = 無 tracked 檔 → 保守跳過）
    tracked = tracked_skill_files(repo_root)
    if not tracked:
        if verify_required:
            raise RuntimeError("skill SSOT sync skipped: tracked file list unavailable")
        sys.exit(0)
    prefix = "skills/mailattic/"
    rel_files = [f.removeprefix(prefix) for f in tracked if f.startswith(prefix)]
    if not rel_files:
        if verify_required:
            raise RuntimeError("skill SSOT sync skipped: no tracked skill files")
        sys.exit(0)

    # 閘 2：內容 diff
    if not dirs_differ(src, dst, rel_files):
        # 完全一致 → 靜默 no-op
        sys.exit(0)

    # 部署
    print("[deploy-skill-to-ssot] main commit + skill 內容與 SSOT 不一致，部署中…")
    print(f"  src: {src}")
    print(f"  dst: {dst}")
    try:
        deploy(src, dst, rel_files)
    except Exception:
        sys.exit(1)

    if verify_required and dirs_differ(src, dst, rel_files):
        raise RuntimeError("skill SSOT verification failed: tracked contents differ")

    print("[deploy-skill-to-ssot] 部署完成。")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify-required", action="store_true")
    main(verify_required=parser.parse_args().verify_required)
