/**
 * frontend : 画面と BFF を兼ねるサービス。
 *
 * 画面は2つに分かれている。
 *   /      公開サイト … Vue 3 + Vite。client/ をビルドした dist/ を静的配信する
 *   /ops   運用画面   … server/ops.html を素の HTML のまま返す
 *
 * BFF の責務 (P2 で増えた)
 * ------------------------
 * Cookie を扱うのは **ここだけ** にしてある。バックエンドの各サービスは
 * ヘッダー (x-session-id / x-cart-id) しか見ない。
 * こうしておくと、サービス側が Cookie の仕様 (SameSite, Secure, パス) を
 * 知らなくて済み、**認証方式を変えても影響が BFF に閉じる。**
 *
 * 学習ポイント:
 *   - Node.js は `--require @opentelemetry/auto-instrumentations-node/register`
 *     だけで SDK 初期化 + 自動計装が済む。Python と同じ「ゼロコード計装」。
 *   - 言語が違っても trace は1本につながる (W3C Trace Context)。
 *   - 認証のホップが1つ増えたことで、waterfall に「毎回必ず出る短い span」が現れる。
 *     これは実務で必ず議論になる形 (キャッシュするか、しないか)。
 */

const express = require('express');
const path = require('path');
const crypto = require('crypto');
const { trace, metrics, SpanStatusCode } = require('@opentelemetry/api');

const tracer = trace.getTracer('frontend');
const meter = metrics.getMeter('frontend');

const checkoutAttempts = meter.createCounter('frontend.checkout.attempts', {
  description: '画面からのチェックアウト試行回数',
});
const checkoutBlocked = meter.createCounter('frontend.checkout.blocked', {
  description: 'ログイン未了・メール未確認で購入手続きを止めた回数',
});

const ORDER_API = process.env.ORDER_API_URL || 'http://order-api:8000';
const PAYMENT_API = process.env.PAYMENT_API_URL || 'http://payment-api:8000';
const INVENTORY_API = process.env.INVENTORY_API_URL || 'http://inventory-api:8000';
const EXTERNAL_STUB = process.env.EXTERNAL_STUB_URL || 'http://external-stub:8000';
const MEMBER_API = process.env.MEMBER_API_URL || 'http://member-api:8000';
const PORT = process.env.PORT || 3000;

// BR-23 / FR-310: 未ログインカートは 30 日。Cookie の寿命も同じ値に揃える。
// ずれていると「Cookie は生きているのにカートが空」「カートはあるが辿り着けない」
// のどちらかが起きる。
const CART_COOKIE = 'rst_cart';
const SESSION_COOKIE = 'rst_session';
const CART_MAX_AGE_MS = 30 * 24 * 60 * 60 * 1000;
const SESSION_MAX_AGE_MS = 7 * 24 * 60 * 60 * 1000;

const app = express();
app.use(express.json());

// --- Cookie の読み書き (依存を増やさないための最小実装) --------------------
function readCookies(req) {
  const out = {};
  const raw = req.headers.cookie;
  if (!raw) return out;
  for (const part of raw.split(';')) {
    const i = part.indexOf('=');
    if (i < 0) continue;
    out[part.slice(0, i).trim()] = decodeURIComponent(part.slice(i + 1).trim());
  }
  return out;
}

function setCookie(res, name, value, maxAgeMs) {
  // HttpOnly: JavaScript から読めない = XSS でセッションを盗まれにくくする
  // SameSite=Lax: 他サイトからの POST に Cookie を付けない (CSRF の緩和)
  // 本番では Secure を必ず付ける。ローカルは http なのでここでは付けない
  const attrs = [
    `${name}=${encodeURIComponent(value)}`,
    'Path=/',
    'HttpOnly',
    'SameSite=Lax',
    `Max-Age=${Math.floor(maxAgeMs / 1000)}`,
  ];
  res.append('Set-Cookie', attrs.join('; '));
}

function clearCookie(res, name) {
  res.append('Set-Cookie', `${name}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0`);
}

