"""
order-api : 注文の受付を担当するサービス。

P1 で変わったこと (UC-01 / BR-03 / BR-04)
------------------------------------------
修正前は ``POST /orders`` が **1明細ずつ** しか受け取れず、
カートに3件入れると注文が3件できていた。利用者から見れば1回の買い物なのに、
システム上は別々の注文になっていた (FR-408)。

さらに、決済に失敗しても **引き当てた在庫が戻らなかった** (FR-603)。
「決済に失敗したのに在庫が減ったまま」という振る舞いが、
要件を決めないまま実装したせいで暗黙に決まっていた。

現在の実装は BR-03 に従う::

    在庫引当 → 与信 → 注文登録
    どこで失敗しても、すべて元に戻る (補償トランザクション)

学習ポイント:
  1. 自動計装 (FastAPI / httpx / psycopg) だけで何が取れるか
  2. 手動計装で「補償が走った」ことをトレース上に見せる
  3. カスタムメトリクス。**補償の回数は独立したメトリクスにする価値がある**
  4. ログに trace_id を埋めて「ログ <-> トレース」を往復する
  5. HTTP ヘッダー traceparent による分散コンテキスト伝播
"""

import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field

from opentelemetry import trace, metrics
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.trace import Status, StatusCode

from . import schema, shipping, states
from .saga import Saga, SagaFailed

tracer = trace.get_tracer("order-api")
meter = metrics.get_meter("order-api")

orders_created = meter.create_counter(
    "orders.created", unit="{order}", description="受け付けた注文の件数"
)
orders_rejected = meter.create_counter(
    "orders.rejected", unit="{order}", description="受け付けられなかった注文の件数"
)
order_amount = meter.create_histogram(
    "orders.amount", unit="JPY", description="注文金額の分布"
)
# 補償が何回走ったかは、それ自体が監視対象になる。
# 「注文は成功しているが補償も多い」= 依存先が不安定、と読める。
orders_compensated = meter.create_counter(
    "orders.compensated", unit="{order}", description="補償トランザクションが走った件数"
)
compensation_failures = meter.create_counter(
    "orders.compensation_failures",
    unit="{failure}",
    description="補償自体が失敗した件数。**人手の対応が要る状態**",
)
# UC-06: 前進復旧が止まった件数。**補償と違い、放っておいても解消しない。**
# 「出荷準備中」で止まった注文は、人が動かすまで永久にそこにいる。
orders_stuck = meter.create_counter(
    "orders.stuck",
    unit="{order}",
    description="出荷準備中で停止し、人手の対応を待っている件数",
)
state_conflicts = meter.create_counter(
    "orders.state_conflicts",
    unit="{conflict}",
    description="キャンセルと出荷が競合して弾かれた回数 (BR-26)",
)

