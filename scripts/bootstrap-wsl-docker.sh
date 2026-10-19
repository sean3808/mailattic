#!/usr/bin/env bash
# 在 WSL2 Ubuntu 24.04 (root user) 內安裝 Docker Engine + 開啟 systemd
# 安全前提：以 root 執行；不覆寫 /etc/wsl.conf 既有設定（保留 [network] 區塊）
set -euo pipefail

echo "==> [1/4] apt update + 裝 docker.io + compose plugin"
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y docker.io docker-compose-v2

echo ""
echo "==> [2/4] 啟用 systemd（追加 [boot] 區塊，不動既有 [network]）"
if ! grep -q "^\[boot\]" /etc/wsl.conf; then
  printf "\n[boot]\nsystemd=true\n" >> /etc/wsl.conf
  echo "    已追加 [boot] systemd=true"
else
  echo "    [boot] 已存在，跳過"
fi

echo ""
echo "==> [3/4] 確認 /etc/wsl.conf 內容"
cat /etc/wsl.conf

echo ""
echo "==> [4/4] 列已裝版本"
docker --version
docker compose version

echo ""
echo "==> 完成。下一步在 Windows 側執行 'wsl --shutdown' 後重開 WSL，systemd 才會啟動。"
