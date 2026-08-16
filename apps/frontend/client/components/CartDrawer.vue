<script setup lang="ts">
import ProductImage from './ProductImage.vue'
import { presentationOf } from '../products'
import { yen } from '../useCart'
import type { Catalog } from '../types'

defineProps<{
  open: boolean
  /** サーバー側カートの写し。SKU -> 数量 */
  entries: [string, number][]
  catalog: Catalog
  total: number
  busy: boolean
  maxPerLine: number
  /** 上限で切り詰められた SKU。黙って数量を変えないために表示する */
  trimmed: string[]
  /** BR-18: カート投入にログインは不要。関門は購入手続きの開始に置く */
  loggedIn: boolean
  /** BR-19: ログインできていても、メール確認前は購入できない */
  emailVerified: boolean
}>()

defineEmits<{
  close: []
  setQty: [sku: string, delta: number]
  remove: [sku: string]
  checkout: []
}>()
</script>

<template>
  <div class="overlay" :class="{ open }" @click="$emit('close')" />

  <aside class="drawer" :class="{ open }" aria-label="カート">
    <div class="drawer-top">
      <h2>カート</h2>
      <button class="icon-btn" aria-label="閉じる" @click="$emit('close')">×</button>
    </div>

    <div class="lines">
      <p v-if="entries.length === 0" class="empty">カートは空です。</p>

      <p v-if="trimmed.length > 0" class="notice">
        1商品あたり {{ maxPerLine }} 点までのため、数量を調整しました。
      </p>

      <div v-for="[sku, qty] in entries" :key="sku" class="line">
        <div class="lt"><ProductImage :sku="sku" :size="80" /></div>
        <div>
          <p class="ln">{{ presentationOf(sku).name }}</p>
          <p class="lp">{{ yen(catalog[sku]?.unit_price ?? 0) }} / 点</p>
          <div class="qty">
            <button aria-label="減らす" @click="$emit('setQty', sku, -1)">−</button>
            <span>{{ qty }}</span>
            <button
              aria-label="増やす"
              :disabled="qty >= maxPerLine"
              @click="$emit('setQty', sku, 1)"
            >+</button>
          </div>
        </div>
        <div class="right">
          <p class="lp strong">{{ yen((catalog[sku]?.unit_price ?? 0) * qty) }}</p>
          <button class="rm" @click="$emit('remove', sku)">削除</button>
        </div>
      </div>
    </div>

    <div v-if="entries.length > 0" class="drawer-foot">
      <div class="total"><span>合計（税込）</span><span class="tv">{{ yen(total) }}</span></div>

      <!--
        BR-18 / BR-19 の関門。**「購入できません」だけにしない。**
        ログインすべきなのか確認すべきなのかで、次にする行動が違う。
        ボタンは押させたうえで理由を出す設計もあるが、
        ここでは押す前に分かるようにしている。
      -->
      <p v-if="!loggedIn" class="gate">
        ご注文にはログインが必要です。カートの内容はそのまま引き継がれます。
      </p>
      <p v-else-if="!emailVerified" class="gate">
        メールアドレスの確認が完了していません。確認後にご注文いただけます。
      </p>

      <button class="btn full" :disabled="busy" @click="$emit('checkout')">
        {{ busy ? '処理中…' : loggedIn ? 'ご注文手続きへ' : 'ログインして購入' }}
      </button>
    </div>
  </aside>
</template>

<style scoped>
.overlay {
  position: fixed; inset: 0; background: rgba(43, 35, 32, .38); z-index: 50;
  opacity: 0; pointer-events: none; transition: opacity .18s;
}
.overlay.open { opacity: 1; pointer-events: auto; }
.drawer {
  position: fixed; top: 0; right: 0; bottom: 0; width: 400px; max-width: 92vw; z-index: 60;
  background: var(--surface); box-shadow: -12px 0 40px rgba(43, 35, 32, .14);
  transform: translateX(100%); transition: transform .22s ease;
  display: flex; flex-direction: column;
}
.drawer.open { transform: translateX(0); }
.notice, .gate {
  font-size: 12px; line-height: 1.6; border-radius: 8px; padding: 8px 10px; margin: 0 0 10px;
  background: var(--bg); color: var(--muted);
}
.gate { border-left: 3px solid var(--warn); }
.qty button:disabled { opacity: .35; cursor: not-allowed; }
.drawer-top {
  display: flex; align-items: center; justify-content: space-between;
  padding: 20px 22px; border-bottom: 1px solid var(--line);
}
.drawer-top h2 { margin: 0; font-size: 17px; }
.icon-btn { background: none; border: none; font-size: 22px; cursor: pointer; color: var(--muted); line-height: 1; padding: 4px; }
.lines { flex: 1; overflow-y: auto; padding: 8px 22px; }
.line {
  display: grid; grid-template-columns: 56px 1fr auto; gap: 12px;
  padding: 16px 0; border-bottom: 1px solid var(--line); align-items: center;
}
.lt { width: 56px; height: 56px; border-radius: 8px; background: #f3ece3; display: grid; place-items: center; }
.ln { font-size: 13px; font-weight: 600; margin: 0 0 4px; }
.lp { font-size: 12px; color: var(--muted); margin: 0; font-variant-numeric: tabular-nums; }
.lp.strong { font-weight: 600; color: var(--ink); }
.right { text-align: right; }
.qty { display: flex; align-items: center; gap: 6px; margin-top: 6px; }
.qty button {
  width: 26px; height: 26px; border: 1px solid var(--line); background: var(--surface);
  border-radius: 6px; cursor: pointer; font-family: inherit; font-size: 14px; line-height: 1; color: var(--ink);
}
.qty button:hover { border-color: var(--accent); }
.qty span { min-width: 22px; text-align: center; font-size: 13px; font-variant-numeric: tabular-nums; }
.rm {
  background: none; border: none; color: var(--muted); font-size: 12px;
  cursor: pointer; text-decoration: underline; padding: 0; font-family: inherit;
}
.drawer-foot { border-top: 1px solid var(--line); padding: 20px 22px; }
.total { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 14px; }
.tv { font-size: 22px; font-weight: 700; font-variant-numeric: tabular-nums; }
.empty { color: var(--muted); font-size: 14px; text-align: center; padding: 56px 0; }
.full { width: 100%; }
</style>
