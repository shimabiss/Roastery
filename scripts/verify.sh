#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# 起動後の動作確認。ここが全部 OK なら一通り動いている。
#
#   ./scripts/verify.sh
#
# 確認する内容:
#   1. 各サービスが応答するか
#   2. Collector が health_check に応答するか
#   3. 注文が通るか (DB / Redis / サービス間通信)
#   4. 障害注入が効くか
#   5. Jaeger にトレースが届いているか (= 計装と Collector が繋がっている)
#   6. Prometheus にカスタムメトリクスが届いているか
# ---------------------------------------------------------------------------
set -uo pipefail
cd "$(dirname "$0")/.."

PASS=0
FAIL=0

ok()   { printf '  \033[32mOK\033[0m   %s\n' "$1"; PASS=$((PASS+1)); }
ng()   { printf '  \033[31mNG\033[0m   %s\n' "$1"; FAIL=$((FAIL+1)); }
info() { printf '\n\033[1m%s\033[0m\n' "$1"; }

wait_for() {  # wait_for <url> <ラベル> [最大秒数]
  local url="$1" label="$2" max="${3:-90}" i=0
  while [ "$i" -lt "$max" ]; do
    if curl -fsS -m 3 "$url" > /dev/null 2>&1; then ok "$label"; return 0; fi
    sleep 2; i=$((i+2))
  done
  ng "$label (${max}秒待っても応答しません)"
  return 1
}

info "1. サービスの起動確認"
wait_for http://localhost:13133          "otel-collector (health_check)"
wait_for http://localhost:8002/healthz   "inventory-api"
wait_for http://localhost:8004/healthz   "external-stub"
wait_for http://localhost:8005/healthz   "member-api"
wait_for http://localhost:8003/healthz   "payment-api"
wait_for http://localhost:8001/healthz   "order-api"
wait_for http://localhost:3000/healthz   "frontend"
wait_for http://localhost:16686/         "jaeger UI"
wait_for http://localhost:9090/-/ready   "prometheus"

# ---------------------------------------------------------------------------
# P2: 会員必須にしたので、以降の注文には会員とセッションが要る。
# **ここが「依存駆動」の実感できるところ。** 認証は4つの学習テーマを1つも
# 支えないが、これが無いと以降の検証が1つも動かない。
# ---------------------------------------------------------------------------
info "1b. 会員登録・メール確認・ログイン (FR-101/102/103/114)"
JAR=$(mktemp)
EMAIL="verify-$$@example.com"
PASSWORD="roastery2026"

# FR-114: 弱いパスワードは **具体的な理由つきで** 拒否されること
WEAK=$(curl -s -o /dev/null -w '%{http_code}' -m 10 -X POST http://localhost:3000/api/members \
  -H 'content-type: application/json' -d '{"email":"weak-'"$$"'@example.com","password":"abc"}')
[ "$WEAK" = "422" ] && ok "弱いパスワードを拒否した" || ng "パスワードポリシーが効いていません ($WEAK)"

REG=$(curl -fsS -m 10 -c "$JAR" -b "$JAR" -X POST http://localhost:3000/api/members \
  -H 'content-type: application/json' \
  -d "{\"email\":\"$EMAIL\",\"password\":\"$PASSWORD\"}" 2>/dev/null || echo '')
case "$REG" in
  *member_id*) ok "会員登録できた" ;;
  *) ng "会員登録に失敗: ${REG:-応答なし}" ;;
esac

# BR-19: メール確認前は購入できないこと。**ログインはできる**
curl -fsS -m 10 -c "$JAR" -b "$JAR" -X POST http://localhost:3000/api/sessions \
  -H 'content-type: application/json' \
  -d "{\"email\":\"$EMAIL\",\"password\":\"$PASSWORD\"}" > /dev/null 2>&1 \
  && ok "未確認でもログインはできる (BR-19 の中間状態)" \
  || ng "ログインに失敗しました"

