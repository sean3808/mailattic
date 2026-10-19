"""
tests/test_deploy_skill_hook.py
================================
Unit tests for scripts/deploy-skill-to-ssot.py

四個 scenario（觸發語意 2026-08-20 改 main gate，見 D-20260820-4 / issue #172）：
  (i)  非 main 分支 → exit 0，SSOT 無變動（不論內容差異）
  (ii) main 但 repo skill == SSOT → exit 0 no-op
  (iii) main 且 skill 有差異 → cp 後 diff 為空
  (iv) cp 失敗（SSOT 父目錄不存在/唯讀）→ stderr 錯誤 + exit 非 0

重要：測試**絕不**寫真實 SSOT 路徑（the SSOT skills repo）。
全部透過 tmp_path + monkeypatch MAILATTIC_SKILL_SSOT env var 隔離。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

# 用 importlib 載入含連字號的模組檔名 deploy-skill-to-ssot.py
_SCRIPT_PATH = (
    Path(__file__).resolve().parent.parent / "scripts" / "deploy-skill-to-ssot.py"
)
_spec = importlib.util.spec_from_file_location("deploy_skill_to_ssot", _SCRIPT_PATH)
_mod = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(_mod)  # type: ignore[union-attr]
sys.modules["deploy_skill_to_ssot"] = _mod

from deploy_skill_to_ssot import (  # noqa: E402
    deploy,
    dirs_differ,
    is_on_main,
    skill_worktree_clean,
    tracked_skill_files,
)

# ── helpers ──────────────────────────────────────────────────────────────────


def _make_skill_tree(base: Path) -> None:
    """在 base 下建立最小 skill 目錄結構（模擬 skills/mailattic/）。"""
    base.mkdir(parents=True, exist_ok=True)
    (base / "SKILL.md").write_text("# mailattic skill\n", encoding="utf-8")
    refs = base / "references"
    refs.mkdir(parents=True, exist_ok=True)
    (refs / "capability-a-sync.md").write_text("# sync\n", encoding="utf-8")
    (refs / "capability-b-find.md").write_text("# find\n", encoding="utf-8")
    scripts = base / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    (scripts / "ball-in-court.py").write_text("# bic\n", encoding="utf-8")


# _make_skill_tree 建出的檔案，以 skill 目錄為根的相對路徑（= tracked 清單）
_SKILL_FILES = [
    "SKILL.md",
    "references/capability-a-sync.md",
    "references/capability-b-find.md",
    "scripts/ball-in-court.py",
]


def _fake_git(branch: str = "main", porcelain: str = ""):
    """依命令回應的假 git：branch / status / ls-files 各回對應輸出。"""
    ls_files = "".join(f"skills/mailattic/{f}\n" for f in _SKILL_FILES)

    def run(cmd, **kwargs):
        if "branch" in cmd:
            out = branch + "\n"
        elif "ls-files" in cmd:
            out = ls_files
        else:
            out = porcelain
        return SimpleNamespace(returncode=0, stdout=out)

    return run


# ── is_on_main ────────────────────────────────────────────────────────────────


def test_is_on_main_true_on_main_branch():
    """branch --show-current 回 main → True。"""
    with patch("deploy_skill_to_ssot.subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = "main\n"
        assert is_on_main() is True


def test_is_on_main_false_on_feature_branch():
    """branch --show-current 回其他分支 → False（WIP 不部署）。"""
    with patch("deploy_skill_to_ssot.subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = "feat/direction-field\n"
        assert is_on_main() is False


def test_is_on_main_false_on_detached_head():
    """detached HEAD（輸出空字串）→ 保守視為 False。"""
    with patch("deploy_skill_to_ssot.subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = "\n"
        assert is_on_main() is False


def test_is_on_main_false_when_git_fails():
    """git 指令失敗（returncode 非 0）→ 保守視為 False。"""
    with patch("deploy_skill_to_ssot.subprocess.run") as mock_run:
        mock_run.return_value.returncode = 128
        mock_run.return_value.stdout = ""
        assert is_on_main() is False


# ── skill_worktree_clean ──────────────────────────────────────────────────────


def test_skill_worktree_clean_true_when_porcelain_empty():
    """status --porcelain 無輸出 → True（可部署）。"""
    with patch("deploy_skill_to_ssot.subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = ""
        assert skill_worktree_clean() is True


def test_skill_worktree_clean_false_when_dirty():
    """skill 目錄有未 commit 改動 → False（半成品不外流）。"""
    with patch("deploy_skill_to_ssot.subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = " M skills/mailattic/SKILL.md\n"
        assert skill_worktree_clean() is False


def test_skill_worktree_clean_false_when_git_fails():
    """git 指令失敗 → 保守視為 False（跳過部署）。"""
    with patch("deploy_skill_to_ssot.subprocess.run") as mock_run:
        mock_run.return_value.returncode = 128
        mock_run.return_value.stdout = ""
        assert skill_worktree_clean() is False


# ── tracked_skill_files ───────────────────────────────────────────────────────


def test_tracked_skill_files_returns_list():
    """ls-files 正常 → 回傳非空清單（含 repo-relative 路徑）。"""
    with patch("deploy_skill_to_ssot.subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = (
            "skills/mailattic/SKILL.md\n"
            "skills/mailattic/references/capability-a-sync.md\n"
        )
        assert tracked_skill_files() == [
            "skills/mailattic/SKILL.md",
            "skills/mailattic/references/capability-a-sync.md",
        ]


def test_tracked_skill_files_none_when_git_fails():
    """git 失敗 → 回 None（呼叫端保守跳過部署）。"""
    with patch("deploy_skill_to_ssot.subprocess.run") as mock_run:
        mock_run.return_value.returncode = 128
        mock_run.return_value.stdout = ""
        assert tracked_skill_files() is None


# ── dirs_differ ───────────────────────────────────────────────────────────────


def test_dirs_differ_true_when_dst_missing(tmp_path):
    """dst 不存在 → 視為有差異，回傳 True。"""
    src = tmp_path / "src"
    _make_skill_tree(src)
    dst = tmp_path / "nonexistent"
    assert dirs_differ(src, dst, _SKILL_FILES) is True


def test_dirs_differ_false_when_identical(tmp_path):
    """src 與 dst 完全一致 → 回傳 False。"""
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _make_skill_tree(src)
    _make_skill_tree(dst)
    assert dirs_differ(src, dst, _SKILL_FILES) is False


def test_dirs_differ_true_when_file_changed(tmp_path):
    """src 某清單內檔案內容與 dst 不同 → 回傳 True。"""
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _make_skill_tree(src)
    _make_skill_tree(dst)
    # 修改 src 的 SKILL.md
    (src / "SKILL.md").write_text("# updated skill\n", encoding="utf-8")
    assert dirs_differ(src, dst, _SKILL_FILES) is True


def test_dirs_differ_true_when_listed_file_missing_in_dst(tmp_path):
    """清單內檔案在 dst 缺失 → 回傳 True（需首次補齊）。"""
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _make_skill_tree(src)
    _make_skill_tree(dst)
    (dst / "references" / "capability-b-find.md").unlink()
    assert dirs_differ(src, dst, _SKILL_FILES) is True


def test_dirs_differ_ignores_files_outside_list(tmp_path):
    """
    清單外的檔案（ignored / untracked junk）不參與比對 —— src 多了 .env 與
    __pycache__ 仍回 False（#172 round-3 review：邊界是 tracked 清單，不是磁碟目錄）。
    """
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _make_skill_tree(src)
    _make_skill_tree(dst)
    (src / ".env").write_text("SECRET=1\n", encoding="utf-8")
    cache = src / "scripts" / "__pycache__"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "ball-in-court.cpython-313.pyc").write_bytes(b"\x00junk")
    assert dirs_differ(src, dst, _SKILL_FILES) is False


# ── deploy ────────────────────────────────────────────────────────────────────


def test_deploy_copies_files(tmp_path):
    """deploy 把清單內檔案 cp 到 dst，事後 dirs_differ 為 False。"""
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _make_skill_tree(src)
    deploy(src, dst, _SKILL_FILES)
    assert not dirs_differ(src, dst, _SKILL_FILES)


def test_deploy_overwrites_changed_file(tmp_path):
    """dst 已有舊版，deploy 後 dst 內容與 src 一致。"""
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _make_skill_tree(src)
    _make_skill_tree(dst)
    # 修改 src
    (src / "SKILL.md").write_text("# new content\n", encoding="utf-8")
    deploy(src, dst, _SKILL_FILES)
    assert (dst / "SKILL.md").read_text(encoding="utf-8") == "# new content\n"
    assert not dirs_differ(src, dst, _SKILL_FILES)


def test_deploy_does_not_copy_files_outside_list(tmp_path):
    """
    清單外的檔案不被搬運 —— src 有 .env 與 __pycache__，deploy 後 dst 不得
    出現它們（#172 round-3 review 的紅綠：改回整目錄 copytree 這條會紅）。
    """
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _make_skill_tree(src)
    (src / ".env").write_text("SECRET=1\n", encoding="utf-8")
    cache = src / "scripts" / "__pycache__"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "ball-in-court.cpython-313.pyc").write_bytes(b"\x00junk")
    deploy(src, dst, _SKILL_FILES)
    assert not (dst / ".env").exists()
    assert not (dst / "scripts" / "__pycache__").exists()
    assert (dst / "SKILL.md").exists()


def test_deploy_fails_loudly_on_permission_error(tmp_path, capsys):
    """
    scenario (iv)：copy2 拋出 PermissionError → deploy raise + stderr 含錯誤訊息。
    用 mock 模擬 PermissionError。
    """
    src = tmp_path / "src"
    _make_skill_tree(src)
    dst = tmp_path / "dst"

    with (
        patch(
            "deploy_skill_to_ssot.shutil.copy2",
            side_effect=PermissionError("access denied"),
        ),
        pytest.raises(PermissionError),
    ):
        deploy(src, dst, _SKILL_FILES)

    captured = capsys.readouterr()
    assert "ERROR" in captured.err
    assert str(src) in captured.err
    assert str(dst) in captured.err
    assert "手動逐檔複製" in captured.err


# ── integration-style：scenario (i) ~ (iv) via main() ────────────────────────


def test_scenario_i_non_main_branch_exits_zero(tmp_path, monkeypatch):
    """
    scenario (i)：非 main 分支 → exit 0，不動 SSOT。
    模擬 git branch --show-current 回傳 feature branch，並把 SSOT 指向 tmp_path。
    """
    fake_ssot = tmp_path / "ssot"
    fake_ssot.mkdir()
    # 放一個 sentinel 檔確認 SSOT 未被碰
    sentinel = fake_ssot / "sentinel.txt"
    sentinel.write_text("original", encoding="utf-8")
    mtime_before = sentinel.stat().st_mtime

    monkeypatch.setenv("MAILATTIC_SKILL_SSOT", str(fake_ssot))

    with (
        patch(
            "deploy_skill_to_ssot.subprocess.run",
            side_effect=_fake_git(branch="feat/some-branch"),
        ),
        pytest.raises(SystemExit) as exc_info,
    ):
        _run_main_with_fake_root(tmp_path, monkeypatch)

    assert exc_info.value.code == 0
    # SSOT sentinel 未被動
    assert sentinel.stat().st_mtime == mtime_before


def test_scenario_i_b_main_but_skill_dir_dirty_exits_zero(tmp_path, monkeypatch):
    """
    scenario (i-b)：main 但 skill 目錄 working tree 有未 commit 改動 →
    exit 0、SSOT 不動（半成品不外流；#172 round-2 review）。
    拿掉 clean gate 這條會紅：main() 會走到 deploy 並覆寫 SSOT 內容。
    """
    fake_ssot = tmp_path / "ssot"
    _make_skill_tree(fake_ssot)
    fake_src = tmp_path / "skills" / "mailattic"
    _make_skill_tree(fake_src)
    (fake_src / "SKILL.md").write_text("# WIP content\n", encoding="utf-8")

    monkeypatch.setenv("MAILATTIC_SKILL_SSOT", str(fake_ssot))

    with (
        patch(
            "deploy_skill_to_ssot.subprocess.run",
            side_effect=_fake_git(porcelain=" M skills/mailattic/SKILL.md\n"),
        ),
        pytest.raises(SystemExit) as exc_info,
    ):
        _run_main_with_fake_root(tmp_path, monkeypatch)

    assert exc_info.value.code == 0
    # SSOT 未被半成品覆寫
    assert (fake_ssot / "SKILL.md").read_text(encoding="utf-8") == "# mailattic skill\n"


def test_scenario_ii_main_but_skill_unchanged_exits_zero(tmp_path, monkeypatch):
    """
    scenario (ii)：main 但 repo skill == SSOT → exit 0 no-op。
    """
    fake_ssot = tmp_path / "ssot"
    _make_skill_tree(fake_ssot)
    fake_src = tmp_path / "skills" / "mailattic"
    _make_skill_tree(fake_src)

    monkeypatch.setenv("MAILATTIC_SKILL_SSOT", str(fake_ssot))

    mtime_before = (fake_ssot / "SKILL.md").stat().st_mtime

    with (
        patch("deploy_skill_to_ssot.subprocess.run", side_effect=_fake_git()),
        pytest.raises(SystemExit) as exc_info,
    ):
        _run_main_with_fake_root(tmp_path, monkeypatch)

    assert exc_info.value.code == 0
    # SSOT SKILL.md mtime 不變
    assert (fake_ssot / "SKILL.md").stat().st_mtime == mtime_before


def test_scenario_iii_main_skill_changed_deploys(tmp_path, monkeypatch):
    """
    scenario (iii)：main 且 skill 有差異 → cp 後 diff 為空。
    """
    fake_ssot = tmp_path / "ssot"
    _make_skill_tree(fake_ssot)
    (fake_ssot / "SKILL.md").write_text("# old content\n", encoding="utf-8")

    fake_src = tmp_path / "skills" / "mailattic"
    _make_skill_tree(fake_src)
    (fake_src / "SKILL.md").write_text("# new content\n", encoding="utf-8")

    monkeypatch.setenv("MAILATTIC_SKILL_SSOT", str(fake_ssot))

    # 清單外 junk：不得被搬進 SSOT（#172 round-3）
    (fake_src / ".env").write_text("SECRET=1\n", encoding="utf-8")

    with patch("deploy_skill_to_ssot.subprocess.run", side_effect=_fake_git()):
        # 部署成功不呼叫 sys.exit，正常 return
        _run_main_with_fake_root(tmp_path, monkeypatch)

    # 部署後 diff 應為空（SSOT 與 repo skill 一致）
    assert not dirs_differ(fake_src, fake_ssot, _SKILL_FILES)
    assert (fake_ssot / "SKILL.md").read_text(encoding="utf-8") == "# new content\n"
    assert not (fake_ssot / ".env").exists()


def test_scenario_iv_cp_fails_exits_nonzero(tmp_path, monkeypatch, capsys):
    """
    scenario (iv)：檔案複製失敗 → stderr 錯誤 + sys.exit(1)。
    用 mock 模擬 PermissionError。
    """
    fake_ssot = tmp_path / "ssot"
    fake_ssot.mkdir()

    fake_src = tmp_path / "skills" / "mailattic"
    _make_skill_tree(fake_src)

    monkeypatch.setenv("MAILATTIC_SKILL_SSOT", str(fake_ssot))

    with (
        patch("deploy_skill_to_ssot.subprocess.run", side_effect=_fake_git()),
        patch(
            "deploy_skill_to_ssot.shutil.copy2",
            side_effect=PermissionError("access denied"),
        ),
        pytest.raises(SystemExit) as exc_info,
    ):
        _run_main_with_fake_root(tmp_path, monkeypatch)

    assert exc_info.value.code != 0
    captured = capsys.readouterr()
    assert "ERROR" in captured.err
    assert "手動逐檔複製" in captured.err


# ── helper：以 fake repo root 跑 production main() ────────────────────────────


def _run_main_with_fake_root(fake_repo_root: Path, monkeypatch) -> None:
    """
    呼叫 production 的 main()，只把 get_repo_root() 換成回傳 tmp_path
    （它會往上找 .git，在 tmp_path 找不到）；SSOT 路徑由各測試以
    MAILATTIC_SKILL_SSOT env var 注入。gate 順序與部署流程走的是
    production 程式碼本體 —— 拿掉 main() 裡的分支 gate，scenario (i)
    會變紅（issue #172 驗收；勿改回複製 gate 邏輯的舊寫法，那是
    PC-079 型同義反覆）。
    """
    import deploy_skill_to_ssot as m

    monkeypatch.setattr(m, "get_repo_root", lambda: fake_repo_root)
    m.main()
