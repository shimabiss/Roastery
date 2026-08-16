<script setup lang="ts">
import { ref, watch } from 'vue'
import { yen } from '../useCart'
import type { OrderSummary } from '../types'

/**
 * 注文履歴 (FR-406 / US-11) とキャンセル (FR-407 / UC-02)。
 *
 * **キャンセルできるかどうかはサーバーが返す ``can_cancel`` に従う。**
 * 画面側で状態名を見て判定すると、状態が増えたときに必ず取り残される。
 * 判定はドメインの側 (states.py) に1つだけ置く。
 */
const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ close: []; changed: [] }>()

const orders = ref<OrderSummary[]>([])
const loading = ref(false)
const error = ref('')
const busyId = ref('')

async function load(): Promise<void> {
  loading.value = true
  error.value = ''
  try {
    const res = await fetch('/api/orders?mine=1', { credentials: 'same-origin' })
    orders.value = res.ok ? await res.json() : []
  } catch {
    error.value = '注文履歴を取得できませんでした。'
  }
  loading.value = false
}

watch(() => props.open, (open) => { if (open) void load() })

async function cancel(orderId: string): Promise<void> {
  busyId.value = orderId
  const res = await fetch(`/api/orders/${orderId}/cancel`, {
    method: 'POST',
    credentials: 'same-origin',
  })
  busyId.value = ''
  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    const detail = body.detail
    // UC-02 E1: キャンセルできない場合は **返品の案内を出す**
    error.value =
      typeof detail === 'object' && detail
        ? `${detail.message}${detail.hint ? ` ${detail.hint}` : ''}`
        : 'キャンセルできませんでした。'
  }
  await load()
  emit('changed')
}
</script>

<template>
  <div v-if="open" class="modal" @click.self="$emit('close')">
    <div class="modal-box">
      <h2>注文履歴</h2>

      <p v-if="loading" class="note">読み込み中…</p>
      <p v-else-if="orders.length === 0" class="note">ご注文はまだありません。</p>
      <p v-if="error" class="err">{{ error }}</p>

      <!-- US-11: 新しい順に表示し、各注文の状態が分かること -->
      <div v-for="o in orders" :key="o.id" class="ord">
        <div class="head">
          <strong>{{ o.id.slice(0, 8) }}</strong>
          <span class="badge" :class="o.status.toLowerCase()">{{ o.status_label }}</span>
        </div>
        <p class="date">{{ new Date(o.created_at).toLocaleString('ja-JP') }}</p>
        <p v-for="i in o.items" :key="i.sku" class="li">
          {{ i.name }} × {{ i.quantity }}<span class="num">{{ yen(i.subtotal) }}</span>
        </p>
        <p class="tot">
          <span>合計</span><span class="num">{{ yen(o.total) }}</span>
        </p>
        <p v-if="o.tracking_no" class="note">追跡番号 {{ o.tracking_no }}</p>

        <button
          v-if="o.can_cancel"
          class="btn ghost sm"
          :disabled="busyId === o.id"
          @click="cancel(o.id)"
        >
          {{ busyId === o.id ? '処理中…' : 'この注文をキャンセルする' }}
        </button>
        <!-- UC-02 E1: 出荷後は返品の案内へ -->
        <p v-else-if="o.status === 'SHIPPED' || o.status === 'DELIVERED'" class="note">
          発送済みのためキャンセルできません。返品をご希望の場合はお問い合わせください。
        </p>
      </div>

      <button class="btn ghost full" @click="$emit('close')">閉じる</button>
    </div>
  </div>
</template>

<style scoped>
.modal {
  position: fixed; inset: 0; z-index: 78; display: grid; place-items: center;
  padding: 24px; background: rgba(43, 35, 32, .42);
}
.modal-box {
  background: var(--surface); border-radius: 16px; padding: 28px; max-width: 480px; width: 100%;
  box-shadow: 0 20px 60px rgba(43, 35, 32, .24); max-height: 88vh; overflow-y: auto;
}
h2 { margin: 0 0 16px; font-size: 20px; }
.ord { border: 1px solid var(--line); border-radius: 10px; padding: 14px; margin-bottom: 12px; }
.head { display: flex; align-items: center; justify-content: space-between; }
.badge {
  font-size: 11px; border-radius: 999px; padding: 2px 10px; background: var(--bg); color: var(--muted);
}
.badge.shipped, .badge.delivered { color: var(--ok); }
.badge.cancelled, .badge.failed { color: var(--err); }
.badge.preparing { color: var(--warn); }
.date { font-size: 11px; color: var(--muted); margin: 4px 0 10px; }
.li { display: flex; justify-content: space-between; font-size: 13px; margin: 3px 0; }
.tot {
  display: flex; justify-content: space-between; font-size: 13px; font-weight: 700;
  border-top: 1px solid var(--line); margin-top: 8px; padding-top: 8px;
}
.num { font-variant-numeric: tabular-nums; }
.note { font-size: 12px; color: var(--muted); line-height: 1.7; margin: 8px 0 0; }
.err { color: var(--err); font-size: 13px; line-height: 1.7; margin: 0 0 12px; }
.full { width: 100%; margin-top: 8px; }
.sm { font-size: 12px; padding: 6px 12px; margin-top: 10px; }
</style>
