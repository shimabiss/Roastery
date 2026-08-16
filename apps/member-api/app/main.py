"""
member-api : 会員・セッション・住所帳・カートを担当するサービス。

この段 (P2) について
--------------------
**P2 は4つの学習テーマ (設計・可観測性・CI/CD・IaC) を1つも支えていない。**
それでも作るのは、会員必須と決めた以上、これが無いと1件も売れないからである。

  価値駆動 … 題材を支えるから作る (P1 / P3 / P4)
  依存駆動 … 無いと他が動かないから作る (この段)

**依存駆動のものは価値が見えないため後回しにされ、後回しにすると全部止まる。**
ユーザーストーリーの実装順で US-12/13 が3番目に繰り上がったのと同じ理由。

FR-118 について (要件を満たす手段は1つではない)
-----------------------------------------------
「期限を過ぎた未確認会員を失効させ、**同じメールアドレスで再登録できるようにする**」
は、定期実行のバッチでレコードを消すのが素直に見える。

ここでは **登録時に期限切れレコードを上書きする**ことで満たしている。
その結果 FR-1303 (未確認会員の失効バッチ) が丸ごと不要になった。
要件は「何を満たすか」であって「どう作るか」ではない。
"""

import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import httpx
import redis
from fastapi import FastAPI, HTTPException, Header
from psycopg_pool import ConnectionPool
from pydantic import BaseModel

from opentelemetry import trace, metrics
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

from . import security as sec
from . import schema
from .carts import CartStore, MAX_QTY_PER_LINE

tracer = trace.get_tracer("member-api")
meter = metrics.get_meter("member-api")

registrations = meter.create_counter(
    "members.registrations", unit="{member}", description="会員登録の件数"
)
logins = meter.create_counter("members.logins", unit="{login}", description="ログイン試行の結果")
cart_merges = meter.create_counter(
    "carts.merges", unit="{merge}", description="未ログインカートのマージ回数"
)
cart_trims = meter.create_counter(
    "carts.trimmed",
    unit="{trim}",
    description="マージ時に数量上限で切り詰めた回数。**合算を選んだ副作用の実測値**",
)

DB_DSN = os.getenv("DATABASE_URL", "postgresql://demo:demo@postgres:5432/demo")
REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
GATEWAY_URL = os.getenv("EXTERNAL_STUB_URL", "http://external-stub:8000")
SITE_URL = os.getenv("SITE_URL", "http://localhost:3000")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("member-api")

pool: ConnectionPool | None = None
client: httpx.AsyncClient | None = None
r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
carts = CartStore(r)


def _now() -> datetime:
    return datetime.now(timezone.utc)


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

    schema.apply(pool, log)
    client = httpx.AsyncClient(timeout=10.0)
    yield
    await client.aclose()
    pool.close()


app = FastAPI(title="member-api", lifespan=lifespan)
FastAPIInstrumentor.instrument_app(app, exclude_spans=["receive", "send"])


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


# ===========================================================================
# 会員登録 (FR-101 / FR-102 / FR-114 / FR-117 / FR-118)
# ===========================================================================
class RegisterRequest(BaseModel):
    email: str
    password: str


async def _send_mail(to: str, subject: str, body: str) -> None:
    """メール送信は失敗しても本処理を止めない。

    UC-01 E5 と同じ判断。「メールが送れなかったから登録を取り消す」は誤り。
    """
    try:
        resp = await client.post(
            f"{GATEWAY_URL}/mail/send", json={"to": to, "subject": subject, "body": body}
        )
        # httpx は 4xx/5xx で例外を投げない。これが無いと
        # **確認メールが送れていないのに送れたことになり**、
        # 会員は永久に確認できないのに、ログにも痕跡が残らない
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        trace.get_current_span().add_event("mail.send_failed")
        log.error("mail send failed to=%s: %s", to, exc)


