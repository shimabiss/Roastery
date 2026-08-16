"""
Saga (補償トランザクション) の枠組みのユニットテスト。

**BR-07 が試験対象**である。
「在庫を戻すのは、与信の取り消しが成功した後のみ」というルールは、
コードだけ読んでも守られているか分からない。テストで固定する。
"""

import pytest

from conftest import load

Saga = load("order-api", "saga").Saga


def _recorder():
    """実行された補償の名前を順に記録するヘルパ。"""
    calls: list[str] = []

    def make(name: str, fail: bool = False):
        async def undo() -> None:
            calls.append(name)
            if fail:
                raise RuntimeError(f"{name} failed")

        return undo

    return calls, make


# ---------------------------------------------------------------------------
# 基本: 逆順で巻き戻す
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_compensations_run_in_reverse_order():
    calls, make = _recorder()
    saga = Saga()
    saga.push("release-inventory", make("release-inventory"))
    saga.push("void-payment", make("void-payment"))

    await saga.compensate()

    # 在庫引当 → 与信 の順で実行したので、与信取消 → 在庫解放 の順で戻る
    assert calls == ["void-payment", "release-inventory"]


@pytest.mark.asyncio
async def test_clear_prevents_compensation():
    calls, make = _recorder()
    saga = Saga()
    saga.push("release-inventory", make("release-inventory"))
    saga.clear()
    await saga.compensate()
    assert calls == []


# ---------------------------------------------------------------------------
# 補償が失敗しても、依存していない残りは続ける
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_independent_compensations_continue_after_failure():
    calls, make = _recorder()
    saga = Saga()
    saga.push("a", make("a"))
    saga.push("b", make("b", fail=True))
    saga.push("c", make("c"))

    await saga.compensate()

    assert calls == ["c", "b", "a"], "b が失敗しても a は実行される"
    assert saga.compensation_failures == ["b"]
    assert saga.needs_manual_intervention is True


# ---------------------------------------------------------------------------
# BR-07: 在庫を戻すのは、与信の取り消しが成功した後のみ
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_inventory_release_is_skipped_when_void_fails():
    """**このテストが BR-07 そのもの。**

    与信の取り消しに失敗したまま在庫だけ戻すと、
    「代金は押さえられているのに在庫は売れる状態」になる。
    在庫と金銭の食い違いのうち、金銭側を守るという判断。
    """
    calls, make = _recorder()
    saga = Saga()
    saga.push("release-inventory", make("release-inventory"), requires="void-payment")
    saga.push("void-payment", make("void-payment", fail=True))

    await saga.compensate()

    assert calls == ["void-payment"], "与信取消が失敗したら在庫は戻さない"
    assert saga.compensation_failures == ["void-payment"]
    assert saga.compensation_skipped == ["release-inventory"]
    assert saga.needs_manual_intervention is True


@pytest.mark.asyncio
async def test_inventory_release_runs_when_void_succeeds():
    calls, make = _recorder()
    saga = Saga()
    saga.push("release-inventory", make("release-inventory"), requires="void-payment")
    saga.push("void-payment", make("void-payment"))

    await saga.compensate()

    assert calls == ["void-payment", "release-inventory"]
    assert saga.needs_manual_intervention is False


@pytest.mark.asyncio
async def test_inventory_release_runs_when_there_was_no_authorization():
    """与信を取る前に失敗した場合。

    取り消す与信が無いので ``void-payment`` は積まれない。
    このとき在庫の解放は **実行されなければならない** (UC-01 E2 9a)。
    requires の相手が積まれていないだけで飛ばしてしまうと、
    在庫が引き当てられたまま永久に戻らない。
    """
    calls, make = _recorder()
    saga = Saga()
    saga.push("release-inventory", make("release-inventory"), requires="void-payment")

    await saga.compensate()

    assert calls == ["release-inventory"]
    assert saga.needs_manual_intervention is False
