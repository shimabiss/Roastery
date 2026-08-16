-- ---------------------------------------------------------------------------
-- Roastery のスキーマ。
--
-- このファイルは **空のボリュームに対してのみ** 実行される
-- (postgres の docker-entrypoint-initdb.d の仕様)。
-- 既存ボリュームがある場合は各サービス起動時の冪等 DDL が差分を埋める。
--   -> apps/*/app/schema.py
--
-- 「初期化スクリプト」と「マイグレーション」を分けているのは、
-- 前者が新規構築、後者が既存環境の更新という **別の仕事** だからである。
-- 片方だけだと、必ずどちらかの環境で動かなくなる。
-- ---------------------------------------------------------------------------

-- ===========================================================================
-- 会員 (P2)
-- ===========================================================================
CREATE TABLE IF NOT EXISTS members (
    id                  TEXT PRIMARY KEY,
    email               TEXT        NOT NULL,
    password_hash       TEXT        NOT NULL,
    -- FR-102 / BR-19: 確認が完了するまで購入できない
    email_verified_at   TIMESTAMPTZ,
    -- FR-117 / BR-24: 確認リンクは 24 時間で失効する
    verify_token        TEXT,
    verify_expires_at   TIMESTAMPTZ,
    -- FR-113: ログイン試行の制限
    failed_login_count  INTEGER     NOT NULL DEFAULT 0,
    locked_until        TIMESTAMPTZ,
    -- FR-107 / BR-21: 退会しても会員レコードは残し、個人情報だけ匿名化する
    status              TEXT        NOT NULL DEFAULT 'ACTIVE',
    withdrawn_at        TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- FR-118 / BR-24: 「未確認かつ期限切れ」は再登録で上書きするため、
-- email の一意制約は **有効な会員に対してのみ** 掛ける。
-- 部分ユニークインデックスにしているのがこの要件の実装そのもの。
CREATE UNIQUE INDEX IF NOT EXISTS uq_members_email_active
    ON members (lower(email))
    WHERE status <> 'WITHDRAWN';

-- ===========================================================================
-- 住所帳 (P3)
-- ===========================================================================
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
);
CREATE INDEX IF NOT EXISTS idx_addresses_member ON addresses (member_id);

-- ===========================================================================
-- 注文 (P1)
-- ===========================================================================
CREATE TABLE IF NOT EXISTS orders (
    id            TEXT PRIMARY KEY,
    -- P2 までは NULL 可。会員必須になった後も、既存データのために NOT NULL にしない
    member_id     TEXT,
    -- ACCEPTED / PREPARING / SHIPPED / DELIVERED / CANCELLED / RETURNED / FAILED
    status        TEXT        NOT NULL,
    subtotal      INTEGER     NOT NULL DEFAULT 0,
    shipping_fee  INTEGER     NOT NULL DEFAULT 0,
    total         INTEGER     NOT NULL DEFAULT 0,
    -- BR-25: 配送先は注文側へ複写する。住所帳を後で変えても過去の注文は変わらない
    ship_to       JSONB,
    tracking_no   TEXT,
    -- 楽観ロック用。BR-26 の排他制御に使う
    version       INTEGER     NOT NULL DEFAULT 1,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_orders_created_at ON orders (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_orders_status     ON orders (status);
CREATE INDEX IF NOT EXISTS idx_orders_member     ON orders (member_id);

-- FR-408 / BR-04: 1回の手続きで確定した明細は1つの注文に属する
CREATE TABLE IF NOT EXISTS order_items (
    id         BIGSERIAL PRIMARY KEY,
    order_id   TEXT    NOT NULL REFERENCES orders (id) ON DELETE CASCADE,
    sku        TEXT    NOT NULL,
    name       TEXT    NOT NULL DEFAULT '',
    -- BR-01: 価格は注文確定時点のものを **コピーする**。商品を参照しない
    unit_price INTEGER NOT NULL,
    quantity   INTEGER NOT NULL,
    subtotal   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_order_items_order ON order_items (order_id);

-- 注文の状態遷移の履歴。UC-06 のデモで「いつ誰が動かしたか」を見せる
CREATE TABLE IF NOT EXISTS order_events (
    id         BIGSERIAL PRIMARY KEY,
    order_id   TEXT        NOT NULL,
    from_state TEXT,
    to_state   TEXT        NOT NULL,
    actor      TEXT        NOT NULL DEFAULT 'system',
    note       TEXT        NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_order_events_order ON order_events (order_id, created_at);

-- ===========================================================================
-- 決済 (P1 / P4)
-- ===========================================================================
CREATE TABLE IF NOT EXISTS payments (
    id               TEXT PRIMARY KEY,
    order_id         TEXT        NOT NULL,
    amount           INTEGER     NOT NULL,
    -- AUTHORIZED / CAPTURED / VOIDED / FAILED
    status           TEXT        NOT NULL,
    authorization_id TEXT,
    capture_id       TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_payments_order ON payments (order_id);

-- FR-512 / BR-28: 冪等キー。
-- 「同じキーなら同じ結果を返す」ための記録であり、**決済の本体とは別のテーブル**。
-- 応答が返らなかったときに再送しても二重に請求しないための唯一の担保。
CREATE TABLE IF NOT EXISTS idempotency_keys (
    key         TEXT PRIMARY KEY,
    scope       TEXT        NOT NULL,
    request_ref TEXT        NOT NULL,
    response    JSONB,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ===========================================================================
-- セッション (P2)
-- ===========================================================================
CREATE TABLE IF NOT EXISTS sessions (
    id         TEXT PRIMARY KEY,
    member_id  TEXT        NOT NULL REFERENCES members (id) ON DELETE CASCADE,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_sessions_member ON sessions (member_id);