@app.post("/members", status_code=201)
async def register(req: RegisterRequest):
    email = sec.normalize_email(req.email)
    if not sec.is_valid_email(email):
        raise HTTPException(status_code=422, detail="メールアドレスの形式が正しくありません")
    try:
        sec.check_password_policy(req.password)
    except sec.PolicyError as exc:
        # FR-114 / US-12: **具体的に**伝える。「不正です」だけでは次の行動が分からない
        raise HTTPException(status_code=422, detail=str(exc))

    token = sec.new_token()
    expires = _now() + timedelta(hours=sec.VERIFY_TOKEN_TTL_HOURS)

    with tracer.start_as_current_span("register-member") as span:
        with pool.connection() as conn:
            row = conn.execute(
                "SELECT id, email_verified_at, verify_expires_at FROM members "
                "WHERE lower(email) = %s AND status <> 'WITHDRAWN'",
                (email,),
            ).fetchone()

            if row is not None:
                member_id, verified_at, verify_expires = row
                # ---------------------------------------------------------
                # FR-118 / BR-24 / US-16 の2つ目の受入基準。
                #
                # **未確認のまま失効したメールアドレスは、再登録できなければならない。**
                # ここが無いと「登録もログインもできない」出口のない状態になる。
                # システム上は「既に使われている」ので、永久に登録できない。
                # ---------------------------------------------------------
                expired = verify_expires is not None and verify_expires < _now()
                if verified_at is None and expired:
                    span.add_event("member.expired_registration_overwritten")
                    conn.execute(
                        "UPDATE members SET password_hash = %s, verify_token = %s, "
                        "verify_expires_at = %s, failed_login_count = 0, locked_until = NULL, "
                        "updated_at = now() WHERE id = %s",
                        (sec.hash_password(req.password), token, expires, member_id),
                    )
                    log.info("expired unverified registration overwritten email=%s", email)
                    await _send_mail(
                        email,
                        "【Roastery】メールアドレスのご確認",
                        f"以下のリンクから確認を完了してください（24時間有効）\n"
                        f"{SITE_URL}/verify?token={token}",
                    )
                    registrations.add(1, {"result": "reregistered"})
                    return {"member_id": member_id, "email": email, "email_verified": False}

                registrations.add(1, {"result": "duplicate"})
                raise HTTPException(
                    status_code=409, detail="このメールアドレスは既に登録されています"
                )

            member_id = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO members (id, email, password_hash, verify_token, verify_expires_at) "
                "VALUES (%s, %s, %s, %s, %s)",
                (member_id, email, sec.hash_password(req.password), token, expires),
            )
        span.set_attribute("member.id", member_id)

    await _send_mail(
        email,
        "【Roastery】メールアドレスのご確認",
        f"以下のリンクから確認を完了してください（24時間有効）\n{SITE_URL}/verify?token={token}",
    )
    registrations.add(1, {"result": "created"})
    log.info("member registered id=%s", member_id)
    # BR-19: ログインはできるが購入はできない、という中間状態から始まる
    return {"member_id": member_id, "email": email, "email_verified": False}


@app.post("/members/verify")
async def verify_email(token: str):
    """FR-102 / FR-117。確認リンクは 24 時間で失効する (BR-24)。"""
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT id, verify_expires_at, email_verified_at FROM members WHERE verify_token = %s",
            (token,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="確認リンクが正しくありません")
        member_id, expires, verified_at = row
        if verified_at is not None:
            return {"member_id": member_id, "email_verified": True, "note": "already verified"}
        if expires is None or expires < _now():
            # US-16 の1つ目の受入基準: 期限切れであることと、再登録できることを伝える
            raise HTTPException(
                status_code=410,
                detail="確認リンクの有効期限が切れています。同じメールアドレスで登録し直してください。",
            )
        conn.execute(
            "UPDATE members SET email_verified_at = now(), verify_token = NULL, "
            "verify_expires_at = NULL, updated_at = now() WHERE id = %s",
            (member_id,),
        )
    log.info("email verified member_id=%s", member_id)
    return {"member_id": member_id, "email_verified": True}


# ===========================================================================
# ログイン / セッション (FR-103 / FR-112 / FR-113)
# ===========================================================================
class LoginRequest(BaseModel):
    email: str
    password: str
    # ログイン時に未ログインカートを引き継ぐ (FR-306)
    cart_id: str | None = None


