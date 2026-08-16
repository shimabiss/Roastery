<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import * as api from '../api'
import type { Me } from '../types'

/**
 * 会員登録とログイン。
 *
 * この画面は4つの学習テーマ (設計・可観測性・CI/CD・IaC) を1つも支えていない。
 * それでも作るのは、**会員必須と決めた以上これが無いと1件も売れない**から。
 * 価値駆動ではなく **依存駆動** でスコープに入っている。
 */
const props = defineProps<{ open: boolean; me: Me; initialMode?: 'login' | 'register' }>()
const emit = defineEmits<{ close: []; changed: [] }>()

const mode = ref<'login' | 'register'>('login')
const email = ref('')
const password = ref('')
const busy = ref(false)
const error = ref('')
const info = ref('')
/** 確認メールの中身。実サービスが無いのでスタブの受信箱から読む */
const verifyLink = ref('')

watch(
  () => props.open,
  (open) => {
    if (!open) return
    mode.value = props.initialMode ?? 'login'
    error.value = ''
    info.value = ''
    verifyLink.value = ''
  },
)

const title = computed(() => (mode.value === 'login' ? 'ログイン' : '会員登録'))

async function submit(): Promise<void> {
  busy.value = true
  error.value = ''
  info.value = ''

  const fn = mode.value === 'login' ? api.login : api.register
  const res = await fn(email.value, password.value)

  if (!res.ok) {
    // FR-114 / US-12: サーバーが返す **具体的な理由** をそのまま見せる。
    // 「パスワードが不正です」に丸めると、次に何をすべきか分からなくなる
    error.value = res.detail || '処理できませんでした。入力内容をご確認ください。'
    busy.value = false
    return
  }

  if (mode.value === 'register') {
    info.value = '確認メールをお送りしました。リンクを開くとご注文いただけます。'
    await pickUpVerifyLink()
  } else {
    emit('changed')
    emit('close')
  }
  busy.value = false
}

/**
 * デモ用。送信済みメールから確認リンクを拾って画面に出す。
 * 実サービスを繋いでいないので、これが無いと確認の導線が試せない。
 */
async function pickUpVerifyLink(): Promise<void> {
  const inbox = await api.fetchInbox(email.value.trim().toLowerCase()).catch(() => [])
  const match = inbox[0]?.body?.match(/token=([\w-]+)/)
  if (match) verifyLink.value = match[1]!
}

async function completeVerification(): Promise<void> {
  busy.value = true
  const res = await api.verifyEmail(verifyLink.value)
  busy.value = false
  if (!res.ok) {
    error.value = res.detail || '確認できませんでした。'
    return
  }
  info.value = 'メールアドレスの確認が完了しました。ログインしてください。'
  verifyLink.value = ''
  mode.value = 'login'
  emit('changed')
}

async function doLogout(): Promise<void> {
  await api.logout()
  emit('changed')
  emit('close')
}

/** 未確認のままログインしている状態。**ログインはできるが購入だけができない** */
const needsVerification = computed(() => props.me.logged_in && !props.me.email_verified)

async function resendPickup(): Promise<void> {
  if (!props.me.email) return
  email.value = props.me.email
  await pickUpVerifyLink()
  if (!verifyLink.value) error.value = '確認メールが見つかりませんでした。'
}
</script>

