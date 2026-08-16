"""
external-stub (外部サービスの模擬) の異常系テスト。

**UC-01 E2 / E3 と UC-06 E1 / E2 がそのままテストケースになっている。**
例外フローを列挙する形式 (ユースケース記述) を選んだ効用が、ここで回収される。

httpx の ASGI トランスポートで直接叩くので、コンテナを起動しなくても走る。
"""

import asyncio
import sys
import types

import httpx
import pytest

# OpenTelemetry の instrumentation はテスト環境に入れない (CI を軽くするため)。
# アプリのコードを変えずにテストから外せるのは、計装が「外付け」だからこそ。
_stub = types.ModuleType("opentelemetry.instrumentation.fastapi")


class _NoopInstrumentor:
    @staticmethod
    def instrument_app(app, **kwargs):
        pass


_stub.FastAPIInstrumentor = _NoopInstrumentor
sys.modules.setdefault("opentelemetry.instrumentation", types.ModuleType("opentelemetry.instrumentation"))
sys.modules["opentelemetry.instrumentation.fastapi"] = _stub

from conftest import load  # noqa: E402

stub = load("external-stub", "main")


@pytest.fixture()
async def client():
    stub.AUTHORIZATIONS.clear()
    stub.MAILBOX.clear()
    stub.CHAOS.update(latency_ms=0, error_rate=0.0, no_response_rate=0.0, no_response_hold_s=60)
    transport = httpx.ASGITransport(app=stub.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://stub") as c:
        yield c


# ---------------------------------------------------------------------------
# BR-28: 冪等キー。二重に請求してはならない
# ---------------------------------------------------------------------------
async def test_authorize_is_idempotent(client):
    body = {"order_id": "o1", "amount": 100}
    a1 = (await client.post("/payment/authorize", json=body, headers={"idempotency-key": "k1"})).json()
    a2 = (await client.post("/payment/authorize", json=body, headers={"idempotency-key": "k1"})).json()
    assert a1["authorization_id"] == a2["authorization_id"]
    assert len(stub.AUTHORIZATIONS) == 1, "与信は1件しか作られない"


async def test_capture_never_charges_twice(client):
    a = (await client.post("/payment/authorize", json={"order_id": "o1", "amount": 100})).json()
    c1 = (await client.post("/payment/capture", json={"authorization_id": a["authorization_id"]})).json()
    c2 = (await client.post("/payment/capture", json={"authorization_id": a["authorization_id"]})).json()
    assert c1["capture_id"] == c2["capture_id"], "再送しても同じ売上確定を返す"


async def test_void_is_idempotent(client):
    a = (await client.post("/payment/authorize", json={"order_id": "o2", "amount": 50})).json()
    for _ in range(3):
        r = await client.post("/payment/void", json={"authorization_id": a["authorization_id"]})
        assert r.status_code == 200
        assert r.json()["status"] == "VOIDED"


# ---------------------------------------------------------------------------
# 状態遷移の禁則
# ---------------------------------------------------------------------------
async def test_cannot_void_after_capture(client):
    """売上確定後は取り消せない。返品 (UC-03) の世界になる。"""
    a = (await client.post("/payment/authorize", json={"order_id": "o1", "amount": 100})).json()
    await client.post("/payment/capture", json={"authorization_id": a["authorization_id"]})
    r = await client.post("/payment/void", json={"authorization_id": a["authorization_id"]})
    assert r.status_code == 409


async def test_cannot_capture_after_void(client):
    a = (await client.post("/payment/authorize", json={"order_id": "o1", "amount": 100})).json()
    await client.post("/payment/void", json={"authorization_id": a["authorization_id"]})
    r = await client.post("/payment/capture", json={"authorization_id": a["authorization_id"]})
    assert r.status_code == 409


# ---------------------------------------------------------------------------
# UC-01 E3: 応答が返らなかった与信を、注文番号から探す
# ---------------------------------------------------------------------------
async def test_find_authorization_by_order_id(client):
    """与信 ID が手元に無いときの唯一の手掛かり。

    応答が途切れた場合、与信 ID は返ってきていない。
    注文番号で照会できないと、成立した与信を **永久に取り消せない**。
    """
    await client.post("/payment/authorize", json={"order_id": "o9", "amount": 10})
    found = (await client.get("/payment/authorizations", params={"order_id": "o9"})).json()
    assert len(found) == 1
    assert found[0]["status"] == "AUTHORIZED"


# ---------------------------------------------------------------------------
# 障害注入
# ---------------------------------------------------------------------------
async def test_error_injection_returns_500(client):
    await client.post("/admin/chaos", params={"error_rate": 1.0})
    r = await client.post("/payment/authorize", json={"order_id": "o3", "amount": 10})
    assert r.status_code == 500


async def test_no_response_injection_makes_caller_time_out(client):
    """**エラーを返すのと応答が返らないのは別の異常系。**

    前者は「失敗した」と分かるので巻き戻せばよい。
    後者は「成立したか分からない」ので、冪等キーと照会が要る。
    片方しか再現できないとテストが片肺になる。
    """
    await client.post("/admin/chaos", params={"no_response_rate": 1.0, "no_response_hold_s": 3})
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(
            client.post("/payment/authorize", json={"order_id": "o4", "amount": 10}),
            timeout=0.4,
        )


async def test_latency_injection_delays_response(client):
    await client.post("/admin/chaos", params={"latency_ms": 300})
    loop = asyncio.get_running_loop()
    started = loop.time()
    r = await client.post("/payment/authorize", json={"order_id": "o5", "amount": 10})
    assert r.status_code == 200
    assert loop.time() - started >= 0.3


# ---------------------------------------------------------------------------
# メール / 配送
# ---------------------------------------------------------------------------
async def test_mail_is_readable_from_inbox(client):
    await client.post("/mail/send", json={"to": "a@example.com", "subject": "確認", "body": "本文"})
    await client.post("/mail/send", json={"to": "b@example.com", "subject": "他", "body": "本文"})
    inbox = (await client.get("/mail/inbox", params={"to": "a@example.com"})).json()
    assert len(inbox) == 1
    assert inbox[0]["subject"] == "確認"


async def test_shipping_label_returns_tracking_number(client):
    r = (await client.post("/shipping/label", json={"order_id": "o1"})).json()
    assert r["tracking_no"].startswith("RST")
