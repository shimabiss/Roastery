#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# 障害注入をコマンドラインから切り替える。画面の Chaos パネルと同じことをする。
#
#   ./scripts/chaos.sh slow 1500        # payment-api に 1.5 秒の遅延
#   ./scripts/chaos.sh error 0.3        # payment-api を 30% の確率で失敗
#   ./scripts/chaos.sh inv-slow 800     # inventory-api の在庫参照を 0.8 秒遅く
#   ./scripts/chaos.sh clear            # すべて解除
#   ./scripts/chaos.sh status           # 現在の設定を表示
# ---------------------------------------------------------------------------
set -euo pipefail

PAYMENT="${PAYMENT_URL:-http://localhost:8003}"
INVENTORY="${INVENTORY_URL:-http://localhost:8002}"

case "${1:-status}" in
  slow)
    curl -s -X POST "${PAYMENT}/admin/chaos?latency_ms=${2:-1500}" | tee; echo ;;
  error)
    curl -s -X POST "${PAYMENT}/admin/chaos?error_rate=${2:-0.3}" | tee; echo ;;
  inv-slow)
    curl -s -X POST "${INVENTORY}/admin/chaos?slow_ms=${2:-800}" | tee; echo ;;
  clear)
    curl -s -X POST "${PAYMENT}/admin/chaos?latency_ms=0&error_rate=0" > /dev/null
    curl -s -X POST "${INVENTORY}/admin/chaos?slow_ms=0" > /dev/null
    curl -s -X POST "${INVENTORY}/admin/reset" > /dev/null
    echo "chaos cleared / inventory reset" ;;
  status)
    echo -n "payment  : "; curl -s "${PAYMENT}/admin/chaos"; echo ;;
  *)
    echo "usage: $0 {slow|error|inv-slow|clear|status} [value]" >&2; exit 1 ;;
esac