<template>
  <div v-if="open" class="modal" @click.self="$emit('close')">
    <div class="modal-box">
      <!-- ログイン済み -->
      <template v-if="me.logged_in">
        <h2>アカウント</h2>
        <p class="sub">{{ me.email }}</p>

        <!--
          BR-19 の中間状態。「確認が済むまでログインさせない」ほうが実装は単純だが、
          利用者は自分が何をすればよいか分からないまま締め出される。
          ログインさせたうえで、理由と復帰導線を出すほうが戻ってこられる。
        -->
        <div v-if="needsVerification" class="gate">
          <p><strong>メールアドレスの確認が完了していません。</strong></p>
          <p>確認が完了するまで、ご注文いただけません。閲覧とカートはご利用いただけます。</p>
          <button class="btn ghost sm" :disabled="busy" @click="resendPickup">
            確認メールを開く
          </button>
          <button v-if="verifyLink" class="btn sm" :disabled="busy" @click="completeVerification">
            確認を完了する
          </button>
        </div>

        <p v-if="info" class="info">{{ info }}</p>
        <p v-if="error" class="err">{{ error }}</p>
        <button class="btn ghost full" @click="doLogout">ログアウト</button>
        <button class="btn ghost full" @click="$emit('close')">閉じる</button>
      </template>

      <!-- 未ログイン -->
      <template v-else>
        <h2>{{ title }}</h2>
        <div class="tabs">
          <button :class="{ on: mode === 'login' }" @click="mode = 'login'">ログイン</button>
          <button :class="{ on: mode === 'register' }" @click="mode = 'register'">会員登録</button>
        </div>

        <form @submit.prevent="submit">
          <label>
            メールアドレス
            <input v-model="email" type="email" autocomplete="email" required />
          </label>
          <label>
            パスワード
            <input
              v-model="password"
              type="password"
              :autocomplete="mode === 'login' ? 'current-password' : 'new-password'"
              required
            />
          </label>
          <p v-if="mode === 'register'" class="hint">
            10 文字以上で、英字と数字を含めてください。
          </p>

          <p v-if="error" class="err">{{ error }}</p>
          <p v-if="info" class="info">{{ info }}</p>

          <div v-if="verifyLink" class="gate">
            <p>デモ環境のため、確認メールの内容をここから開けます。</p>
            <button type="button" class="btn sm" :disabled="busy" @click="completeVerification">
              確認を完了する
            </button>
          </div>

          <button class="btn full" type="submit" :disabled="busy">
            {{ busy ? '処理中…' : title }}
          </button>
        </form>
        <button class="btn ghost full" @click="$emit('close')">閉じる</button>
      </template>
    </div>
  </div>
</template>

<style scoped>
.modal {
  position: fixed; inset: 0; z-index: 80; display: grid; place-items: center;
  padding: 24px; background: rgba(43, 35, 32, .42);
}
.modal-box {
  background: var(--surface); border-radius: 16px; padding: 28px; max-width: 420px; width: 100%;
  box-shadow: 0 20px 60px rgba(43, 35, 32, .24); max-height: 88vh; overflow-y: auto;
}
h2 { margin: 0 0 8px; font-size: 20px; }
.sub { color: var(--muted); font-size: 13px; margin: 0 0 18px; }
.tabs { display: flex; gap: 6px; margin-bottom: 18px; }
.tabs button {
  flex: 1; padding: 8px; border: 1px solid var(--line); background: transparent;
  border-radius: 8px; font-size: 13px; cursor: pointer; color: var(--muted);
}
.tabs button.on { background: var(--bg); color: inherit; font-weight: 600; }
label { display: block; font-size: 12px; color: var(--muted); margin-bottom: 12px; }
input {
  display: block; width: 100%; margin-top: 4px; padding: 9px 10px; font-size: 14px;
  border: 1px solid var(--line); border-radius: 8px; background: var(--surface); color: inherit;
}
.hint { font-size: 12px; color: var(--muted); margin: -4px 0 12px; }
.err { color: var(--err); font-size: 13px; line-height: 1.6; margin: 0 0 12px; }
.info { color: var(--ok); font-size: 13px; line-height: 1.6; margin: 0 0 12px; }
.gate {
  background: var(--bg); border-left: 3px solid var(--warn); border-radius: 8px;
  padding: 12px; margin: 0 0 14px; font-size: 12px; line-height: 1.7;
}
.gate p { margin: 0 0 8px; }
.full { width: 100%; margin-top: 8px; }
.sm { font-size: 12px; padding: 6px 12px; margin-right: 6px; }
</style>
