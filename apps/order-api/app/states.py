"""
注文の状態遷移 (05-domain-model.md 4節)。

なぜ表にするのか
----------------
遷移の可否を ``if`` の連なりで書くと、必ずどこかに抜けが出る。
**許す遷移を列挙し、それ以外は全部禁止**にすると、抜けが「書き忘れ」として現れる。

「出荷準備中」について
----------------------
BR-06 は当初「**出荷処理の開始と同時に**キャンセル不可になる」だった。
では「開始」とはいつか。画面で選んだ瞬間か、梱包を始めた瞬間か、
売上を確定した瞬間か。**UC-02 を書いた時点では、この曖昧さに気づいていない。**

UC-06 を書いて「出荷準備中」という状態を置いたことで、
境界が「いつ」ではなく「どの状態か」で表現できるようになった。

  業務ルールに「〜と同時に」「〜のタイミングで」と書きたくなったら、
  **その時点を表す状態が足りていない**ことを疑う。
"""

# 状態
ACCEPTED = "ACCEPTED"      # 受付済: 与信済み・出荷処理未着手。キャンセルできる
PREPARING = "PREPARING"    # 出荷準備中: 運用担当者が着手した。キャンセルできない
SHIPPED = "SHIPPED"        # 出荷済: 売上が確定し、実在庫が減った
DELIVERED = "DELIVERED"    # 配達完了
CANCELLED = "CANCELLED"    # キャンセル済 (終端)
RETURNED = "RETURNED"      # 返品済 (終端)
FAILED = "FAILED"          # 失敗 (終端)

TERMINAL = {CANCELLED, RETURNED, FAILED}

# 許す遷移だけを列挙する。ここに無いものはすべて禁止。
ALLOWED: dict[str, set[str]] = {
    ACCEPTED: {PREPARING, CANCELLED},
    # UC-06 E3: 引当済なのに実在庫が無い場合だけ、準備中からキャンセルへ戻れる。
    # **これは「巻き戻し」ではなく、業務としての取り消し**である。
    # 梱包前に気づいた場合に限る、という前提が裏にある。
    PREPARING: {SHIPPED, CANCELLED},
    SHIPPED: {DELIVERED},
    DELIVERED: {RETURNED},
    CANCELLED: set(),
    RETURNED: set(),
    FAILED: set(),
}

# 利用者に見せる日本語名
LABELS = {
    ACCEPTED: "受付済",
    PREPARING: "出荷準備中",
    SHIPPED: "出荷済",
    DELIVERED: "配達完了",
    CANCELLED: "キャンセル済",
    RETURNED: "返品済",
    FAILED: "失敗",
}


class IllegalTransition(ValueError):
    def __init__(self, current: str, target: str):
        super().__init__(
            f"{LABELS.get(current, current)} から {LABELS.get(target, target)} へは遷移できません"
        )
        self.current = current
        self.target = target


def can_transition(current: str, target: str) -> bool:
    return target in ALLOWED.get(current, set())


def assert_transition(current: str, target: str) -> None:
    if not can_transition(current, target):
        raise IllegalTransition(current, target)


def can_cancel(current: str) -> bool:
    """BR-05 / BR-06。

    **「出荷準備中」に入った時点でキャンセル不可**になる。
    利用者から見たキャンセル可否はこれ1本で判定する。
    運用担当者が実在庫不足で取り消す (UC-06 E3) のは別の操作であり、
    同じ「キャンセル」という言葉でも入口が違う。
    """
    return current == ACCEPTED


def is_terminal(current: str) -> bool:
    return current in TERMINAL


def reachable_from(current: str) -> set[str]:
    return set(ALLOWED.get(current, set()))