INVENTORY_URL = os.getenv("INVENTORY_API_URL", "http://inventory-api:8000")
PAYMENT_URL = os.getenv("PAYMENT_API_URL", "http://payment-api:8000")
GATEWAY_URL = os.getenv("EXTERNAL_STUB_URL", "http://external-stub:8000")
MEMBER_URL = os.getenv("MEMBER_API_URL", "http://member-api:8000")
DB_DSN = os.getenv("DATABASE_URL", "postgresql://demo:demo@postgres:5432/demo")
FREE_SHIPPING_THRESHOLD = int(
    os.getenv("FREE_SHIPPING_THRESHOLD", str(shipping.FREE_SHIPPING_THRESHOLD))
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("order-api")

pool: ConnectionPool | None = None
client: httpx.AsyncClient | None = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    global pool, client
    for attempt in range(30):
        try:
            pool = ConnectionPool(DB_DSN, min_size=1, max_size=5, open=True)
            with pool.connection() as conn:
                conn.execute("SELECT 1")
            break
        except Exception as exc:  # noqa: BLE001
            log.warning("postgres not ready (%s/30): %s", attempt + 1, exc)
            time.sleep(2)
    else:
        raise RuntimeError("postgres に接続できませんでした")

    # 既存ボリュームにも新しいテーブル・列を入れる (init.sql は空ボリュームでしか動かない)
    schema.apply(pool, log)

    client = httpx.AsyncClient(timeout=10.0)
    yield
    await client.aclose()
    pool.close()


app = FastAPI(title="order-api", lifespan=lifespan)

# ---------------------------------------------------------------------------
# FastAPI だけは明示的に計装する (OTEL_PYTHON_DISABLED_INSTRUMENTATIONS=fastapi)。
# 既定では ASGI の "http send" / "http receive" という 0ms の span が大量に出て
# waterfall が読みづらくなるため。
# 「自動計装はまず入れる。ノイズが出たら個別に絞る」という実務の型。
# ---------------------------------------------------------------------------
FastAPIInstrumentor.instrument_app(app, exclude_spans=["receive", "send"])


@app.exception_handler(states.IllegalTransition)
async def _illegal_transition_handler(_request, exc: states.IllegalTransition):
    """禁じられた状態遷移は **409 で返す**。

    これが無いと、``/ops`` で「出荷準備を開始」を2回押しただけで
    text/plain の 500 が返り、BFF 側の ``r.json()`` も失敗して
    原因の分からない二重のエラーになる。
    **状態遷移の禁則は業務上の分岐であって、サーバーの故障ではない。**
    """
    state_conflicts.add(1, {"target": exc.target})
    return JSONResponse(
        status_code=409,
        content={
            "detail": {
                "message": str(exc),
                "actual": exc.current,
                "actual_label": states.LABELS.get(exc.current, exc.current),
            }
        },
    )


# ===========================================================================
# 入出力
# ===========================================================================
class OrderLine(BaseModel):
    sku: str
    quantity: int = Field(ge=1)


class ShipTo(BaseModel):
    """配送先 (FR-701)。

    BR-25 により、**注文側へ複写する**。住所帳 (FR-106) を参照するだけの設計だと、
    利用者が住所を編集した瞬間に過去の注文の配送先まで書き換わる。
    価格を注文明細にコピーする (BR-01) のとまったく同じ理由。
    """

    recipient: str
    postal_code: str
    prefecture: str
    city: str
    address_line: str
    phone: str


class OrderRequest(BaseModel):
    """FR-408 / BR-04: 1回の手続きで確定した明細は **1つの注文** として扱う。

    ``member_id`` は必須。**FR-108 (ゲスト購入) を Won't にしたため**、
    会員を持たない注文は存在しない (多重度 1)。
    型で必須にしておくと、呼び出し側の実装漏れが起動時ではなく
    リクエスト時に 422 で分かる。
    """

    lines: list[OrderLine] = Field(min_length=1)
    member_id: str
    ship_to: ShipTo | None = None
    # FR-501 / BR-16: カード情報そのものは受け取らない。トークンだけ。
    # **型でそう宣言しておくと、うっかり生のカード番号を受ける実装にならない。**
    card_token: str = "tok_test_visa"
    # 注文確認メール (FR-1001) の宛先
    member_email: str | None = None
    customer: str = "demo-user"


def _shipping_fee(prefecture: str | None, subtotal: int) -> int:
    """FR-404。配送先が無い場合は最も高い区分で見積もる。

    **安いほうに倒さない。** 取りこぼした運賃は店舗が負担することになる。
    """
    if not prefecture:
        return 0 if subtotal >= FREE_SHIPPING_THRESHOLD else max(shipping.ZONE_FEES.values())
    try:
        return shipping.shipping_fee(prefecture, subtotal, FREE_SHIPPING_THRESHOLD)
    except shipping.UnknownPrefecture:
        return 0 if subtotal >= FREE_SHIPPING_THRESHOLD else max(shipping.ZONE_FEES.values())


def _record_event(conn, order_id: str, from_state, to_state: str, actor="system", note="") -> None:
    conn.execute(
        "INSERT INTO order_events (order_id, from_state, to_state, actor, note) "
        "VALUES (%s, %s, %s, %s, %s)",
        (order_id, from_state, to_state, actor, note),
    )


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


# ===========================================================================
# 参照
# ===========================================================================
def _load_orders(where: str = "", params: tuple = (), limit: int = 20) -> list[dict]:
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT id, member_id, status, subtotal, shipping_fee, total, tracking_no, created_at "
            f"FROM orders {where} ORDER BY created_at DESC LIMIT %s",
            (*params, limit),
        ).fetchall()
        orders = []
        for r in rows:
            items = conn.execute(
                "SELECT sku, name, unit_price, quantity, subtotal FROM order_items "
                "WHERE order_id = %s ORDER BY id",
                (r[0],),
            ).fetchall()
            orders.append(
                {
                    "id": r[0],
                    "member_id": r[1],
                    "status": r[2],
                    "status_label": states.LABELS.get(r[2], r[2]),
                    "can_cancel": states.can_cancel(r[2]),
                    "subtotal": r[3],
                    "shipping_fee": r[4],
                    "total": r[5],
                    "tracking_no": r[6],
                    "created_at": r[7].isoformat(),
                    "items": [
                        {
                            "sku": i[0],
                            "name": i[1],
                            "unit_price": i[2],
                            "quantity": i[3],
                            "subtotal": i[4],
                        }
                        for i in items
                    ],
                    # 旧 API 互換。1明細目を平坦化して返す
                    "sku": items[0][0] if items else None,
                    "quantity": items[0][3] if items else None,
                    "amount": r[5],
                }
            )
    return orders


@app.get("/orders")
async def list_orders(limit: int = 20, status: str | None = None, member_id: str | None = None):
    clauses, params = [], []
    if status:
        clauses.append("status = %s")
        params.append(status)
    if member_id:
        clauses.append("member_id = %s")
        params.append(member_id)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    return _load_orders(where, tuple(params), limit)


