"""
注文の状態遷移のユニットテスト。

**BR-05 / BR-06 が試験対象。** 状態遷移図 (05-domain-model.md 4節) を
そのままコードに落としたものが正しいかを固定する。
"""

import pytest

from conftest import load

st = load("order-api", "states")


# ---------------------------------------------------------------------------
# 表そのものの健全性
# ---------------------------------------------------------------------------
def test_every_state_has_a_label():
    """状態を足してラベルを足し忘れる、は必ず起きる。"""
    for state in st.ALLOWED:
        assert state in st.LABELS, f"{state} のラベルが無い"


def test_transitions_only_point_to_known_states():
    for src, targets in st.ALLOWED.items():
        for target in targets:
            assert target in st.ALLOWED, f"{src} -> {target} の遷移先が未定義"


def test_terminal_states_have_no_outgoing_transitions():
    for state in st.TERMINAL:
        assert st.reachable_from(state) == set(), f"{state} は終端のはず"


# ---------------------------------------------------------------------------
# BR-05 / BR-06: キャンセルできるのは「受付済」のみ
# ---------------------------------------------------------------------------
def test_cancel_allowed_only_in_accepted():
    assert st.can_cancel(st.ACCEPTED) is True
    for state in [st.PREPARING, st.SHIPPED, st.DELIVERED, st.CANCELLED, st.RETURNED, st.FAILED]:
        assert st.can_cancel(state) is False, f"{state} でキャンセルできてはいけない"


def test_preparing_blocks_customer_cancellation():
    """**BR-06 の実体。**

    「出荷処理の開始と同時に」という曖昧な表現を、
    「出荷準備中という状態に入った時点で」に置き換えたのがこの1行。
    """
    assert st.can_cancel(st.PREPARING) is False


# ---------------------------------------------------------------------------
# 正常な流れ
# ---------------------------------------------------------------------------
def test_happy_path():
    chain = [st.ACCEPTED, st.PREPARING, st.SHIPPED, st.DELIVERED, st.RETURNED]
    for current, target in zip(chain, chain[1:]):
        st.assert_transition(current, target)


def test_accepted_can_be_cancelled():
    st.assert_transition(st.ACCEPTED, st.CANCELLED)


def test_preparing_can_be_cancelled_for_physical_shortage():
    """UC-06 E3。引当済でも実物が無ければ、注文全体を取り消すしかない。

    分割出荷を扱わない (FR-410 は Won't) ため、
    **明細単位で一部だけ出荷することはできない。**
    """
    st.assert_transition(st.PREPARING, st.CANCELLED)


# ---------------------------------------------------------------------------
# 禁じられた遷移
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "current, target",
    [
        (st.SHIPPED, st.CANCELLED),    # 出荷後はキャンセルできない。返品の世界になる
        (st.SHIPPED, st.PREPARING),    # 巻き戻さない (BR-31)
        (st.DELIVERED, st.CANCELLED),
        (st.CANCELLED, st.ACCEPTED),
        (st.RETURNED, st.SHIPPED),
        (st.ACCEPTED, st.SHIPPED),     # 準備中を飛ばせない
        (st.ACCEPTED, st.DELIVERED),
        (st.FAILED, st.ACCEPTED),
    ],
)
def test_illegal_transitions(current, target):
    assert st.can_transition(current, target) is False
    with pytest.raises(st.IllegalTransition):
        st.assert_transition(current, target)


def test_shipped_cannot_go_back_to_preparing():
    """**BR-31: 出荷確定後の巻き戻しは行わない。**

    UC-01 は「すべて成功するかすべて元に戻る」だったが、
    UC-06 は元に戻せない。梱包した箱は元に戻らないからである。
    誤りは返品 (UC-03) または在庫調整 (FR-606) で対応する。
    """
    assert st.can_transition(st.SHIPPED, st.PREPARING) is False


def test_illegal_transition_message_is_in_japanese():
    """運用担当者が読むメッセージなので、状態名は日本語で出す。"""
    with pytest.raises(st.IllegalTransition) as exc:
        st.assert_transition(st.SHIPPED, st.CANCELLED)
    assert "出荷済" in str(exc.value)
    assert "キャンセル済" in str(exc.value)
