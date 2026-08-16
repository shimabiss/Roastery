"""
inventory-api : 在庫の参照・引当・解放・出荷確定を担当するサービス。

P1 で変わったこと
-----------------
1. **「引当済数」を導入した** (FR-602 / FR-603)。
   修正前は注文時に実在庫を直接 ``DECRBY`` していたため、
   「注文されたが未出荷」と「出荷済み」が区別できず、
   決済に失敗しても戻す先が無かった。
2. **引当を原子的にした** (FR-608 / BR-14)。詳細は ``app/stock.py``。
3. **複数明細をまとめて引き当てられるようにした** (BR-03 / BR-04)。
   全部引き当てるか、1つも引き当てないか。

学習ポイント:
  - Redis の自動計装。アプリのコードを変えずに Redis アクセスが span になる
  - 上流 (order-api) から traceparent が伝播し、同じ trace に載る
  - Lua スクリプトは1つの span にまとまる = 「原子的な操作」がトレース上でも1点に見える
"""

import logging
import os
import time

import redis
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from opentelemetry import trace, metrics
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

from .stock import StockOps

tracer = trace.get_tracer("inventory-api")
meter = metrics.get_meter("inventory-api")

reservations = meter.create_counter(
    "inventory.reservations",
    unit="{reservation}",
    description="在庫引き当ての試行回数",
)
releases = meter.create_counter(
    "inventory.releases",
    unit="{release}",
    description="引当解放 (補償) の回数",
)
shipments = meter.create_counter(
    "inventory.shipments",
    unit="{shipment}",
    description="出荷確定の回数",
)

REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("inventory-api")

r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
ops = StockOps(r)

CATALOG = {
    "COFFEE-BEANS-1KG": {
        "name": "エチオピア イルガチェフェ 1kg",
        "price": 3800,
        "initial_stock": 50,
        "category": "beans",
    },
    "MUG-CERAMIC": {
        "name": "陶器マグ 320ml",
        "price": 1800,
        "initial_stock": 30,
        "category": "goods",
    },
    "DRIP-KETTLE": {
        "name": "ドリップケトル 0.9L",
        "price": 9800,
        "initial_stock": 5,
        "category": "goods",
    },
    "FILTER-100P": {
        "name": "ペーパーフィルター 100枚",
        "price": 600,
        "initial_stock": 200,
        "category": "supplies",
    },
}

app = FastAPI(title="inventory-api")
FastAPIInstrumentor.instrument_app(app, exclude_spans=["receive", "send"])


def _chaos() -> dict:
    slow = r.get("chaos:inventory:slow_ms")
    return {"slow_ms": int(slow) if slow else int(os.getenv("CHAOS_SLOW_MS", "0"))}


def _maybe_slow(span) -> None:
    c = _chaos()
    if c["slow_ms"] > 0:
        span.add_event("chaos.slow_query_injected", {"delay_ms": c["slow_ms"]})
        time.sleep(c["slow_ms"] / 1000)


# ---------------------------------------------------------------------------
# 商品の運用 (FR-1101 / FR-1102)
#
# 商品マスタのテーブルは作っていない。カタログは定数のまま持ち、
# **運用で変わる値 (価格・公開状態) だけを Redis に上書きとして置く。**
# P5 は4つの学習テーマを1つも支えないため、ここに構造を足す価値が薄い。
# 商品マスタが要る規模になったら、この上書きをテーブルに移す。
# ---------------------------------------------------------------------------
def _price_of(sku: str) -> int:
    override = r.get(f"price:{sku}")
    return int(override) if override else CATALOG[sku]["price"]


def _published(sku: str) -> bool:
    return r.get(f"published:{sku}") != "0"


def _view(sku: str) -> dict:
    meta = CATALOG[sku]
    return {
        "sku": sku,
        "name": meta["name"],
        "category": meta["category"],
        "unit_price": _price_of(sku),
        # BR-13: 利用者に見せるのは引当可能在庫
        "stock": ops.available(sku),
        "published": _published(sku),
        # 運用画面のために内訳も返す。公開サイトは "stock" しか使わない
        "physical": ops.physical(sku),
        "reserved": ops.reserved(sku),
    }


