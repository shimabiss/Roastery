"""
Saga (補償トランザクション) の最小限の枠組み。

なぜ枠組みにするのか
--------------------
補償処理を ``try/except`` の中に散らして書くと、以下が必ず起きる。

  - 補償の順序が呼び出しの逆順になっていない
  - 途中で失敗したとき、どこまで補償すべきか分からなくなる
  - **補償自体が失敗したときの扱いが each で違う**

「実行した操作を積んでおき、失敗したら逆順に巻き戻す」という形にすると、
この3つが1箇所で解ける。

UC-06 との違い (重要)
---------------------
**この枠組みが使えるのは UC-01 までである。**
UC-06 (出荷) は梱包という物理作業を含むため、巻き戻せない。
そちらは「前進復旧」——失敗しても後退せず、リトライか人手で完了させる——になる。

  Saga = 補償トランザクション、と説明されることが多いが、
  **物理作業が混ざった瞬間に補償は使えなくなる。**
"""

import logging
from typing import Awaitable, Callable

from opentelemetry import trace

log = logging.getLogger("order-api.saga")
tracer = trace.get_tracer("order-api")

Compensation = Callable[[], Awaitable[None]]


class SagaFailed(Exception):
    """業務上の失敗。補償は完了している。"""

    def __init__(self, step: str, detail, status_code: int = 409):
        super().__init__(f"{step}: {detail}")
        self.step = step
        self.detail = detail
        self.status_code = status_code


class Saga:
    """実行した操作の補償を積み上げ、失敗時に逆順で巻き戻す。

    使い方::

        saga = Saga()
        await do_something()
        saga.push("release-stock", lambda: release())
        ...
        # 例外が出たら
        await saga.compensate()
    """

    def __init__(self) -> None:
        self._steps: list[tuple[str, Compensation, str | None]] = []
        self.compensation_failures: list[str] = []
        self.compensation_skipped: list[str] = []

    def push(self, name: str, undo: Compensation, requires: str | None = None) -> None:
        """補償を積む。

        Args:
            requires: この名前の補償が成功したときにだけ実行する。
                BR-07「在庫を戻すのは、与信の取り消しが成功した後のみ」のように、
                **補償どうしに前提条件がある**場合に使う。
        """
        self._steps.append((name, undo, requires))

    async def compensate(self) -> None:
        """積んだ補償を **逆順に** 実行する。

        順序が逆でなければならないのは、後の操作が前の操作に依存しているため。
        在庫引当 → 与信 の順で実行したなら、与信取消 → 在庫解放 の順で戻す。

        補償が失敗しても、**依存していない残りの補償は続ける**。
        ここで全部止めると、巻き戻せたはずのものまで残ってしまう。
        ただし ``requires`` で前提を宣言したものは飛ばす。
        """
        with tracer.start_as_current_span("saga.compensate") as span:
            span.set_attribute("saga.step_count", len(self._steps))
            for name, undo, requires in reversed(self._steps):
                if requires and requires in self.compensation_failures:
                    # BR-07: 前提の補償が失敗したので、これは実行してはならない。
                    # 「在庫だけ戻って与信が生きている」= 在庫と金銭の食い違いを防ぐ
                    self.compensation_skipped.append(name)
                    span.add_event(
                        "saga.compensation_skipped",
                        {"saga.step": name, "saga.requires": requires},
                    )
                    log.error("compensation SKIPPED step=%s (requires %s failed)", name, requires)
                    continue
                try:
                    with tracer.start_as_current_span(f"compensate.{name}"):
                        await undo()
                    log.info("compensated step=%s", name)
                except Exception as exc:  # noqa: BLE001
                    self.compensation_failures.append(name)
                    span.add_event("saga.compensation_failed", {"saga.step": name})
                    log.error("compensation FAILED step=%s: %s", name, exc)
            if self.compensation_failures:
                span.set_attribute(
                    "saga.compensation_failures", ",".join(self.compensation_failures)
                )
            if self.compensation_skipped:
                span.set_attribute(
                    "saga.compensation_skipped", ",".join(self.compensation_skipped)
                )
        self._steps.clear()

    @property
    def needs_manual_intervention(self) -> bool:
        """人手の対応が要る状態か。**最も重要なアラート条件。**"""
        return bool(self.compensation_failures or self.compensation_skipped)

    def clear(self) -> None:
        """成功したので巻き戻す必要が無くなった。"""
        self._steps.clear()