@app.post("/sessions")
async def login(req: LoginRequest):
    email = sec.normalize_email(req.email)

    with tracer.start_as_current_span("login") as span:
        with pool.connection() as conn:
            row = conn.execute(
                "SELECT id, password_hash, email_verified_at, failed_login_count, locked_until "
                "FROM members WHERE lower(email) = %s AND status <> 'WITHDRAWN'",
                (email,),
            ).fetchone()

            # 存在しない場合も同じ文言・同じ経路にする。
            # 「そのメールアドレスは登録されていません」と返すと、
            # **どのアドレスが登録済みかを外部から列挙できてしまう。**
            if row is None:
                logins.add(1, {"result": "no_such_member"})
                raise HTTPException(
                    status_code=401, detail="メールアドレスまたはパスワードが正しくありません"
                )

            member_id, pw_hash, verified_at, failed, locked_until = row

            # FR-113: ログイン試行の制限 (US-13 の2つ目の受入基準)
            if locked_until is not None and locked_until > _now():
                span.add_event("login.locked")
                logins.add(1, {"result": "locked"})
                remaining = int((locked_until - _now()).total_seconds() // 60) + 1
                raise HTTPException(
                    status_code=429,
                    detail=f"試行回数の上限を超えました。約 {remaining} 分後に再度お試しください。",
                )

            if not sec.verify_password(req.password, pw_hash):
                failed = (failed or 0) + 1
                lock_to = (
                    _now() + timedelta(minutes=sec.LOCK_DURATION_MINUTES)
                    if failed >= sec.MAX_FAILED_LOGINS
                    else None
                )
                conn.execute(
                    "UPDATE members SET failed_login_count = %s, locked_until = %s, "
                    "updated_at = now() WHERE id = %s",
                    (failed, lock_to, member_id),
                )
                logins.add(1, {"result": "bad_password"})
                raise HTTPException(
                    status_code=401, detail="メールアドレスまたはパスワードが正しくありません"
                )

            session_id = sec.new_token()
            conn.execute(
                "UPDATE members SET failed_login_count = 0, locked_until = NULL, "
                "updated_at = now() WHERE id = %s",
                (member_id,),
            )
            conn.execute(
                "INSERT INTO sessions (id, member_id, expires_at) VALUES (%s, %s, %s)",
                (session_id, member_id, _now() + timedelta(hours=sec.SESSION_TTL_HOURS)),
            )

        span.set_attribute("member.id", member_id)
        span.set_attribute("member.email_verified", verified_at is not None)

        # -------------------------------------------------------------------
        # FR-306 / BR-20: ログインしたので、未ログインカートを会員カートへ統合する
        # -------------------------------------------------------------------
        member_cart = _member_cart_id(member_id)
        trimmed: list[str] = []
        if req.cart_id:
            with tracer.start_as_current_span("merge-cart") as merge_span:
                _, trimmed = carts.merge_into(_guest_cart_id(req.cart_id), member_cart)
                merge_span.set_attribute("cart.trimmed_count", len(trimmed))
                cart_merges.add(1, {})
                if trimmed:
                    cart_trims.add(1, {})

    logins.add(1, {"result": "ok"})
    log.info("login ok member_id=%s", member_id)
    return {
        "session_id": session_id,
        "member_id": member_id,
        # BR-19: 未確認でもログインはできる。**購入だけができない。**
        # 「確認が済むまでログインさせない」ほうが実装は単純だが、
        # 利用者は自分が何をすればよいか分からないまま締め出される。
        "email_verified": verified_at is not None,
        "cart": carts.get(member_cart),
        "cart_trimmed": trimmed,
    }


@app.delete("/sessions/{session_id}")
async def logout(session_id: str):
    with pool.connection() as conn:
        conn.execute("DELETE FROM sessions WHERE id = %s", (session_id,))
    return {"status": "logged_out"}


def _load_session(session_id: str | None) -> dict | None:
    """FR-112: 期限切れのセッションは無効として扱う。"""
    if not session_id:
        return None
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT s.id, s.member_id, s.expires_at, m.email, m.email_verified_at, m.status "
            "FROM sessions s JOIN members m ON m.id = s.member_id WHERE s.id = %s",
            (session_id,),
        ).fetchone()
    if row is None:
        return None
    _, member_id, expires, email, verified_at, status = row
    if expires < _now() or status == "WITHDRAWN":
        return None
    return {
        "member_id": member_id,
        "email": email,
        "email_verified": verified_at is not None,
    }


@app.get("/sessions/current")
async def current_session(x_session_id: str | None = Header(None)):
    """BFF が毎リクエストで叩く。認証のホップがトレース上に1つ増える。"""
    session = _load_session(x_session_id)
    if session is None:
        raise HTTPException(status_code=401, detail="ログインしていません")
    return session