curl -fsS -m 10 -c "$JAR" -b "$JAR" -X POST http://localhost:3000/api/cart/items \
  -H 'content-type: application/json' -d '{"sku":"COFFEE-BEANS-1KG","quantity":1}' > /dev/null
CODE=$(curl -s -o /dev/null -w '%{http_code}' -m 15 -c "$JAR" -b "$JAR" \
  -X POST http://localhost:3000/api/checkout)
[ "$CODE" = "403" ] \
  && ok "メール未確認では購入できない (403)" \
  || ng "メール確認の関門が効いていません ($CODE)"

TOKEN=$(curl -fsS -m 10 "http://localhost:3000/api/mail/inbox?to=$EMAIL" \
  | tr ',' '\n' | grep -o 'token=[A-Za-z0-9_-]*' | head -1 | cut -d= -f2)
if [ -n "$TOKEN" ]; then
  curl -fsS -m 10 -X POST http://localhost:3000/api/members/verify \
    -H 'content-type: application/json' -d "{\"token\":\"$TOKEN\"}" > /dev/null \
    && ok "確認メールのリンクで確認が完了した" \
    || ng "メール確認に失敗しました"
else
  ng "確認メールが見つかりません"
fi

info "1b2. 住所帳と送料 (FR-106 / FR-404 / UC-01 手順4)"
ADDR=$(curl -fsS -m 10 -c "$JAR" -b "$JAR" -X POST http://localhost:3000/api/addresses \
  -H 'content-type: application/json' \
  -d '{"recipient":"検証 太郎","postal_code":"1000001","prefecture":"東京都","city":"千代田区","address_line":"1-1-1","phone":"0312345678"}' \
  2>/dev/null || echo '')
ADDR_ID=$(printf '%s' "$ADDR" | tr ',' '\n' | grep -o '"id": *"[^"]*"' | head -1 | cut -d'"' -f4)
[ -n "$ADDR_ID" ] && ok "住所を登録できた" || ng "住所帳の登録に失敗: ${ADDR:-応答なし}"

# 送料が **配送先で変わる** こと。同じ小計でも東京都と沖縄県で違う
FEE_TOKYO=$(curl -fsS -m 10 "http://localhost:3000/api/shipping/quote?prefecture=%E6%9D%B1%E4%BA%AC%E9%83%BD&subtotal=1000" \
  | tr ',' '\n' | grep '"shipping_fee"' | tr -dc '0-9')
FEE_OKINAWA=$(curl -fsS -m 10 "http://localhost:3000/api/shipping/quote?prefecture=%E6%B2%96%E7%B8%84%E7%9C%8C&subtotal=1000" \
  | tr ',' '\n' | grep '"shipping_fee"' | tr -dc '0-9')
if [ -n "$FEE_TOKYO" ] && [ -n "$FEE_OKINAWA" ] && [ "$FEE_TOKYO" -lt "$FEE_OKINAWA" ]; then
  ok "送料が配送先で変わる (東京 ${FEE_TOKYO} < 沖縄 ${FEE_OKINAWA})"
else
  ng "送料の地域差が反映されていません (${FEE_TOKYO:-?} / ${FEE_OKINAWA:-?})"
fi

# UC-01 A2: 閾値以上で送料無料
FEE_FREE=$(curl -fsS -m 10 "http://localhost:3000/api/shipping/quote?prefecture=%E6%B2%96%E7%B8%84%E7%9C%8C&subtotal=9000" \
  | tr ',' '\n' | grep '"shipping_fee"' | tr -dc '0-9')
[ "${FEE_FREE:-1}" = "0" ] && ok "閾値以上で送料無料になる (UC-01 A2)" \
  || ng "送料無料が効いていません (${FEE_FREE:-?})"

info "1c. 未ログインカートのマージ (FR-306 / BR-20)"
JAR2=$(mktemp)
EMAIL2="merge-$$@example.com"
curl -fsS -m 10 -X POST http://localhost:3000/api/members -H 'content-type: application/json' \
  -d "{\"email\":\"$EMAIL2\",\"password\":\"$PASSWORD\"}" > /dev/null 2>&1
T2=$(curl -fsS -m 10 "http://localhost:3000/api/mail/inbox?to=$EMAIL2" \
  | tr ',' '\n' | grep -o 'token=[A-Za-z0-9_-]*' | head -1 | cut -d= -f2)
curl -fsS -m 10 -X POST http://localhost:3000/api/members/verify \
  -H 'content-type: application/json' -d "{\"token\":\"$T2\"}" > /dev/null 2>&1
# 未ログインのままカートに入れる (BR-18: ログイン不要)
curl -fsS -m 10 -c "$JAR2" -b "$JAR2" -X POST http://localhost:3000/api/cart/items \
  -H 'content-type: application/json' -d '{"sku":"MUG-CERAMIC","quantity":2}' > /dev/null
MERGED=$(curl -fsS -m 10 -c "$JAR2" -b "$JAR2" -X POST http://localhost:3000/api/sessions \
  -H 'content-type: application/json' \
  -d "{\"email\":\"$EMAIL2\",\"password\":\"$PASSWORD\"}" 2>/dev/null || echo '')
case "$MERGED" in
  *MUG-CERAMIC*) ok "未ログインで入れた商品がログイン後も残っている" ;;
  *) ng "カートのマージができていません: ${MERGED:-応答なし}" ;;
