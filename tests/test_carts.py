"""
カートのマージと上限のユニットテスト。

**US-14 の受入基準がそのままテストになっている。**
第3版の時点では「同じ商品が両方にあったら」の基準が書けなかった。
マージ規則 (BR-20) が未決だったからで、
**受入基準が書けないことが未決の検出装置になっていた。**
"""

import fakeredis
import pytest

from conftest import load

carts = load("member-api", "carts")
CartStore = carts.CartStore
MAX = carts.MAX_QTY_PER_LINE


@pytest.fixture()
def store():
    return CartStore(fakeredis.FakeStrictRedis(decode_responses=True))


# ---------------------------------------------------------------------------
# BR-20 / FR-306: 同一 SKU は数量を合算する
# ---------------------------------------------------------------------------
def test_merge_sums_quantities_for_same_sku():
    """US-14 の3つ目の受入基準。

    未ログイン側に商品A×2、会員側に商品A×1 → 3つになる。
    """
    assert carts.merge_quantities({"A": 2}, {"A": 1}) == {"A": 3}


def test_merge_keeps_items_from_both_sides():
    """US-14 の2つ目の受入基準。「入れたものが消える」を防ぐのが合算の理由。"""
    assert carts.merge_quantities({"A": 1}, {"B": 1}) == {"A": 1, "B": 1}


def test_merge_of_empty_guest_cart_is_noop():
    assert carts.merge_quantities({}, {"B": 2}) == {"B": 2}


# ---------------------------------------------------------------------------
# BR-22 / FR-311: 合算が上限を超えたらクランプする
# ---------------------------------------------------------------------------
def test_merge_clamps_at_max_quantity():
    """US-14 の4つ目の受入基準。

    **この基準は「合算する」と決めたから生まれた。**
    上書きを選んでいれば、上限という概念自体が不要だった。
    """
    assert carts.merge_quantities({"A": MAX}, {"A": MAX}) == {"A": MAX}


def test_clamped_lines_reports_what_was_trimmed():
    """切り詰めたことを伝えないと「勝手に数量が変わった」ことになる。"""
    final, trimmed = carts.clamped_lines({"A": MAX + 5, "B": 1})
    assert final == {"A": MAX, "B": 1}
    assert trimmed == ["A"]


def test_zero_and_negative_quantities_are_dropped():
    final, trimmed = carts.clamped_lines({"A": 0, "B": -3, "C": 2})
    assert final == {"C": 2}
    assert trimmed == []


# ---------------------------------------------------------------------------
# CartStore
# ---------------------------------------------------------------------------
def test_merge_into_moves_guest_cart_and_clears_it(store):
    store.put("guest-1", {"A": 2, "B": 1})
    store.put("member-1", {"A": 1})

    final, trimmed = store.merge_into("guest-1", "member-1")

    assert final == {"A": 3, "B": 1}
    assert trimmed == []
    assert store.get("guest-1") == {}, "未ログインカートは統合後に消す"
    assert store.get("member-1") == {"A": 3, "B": 1}


def test_merge_into_reports_trimmed_skus(store):
    store.put("guest-1", {"A": MAX})
    store.put("member-1", {"A": MAX})
    final, trimmed = store.merge_into("guest-1", "member-1")
    assert final == {"A": MAX}
    assert trimmed == ["A"]


def test_add_line_accumulates_then_clamps(store):
    for _ in range(MAX + 3):
        store.add_line("c1", "A", 1)
    assert store.get("c1") == {"A": MAX}


def test_set_line_to_zero_removes_it(store):
    store.put("c1", {"A": 2, "B": 1})
    assert store.set_line("c1", "A", 0) == {"B": 1}


# ---------------------------------------------------------------------------
# BR-23 / FR-310: 保持期間は「最終更新から」30 日
# ---------------------------------------------------------------------------
def test_ttl_is_set_on_write(store):
    store.put("c1", {"A": 1})
    assert 0 < store.ttl("c1") <= carts.CART_TTL_SECONDS


def test_ttl_is_refreshed_on_every_write(store):
    """**「作成から 30 日」ではなく「最終更新から 30 日」。**

    作成時に一度だけ設定すると、使い続けているカートが突然消える。
    BR-23 は前者ではなく後者を指している。
    """
    store.put("c1", {"A": 1})
    store.r.expire("cart:c1", 100)  # 時間が経ったことにする
    assert store.ttl("c1") <= 100

    store.add_line("c1", "B", 1)
    assert store.ttl("c1") > 100, "書き込みで TTL が引き直されること"


def test_empty_cart_is_deleted_not_kept_with_ttl(store):
    store.put("c1", {"A": 1})
    store.put("c1", {})
    assert store.get("c1") == {}
    assert store.ttl("c1") == -2, "空になったキーは残さない"