/**
 * 未ログインカートの識別子 (FR-310)。
 * 無ければ発行する。**カート投入にログインは要らない** (BR-18) ので、
 * ここで会員かどうかを問わない。
 */
function ensureCartId(req, res) {
  const cookies = readCookies(req);
  let id = cookies[CART_COOKIE];
  if (!id) {
    id = crypto.randomUUID();
    setCookie(res, CART_COOKIE, id, CART_MAX_AGE_MS);
  }
  return id;
}

function authHeaders(req, res) {
  const cookies = readCookies(req);
  const headers = { 'content-type': 'application/json', 'x-cart-id': ensureCartId(req, res) };
  if (cookies[SESSION_COOKIE]) headers['x-session-id'] = cookies[SESSION_COOKIE];
  return headers;
}

app.use(express.static(path.join(__dirname, '..', 'dist')));

// ---------------------------------------------------------------------------
// 自動計装が付ける HTTP サーバー span の名前は "POST" だけになって読みづらいので、
// ここでルート名に付け替える。App Insights の「要求」名もこれになる。
//   -> 自動計装の出力は updateName() で後から調整できる、という実演ポイント。
// ---------------------------------------------------------------------------
app.use((req, _res, next) => {
  const active = trace.getActiveSpan();
  if (active) active.updateName(`${req.method} ${req.path}`);
  next();
});

app.get('/healthz', (_req, res) => res.json({ status: 'ok' }));

// 運用画面。公開サイトとは配信元のディレクトリが違う。
app.get('/ops', (_req, res) => {
  res.sendFile(path.join(__dirname, 'ops.html'));
});

// 確認メールのリンク先。SPA に流し込むだけ
app.get('/verify', (_req, res) => {
  res.sendFile(path.join(__dirname, '..', 'dist', 'index.html'));
});

// ===========================================================================
// カタログ
// ===========================================================================
app.get('/api/catalog', async (req, res) => {
  try {
    // FR-1102: 非公開の商品は公開サイトに出さない。
    // 運用画面だけが all=1 を付ける
    const all = req.query.all === '1' ? '?include_unpublished=true' : '';
    const r = await fetch(`${INVENTORY_API}/inventory${all}`);
    res.json(await r.json());
  } catch (err) {
    res.status(502).json({ error: String(err) });
  }
});

// 商品の価格変更・公開切替 (FR-1101 / FR-1102)
app.put('/api/ops/products/:sku', async (req, res) => {
  const r = await fetch(`${INVENTORY_API}/inventory/${encodeURIComponent(req.params.sku)}/product`, {
    method: 'PUT',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(req.body || {}),
  });
  res.status(r.status).json(await r.json());
});

// ===========================================================================
// 会員 (FR-101 / FR-102 / FR-103 / FR-112)
// ===========================================================================
app.post('/api/members', async (req, res) => {
  const r = await fetch(`${MEMBER_API}/members`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(req.body || {}),
  });
  res.status(r.status).json(await r.json());
});

app.post('/api/members/verify', async (req, res) => {
  const token = encodeURIComponent(String(req.body?.token || ''));
  const r = await fetch(`${MEMBER_API}/members/verify?token=${token}`, { method: 'POST' });
  res.status(r.status).json(await r.json());
});

app.post('/api/sessions', async (req, res) => {
  // ログイン時に未ログインカートの識別子を渡す。
  // member-api 側でマージされる (FR-306 / BR-20)
  //
  // **ここでは ensureCartId を使わない。** 未ログインカートが無い利用者に
  // 新しい Cookie を発行した直後、下で同じ Cookie を削除することになり、
  // 1つの応答に set と clear の Set-Cookie が両方載る。
  // 既にあるものだけを読み、無ければマージ対象なしとして扱う。
  const cartId = readCookies(req)[CART_COOKIE] || null;
  const r = await fetch(`${MEMBER_API}/sessions`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ ...(req.body || {}), cart_id: cartId }),
  });
  const body = await r.json();
  if (r.ok && body.session_id) {
    setCookie(res, SESSION_COOKIE, body.session_id, SESSION_MAX_AGE_MS);
    // 統合済みなので未ログインカートの Cookie は捨てる (あった場合だけ)
    if (cartId) clearCookie(res, CART_COOKIE);
    delete body.session_id; // セッション ID を画面側に渡さない (HttpOnly の意味が消えるため)
  }
  res.status(r.status).json(body);
});