@app.get("/orders/{order_id}")
async def get_order(order_id: str):
    found = _load_orders("WHERE id = %s", (order_id,), 1)
    if not found:
        raise HTTPException(status_code=404, detail="unknown order")
    with pool.connection() as conn:
        events = conn.execute(
            "SELECT from_state, to_state, actor, note, created_at FROM order_events "
            "WHERE order_id = %s ORDER BY id",
            (order_id,),
        ).fetchall()
    order = found[0]
    order["events"] = [
        {"from": e[0], "to": e[1], "actor": e[2], "note": e[3], "at": e[4].isoformat()}
        for e in events
    ]
    return order


# ===========================================================================
# 注文の確定 (UC-01)
# ===========================================================================
@app.post("/orders")
async def create_order(req: OrderRequest):
    """UC-01 の手順7〜10 にあたる処理。

    BR-03: **在庫引当・与信・注文登録は、すべて成功するかすべて元に戻る。**
    """
    with tracer.start_as_current_span("checkout") as span:
        order_id = str(uuid.uuid4())
        saga = Saga()

        span.set_attribute("order.id", order_id)
        span.set_attribute("order.line_count", len(req.lines))
        span.set_attribute("order.skus", ",".join(l.sku for l in req.lines))
        span.set_attribute("enduser.id", req.member_id)

        log.info("checkout start order_id=%s lines=%s", order_id, len(req.lines))
        pairs = [{"sku": l.sku, "quantity": l.quantity} for l in req.lines]
        payment_id: str | None = None

        try:
            # ---------------------------------------------------------------
            # 1) 在庫を引き当てる (全明細まとめて / 全部か無か)
            # ---------------------------------------------------------------
            with tracer.start_as_current_span("reserve-inventory") as inv_span:
                resp = await client.post(f"{INVENTORY_URL}/inventory/reserve", json={"lines": pairs})
                inv_span.set_attribute("inventory.http_status", resp.status_code)
                if resp.status_code == 409:
                    detail = resp.json().get("detail", {})
                    inv_span.add_event("inventory.insufficient", {"order.sku": str(detail.get("sku"))})
                    raise SagaFailed("reserve-inventory", detail, 409)
                if resp.status_code >= 400:
                    # 404 (未知の SKU) や 422 (数量が不正) もここに来る。
                    # **409 だけを拾って残りを raise_for_status に流すと、
                    # SagaFailed にならず 500 になる。** 業務上の入力エラーが
                    # サーバー障害として見えるのは、原因追跡を著しく妨げる
                    detail = resp.json().get("detail", "在庫を引き当てられませんでした")
                    raise SagaFailed("reserve-inventory", detail, resp.status_code)
                inventory = resp.json()["inventory"]

            # 引当に成功した時点で、その補償 (解放) を積む。
            # **成功した直後に積む**のが要点。後でまとめて積もうとすると必ず漏れる。
            #
            # requires="void-payment": BR-07「在庫を戻すのは、与信の取り消しが
            # 成功した後のみ」。与信が生きたまま在庫だけ戻ると、
            # **在庫と金銭の状態が食い違う。** どちらの不整合を許容するかの判断であり、
            # ここでは「金銭の不整合を避ける」を優先している。
            saga.push("release-inventory", _release_inventory(pairs), requires="void-payment")

            # BR-01: 価格は注文確定時点のものを使い、注文明細にコピーする
            items = []
            for line in req.lines:
                view = inventory[line.sku]
                items.append(
                    {
                        "sku": line.sku,
                        "name": view["name"],
                        "unit_price": view["unit_price"],
                        "quantity": line.quantity,
                        "subtotal": view["unit_price"] * line.quantity,
                    }
                )
            subtotal = sum(i["subtotal"] for i in items)
            shipping_fee = _shipping_fee(req.ship_to.prefecture if req.ship_to else None, subtotal)
            total = subtotal + shipping_fee
            span.set_attribute("order.subtotal", subtotal)
            span.set_attribute("order.amount", total)

            # ---------------------------------------------------------------
            # 2) 与信を取る
            # ---------------------------------------------------------------
            with tracer.start_as_current_span("authorize-payment") as pay_span:
                try:
                    resp = await client.post(
                        f"{PAYMENT_URL}/payments/authorize",
                        json={
                            "order_id": order_id,
                            "amount": total,
                            # 同じ注文の再試行では同じキーになる = 二重与信しない
                            "card_token": req.card_token,
                            "idempotency_key": f"auth:{order_id}",
                        },
                    )
                    pay_span.set_attribute("payment.http_status", resp.status_code)
                    resp.raise_for_status()
                    payment = resp.json()
                    payment_id = payment["payment_id"]
                    pay_span.set_attribute("payment.id", payment_id)
                except httpx.HTTPStatusError as exc:
                    body = {}
                    try:
                        body = exc.response.json().get("detail", {})
                    except Exception:  # noqa: BLE001
                        pass
                    unknown = bool(body.get("outcome_unknown"))
                    pay_span.record_exception(exc)
                    pay_span.set_attribute("payment.outcome_unknown", unknown)
                    if unknown:
                        # UC-01 E3: 応答が返らなかったので与信が成立している可能性がある。
                        # 与信 ID が手元に無いため、注文番号から照会して取り消す。
                        #
                        # 名前をあえて "void-payment" に揃えているのは、
                        # release-inventory の requires と一致させるため。
                        # **ここを別名にすると BR-07 の前提条件が効かなくなる。**
                        pay_span.add_event("payment.outcome_unknown")
                        saga.push("void-payment", lambda: _void_by_order(order_id))
                    raise SagaFailed("authorize-payment", "決済に失敗しました", 502)
                except httpx.HTTPError as exc:
                    pay_span.record_exception(exc)
                    raise SagaFailed("authorize-payment", "決済に失敗しました", 502)

            saga.push("void-payment", lambda: _void_payment(payment_id))

            # ---------------------------------------------------------------
            # 3) 注文を登録する
            # ---------------------------------------------------------------
            with tracer.start_as_current_span("persist-order"):
                try:
                    with pool.connection() as conn:
                        with conn.transaction():
                            conn.execute(
                                "INSERT INTO orders "
                                "(id, member_id, status, subtotal, shipping_fee, total, ship_to) "
                                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                                (
                                    order_id,
                                    req.member_id,
                                    "ACCEPTED",
                                    subtotal,
                                    shipping_fee,
                                    total,
                                    # BR-25: 配送先を注文側へ複写する
                                    json.dumps(req.ship_to.model_dump()) if req.ship_to else None,
                                ),
                            )
                            for i in items:
                                conn.execute(
                                    "INSERT INTO order_items "
                                    "(order_id, sku, name, unit_price, quantity, subtotal) "
                                    "VALUES (%s, %s, %s, %s, %s, %s)",
                                    (
                                        order_id,
                                        i["sku"],
                                        i["name"],
                                        i["unit_price"],
                                        i["quantity"],
                                        i["subtotal"],
                                    ),
                                )
                            _record_event(conn, order_id, None, "ACCEPTED", "system", "注文確定")
                except Exception as exc:  # noqa: BLE001
                    # UC-01 E4: 与信を取り消し、在庫を解放する
                    log.error("persist failed order_id=%s: %s", order_id, exc)
                    raise SagaFailed("persist-order", "注文の登録に失敗しました", 500)

        except SagaFailed as failed:
            # -----------------------------------------------------------------
            # 補償: 積んだ操作を逆順に巻き戻す
            # -----------------------------------------------------------------
            span.add_event("saga.failed", {"saga.step": failed.step})
            await saga.compensate()
            orders_compensated.add(1, {"failed_step": failed.step})
            if saga.needs_manual_intervention:
                # **補償が失敗した / 前提が崩れて実行できなかった = 人手の対応が要る状態。**
                # 注文が失敗したことより、こちらのほうが重い。最優先のアラート対象。
                compensation_failures.add(1, {"failed_step": failed.step})
                span.set_attribute("saga.needs_manual_intervention", True)
                log.error(
                    "MANUAL INTERVENTION REQUIRED order_id=%s failed=%s skipped=%s",
                    order_id,
                    saga.compensation_failures,
                    saga.compensation_skipped,
                )
            span.set_status(Status(StatusCode.ERROR, failed.step))
            orders_rejected.add(1, {"reason": failed.step})
            log.warning("checkout failed order_id=%s step=%s", order_id, failed.step)
            raise HTTPException(status_code=failed.status_code, detail=failed.detail)

        # --------------------------------------------------------------------
        # **想定外の例外でも必ず補償する。**
        # SagaFailed だけを捕まえていると、その他の例外は補償を素通りして
        # 引当を残したまま 500 になる。「握り潰さないが後始末はする」が最低条件。
        #
        # except の順序が重要。SagaFailed / HTTPException はどちらも Exception の
        # 派生なので、**広いものを先に書くと下の節に到達しない。**
        # --------------------------------------------------------------------
        except HTTPException:
            await saga.compensate()
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("unexpected error during checkout order_id=%s", order_id)
            span.record_exception(exc)
            await saga.compensate()
            orders_compensated.add(1, {"failed_step": "unexpected"})
            orders_rejected.add(1, {"reason": "unexpected"})
            span.set_status(Status(StatusCode.ERROR, "unexpected"))
            raise HTTPException(status_code=500, detail="注文処理でエラーが発生しました")

        # 成功。巻き戻す必要は無くなった
        saga.clear()

        # -------------------------------------------------------------------
        # UC-01 手順11: 注文確認メール (FR-1001)
        #
        # **saga.clear() の後に置いてあることが重要。**
        # ここより前に置くと、メール送信の失敗が補償を起動してしまう。
        # UC-01 E5「注文は成立させたままにする。メール送信の失敗を理由に
        # 注文を取り消してはならない」は、**コードの位置で表現される。**
        # -------------------------------------------------------------------
        await _send_confirmation(order_id, req, items, subtotal, shipping_fee, total)

        orders_created.add(1, {"line_count": str(len(items))})
        order_amount.record(total, {})
        span.set_status(Status(StatusCode.OK))
        log.info("checkout done order_id=%s total=%s", order_id, total)

        return {
            "order_id": order_id,
            "status": "ACCEPTED",
            "items": items,
            "subtotal": subtotal,
            "shipping_fee": shipping_fee,
            "total": total,
            "payment_id": payment_id,
            "ship_to": req.ship_to.model_dump() if req.ship_to else None,
            # 旧 API 互換
            "amount": total,
            "sku": items[0]["sku"],
            "quantity": items[0]["quantity"],
        }


