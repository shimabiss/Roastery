/** バックエンド (inventory-api) が返すカタログの1件 */
export interface CatalogItem {
  name: string
  category: string
  unit_price: number
  /**
   * BR-13: これは **引当可能在庫** (実在庫 − 引当済数) であり、実在庫ではない。
   * 利用者に見せてよいのはこちらだけ。
   */
  stock: number
  /** 運用画面向けの内訳。公開サイトでは使わない */
  physical?: number
  reserved?: number
}

/** SKU をキーにしたカタログ。/api/catalog のレスポンス形 */
export type Catalog = Record<string, CatalogItem>

/** 表示上の商品情報。SKU はシステムの識別子のままにし、見せ方だけフロントで持つ */
export interface ProductPresentation {
  name: string
  category: string
  copy: string
  note: string
  /** インライン SVG の中身 (外部画像を持たないため) */
  art: string
}

/** カートの1行 */
export interface CartLine {
  sku: string
  qty: number
}

/** 注文明細 (バックエンドが返すもの) */
export interface OrderItem {
  sku: string
  name: string
  unit_price: number
  quantity: number
  subtotal: number
}

/** /api/checkout の成功レスポンス。**注文1件** を表す */
export interface OrderResponse {
  order_id: string
  status: string
  items: OrderItem[]
  subtotal: number
  shipping_fee: number
  total: number
  payment_id?: string
}

/**
 * 在庫不足のときにバックエンドが返す detail。
 * どの SKU が足りなかったかを利用者に伝えるために使う (UC-01 E1)。
 */
export interface InsufficientDetail {
  message: string
  sku: string
  available: number
}

/** チェックアウトの結果。**成功か失敗のどちらか一つ** (BR-03) */
export interface CheckoutOutcome {
  ok: boolean
  status: number
  body: Partial<OrderResponse> & { detail?: unknown; reason?: string; email?: string }
}

// ---------------------------------------------------------------------------
// P2: 会員とサーバー側カート
// ---------------------------------------------------------------------------

/** ログイン状態。**未確認の会員という中間状態がある** (BR-19) */
export interface Me {
  logged_in: boolean
  member_id?: string
  email?: string
  /** false のときはログインできているが購入だけができない */
  email_verified?: boolean
}

/** サーバー側カートの状態。/api/cart のレスポンス形 */
export interface CartState {
  /** SKU -> 数量 */
  lines: Record<string, number>
  /** BR-22 / FR-311: 1明細あたりの数量上限 */
  max_quantity_per_line: number
  logged_in: boolean
  email_verified: boolean
  /** 上限で切り詰めた SKU。**伝えないと「勝手に数量が変わった」ことになる** */
  trimmed: string[]
}

/** 購入手続きが止められた理由。**「購入できません」だけでは次の行動が分からない** */
export type CheckoutBlockedReason = 'not_logged_in' | 'email_not_verified' | 'empty_cart'

// ---------------------------------------------------------------------------
// P3: 配送先と送料
// ---------------------------------------------------------------------------

/** 住所帳の1件 (FR-106) */
export interface Address {
  id: string
  label: string
  recipient: string
  postal_code: string
  prefecture: string
  city: string
  address_line: string
  phone: string
  is_default: boolean
}

/**
 * 送料の見積もり (FR-404 / UC-01 手順4)。
 * **なぜ 0 円なのかまで返す。** 金額だけだと「無料の地域なのか閾値超えなのか」
 * が分からず、利用者は「あと少し買えば無料」を判断できない。
 */
export interface ShippingQuote {
  zone: string
  base_fee: number
  shipping_fee: number
  free_shipping_applied: boolean
  free_shipping_threshold: number
  remaining_for_free: number
  subtotal: number
  total: number
}

/**
 * 注文履歴の1件 (FR-406)。
 *
 * ``can_cancel`` は **サーバーが判定して返す** (states.py)。
 * 画面側で状態名から判定すると、状態が増えたときに必ず取り残される。
 */
export interface OrderSummary {
  id: string
  status: string
  status_label: string
  can_cancel: boolean
  items: OrderItem[]
  subtotal: number
  shipping_fee: number
  total: number
  tracking_no: string | null
  created_at: string
}
