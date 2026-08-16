#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Docker 環境の事前チェック。local-start.sh を実行する前に、
# ホスト側の前提が揃っているかをまとめて確認する。
#
#   ./scripts/check-docker.sh
#
# 確認する内容:
#   1. docker / compose / buildx が使えるか
#   2. sudo なしで docker を叩けるか
#   3. イメージを pull できるか (レジストリへの到達性)
#   4. コンテナ内から名前解決と外部通信ができるか
#   5. バインドマウントが効くか (compose が設定ファイルを渡すのに必要)
#   6. ポート公開が効くか (Windows のブラウザから見えるかの前提)
#   7. WSL2 固有の注意点 (systemd / リポジトリの置き場所 / メモリ)
#
# NG が出た項目には、そのまま打てる対処コマンドを表示する。
# ---------------------------------------------------------------------------
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

PASS=0
FAIL=0
WARN=0

ok()   { printf '  \033[32mOK\033[0m   %s\n' "$1"; PASS=$((PASS+1)); }
ng()   { printf '  \033[31mNG\033[0m   %s\n' "$1"; FAIL=$((FAIL+1)); }
warn() { printf '  \033[33m--\033[0m   %s\n' "$1"; WARN=$((WARN+1)); }
info() { printf '\n\033[1m%s\033[0m\n' "$1"; }
hint() { printf '       \033[2m%s\033[0m\n' "$1"; }

IS_WSL=0
grep -qi microsoft /proc/version 2>/dev/null && IS_WSL=1

# ---------------------------------------------------------------------------
info "1. コマンドが入っているか"

if command -v docker > /dev/null 2>&1; then
  ok "docker ($(docker --version 2>/dev/null | head -1))"
else
  ng "docker が見つかりません"
  hint "docs/wsl2-setup.md の「2. Docker を入れる」を実施してください"
  echo; printf '\033[31m先に docker のインストールが必要です。\033[0m\n'; exit 1
fi

if docker compose version > /dev/null 2>&1; then
  # メジャー番号を数値で比較する。将来 v6, v7 が出ても通るようにするため、
  # 「既知のバージョンを列挙する」書き方は避ける。
  CV=$(docker compose version --short 2>/dev/null | tr -d '[:space:]')
  CV=${CV#v}
  CV_MAJOR=${CV%%.*}
  case "$CV_MAJOR" in
    ''|*[!0-9]*)
      warn "docker compose は使えますが、バージョンを判定できませんでした (${CV:-空})"
      hint "docker compose version --short の出力を確認してください" ;;
    *)
      if [ "$CV_MAJOR" -ge 2 ]; then
        ok "docker compose v$CV"
      else
        ng "docker compose が v1 です ($CV)。v1 はサポートが終了しています"
        hint "sudo apt install -y docker-compose-plugin"
      fi ;;
  esac
else
  ng "docker compose が使えません (Compose プラグインが未導入)"
  hint "sudo apt install -y docker-compose-plugin"
fi

if docker buildx version > /dev/null 2>&1; then
  ok "docker buildx"
else
  warn "docker buildx がありません (ビルドは動きますが遅くなる場合があります)"
  hint "sudo apt install -y docker-buildx-plugin"
fi

# ---------------------------------------------------------------------------
info "2. デーモンに繋がるか"

if docker info > /dev/null 2>&1; then
  ok "docker デーモンに接続できる（sudo なし）"
elif sudo -n docker info > /dev/null 2>&1; then
  ng "sudo なしでは docker を叩けません"
  hint "sudo usermod -aG docker \$USER  の後、PowerShell で wsl --shutdown"
elif sudo docker info > /dev/null 2>&1; then
  ng "sudo なしでは docker を叩けません"
  hint "sudo usermod -aG docker \$USER  の後、PowerShell で wsl --shutdown"
else
  ng "docker デーモンが起動していません"
  if [ "$IS_WSL" = "1" ]; then
    hint "systemd 有効なら: sudo systemctl enable --now docker"
    hint "systemd 無効なら: sudo service docker start"
  else
    hint "sudo systemctl enable --now docker"
  fi
  echo; printf '\033[31mデーモンが動いていないため、以降の確認をスキップします。\033[0m\n'; exit 1
fi

if id -nG 2>/dev/null | tr ' ' '\n' | grep -qx docker; then
  ok "$USER が docker グループに所属している"
else
  warn "$USER が docker グループにいません（今は動いていても再ログイン後に失敗します）"
  hint "sudo usermod -aG docker \$USER  の後、PowerShell で wsl --shutdown"
fi

# ---------------------------------------------------------------------------
info "3. イメージの取得と実行"

if docker run --rm hello-world > /dev/null 2>&1; then
  ok "hello-world の pull と実行"
else
  ng "イメージの取得または実行に失敗しました"
  hint "docker run --rm hello-world  を直接実行してエラーを確認してください"
fi

# ---------------------------------------------------------------------------
info "4. コンテナからの通信"

