import type { ProductPresentation } from './types'

/**
 * 商品の表示情報。
 * SKU (COFFEE-BEANS-1KG など) はバックエンドの識別子のままにし、
 * 表示名・説明・イラストはフロント側だけで持つ。
 * こうすることで inventory-api の API 契約を変えずに見せ方を変えられる。
 */
export const PRODUCTS: Record<string, ProductPresentation> = {
  'COFFEE-BEANS-1KG': {
    name: 'エチオピア イルガチェフェ 1kg',
    category: 'コーヒー豆',
    copy: '花のような香りと、レモンを思わせる明るい酸。浅めの焙煎で軽やかに仕上げました。',
    note: '中浅煎り / ホールビーン',
    art: `<path d="M24 50c0-14 11-26 26-26s26 12 26 26-11 26-26 26S24 64 24 50Z" fill="#6d4529"/>
          <path d="M50 24c-7 8-7 44 0 52" stroke="#f3ece3" stroke-width="3" fill="none"/>`,
  },
  'MUG-CERAMIC': {
    name: '陶器マグ 320ml',
    category: '器具',
    copy: '厚みのある陶器で温度が落ちにくい。手になじむ、少し大きめのマグです。',
    note: '電子レンジ・食洗機対応',
    art: `<path d="M30 34h34v30a12 12 0 0 1-12 12H42a12 12 0 0 1-12-12V34Z" fill="#fff" stroke="#6d4529" stroke-width="3"/>
          <path d="M64 42h6a8 8 0 0 1 0 16h-6" stroke="#6d4529" stroke-width="3" fill="none"/>
          <path d="M34 44h26v20a8 8 0 0 1-8 8h-10a8 8 0 0 1-8-8V44Z" fill="#8a5a3b" opacity=".75"/>`,
  },
  'DRIP-KETTLE': {
    name: 'ドリップケトル 0.9L',
    category: '器具',
    copy: '細口ノズルで湯量を細かく操れます。一杯目から安定した抽出に。',
    note: 'ステンレス / IH 対応',
    art: `<path d="M32 44h30v22a12 12 0 0 1-12 12H44a12 12 0 0 1-12-12V44Z" fill="#fff" stroke="#6d4529" stroke-width="3"/>
          <path d="M62 50c10 0 16-10 20-18" stroke="#6d4529" stroke-width="3" fill="none" stroke-linecap="round"/>
          <path d="M38 44c0-8 18-8 18 0" stroke="#6d4529" stroke-width="3" fill="none"/>
          <path d="M40 30h14" stroke="#8a5a3b" stroke-width="3" stroke-linecap="round"/>`,
  },
  'FILTER-100P': {
    name: 'ペーパーフィルター 100枚',
    category: '消耗品',
    copy: '目詰まりしにくい無漂白タイプ。円錐ドリッパー用の定番です。',
    note: '円錐形 / 1〜2杯用',
    art: `<path d="M26 34h48L54 78h-8L26 34Z" fill="#f7f1e8" stroke="#6d4529" stroke-width="3"/>
          <path d="M34 34h32L52 66h-4L34 34Z" fill="#e8dcc9"/>
          <path d="M26 34h48" stroke="#6d4529" stroke-width="3"/>`,
  },
}

/** カタログに無い SKU が来ても画面が壊れないようにするためのフォールバック */
export function presentationOf(sku: string): ProductPresentation {
  return (
    PRODUCTS[sku] ?? { name: sku, category: '', copy: '', note: '', art: '' }
  )
}
