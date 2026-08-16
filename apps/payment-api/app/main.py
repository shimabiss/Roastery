"""
payment-api : 自システム側の決済サービス。

**外部の決済代行そのものではない。** 決済代行は ``external-stub`` が模しており、
このサービスはその手前に立って以下を担当する。

  - 与信 / 売上確定 / 取り消しのオーケストレーション (FR-502 / 503 / 504)
  - **冪等キーの管理** (FR-512 / BR-28)
  - 決済の状態を Postgres に永続化する

なぜ層を分けるのか
------------------
BR-28「売上確定の依頼には冪等キーを付す。二重に請求してはならない」は
**自システムの責任** である。決済代行が応答を返さなかったとき、
「与信が成立したかどうか分からない」状態に置かれるのは自分たちだからだ。

  エラーが返る  → 失敗したと分かる → 巻き戻せばよい
  応答が返らない → 成立したか不明   → **記録が無いと二度と分からない**

だから、外部を呼ぶ前に「このキーで呼びに行く」と自分の DB に書く。
これが冪等キーの本質であり、外部に任せてよい仕事ではない。
"""

import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException
from psycopg_pool import ConnectionPool
from pydantic import BaseModel

from opentelemetry import trace, metrics
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.trace import Status, StatusCode

tracer = trace.get_tracer("payment-api")
meter = metrics.get_meter("payment-api")

payment_latency = meter.create_histogram(
    "payment.authorize.duration", unit="ms", description="与信処理の所要時間"
)
payment_result = meter.create_counter(
    "payment.result", unit="{payment}", description="決済操作の結果件数"
)
idempotent_replays = meter.create_counter(
    "payment.idempotent_replays",
    unit="{replay}",
    description="冪等キーにより再実行を回避した回数",
)

GATEWAY_URL = os.getenv("EXTERNAL_STUB_URL", "http://external-stub:8000")
DB_DSN = os.getenv("DATABASE_URL", "postgresql://demo:demo@postgres:5432/demo")
# 外部呼び出しのタイムアウト。**無応答を「無応答として」検出するために必須**。
GATEWAY_TIMEOUT_S = float(os.getenv("GATEWAY_TIMEOUT_S", "5"))

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("payment-api")

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

    client = httpx.AsyncClient(timeout=GATEWAY_TIMEOUT_S)
    yield
    await client.aclose()
    pool.close()


app = FastAPI(title="payment-api", lifespan=lifespan)
FastAPIInstrumentor.instrument_app(app, exclude_spans=["receive", "send"])


# ---------------------------------------------------------------------------
# 冪等キー
# ---------------------------------------------------------------------------
def _lookup_idempotent(key: str, scope: str) -> dict | None:
    """同じキーで既に完了した操作があれば、その結果をそのまま返す。"""
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT response FROM idempotency_keys WHERE key = %s AND scope = %s",
            (key, scope),
        ).fetchone()
    if row and row[0]:
        return row[0] if isinstance(row[0], dict) else json.loads(row[0])
    return None


def _claim_idempotent(key: str, scope: str, ref: str) -> bool:
    """キーを先に取る。**外部を呼ぶ前に書く**のが要点。

    後から書くと、外部呼び出し中にプロセスが落ちた場合に記録が残らず、
    再実行で二重請求になる。
    """
    try:
        with pool.connection() as conn:
            conn.execute(
                "INSERT INTO idempotency_keys (key, scope, request_ref) VALUES (%s, %s, %s)",
                (key, scope, ref),
            )
        return True
    except Exception:  # noqa: BLE001  重複 = 既に誰かが処理中/処理済み
        return False


def _complete_idempotent(key: str, scope: str, response: dict) -> None:
    with pool.connection() as conn:
        conn.execute(
            "UPDATE idempotency_keys SET response = %s WHERE key = %s AND scope = %s",
            (json.dumps(response), key, scope),
        )


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


