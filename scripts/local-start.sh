#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Azure を使わず、ローカルだけでデモ環境を立ち上げる。
# Collector の送信先を Jaeger / Prometheus に切り替えるだけで、
# アプリ側のコードも設定も一切変えない。
#
#   ./scripts/local-start.sh
# ---------------------------------------------------------------------------
set -euo pipefail
cd "$(dirname "$0")/.."

# --- 事前チェック ----------------------------------------------------------
if ! command -v docker > /dev/null; then
  echo "docker が見つかりません。docs/wsl2-setup.md を参照してください。" >&2
  exit 1
fi

if ! docker compose version > /dev/null 2>&1; then
  echo "docker compose (v2) が使えません。docker-compose ではなく Compose v2 が必要です。" >&2
  exit 1
fi

if ! docker info > /dev/null 2>&1; then
  cat >&2 <<'EOF'
Docker デーモンに接続できません。次のいずれかを確認してください。

  * Docker Desktop を使っている場合
      Settings > Resources > WSL Integration で、このディストリビューションが ON か
  * WSL2 内に Docker Engine を入れている場合
      sudo service docker start          (systemd 無効時)
      sudo systemctl start docker        (systemd 有効時)
  * 権限エラーの場合
      sudo usermod -aG docker $USER  を実行後、Ubuntu を開き直す
EOF
  exit 1
fi

# WSL2 で Windows 側のドライブに置くとビルドが数倍遅くなる
case "$(pwd)" in
  /mnt/*)
    echo "警告: Windows 側のドライブ ($(pwd)) で実行しています。" >&2
    echo "      ビルドが極端に遅くなります。~/ 配下に移動することを強く推奨します。" >&2
    echo "      このまま続ける場合は Enter、中止する場合は Ctrl-C を押してください。" >&2
    read -r _
    ;;
esac
# ---------------------------------------------------------------------------

if [ ! -f .env ]; then
  cp .env.example .env
  echo "==> .env を作成しました"
fi

# COLLECTOR_CONFIG をローカル用に切り替える (行が無ければ追記)
if grep -q '^COLLECTOR_CONFIG=' .env; then
  sed -i.bak 's|^COLLECTOR_CONFIG=.*|COLLECTOR_CONFIG=config.local.yaml|' .env && rm -f .env.bak
else
  echo 'COLLECTOR_CONFIG=config.local.yaml' >> .env
fi
echo "==> COLLECTOR_CONFIG=config.local.yaml (Azure には送りません)"

echo "==> イメージをビルドします (初回は 3〜5 分かかります)"
docker compose build

echo "==> 起動します"
docker compose up -d

echo "==> 起動待ち"
./scripts/verify.sh
