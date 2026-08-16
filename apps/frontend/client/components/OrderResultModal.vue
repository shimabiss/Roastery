<script setup lang="ts">
import { computed } from 'vue'
import { yen } from '../useCart'
import type { CheckoutOutcome, InsufficientDetail } from '../types'

/**
 * 注文結果の表示。
 *
 * **BR-03 により「一部だけ成功」という状態が消えた。**
 * 修正前はここで明細ごとの成否を並べていたが、
 * 現在は注文1件の成功か失敗のどちらかしかない。
 * 要件の変更が、そのまま画面の単純化になっている。
 */
const props = defineProps<{ outcome: CheckoutOutcome | null }>()
defineEmits<{ close: [] }>()

const ok = computed(() => props.outcome?.ok === true)
const order = computed(() => props.outcome?.body ?? {})

const title = computed(() =>
  ok.value ? 'ご注文ありがとうございました' : 'ご注文を承れませんでした',
)

/** 在庫不足のとき、どの商品が足りなかったかを取り出す (UC-01 E1) */
const shortage = computed<InsufficientDetail | null>(() => {
  const d = props.outcome?.body?.detail
  if (d && typeof d === 'object' && 'sku' in d) return d as InsufficientDetail
  return null
})

/** HTTP ステータスを利用者向けの文言に置き換える */
const reason = computed(() => {
  const status = props.outcome?.status ?? 0
  if (status === 409) {
    return shortage.value
      ? `申し訳ありません。「${shortage.value.sku}」の在庫が不足しています（残り ${shortage.value.available} 点）。数量をご確認ください。`
      : '申し訳ありません。ご注文の数量に対して在庫が不足しています。'
  }
  if (status === 502 || status === 504)
    return '決済処理が完了しませんでした。ご請求は発生していません。お手数ですが、時間をおいて再度お試しください。'
  if (status === 0) return 'ネットワークに接続できませんでした。'
  return '注文処理中にエラーが発生しました。'
})
</script>

<template>
  <div v-if="outcome" class="modal">
    <div class="modal-box">
      <h2>{{ title }}</h2>

      <template v-if="ok">
        <p class="sub">
          注文番号 {{ order.order_id?.slice(0, 8) }} ・ 確認メールをお送りします。
        </p>
        <div v-for="item in order.items" :key="item.sku" class="res okr">
          <p class="rn">{{ item.name }} × {{ item.quantity }}</p>
          <p class="rd">{{ yen(item.subtotal) }}</p>
        </div>
        <dl class="totals">
          <div><dt>商品計</dt><dd>{{ yen(order.subtotal ?? 0) }}</dd></div>
          <div>
            <dt>送料</dt>
            <dd>{{ (order.shipping_fee ?? 0) === 0 ? '無料' : yen(order.shipping_fee ?? 0) }}</dd>
          </div>
          <div class="grand"><dt>合計</dt><dd>{{ yen(order.total ?? 0) }}</dd></div>
        </dl>
      </template>

      <template v-else>
        <p class="sub">{{ reason }}</p>
        <!--
          BR-03 の効果がここに出ている。全体が取り消されているので、
          「カートはそのまま残っている」と言い切れる (UC-01 E2 9c)。
        -->
        <p class="keep">カートの内容はそのまま残っています。</p>
      </template>

      <button class="btn ghost full" @click="$emit('close')">閉じる</button>
    </div>
  </div>
</template>

<style scoped>
.modal {
  position: fixed; inset: 0; z-index: 70; display: grid; place-items: center;
  padding: 24px; background: rgba(43, 35, 32, .42);
}
.modal-box {
  background: var(--surface); border-radius: 16px; padding: 32px; max-width: 480px; width: 100%;
  box-shadow: 0 20px 60px rgba(43, 35, 32, .24); max-height: 84vh; overflow-y: auto;
}
h2 { margin: 0 0 8px; font-size: 20px; }
.sub { color: var(--muted); font-size: 14px; margin: 0 0 20px; }
.res { border: 1px solid var(--line); border-radius: 10px; padding: 12px 14px; margin-bottom: 10px; font-size: 13px; }
.res.okr { border-left: 3px solid var(--ok); }
.rn { font-weight: 600; margin: 0 0 2px; }
.rd { color: var(--muted); margin: 0; font-size: 12px; font-variant-numeric: tabular-nums; }
.totals { margin: 16px 0 4px; font-size: 13px; }
.totals div { display: flex; justify-content: space-between; padding: 4px 0; }
.totals dt, .totals dd { margin: 0; }
.totals dd { font-variant-numeric: tabular-nums; }
.totals .grand { border-top: 1px solid var(--line); margin-top: 6px; padding-top: 10px; font-weight: 700; font-size: 15px; }
.keep { font-size: 13px; color: var(--muted); background: var(--bg); border-radius: 8px; padding: 10px 12px; margin: 0 0 8px; }
.full { width: 100%; margin-top: 8px; }
</style>