esac
curl -fsS -m 10 -c "$JAR2" -b "$JAR2" -X DELETE http://localhost:3000/api/cart/items/MUG-CERAMIC > /dev/null

info "2. 正常系の注文 (複数明細が1注文になること / FR-408)"
# カートはサーバー側にあるので、明細は API で積んでから確定する
curl -fsS -m 10 -c "$JAR" -b "$JAR" -X POST http://localhost:3000/api/cart/items \
  -H 'content-type: application/json' -d '{"sku":"FILTER-100P","quantity":2}' > /dev/null
RESP=$(curl -fsS -m 15 -c "$JAR" -b "$JAR" -X POST http://localhost:3000/api/checkout \
  -H 'content-type: application/json' -d "{\"address_id\":\"$ADDR_ID\"}" \
  2>/dev/null || echo '')
ITEMS=$(printf '%s' "$RESP" | tr ',' '\n' | grep -c '"sku"' || true)
case "$RESP" in
  *ACCEPTED*)
    # 明細2件が1つの注文番号に入っていること。**注文が2件できていたら失敗**
    if [ "${ITEMS:-0}" -ge 2 ]; then
      ok "2明細が1つの注文になった ($(printf '%s' "$RESP" | head -c 60)...)"
    else
      ng "明細が1件しか入っていません: $RESP"
    fi ;;
  *) ng "注文に失敗: ${RESP:-応答なし}" ;;
esac

# FR-1001 / UC-01 手順11: 注文確認メール。
# **メール送信の失敗で注文を取り消してはならない** (E5) ので、
# ここでは「注文が成立していること」と「メールが届いていること」を別々に見る
sleep 1
MAILS=$(curl -fsS -m 10 "http://localhost:3000/api/mail/inbox?to=$EMAIL" 2>/dev/null || echo '')
case "$MAILS" in
  *ご注文を承りました*) ok "注文確認メールが送信された (FR-1001)" ;;
  *) ng "注文確認メールが見つかりません" ;;
esac

# ---------------------------------------------------------------------------
# UC-06 / UC-02。**P1 の補償と P4 の前進復旧の対比がここで見える。**
# ---------------------------------------------------------------------------
info "2b. 出荷処理と状態遷移 (UC-06 / BR-06 / BR-26)"
OID=$(printf '%s' "$RESP" | tr ',' '\n' | grep -o '"order_id": *"[^"]*"' | head -1 | cut -d'"' -f4)
if [ -z "$OID" ]; then
  ng "注文番号を取得できませんでした"
