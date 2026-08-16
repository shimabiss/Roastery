#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# 商品写真とヒーロー画像を、配信に適した形へ変換する。
#
# なぜ変換が要るか
# ----------------
# スマホやストックサイトの写真はそのままだと 2〜6MB ある。
# 7サービスのうち frontend のコンテナイメージにそれが全部乗るので、
# **ACA のデプロイ時間と pull 時間に直に効く。**
# WebP に落として幅を2種類だけ持てば、合計で数百 KB に収まる。
#
# やること
#   client/assets/products/<SKU>.jpg  （元の写真）
#     → <SKU>-400.webp  一覧・カート用
#     → <SKU>-800.webp  高解像度ディスプレイ用
#     → <SKU>-800.jpg   WebP 非対応ブラウザ向けフォールバック
#
#   client/assets/hero.jpg
#     → hero-800.webp / hero-1600.webp / hero-1600.jpg
#
# 変換後は元ファイルを `_original/` に退避する（消さない）。
# **リポジトリには入らないよう .gitignore してある。**
#
# 使い方
#   ./scripts/optimize-images.sh
# ---------------------------------------------------------------------------
set -euo pipefail

ASSETS="apps/frontend/client/assets"
PRODUCTS="${ASSETS}/products"
ORIGINALS="${ASSETS}/_original"

if ! command -v magick > /dev/null 2>&1 && ! command -v convert > /dev/null 2>&1; then
  cat >&2 <<'EOF'
ImageMagick が見つかりません。

  Ubuntu / WSL2 : sudo apt-get install -y imagemagick webp
  macOS         : brew install imagemagick webp

インストール後にもう一度実行してください。
EOF
  exit 1
fi

# ImageMagick 7 は magick、6 は convert
IM="$(command -v magick || command -v convert)"

# 商品写真は 4:3 に切り出す。ProductImage.vue の枠がその比率で固定されているため、
# ここで揃えておかないと object-fit: cover で被写体が切れる。
crop_and_resize() {
  local src="$1" out="$2" width="$3" quality="$4"
  "$IM" "$src" \
    -auto-orient \
    -resize "$((width * 2))x" \
    -gravity center -crop "4:3" +repage \
    -resize "${width}x" \
    -strip \
    -quality "$quality" \
    "$out"
}

mkdir -p "$PRODUCTS" "$ORIGINALS"

shopt -s nullglob nocaseglob
found=0

# --- 商品写真 --------------------------------------------------------------
for src in "$PRODUCTS"/*.jpg "$PRODUCTS"/*.jpeg "$PRODUCTS"/*.png; do
  base="$(basename "$src")"
  name="${base%.*}"

  # 既に変換済み (-400 / -800 が付いている) ものは対象外
  case "$name" in
    *-[0-9][0-9][0-9]|*-[0-9][0-9][0-9][0-9]) continue ;;
  esac

  echo "商品: ${name}"
  crop_and_resize "$src" "${PRODUCTS}/${name}-400.webp" 400 82
  crop_and_resize "$src" "${PRODUCTS}/${name}-800.webp" 800 80
  crop_and_resize "$src" "${PRODUCTS}/${name}-800.jpg" 800 82
  mv "$src" "${ORIGINALS}/"
  found=$((found + 1))
done

# --- ヒーロー画像 ----------------------------------------------------------
for src in "$ASSETS"/hero.jpg "$ASSETS"/hero.jpeg "$ASSETS"/hero.png; do
  echo "ヒーロー: $(basename "$src")"
  crop_and_resize "$src" "${ASSETS}/hero-800.webp" 800 82
  crop_and_resize "$src" "${ASSETS}/hero-1600.webp" 1600 78
  crop_and_resize "$src" "${ASSETS}/hero-1600.jpg" 1600 80
  mv "$src" "${ORIGINALS}/"
  found=$((found + 1))
done

shopt -u nullglob nocaseglob

if [ "$found" -eq 0 ]; then
  cat <<EOF
変換対象がありませんでした。

写真を以下に置いてから、もう一度実行してください。

  ${PRODUCTS}/COFFEE-BEANS-1KG.jpg
  ${PRODUCTS}/MUG-CERAMIC.jpg
  ${PRODUCTS}/DRIP-KETTLE.jpg
  ${PRODUCTS}/FILTER-100P.jpg
  ${ASSETS}/hero.jpg

**ファイル名は SKU と完全に一致させてください。** 一致しないものは無視されます。
EOF
  exit 0
fi

echo
echo "--- 変換後のサイズ ---"
du -ch "${PRODUCTS}"/*.webp "${PRODUCTS}"/*.jpg "${ASSETS}"/hero-*.webp "${ASSETS}"/hero-*.jpg 2>/dev/null | tail -1

cat <<EOF

完了しました（${found} 件）。元ファイルは ${ORIGINALS}/ に退避してあります
（.gitignore 対象なのでコミットされません）。

次にやること:
  1. ${ASSETS}/CREDITS.md に出典を記入する（Public リポジトリなので必須）
  2. cd apps/frontend && npm run build   で取り込まれることを確認する
EOF