# ===========================================================================
# 状態遷移 (UC-02 / UC-06)
# ===========================================================================
def _transition(order_id: str, expected: str, target: str, actor: str, note: str = "") -> None:
    """**BR-26: 状態遷移は排他的に行う。**

    ``WHERE status = %s`` を付けた UPDATE の更新件数で判定する。
    読んでから書くまでの間に他の誰かが動かしていたら、更新件数は 0 になる。
    在庫の FR-608 (原子的な引当) とまったく同じ形の問題であり、
    **在庫で見つけた問題の形を、注文の状態にも当てはめて探した**結果見つかった。

    これが無いと、会員のキャンセル (UC-02) と運用担当者の出荷 (UC-06) が
    同時に走ったとき、**両方成立して「出荷したのに返金した」**が起きる。
    """
    states.assert_transition(expected, target)
    with pool.connection() as conn:
        with conn.transaction():
            cur = conn.execute(
                "UPDATE orders SET status = %s, version = version + 1, updated_at = now() "
                "WHERE id = %s AND status = %s",
                (target, order_id, expected),
            )
            if cur.rowcount == 0:
                state_conflicts.add(1, {"target": target})
                current = conn.execute(
                    "SELECT status FROM orders WHERE id = %s", (order_id,)
                ).fetchone()
                actual = current[0] if current else "UNKNOWN"
                log.warning(
                    "state conflict order_id=%s expected=%s actual=%s", order_id, expected, actual
                )
                raise HTTPException(
                    status_code=409,
                    detail={
                        "message": "注文の状態が変わっています",
                        "expected": expected,
                        "actual": actual,
                        "actual_label": states.LABELS.get(actual, actual),
                    },
                )
            _record_event(conn, order_id, expected, target, actor, note)