# ===========================================================================
# カート (FR-306 / FR-310 / FR-311)
# ===========================================================================
def _member_cart_id(member_id: str) -> str:
    return f"m:{member_id}"


def _guest_cart_id(cart_id: str) -> str:
    return f"g:{cart_id}"


def _resolve_cart(session_id: str | None, cart_id: str | None) -> tuple[str, dict | None]:
    """ログイン中なら会員カート、そうでなければ未ログインカートを使う。

    **ここが「カートは会員を持たないことがある」(多重度 0..1) の実装。**
    注文が必ず会員を持つ (1) のとは非対称であり、
    「会員必須」の一言ではこの差を表現できない。
    """
    session = _load_session(session_id)
    if session:
        return _member_cart_id(session["member_id"]), session
    if not cart_id:
        raise HTTPException(status_code=400, detail="cart id required")
    return _guest_cart_id(cart_id), None


def _cart_response(key: str, session: dict | None, trimmed: list[str] | None = None) -> dict:
    return {
        "lines": carts.get(key),
        "max_quantity_per_line": MAX_QTY_PER_LINE,
        "logged_in": session is not None,
        "email_verified": bool(session and session["email_verified"]),
        "trimmed": trimmed or [],
    }


@app.get("/carts")
async def get_cart(x_session_id: str | None = Header(None), x_cart_id: str | None = Header(None)):
    key, session = _resolve_cart(x_session_id, x_cart_id)
    return _cart_response(key, session)


class CartLineRequest(BaseModel):
    sku: str
    quantity: int = 1


@app.post("/carts/items")
async def add_item(
    req: CartLineRequest,
    x_session_id: str | None = Header(None),
    x_cart_id: str | None = Header(None),
):
    """BR-18: **カートへの投入にログインは不要。**

    関門は購入手続きの開始に置く。閲覧からカート投入までは摩擦をなくし、
    購入の直前に確実な関門 (ログイン済み・メール確認済み) を置く、という組み合わせ。
    """
    key, session = _resolve_cart(x_session_id, x_cart_id)
    before = carts.get(key).get(req.sku, 0)
    carts.add_line(key, req.sku, req.quantity)
    after = carts.get(key).get(req.sku, 0)
    trimmed = [req.sku] if after < before + req.quantity else []
    return _cart_response(key, session, trimmed)


@app.put("/carts/items/{sku}")
async def set_item(
    sku: str,
    req: CartLineRequest,
    x_session_id: str | None = Header(None),
    x_cart_id: str | None = Header(None),
):
    key, session = _resolve_cart(x_session_id, x_cart_id)
    carts.set_line(key, sku, req.quantity)
    return _cart_response(key, session)


@app.delete("/carts/items/{sku}")
async def delete_item(
    sku: str,
    x_session_id: str | None = Header(None),
    x_cart_id: str | None = Header(None),
):
    key, session = _resolve_cart(x_session_id, x_cart_id)
    carts.set_line(key, sku, 0)
    return _cart_response(key, session)


@app.delete("/carts")
async def clear_cart(
    x_session_id: str | None = Header(None), x_cart_id: str | None = Header(None)
):
    key, session = _resolve_cart(x_session_id, x_cart_id)
    carts.clear(key)
    return _cart_response(key, session)


@app.get("/members/{member_id}/email")
async def member_email(member_id: str):
    """発送通知 (FR-1002) の宛先を解決するための内部 API。

    **匿名化済みの会員には送らない** (BR-21)。退会後に通知が飛ぶのは事故。
    """
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT email, status FROM members WHERE id = %s", (member_id,)
        ).fetchone()
    if row is None or row[1] == "WITHDRAWN":
        raise HTTPException(status_code=404, detail="unknown member")
    return {"member_id": member_id, "email": row[0]}


# ===========================================================================
# 住所帳 (FR-106 / FR-701)
# ===========================================================================
class AddressRequest(BaseModel):
    label: str = ""
    recipient: str
    postal_code: str
    prefecture: str
    city: str
    address_line: str
    phone: str
    is_default: bool = False


def _require_session(session_id: str | None) -> dict:
    session = _load_session(session_id)
    if session is None:
        raise HTTPException(status_code=401, detail="ログインしていません")
    return session


