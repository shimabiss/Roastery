"""
パスワードとトークンの扱い。**外部依存なし** なので単体でテストできる。

なぜハッシュ関数を直接使ってはいけないか
----------------------------------------
``sha256(password)`` は **絶対に使ってはならない。**
SHA-256 は「速い」ことが設計目標であり、総当たりも速いということでもある。

パスワードには **鍵導出関数 (KDF)** を使う。KDF はわざと遅く、
かつメモリを食うように作られていて、GPU による並列化が効きにくい。

ここでは標準ライブラリだけで完結する ``hashlib.scrypt`` を使う。
実務では argon2id や bcrypt を選ぶことが多いが、
**「専用の KDF を使う」という点が同じ**であれば、どれでも要件は満たす。
依存を増やさない判断としてここでは scrypt にしている。
"""

import hashlib
import hmac
import os
import re
import secrets

# scrypt のパラメータ。n を上げるほど遅く・重くなる (= 攻撃者も遅くなる)
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SALT_BYTES = 16

# FR-114 パスワードポリシー
MIN_PASSWORD_LENGTH = 10
MAX_PASSWORD_LENGTH = 128

# FR-113 ログイン試行の制限
MAX_FAILED_LOGINS = 5
LOCK_DURATION_MINUTES = 15

# FR-117 / BR-24 確認リンクの有効期限
VERIFY_TOKEN_TTL_HOURS = 24

# FR-112 セッションの有効期間
SESSION_TTL_HOURS = 24 * 7


class PolicyError(ValueError):
    """パスワードポリシー違反。**何が足りないかを具体的に伝える**ためのもの。"""


def check_password_policy(password: str) -> None:
    """FR-114。違反していたら ``PolicyError`` を投げる。

    US-12 の受入基準に「条件を満たしていないことが **具体的に** 表示される」とある。
    「パスワードが不正です」とだけ返すのは、要件としては満たしていても
    利用者は次に何をすればよいか分からない。だから理由を文にして返す。
    """
    problems: list[str] = []
    if len(password) < MIN_PASSWORD_LENGTH:
        problems.append(f"{MIN_PASSWORD_LENGTH} 文字以上にしてください")
    if len(password) > MAX_PASSWORD_LENGTH:
        problems.append(f"{MAX_PASSWORD_LENGTH} 文字以内にしてください")
    if not re.search(r"[A-Za-z]", password):
        problems.append("英字を1文字以上含めてください")
    if not re.search(r"[0-9]", password):
        problems.append("数字を1文字以上含めてください")
    if password and len(set(password)) == 1:
        problems.append("同じ文字の繰り返しは使えません")
    if problems:
        raise PolicyError("／".join(problems))


def hash_password(password: str) -> str:
    """``scrypt$<n>$<r>$<p>$<salt>$<hash>`` 形式で返す。

    パラメータを一緒に保存するのが要点。後でコストを上げたくなったとき、
    **古いハッシュも検証できる**ようにするため。
    埋め込んでいないと、パラメータ変更＝全員パスワード再設定になる。
    """
    salt = os.urandom(_SALT_BYTES)
    dk = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """保存済みハッシュと照合する。

    比較は ``hmac.compare_digest`` を使う。``==`` は先頭から比べて
    違った時点で返るため、**一致した文字数が処理時間に漏れる** (タイミング攻撃)。
    """
    try:
        algo, n, r, p, salt_hex, hash_hex = stored.split("$")
        if algo != "scrypt":
            return False
        dk = hashlib.scrypt(
            password.encode("utf-8"),
            salt=bytes.fromhex(salt_hex),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(bytes.fromhex(hash_hex)),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk, bytes.fromhex(hash_hex))


def new_token(nbytes: int = 32) -> str:
    """確認トークン・セッション ID に使う乱数。

    ``random`` ではなく ``secrets`` を使う。前者は乱数列を予測できる。
    """
    return secrets.token_urlsafe(nbytes)


def normalize_email(email: str) -> str:
    return email.strip().lower()


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def is_valid_email(email: str) -> bool:
    return bool(_EMAIL_RE.match(email.strip()))