def _order_row(order_id: str) -> tuple[str, str | None, str | None]:
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT status, member_id, tracking_no FROM orders WHERE id = %s", (order_id,)
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="unknown order")
    return row


def _order_lines(order_id: str) -> list[dict]:
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT sku, quantity FROM order_items WHERE order_id = %s ORDER BY id", (order_id,)
        ).fetchall()
    return [{"sku": r[0], "quantity": r[1]} for r in rows]


async def _payment_of(order_id: str) -> dict | None:
    resp = await client.get(f"{PAYMENT_URL}/payments", params={"order_id": order_id})
    resp.raise_for_status()
    live = [p for p in resp.json() if p["status"] in ("AUTHORIZED", "CAPTURED", "UNKNOWN")]
    return live[0] if live else None


def _inventory_already_shipped(order_id: str) -> bool:
    """在庫の出荷確定を適用済みか。冪等キーの表を流用する。"""
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM idempotency_keys WHERE key = %s", (f"ship-inventory:{order_id}",)
        ).fetchone()
    return row is not None


def _mark_inventory_shipped(order_id: str) -> None:
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO idempotency_keys (key, scope, request_ref) VALUES (%s, %s, %s) "
            "ON CONFLICT (key) DO NOTHING",
            (f"ship-inventory:{order_id}", "ship-inventory", order_id),
        )


class ActorRequest(BaseModel):
    actor: str = "ops"


@app.post("/orders/{order_id}/prepare")
async def start_preparation(order_id: str, req: ActorRequest):
    """UC-06 手順4: 出荷処理を開始し、**キャンセルを受け付けなくする** (BR-06)。

    梱包を始めてからキャンセルされると作業が無駄になり、
    画面で選んだだけでキャンセル不可にすると会員に厳しすぎる。
    **その境界をこの1回の遷移で表している。**
    """
    status, _, _ = _order_row(order_id)
    with tracer.start_as_current_span("start-preparation") as span:
        span.set_attribute("order.id", order_id)
        _transition(order_id, status, states.PREPARING, req.actor, "出荷処理を開始")
    return {"order_id": order_id, "status": states.PREPARING}


class ShipRequest(BaseModel):
    actor: str = "ops"
    # UC-06 手順7。実務では配送業者の送り状発行で採番される
    tracking_no: str | None = None