app.post('/api/sessions/logout', async (req, res) => {
  const cookies = readCookies(req);
  if (cookies[SESSION_COOKIE]) {
    await fetch(`${MEMBER_API}/sessions/${encodeURIComponent(cookies[SESSION_COOKIE])}`, {
      method: 'DELETE',
    }).catch(() => null);
  }
  clearCookie(res, SESSION_COOKIE);
  res.json({ status: 'logged_out' });
});

app.get('/api/me', async (req, res) => {
  const cookies = readCookies(req);
  if (!cookies[SESSION_COOKIE]) return res.json({ logged_in: false });
  const r = await fetch(`${MEMBER_API}/sessions/current`, {
    headers: { 'x-session-id': cookies[SESSION_COOKIE] },
  });
  if (!r.ok) {
    // 期限切れセッション。Cookie を消して未ログイン扱いに戻す (FR-112)
    clearCookie(res, SESSION_COOKIE);
    return res.json({ logged_in: false });
  }
  res.json({ logged_in: true, ...(await r.json()) });
});

// 確認メールの中身を見るための窓口 (実サービスが無くても導線をデモできる)
app.get('/api/mail/inbox', async (req, res) => {
  const q = req.query.to ? `?to=${encodeURIComponent(String(req.query.to))}` : '';
  const r = await fetch(`${EXTERNAL_STUB}/mail/inbox${q}`);
  res.status(r.status).json(await r.json());
});

// ===========================================================================
// カート (FR-306 / FR-310 / FR-311)
// サーバー側で持つようになった。ブラウザを変えても、ログインすれば同じカートになる。
// ===========================================================================
app.get('/api/cart', async (req, res) => {
  const r = await fetch(`${MEMBER_API}/carts`, { headers: authHeaders(req, res) });
  res.status(r.status).json(await r.json());
});

app.post('/api/cart/items', async (req, res) => {
  const r = await fetch(`${MEMBER_API}/carts/items`, {
    method: 'POST',
    headers: authHeaders(req, res),
    body: JSON.stringify(req.body || {}),
  });
  res.status(r.status).json(await r.json());
});

app.put('/api/cart/items/:sku', async (req, res) => {
  const r = await fetch(`${MEMBER_API}/carts/items/${encodeURIComponent(req.params.sku)}`, {
    method: 'PUT',
    headers: authHeaders(req, res),
    body: JSON.stringify({ sku: req.params.sku, ...(req.body || {}) }),
  });
  res.status(r.status).json(await r.json());
});

app.delete('/api/cart/items/:sku', async (req, res) => {
  const r = await fetch(`${MEMBER_API}/carts/items/${encodeURIComponent(req.params.sku)}`, {
    method: 'DELETE',
    headers: authHeaders(req, res),
  });
  res.status(r.status).json(await r.json());
});

// ===========================================================================
// 住所帳 (FR-106 / FR-701)
// ===========================================================================
async function proxyAddresses(req, res, method, suffix = '', body) {
  const r = await fetch(`${MEMBER_API}/addresses${suffix}`, {
    method,
    headers: authHeaders(req, res),
    ...(body ? { body: JSON.stringify(body) } : {}),
  });
  res.status(r.status).json(await r.json());
}

app.get('/api/addresses', (req, res) => proxyAddresses(req, res, 'GET'));
app.post('/api/addresses', (req, res) => proxyAddresses(req, res, 'POST', '', req.body || {}));
app.put('/api/addresses/:id/default', (req, res) =>
  proxyAddresses(req, res, 'PUT', `/${encodeURIComponent(req.params.id)}/default`)
);
app.delete('/api/addresses/:id', (req, res) =>
  proxyAddresses(req, res, 'DELETE', `/${encodeURIComponent(req.params.id)}`)
);