def _address_rows(member_id: str) -> list[dict]:
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT id, label, recipient, postal_code, prefecture, city, address_line, "
            "phone, is_default FROM addresses WHERE member_id = %s "
            "ORDER BY is_default DESC, created_at",
            (member_id,),
        ).fetchall()
    keys = [
        "id", "label", "recipient", "postal_code", "prefecture",
        "city", "address_line", "phone", "is_default",
    ]
    return [dict(zip(keys, r)) for r in rows]


@app.get("/addresses")
async def list_addresses(x_session_id: str | None = Header(None)):
    session = _require_session(x_session_id)
    return _address_rows(session["member_id"])


@app.post("/addresses", status_code=201)
async def create_address(req: AddressRequest, x_session_id: str | None = Header(None)):
    session = _require_session(x_session_id)
    address_id = str(uuid.uuid4())
    with pool.connection() as conn:
        with conn.transaction():
            existing = conn.execute(
                "SELECT count(*) FROM addresses WHERE member_id = %s", (session["member_id"],)
            ).fetchone()[0]
            # 1件目は自動で既定にする。US-10「既定の配送先が初期表示される」は、
            # **既定が1つも無いと成立しない。**
            make_default = req.is_default or existing == 0
            if make_default:
                conn.execute(
                    "UPDATE addresses SET is_default = false WHERE member_id = %s",
                    (session["member_id"],),
                )
            conn.execute(
                "INSERT INTO addresses (id, member_id, label, recipient, postal_code, "
                "prefecture, city, address_line, phone, is_default) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    address_id, session["member_id"], req.label, req.recipient, req.postal_code,
                    req.prefecture, req.city, req.address_line, req.phone, make_default,
                ),
            )
    return {"id": address_id, "addresses": _address_rows(session["member_id"])}


@app.put("/addresses/{address_id}/default")
async def set_default_address(address_id: str, x_session_id: str | None = Header(None)):
    session = _require_session(x_session_id)
    with pool.connection() as conn:
        with conn.transaction():
            owned = conn.execute(
                "SELECT 1 FROM addresses WHERE id = %s AND member_id = %s",
                (address_id, session["member_id"]),
            ).fetchone()
            # **他人の住所を既定にできないこと。** ID を知っているだけで
            # 他人のデータを触れる、が最も起きやすい認可の穴
            if owned is None:
                raise HTTPException(status_code=404, detail="unknown address")
            conn.execute(
                "UPDATE addresses SET is_default = false WHERE member_id = %s",
                (session["member_id"],),
            )
            conn.execute("UPDATE addresses SET is_default = true WHERE id = %s", (address_id,))
    return _address_rows(session["member_id"])


@app.delete("/addresses/{address_id}")
async def delete_address(address_id: str, x_session_id: str | None = Header(None)):
    session = _require_session(x_session_id)
    with pool.connection() as conn:
        conn.execute(
            "DELETE FROM addresses WHERE id = %s AND member_id = %s",
            (address_id, session["member_id"]),
        )
    return _address_rows(session["member_id"])


@app.get("/addresses/{address_id}")
async def get_address(address_id: str, x_session_id: str | None = Header(None)):
    session = _require_session(x_session_id)
    found = [a for a in _address_rows(session["member_id"]) if a["id"] == address_id]
    if not found:
        raise HTTPException(status_code=404, detail="unknown address")
    return found[0]


# ===========================================================================
# 購入可否の判定 (BR-18 / BR-19)
# ===========================================================================
@app.get("/members/can-checkout")
async def can_checkout(x_session_id: str | None = Header(None)):
    """購入手続きに進めるかどうかを1箇所で判定する。

    条件は2つ。**ログイン済み** (BR-18) かつ **メール確認済み** (BR-19)。
    分けて返すのは、利用者に「次に何をすればよいか」を伝えるため。
    「購入できません」だけでは、登録すべきか確認すべきか分からない。
    """
    session = _load_session(x_session_id)
    if session is None:
        return {"allowed": False, "reason": "not_logged_in", "logged_in": False}
    if not session["email_verified"]:
        return {
            "allowed": False,
            "reason": "email_not_verified",
            "logged_in": True,
            "email": session["email"],
        }
    return {"allowed": True, "logged_in": True, **session}
