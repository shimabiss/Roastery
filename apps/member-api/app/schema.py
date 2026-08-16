"""
起動時に流す冪等な DDL。

なぜ init.sql と別に要るのか
---------------------------
postgres の ``docker-entrypoint-initdb.d`` は **空のボリュームでしか動かない**。
つまり init.sql だけだと、既に一度起動したことのある環境には
新しいテーブルも新しい列も一生入らない。

「新規構築」と「既存環境の更新」は別の仕事であり、
片方だけの仕組みでは必ずどちらかが壊れる。ここは後者を担当する。

すべて ``IF NOT EXISTS`` / ``ADD COLUMN IF NOT EXISTS`` で書いてあるので、
何度実行しても同じ結果になる (冪等)。CI から何度流しても壊れない、というのが
マイグレーションに求められる最低条件。
"""

# 旧スキーマ (orders が sku / quantity / amount を直接持っていた頃) からの移行も含む。
# 旧列は DROP せず NOT NULL だけ外す。**消すのは、消しても困らないと確認できてから**。
MIGRATIONS = [
    # --- 会員 -------------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS members (
        id                 TEXT PRIMARY KEY,
        email              TEXT        NOT NULL,
        password_hash      TEXT        NOT NULL,
        email_verified_at  TIMESTAMPTZ,
        verify_token       TEXT,
        verify_expires_at  TIMESTAMPTZ,
        failed_login_count INTEGER     NOT NULL DEFAULT 0,
        locked_until       TIMESTAMPTZ,
        status             TEXT        NOT NULL DEFAULT 'ACTIVE',
        withdrawn_at       TIMESTAMPTZ,
        created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS uq_members_email_active
        ON members (lower(email)) WHERE status <> 'WITHDRAWN'
    """,
    """
    CREATE TABLE IF NOT EXISTS addresses (
        id           TEXT PRIMARY KEY,
        member_id    TEXT        NOT NULL REFERENCES members (id) ON DELETE CASCADE,
        label        TEXT        NOT NULL DEFAULT '',
        recipient    TEXT        NOT NULL,
        postal_code  TEXT        NOT NULL,
        prefecture   TEXT        NOT NULL,
        city         TEXT        NOT NULL,
        address_line TEXT        NOT NULL,
        phone        TEXT        NOT NULL,
        is_default   BOOLEAN     NOT NULL DEFAULT false,
        created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_addresses_member ON addresses (member_id)",
    """
    CREATE TABLE IF NOT EXISTS sessions (
        id         TEXT PRIMARY KEY,
        member_id  TEXT        NOT NULL REFERENCES members (id) ON DELETE CASCADE,
        expires_at TIMESTAMPTZ NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_sessions_member ON sessions (member_id)",
    # --- 注文 -------------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS orders (
        id           TEXT PRIMARY KEY,
        member_id    TEXT,
        status       TEXT        NOT NULL,
        subtotal     INTEGER     NOT NULL DEFAULT 0,
        shipping_fee INTEGER     NOT NULL DEFAULT 0,
        total        INTEGER     NOT NULL DEFAULT 0,
        ship_to      JSONB,
        tracking_no  TEXT,
        version      INTEGER     NOT NULL DEFAULT 1,
        created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS member_id    TEXT",
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS subtotal     INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS shipping_fee INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS total        INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS ship_to      JSONB",
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS tracking_no  TEXT",
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS version      INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()",
    # 旧スキーマの列。明細が order_items に移ったので NOT NULL を外す
    "ALTER TABLE orders ALTER COLUMN sku      DROP NOT NULL",
    "ALTER TABLE orders ALTER COLUMN quantity DROP NOT NULL",
    "ALTER TABLE orders ALTER COLUMN amount   DROP NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_orders_status ON orders (status)",
    "CREATE INDEX IF NOT EXISTS idx_orders_member ON orders (member_id)",
    """
    CREATE TABLE IF NOT EXISTS order_items (
        id         BIGSERIAL PRIMARY KEY,
        order_id   TEXT    NOT NULL REFERENCES orders (id) ON DELETE CASCADE,
        sku        TEXT    NOT NULL,
        name       TEXT    NOT NULL DEFAULT '',
        unit_price INTEGER NOT NULL,
        quantity   INTEGER NOT NULL,
        subtotal   INTEGER NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_order_items_order ON order_items (order_id)",
    """
    CREATE TABLE IF NOT EXISTS order_events (
        id         BIGSERIAL PRIMARY KEY,
        order_id   TEXT        NOT NULL,
        from_state TEXT,
        to_state   TEXT        NOT NULL,
        actor      TEXT        NOT NULL DEFAULT 'system',
        note       TEXT        NOT NULL DEFAULT '',
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_order_events_order ON order_events (order_id, created_at)",
    # --- 決済 -------------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS payments (
        id               TEXT PRIMARY KEY,
        order_id         TEXT        NOT NULL,
        amount           INTEGER     NOT NULL,
        status           TEXT        NOT NULL,
        authorization_id TEXT,
        capture_id       TEXT,
        created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_payments_order ON payments (order_id)",
    """
    CREATE TABLE IF NOT EXISTS idempotency_keys (
        key         TEXT PRIMARY KEY,
        scope       TEXT        NOT NULL,
        request_ref TEXT        NOT NULL,
        response    JSONB,
        created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
]


def apply(pool, log) -> None:
    """接続プールに対してマイグレーションを順に流す。

    1文ずつ独立したトランザクションで実行する。理由は、旧スキーマが無い環境で
    ``ALTER TABLE orders ALTER COLUMN sku DROP NOT NULL`` のような文が
    「列が無い」で失敗しても、**後続を巻き込まないようにするため**。
    """
    applied, skipped = 0, 0
    for stmt in MIGRATIONS:
        try:
            with pool.connection() as conn:
                conn.execute(stmt)
            applied += 1
        except Exception as exc:  # noqa: BLE001
            # 旧列が存在しない環境では DROP NOT NULL が落ちる。想定内なので握る。
            skipped += 1
            log.debug("migration skipped: %s (%s)", stmt.strip()[:60], exc)
    log.info("schema ready (applied=%s skipped=%s)", applied, skipped)
