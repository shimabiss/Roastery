import type {
  Catalog, OrderResponse, CheckoutOutcome, CartState, Me, Address, ShippingQuote,
} from './types'

/** Cookie を送るために credentials を付ける。BFF が Cookie を読む唯一の相手 */
const opts: RequestInit = { credentials: 'same-origin' }

async function json<T>(res: Response): Promise<T> {
  return (await res.json().catch(() => ({}))) as T
}

/** 在庫と価格を取得する。frontend の BFF 経由で inventory-api を呼ぶ */
export async function fetchCatalog(): Promise<Catalog> {
  const res = await fetch('/api/catalog', opts)
  if (!res.ok) throw new Error(`catalog ${res.status}`)
  return json<Catalog>(res)
}

// ---------------------------------------------------------------------------
// カート (FR-306 / FR-310 / FR-311)
//
// **カートはサーバー側にある。** ブラウザ内の状態ではないので、
// ログインすれば別のブラウザでも同じカートが見える。
// 未ログインでも Cookie の識別子で 30 日保持される (BR-23)。
// ---------------------------------------------------------------------------
export async function fetchCart(): Promise<CartState> {
  return json<CartState>(await fetch('/api/cart', opts))
}

export async function addToCart(sku: string, quantity = 1): Promise<CartState> {
  return json<CartState>(
    await fetch('/api/cart/items', {
      ...opts,
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ sku, quantity }),
    }),
  )
}

export async function setCartQuantity(sku: string, quantity: number): Promise<CartState> {
  return json<CartState>(
    await fetch(`/api/cart/items/${encodeURIComponent(sku)}`, {
      ...opts,
      method: 'PUT',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ quantity }),
    }),
  )
}

export async function removeFromCart(sku: string): Promise<CartState> {
  return json<CartState>(
    await fetch(`/api/cart/items/${encodeURIComponent(sku)}`, { ...opts, method: 'DELETE' }),
  )
}

// ---------------------------------------------------------------------------
// 会員
// ---------------------------------------------------------------------------
export async function fetchMe(): Promise<Me> {
  return json<Me>(await fetch('/api/me', opts))
}

export interface AuthOutcome {
  ok: boolean
  status: number
  detail?: string
  body: Record<string, unknown>
}

async function post(url: string, payload: unknown): Promise<AuthOutcome> {
  try {
    const res = await fetch(url, {
      ...opts,
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(payload),
    })
    const body = await json<Record<string, unknown>>(res)
    return { ok: res.ok, status: res.status, detail: body.detail as string | undefined, body }
  } catch {
    return { ok: false, status: 0, body: {} }
  }
}

export const register = (email: string, password: string) =>
  post('/api/members', { email, password })

export const login = (email: string, password: string) =>
  post('/api/sessions', { email, password })

export const logout = () => post('/api/sessions/logout', {})

export const verifyEmail = (token: string) => post('/api/members/verify', { token })

/** 確認メールを読むための窓口。実サービスが無いので受信箱を覗く */
export async function fetchInbox(to: string): Promise<Array<{ subject: string; body: string }>> {
  return json(await fetch(`/api/mail/inbox?to=${encodeURIComponent(to)}`, opts))
}

// ---------------------------------------------------------------------------
// 注文
// ---------------------------------------------------------------------------
/**
 * カートの中身を **1回の注文** として確定する (FR-408 / BR-04)。
 *
 * 明細は送らない。**サーバー側のカートを正とする。**
 * 画面から数量を送ると、上限 (BR-22) や価格を回避できてしまう。
 *
 * BR-03 により結果は「全部成立」か「1つも成立しない」のどちらか。
 * 401 = 未ログイン (BR-18)、403 = メール未確認 (BR-19) を分けて返す。
 */
export async function checkout(addressId: string): Promise<CheckoutOutcome> {
  try {
    const res = await fetch('/api/checkout', {
      ...opts,
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      // 住所そのものではなく **住所帳の ID** を送る。
      // 住所を丸ごと送れると、他人の住所帳に無い任意の値を注文に載せられる
      body: JSON.stringify({ address_id: addressId, card_token: 'tok_test_visa' }),
    })
    const body = await json<Partial<OrderResponse> & { detail?: unknown; reason?: string }>(res)
    return { ok: res.ok, status: res.status, body }
  } catch {
    return { ok: false, status: 0, body: {} }
  }
}

// ---------------------------------------------------------------------------
// 住所帳と送料 (P3)
// ---------------------------------------------------------------------------
export async function fetchAddresses(): Promise<Address[]> {
  const res = await fetch('/api/addresses', opts)
  if (!res.ok) return []
  return json<Address[]>(res)
}

export async function createAddress(
  input: Omit<Address, 'id' | 'is_default'> & { is_default?: boolean },
): Promise<{ ok: boolean; detail?: string; addresses: Address[] }> {
  const res = await fetch('/api/addresses', {
    ...opts,
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(input),
  })
  const body = await json<{ detail?: string; addresses?: Address[] }>(res)
  return { ok: res.ok, detail: body.detail, addresses: body.addresses ?? [] }
}

export async function fetchPrefectures(): Promise<string[]> {
  const res = await fetch('/api/shipping/prefectures', opts)
  if (!res.ok) return []
  return (await json<{ prefectures: string[] }>(res)).prefectures ?? []
}

/**
 * UC-01 手順4。配送先を入力した時点で送料と合計を提示する。
 * **確定する前に金額が確定していること** が US-04 の要件。
 */
export async function fetchQuote(prefecture: string, subtotal: number): Promise<ShippingQuote | null> {
  const q = new URLSearchParams({ prefecture, subtotal: String(subtotal) })
  const res = await fetch(`/api/shipping/quote?${q}`, opts)
  if (!res.ok) return null
  return json<ShippingQuote>(res)
}
