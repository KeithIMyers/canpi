import sqlite3
import hashlib
import secrets
import os
from datetime import datetime

DB_PATH = os.environ.get("DB_PATH", "/data/canpi.db")


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = get_db()
    c = conn.cursor()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'user',
            can_access TEXT NOT NULL DEFAULT 'can0,can1,can9',
            created_at TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            selected_interface TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS api_keys (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key_hash TEXT UNIQUE NOT NULL,
            key_prefix TEXT NOT NULL,
            name TEXT NOT NULL,
            can_access TEXT NOT NULL DEFAULT 'can0,can1,can9',
            created_by INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1
        );
    """)
    existing = c.execute("SELECT id FROM users WHERE username='admin'").fetchone()
    if not existing:
        pw = hash_password("admin")
        c.execute(
            "INSERT INTO users (username, password_hash, role, can_access, created_at) VALUES (?,?,?,?,?)",
            ("admin", pw, "admin", "can0,can1,can9", datetime.utcnow().isoformat())
        )
    conn.commit()
    conn.close()


# --------------- password helpers ---------------

def hash_password(pw: str) -> str:
    salt = secrets.token_hex(16)
    h = hashlib.sha256(f"{salt}{pw}".encode()).hexdigest()
    return f"{salt}:{h}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        salt, h = stored.split(":", 1)
        return hashlib.sha256(f"{salt}{pw}".encode()).hexdigest() == h
    except Exception:
        return False


# --------------- session helpers ---------------

def create_session(user_id: int, interface: str) -> str:
    token = secrets.token_urlsafe(32)
    conn = get_db()
    conn.execute(
        "INSERT INTO sessions (token, user_id, selected_interface, created_at, expires_at) VALUES (?,?,?,?,?)",
        (token, user_id, interface,
         datetime.utcnow().isoformat(),
         datetime.utcnow().replace(year=datetime.utcnow().year + 1).isoformat())
    )
    conn.commit()
    conn.close()
    return token


def get_session(token: str):
    if not token:
        return None
    conn = get_db()
    row = conn.execute(
        """SELECT s.token, s.user_id, s.selected_interface,
                  u.username, u.role, u.can_access, u.active
           FROM sessions s JOIN users u ON s.user_id = u.id
           WHERE s.token = ? AND u.active = 1""",
        (token,)
    ).fetchone()
    conn.close()
    return row


def delete_session(token: str):
    conn = get_db()
    conn.execute("DELETE FROM sessions WHERE token=?", (token,))
    conn.commit()
    conn.close()


# --------------- user CRUD ---------------

def create_user(username: str, password: str, role: str, can_access: str):
    conn = get_db()
    pw_hash = hash_password(password)
    conn.execute(
        "INSERT INTO users (username, password_hash, role, can_access, created_at) VALUES (?,?,?,?,?)",
        (username, pw_hash, role, can_access, datetime.utcnow().isoformat())
    )
    conn.commit()
    conn.close()


def get_user_by_username(username: str):
    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    conn.close()
    return row


def get_user_by_id(user_id: int):
    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    conn.close()
    return row


def get_all_users():
    conn = get_db()
    rows = conn.execute("SELECT id, username, role, can_access, active, created_at FROM users").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_user(user_id: int, **kwargs):
    conn = get_db()
    allowed = {"username", "role", "can_access", "active"}
    sets = []
    vals = []
    for k, v in kwargs.items():
        if k in allowed:
            sets.append(f"{k}=?")
            vals.append(v)
    if not sets:
        conn.close()
        return
    vals.append(user_id)
    conn.execute(f"UPDATE users SET {','.join(sets)} WHERE id=?", vals)
    conn.commit()
    conn.close()


def delete_user(user_id: int):
    conn = get_db()
    conn.execute("DELETE FROM users WHERE id=?", (user_id,))
    conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()


def change_password(user_id: int, new_password: str):
    conn = get_db()
    pw_hash = hash_password(new_password)
    conn.execute("UPDATE users SET password_hash=? WHERE id=?", (pw_hash, user_id))
    conn.commit()
    conn.close()


# --------------- API key helpers ---------------

def create_api_key(name: str, can_access: str, created_by: int) -> str:
    raw_key = secrets.token_urlsafe(32)
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    prefix = raw_key[:8]
    conn = get_db()
    conn.execute(
        "INSERT INTO api_keys (key_hash, key_prefix, name, can_access, created_by, created_at) VALUES (?,?,?,?,?,?)",
        (key_hash, prefix, name, can_access, created_by, datetime.utcnow().isoformat())
    )
    conn.commit()
    conn.close()
    return raw_key


def get_api_key(raw_key: str):
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM api_keys WHERE key_hash=? AND active=1", (key_hash,)
    ).fetchone()
    conn.close()
    return row


def get_all_api_keys():
    conn = get_db()
    rows = conn.execute(
        "SELECT id, key_prefix, name, can_access, created_by, created_at, active FROM api_keys WHERE active=1"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def revoke_api_key(key_id: int):
    conn = get_db()
    conn.execute("UPDATE api_keys SET active=0 WHERE id=?", (key_id,))
    conn.commit()
    conn.close()