// ===========================================================================
// 送料の見積もり (FR-404 / UC-01 手順4)
//
// **注文を確定する前に金額が確定していること**が US-04 の要件。
// 「確認画面に表示されていた金額で決済される」を守るには、
// 確定前に同じ計算式を通しておく必要がある。
// ===========================================================================
app.get('/api/shipping/quote', async (req, res) => {
  const q = new URLSearchParams({
    prefecture: String(req.query.prefecture || ''),
    subtotal: String(req.query.subtotal || 0),
  });
  const r = await fetch(`${ORDER_API}/shipping/quote?${q}`);
  res.status(r.status).json(await r.json());
});

app.get('/api/shipping/prefectures', async (_req, res) => {
  const r = await fetch(`${ORDER_API}/shipping/prefectures`);
  res.status(r.status).json(await r.json());
});

// ===========================================================================
// 購入手続き (UC-01)
// ===========================================================================
/**
 * FR-408 / BR-04: カートの中身を **1つの注文** として送る。
 *
 * P2 で変わった点が2つある。
 *
 * 1. **明細をクライアントから受け取らない。** サーバー側のカートを読む。
 *    画面から送られてきた数量をそのまま信じると、価格や上限を回避できてしまう。
 * 2. **BR-18 / BR-19 の関門をここに置く。** ログイン済みかつメール確認済みでなければ
 *    注文に進ませない。カート投入までは摩擦をゼロにし、購入の直前だけ関門を置く、
 *    という組み合わせ。
 */
app.post('/api/checkout', async (req, res) => {
  await tracer.startActiveSpan('frontend.checkout', async (span) => {
    try {
      const headers = authHeaders(req, res);

      // --- 関門 (BR-18 / BR-19) -------------------------------------------
      const gate = await fetch(`${MEMBER_API}/members/can-checkout`, { headers }).then((r) =>
        r.json()
      );
      span.setAttribute('checkout.allowed', !!gate.allowed);
      if (!gate.allowed) {
        span.setAttribute('checkout.blocked_reason', gate.reason);
        checkoutBlocked.add(1, { reason: gate.reason });
        span.setStatus({ code: SpanStatusCode.OK }); // 業務上の分岐であってエラーではない
        // 401 と 403 を分ける。**「購入できません」だけでは次の行動が分からない**
        return res
          .status(gate.reason === 'not_logged_in' ? 401 : 403)
          .json({ reason: gate.reason, email: gate.email });
      }

      // --- カートの中身を読む ---------------------------------------------
      const cart = await fetch(`${MEMBER_API}/carts`, { headers }).then((r) => r.json());
      const lines = Object.entries(cart.lines || {}).map(([sku, quantity]) => ({ sku, quantity }));
      if (lines.length === 0) {
        return res.status(400).json({ reason: 'empty_cart' });
      }
      span.setAttribute('order.line_count', lines.length);
      span.setAttribute('order.skus', lines.map((l) => l.sku).join(','));
      span.setAttribute('enduser.id', gate.member_id);
      checkoutAttempts.add(1, { line_count: String(lines.length) });

      // --- 配送先を解決する (FR-701) --------------------------------------
      // 住所そのものではなく **住所帳の ID** を受け取る。
      // 画面から住所を丸ごと受け取ると、他人の住所帳に無い任意の値を
      // 注文に載せられてしまう。
      let shipTo = null;
      if (req.body?.address_id) {
        const a = await fetch(
          `${MEMBER_API}/addresses/${encodeURIComponent(req.body.address_id)}`,
          { headers: authHeaders(req, res) }
        );
        if (!a.ok) return res.status(400).json({ reason: 'unknown_address' });
        const addr = await a.json();
        shipTo = {
          recipient: addr.recipient,
          postal_code: addr.postal_code,
          prefecture: addr.prefecture,
          city: addr.city,
          address_line: addr.address_line,
          phone: addr.phone,
        };
        span.setAttribute('shipping.prefecture', addr.prefecture);
      }

      // --- 注文する -------------------------------------------------------
      const r = await fetch(`${ORDER_API}/orders`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({
          lines,
          member_id: gate.member_id,
          ship_to: shipTo,
          // FR-501 / BR-16: カード情報は自システムを通過させない。
          // 画面から来るのは決済代行が発行したトークンだけ
          card_token: req.body?.card_token || 'tok_test_visa',
          member_email: gate.email,
        }),
      });
      const out = await r.json();
      span.setAttribute('http.response.status_code', r.status);

      if (!r.ok) {
        const reason = typeof out.detail === 'object' ? out.detail.message : out.detail;
        span.setStatus({ code: SpanStatusCode.ERROR, message: reason || 'checkout failed' });
        return res.status(r.status).json(out);
      }

      // 注文が成立したのでカートを空にする。
      // **失敗したときは空にしない** (UC-01 E2 9c: カートの内容は保持する)
      await fetch(`${MEMBER_API}/carts`, { method: 'DELETE', headers }).catch(() => null);
      span.setStatus({ code: SpanStatusCode.OK });
      res.json(out);
    } catch (err) {
      span.recordException(err);
      span.setStatus({ code: SpanStatusCode.ERROR, message: String(err) });
      res.status(502).json({ error: String(err) });
    } finally {
      span.end();
    }
  });
});

