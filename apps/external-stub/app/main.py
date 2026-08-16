"""
external-stub : 外部サービス (決済代行 / メール配信 / 配送業者) の模擬。

なぜ1つのサービスにまとめているか
----------------------------------
スタブは **学習対象ではない** ため、サービスを分ける理由がない。
分ければ Azure Container Apps のコンテナが3つ増え、IaC とデプロイのコストだけが
上がる。分散トレースの見え方としても「外部への1ホップ」が表現できれば十分。
  -> docs/requirements/07-implementation-scope.md 「4. スタブサービスの設計」

障害注入について
----------------
``/admin/chaos`` で **遅延・エラー率・無応答** の3つを設定できる。
このうち **無応答 (no_response) が最も重要** で、
UC-01 E3 と UC-06 E2 はいずれも「決済代行が応答しない」場面である。

  エラーを返すのと、応答が返ってこないのは **まったく別の異常系**。

前者は「失敗した」と分かるので巻き戻せばよいが、
後者は「成立したかどうか分からない」。だから冪等キー (BR-28) が要る。
片方しか再現できないと、テストが片肺になる。
"""

import asyncio
import logging
import os
import random
import time
import uuid

from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel

from opentelemetry import trace, metrics
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.trace import Status, StatusCode

tracer = trace.get_tracer("external-stub")
meter = metrics.get_meter("external-stub")