# ===========================================================================
# 与信 (FR-502)
# ===========================================================================
class AuthorizeRequest(BaseModel):
    order_id: str
    amount: int
    card_token: str = "tok_test_visa"
    # 呼び出し側 (order-api) が採番する。同じ注文の再試行では同じ値を使う
    idempotency_key: str | None = None


@app.post("/payments/authorize")
async def authorize(req: AuthorizeRequest):
    started = time.perf_counter()
    key = req.idempotency_key or f"auth:{req.order_id}"

    with tracer.start_as_current_span("authorize") as span:
        span.set_attribute("payment.order_id", req.order_id)
        span.set_attribute("payment.amount", req.amount)
        span.set_attribute("payment.idempotency_key", key)

        cached = _lookup_idempotent(key, "authorize")
        if cached:
            span.add_event("payment.idempotent_replay")
            idempotent_replays.add(1, {"scope": "authorize"})
            log.info("idempotent replay order_id=%s", req.order_id)
            return cached

        _claim_idempotent(key, "authorize", req.order_id)

        payment_id = f"pay_{uuid.uuid4().hex[:12]}"
        with pool.connection() as conn:
            conn.execute(
                "INSERT INTO payments (id, order_id, amount, status) VALUES (%s, %s, %s, %s)",
                (payment_id, req.order_id, req.amount, "PENDING"),
            )

        try:
            resp = await client.post(
                f"{GATEWAY_URL}/payment/authorize",
                json={
                    "order_id": req.order_id,
                    "amount": req.amount,
                    "card_token": req.card_token,
                },
                headers={"idempotency-key": key},
            )
            resp.raise_for_status()
            gw = resp.json()
        except Exception as exc:  # noqa: BLE001
            # --- ここが UC-01 E2 / E3 の分岐点 -----------------------------
            # httpx.TimeoutException なら「応答が返らなかった」= 成立したか不明。
            # それ以外は「失敗した」と確定できる。**扱いが違うので区別する。**
            unknown = isinstance(exc, (httpx.TimeoutException, httpx.ConnectError))
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, "与信失敗"))
            span.set_attribute("payment.outcome_unknown", unknown)
            with pool.connection() as conn:
                conn.execute(
                    "UPDATE payments SET status = %s, updated_at = now() WHERE id = %s",
                    ("UNKNOWN" if unknown else "FAILED", payment_id),
                )
            payment_result.add(1, {"op": "authorize", "result": "unknown" if unknown else "failed"})
            log.error("authorize failed order_id=%s unknown=%s: %s", req.order_id, unknown, exc)
            raise HTTPException(
                status_code=504 if unknown else 502,
                detail={
                    "message": "authorization failed",
                    "outcome_unknown": unknown,
                    "payment_id": payment_id,
                },
            )

        with pool.connection() as conn:
            conn.execute(
                "UPDATE payments SET status = %s, authorization_id = %s, updated_at = now() "
                "WHERE id = %s",
                ("AUTHORIZED", gw["authorization_id"], payment_id),
            )

        result = {
            "payment_id": payment_id,
            "order_id": req.order_id,
            "amount": req.amount,
            "status": "AUTHORIZED",
            "authorization_id": gw["authorization_id"],
        }
        _complete_idempotent(key, "authorize", result)
        span.set_attribute("payment.id", payment_id)
        span.set_status(Status(StatusCode.OK))

    payment_result.add(1, {"op": "authorize", "result": "ok"})
    payment_latency.record((time.perf_counter() - started) * 1000, {"result": "ok"})
    return result


# ===========================================================================
# 与信の取り消し (FR-504) — 補償トランザクション
# ===========================================================================
class ByPaymentRequest(BaseModel):
    payment_id: str
    idempotency_key: str | None = None


