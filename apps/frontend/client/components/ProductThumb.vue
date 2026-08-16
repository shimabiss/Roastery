<script setup lang="ts">
/** 商品のインライン SVG。外部画像を持たないため、SVG 文字列を描画する。 */
defineProps<{ art: string; size: number }>()
</script>

<template>
  <svg :width="size" :height="size" viewBox="0 0 100 100" aria-hidden="true" v-html="art" />
</template>
