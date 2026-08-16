<script setup lang="ts">
import { computed } from 'vue'
import ProductImage from './ProductImage.vue'
import { presentationOf } from '../products'
import { yen } from '../useCart'
import type { CatalogItem } from '../types'

const props = withDefaults(
  defineProps<{ sku: string; item: CatalogItem; eager?: boolean }>(),
  { eager: false },
)
defineEmits<{ add: [sku: string] }>()

const p = computed(() => presentationOf(props.sku))
const soldOut = computed(() => props.item.stock === 0)

/** 在庫は生の数値ではなく EC らしい表現にする。残りわずかのときだけ点数を出す */
const stockLabel = computed(() => {
  if (props.item.stock === 0) return { text: '売り切れ', cls: 'out' }
  if (props.item.stock <= 5) return { text: `残り ${props.item.stock} 点`, cls: 'low' }
  return { text: '在庫あり', cls: 'in' }
})
</script>

<template>
  <article class="card">
    <div class="thumb">
      <ProductImage :sku="sku" :size="400" :lazy="!eager" />
    </div>
    <div class="card-body">
      <span class="cat">{{ p.category }}</span>
      <h3>{{ p.name }}</h3>
      <p class="copy">{{ p.copy }}</p>
      <p class="note">{{ p.note }}</p>
      <div class="price-row">
        <span class="price">{{ yen(item.unit_price) }}<small>税込</small></span>
        <span class="stock" :class="stockLabel.cls">{{ stockLabel.text }}</span>
      </div>
      <button class="add" :disabled="soldOut" @click="$emit('add', sku)">
        {{ soldOut ? '売り切れ' : 'カートに入れる' }}
      </button>
    </div>
  </article>
</template>

<style scoped>
.card {
  background: var(--surface); border: 1px solid var(--line); border-radius: 14px;
  overflow: hidden; display: flex; flex-direction: column; box-shadow: var(--shadow);
}
.thumb { aspect-ratio: 1 / 1; display: grid; place-items: center; background: #f3ece3; }
.card-body { padding: 16px 16px 18px; display: flex; flex-direction: column; flex: 1; gap: 8px; }
.cat { font-size: 11px; letter-spacing: .1em; color: var(--accent); font-weight: 700; }
h3 { font-size: 15px; margin: 0; line-height: 1.5; }
.copy { font-size: 13px; color: var(--muted); margin: 0; flex: 1; }
.note { font-size: 12px; color: var(--muted); margin: 0; }
.price-row { display: flex; align-items: center; justify-content: space-between; margin-top: 6px; }
.price { font-size: 18px; font-weight: 700; font-variant-numeric: tabular-nums; }
.price small { font-size: 11px; font-weight: 400; color: var(--muted); margin-left: 2px; }
.stock { font-size: 12px; }
.stock.in { color: var(--ok); }
.stock.low { color: var(--warn); font-weight: 600; }
.stock.out { color: var(--err); }
.add {
  width: 100%; margin-top: 8px; background: var(--accent); color: #fff; border: none;
  border-radius: 8px; padding: 11px; font-size: 14px; font-weight: 600;
  cursor: pointer; font-family: inherit;
}
.add:hover { background: var(--accent-d); }
.add:disabled { background: #d8cec3; cursor: not-allowed; }
</style>
