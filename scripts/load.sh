#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# 継続的に注文を流し続ける負荷生成スクリプト。
# 裏で回しておくと、メトリクスのグラフが動いて挙動を追いやすい。
#
#   ./scripts/load.sh            # 既定: 1秒に約2件を無限に
#   ./scripts/load.sh 0.2 100    # 0.2秒間隔で100件
# ---------------------------------------------------------------------------
set -euo pipefail

INTERVAL="${1:-0.5}"
COUNT="${2:-0}"          # 0 = 無限
BASE="${FRONTEND_URL:-http://localhost:3000}"

SKUS=("COFFEE-BEANS-1KG" "MUG-CERAMIC" "FILTER-100P" "DRIP-KETTLE")

echo "load generator -> ${BASE}  interval=${INTERVAL}s count=${COUNT:-inf}"
echo "停止は Ctrl-C"

i=0
while true; do
  sku="${SKUS[$((RANDOM % ${#SKUS[@]}))]}"
  code=$(curl -s -o /dev/null -w '%{http_code}' \
    -X POST "${BASE}/api/checkout" \
    -H 'content-type: application/json' \
    -d "{\"sku\":\"${sku}\",\"quantity\":1}") || code="ERR"
  i=$((i + 1))
  printf '\r  sent=%-6s last=%s %-20s' "$i" "$code" "$sku"

  if [ "$COUNT" -ne 0 ] && [ "$i" -ge "$COUNT" ]; then
    echo ""
    break
  fi
  sleep "$INTERVAL"
done