@app.post("/payments/void")
async def void(req: ByPaymentRequest):
    """与信を取り消す。UC-01 E2 / UC-02 手順4。

    **冪等**。既に取り消し済みなら成功として返す。
    補償処理はリトライされる前提なので、そうでないと使えない。
    """
    with tracer.start_as_current_span("void") as span:
        span.set_attribute("payment.id", req.payment_id)
        span.add_event("payment.compensating")

        with pool.connection() as conn:
            row = conn.execute(
                "SELECT status, authorization_id FROM payments WHERE id = %s", (req.payment_id,)
            ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="unknown payment")
        status, auth_id = row

        if status == "VOIDED":
            span.add_event("payment.already_voided")
            return {"payment_id": req.payment_id, "status": "VOIDED"}
        if status == "CAPTURED":
            raise HTTPException(status_code=409, detail="already captured; refund instead")
        if not auth_id:
            # ---------------------------------------------------------------
            # **与信 ID が無い = 与信が無い、ではない。** (UC-01 E3)
            # 応答が返らなかった場合、与信は成立しているのに ID だけ
            # 手元に無いことがある。ここで「対象なし」と即断すると、
            # 生きた与信が残ったまま補償が成功したことになる。
            # **最も見つけにくい種類の不具合**なので、必ず照会する。
            # ---------------------------------------------------------------
            span.add_event("payment.void_lookup_by_order")
            found = []
            try:
                with pool.connection() as conn:
                    row = conn.execute(
                        "SELECT order_id FROM payments WHERE id = %s", (req.payment_id,)
                    ).fetchone()
                order_id = row[0] if row else None
                if order_id:
                    lookup = await client.get(
                        f"{GATEWAY_URL}/payment/authorizations", params={"order_id": order_id}
                    )
                    lookup.raise_for_status()
                    found = [a for a in lookup.json() if a["status"] == "AUTHORIZED"]
            except Exception as exc:  # noqa: BLE001
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR, "照会失敗"))
                payment_result.add(1, {"op": "void", "result": "lookup_failed"})
                # 照会できない = 与信の有無が分からない。**成功と報告してはならない**
                raise HTTPException(status_code=502, detail="与信の照会に失敗しました")

            for a in found:
                v = await client.post(
                    f"{GATEWAY_URL}/payment/void",
                    json={"authorization_id": a["authorization_id"]},
                )
                if v.status_code >= 400:
                    payment_result.add(1, {"op": "void", "result": "failed"})
                    raise HTTPException(status_code=502, detail="void failed")
                span.add_event("payment.orphan_authorization_voided")
                log.warning("voided orphan authorization %s", a["authorization_id"])

            with pool.connection() as conn:
                conn.execute(
                    "UPDATE payments SET status = 'VOIDED', updated_at = now() WHERE id = %s",
                    (req.payment_id,),
                )
            return {
                "payment_id": req.payment_id,
                "status": "VOIDED",
                "orphans_voided": len(found),
            }

        resp = await client.post(
            f"{GATEWAY_URL}/payment/void", json={"authorization_id": auth_id}
        )
        if resp.status_code >= 400:
            span.set_status(Status(StatusCode.ERROR, "取り消し失敗"))
            payment_result.add(1, {"op": "void", "result": "failed"})
            # BR-07: 取り消しに失敗したら **在庫は戻さない**。
            # 呼び出し側がこのエラーを見て判断する。
            raise HTTPException(status_code=502, detail="void failed")

        with pool.connection() as conn:
            conn.execute(
                "UPDATE payments SET status = 'VOIDED', updated_at = now() WHERE id = %s",
                (req.payment_id,),
            )
        span.set_status(Status(StatusCode.OK))

    payment_result.add(1, {"op": "void", "result": "ok"})
    log.info("voided payment_id=%s", req.payment_id)
    return {"payment_id": req.payment_id, "status": "VOIDED"}


