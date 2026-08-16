"""
在庫操作の中核。**Redis の Lua スクリプトによる原子的な引当**。

なぜ Lua なのか
---------------
修正前の実装はこうだった::

    current = int(r.get(f"stock:{sku}") or 0)   # 読む
    if current < qty:                            # 判定する
        raise HTTPException(409)
    remaining = r.decrby(f"stock:{sku}", qty)    # 減らす

読んでから減らすまでの間に別のリクエストが割り込むと、
**両方が判定を通過して在庫がマイナスになる。** (BR-14 / FR-608)

Redis は Lua スクリプトを1つの原子的な単位として実行するため、
「判定と更新の間に誰も割り込めない」ことが保証される。

用語との対応 (docs/requirements/05-domain-model.md)
--------------------------------------------------
==================  ==========================  ==================================
用語                キー                        意味
==================  ==========================  ==================================
実在庫              ``stock:<SKU>``             倉庫に物理的にある数
引当済数            ``reserved:<SKU>``          注文で確保済み・未出荷の数
引当可能在庫        (計算値) 実在庫 − 引当済数  **利用者に見せるのはこれ** (BR-13)
==================  ==========================  ==================================

修正前の実装には「引当済数」という概念が無く、注文時に実在庫を直接減らしていた。
そのため「注文されたが未出荷」と「出荷済み」が区別できず、
**決済に失敗しても戻す先が無かった。**
"""

# ---------------------------------------------------------------------------
# 引当 (FR-602)
#   KEYS: stock:<sku1>, reserved:<sku1>, stock:<sku2>, reserved:<sku2>, ...
#   ARGV: qty1, qty2, ...
#   返り値: {1}            成功
#           {0, index}     index 番目 (1 始まり) の明細が引当不能
#
# BR-03 / BR-04: **全明細が引き当てられるか、1つも引き当てないか**。
# 先に全件を検査し、1件でも足りなければ何も書き換えずに戻る。
# これを「先に検査、後で更新」の順で1スクリプトに閉じ込めているのが要点。
# ---------------------------------------------------------------------------
# **同一 SKU が複数明細に現れる場合に注意。**
# 素朴に「全件を検査してから全件を更新」と書くと、検査がループ前の値を見るため
# 同じ SKU の2行目が1行目の引当を見落とし、合計が在庫を超える。
#   例: 在庫5 に対して [A×3, A×3] -> 両方が「5-0>=3」を通り、引当済が 6 になる
# 累積 (want_total) を持って判定することで防ぐ。
RESERVE_LUA = """
local n = #ARGV
local want_total = {}
for i = 1, n do
  local skey = KEYS[i*2-1]
  local physical = tonumber(redis.call('GET', skey) or '0')
  local reserved = tonumber(redis.call('GET', KEYS[i*2]) or '0')
  local want     = tonumber(ARGV[i])
  want_total[skey] = (want_total[skey] or 0) + want
  if (physical - reserved) < want_total[skey] then
    return {0, i}
  end
end
for i = 1, n do
  redis.call('INCRBY', KEYS[i*2], ARGV[i])
end
return {1, 0}
"""

# ---------------------------------------------------------------------------
# 引当の解放 (FR-603) — 補償トランザクションの本体
#   決済失敗・キャンセル時に引当済数を戻す。実在庫は動かさない。
#   0 未満に落ちないようクランプする (二重解放されても壊れないように)
# ---------------------------------------------------------------------------
RELEASE_LUA = """
local n = #ARGV
for i = 1, n do
  local key  = KEYS[i]
  local cur  = tonumber(redis.call('GET', key) or '0')
  local back = tonumber(ARGV[i])
  local next_val = cur - back
  if next_val < 0 then next_val = 0 end
  redis.call('SET', key, next_val)
end
return 1
"""

# ---------------------------------------------------------------------------
# 出荷確定 (UC-06 手順9)
#   実在庫 −n、引当済数 −n。**引当可能在庫は変化しない** (既に引当で減らしてある)
#   引当済数が足りない場合は 0 を返して呼び出し側に判断させる
# ---------------------------------------------------------------------------
SHIP_LUA = """
local n = #ARGV
local want_total = {}
for i = 1, n do
  local skey = KEYS[i*2-1]
  local physical = tonumber(redis.call('GET', skey) or '0')
  local reserved = tonumber(redis.call('GET', KEYS[i*2]) or '0')
  local want     = tonumber(ARGV[i])
  want_total[skey] = (want_total[skey] or 0) + want
  if reserved < want_total[skey] or physical < want_total[skey] then
    return {0, i}
  end
end
for i = 1, n do
  redis.call('DECRBY', KEYS[i*2-1], ARGV[i])
  redis.call('DECRBY', KEYS[i*2],   ARGV[i])
end
return {1, 0}
"""


class StockOps:
    """Lua スクリプトを登録して呼び出すだけの薄いラッパー。

    テストから直接使えるよう、FastAPI にも OpenTelemetry にも依存させていない。
    **依存を切っておくと、テストのために Redis 以外を起動しなくて済む。**
    """

    def __init__(self, redis_client):
        self.r = redis_client
        self._reserve = redis_client.register_script(RESERVE_LUA)
        self._release = redis_client.register_script(RELEASE_LUA)
        self._ship = redis_client.register_script(SHIP_LUA)

    # -- 参照 ---------------------------------------------------------------
    def physical(self, sku: str) -> int:
        return int(self.r.get(f"stock:{sku}") or 0)

    def reserved(self, sku: str) -> int:
        return int(self.r.get(f"reserved:{sku}") or 0)

    def available(self, sku: str) -> int:
        """引当可能在庫 = 実在庫 − 引当済数。BR-13 により、これを利用者に見せる。"""
        return self.physical(sku) - self.reserved(sku)

    # -- 更新 ---------------------------------------------------------------
    def reserve(self, lines: list[tuple[str, int]]) -> tuple[bool, str | None]:
        """全明細をまとめて引き当てる。

        Returns:
            ``(True, None)`` 成功。``(False, sku)`` 引当不能だった SKU。
        """
        if not lines:
            return True, None
        keys, args = [], []
        for sku, qty in lines:
            keys += [f"stock:{sku}", f"reserved:{sku}"]
            args.append(qty)
        ok, idx = self._reserve(keys=keys, args=args)
        if int(ok) == 1:
            return True, None
        return False, lines[int(idx) - 1][0]

    def release(self, lines: list[tuple[str, int]]) -> None:
        """引当を解放する。**何度呼んでも安全** (0 でクランプする)。

        補償処理は「失敗したらもう一度呼ぶ」が基本なので、
        冪等でないと使い物にならない。
        """
        if not lines:
            return
        keys = [f"reserved:{sku}" for sku, _ in lines]
        args = [qty for _, qty in lines]
        self._release(keys=keys, args=args)

    def ship(self, lines: list[tuple[str, int]]) -> tuple[bool, str | None]:
        """出荷確定。実在庫と引当済数の両方を減らす。"""
        if not lines:
            return True, None
        keys, args = [], []
        for sku, qty in lines:
            keys += [f"stock:{sku}", f"reserved:{sku}"]
            args.append(qty)
        ok, idx = self._ship(keys=keys, args=args)
        if int(ok) == 1:
            return True, None
        return False, lines[int(idx) - 1][0]

    def adjust_physical(self, sku: str, delta: int) -> int:
        """棚卸差異の補正・入荷 (FR-605 / FR-606)。実在庫だけを動かす。"""
        new = self.r.incrby(f"stock:{sku}", delta)
        if new < 0:
            self.r.set(f"stock:{sku}", 0)
            new = 0
        return int(new)
