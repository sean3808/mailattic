# WSL Environment — mailattic Mail Platform

> 跨 session 冷啟動入口：每次需要在 WSL 跑 mbsync / 整合 smoke / docker compose 時直接讀這份，**不要重查**。內容若跟現況不符（distro 升版、docker engine 重裝、repo mirror 位置搬遷等），就地更新此檔，不要另開 session-specific 副本。

## 基本盤

| 項目 | 值 |
|---|---|
| Distro | `Ubuntu-24.04` |
| 觸發指令 | `wsl -d Ubuntu-24.04 -- bash <script>` |
| Docker | `/usr/bin/docker` v29.1.3（Docker Engine on WSL2，無 `docker.exe`，見 PC-019） |
| Python `uv` 二進位 | `/root/.local/bin/uv`（**預設 PATH 不含**，跑前 `export PATH="/root/.local/bin:$PATH"`） |

## Repo 鏡像

| 端 | 路徑 | git 狀態 | 用途 |
|---|---|---|---|
| Windows | `C:\IT_Projects\mailattic` | ✓ git repo | **正典** — 編輯 / commit / push |
| WSL | `/root/projects/mailattic` | ✗ **無 `.git`** | 跑 docker compose / mbsync 用的 mirror |

**Sync 規則**（單向 Windows → WSL，安全方向）：
- WSL mirror **不是** git；每次跑 smoke / integration 前必須手動 sync 改動過去
- WSL → Windows 大量小檔禁用（PC-026，見 D-20260525-1）
- 反向少量檔（< 100 個）安全，但**必須**用 `MSYS_NO_PATHCONV=1` 包裝 wsl 命令 + 寫成 .sh 檔執行（PC-038 + PC-041：Git Bash inline `bash -c '...'` 內含 `/mnt/c/...` 或 `/root/...` 會被 MSYS 翻成 Windows path，$variable 被吞為空字串）

> **🔴 硬性規則 — WSL 端任何 `uv` 操作只在 `/root/projects`，禁在 `/mnt/c` 上執行 `uv`**（PC-017 reparse-point 污染根因）。在 WSL 內對 `/mnt/c/IT_Projects/mailattic/.venv`（即 Windows 開發 venv）跑 `uv sync` / `uv run` 會種進 Linux-only 的 `lib64` reparse point，被 Windows file lock 卡住，使隔日 Windows 端 `uv` 操作（含每日同步若誤用 `uv run`）silent 失敗。任何 WSL 上的 `uv sync` / `uv run` / venv rebuild **一律**在 ext4 mirror `/root/projects/mailattic` 上做，絕不在 `/mnt/c` mount 上做。每日同步排程本身已改呼叫 Windows-native 全域 `bes`（不再 `uv run`），不碰任何 venv；此禁令守的是手動 WSL 操作不要再生污染向量。背景見 `harden-sync-scheduling` change 與 PC-017。

### Sync 標準命令

```bash
# 1. 把 .sh 腳本寫到 work-tmp/（gitignored）
# 2. 從 Git Bash / Claude Bash tool 跑：
MSYS_NO_PATHCONV=1 wsl -d Ubuntu-24.04 -- bash /mnt/c/IT_Projects/mailattic/work-tmp/sync-XXX.sh
```

範本見 `work-tmp/sync-change-a.sh`（個別檔同步）、`work-tmp/sync-full-src.sh`（package 全量 mirror）。

## Docker Compose Mount 結構

`docker/docker-compose.yml` 關鍵：

```yaml
volumes:
  - ..:/workspace                # repo 根 mount 進 container 為 /workspace
  - ../.mailattic-work:/workspace/.mailattic-work
working_dir: /workspace
env_file:
  - ../.env.sean3808             # IMAP credentials
```

**Container 內路徑 vs Host 路徑**：

| Container 內 | Host (WSL) |
|---|---|
| `/workspace/` | `/root/projects/mailattic/` |
| `/workspace/work/smoke-stage.db` | `/root/projects/mailattic/work/smoke-stage.db` |
| `/workspace/docker/run_sync.py` | `/root/projects/mailattic/docker/run_sync.py` ← **mount 的最新版** |
| `/opt/mailattic/run_sync.py` | （image build-time COPY，**舊版** — 改 code 後不會自動更新，要 `docker compose build mbsync` 才會 refresh） |

**陷阱**：production task scheduler XML (`scripts/mbsync-daily.xml`) 現已改為以絕對路徑直接呼叫 Windows-native 解耦全域 `bes sync-imap`（不走 `uv run`、不綁 dev venv、不透過 WSL/Docker；見 `harden-sync-scheduling` change）。Docker/mbsync 路徑（`/opt/mailattic/run_sync.py`）僅供 PST backfill / 歷史匯入；日常每日 sync 不走 WSL。

## 跑 mbsync 一手指南