app.get('/api/orders', async (req, res) => {
  try {
    const cookies = readCookies(req);
    let url = `${ORDER_API}/orders`;
    // 運用画面 (/ops) は全件、公開サイトは自分の注文だけ
    if (req.query.mine === '1' && cookies[SESSION_COOKIE]) {
      const me = await fetch(`${MEMBER_API}/sessions/current`, {
        headers: { 'x-session-id': cookies[SESSION_COOKIE] },
      });
      if (!me.ok) return res.json([]);
      const { member_id } = await me.json();
      url += `?member_id=${encodeURIComponent(member_id)}`;
    }
    const r = await fetch(url);
    res.json(await r.json());
  } catch (err) {
    res.status(502).json({ error: String(err) });
  }
});

// ===========================================================================
// 出荷とキャンセル (UC-06 / UC-02)
// ===========================================================================

/** 運用担当者の操作。現状 /ops に認証が無いため、これらも無認証で通る。
 *  **docs/security-checklist.md に「外部公開しない」と書いてあるのはこのため。**
 *  ACA では ingress を絞り、本来は運用者の認証を入れる。 */
async function opsAction(req, res, action, body) {
  const r = await fetch(`${ORDER_API}/orders/${encodeURIComponent(req.params.id)}/${action}`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body ?? { actor: 'ops' }),
  });
  res.status(r.status).json(await r.json());
}

/**
 * 運用画面からの注文投入 (デモ・負荷試験用)。
 *
 * **公開サイトの /api/checkout は会員必須の関門を通る (BR-18 / BR-19)。**
 * 運用画面にはセッションが無いので、そのままでは 401 になる。
 * ここは関門を通さずに order-api を直接叩く経路であり、
 * **だからこそ /ops を外部公開してはならない** (docs/security-checklist.md)。
 * 「デモのために関門を迂回する口を作った」ことを、名前とコメントで明示しておく。
 */
app.post('/api/ops/orders', async (req, res) => {
  const lines = Array.isArray(req.body?.lines) && req.body.lines.length
    ? req.body.lines
    : [{ sku: req.body?.sku || 'COFFEE-BEANS-1KG', quantity: req.body?.quantity || 1 }];
  const r = await fetch(`${ORDER_API}/orders`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ lines, member_id: 'ops-console', customer: 'ops-console' }),
  });
  res.status(r.status).json(await r.json());
});

app.post('/api/ops/orders/:id/prepare', (req, res) => opsAction(req, res, 'prepare'));
app.post('/api/ops/orders/:id/ship', (req, res) =>
  opsAction(req, res, 'ship', { actor: 'ops', tracking_no: req.body?.tracking_no ?? null })
);
app.post('/api/ops/orders/:id/deliver', (req, res) => opsAction(req, res, 'deliver'));