@app.on_event("startup")
def seed():
    for attempt in range(30):
        try:
            r.ping()
            break
        except Exception as exc:  # noqa: BLE001
            log.warning("redis not ready (%s/30): %s", attempt + 1, exc)
            time.sleep(2)
    for sku, meta in CATALOG.items():
        r.setnx(f"stock:{sku}", meta["initial_stock"])
        r.setnx(f"reserved:{sku}", 0)
    log.info("inventory seeded")


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


# ===========================================================================
# 参照
# ===========================================================================
@app.get("/inventory")
def list_inventory(include_unpublished: bool = False):
    """FR-1102: 非公開の商品は公開サイトに出さない。

    ``include_unpublished`` は運用画面だけが使う。
    **既定を「出さない」にしてある**のが要点で、
    既定を「出す」にすると、呼び出し側が指定を忘れた瞬間に非公開が漏れる。
    """
    return {
        sku: _view(sku)
        for sku in CATALOG
        if include_unpublished or _published(sku)
    }


@app.get("/inventory/{sku}")
def get_inventory(sku: str):
    if sku not in CATALOG:
        raise HTTPException(status_code=404, detail="unknown sku")
    return _view(sku)


# ===========================================================================
# 引当 / 解放 / 出荷
# ===========================================================================
class Line(BaseModel):
    sku: str
    quantity: int


class LinesRequest(BaseModel):
    lines: list[Line]


def _to_pairs(lines: list[Line]) -> list[tuple[str, int]]:
    for line in lines:
        if line.sku not in CATALOG:
            raise HTTPException(status_code=404, detail=f"unknown sku: {line.sku}")
        if line.quantity <= 0:
            raise HTTPException(status_code=422, detail="quantity must be positive")
    return [(line.sku, line.quantity) for line in lines]


@app.post("/inventory/reserve")
def reserve_bulk(req: LinesRequest):
    """複数明細をまとめて引き当てる。**全部成功するか、1つも引き当てないか** (BR-03)。"""
    pairs = _to_pairs(req.lines)

    with tracer.start_as_current_span("reserve-stock") as span:
        span.set_attribute("inventory.line_count", len(pairs))
        span.set_attribute("inventory.skus", ",".join(s for s, _ in pairs))
        _maybe_slow(span)

        ok, bad_sku = ops.reserve(pairs)

        if not ok:
            span.add_event(
                "inventory.insufficient",
                {"inventory.sku": bad_sku, "inventory.available": ops.available(bad_sku)},
            )
            reservations.add(1, {"result": "insufficient"})
            log.warning("insufficient stock sku=%s", bad_sku)
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "insufficient stock",
                    "sku": bad_sku,
                    "available": ops.available(bad_sku),
                },
            )

        result = {s: _view(s) for s, _ in pairs}
        span.set_attribute("inventory.reserved_ok", True)

    reservations.add(1, {"result": "ok"})
    return {"reserved": [{"sku": s, "quantity": q} for s, q in pairs], "inventory": result}


@app.post("/inventory/release")
def release_bulk(req: LinesRequest):
    """引当を解放する (FR-603)。**補償トランザクションの本体。**

    冪等なので、呼び出し側は「失敗したらもう一度呼ぶ」でよい。
    補償処理が冪等でないと、リトライのたびに状態が壊れる。
    """
    pairs = _to_pairs(req.lines)
    with tracer.start_as_current_span("release-stock") as span:
        span.set_attribute("inventory.line_count", len(pairs))
        span.set_attribute("inventory.skus", ",".join(s for s, _ in pairs))
        # 補償が走ったこと自体をイベントにする。トレース上で「巻き戻した」が見える
        span.add_event("inventory.compensated")
        ops.release(pairs)
    releases.add(1, {})
    log.info("released %s", [(s, q) for s, q in pairs])
    return {"released": [{"sku": s, "quantity": q} for s, q in pairs]}


