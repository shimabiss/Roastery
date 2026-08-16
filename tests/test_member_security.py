"""
パスワードとトークンのユニットテスト。

**FR-114（パスワードポリシー）は「具体的に伝える」ところまでが要件。**
US-12 の受入基準に「条件を満たしていないことが具体的に表示される」とあるので、
メッセージの中身までテストする。
"""

import pytest

from conftest import load

sec = load("member-api", "security")


# ---------------------------------------------------------------------------
# FR-114 パスワードポリシー
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "password, expected_hint",
    [
        ("short1", "10 文字以上"),
        ("abcdefghijkl", "数字を1文字以上"),
        ("123456789012", "英字を1文字以上"),
        ("aaaaaaaaaaaa", "同じ文字の繰り返し"),
    ],
)
def test_policy_rejects_with_specific_reason(password, expected_hint):
    """**「パスワードが不正です」だけでは要件を満たさない。**

    利用者が次に何をすればよいか分かる文言になっていること。
    """
    with pytest.raises(sec.PolicyError) as exc:
        sec.check_password_policy(password)
    assert expected_hint in str(exc.value)


def test_policy_accepts_valid_password():
    sec.check_password_policy("roastery2026")


def test_policy_reports_multiple_problems_at_once():
    """1つ直すたびに怒られるのは体験として悪い。まとめて返す。"""
    with pytest.raises(sec.PolicyError) as exc:
        sec.check_password_policy("abc")
    message = str(exc.value)
    assert "10 文字以上" in message
    assert "数字を1文字以上" in message


# ---------------------------------------------------------------------------
# ハッシュ化
# ---------------------------------------------------------------------------
def test_hash_is_verifiable():
    stored = sec.hash_password("roastery2026")
    assert sec.verify_password("roastery2026", stored) is True
    assert sec.verify_password("roastery2027", stored) is False


def test_hash_is_salted_so_same_password_differs():
    """ソルトが無いと、同じパスワードの利用者が一目で分かってしまう。"""
    a = sec.hash_password("roastery2026")
    b = sec.hash_password("roastery2026")
    assert a != b
    assert sec.verify_password("roastery2026", a)
    assert sec.verify_password("roastery2026", b)


def test_stored_hash_contains_parameters():
    """パラメータを埋め込んでおかないと、後でコストを上げられない。

    埋め込んでいないと、パラメータ変更＝全員パスワード再設定になる。
    """
    stored = sec.hash_password("roastery2026")
    algo, n, r, p, salt, digest = stored.split("$")
    assert algo == "scrypt"
    assert int(n) >= 2**14
    assert len(bytes.fromhex(salt)) == 16


def test_password_is_not_recoverable_from_stored_value():
    stored = sec.hash_password("roastery2026")
    assert "roastery2026" not in stored


@pytest.mark.parametrize("broken", ["", "notscrypt$1$1$1$aa$bb", "scrypt$x$y$z$aa$bb", "plain"])
def test_verify_rejects_malformed_stored_values(broken):
    """壊れた値で例外を投げると、そこが可用性の穴になる。False を返すこと。"""
    assert sec.verify_password("roastery2026", broken) is False


# ---------------------------------------------------------------------------
# トークンとメール
# ---------------------------------------------------------------------------
def test_tokens_are_unique_and_long():
    tokens = {sec.new_token() for _ in range(200)}
    assert len(tokens) == 200
    assert all(len(t) >= 32 for t in tokens)


@pytest.mark.parametrize("email", ["a@example.com", "A.B+c@example.co.jp"])
def test_valid_emails(email):
    assert sec.is_valid_email(email) is True


@pytest.mark.parametrize("email", ["a@example", "no-at-sign", "@example.com", "a b@example.com"])
def test_invalid_emails(email):
    assert sec.is_valid_email(email) is False


def test_normalize_email_is_case_insensitive():
    """大文字小文字だけ違う登録を別人にしないこと。"""
    assert sec.normalize_email("  A@Example.COM ") == "a@example.com"
