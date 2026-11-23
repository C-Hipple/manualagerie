"""Household accounts: who asked what and who did what. Everything else stays shared."""
import base64
import hashlib
import hmac
import re
import secrets
import sqlite3

# scrypt cost: 16 MiB of memory and roughly 50 ms per hash on a small server.
SCRYPT_N, SCRYPT_R, SCRYPT_P, DKLEN = 2 ** 14, 8, 1, 32
USERNAME_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,31}")
DISPLAY_MAX = 60
PASSWORD_MIN = 8
PASSWORD_MAX = 1024


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def _scrypt(password: str, salt: bytes, n: int, r: int, p: int, dklen: int) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, dklen=dklen, maxmem=64 * 1024 * 1024)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = _scrypt(password, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P, DKLEN)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, digest = stored.split("$")
        if algo != "scrypt":
            return False
        expected = base64.b64decode(digest)
        got = _scrypt(password, base64.b64decode(salt), int(n), int(r), int(p), len(expected))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got, expected)


# Verified against when the username is unknown, so a miss costs the same as a wrong password.
_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


def normalize_username(username) -> str:
    return str(username or "").strip().lower()


def validate(username: str, display_name: str, password: str | None) -> str | None:
    """An error message for bad input, else None. `password=None` skips the password check."""
    if not USERNAME_RE.fullmatch(username):
        return "Usernames are 1–32 characters: lowercase letters, digits, dot, dash or underscore."
    if not display_name or len(display_name) > DISPLAY_MAX:
        return f"Give a display name of at most {DISPLAY_MAX} characters."
    if password is not None:
        return password_error(password)
    return None


def password_error(password: str) -> str | None:
    if len(password) < PASSWORD_MIN:
        return f"Passwords need at least {PASSWORD_MIN} characters."
    if len(password) > PASSWORD_MAX:
        return "That password is too long."
    return None


def count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT count(*) FROM users").fetchone()[0]


def get(conn: sqlite3.Connection, user_id: int):
    return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def get_by_username(conn: sqlite3.Connection, username: str):
    return conn.execute("SELECT * FROM users WHERE username = ?", (normalize_username(username),)).fetchone()


def list_all(conn: sqlite3.Connection):
    return conn.execute("SELECT * FROM users ORDER BY disabled_at IS NOT NULL, username").fetchall()


def create(conn: sqlite3.Connection, username: str, display_name: str, password: str, *,
           is_admin: bool = False) -> int:
    return conn.execute(
        "INSERT INTO users (username, display_name, password_hash, is_admin) VALUES (?, ?, ?, ?)",
        (normalize_username(username), display_name.strip(), hash_password(password), int(is_admin)),
    ).lastrowid


def authenticate(conn: sqlite3.Connection, username: str, password: str):
    """The enabled user with these credentials, or None. Unknown users cost one hash too."""
    u = get_by_username(conn, username)
    ok = verify_password(password, u["password_hash"] if u else _DUMMY_HASH)
    if not ok or u is None or u["disabled_at"] is not None:
        return None
    return u


def set_password(conn: sqlite3.Connection, user_id: int, password: str) -> None:
    """New hash and a new session generation, so every existing session for this user ends."""
    conn.execute("UPDATE users SET password_hash = ?, session_gen = session_gen + 1 WHERE id = ?",
                 (hash_password(password), user_id))


def active_admin_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT count(*) FROM users WHERE is_admin = 1 AND disabled_at IS NULL").fetchone()[0]


def is_last_admin(conn: sqlite3.Connection, user_id: int) -> bool:
    u = get(conn, user_id)
    return bool(u and u["is_admin"] and u["disabled_at"] is None and active_admin_count(conn) <= 1)


def set_disabled(conn: sqlite3.Connection, user_id: int, disabled: bool) -> None:
    if disabled:
        conn.execute("UPDATE users SET disabled_at = datetime('now'), session_gen = session_gen + 1 "
                     "WHERE id = ? AND disabled_at IS NULL", (user_id,))
    else:
        conn.execute("UPDATE users SET disabled_at = NULL WHERE id = ?", (user_id,))


def set_admin(conn: sqlite3.Connection, user_id: int, is_admin: bool) -> None:
    conn.execute("UPDATE users SET is_admin = ? WHERE id = ?", (int(is_admin), user_id))
