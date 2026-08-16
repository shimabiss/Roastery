import { computed, reactive, ref } from 'vue'
import type { Catalog, CartState } from './types'
import * as api from './api'

/**
 * カートの状態。
 *
 * **P2 でサーバー側に移した** (FR-310)。
 * 以前はブラウザ内の reactive 配列だけで、リロードで消え、
 * 別のブラウザでは引き継げなかった。
 *
 * ここに残っているのは「サーバーの状態の写し」であり、
 * 更新は必ずサーバーの応答で上書きする。
 * **ローカルで先に更新してからサーバーに送ると、上限クランプ (BR-22) の結果と
 * 画面がずれる。** 楽観更新をするなら、ずれたときの戻し方まで決める必要がある。
 */
const lines = reactive<Record<string, number>>({})
const maxPerLine = ref(10)
/** 直近の操作で上限に切り詰められた SKU。US-14 の4つ目の受入基準 */
const trimmed = ref<string[]>([])

function absorb(state: CartState): void {
  for (const key of Object.keys(lines)) delete lines[key]
  Object.assign(lines, state.lines ?? {})
  if (state.max_quantity_per_line) maxPerLine.value = state.max_quantity_per_line
  trimmed.value = state.trimmed ?? []
}

export function useCart(catalog: () => Catalog) {
  const entries = computed(() => Object.entries(lines))
  const count = computed(() => entries.value.reduce((n, [, qty]) => n + qty, 0))
  const total = computed(() =>
    entries.value.reduce((sum, [sku, qty]) => sum + (catalog()[sku]?.unit_price ?? 0) * qty, 0),
  )

  async function load(): Promise<void> {
    absorb(await api.fetchCart())
  }

  async function add(sku: string): Promise<void> {
    absorb(await api.addToCart(sku, 1))
  }

  async function setQty(sku: string, delta: number): Promise<void> {
    const next = (lines[sku] ?? 0) + delta
    absorb(next <= 0 ? await api.removeFromCart(sku) : await api.setCartQuantity(sku, next))
  }

  async function remove(sku: string): Promise<void> {
    absorb(await api.removeFromCart(sku))
  }

  function clearLocal(): void {
    for (const key of Object.keys(lines)) delete lines[key]
  }

  return { lines, entries, count, total, maxPerLine, trimmed, load, add, setQty, remove, clearLocal, absorb }
}

/** 金額表示。円は小数を扱わないので toLocaleString で十分 */
export function yen(n: number): string {
  return '¥' + n.toLocaleString('ja-JP')
}
