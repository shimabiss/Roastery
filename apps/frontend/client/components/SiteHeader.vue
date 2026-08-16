<script setup lang="ts">
import type { Me } from '../types'

defineProps<{ count: number; me: Me }>()
defineEmits<{ openCart: []; openAccount: []; openHistory: [] }>()
</script>

<template>
  <header>
    <div class="wrap bar">
      <div class="logo"><span class="mark">R</span> Roastery</div>
      <nav>
        <a href="#products">商品一覧</a>
        <a href="#about">焙煎について</a>
        <a href="#shipping">配送・返品</a>
      </nav>
      <div class="actions">
        <button v-if="me.logged_in" class="acct-btn" @click="$emit('openHistory')">注文履歴</button>
        <!--
          BR-19 の中間状態を、ヘッダーで常に見えるようにしている。
          「ログインできているのに買えない」理由が、購入直前まで分からないと詰む。
        -->
        <button class="acct-btn" @click="$emit('openAccount')">
          <template v-if="me.logged_in">
            {{ me.email_verified ? 'アカウント' : '要メール確認' }}
          </template>
          <template v-else>ログイン</template>
          <span v-if="me.logged_in && !me.email_verified" class="warn-dot" aria-hidden="true" />
        </button>
        <button class="cart-btn" aria-label="カートを開く" @click="$emit('openCart')">
          カート
          <span v-if="count > 0" class="cart-count">{{ count }}</span>
        </button>
      </div>
    </div>
  </header>
</template>

<style scoped>
header {
  position: sticky; top: 0; z-index: 40;
  background: rgba(250, 247, 242, .88); backdrop-filter: blur(8px);
  border-bottom: 1px solid var(--line);
}
.bar { display: flex; align-items: center; justify-content: space-between; height: 68px; gap: 16px; }
.actions { display: flex; align-items: center; gap: 10px; }
.acct-btn {
  position: relative; border: 1px solid var(--line); background: transparent; cursor: pointer;
  border-radius: 999px; padding: 8px 14px; font-size: 13px; color: inherit;
}
.warn-dot {
  position: absolute; top: -2px; right: -2px; width: 8px; height: 8px; border-radius: 50%;
  background: var(--warn);
}
.logo { display: flex; align-items: center; gap: 10px; font-weight: 700; font-size: 19px; letter-spacing: .02em; }
.mark {
  width: 30px; height: 30px; border-radius: 50%;
  background: linear-gradient(135deg, var(--accent), #4a2f1e);
  display: grid; place-items: center; color: #fff; font-size: 15px;
}
nav { display: flex; gap: 22px; font-size: 14px; color: var(--muted); }
nav a { text-decoration: none; }
nav a:hover { color: var(--ink); }
@media (max-width: 700px) { nav { display: none; } }
.cart-btn {
  position: relative; border: 1px solid var(--line); background: var(--surface);
  border-radius: 999px; padding: 9px 18px 9px 16px; font-size: 14px; font-weight: 600;
  cursor: pointer; font-family: inherit; color: var(--ink);
  display: flex; align-items: center; gap: 8px;
}
.cart-btn:hover { border-color: var(--accent); }
.cart-count {
  background: var(--accent); color: #fff; border-radius: 999px;
  min-width: 20px; height: 20px; padding: 0 6px; font-size: 12px;
  display: grid; place-items: center; font-variant-numeric: tabular-nums;
}
</style>
