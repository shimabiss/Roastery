<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import * as api from '../api'
import { yen } from '../useCart'
import { presentationOf } from '../products'
import type { Address, Catalog, ShippingQuote } from '../types'

/**
 * 注文手続き (UC-01 手順3〜7)。
 *
 * 3段階に分けている。**確認画面 (手順6) を独立させているのが要点。**
 *   1. 配送先を選ぶ / 入力する   … 手順3
 *   2. 送料と合計を確認する       … 手順4〜6
 *   3. 確定する                   … 手順7
 *
 * US-04 の3つ目の受入基準「確認画面に表示されていた金額で決済される」は、
 * **確定前に金額が確定していること**を求めている (BR-01)。
 * 配送先を決めないと送料が出ないので、手順3が手順4より前にある。
 */
const props = defineProps<{
  open: boolean
  subtotal: number
  entries: [string, number][]
  catalog: Catalog
  busy: boolean
}>()
const emit = defineEmits<{ close: []; place: [addressId: string] }>()

const step = ref<'address' | 'confirm'>('address')
const addresses = ref<Address[]>([])
const prefectures = ref<string[]>([])
const selectedId = ref('')
const quote = ref<ShippingQuote | null>(null)
const quoteError = ref('')
const adding = ref(false)
const formError = ref('')

const blank = () => ({
  label: '',
  recipient: '',
  postal_code: '',
  prefecture: '東京都',
  city: '',
  address_line: '',
  phone: '',
})
const form = ref(blank())

const selected = computed(() => addresses.value.find((a) => a.id === selectedId.value) ?? null)

watch(
  () => props.open,
  async (open) => {
    if (!open) return
    step.value = 'address'
    quoteError.value = ''
    formError.value = ''
    addresses.value = await api.fetchAddresses()
    prefectures.value = await api.fetchPrefectures()
    // US-10: **既定の配送先が初期表示される**
    selectedId.value = (addresses.value.find((a) => a.is_default) ?? addresses.value[0])?.id ?? ''
    adding.value = addresses.value.length === 0
  },
)

async function saveAddress(): Promise<void> {
  formError.value = ''
  const res = await api.createAddress(form.value)
  if (!res.ok) {
    formError.value = res.detail || '登録できませんでした。入力内容をご確認ください。'
    return
  }
  addresses.value = res.addresses
  selectedId.value = (res.addresses.find((a) => a.is_default) ?? res.addresses[0])?.id ?? ''
  form.value = blank()
  adding.value = false
}

/** UC-01 手順4〜6。**配送先が決まって初めて合計が出せる。** */
async function toConfirm(): Promise<void> {
  if (!selected.value) return
  quoteError.value = ''
  quote.value = await api.fetchQuote(selected.value.prefecture, props.subtotal)
  if (!quote.value) {
    quoteError.value = '送料を計算できませんでした。配送先をご確認ください。'
    return
  }
  step.value = 'confirm'
}
</script>