@app.post("/orders/{order_id}/ship")
async def ship(order_id: str, req: ShipRequest):
    """UC-06 手順7〜11。**ここが「前進復旧」の実装。**

    UC-01 との違いを、コードの形で示している。

      UC-01  失敗したら **後退** する (補償トランザクションで元に戻す)
      UC-06  失敗しても **後退しない**。「出荷準備中」で止め、人が進める

    梱包した箱は元に戻らない。だから ``Saga`` を使わない。
    順序も重要で、**売上確定を実在庫の減算より前に置いている** (BR-27)。
    引き渡した後に確定できないと、商品が出て代金が取れない = 実損になる。
    """
    status, _, _ = _order_row(order_id)
    if status != states.PREPARING:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "出荷準備中の注文のみ出荷できます",
                "actual": status,
                "actual_label": states.LABELS.get(status, status),
            },
        )

    lines = _order_lines(order_id)

    with tracer.start_as_current_span("ship-order") as span:
        span.set_attribute("order.id", order_id)
        span.set_attribute("order.line_count", len(lines))

        # --- UC-06 手順8: 売上確定 (BR-27 / FR-503 / FR-512) --------------
        payment = await _payment_of(order_id)
        if payment is None:
            raise HTTPException(status_code=409, detail="この注文には与信がありません")
        try:
            resp = await client.post(
                f"{PAYMENT_URL}/payments/capture",
                json={
                    "payment_id": payment["payment_id"],
                    # 同じ注文の再試行では同じキー。**二重請求しない** (BR-28)
                    "idempotency_key": f"capture:{order_id}",
                },
            )
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            # ---------------------------------------------------------------
            # UC-06 E1。**「受付済」には戻さない。**
            # 引当も与信もそのまま残す。ここで巻き戻すと、
            # 既に梱包した荷物と帳簿が食い違う。
            # 荷物を引き渡してはならない、という指示を運用担当者に返す。
            # ---------------------------------------------------------------
            span.record_exception(exc)
            span.add_event("ship.capture_failed_order_kept_preparing")
            span.set_attribute("order.needs_manual_intervention", True)
            orders_stuck.add(1, {"reason": "capture_failed"})
            log.error("capture failed; order stays PREPARING order_id=%s: %s", order_id, exc)
            raise HTTPException(
                status_code=502,
                detail={
                    "message": "売上確定に失敗しました。注文は出荷準備中のままです。"
                    "**荷物を引き渡さないでください。**",
                    "order_id": order_id,
                    "status": states.PREPARING,
                    "hint": "与信の期限切れ、または決済代行の障害の可能性があります。",
                },
            )

        # --- UC-06 手順9: 実在庫を減らす (FR-611 / BR-29) ------------------
        #
        # **リトライで二重に減らしてはならない。**
        # ship() は「失敗しても後退せず、リトライで前に進める」設計なので、
        # 在庫の減算だけ非冪等だと、2回目の呼び出しで
        #   - 引当済が既に 0 → 永久に出荷できない
        #   - 他の注文の引当を食う → 別の注文が出荷できなくなる
        # のどちらかが起きる。**前進復旧を選ぶなら、途中の操作は全部冪等が要る。**
        # ここでは注文ごとの適用済みフラグで守る (決済の冪等キーと同じ考え方)。
        already_shipped = _inventory_already_shipped(order_id)
        span.set_attribute("inventory.already_shipped", already_shipped)
        try:
            if not already_shipped:
                resp = await client.post(f"{INVENTORY_URL}/inventory/ship", json={"lines": lines})
                resp.raise_for_status()
                _mark_inventory_shipped(order_id)
            else:
                span.add_event("ship.inventory_already_applied")
        except Exception as exc:  # noqa: BLE001
            # BR-29: 引当済でも実物が無いことがある (棚卸差異・破損・紛失)。
            # **売上は確定済みなので、ここも巻き戻さない。**
            # 返金 (UC-03) と在庫調整 (FR-606) で人が解決する世界に入る。
            span.record_exception(exc)
            span.add_event("ship.physical_shortage_after_capture")
            span.set_attribute("order.needs_manual_intervention", True)
            orders_stuck.add(1, {"reason": "physical_shortage"})
            log.error("physical shortage AFTER capture order_id=%s: %s", order_id, exc)
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "実在庫が不足しています。**売上は既に確定しています。**"
                    "返金または在庫調整で対応してください。",
                    "order_id": order_id,
                    "status": states.PREPARING,
                },
            )

        # --- UC-06 手順7 の続き: 追跡番号 (FR-704) -------------------------
        tracking = req.tracking_no
        if not tracking:
            try:
                r = await client.post(
                    f"{GATEWAY_URL}/shipping/label", json={"order_id": order_id}
                )
                r.raise_for_status()
                tracking = r.json()["tracking_no"]
            except Exception as exc:  # noqa: BLE001
                # 送り状が取れなくても出荷は成立させる。後から訂正できる (FR-1112)
                span.add_event("ship.label_failed")
                log.error("label failed order_id=%s: %s", order_id, exc)

        # --- UC-06 手順10: 状態遷移 ----------------------------------------
        _transition(order_id, states.PREPARING, states.SHIPPED, req.actor, "出荷確定")
        if tracking:
            with pool.connection() as conn:
                conn.execute(
                    "UPDATE orders SET tracking_no = %s, updated_at = now() WHERE id = %s",
                    (tracking, order_id),
                )
        span.set_attribute("shipping.tracking_no", tracking or "")

    # --- UC-06 手順11: 発送通知 (FR-1002) ---------------------------------
    # **状態遷移の後に置く。** UC-01 E5 と同じ判断で、
    # メール送信の失敗を理由に出荷を取り消してはならない。
    await _send_shipment_notice(order_id, tracking)

    log.info("shipped order_id=%s tracking=%s", order_id, tracking)
    return {"order_id": order_id, "status": states.SHIPPED, "tracking_no": tracking}


