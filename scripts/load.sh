#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# 継続的に注文を流し続ける負荷生成スクリプト。
# 裏で回しておくと、メトリクスのグラフが動いて挙動を追いやすい。
#
#   ./scripts/load.sh            # 既定: 0.5秒間隔で無限に
#   ./scripts/load.sh 0.2 100    # 0.2秒間隔で100件
#
# **P2 で会員必須になったので、注文する前に会員とセッションが要る。**
# 旧版は SKU を /api/checkout に直接投げていたが、現在の checkout は
#   ログイン済み / メール確認済み (BR-19) / サーバー側カートに明細がある (FR-306)
# の3つを要求するため、そのままでは 401 で1件も通らない。
# そこで起動時に一度だけ次を通してから、ループに入る。
#
#   会員登録 -> 確認メールのトークンで確認 -> ログイン (Cookie を保持) -> 住所登録
#
# **画面と同じ経路 (frontend の API) だけを使う**のが要点。
# order-api を直接叩けば会員もカートも要らないが、それでは frontend 起点の
# trace にならず、waterfall が本番の形と変わってしまう。
#
# 環境変数:
#   FRONTEND_URL   既定 http://localhost:3000
#   LOAD_EMAIL     既定 load-<PID>@example.com。固定すると会員を使い回せる
#   LOAD_PASSWORD  既定 roastery-load-2026 (10文字以上。FR-114 のポリシー)
#   AUTO_RESTOCK   既定 1。在庫切れ (409) を検出したら在庫をリセットして続行する
# ---------------------------------------------------------------------------
# set -e は付けない。負荷生成は **1回の失敗で止まってはいけない**。
# 失敗も含めて数え続けるほうが、メトリクスを見るという目的に合う。
set -uo pipefail

INTERVAL="${1:-0.5}"
COUNT="${2:-0}"          # 0 = 無限
BASE="${FRONTEND_URL:-http://localhost:3000}"
EMAIL="${LOAD_EMAIL:-load-$$@example.com}"
PASSWORD="${LOAD_PASSWORD:-roastery-load-2026}"
AUTO_RESTOCK="${AUTO_RESTOCK:-1}"

SKUS=("COFFEE-BEANS-1KG" "MUG-CERAMIC" "FILTER-100P" "DRIP-KETTLE")

JAR=$(mktemp)
BODY=$(mktemp)
cleanup() { rm -f "$JAR" "$BODY"; }
trap cleanup EXIT
# Ctrl-C で止めたときに、進捗行の途中でプロンプトが戻らないようにする
trap 'echo; exit 0' INT

api() {  # api <メソッド> <パス> [JSON本文] -> 本文は $BODY / 標準出力は HTTP status
  local method="$1" path="$2" data="${3:-}"
  if [ -n "$data" ]; then
    curl -s -o "$BODY" -w '%{http_code}' -m 20 -c "$JAR" -b "$JAR" \
      -X "$method" "${BASE}${path}" -H 'content-type: application/json' -d "$data"
  else
    curl -s -o "$BODY" -w '%{http_code}' -m 20 -c "$JAR" -b "$JAR" \
      -X "$method" "${BASE}${path}"
  fi
}

die() { echo "$*" >&2; exit 1; }

# --- 0. frontend が居るか ---------------------------------------------------
curl -fsS -m 5 "${BASE}/healthz" > /dev/null 2>&1 \
  || die "frontend (${BASE}) に接続できません。先に ./scripts/local-start.sh を実行してください。"

# --- 1. 会員登録 (FR-101) ---------------------------------------------------
echo "==> 会員を準備します: ${EMAIL}"
CODE=$(api POST /api/members "{\"email\":\"${EMAIL}\",\"password\":\"${PASSWORD}\"}")
case "$CODE" in
  201) echo "    登録しました" ;;
  409) echo "    既に登録済みの会員を使います" ;;
  *)   die "会員登録に失敗しました (HTTP ${CODE}): $(cat "$BODY")" ;;
esac

# --- 2. メール確認 (FR-102 / BR-19) -----------------------------------------
# **確認前は購入できない。** 確認メールは external-stub の受信箱に溜まるので、
# そこからトークンを取り出す。既に確認済みならトークンは残っていない。
TOKEN=$(curl -fsS -m 10 "${BASE}/api/mail/inbox?to=${EMAIL}" 2>/dev/null \
  | tr ',' '\n' | grep -o 'token=[A-Za-z0-9_-]*' | head -1 | cut -d= -f2)
