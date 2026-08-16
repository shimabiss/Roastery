"""
カートの保持とマージ。

なぜ member-api がカートを持つのか (正直な注記)
----------------------------------------------
ドメインモデル上、**カートは会員を持たないことがある** (多重度 0..1)。
注文が必ず会員を持つ (1) のとは非対称で、本来は別の境界に置くのが素直。

それでも同居させているのは、**マージ (FR-306) が会員とカートの両方を触る**ためと、
サービスを増やすと Azure Container Apps のコンテナと IaC のコストが上がるため。
**設計として正しいからではなく、コストを優先した判断**である。
分けるべきだと感じたら、ここが最初の分割候補になる。

保持期間について (BR-23 / FR-310)
---------------------------------
未ログインカートは 30 日で消える。実装は **Redis の TTL** で行う。

07-implementation-scope.md では FR-1301 (期限切れカートの削除) を
定期実行処理として書いたが、**TTL を使うとバッチが要らない。**
FR-118 を「登録時の上書き」で満たして FR-1303 が消えたのと同じ構図で、
**要件は「何を満たすか」であって「どう作るか」ではない。**
"""

# BR-23 / FR-310
CART_TTL_SECONDS = 30 * 24 * 60 * 60

# BR-22 / FR-311: 1明細あたりの数量上限。
# 「合算する」と決めたことで初めて必要になった概念。
# 上書きを選んでいれば、この定数は存在しない。
MAX_QTY_PER_LINE = 10


def merge_quantities(guest: dict[str, int], member: dict[str, int]) -> dict[str, int]:
    """BR-20 / FR-306: 未ログインカートを会員カートへ統合する。

    **同一 SKU は数量を合算する。** 上限 (BR-22) を超えたら上限でクランプする。

    合算を選んだ理由は「入れたものが消える」のが最も納得しにくいため。
    ただし合算には上限の問題が付いてきた。
    **「合算する」という一行の決定が、それまで存在しなかった
    「1明細の数量上限」という概念を要求している。**

    純粋関数にしてあるので、Redis なしでテストできる。
    """
    merged = dict(member)
    for sku, qty in guest.items():
        merged[sku] = merged.get(sku, 0) + qty

    clamped = {}
    for sku, qty in merged.items():
        if qty <= 0:
            continue
        clamped[sku] = min(qty, MAX_QTY_PER_LINE)
    return clamped


def clamped_lines(quantities: dict[str, int]) -> tuple[dict[str, int], list[str]]:
    """上限で切り詰めた結果と、**切り詰めた SKU の一覧** を返す。

    切り詰めたことを利用者に伝えないと「勝手に数量が変わった」ことになる。
    US-14 の受入基準「上限に切り詰めたことが表示される」に対応する。
    """
    out, trimmed = {}, []
    for sku, qty in quantities.items():
        if qty > MAX_QTY_PER_LINE:
            trimmed.append(sku)
            out[sku] = MAX_QTY_PER_LINE
        elif qty > 0:
            out[sku] = qty
    return out, trimmed


class CartStore:
    """Redis のハッシュ1本でカートを持つ。``cart:<id>`` -> {sku: qty}"""

    def __init__(self, redis_client):
        self.r = redis_client

    @staticmethod
    def _key(cart_id: str) -> str:
        return f"cart:{cart_id}"

    def get(self, cart_id: str) -> dict[str, int]:
        if not cart_id:
            return {}
        raw = self.r.hgetall(self._key(cart_id))
        return {k: int(v) for k, v in raw.items() if int(v) > 0}

    def put(self, cart_id: str, quantities: dict[str, int]) -> dict[str, int]:
        """カートを丸ごと書き換え、TTL を引き直す。

        **書き込みのたびに TTL を延ばす**ので、「最終更新から 30 日」になる。
        作成時に一度だけ設定すると「作成から 30 日」になってしまい、
        使い続けているカートが突然消える。BR-23 は前者を指している。
        """
        key = self._key(cart_id)
        cleaned, _ = clamped_lines(quantities)
        pipe = self.r.pipeline()
        pipe.delete(key)
        if cleaned:
            pipe.hset(key, mapping={k: str(v) for k, v in cleaned.items()})
            pipe.expire(key, CART_TTL_SECONDS)
        pipe.execute()
        return cleaned

    def set_line(self, cart_id: str, sku: str, qty: int) -> dict[str, int]:
        current = self.get(cart_id)
        if qty <= 0:
            current.pop(sku, None)
        else:
            current[sku] = min(qty, MAX_QTY_PER_LINE)
        return self.put(cart_id, current)

    def add_line(self, cart_id: str, sku: str, qty: int = 1) -> dict[str, int]:
        current = self.get(cart_id)
        current[sku] = current.get(sku, 0) + qty
        return self.put(cart_id, current)

    def clear(self, cart_id: str) -> None:
        self.r.delete(self._key(cart_id))

    def merge_into(self, guest_id: str, member_cart_id: str) -> tuple[dict[str, int], list[str]]:
        """ログイン時のマージ (FR-306)。未ログイン側は消す。

        Returns:
            ``(統合後の数量, 上限で切り詰めた SKU)``
        """
        guest = self.get(guest_id)
        member = self.get(member_cart_id)
        merged = dict(member)
        for sku, qty in guest.items():
            merged[sku] = merged.get(sku, 0) + qty
        final, trimmed = clamped_lines(merged)
        self.put(member_cart_id, final)
        if guest_id and guest_id != member_cart_id:
            self.clear(guest_id)
        return final, trimmed

    def ttl(self, cart_id: str) -> int:
        return int(self.r.ttl(self._key(cart_id)))