@app.post("/orders/{order_id}/cancel")
async def cancel(order_id: str, req: ActorRequest):
    """UC-02。**BR-07: 在庫を戻すのは、与信の取り消しが成功した後のみ。**

    与信が生きたまま在庫だけ戻ると、在庫と金銭の状態が食い違う。
    どちらの不整合を許容するかの判断であり、金銭側を守っている。
    """
    status, _, _ = _order_row(order_id)
    if not states.can_cancel(status):
        # UC-02 E1: キャンセルできない場合は **返品 (UC-03) を案内する**
        raise HTTPException(
            status_code=409,
            detail={
                "message": f"{states.LABELS.get(status, status)}の注文はキャンセルできません",
                "actual": status,
                "hint": "出荷済みの場合は返品の手続きをご利用ください。",
            },
        )

    lines = _order_lines(order_id)
    with tracer.start_as_current_span("cancel-order") as span:
        span.set_attribute("order.id", order_id)

        # ---------------------------------------------------------------
        # **状態遷移を先に確定させる (BR-26)。**
        #
        # 当初は「与信取消 → 在庫解放 → 状態遷移」の順に書いていた。
        # UC-02 の記述 (E2「注文の状態は変えず」) をそのままなぞった形である。
        # しかしこの順序だと、キャンセルと出荷準備が同時に走ったときに
        # **与信を取り消して在庫も戻した後で、状態遷移だけが 409 で弾かれる。**
        # 注文は「出荷準備中」のまま、与信は消え、在庫は戻っている——
        # 3つの状態がすべて食い違う最悪の結果になる。
        #
        # 先に CAS で「この注文は自分が取り消す」と確定させれば、
        # 出荷準備は同時に成立できない。副作用はその後に行う。
        #
        # **要件の記述 (UC-02 E2) と実装がここでずれた。**
        # 記述は競合を考慮していなかったので、要件側を直す必要がある。
        # ---------------------------------------------------------------
        _transition(order_id, status, states.CANCELLED, req.actor, "キャンセル")

        # UC-02 手順4: 与信を取り消す
        payment = await _payment_of(order_id)
        if payment and payment["status"] != "CAPTURED":
            try:
                resp = await client.post(
                    f"{PAYMENT_URL}/payments/void", json={"payment_id": payment["payment_id"]}
                )
                resp.raise_for_status()
            except Exception as exc:  # noqa: BLE001
                # BR-07: 与信の取り消しに失敗したので **在庫も戻さない**。
                # 与信が生きたまま在庫だけ戻ると、在庫と金銭の状態が食い違う。
                # 注文は「キャンセル済」になっているが、金銭の後始末は人が行う。
                span.record_exception(exc)
                span.add_event("cancel.void_failed_inventory_kept")
                span.set_attribute("order.needs_manual_intervention", True)
                orders_stuck.add(1, {"reason": "void_failed"})
                with pool.connection() as conn:
                    _record_event(
                        conn, order_id, states.CANCELLED, states.CANCELLED, "system",
                        "与信の取り消しに失敗。在庫は解放していません（要手動対応）",
                    )
                log.error("void failed; inventory NOT released order_id=%s: %s", order_id, exc)
                raise HTTPException(
                    status_code=502,
                    detail={
                        "message": "キャンセルは受け付けましたが、与信の取り消しに失敗しました。"
                        "運用担当者が対応します。",
                        "order_id": order_id,
                        "status": states.CANCELLED,
                    },
                )

        # UC-02 手順5: 与信の取り消しが成功したので在庫を戻す (BR-07)
        resp = await client.post(f"{INVENTORY_URL}/inventory/release", json={"lines": lines})
        resp.raise_for_status()

    orders_compensated.add(1, {"failed_step": "user_cancel"})
    log.info("cancelled order_id=%s", order_id)
    return {"order_id": order_id, "status": states.CANCELLED}


@app.post("/orders/{order_id}/deliver")
async def deliver(order_id: str, req: ActorRequest):
    status, _, _ = _order_row(order_id)
    _transition(order_id, status, states.DELIVERED, req.actor, "配達完了")
    return {"order_id": order_id, "status": states.DELIVERED}


class TrackingRequest(BaseModel):
    tracking_no: str