if [ -n "$TOKEN" ]; then
  CODE=$(api POST /api/members/verify "{\"token\":\"${TOKEN}\"}")
  [ "$CODE" = "200" ] && echo "    メール確認を完了しました" \
                      || echo "    メール確認に失敗しました (HTTP ${CODE})" >&2
fi

# --- 3. ログイン (FR-103) ---------------------------------------------------
CODE=$(api POST /api/sessions "{\"email\":\"${EMAIL}\",\"password\":\"${PASSWORD}\"}")
[ "$CODE" = "200" ] || die "ログインに失敗しました (HTTP ${CODE}): $(cat "$BODY")"
echo "    ログインしました"

# --- 4. 配送先 (FR-106 / FR-404) --------------------------------------------
# 住所を1件だけ登録して使い回す。送料は配送先の都道府県から決まるので、
# ここを付けておかないと送料計算の経路が trace に出ない。
ADDR_ID=$(curl -fsS -m 10 -c "$JAR" -b "$JAR" -X POST "${BASE}/api/addresses" \
  -H 'content-type: application/json' \
  -d '{"recipient":"負荷 太郎","postal_code":"1000001","prefecture":"東京都","city":"千代田区","address_line":"1-1-1","phone":"0312345678"}' \
  2>/dev/null | tr ',' '\n' | grep -o '"id": *"[^"]*"' | head -1 | cut -d'"' -f4)
[ -n "$ADDR_ID" ] || echo "    住所を登録できませんでした。送料なしで続行します" >&2

echo
echo "load generator -> ${BASE}  interval=${INTERVAL}s count=${COUNT}  (0 = 無限)"
echo "停止は Ctrl-C"

CHECKOUT_BODY='{}'
[ -n "$ADDR_ID" ] && CHECKOUT_BODY="{\"address_id\":\"${ADDR_ID}\"}"

i=0; ok=0; ng=0
while true; do
  sku="${SKUS[$((RANDOM % ${#SKUS[@]}))]}"

  # カートに1明細だけ入れてから確定する (checkout はカートの中身を見る)
  api POST /api/cart/items "{\"sku\":\"${sku}\",\"quantity\":1}" > /dev/null
  code=$(api POST /api/checkout "$CHECKOUT_BODY")
  i=$((i + 1))

  case "$code" in
    200)
      ok=$((ok + 1)) ;;
    409)
      # 在庫切れ。**業務上の拒否であって障害ではない。**
      # 流し続けたいだけなので、既定では在庫を戻して続行する。
      # /api/reset は inventory-api の在庫と external-stub の受信箱・与信台帳を
      # まとめて初期化する。メールの中身を見ている最中は AUTO_RESTOCK=0 にする。
      ng=$((ng + 1))
      api DELETE "/api/cart/items/${sku}" > /dev/null
      if [ "$AUTO_RESTOCK" = "1" ]; then
        curl -fsS -m 10 -X POST "${BASE}/api/reset" > /dev/null 2>&1
        printf '\r  在庫が尽きたのでリセットしました%-30s\n' ""
      fi ;;
    401|403)
      # セッション切れ (FR-112) か、メール未確認。1度だけ再ログインを試す。
      ng=$((ng + 1))
      api DELETE "/api/cart/items/${sku}" > /dev/null
      if [ "$(api POST /api/sessions "{\"email\":\"${EMAIL}\",\"password\":\"${PASSWORD}\"}")" != "200" ]; then
        echo; die "セッションを再取得できませんでした (HTTP ${code})"
      fi
      printf '\r  セッションを取り直しました%-30s\n' "" ;;
    *)
      # 障害注入中はここに来る (502 = 決済失敗 / 504 = 決済代行の無応答)。
      # **カートは残る**ので、次の明細と混ざらないように片付ける。
      ng=$((ng + 1))
      api DELETE "/api/cart/items/${sku}" > /dev/null ;;
  esac

  printf '\r  sent=%-6s ok=%-6s ng=%-6s last=%s %-20s' "$i" "$ok" "$ng" "$code" "$sku"

  if [ "$COUNT" -ne 0 ] && [ "$i" -ge "$COUNT" ]; then
    echo ""
    break
  fi
  sleep "$INTERVAL"
done
