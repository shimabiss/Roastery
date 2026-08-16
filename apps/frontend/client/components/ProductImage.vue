<script setup lang="ts">
import { computed } from 'vue'
import { productImage } from '../images'
import { presentationOf } from '../products'

/**
 * 商品画像。**写真があれば写真、無ければイラスト。**
 *
 * 判定はビルド時に確定している（images.ts）ので、実行時に「読み込んでみて
 * 失敗したら差し替える」ようなことはしない。
 * onerror での差し替えは、**一度壊れた画像が描画されてから**入れ替わるため、
 * 一瞬ぐしゃっと見える。
 */
const props = withDefaults(
  defineProps<{
    sku: string
    /** 表示上の最大幅(px)。sizes 属性に使う */
    size?: number
    /** 一覧の先頭など、すぐ見える位置なら false にして遅延読み込みを外す */
    lazy?: boolean
  }>(),
  { size: 400, lazy: true },
)

const image = computed(() => productImage(props.sku))
const art = computed(() => presentationOf(props.sku).art)
const alt = computed(() => presentationOf(props.sku).name)
</script>

<template>
  <!--
    aspect-ratio を CSS で固定しておく。**これが無いと、画像の読み込み完了時に
    高さが変わってページ全体が飛ぶ**（Cumulative Layout Shift）。
    写真でもイラストでも同じ枠に収まるようにしてある。
  -->
  <div class="frame">
    <picture v-if="image">
      <source v-if="image.webpSrcset" type="image/webp" :srcset="image.webpSrcset" :sizes="`${size}px`" />
      <img
        :src="image.fallbackSrc"
        :alt="alt"
        :width="image.width"
        :height="image.height"
        :loading="lazy ? 'lazy' : 'eager'"
        decoding="async"
      />
    </picture>

    <!-- 写真が無いときのイラスト。装飾なので支援技術からは隠す -->
    <svg v-else viewBox="0 0 100 100" fill="none" aria-hidden="true" v-html="art" />
  </div>
</template>

<style scoped>
.frame {
  position: relative;
  aspect-ratio: 4 / 3;
  border-radius: 10px;
  overflow: hidden;
  background: var(--bg);
  display: grid;
  place-items: center;
}
.frame img {
  width: 100%;
  height: 100%;
  /* cover にすると被写体が切れることがあるが、枠が揃うほうを優先する。
     素材側を 4:3 で用意しておけば切れない */
  object-fit: cover;
  display: block;
}
.frame svg {
  width: 62%;
  height: 62%;
}
</style>