else
  # BR-05: 受付済ならキャンセルできる (この注文は出荷に使うので確認だけ)
  CANCELABLE=$(curl -fsS -m 10 "http://localhost:3000/api/orders/$OID" \
    | tr ',' '\n' | grep '"can_cancel"' | grep -c true || true)
  [ "${CANCELABLE:-0}" -ge 1 ] && ok "受付済はキャンセル可能 (BR-05)" \
    || ng "受付済なのにキャンセル不可になっています"

  # UC-06 手順4: 出荷準備中へ
  curl -fsS -m 10 -X POST "http://localhost:3000/api/ops/orders/$OID/prepare" > /dev/null 2>&1 \
    && ok "出荷準備中へ遷移した" || ng "出荷準備中への遷移に失敗しました"

  # BR-06: 出荷準備中に入った時点でキャンセル不可になること
  CODE=$(curl -s -o /dev/null -w '%{http_code}' -m 10 -c "$JAR" -b "$JAR" \
    -X POST "http://localhost:3000/api/orders/$OID/cancel")
  [ "$CODE" = "409" ] && ok "出荷準備中はキャンセルできない (BR-06)" \
    || ng "キャンセルの禁則が効いていません ($CODE)"

  # BR-26: 同じ遷移を2回投げたら2回目は弾かれること (排他制御)
  CODE=$(curl -s -o /dev/null -w '%{http_code}' -m 10 \
    -X POST "http://localhost:3000/api/ops/orders/$OID/prepare")
  [ "$CODE" = "409" ] && ok "重複した状態遷移を弾いた (BR-26)" \
    || ng "状態遷移の排他制御が効いていません ($CODE)"

  # UC-06 手順7〜11: 出荷確定。売上確定 → 実在庫減算 → 出荷済 → 通知
  SHIP=$(curl -fsS -m 20 -X POST "http://localhost:3000/api/ops/orders/$OID/ship" \
    -H 'content-type: application/json' -d '{}' 2>/dev/null || echo '')
  case "$SHIP" in
    *SHIPPED*) ok "出荷確定した (売上確定・実在庫の減算・追跡番号)" ;;
    *) ng "出荷に失敗: ${SHIP:-応答なし}" ;;
  esac

  # BR-31: 出荷確定後は巻き戻さない
  CODE=$(curl -s -o /dev/null -w '%{http_code}' -m 10 -c "$JAR" -b "$JAR" \
    -X POST "http://localhost:3000/api/orders/$OID/cancel")
  [ "$CODE" = "409" ] && ok "出荷済はキャンセルできない (BR-31)" \
    || ng "出荷後の巻き戻しが防げていません ($CODE)"

  sleep 1
  MAILS=$(curl -fsS -m 10 "http://localhost:3000/api/mail/inbox?to=$EMAIL" 2>/dev/null || echo '')
  case "$MAILS" in
    *発送しました*) ok "発送通知メールが送信された (FR-1002)" ;;
    *) ng "発送通知メールが見つかりません" ;;
  esac
fi

info "2c. 売上確定に失敗しても「受付済」に戻さない (UC-06 E1 / 前進復旧)"
# **ここが P1 との決定的な違い。** UC-01 は失敗したら全部巻き戻したが、
# UC-06 は巻き戻さない。梱包した箱は元に戻らないため。
curl -fsS -m 10 -c "$JAR" -b "$JAR" -X POST http://localhost:3000/api/cart/items \
  -H 'content-type: application/json' -d '{"sku":"FILTER-100P","quantity":1}' > /dev/null
ORD2=$(curl -fsS -m 15 -c "$JAR" -b "$JAR" -X POST http://localhost:3000/api/checkout \
  -H 'content-type: application/json' -d "{\"address_id\":\"$ADDR_ID\"}" 2>/dev/null || echo '')