app.put('/api/ops/orders/:id/tracking', async (req, res) => {
  const r = await fetch(`${ORDER_API}/orders/${encodeURIComponent(req.params.id)}/tracking`, {
    method: 'PUT',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ tracking_no: req.body?.tracking_no || '' }),
  });
  res.status(r.status).json(await r.json());
});

/**
 * 会員によるキャンセル (UC-02 / FR-407)。
 *
 * **自分の注文かどうかを必ず確かめる。** ID を知っているだけで
 * 他人の注文を取り消せる、が最も起きやすい認可の穴。
 */
app.post('/api/orders/:id/cancel', async (req, res) => {
  const cookies = readCookies(req);
  if (!cookies[SESSION_COOKIE]) return res.status(401).json({ reason: 'not_logged_in' });

  const me = await fetch(`${MEMBER_API}/sessions/current`, {
    headers: { 'x-session-id': cookies[SESSION_COOKIE] },
  });
  if (!me.ok) return res.status(401).json({ reason: 'not_logged_in' });
  const { member_id } = await me.json();

  const order = await fetch(`${ORDER_API}/orders/${encodeURIComponent(req.params.id)}`);
  if (!order.ok) return res.status(404).json({ reason: 'unknown_order' });
  const found = await order.json();
  if (found.member_id !== member_id) {
    // 「他人の注文です」と返すと、その注文番号が存在することが分かってしまう。
    // 存在しないのと同じ応答にする
    return res.status(404).json({ reason: 'unknown_order' });
  }

  const r = await fetch(`${ORDER_API}/orders/${encodeURIComponent(req.params.id)}/cancel`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ actor: `member:${member_id}` }),
  });
  res.status(r.status).json(await r.json());
});

app.get('/api/orders/:id', async (req, res) => {
  const r = await fetch(`${ORDER_API}/orders/${encodeURIComponent(req.params.id)}`);
  res.status(r.status).json(await r.json());
});

// ===========================================================================
// 障害注入 (運用画面から叩くための薄いプロキシ)
// ===========================================================================
app.get('/api/chaos/payment/status', async (_req, res) => {
  try {
    const r = await fetch(`${EXTERNAL_STUB}/admin/chaos`);
    res.status(r.status).json(await r.json());
  } catch (err) {
    res.status(502).json({ error: String(err) });
  }
});

// no_response_rate が最も重要な設定。
// エラーを返すのと応答が返らないのは **別の異常系** であり、
// 前者しか再現できないとテストが片肺になる (UC-01 E3 / UC-06 E2)。
app.post('/api/chaos/payment', async (req, res) => {
  const { latency_ms = 0, error_rate = 0, no_response_rate = 0 } = req.body || {};
  const q = new URLSearchParams({
    latency_ms: String(latency_ms),
    error_rate: String(error_rate),
    no_response_rate: String(no_response_rate),
  });
  const r = await fetch(`${EXTERNAL_STUB}/admin/chaos?${q}`, { method: 'POST' });
  res.status(r.status).json(await r.json());
});

app.post('/api/chaos/inventory', async (req, res) => {
  const { slow_ms = 0 } = req.body || {};
  const r = await fetch(`${INVENTORY_API}/admin/chaos?slow_ms=${slow_ms}`, { method: 'POST' });
  res.status(r.status).json(await r.json());
});

app.post('/api/reset', async (_req, res) => {
  const [inv] = await Promise.all([
    fetch(`${INVENTORY_API}/admin/reset`, { method: 'POST' }).then((r) => r.json()),
    fetch(`${EXTERNAL_STUB}/admin/reset`, { method: 'POST' }).catch(() => null),
  ]);
  res.json(inv);
});

// 在庫の手動調整。UC-06 E3「引当済なのに棚に物が無い」を再現するのに使う。
// 在庫版の障害注入だと考えてよい (FR-606)。
app.post('/api/inventory/:sku/adjust', async (req, res) => {
  const r = await fetch(`${INVENTORY_API}/inventory/${req.params.sku}/adjust`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ delta: Number(req.body?.delta ?? 0) }),
  });
  res.status(r.status).json(await r.json());
});

app.listen(PORT, () => {
  console.log(`frontend listening on :${PORT}`);
});
