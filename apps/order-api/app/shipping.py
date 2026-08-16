"""
送料の算出 (FR-404)。

なぜ独立したモジュールにするか
------------------------------
送料は **業務ルールの塊** であり、かつ最も頻繁に変わる。
外部呼び出しにも DB にも依存させずに純粋関数にしておくと、
テストが速く、変更したときに壊れた箇所がすぐ分かる。

UC-01 の手順4「システムが配送先と注文金額から送料を算出し、合計金額を提示する」
に対応する。**配送先が決まらないと合計金額が出ない**というのが、
注文手続きに手順3 (配送先の入力) が必要な理由でもある。
"""

# 地域区分ごとの送料 (税込・円)。
# 実際の宅配便の運賃表を大幅に単純化したもの。
# **区分の切り方そのものが業務判断**であり、実装の都合ではない。
ZONE_FEES: dict[str, int] = {
    "北海道": 1200,
    "北東北": 800,
    "南東北": 700,
    "関東": 600,
    "信越": 700,
    "北陸": 700,
    "中部": 700,
    "関西": 800,
    "中国": 900,
    "四国": 900,
    "九州": 1000,
    "沖縄": 1500,
}

PREFECTURE_ZONE: dict[str, str] = {
    "北海道": "北海道",
    "青森県": "北東北", "秋田県": "北東北", "岩手県": "北東北",
    "宮城県": "南東北", "山形県": "南東北", "福島県": "南東北",
    "茨城県": "関東", "栃木県": "関東", "群馬県": "関東", "埼玉県": "関東",
    "千葉県": "関東", "東京都": "関東", "神奈川県": "関東", "山梨県": "関東",
    "新潟県": "信越", "長野県": "信越",
    "富山県": "北陸", "石川県": "北陸", "福井県": "北陸",
    "静岡県": "中部", "愛知県": "中部", "岐阜県": "中部", "三重県": "中部",
    "大阪府": "関西", "京都府": "関西", "兵庫県": "関西",
    "滋賀県": "関西", "奈良県": "関西", "和歌山県": "関西",
    "岡山県": "中国", "広島県": "中国", "山口県": "中国",
    "鳥取県": "中国", "島根県": "中国",
    "香川県": "四国", "徳島県": "四国", "愛媛県": "四国", "高知県": "四国",
    "福岡県": "九州", "佐賀県": "九州", "長崎県": "九州", "熊本県": "九州",
    "大分県": "九州", "宮崎県": "九州", "鹿児島県": "九州",
    "沖縄県": "沖縄",
}

# UC-01 A2「送料無料の条件を満たす場合」の閾値
FREE_SHIPPING_THRESHOLD = 5000

# 未知の都道府県が来たときの既定。**エラーにせず高いほうに寄せる。**
# 安いほうに倒すと、取りこぼした運賃を店舗が負担することになる。
DEFAULT_ZONE = "関東"


class UnknownPrefecture(ValueError):
    pass


def zone_of(prefecture: str) -> str:
    zone = PREFECTURE_ZONE.get((prefecture or "").strip())
    if zone is None:
        raise UnknownPrefecture(f"配送先の都道府県を判定できません: {prefecture!r}")
    return zone


def shipping_fee(prefecture: str, subtotal: int, threshold: int = FREE_SHIPPING_THRESHOLD) -> int:
    """配送先と商品小計から送料を返す。

    **送料無料の判定に使うのは「商品小計」であって「合計」ではない。**
    合計 (= 小計 + 送料) で判定すると、送料が閾値を押し上げて
    「送料を足したから無料になった」という循環が起きる。
    要件に一行で書くと見落としやすいが、実装では必ずどちらか決まる。
    """
    if subtotal >= threshold:
        return 0
    return ZONE_FEES[zone_of(prefecture)]


def quote(prefecture: str, subtotal: int, threshold: int = FREE_SHIPPING_THRESHOLD) -> dict:
    """画面に出すための内訳。UC-01 手順4 の「合計金額を提示する」。

    **なぜ 0 円なのかを一緒に返す**のが要点。
    金額だけ返すと「送料が無料の地域なのか、閾値を超えたのか」が分からず、
    利用者は「あと少し買えば無料になる」かどうかを判断できない。
    """
    zone = zone_of(prefecture)
    base = ZONE_FEES[zone]
    free = subtotal >= threshold
    fee = 0 if free else base
    return {
        "zone": zone,
        "base_fee": base,
        "shipping_fee": fee,
        "free_shipping_applied": free,
        "free_shipping_threshold": threshold,
        "remaining_for_free": max(0, threshold - subtotal),
        "subtotal": subtotal,
        "total": subtotal + fee,
    }


def prefectures() -> list[str]:
    return list(PREFECTURE_ZONE.keys())