> **注意（2026-05 起）**：每日 IMAP sync 已切換為 Windows-native，並進一步改為以絕對路徑呼叫解耦全域 `bes sync-imap`（直接 imaplib，不走 `uv run`、不綁 dev venv、不需 WSL/Docker；見 `harden-sync-scheduling`）。以下 A/B 兩節描述的 Docker/WSL 路徑**僅保留供 PST backfill 及偶爾整合 smoke 使用**，正常日常 sync 請直接在 Windows 跑 `bes sync-imap`。

### A. Dry-run（估規模、不寫 state）

```bash
# 列 IMAP folders + count messages per folder（找最小 folder 做 smoke）
MSYS_NO_PATHCONV=1 wsl -d Ubuntu-24.04 -- bash /mnt/c/IT_Projects/mailattic/work-tmp/smoke-dry-run.sh
```

smoke 前先用 `smoke-dry-run.sh` 列出帳號的 IMAP folders 與各 folder 筆數，挑最小、含附件機率高的 folder 試 smoke。IMAP folder 名稱可能是 UTF-7 編碼（中文資料夾名）。

### B. 整合 smoke（fresh DB + mbsync 真實流量 + 驗 attachment wiring）

```bash
MSYS_NO_PATHCONV=1 wsl -d Ubuntu-24.04 -- bash /mnt/c/IT_Projects/mailattic/work-tmp/smoke-run.sh
```

腳本流程：
1. 砍上次 smoke 殘留 (`work/smoke-stage.db`、`work/smoke-maildir/`、`work/smoke-attachments/`)
2. **手動 mkdir `<MAILDIR>/<ACCOUNT>/`**（必要！見下方陷阱）
3. 跑 `storage.initialize_database` init fresh schema
4. `docker compose run --rm mbsync python /workspace/docker/run_sync.py --folder sent-mail --attachment-root ...`
5. SQL 驗 `COUNT(*) WHERE content_hash IS NULL = 0`
6. SHA-256 spot-check 3 個 random CAS 檔

### 陷阱：`docker compose run` 跳過 entrypoint

`entrypoint.sh` 負責 `mkdir -p ${MAILDIR_ROOT}/${IMAP_ACCOUNT_NAME}` + 建 mbsync crontab。但 `docker compose run --rm mbsync python ...` 用 python 命令 override CMD，**完全跳過 entrypoint**。後果：mbsync 抱怨 `cannot open store '/workspace/.../sean3808/'`。

修法：smoke / one-shot 模式必須在 host 端先 `mkdir -p $MAILDIR_ROOT/$ACCOUNT_NAME` 補上。`work-tmp/smoke-run.sh` 已包含這步。

### 陷阱：python deps drift

WSL `.venv` 是 May 25 設的；之後 `pyproject.toml` 加新 dependency（例：`requests` for `archive_scrape.py`）後 WSL .venv 不會自動同步。症狀：`ModuleNotFoundError: No module named 'requests'`。

修法（按嚴重度遞增）：
- (a) Smoke / ad-hoc：避開受影響的 entry（例：用 `python -c "from storage import initialize_database"` 取代 `uv run bes init-db`）— 不碰 `.venv`
- (b) 開發測試：WSL 跑 `cd /root/projects/mailattic && uv sync` 把 deps 對齊
- (c) 完全 rebuild：刪 `.venv` 後 `uv sync`（注意 PC-017 `.venv/lib64` reparse-point lock，必要時跑 `scripts/rebuild-venv.ps1`）

## .env 與 credentials

| File | Path | 用途 |
|---|---|---|
| IMAP credentials | `.env.sean3808`（repo 根） | docker-compose `env_file` 載入 + `imap_runtime.load_env_file` |
| 範本 | `.env.sean3808.example` | 入 git，新環境參考 |
| Required keys | `IMAP_HOST`, `IMAP_PORT`, `IMAP_USER`, `IMAP_PASSWORD`, `IMAP_ACCOUNT_NAME` | 缺任一 `validate_sync_environment` 會 raise ValueError |
| Native IMAP security | `IMAP_SECURITY=tls`（省略時的預設）或明示 `plain` | `plain` 不加密帳密與郵件內容，只能用於受信任網路或受保護 tunnel；設定只跟隨所選 `--env-file`，不會成為 process-global 狀態 |

當前 `IMAP_ACCOUNT_NAME=sean3808`（決定 maildir 子目錄結構 + run_sync.py 內 account name）。

## work-tmp/ 腳本清單（gitignored，用完可刪）

| 腳本 | 用途 | 何時用 |
|---|---|---|
| `sync-change-a.sh` | sync 一組指定 source / test 檔 W→WSL | 開發 change，需推給 WSL smoke 時 |
| `sync-full-src.sh` | mirror 整個 `src/mailattic/*.py` + `scripts/` | WSL 落後超過幾天、抓不到某 module |
| `smoke-dry-run.sh` | 列 IMAP folders + per-folder counts | 找最小 folder 試 smoke |
| `smoke-run.sh` | Change A 完整 integration smoke（可改 `--folder` 參數重用） | IMAP-related change 收尾驗證 |

