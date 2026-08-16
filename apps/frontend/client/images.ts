/**
 * 商品写真とヒーロー画像の解決。
 *
 * 設計方針: **素材が無くても壊れない。**
 * ------------------------------------
 * `import.meta.glob` はビルド時に「実際に存在するファイル」だけを拾う。
 * 画像を1枚も置いていなければマップは空になり、呼び出し側は
 * インライン SVG のイラストにフォールバックする。
 *
 * つまり **画像はあとから足せる。** 置けば差し替わり、消せば戻る。
 * 「アセットを用意しないとビルドが通らない」形にすると、
 * リポジトリを clone した人が最初の1回で詰まる。
 *
 * 置き場所と命名
 * --------------
 *   client/assets/products/<SKU>-400.webp    一覧・カート用
 *   client/assets/products/<SKU>-800.webp    詳細・高解像度ディスプレイ用
 *   client/assets/products/<SKU>-800.jpg     WebP 非対応ブラウザ向けフォールバック
 *   client/assets/hero-1600.webp             ヒーロー画像
 *
 * 元の写真を `client/assets/products/<SKU>.jpg` に置いて
 * `./scripts/optimize-images.sh` を実行すると、上の形に変換される。
 */

type UrlMap = Record<string, string>

/**
 * ビルド時にファイルを URL へ解決する。
 * `eager: true` にしているのは、**画像の有無を実行時ではなくビルド時に確定**
 * させるため。動的 import にすると、無い画像を掴んだときに実行時エラーになる。
 */
const productFiles = import.meta.glob('./assets/products/*.{webp,jpg,jpeg,png}', {
  eager: true,
  query: '?url',
  import: 'default',
}) as UrlMap

const heroFiles = import.meta.glob('./assets/hero-*.{webp,jpg,jpeg,png}', {
  eager: true,
  query: '?url',
  import: 'default',
}) as UrlMap

/** `./assets/products/COFFEE-BEANS-1KG-400.webp` → `COFFEE-BEANS-1KG-400.webp` */
function basename(path: string): string {
  return path.slice(path.lastIndexOf('/') + 1)
}

/** ファイル名を `<SKU>`・`<幅>`・`<拡張子>` に分解する。規約に合わないものは無視する */
function parse(name: string): { sku: string; width: number; ext: string } | null {
  const m = name.match(/^(.+?)-(\d+)\.(webp|jpg|jpeg|png)$/i)
  if (!m) return null
  return { sku: m[1]!, width: Number(m[2]), ext: m[3]!.toLowerCase() }
}

export interface ImageSet {
  /** WebP の srcset。空文字なら WebP が無い */
  webpSrcset: string
  /** フォールバックの src（jpg/png）。無ければ WebP の最大サイズ */
  fallbackSrc: string
  /** レイアウトシフトを防ぐための実寸 */
  width: number
  height: number
}

/** 幅からアスペクト比 4:3 で高さを出す。商品写真は 4:3 に揃える前提 */
function heightOf(width: number): number {
  return Math.round((width * 3) / 4)
}

function build(files: UrlMap, key: string): ImageSet | null {
  const entries = Object.entries(files)
    .map(([path, url]) => ({ ...parse(basename(path)), url }))
    .filter((e): e is { sku: string; width: number; ext: string; url: string } => !!e.sku)
    .filter((e) => e.sku === key)

  if (entries.length === 0) return null

  const webp = entries.filter((e) => e.ext === 'webp').sort((a, b) => a.width - b.width)
  const raster = entries.filter((e) => e.ext !== 'webp').sort((a, b) => a.width - b.width)

  const widest = [...raster, ...webp].sort((a, b) => b.width - a.width)[0]!

  return {
    // srcset に幅を書くと、ブラウザが画面幅と DPR を見て自分で選ぶ。
    // **選択をブラウザに任せるのが正解**で、JS で判定すると必ずずれる
    webpSrcset: webp.map((e) => `${e.url} ${e.width}w`).join(', '),
    fallbackSrc: (raster[raster.length - 1] ?? webp[webp.length - 1])!.url,
    width: widest.width,
    height: heightOf(widest.width),
  }
}

/** SKU に対応する商品写真。無ければ null（呼び出し側は SVG にフォールバック） */
export function productImage(sku: string): ImageSet | null {
  return build(productFiles, sku)
}

/** ヒーロー画像。無ければ null */
export function heroImage(): ImageSet | null {
  return build(heroFiles, 'hero')
}

/** 1枚でも写真が置かれているか。案内文の出し分けに使う */
export const hasAnyPhoto = Object.keys(productFiles).length > 0