OID2=$(printf '%s' "$ORD2" | tr ',' '\n' | grep -o '"order_id": *"[^"]*"' | head -1 | cut -d'"' -f4)
if [ -n "$OID2" ]; then
  curl -fsS -m 10 -X POST "http://localhost:3000/api/ops/orders/$OID2/prepare" > /dev/null
  # 決済代行を必ず失敗させる
  curl -fsS -m 5 -X POST "http://localhost:8004/admin/chaos?error_rate=1.0" > /dev/null
  CODE=$(curl -s -o /dev/null -w '%{http_code}' -m 20 \
    -X POST "http://localhost:3000/api/ops/orders/$OID2/ship" -H 'content-type: application/json' -d '{}')
  curl -fsS -m 5 -X POST "http://localhost:8004/admin/chaos" > /dev/null
  [ "$CODE" = "502" ] && ok "売上確定の失敗を検出した" || ng "売上確定の失敗応答が想定外 ($CODE)"

  STATE=$(curl -fsS -m 10 "http://localhost:3000/api/orders/$OID2" \
    | tr ',' '\n' | grep '"status"' | head -1)
  case "$STATE" in
    *PREPARING*) ok "失敗しても出荷準備中のまま留まっている (前進復旧)" ;;
    *) ng "状態が巻き戻っています: ${STATE:-不明}" ;;
  esac

  # リトライすれば前に進めること。**後退ではなく前進で解決する**
  SHIP2=$(curl -fsS -m 20 -X POST "http://localhost:3000/api/ops/orders/$OID2/ship" \
    -H 'content-type: application/json' -d '{}' 2>/dev/null || echo '')
  case "$SHIP2" in
    *SHIPPED*) ok "リトライで出荷を完了できた (二重請求なし / BR-28)" ;;
    *) ng "リトライで復旧できません: ${SHIP2:-応答なし}" ;;
  esac
else
  ng "前進復旧の検証用の注文を作れませんでした"
fi

info "2d. キャンセルで在庫と与信が戻る (UC-02 / FR-407)"
BEFORE=$(curl -fsS -m 10 http://localhost:8002/inventory/MUG-CERAMIC | tr ',' '\n' \
  | grep '"stock"' | tr -dc '0-9')
curl -fsS -m 10 -c "$JAR" -b "$JAR" -X POST http://localhost:3000/api/cart/items \
  -H 'content-type: application/json' -d '{"sku":"MUG-CERAMIC","quantity":1}' > /dev/null
ORD3=$(curl -fsS -m 15 -c "$JAR" -b "$JAR" -X POST http://localhost:3000/api/checkout \
  -H 'content-type: application/json' -d "{\"address_id\":\"$ADDR_ID\"}" 2>/dev/null || echo '')
OID3=$(printf '%s' "$ORD3" | tr ',' '\n' | grep -o '"order_id": *"[^"]*"' | head -1 | cut -d'"' -f4)
if [ -n "$OID3" ]; then
  curl -fsS -m 15 -c "$JAR" -b "$JAR" -X POST "http://localhost:3000/api/orders/$OID3/cancel" \
    > /dev/null 2>&1 && ok "会員が自分の注文をキャンセルできた" || ng "キャンセルに失敗しました"
  sleep 1
  AFTER=$(curl -fsS -m 10 http://localhost:8002/inventory/MUG-CERAMIC | tr ',' '\n' \
    | grep '"stock"' | tr -dc '0-9')
  [ "${BEFORE:-0}" = "${AFTER:-1}" ] \
    && ok "キャンセルで引当が解放された (${BEFORE} のまま)" \
    || ng "在庫が戻っていません (${BEFORE} -> ${AFTER})"
else
  ng "キャンセル検証用の注文を作れませんでした"
fi

info "2e. 運用機能 (FR-406 / FR-605 / FR-1102 / FR-1201)"
# FR-1102: 非公開にすると公開サイトのカタログから消えること。
# **既定を「出さない」にしてあるかの確認**でもある
curl -fsS -m 10 -X PUT http://localhost:3000/api/ops/products/FILTER-100P \
  -H 'content-type: application/json' -d '{"published":false}' > /dev/null