未來若高頻使用，promote 到 `scripts/` + arg-parameterize。

## Cross-reference

- **PC-019**：Docker Engine on WSL2 沒 `docker.exe`，Windows host 不能直跑 `docker compose`
- **PC-026**：WSL → /mnt/c 大量小檔禁用，必走 robocopy 從 Windows 端拉
- **PC-038**：Git Bash → wsl args 必加 `MSYS_NO_PATHCONV=1`
- **PC-041**：腳本內含 `export PATH=/root/.local/bin:$PATH` 必須寫進 .sh 檔，不可 inline 在 `bash -c '...'`（Git Bash outer shell 會展開 `(x86)` paren 字元炸 syntax）
- **PC-017** / `scripts/rebuild-venv.ps1`：`.venv/lib64` reparse-point file lock 卡 uv sync 的修法
- **Spectra 時期 `add-mbsync-imap-sync` change 的 design Decision 5（不隨本 repo 帶出）**：為何 mbsync 走 one-shot 而非 container 內 cron（WSL2 distro idle shutdown）

## 已知陷阱（2026-10-23 自 lesson-lookup 搬入）

<!-- from lesson-lookup/wsl -->
- **WSL `rsync` / 大量小檔寫入 `/mnt/<drive>` 會把 Windows 儲存控制器壓出 timeout/reset，整顆硬碟短暫消失**：I/O path（Linux → drvfs → NTFS → Intel RST/VMD → SATA）對 metadata-heavy 寫入開銷大，觸發 `iaStorVD` Event 129 reset + `disk` 51 + `Ntfs` 50/141（delayed write failure）；BIOS 一度看不到 SSD（需完整斷電恢復），chkdsk 卻顯示 NTFS 正常 = 控制器層 timeout 非 FS 損毀。修法：(1) WSL→Windows 大量搬遷一律 Windows-native `robocopy SRC DST /E /Z /R:2 /W:2 /MT:8`（不要激進值）；(2) WSL 來源先 `tar -czf` 打包成單一檔；(3) 開發目錄要嘛全 WSL ext4 要嘛全 Windows，不混合 heavy-write 跨邊界。判準：寫入端在 WSL + 目的地 `/mnt/<drive>` + >1 萬檔或 metadata-heavy → 禁 rsync。退場：同碟再現三組 Event signature → SSD 直接退場。 `[host-maintenance 2026-05; jean-wan-mail-platform 2026-05]`

<!-- from lesson-lookup/wsl -->
- **production 跑 Docker container 用的 binary ≠ native WSL 也有**：container image 內 `apt-get install` 的工具（mbsync/isync 等）native distro 預設沒裝，直接跑報 exit 127。native 直跑的 script 要 pre-flight `command -v <bin>`；新 distro bootstrap doc 列清單。 `[jean-wan-mail-platform 2026-05]`

<!-- from lesson-lookup/wsl -->
- **Git Bash 呼叫 wsl.exe 傳 Linux 路徑要 `MSYS_NO_PATHCONV=1`**：MSYS 把 `/mnt/d/...` 自動轉成 Windows 路徑（前綴 Git 安裝目錄）→ wsl 內 bash 報 No such file。`MSYS_NO_PATHCONV=1 wsl -d <distro> -- bash /mnt/d/.../script.sh`。 `[jean-wan-mail-platform 2026-05]`

<!-- from lesson-lookup/wsl -->
- **`.venv/lib64` 是 wsl 建的 NTFS reparse point，會被 Windows file lock 卡住刪不掉**：症狀：venv 半毀（剩 lib64 + pyvenv.cfg）、`uv sync` 噴 `存取被拒 (os error 5)`。修法：用 .NET API 拆 reparse point（`[System.IO.Directory]::Delete($path, $false)`，**不可** `Remove-Item -Recurse`——會嘗試遞迴進 target）再刪 .venv 重建（jean-wan 有現成 `scripts/rebuild-venv.ps1`，Claude Code Bash 可直接跑）。預防：同一 venv 別混用 wsl uv 與 Windows uv。 `[jean-wan-mail-platform 2026-05]`

<!-- from lesson-lookup/docker -->
- **Docker Engine on WSL2 不會把 `docker.exe` 裝到 Windows PATH**：Windows-side 程式 `subprocess.run(["docker", ...])` 直接 `FileNotFoundError`（Docker Desktop 才裝 Windows CLI + socket share）。修法依侵入性：(1) user-facing wrapper `wsl -e bash -c "docker compose ..."`；(2) source 偵測 `sys.platform=="win32" and not shutil.which("docker")` 時加 wsl prefix；(3) 裝 Windows-side docker CLI 指向 WSL daemon。寫「Windows 程式喊 docker」前先問「目標機一定有 Docker Desktop 嗎？」。 `[jean-wan-mail-platform 2026-05]`
