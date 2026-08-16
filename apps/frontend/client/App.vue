<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import SiteHeader from './components/SiteHeader.vue'
import ProductCard from './components/ProductCard.vue'
import CartDrawer from './components/CartDrawer.vue'
import OrderResultModal from './components/OrderResultModal.vue'
import AuthModal from './components/AuthModal.vue'
import CheckoutModal from './components/CheckoutModal.vue'
import OrderHistoryModal from './components/OrderHistoryModal.vue'
import { heroImage } from './images'
import * as api from './api'
import { useCart } from './useCart'
import type { Catalog, CheckoutOutcome, Me } from './types'

const catalog = ref<Catalog>({})
const loadError = ref(false)
const cartOpen = ref(false)
const authOpen = ref(false)
const checkoutOpen = ref(false)
const historyOpen = ref(false)
const authMode = ref<'login' | 'register'>('login')
const busy = ref(false)
const outcome = ref<CheckoutOutcome | null>(null)
const me = ref<Me>({ logged_in: false })

/** ヒーロー写真。無ければイラストにフォールバックする（images.ts 参照） */
const hero = heroImage()

const cart = useCart(() => catalog.value)
const products = computed(() => Object.entries(catalog.value))

async function loadCatalog(): Promise<void> {
  try {
    catalog.value = await api.fetchCatalog()
    loadError.value = false
  } catch {
    loadError.value = true
  }
}

async function refreshSession(): Promise<void> {
  me.value = await api.fetchMe().catch(() => ({ logged_in: false }))
  // ログイン状態が変わるとカートも変わる (未ログインカートが会員カートに統合される)
  await cart.load().catch(() => undefined)
}

/**
 * BR-18: **カート投入にログインは不要。**
 * ここで会員かどうかを一切見ていないのが、この要件の実装そのもの。
 */
async function addToCart(sku: string): Promise<void> {
  await cart.add(sku)
  cartOpen.value = true
}

/** 確認メールのリンク (/verify?token=...) から戻ってきた場合の処理 */
async function consumeVerifyToken(): Promise<void> {
  const token = new URLSearchParams(window.location.search).get('token')
  if (!token) return
  await api.verifyEmail(token)
  window.history.replaceState({}, '', '/')
  await refreshSession()
  authOpen.value = true
}

/**
 * カートの中身を **1回の注文** として送る (FR-408 / BR-04)。
 *
 * 修正前はここで明細ごとにループを回し、trace も注文も明細数だけ作られていた。
 * 現在は1リクエスト・1トレース・1注文。
 *
 * BR-03 により結果は「全部成立」か「1つも成立しない」のどちらかなので、
 * 成功時はカートを空に、失敗時はそのまま残す。
 * **keepOnly のような「一部だけ残す」処理が不要になった。**
 */
/**
 * UC-01 手順1〜2。**カートから注文手続きへ入る関門はここ。**
 *
 * BR-18 / BR-19 の判定を、注文を投げる前に済ませている。
 * 投げてから 401/403 で弾かれるより、押した瞬間に理由が出るほうが親切。
 * (サーバー側の判定も残してある。画面の判定だけに頼ってはいけない)
 */
function startCheckout(): void {
  if (cart.entries.value.length === 0) return
  if (!me.value.logged_in) {
    authMode.value = 'login'
    authOpen.value = true
    return
  }
  if (!me.value.email_verified) {
    authOpen.value = true
    return
  }
  cartOpen.value = false
  checkoutOpen.value = true
}

/** UC-01 手順7。確認画面で提示した金額のまま確定する (US-04 / BR-01) */
async function placeOrder(addressId: string): Promise<void> {
  busy.value = true
  const result = await api.checkout(addressId)
  busy.value = false

  // --- BR-18 / BR-19 の関門に当たった場合 -------------------------------
  // **注文の失敗ではないので、注文結果モーダルを出さない。**
  // 「ご注文を承れませんでした」と出すと、原因が決済や在庫だと誤解される。
  if (result.status === 401 || result.status === 403) {
    checkoutOpen.value = false
    authMode.value = 'login'
    authOpen.value = true
    await refreshSession()
    return
  }

  if (result.ok) {
    cart.clearLocal()
    checkoutOpen.value = false
  }
  // 失敗時は確認画面を閉じない。配送先を直してすぐ再試行できるほうが親切
  outcome.value = result
  await loadCatalog()
}

