"""
在庫操作のユニットテスト。

**このテストが CI の主役**である。FR-608 / BR-14「引当可能在庫は負にならない」は
要件として明示されて初めて欠陥だと分かったもので、
**要件 → テスト → 実装** の順に降りてきた最も分かりやすい例になっている。

fakeredis を使うので Redis を起動しなくても走る。CI が速いほど回る。
"""

from concurrent.futures import ThreadPoolExecutor

import fakeredis
import pytest

from conftest import load

StockOps = load("inventory-api", "stock").StockOps


@pytest.fixture()
def ops():
    r = fakeredis.FakeStrictRedis(decode_responses=True)
    r.set("stock:A", 10)
    r.set("stock:B", 3)
    r.set("stock:C", 0)
    return StockOps(r)


# ---------------------------------------------------------------------------
# 用語の区別 (05-domain-model.md)
# ---------------------------------------------------------------------------
def test_available_is_physical_minus_reserved(ops):
    """BR-13: 利用者に見せるのは引当可能在庫であり、実在庫ではない。"""
    assert ops.available("A") == 10
    ops.reserve([("A", 4)])
    assert ops.physical("A") == 10, "引当では実在庫は動かない"
    assert ops.reserved("A") == 4
    assert ops.available("A") == 6


# ---------------------------------------------------------------------------
# BR-03 / BR-04: 全明細が引き当てられるか、1つも引き当てないか
# ---------------------------------------------------------------------------
def test_multi_line_reserve_is_all_or_nothing(ops):
    ok, bad = ops.reserve([("A", 2), ("B", 99), ("C", 1)])
    assert ok is False
    assert bad == "B"
    # 1件目の A も引き当てられていないこと。ここが「全部か無か」の実体
    assert ops.reserved("A") == 0
    assert ops.reserved("B") == 0


def test_multi_line_reserve_success(ops):
    ok, bad = ops.reserve([("A", 2), ("B", 3)])
    assert (ok, bad) == (True, None)
    assert ops.available("A") == 8
    assert ops.available("B") == 0


# ---------------------------------------------------------------------------
# FR-608 / BR-14: 同時実行でも引当可能在庫は負にならない
# ---------------------------------------------------------------------------
def test_concurrent_reserve_never_oversells(ops):
    """在庫1に対して 20 並列で1つずつ引き当て、成功が1件だけであることを確かめる。

    修正前の「GET → 判定 → DECRBY」実装では、ここで2件以上成功して
    引当可能在庫がマイナスになった。
    """
    ops.r.set("stock:D", 1)

    def attempt(_):
        ok, _bad = ops.reserve([("D", 1)])
        return ok

    with ThreadPoolExecutor(max_workers=20) as ex:
        results = list(ex.map(attempt, range(20)))

    assert sum(results) == 1, "在庫1に対して成功は1件だけであるべき"
    assert ops.available("D") == 0
    assert ops.available("D") >= 0, "引当可能在庫は負にならない (BR-14)"


def test_reserve_rejects_when_only_reserved_blocks_it(ops):
    """実在庫はあるが全部引当済み、というケース。

    実在庫だけを見ていると通ってしまう。**引当済数を持っていないと表現できない状態。**
    """
    ops.reserve([("B", 3)])
    assert ops.physical("B") == 3
    ok, bad = ops.reserve([("B", 1)])
    assert (ok, bad) == (False, "B")


# ---------------------------------------------------------------------------
# FR-603: 引当の解放 (補償トランザクション)
# ---------------------------------------------------------------------------
def test_release_restores_available(ops):
    ops.reserve([("A", 4)])
    ops.release([("A", 4)])
    assert ops.reserved("A") == 0
    assert ops.available("A") == 10


def test_release_is_idempotent(ops):
    """補償処理は「失敗したらもう一度呼ぶ」が基本。冪等でないと使えない。"""
    ops.reserve([("A", 4)])
    ops.release([("A", 4)])
    ops.release([("A", 4)])
    ops.release([("A", 4)])
    assert ops.reserved("A") == 0, "二重解放でマイナスに落ちてはいけない"
    assert ops.available("A") == 10


# ---------------------------------------------------------------------------
# UC-06: 出荷確定
# ---------------------------------------------------------------------------
def test_ship_reduces_physical_and_reserved_but_not_available(ops):
    """在庫遷移表 (05-domain-model.md 4節) の「出荷確定」の行。

    **出荷しても引当可能在庫は変化しない。** 既に引当で減らしてあるため。
    """
    ops.reserve([("A", 4)])
    before = ops.available("A")
    ok, _ = ops.ship([("A", 4)])
    assert ok is True
    assert ops.physical("A") == 6
    assert ops.reserved("A") == 0
    assert ops.available("A") == before, "出荷確定で引当可能在庫は動かない"


def test_ship_fails_when_physical_missing(ops):
    """BR-29: 引当済数があっても実在庫があるとは限らない。

    棚卸差異・破損・紛失。UC-06 A2 / E3 のシナリオ。
    """
    ops.reserve([("A", 4)])
    ops.adjust_physical("A", -8)  # 棚に2つしか残っていなかった
    ok, bad = ops.ship([("A", 4)])
    assert (ok, bad) == (False, "A")


def test_adjust_physical_clamps_at_zero(ops):
    assert ops.adjust_physical("C", -5) == 0


# ---------------------------------------------------------------------------
# 同一 SKU が複数明細に現れる場合 (レビューで見つかった欠陥)
# ---------------------------------------------------------------------------
def test_duplicate_sku_in_one_request_cannot_oversell(ops):
    """**「全件を検査してから全件を更新」だけでは足りない。**

    検査がループ前の値を見るため、同じ SKU の2行目が1行目の引当を見落とす。
    在庫5 に対して [A×3, A×3] を投げると、素朴な実装では両方が
    「5-0 >= 3」を通り、引当済が 6 になって在庫を超える。
    """
    ops.r.set("stock:E", 5)
    ok, bad = ops.reserve([("E", 3), ("E", 3)])
    assert ok is False, "同一 SKU の合計が在庫を超えたら失敗すること"
    assert bad == "E"
    assert ops.reserved("E") == 0, "1つも引き当てられていないこと (BR-03)"
    assert ops.available("E") == 5


def test_duplicate_sku_within_capacity_succeeds(ops):
    ops.r.set("stock:E", 5)
    ok, _ = ops.reserve([("E", 2), ("E", 3)])
    assert ok is True
    assert ops.reserved("E") == 5
    assert ops.available("E") == 0


def test_duplicate_sku_on_ship_is_bounded_by_reserved(ops):
    ops.r.set("stock:E", 5)
    ops.reserve([("E", 3)])
    ok, bad = ops.ship([("E", 2), ("E", 2)])
    assert (ok, bad) == (False, "E"), "引当済 3 に対して合計 4 は出荷できない"
    assert ops.reserved("E") == 3, "失敗時は何も動かさない"
