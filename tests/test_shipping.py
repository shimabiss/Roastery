"""
送料計算のユニットテスト (FR-404)。

**UC-01 の手順4 と代替フロー A2 がそのままテストになっている。**
"""

import pytest

from conftest import load

sh = load("order-api", "shipping")


# ---------------------------------------------------------------------------
# 地域区分
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "prefecture, zone",
    [
        ("東京都", "関東"),
        ("北海道", "北海道"),
        ("沖縄県", "沖縄"),
        ("大阪府", "関西"),
        ("福岡県", "九州"),
    ],
)
def test_zone_of(prefecture, zone):
    assert sh.zone_of(prefecture) == zone


def test_all_47_prefectures_are_mapped():
    """**47 都道府県すべてに区分がある**こと。

    抜けがあると、その県の利用者だけ注文できない。
    一覧をテストで固定しておかないと、追加時に気づけない。
    """
    assert len(sh.PREFECTURE_ZONE) == 47


def test_every_zone_has_a_fee():
    """区分を足して運賃表を足し忘れる、は最も起きやすい漏れ。"""
    for zone in set(sh.PREFECTURE_ZONE.values()):
        assert zone in sh.ZONE_FEES, f"{zone} の運賃が未定義"


def test_unknown_prefecture_raises():
    with pytest.raises(sh.UnknownPrefecture):
        sh.zone_of("東京")  # 「都」が無い


# ---------------------------------------------------------------------------
# 送料
# ---------------------------------------------------------------------------
def test_fee_varies_by_zone():
    assert sh.shipping_fee("東京都", 1000) == 600
    assert sh.shipping_fee("沖縄県", 1000) == 1500
    assert sh.shipping_fee("東京都", 1000) < sh.shipping_fee("北海道", 1000)


# ---------------------------------------------------------------------------
# UC-01 A2: 送料無料
# ---------------------------------------------------------------------------
def test_free_shipping_above_threshold():
    assert sh.shipping_fee("沖縄県", sh.FREE_SHIPPING_THRESHOLD) == 0


def test_threshold_is_inclusive():
    """閾値ちょうどが無料か有料かは、要件に書いていないと必ず揉める。

    ここでは「以上」= ちょうどで無料と決めている。
    """
    t = sh.FREE_SHIPPING_THRESHOLD
    assert sh.shipping_fee("東京都", t) == 0
    assert sh.shipping_fee("東京都", t - 1) > 0


def test_free_shipping_is_judged_on_subtotal_not_total():
    """**送料無料の判定に使うのは商品小計であって合計ではない。**

    合計 (小計 + 送料) で判定すると、送料が閾値を押し上げて
    「送料を足したから無料になった」という循環が起きる。
    """
    subtotal = sh.FREE_SHIPPING_THRESHOLD - 100  # 送料を足せば閾値を超える額
    fee = sh.shipping_fee("東京都", subtotal)
    assert fee > 0, "送料を足した額で判定してはいけない"
    assert subtotal + fee > sh.FREE_SHIPPING_THRESHOLD


# ---------------------------------------------------------------------------
# 画面に出す内訳
# ---------------------------------------------------------------------------
def test_quote_explains_why_it_is_free():
    """金額だけ返すと「無料の地域なのか閾値超えなのか」が分からない。"""
    q = sh.quote("沖縄県", 6000)
    assert q["shipping_fee"] == 0
    assert q["free_shipping_applied"] is True
    assert q["base_fee"] == 1500, "無料でも本来の送料は伝える"
    assert q["remaining_for_free"] == 0


def test_quote_tells_how_much_more_is_needed():
    """「あと少し買えば無料」を判断できるようにする。"""
    q = sh.quote("東京都", 4000)
    assert q["free_shipping_applied"] is False
    assert q["remaining_for_free"] == 1000
    assert q["total"] == 4000 + 600


def test_quote_total_is_subtotal_plus_fee():
    q = sh.quote("北海道", 3000)
    assert q["total"] == q["subtotal"] + q["shipping_fee"]