function onKeydown(e: KeyboardEvent): void {
  if (e.key !== 'Escape') return
  cartOpen.value = false
  authOpen.value = false
  checkoutOpen.value = false
  historyOpen.value = false
  outcome.value = null
}

onMounted(async () => {
  await loadCatalog()
  await refreshSession()
  await consumeVerifyToken()
  window.addEventListener('keydown', onKeydown)
})
</script>

<template>
  <SiteHeader
    :count="cart.count.value"
    :me="me"
    @open-cart="cartOpen = true"
    @open-account="authOpen = true"
    @open-history="historyOpen = true"
  />

  <main class="wrap">
    <div class="hero">
      <div>
        <span class="eyebrow">Specialty Coffee</span>
        <h1>その日の一杯を、<br />すこし特別に。</h1>
        <p>
          小さなロースターから届く、生産者の見えるコーヒー。
          焙煎したての豆と、抽出をたのしむための道具を揃えました。
        </p>
        <a class="btn" href="#products">商品を見る</a>
      </div>
      <div class="hero-art">
        <!--
          ヒーローは **遅延読み込みしない**。最初に見える位置の画像を lazy にすると、
          読み込みが後回しになって「開いた瞬間だけ空っぽ」に見える。
        -->
        <picture v-if="hero">
          <source v-if="hero.webpSrcset" type="image/webp" :srcset="hero.webpSrcset" sizes="(max-width: 720px) 90vw, 420px" />
          <img
            :src="hero.fallbackSrc"
            alt="ガラス製のドリッパーと銅のケトルが並ぶ抽出台"
            :width="hero.width"
            :height="hero.height"
            loading="eager"
            fetchpriority="high"
            decoding="async"
          />
        </picture>
        <svg v-else width="180" height="180" viewBox="0 0 100 100" fill="none" aria-hidden="true">
          <ellipse cx="50" cy="76" rx="30" ry="5" fill="#00000012" />
          <path d="M28 34h44l-4 34a10 10 0 0 1-10 9H42a10 10 0 0 1-10-9L28 34Z" fill="#fff" stroke="#6d4529" stroke-width="2.5" />
          <path d="M32 42h36l-3 25a7 7 0 0 1-7 6H42a7 7 0 0 1-7-6l-3-25Z" fill="#6d4529" opacity=".85" />
          <path d="M72 42h6a9 9 0 0 1 0 18h-4" stroke="#6d4529" stroke-width="2.5" fill="none" />
          <path d="M44 24c0-5 5-5 5-10M53 24c0-5 5-5 5-10" stroke="#8a5a3b" stroke-width="2.5" stroke-linecap="round" opacity=".6" />
        </svg>
      </div>
    </div>

    <section id="products">
      <div class="sec-head">
        <h2>商品一覧</h2>
        <span>価格は税込・送料別です</span>
      </div>

      <p v-if="loadError" class="load-error">
        商品情報を取得できませんでした。時間をおいてお試しください。
      </p>

      <div v-else class="grid">
        <ProductCard
          v-for="([sku, item], i) in products"
          :key="sku"
          :sku="sku"
          :item="item"
          :eager="i < 2"
          @add="addToCart"
        />
      </div>
    </section>

    <!--
      FR-1205 焙煎についての紹介（ブランド訴求）。

      ここは **ナビの「焙煎について」が指す先**である。
      以前はこの位置にデモの免責文が入っていて、ラベルと中身が一致していなかった。
      「導線だけ作って中身が無い」状態で、02 の実装状況が 🔶 だったのはこのため。
      免責文はフッターに移した。
    -->
    <section id="about">
      <div class="sec-head">
        <h2>焙煎について</h2>
        <span>Our Roasting</span>
      </div>

      <div class="about-grid">
        <div class="about-lead">
          <p>
            豆の個性が消えない手前で止める、というのが Roastery の焙煎です。
            深く煎れば味は揃いますが、産地ごとの違いは同じ方向に寄っていきます。
            わたしたちは <strong>浅めから中煎り</strong>を軸に、
            その豆がいちばん明るく鳴る一点を探しています。
          </p>
          <p>
            焙煎は注文を受けてからまとめて行い、<strong>焙煎日から中2日以内</strong>に発送します。
            豆は焼いた直後より、数日おいたころが飲みごろです。
            届いてすぐより、週末のほうがおいしいかもしれません。
          </p>
        </div>

        <dl class="about-facts">
          <div>
            <dt>焙煎度</dt>
            <dd>中浅煎り中心。器具や消耗品は焙煎度に関わらず通年でお取り扱いします</dd>
          </div>
          <div>
            <dt>焙煎日</dt>
            <dd>商品ページに記載します。<strong>賞味期限は焙煎日から 6 か月</strong>を目安に</dd>
          </div>
          <div>
            <dt>生豆の調達</dt>
            <dd>取引の経路がたどれるものだけを扱います。生産者・農園・精製方法を明記します</dd>
          </div>
          <div>
            <dt>おすすめの飲み方</dt>
            <dd>90〜92℃、豆 12g に対して湯 200ml。まず 30 秒蒸らしてください</dd>
          </div>
        </dl>
      </div>
    </section>

    <!--
      法定表示 (FR-1201〜1204)。**架空のサイトでも Must にしてある。**
      実在の EC を題材にする以上、「これが必要である」ことを要件として
      認識しておく価値がある。機能一覧から法務要件が抜けるのは実務でもよくある漏れ。
    -->
    <section id="legal">
      <div class="sec-head">
        <h2>ご利用にあたって</h2>
        <span>架空のサイトのため、記載は書式の例です</span>
      </div>

      <div class="legal-grid">
        <article id="shipping">
          <h3>配送・返品について</h3>
          <dl>
            <dt>送料</dt>
            <dd>お届け先の地域により 600〜1,500 円（税込）。<strong>5,000 円以上のお買い上げで無料</strong>。</dd>
            <dt>お届け</dt>
            <dd>ご注文確定後、通常 2〜4 日で発送します。追跡番号はメールでお知らせします。</dd>
            <dt>返品</dt>
            <dd>
              商品到着後 <strong>7 日以内</strong>にご連絡ください。
              コーヒー豆は<strong>開封後の返品を承れません</strong>（不良品を除く）。
              器具・消耗品は未使用・未開封の場合に限り承ります。
            </dd>
            <dt>返送料</dt>
            <dd>不良品・誤配送は当店負担、お客様都合の場合はお客様負担となります。</dd>
          </dl>
        </article>

        <article id="tokushoho">
          <h3>特定商取引法に基づく表記</h3>
          <dl>
            <dt>販売事業者</dt><dd>Roastery（架空）</dd>
            <dt>運営責任者</dt><dd>—</dd>
            <dt>所在地・連絡先</dt><dd>架空のデモサイトのため記載はありません。</dd>
            <dt>販売価格</dt><dd>各商品ページに税込価格で表示しています。</dd>
            <dt>代金の支払時期</dt><dd>クレジットカード決済。ご注文時に与信、<strong>出荷時に売上を確定</strong>します。</dd>
            <dt>引渡し時期</dt><dd>ご注文確定後 2〜4 日で発送します。</dd>
          </dl>
        </article>

        <article id="privacy">
          <h3>プライバシーポリシー</h3>
          <p>
            ご注文に必要な範囲で氏名・住所・電話番号・メールアドレスをお預かりします。
            <strong>クレジットカード情報は当サイトを通過せず、保持しません。</strong>
            退会された場合、会計上の保存義務がある記録を除き、
            お預かりした個人情報は匿名化します。
          </p>
        </article>

        <article id="terms">
          <h3>利用規約</h3>
          <p>
            ご購入には会員登録とメールアドレスの確認が必要です。
            在庫はご注文の確定時に確保され、確保できない場合はご注文全体が成立しません。
            出荷手続きの開始後はキャンセルを承れません。返品の手続きをご利用ください。
          </p>
        </article>
      </div>
    </section>
  </main>

  <footer>
    <div class="wrap foot">
      <div>
        © 2026 Roastery
        <p class="disclaimer">
          技術学習用に作られた <strong>架空のオンラインストア</strong>です。
          実際の商品販売・決済は行っていません。
          注文操作はデモ用のバックエンドに対して実行されます。
        </p>
      </div>
      <div class="foot-links">
        <a href="#shipping">配送・返品</a>
        <a href="#tokushoho">特定商取引法に基づく表記</a>
        <a href="#privacy">プライバシー</a>
        <a href="#terms">利用規約</a>
        <a href="/ops">運用画面</a>
      </div>
    </div>
  </footer>

  <CartDrawer
    :open="cartOpen"
    :entries="cart.entries.value"
    :catalog="catalog"
    :total="cart.total.value"
    :busy="busy"
    :max-per-line="cart.maxPerLine.value"
    :trimmed="cart.trimmed.value"
    :logged-in="me.logged_in"
    :email-verified="me.email_verified ?? false"
    @close="cartOpen = false"
    @set-qty="cart.setQty"
    @remove="cart.remove"
    @checkout="startCheckout"
  />

  <CheckoutModal
    :open="checkoutOpen"
    :subtotal="cart.total.value"
    :entries="cart.entries.value"
    :catalog="catalog"
    :busy="busy"
    @close="checkoutOpen = false"
    @place="placeOrder"
  />

  <OrderHistoryModal
    :open="historyOpen"
    @close="historyOpen = false"
    @changed="loadCatalog"
  />

  <OrderResultModal :outcome="outcome" @close="outcome = null" />

  <AuthModal
    :open="authOpen"
    :me="me"
    :initial-mode="authMode"
    @close="authOpen = false"
    @changed="refreshSession"
  />