gateway_calls = meter.create_counter(
    "external.gateway.calls",
    unit="{call}",
    description="外部サービス呼び出しの件数",
)
gateway_latency = meter.create_histogram(
    "external.gateway.duration",
    unit="ms",
    description="外部サービス呼び出しの所要時間",
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("external-stub")

app = FastAPI(title="external-stub")
FastAPIInstrumentor.instrument_app(app, exclude_spans=["receive", "send"])

# ---------------------------------------------------------------------------
# 障害注入の設定 (実行中に書き換えられる)
# ---------------------------------------------------------------------------
CHAOS = {
    "latency_ms": int(os.getenv("CHAOS_LATENCY_MS", "0")),
    "error_rate": float(os.getenv("CHAOS_ERROR_RATE", "0")),
    # 応答を返さない確率。呼び出し側をタイムアウトさせる
    "no_response_rate": float(os.getenv("CHAOS_NO_RESPONSE_RATE", "0")),
    # 無応答時に何秒待たせるか (呼び出し側のタイムアウトより長くする)
    "no_response_hold_s": int(os.getenv("CHAOS_NO_RESPONSE_HOLD_S", "60")),
}

BASE_LATENCY_MS = (40, 90)

# 決済代行が持っている与信の台帳。プロセス内メモリで十分 (スタブなので)
AUTHORIZATIONS: dict[str, dict] = {}
# 送ったメールの控え。/mail/inbox で読める
MAILBOX: list[dict] = []


async def _simulate(kind: str) -> None:
    """外部サービスらしい振る舞い (ゆらぎ・遅延・エラー・無応答) を再現する。"""
    span = trace.get_current_span()
    span.set_attribute("chaos.latency_ms", CHAOS["latency_ms"])
    span.set_attribute("chaos.error_rate", CHAOS["error_rate"])
    span.set_attribute("chaos.no_response_rate", CHAOS["no_response_rate"])

    await asyncio.sleep(random.uniform(*BASE_LATENCY_MS) / 1000)

    if CHAOS["latency_ms"] > 0:
        span.add_event("chaos.latency_injected", {"delay_ms": CHAOS["latency_ms"]})
        await asyncio.sleep(CHAOS["latency_ms"] / 1000)

    if CHAOS["no_response_rate"] > 0 and random.random() < CHAOS["no_response_rate"]:
        # 呼び出し側のタイムアウトより長く待つ = 実質「応答が返らない」
        span.add_event("chaos.no_response_injected", {"hold_s": CHAOS["no_response_hold_s"]})
        gateway_calls.add(1, {"kind": kind, "result": "no_response"})
        log.error("no response injected kind=%s", kind)
        await asyncio.sleep(CHAOS["no_response_hold_s"])
        # ここに到達したら呼び出し側は既に諦めている
        raise HTTPException(status_code=504, detail="gateway timeout (injected)")

    if CHAOS["error_rate"] > 0 and random.random() < CHAOS["error_rate"]:
        err = RuntimeError(f"{kind} gateway error (injected)")
        span.record_exception(err)
        span.set_status(Status(StatusCode.ERROR, str(err)))
        gateway_calls.add(1, {"kind": kind, "result": "error"})
        log.error("error injected kind=%s", kind)
        raise HTTPException(status_code=500, detail=f"{kind} gateway error (injected)")


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


# ===========================================================================
# 管理
# ===========================================================================
@app.get("/admin/chaos")
async def get_chaos():
    return CHAOS


@app.post("/admin/chaos")
async def set_chaos(
    latency_ms: int = 0,
    error_rate: float = 0.0,
    no_response_rate: float = 0.0,
    no_response_hold_s: int = 60,
):
    """
    例::

        POST /admin/chaos?latency_ms=1500        全リクエストに 1.5 秒上乗せ
        POST /admin/chaos?error_rate=1.0         必ず 500 で失敗する
        POST /admin/chaos?no_response_rate=1.0   応答を返さない (呼び出し側がタイムアウト)
        POST /admin/chaos                        解除
    """
    CHAOS.update(
        latency_ms=latency_ms,
        error_rate=error_rate,
        no_response_rate=no_response_rate,
        no_response_hold_s=no_response_hold_s,
    )
    log.info("chaos updated: %s", CHAOS)
    return CHAOS


@app.post("/admin/reset")
async def reset():
    AUTHORIZATIONS.clear()
    MAILBOX.clear()
    return {"status": "reset"}


# ===========================================================================
# 決済代行
# ===========================================================================
class AuthorizeRequest(BaseModel):
    order_id: str
    amount: int
    # FR-501: カード情報そのものは受け取らない。トークンだけを受け取る。
    # BR-16「カード情報は自システムを通過させない」を型で表明している。
    card_token: str = "tok_test_visa"


@app.post("/payment/authorize")
async def authorize(req: AuthorizeRequest, idempotency_key: str | None = Header(None)):
    started = time.perf_counter()
    with tracer.start_as_current_span("gateway.authorize") as span:
        span.set_attribute("payment.order_id", req.order_id)
        span.set_attribute("payment.amount", req.amount)

        # 決済代行側も冪等キーを見る。同じキーなら同じ与信を返す。
        # 自システム側 (payment-api) と **二重に** 冪等性を持たせている。
        # 片方が落ちてももう片方が守る、という多層防御。
        if idempotency_key:
            for a in AUTHORIZATIONS.values():
                if a.get("idempotency_key") == idempotency_key:
                    span.add_event("gateway.idempotent_replay")
                    return a

        await _simulate("payment")

        auth_id = f"auth_{uuid.uuid4().hex[:12]}"
        record = {
            "authorization_id": auth_id,
            "order_id": req.order_id,
            "amount": req.amount,
            "status": "AUTHORIZED",
            "idempotency_key": idempotency_key,
        }
        AUTHORIZATIONS[auth_id] = record
        span.set_attribute("payment.authorization_id", auth_id)
        span.set_status(Status(StatusCode.OK))

    gateway_calls.add(1, {"kind": "payment", "result": "ok"})
    gateway_latency.record((time.perf_counter() - started) * 1000, {"kind": "authorize"})
    log.info("authorized order_id=%s auth=%s", req.order_id, auth_id)
    return record


class CaptureRequest(BaseModel):
    authorization_id: str


@app.post("/payment/capture")
async def capture(req: CaptureRequest, idempotency_key: str | None = Header(None)):
    """売上確定 (キャプチャ)。UC-06 手順8。"""
    with tracer.start_as_current_span("gateway.capture") as span:
        rec = AUTHORIZATIONS.get(req.authorization_id)
        if rec is None:
            raise HTTPException(status_code=404, detail="unknown authorization")
        span.set_attribute("payment.authorization_id", req.authorization_id)

        # 既に確定済みなら、そのまま同じ結果を返す。
        # **これが無いと、リトライのたびに二重請求になる。**
        if rec["status"] == "CAPTURED":
            span.add_event("gateway.already_captured")
            return rec
        if rec["status"] == "VOIDED":
            raise HTTPException(status_code=409, detail="authorization already voided")

        await _simulate("payment")

        rec["status"] = "CAPTURED"
        rec["capture_id"] = f"cap_{uuid.uuid4().hex[:12]}"
        span.set_status(Status(StatusCode.OK))

    gateway_calls.add(1, {"kind": "payment", "result": "ok"})
    log.info("captured auth=%s", req.authorization_id)
    return rec


class VoidRequest(BaseModel):
    authorization_id: str


@app.post("/payment/void")
async def void(req: VoidRequest):
    """与信の取り消し。UC-01 E2 / UC-02 手順4 の補償処理。"""
    with tracer.start_as_current_span("gateway.void") as span:
        rec = AUTHORIZATIONS.get(req.authorization_id)
        if rec is None:
            raise HTTPException(status_code=404, detail="unknown authorization")
        span.set_attribute("payment.authorization_id", req.authorization_id)

        if rec["status"] == "VOIDED":
            span.add_event("gateway.already_voided")
            return rec
        if rec["status"] == "CAPTURED":
            # 売上確定後は取り消せない。返金 (UC-03) の世界になる
            raise HTTPException(status_code=409, detail="already captured; refund instead")

        await _simulate("payment")
        rec["status"] = "VOIDED"
        span.set_status(Status(StatusCode.OK))

    log.info("voided auth=%s", req.authorization_id)
    return rec


@app.get("/payment/authorizations/{authorization_id}")
async def get_authorization(authorization_id: str):
    """UC-01 E3 で使う照会。無応答だった与信が成立していないかを後から確認する。"""
    rec = AUTHORIZATIONS.get(authorization_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="unknown authorization")
    return rec


@app.get("/payment/authorizations")
async def find_authorizations(order_id: str):
    """注文番号から与信を探す。**無応答だったときの唯一の手掛かり。**

    与信 ID が手元に無いまま応答が途切れた場合、注文番号で照会するしかない。
    UC-01 E3「与信が成立している可能性があるため、後続の照会処理で確認する」の実体。
    """
    return [a for a in AUTHORIZATIONS.values() if a["order_id"] == order_id]


# ===========================================================================
# メール配信
# ===========================================================================
class MailRequest(BaseModel):
    to: str
    subject: str
    body: str


@app.post("/mail/send")
async def send_mail(req: MailRequest):
    with tracer.start_as_current_span("gateway.mail.send") as span:
        span.set_attribute("mail.to", req.to)
        span.set_attribute("mail.subject", req.subject)
        await _simulate("mail")
        msg = {
            "id": f"msg_{uuid.uuid4().hex[:12]}",
            "to": req.to,
            "subject": req.subject,
            "body": req.body,
            "sent_at": time.time(),
        }
        MAILBOX.insert(0, msg)
        del MAILBOX[200:]
    gateway_calls.add(1, {"kind": "mail", "result": "ok"})
    log.info("mail sent to=%s subject=%s", req.to, req.subject)
    return msg


@app.get("/mail/inbox")
async def inbox(to: str | None = None, limit: int = 20):
    """送ったメールを読むための窓口。実サービスが無くても確認導線をデモできる。"""
    items = [m for m in MAILBOX if to is None or m["to"] == to]
    return items[:limit]


# ===========================================================================
# 配送業者
# ===========================================================================
class LabelRequest(BaseModel):
    order_id: str


@app.post("/shipping/label")
async def create_label(req: LabelRequest):
    with tracer.start_as_current_span("gateway.shipping.label") as span:
        span.set_attribute("shipping.order_id", req.order_id)
        await _simulate("shipping")
        tracking = f"RST{random.randint(10**11, 10**12 - 1)}"
        span.set_attribute("shipping.tracking_no", tracking)
    gateway_calls.add(1, {"kind": "shipping", "result": "ok"})
    return {"order_id": req.order_id, "tracking_no": tracking}