PUB=$(curl -fsS -m 10 http://localhost:3000/api/catalog | grep -c FILTER-100P || true)
ALL=$(curl -fsS -m 10 "http://localhost:3000/api/catalog?all=1" | grep -c FILTER-100P || true)
[ "${PUB:-1}" = "0" ] && [ "${ALL:-0}" -ge 1 ] \
  && ok "非公開商品が公開サイトから消え、運用画面には残る (FR-1102)" \
  || ng "公開・非公開の切替が効いていません (public=${PUB} ops=${ALL})"
curl -fsS -m 10 -X PUT http://localhost:3000/api/ops/products/FILTER-100P \
  -H 'content-type: application/json' -d '{"published":true}' > /dev/null

# FR-605: 入荷で実在庫が増えること。**引当済数は動かない**
BEFORE=$(curl -fsS -m 10 http://localhost:8002/inventory/FILTER-100P | tr ',' '\n' \
  | grep '"physical"' | tr -dc '0-9')
curl -fsS -m 10 -X POST http://localhost:3000/api/inventory/FILTER-100P/adjust \
  -H 'content-type: application/json' -d '{"delta":10}' > /dev/null
AFTER=$(curl -fsS -m 10 http://localhost:8002/inventory/FILTER-100P | tr ',' '\n' \
  | grep '"physical"' | tr -dc '0-9')
[ "$((${BEFORE:-0} + 10))" = "${AFTER:-0}" ] \
  && ok "入荷で実在庫が増えた (${BEFORE} -> ${AFTER})" \
  || ng "在庫調整が効いていません (${BEFORE} -> ${AFTER})"

# FR-406: 自分の注文だけが返ること
MINE=$(curl -fsS -m 10 -c "$JAR" -b "$JAR" "http://localhost:3000/api/orders?mine=1" \
  | grep -c '"order_id"\|"id"' || true)
[ "${MINE:-0}" -ge 1 ] && ok "注文履歴を参照できた (FR-406)" || ng "注文履歴が取得できません"

# FR-1201〜1204: 法定表示が配信されていること
LEGAL=$(curl -fsS -m 10 http://localhost:3000/ | grep -c 'index-' || true)
[ "${LEGAL:-0}" -ge 1 ] && ok "公開サイトが配信されている" || ng "公開サイトの配信に失敗しています"

info "3. 在庫不足の検出と、全明細の巻き戻し (BR-03)"
BEFORE=$(curl -fsS -m 10 http://localhost:8002/inventory/COFFEE-BEANS-1KG | tr ',' '\n' \
  | grep '"stock"' | tr -dc '0-9')
# order-api を直接叩く。**member_id が必須** (FR-108 ゲスト購入を Won't にしたため)
CODE=$(curl -s -o /dev/null -w '%{http_code}' -m 15 -X POST http://localhost:8001/orders \
  -H 'content-type: application/json' \
  -d '{"member_id":"verify-probe","lines":[{"sku":"COFFEE-BEANS-1KG","quantity":1},{"sku":"DRIP-KETTLE","quantity":999}]}')
AFTER=$(curl -fsS -m 10 http://localhost:8002/inventory/COFFEE-BEANS-1KG | tr ',' '\n' \
  | grep '"stock"' | tr -dc '0-9')
[ "$CODE" = "409" ] && ok "在庫不足で 409 を返した" || ng "在庫不足の応答が 409 ではなく $CODE"
# 1件目 (在庫あり) が引き当てられたまま残っていないこと
[ "${BEFORE:-0}" = "${AFTER:-1}" ] \
  && ok "失敗した注文で他の明細も引き当てられていない (${BEFORE} のまま)" \
  || ng "全部か無かが守られていません (${BEFORE} -> ${AFTER})"

info "4. 障害注入 (遅延)"
curl -fsS -m 5 -X POST "http://localhost:8004/admin/chaos?latency_ms=1500" > /dev/null
# 経過時間は curl の %{time_total} で測る (macOS の date は %N 非対応のため)
curl -fsS -m 10 -c "$JAR" -b "$JAR" -X POST http://localhost:3000/api/cart/items \
  -H 'content-type: application/json' -d '{"sku":"MUG-CERAMIC","quantity":1}' > /dev/null
SEC=$(curl -fsS -o /dev/null -w '%{time_total}' -m 20 -c "$JAR" -b "$JAR" \
  -X POST http://localhost:3000/api/checkout)
MS=$(printf '%.0f' "$(echo "${SEC:-0} 1000" | awk '{print $1 * $2}')")
[ "$MS" -gt 1400 ] && ok "遅延注入が効いている (${MS}ms)" || ng "遅延が反映されていない (${MS}ms)"

info "5. 決済失敗で在庫が戻ること (FR-603 / 補償トランザクション)"
curl -fsS -m 5 -X POST "http://localhost:8004/admin/chaos?latency_ms=0&error_rate=1.0" > /dev/null
BEFORE=$(curl -fsS -m 10 http://localhost:8002/inventory/MUG-CERAMIC | tr ',' '\n' \
  | grep '"stock"' | tr -dc '0-9')
curl -fsS -m 10 -c "$JAR" -b "$JAR" -X POST http://localhost:3000/api/cart/items \
  -H 'content-type: application/json' -d '{"sku":"MUG-CERAMIC","quantity":1}' > /dev/null
CODE=$(curl -s -o /dev/null -w '%{http_code}' -m 20 -c "$JAR" -b "$JAR" \
  -X POST http://localhost:3000/api/checkout)
sleep 1
AFTER=$(curl -fsS -m 10 http://localhost:8002/inventory/MUG-CERAMIC | tr ',' '\n' \
  | grep '"stock"' | tr -dc '0-9')
[ "$CODE" = "502" ] && ok "決済失敗で 502 を返した" || ng "決済失敗の応答が 502 ではなく $CODE"
# **修正前はここが必ず失敗した。** 引き当てた在庫が戻らなかったため
[ "${BEFORE:-0}" = "${AFTER:-1}" ] \
  && ok "決済失敗後に引当が解放されている (${BEFORE} のまま)" \
  || ng "在庫が戻っていません (${BEFORE} -> ${AFTER})"

info "5b. 決済代行の無応答 (UC-01 E3)"
# エラー応答と無応答は **別の異常系**。前者しか試さないとテストが片肺になる
curl -fsS -m 5 -X POST "http://localhost:8004/admin/chaos?error_rate=0&no_response_rate=1.0&no_response_hold_s=30" > /dev/null
BEFORE=$(curl -fsS -m 10 http://localhost:8002/inventory/FILTER-100P | tr ',' '\n' \
  | grep '"stock"' | tr -dc '0-9')
curl -fsS -m 10 -c "$JAR" -b "$JAR" -X POST http://localhost:3000/api/cart/items \
  -H 'content-type: application/json' -d '{"sku":"FILTER-100P","quantity":1}' > /dev/null
CODE=$(curl -s -o /dev/null -w '%{http_code}' -m 30 -c "$JAR" -b "$JAR" \
  -X POST http://localhost:3000/api/checkout)
sleep 2
AFTER=$(curl -fsS -m 10 http://localhost:8002/inventory/FILTER-100P | tr ',' '\n' \
  | grep '"stock"' | tr -dc '0-9')
case "$CODE" in
  502|504) ok "無応答を検出して注文を成立させなかった ($CODE)" ;;
  *)       ng "無応答時の応答が想定外です: $CODE" ;;
esac
[ "${BEFORE:-0}" = "${AFTER:-1}" ] \
  && ok "無応答でも引当が解放されている (${BEFORE} のまま)" \
  || ng "在庫が戻っていません (${BEFORE} -> ${AFTER})"
curl -fsS -m 5 -X POST "http://localhost:8004/admin/chaos" > /dev/null
curl -fsS -m 5 -X POST "http://localhost:8002/admin/reset" > /dev/null

info "6. Jaeger にトレースが届いているか"
echo "     (Collector のバッチ待ちで 15 秒ほどかかります)"
FOUND=0
for i in $(seq 1 12); do
  sleep 5
  SERVICES=$(curl -fsS -m 5 "http://localhost:16686/api/services" 2>/dev/null || echo '')
  MISSING=""
  for s in frontend order-api inventory-api payment-api external-stub member-api; do
    echo "$SERVICES" | grep -q "\"$s\"" || MISSING="$MISSING $s"
  done
  if [ -z "$MISSING" ]; then FOUND=1; break; fi
done
if [ "$FOUND" = "1" ]; then
  ok "6サービスすべてが Jaeger に登録された"
else
  ng "Jaeger に未登録のサービスがあります:$MISSING"
fi

# 6サービスを跨ぐ trace が実際に1本になっているか
TRACE=$(curl -fsS -m 10 "http://localhost:16686/api/traces?service=frontend&operation=POST%20%2Fapi%2Fcheckout&limit=5" 2>/dev/null || echo '')
COUNT=$(echo "$TRACE" | grep -o '"serviceName"' | wc -l | tr -d ' ')
if [ "${COUNT:-0}" -gt 10 ]; then
  ok "frontend 起点の trace に複数サービスの span が含まれている"
else
  ng "frontend 起点の trace が期待どおり取得できません"
fi

info "7. Prometheus にカスタムメトリクスが届いているか"
# ---------------------------------------------------------------------------
# 注意点が2つある。
#
# (1) カスタムメトリクスは注文が「成功」したときにしかデータ点が出ない。さらに
#     delta temporality なので、トラフィックが止まると Collector の prometheus
#     エクスポータが metric_expiration (既定 5分) でシリーズごと破棄する。
#     そのため、ここで自分で1件流してから待つ。
#
# (2) 取得結果は一時ファイルに落として grep する。
#     `M=$(curl ...); echo "$M" | grep -q PATTERN` と書くと、応答が大きいときに
#     grep が一致した時点で終了してパイプを閉じ、書き込み途中の echo が SIGPIPE
#     で死ぬ。冒頭の `set -o pipefail` によりパイプライン全体が exit 141 となり、
#     「一致しているのに一致していない」と判定される。
#     応答が数十 KB 未満なら顕在化しないため、データ量が増えて初めて壊れる。
# ---------------------------------------------------------------------------
curl -fsS -m 10 -c "$JAR" -b "$JAR" -X POST http://localhost:3000/api/cart/items \
  -H 'content-type: application/json' -d '{"sku":"COFFEE-BEANS-1KG","quantity":1}' > /dev/null
ORDER=$(curl -fsS -m 15 -c "$JAR" -b "$JAR" -X POST http://localhost:3000/api/checkout \
  2>/dev/null || echo '')

case "$ORDER" in
  *ACCEPTED*)
    echo "     (メトリクスのエクスポート間隔 15 秒を待ちます)"
    METRICS_TMP=$(mktemp)
    FOUND=0
    for _ in $(seq 1 12); do
      sleep 5
      if curl -fsS -m 15 -o "$METRICS_TMP" "http://localhost:8889/metrics" 2>/dev/null &&
         grep -q '^orders_created' "$METRICS_TMP"; then
        FOUND=1; break
      fi
    done
    rm -f "$METRICS_TMP"
    [ "$FOUND" = "1" ] && ok "orders.created が Collector から公開されている" \
                       || ng "カスタムメトリクスが見つかりません"
    ;;
  *)
    ng "メトリクス確認用の注文が失敗しました: ${ORDER:-応答なし}"
    ;;
esac

info "結果: ${PASS} OK / ${FAIL} NG"
if [ "$FAIL" -eq 0 ]; then
  cat <<'EOF'

すべて正常です。次を開いて確認してください。

  http://localhost:3000        デモ画面 (注文 / 障害注入パネル)
  http://localhost:16686       Jaeger — トレースの waterfall
  http://localhost:9090        Prometheus — メトリクス
  http://localhost:55679/debug/tracez   Collector の中身

負荷をかけ続けるなら:  ./scripts/load.sh
EOF
  exit 0
else
  cat <<'EOF'

NG があります。まずログを確認してください。

  docker compose ps
  docker compose logs otel-collector | tail -50
  docker compose logs order-api | tail -50
EOF
  exit 1
fi