# ===========================================================================
# 売上確定 (FR-503 / FR-512) — UC-06 手順8
# ===========================================================================
@app.post("/payments/capture")
async def capture(req: ByPaymentRequest):
    """売上を確定する。**BR-27: 荷物を引き渡す前に呼ぶ。**

    引き渡した後に確定できないと、商品は出たのに代金が取れない = 実損になる。
    """
    key = req.idempotency_key or f"capture:{req.payment_id}"

    with tracer.start_as_current_span("capture") as span:
        span.set_attribute("payment.id", req.payment_id)
        span.set_attribute("payment.idempotency_key", key)

        cached = _lookup_idempotent(key, "capture")
        if cached:
            span.add_event("payment.idempotent_replay")
            idempotent_replays.add(1, {"scope": "capture"})
            return cached

        with pool.connection() as conn:
            row = conn.execute(
                "SELECT status, authorization_id FROM payments WHERE id = %s", (req.payment_id,)
            ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="unknown payment")
        status, auth_id = row

        if status == "CAPTURED":
            return {"payment_id": req.payment_id, "status": "CAPTURED"}
        if status == "VOIDED":
            raise HTTPException(status_code=409, detail="authorization already voided")
        if not auth_id:
            raise HTTPException(status_code=409, detail="no authorization to capture")

        _claim_idempotent(key, "capture", req.payment_id)

        try:
            resp = await client.post(
                f"{GATEWAY_URL}/payment/capture",
                json={"authorization_id": auth_id},
                headers={"idempotency-key": key},
            )
            resp.raise_for_status()
            gw = resp.json()
        except Exception as exc:  # noqa: BLE001
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, "売上確定失敗"))
            payment_result.add(1, {"op": "capture", "result": "failed"})
            # UC-06 E1: 注文は「出荷準備中」のまま留める。**「受付済」には戻さない。**
            log.error("capture failed payment_id=%s: %s", req.payment_id, exc)
            raise HTTPException(status_code=502, detail="capture failed")

        with pool.connection() as conn:
            conn.execute(
                "UPDATE payments SET status = 'CAPTURED', capture_id = %s, updated_at = now() "
                "WHERE id = %s",
                (gw.get("capture_id"), req.payment_id),
            )
        result = {
            "payment_id": req.payment_id,
            "status": "CAPTURED",
            "capture_id": gw.get("capture_id"),
        }
        _complete_idempotent(key, "capture", result)
        span.set_status(Status(StatusCode.OK))

    payment_result.add(1, {"op": "capture", "result": "ok"})
    log.info("captured payment_id=%s", req.payment_id)
    return result


@app.get("/payments/{payment_id}")
async def get_payment(payment_id: str):
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT id, order_id, amount, status, authorization_id, capture_id "
            "FROM payments WHERE id = %s",
            (payment_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="unknown payment")
    return dict(
        zip(["payment_id", "order_id", "amount", "status", "authorization_id", "capture_id"], row)
    )


@app.get("/payments")
async def list_payments(order_id: str):
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT id, order_id, amount, status, authorization_id, capture_id "
            "FROM payments WHERE order_id = %s ORDER BY created_at",
            (order_id,),
        ).fetchall()
    return [
        dict(zip(["payment_id", "order_id", "amount", "status", "authorization_id", "capture_id"], r))
        for r in rows
    ]


# ---------------------------------------------------------------------------
# 障害注入は external-stub 側に移した。運用画面からの経路を壊さないよう
# ここでは薄く中継するだけにしておく。
# ---------------------------------------------------------------------------
@app.get("/admin/chaos")
async def get_chaos():
    resp = await client.get(f"{GATEWAY_URL}/admin/chaos")
    # 状態を見ないと、エラー応答をそのまま「現在の設定」として返してしまい、
    # **運用画面には「注入オフ」と出るのに実際は注入されている**状態になる
    resp.raise_for_status()
    return resp.json()


@app.post("/admin/chaos")
async def set_chaos(
    latency_ms: int = 0,
    error_rate: float = 0.0,
    no_response_rate: float = 0.0,
):
    resp = await client.post(
        f"{GATEWAY_URL}/admin/chaos",
        params={
            "latency_ms": latency_ms,
            "error_rate": error_rate,
            "no_response_rate": no_response_rate,
        },
    )
    resp.raise_for_status()
    return resp.json()


# 旧 API。負荷試験スクリプトが叩いているので残す
class PaymentRequest(BaseModel):
    order_id: str
    amount: int


@app.post("/payments")
async def authorize_legacy(req: PaymentRequest):
    return await authorize(AuthorizeRequest(order_id=req.order_id, amount=req.amount))