@app.put("/orders/{order_id}/tracking")
async def correct_tracking(order_id: str, req: TrackingRequest):
    """FR-1112 / UC-06 E6: 追跡番号は訂正できる。

    **状態遷移と売上確定はやり直さない。** 番号の打ち間違いは
    業務の進行とは別の話なので、同じ経路に載せる必要がない。
    """
    _order_row(order_id)
    with pool.connection() as conn:
        conn.execute(
            "UPDATE orders SET tracking_no = %s, updated_at = now() WHERE id = %s",
            (req.tracking_no, order_id),
        )
    return {"order_id": order_id, "tracking_no": req.tracking_no}


async def _send_shipment_notice(order_id: str, tracking: str | None) -> None:
    """FR-1002 発送通知メール。失敗しても出荷は成立したままにする。"""
    with pool.connection() as conn:
        row = conn.execute("SELECT member_id FROM orders WHERE id = %s", (order_id,)).fetchone()
    member_id = row[0] if row else None
    if not member_id:
        return
    try:
        me = await client.get(f"{MEMBER_URL}/members/{member_id}/email")
        if me.status_code != 200:
            return
        email = me.json().get("email")
        if not email:
            return
        resp = await client.post(
            f"{GATEWAY_URL}/mail/send",
            json={
                "to": email,
                "subject": f"【Roastery】商品を発送しました（{order_id[:8]}）",
                "body": (
                    f"ご注文の商品を発送しました。\n\n"
                    f"注文番号: {order_id}\n"
                    f"追跡番号: {tracking or '（準備中）'}\n"
                ),
            },
        )
        # httpx は 4xx/5xx で例外を投げない。これが無いと
        # **メールが送れていないのに送れたことになる**
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        log.error("shipment notice failed order_id=%s: %s", order_id, exc)


async def _send_confirmation(
    order_id: str, req: "OrderRequest", items: list[dict], subtotal: int, fee: int, total: int
) -> None:
    """注文確認メールを送る。**失敗しても注文は成立したままにする** (UC-01 E5)。"""
    if not req.member_email:
        return
    with tracer.start_as_current_span("send-confirmation-mail") as span:
        lines = "\n".join(
            f"  {i['name']} × {i['quantity']}  {i['subtotal']:,} 円" for i in items
        )
        body = (
            f"ご注文ありがとうございました。\n\n"
            f"注文番号: {order_id}\n\n{lines}\n\n"
            f"  商品計  {subtotal:,} 円\n"
            f"  送料    {fee:,} 円\n"
            f"  合計    {total:,} 円\n"
        )
        try:
            resp = await client.post(
                f"{GATEWAY_URL}/mail/send",
                json={
                    "to": req.member_email,
                    "subject": f"【Roastery】ご注文を承りました（{order_id[:8]}）",
                    "body": body,
                },
            )
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            # 運用担当者への通知対象。**注文は取り消さない**
            span.record_exception(exc)
            span.add_event("mail.send_failed_order_kept")
            log.error("confirmation mail failed order_id=%s: %s", order_id, exc)


@app.get("/shipping/quote")
async def shipping_quote(prefecture: str, subtotal: int):
    """UC-01 手順4。配送先を入力した時点で送料と合計を提示する。

    **注文を確定する前に金額が確定していること**が US-04 の要件。
    """
    try:
        return shipping.quote(prefecture, subtotal, FREE_SHIPPING_THRESHOLD)
    except shipping.UnknownPrefecture as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@app.get("/shipping/prefectures")
async def shipping_prefectures():
    return {"prefectures": shipping.prefectures(), "zones": shipping.ZONE_FEES}


def _release_inventory(pairs: list[dict]):
    """在庫解放の補償を作る。

    ``raise_for_status()`` を必ず呼ぶこと。httpx は 4xx/5xx でも例外を投げないため、
    これが無いと **補償が失敗しても成功したことになる。**
    補償の失敗を検知できないのは、補償が無いのとほぼ同じくらい悪い。
    """

    async def undo() -> None:
        resp = await client.post(f"{INVENTORY_URL}/inventory/release", json={"lines": pairs})
        resp.raise_for_status()

    return undo


async def _void_payment(payment_id: str | None) -> None:
    if not payment_id:
        return
    resp = await client.post(f"{PAYMENT_URL}/payments/void", json={"payment_id": payment_id})
    resp.raise_for_status()


async def _void_by_order(order_id: str) -> None:
    """UC-01 E3: 応答が返らなかった与信を、注文番号から探して取り消す。

    与信 ID が手元に無いので、注文番号で照会するしかない。
    **「応答が返らない」と「失敗した」の扱いが違う**のは、ここが理由。
    """
    resp = await client.get(f"{PAYMENT_URL}/payments", params={"order_id": order_id})
    resp.raise_for_status()
    for p in resp.json():
        if p["status"] in ("AUTHORIZED", "UNKNOWN"):
            r = await client.post(
                f"{PAYMENT_URL}/payments/void", json={"payment_id": p["payment_id"]}
            )
            r.raise_for_status()