</template>

<style scoped>
.hero { padding: 72px 0 56px; display: grid; grid-template-columns: 1.15fr .85fr; gap: 40px; align-items: center; }
@media (max-width: 820px) { .hero { grid-template-columns: 1fr; padding: 48px 0 36px; } }
h1 { font-size: 40px; line-height: 1.3; margin: 0 0 18px; letter-spacing: -.01em; }
@media (max-width: 820px) { h1 { font-size: 30px; } }
.hero p { color: var(--muted); margin: 0 0 26px; font-size: 16px; max-width: 46ch; }
.eyebrow {
  display: inline-block; font-size: 12px; letter-spacing: .14em; text-transform: uppercase;
  color: var(--accent); font-weight: 700; margin-bottom: 14px;
}
.hero-art {
  aspect-ratio: 4 / 3; border-radius: 16px; box-shadow: var(--shadow);
  background: linear-gradient(150deg, #f0e6da, #dcc9b4 60%, #c4a888);
  display: grid; place-items: center; overflow: hidden;
}
section { padding: 24px 0 56px; }
.sec-head { display: flex; align-items: baseline; justify-content: space-between; margin-bottom: 24px; gap: 16px; }
.sec-head h2 { font-size: 22px; margin: 0; }
.sec-head span { color: var(--muted); font-size: 13px; }
.grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 20px; }
@media (max-width: 980px) { .grid { grid-template-columns: repeat(2, 1fr); } }
@media (max-width: 560px) { .grid { grid-template-columns: 1fr; } }
.load-error { color: var(--err); font-size: 14px; }
.notice {
  background: #f3ece3; border: 1px solid var(--line); border-radius: 10px;
  padding: 12px 16px; font-size: 12.5px; color: var(--muted); margin-bottom: 22px;
}
footer { border-top: 1px solid var(--line); margin-top: 40px; padding: 32px 0 48px; }
.foot { display: flex; justify-content: space-between; gap: 20px; flex-wrap: wrap; font-size: 13px; color: var(--muted); }
</style>