@app.post("/inventory/ship")
def ship_bulk(req: LinesRequest):
    """出荷確定 (UC-06 手順9)。実在庫と引当済数の両方を減らす。

    **引当可能在庫は変化しない。** 既に引当で減らしてあるため。
    """
    pairs = _to_pairs(req.lines)
    with tracer.start_as_current_span("ship-stock") as span:
        span.set_attribute("inventory.line_count", len(pairs))
        ok, bad_sku = ops.ship(pairs)
        if not ok:
            # BR-29: 引当済数があっても実在庫があるとは限らない
            span.add_event(
                "inventory.physical_shortage",
                {"inventory.sku": bad_sku, "inventory.physical": ops.physical(bad_sku)},
            )
            shipments.add(1, {"result": "physical_shortage"})
            log.error("physical shortage on ship sku=%s", bad_sku)
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "physical stock shortage",
                    "sku": bad_sku,
                    "physical": ops.physical(bad_sku),
                },
            )
    shipments.add(1, {"result": "ok"})
    return {"shipped": [{"sku": s, "quantity": q} for s, q in pairs]}


# ---------------------------------------------------------------------------
# 単一 SKU の引当 (旧 API)。既存のスクリプトや負荷試験のために残す。
# 中身は複数明細版に委譲するだけ。
# ---------------------------------------------------------------------------
class ReserveRequest(BaseModel):
    quantity: int = 1


@app.post("/inventory/{sku}/reserve")
def reserve_single(sku: str, req: ReserveRequest):
    out = reserve_bulk(LinesRequest(lines=[Line(sku=sku, quantity=req.quantity)]))
    view = out["inventory"][sku]
    return {
        "sku": sku,
        "reserved": req.quantity,
        "remaining": view["stock"],
        "unit_price": view["unit_price"],
    }


# ===========================================================================
# 運用 / 管理
# ===========================================================================
class AdjustRequest(BaseModel):
    delta: int


class ProductUpdate(BaseModel):
    unit_price: int | None = None
    published: bool | None = None


@app.put("/inventory/{sku}/product")
def update_product(sku: str, req: ProductUpdate):
    """商品の価格変更と公開・非公開の切替 (FR-1101 / FR-1102)。

    **価格を変えても過去の注文金額は変わらない。**
    注文明細に価格をコピーしてあるため (BR-01)。
    参照するだけの設計だと、ここを触った瞬間に会計が壊れる。
    """
    if sku not in CATALOG:
        raise HTTPException(status_code=404, detail="unknown sku")
    if req.unit_price is not None:
        if req.unit_price <= 0:
            raise HTTPException(status_code=422, detail="価格は正の整数で指定してください")
        r.set(f"price:{sku}", req.unit_price)
    if req.published is not None:
        r.set(f"published:{sku}", "1" if req.published else "0")
    return _view(sku)


@app.post("/inventory/{sku}/adjust")
def adjust(sku: str, req: AdjustRequest):
    """入荷 (FR-605) と棚卸差異の補正 (FR-606)。**実在庫だけ**を動かす。

    UC-06 E3「引当済なのに棚に物が無い」を再現するのにも使う。
    在庫版の障害注入だと考えてよい。
    """
    if sku not in CATALOG:
        raise HTTPException(status_code=404, detail="unknown sku")
    with tracer.start_as_current_span("adjust-stock") as span:
        span.set_attribute("inventory.sku", sku)
        span.set_attribute("inventory.delta", req.delta)
        ops.adjust_physical(sku, req.delta)
    return _view(sku)


@app.post("/admin/reset")
def reset():
    """デモをやり直すためのリセット。引当済数も 0 に戻す。"""
    for sku, meta in CATALOG.items():
        r.set(f"stock:{sku}", meta["initial_stock"])
        r.set(f"reserved:{sku}", 0)
        r.delete(f"price:{sku}")
        r.delete(f"published:{sku}")
    return {"status": "reset", "inventory": {s: _view(s) for s in CATALOG}}


@app.post("/admin/chaos")
def set_chaos(slow_ms: int = 0):
    """在庫参照をわざと遅くする。0 で解除。"""
    r.set("chaos:inventory:slow_ms", slow_ms)
    return {"slow_ms": slow_ms}


@app.get("/admin/chaos")
def get_chaos():
    return _chaos()