<template>
  <div v-if="open" class="modal" @click.self="$emit('close')">
    <div class="modal-box">
      <!-- ===== 手順3: 配送先 ===== -->
      <template v-if="step === 'address'">
        <h2>お届け先</h2>

        <div v-if="!adding">
          <label v-for="a in addresses" :key="a.id" class="pick">
            <input v-model="selectedId" type="radio" :value="a.id" />
            <span>
              <strong>{{ a.recipient }}</strong>
              <em v-if="a.is_default" class="tag">既定</em><br />
              〒{{ a.postal_code }} {{ a.prefecture }}{{ a.city }}{{ a.address_line }}<br />
              {{ a.phone }}
            </span>
          </label>
          <button class="btn ghost sm" @click="adding = true">別の住所を追加</button>
        </div>

        <form v-else class="form" @submit.prevent="saveAddress">
          <label>お名前<input v-model="form.recipient" required /></label>
          <label>郵便番号<input v-model="form.postal_code" required placeholder="1000001" /></label>
          <label>
            都道府県
            <select v-model="form.prefecture" required>
              <option v-for="p in prefectures" :key="p" :value="p">{{ p }}</option>
            </select>
          </label>
          <label>市区町村<input v-model="form.city" required /></label>
          <label>番地・建物<input v-model="form.address_line" required /></label>
          <label>電話番号<input v-model="form.phone" required placeholder="0312345678" /></label>
          <p v-if="formError" class="err">{{ formError }}</p>
          <button class="btn full" type="submit">この住所を使う</button>
          <button
            v-if="addresses.length > 0"
            class="btn ghost full"
            type="button"
            @click="adding = false"
          >
            戻る
          </button>
        </form>

        <p v-if="quoteError" class="err">{{ quoteError }}</p>
        <button v-if="!adding" class="btn full" :disabled="!selectedId" @click="toConfirm">
          お支払い内容の確認へ
        </button>
        <button class="btn ghost full" @click="$emit('close')">閉じる</button>
      </template>

      <!-- ===== 手順6: 確認 ===== -->
      <template v-else>
        <h2>ご注文内容の確認</h2>

        <div v-for="[sku, qty] in entries" :key="sku" class="row">
          <span>{{ presentationOf(sku).name }} × {{ qty }}</span>
          <span class="num">{{ yen((catalog[sku]?.unit_price ?? 0) * qty) }}</span>
        </div>

        <div v-if="selected" class="ship">
          <strong>お届け先</strong><br />
          {{ selected.recipient }} 様<br />
          〒{{ selected.postal_code }} {{ selected.prefecture }}{{ selected.city
          }}{{ selected.address_line }}
        </div>

        <dl v-if="quote" class="totals">
          <div><dt>商品計</dt><dd>{{ yen(quote.subtotal) }}</dd></div>
          <div>
            <dt>送料（{{ quote.zone }}）</dt>
            <dd>
              <template v-if="quote.free_shipping_applied">
                <s>{{ yen(quote.base_fee) }}</s> 無料
              </template>
              <template v-else>{{ yen(quote.shipping_fee) }}</template>
            </dd>
          </div>
          <div class="grand"><dt>合計（税込）</dt><dd>{{ yen(quote.total) }}</dd></div>
        </dl>

        <!--
          「あと少しで無料」を伝える。金額だけ出すと、利用者は
          もう少し買えば無料になるかどうかを判断できない
        -->
        <p v-if="quote && !quote.free_shipping_applied" class="hint">
          あと {{ yen(quote.remaining_for_free) }} のお買い上げで送料が無料になります。
        </p>

        <p class="hint">
          お支払いはクレジットカードのみです。カード情報は当サイトを通過しません。
        </p>

        <button class="btn full" :disabled="busy" @click="$emit('place', selectedId)">
          {{ busy ? '処理中…' : 'この内容で注文する' }}
        </button>
        <button class="btn ghost full" :disabled="busy" @click="step = 'address'">
          お届け先を変更する
        </button>
      </template>
    </div>
  </div>
</template>

<style scoped>
.modal {
  position: fixed; inset: 0; z-index: 75; display: grid; place-items: center;
  padding: 24px; background: rgba(43, 35, 32, .42);
}
.modal-box {
  background: var(--surface); border-radius: 16px; padding: 28px; max-width: 460px; width: 100%;
  box-shadow: 0 20px 60px rgba(43, 35, 32, .24); max-height: 88vh; overflow-y: auto;
}
h2 { margin: 0 0 16px; font-size: 20px; }
.pick {
  display: flex; gap: 10px; align-items: flex-start; border: 1px solid var(--line);
  border-radius: 10px; padding: 12px; margin-bottom: 8px; font-size: 13px; line-height: 1.7;
  cursor: pointer;
}
.tag {
  font-style: normal; font-size: 10px; background: var(--bg); border-radius: 4px;
  padding: 1px 6px; margin-left: 6px; color: var(--muted);
}
.form label { display: block; font-size: 12px; color: var(--muted); margin-bottom: 10px; }
.form input, .form select {
  display: block; width: 100%; margin-top: 4px; padding: 8px 10px; font-size: 14px;
  border: 1px solid var(--line); border-radius: 8px; background: var(--surface); color: inherit;
}
.row { display: flex; justify-content: space-between; font-size: 13px; padding: 5px 0; }
.num { font-variant-numeric: tabular-nums; }
.ship {
  background: var(--bg); border-radius: 8px; padding: 12px; margin: 14px 0;
  font-size: 12px; line-height: 1.8;
}
.totals { margin: 14px 0 6px; font-size: 13px; }
.totals div { display: flex; justify-content: space-between; padding: 4px 0; }
.totals dt, .totals dd { margin: 0; }
.totals dd { font-variant-numeric: tabular-nums; }
.totals s { color: var(--muted); margin-right: 4px; }
.totals .grand {
  border-top: 1px solid var(--line); margin-top: 6px; padding-top: 10px;
  font-weight: 700; font-size: 15px;
}
.hint { font-size: 12px; color: var(--muted); line-height: 1.7; margin: 8px 0 0; }
.err { color: var(--err); font-size: 13px; margin: 8px 0; }
.full { width: 100%; margin-top: 10px; }
.sm { font-size: 12px; padding: 6px 12px; }
</style>