if docker run --rm alpine:3 sh -c 'nslookup registry-1.docker.io > /dev/null 2>&1' 2>/dev/null; then
  ok "コンテナ内の名前解決 (DNS)"
else
  ng "コンテナ内で名前解決ができません"
  hint "/etc/resolv.conf が壊れている可能性。/etc/wsl.conf に [network] generateResolvConf=true を確認"
fi

if docker run --rm alpine:3 sh -c 'apk add --no-cache curl > /dev/null 2>&1 && curl -fsS -m 10 -o /dev/null https://github.com' 2>/dev/null; then
  ok "コンテナから外部への HTTPS 通信"
else
  warn "コンテナから外部に出られませんでした（プロキシ環境なら要設定）"
  hint "社内プロキシ配下なら ~/.docker/config.json に proxies を設定してください"
fi

# ---------------------------------------------------------------------------
info "5. バインドマウント"
# compose が platform/otel-collector などをコンテナに渡すのに必須

MT=$(mktemp -d)
echo "roastery-mount-test" > "$MT/probe.txt"
if [ "$(docker run --rm -v "$MT:/probe:ro" alpine:3 cat /probe/probe.txt 2>/dev/null)" = "roastery-mount-test" ]; then
  ok "ホストのディレクトリをコンテナにマウントできる"
else
  ng "バインドマウントが機能していません"
  hint "compose が設定ファイルを渡せないため、このままでは起動しません"
fi
rm -rf "$MT"

# ---------------------------------------------------------------------------
info "6. ポート公開"

if docker run --rm -d --name roastery-port-probe -p 18080:80 nginx:alpine > /dev/null 2>&1; then
  sleep 2
  if curl -fsS -m 5 -o /dev/null http://localhost:18080; then
    ok "コンテナのポートをホストから叩ける (18080)"
    if [ "$IS_WSL" = "1" ]; then
      hint "Windows のブラウザで http://localhost:18080 が開けるかも見ておくと確実です"
    fi
  else
    ng "公開ポートにホストから到達できません"
    hint "3000 / 5432 / 6379 / 9090 / 16686 が他プロセスに使われていないか確認してください"
  fi
  docker rm -f roastery-port-probe > /dev/null 2>&1
else
  ng "ポートを公開したコンテナを起動できませんでした"
  hint "18080 番が既に使われている可能性があります"
fi

# ---------------------------------------------------------------------------
if [ "$IS_WSL" = "1" ]; then
  info "7. WSL2 固有の確認"

  if command -v systemctl > /dev/null 2>&1 && systemctl is-system-running > /dev/null 2>&1; then
    if systemctl is-enabled docker > /dev/null 2>&1; then
      ok "systemd で docker が自動起動する設定になっている"
    else
      warn "docker が自動起動しません（WSL を開き直すたびに手動起動が必要）"
      hint "sudo systemctl enable --now docker"
    fi
  else
    warn "systemd が有効ではありません"
    hint "/etc/wsl.conf に [boot] systemd=true を書き、PowerShell で wsl --shutdown"
    hint "有効にしない場合は起動のたびに sudo service docker start が必要です"
  fi

  case "$(pwd)" in
    /mnt/*)
      warn "リポジトリが Windows 側 ($(pwd)) にあります"
      hint "ビルドが数倍遅くなります。~/ 配下に clone し直すことを推奨します"
      ;;
    *)
      ok "リポジトリが Linux ファイルシステム上にある ($(pwd))"
      ;;
  esac

  MEM_GB=$(awk '/MemTotal/ {printf "%.1f", $2/1024/1024}' /proc/meminfo 2>/dev/null)
  if [ -n "$MEM_GB" ]; then
    if awk "BEGIN{exit !($MEM_GB >= 4)}"; then
      ok "WSL2 に割り当てられたメモリ ${MEM_GB}GB"
    else
      warn "WSL2 のメモリが ${MEM_GB}GB です（コンテナ 9 個で 2〜3GB 使います）"
      hint "%USERPROFILE%\\.wslconfig で memory=6GB 程度に増やすことを検討してください"
    fi
  fi

  DISK_GB=$(df -BG --output=avail . 2>/dev/null | tail -1 | tr -dc '0-9')
  if [ -n "$DISK_GB" ]; then
    if [ "$DISK_GB" -ge 10 ]; then
      ok "空きディスク ${DISK_GB}GB"
    else
      warn "空きディスクが ${DISK_GB}GB です（イメージだけで約 2.5GB 必要）"
    fi
  fi
fi

# ---------------------------------------------------------------------------
echo
printf '\033[1m結果: OK %d / NG %d / 注意 %d\033[0m\n' "$PASS" "$FAIL" "$WARN"
if [ "$FAIL" -eq 0 ]; then
  printf '\033[32mDocker 環境は準備できています。./scripts/local-start.sh に進めます。\033[0m\n'
  exit 0
else
  printf '\033[31mNG の項目を解消してから ./scripts/local-start.sh を実行してください。\033[0m\n'
  exit 1
fi
