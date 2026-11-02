"""SQLite-хранилище.

Принцип: отсутствующая метрика хранится как NULL и отображается как «нет данных».
Мы никогда не подставляем 0 или оценку вместо неизвестного значения.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    is_own INTEGER NOT NULL DEFAULT 0,
    tracked INTEGER NOT NULL DEFAULT 1,
    ig_user_id TEXT,
    account_type TEXT,              -- business / creator / personal / unknown
    discovery_status TEXT,          -- ok / not_professional / error / not_checked
    discovery_detail TEXT,
    name TEXT,
    biography TEXT,
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Снимки профиля: подписчики, число публикаций. Источник прироста подписчиков.
CREATE TABLE IF NOT EXISTS account_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    captured_on TEXT NOT NULL,      -- YYYY-MM-DD
    followers_count INTEGER,
    follows_count INTEGER,
    media_count INTEGER,
    source TEXT NOT NULL,           -- manual / api / business_discovery
    UNIQUE(account_id, captured_on, source)
);

-- Дневные метрики собственного аккаунта (insights уровня аккаунта).
CREATE TABLE IF NOT EXISTS account_daily (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    day TEXT NOT NULL,
    reach INTEGER,
    views INTEGER,
    profile_views INTEGER,
    accounts_engaged INTEGER,
    total_interactions INTEGER,
    follows INTEGER,
    unfollows INTEGER,
    source TEXT NOT NULL,
    UNIQUE(account_id, day, source)
);

CREATE TABLE IF NOT EXISTS media (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    ig_media_id TEXT,
    permalink TEXT,
    media_type TEXT,                -- VIDEO / IMAGE / CAROUSEL_ALBUM
    media_product_type TEXT,        -- REELS / FEED / STORY
    caption TEXT,
    published_at TEXT,
    duration_sec REAL,
    -- Классификация (вручную, правилами или Claude)
    format TEXT,
    topic TEXT,
    hook_type TEXT,
    hook_text TEXT,
    classification_source TEXT,     -- manual / rules / claude
    -- Метрики. NULL = недоступно / не получено.
    views INTEGER,
    reach INTEGER,
    likes INTEGER,
    comments INTEGER,
    shares INTEGER,
    saved INTEGER,
    total_interactions INTEGER,
    avg_watch_time_ms REAL,
    total_watch_time_ms REAL,
    follows INTEGER,
    profile_visits INTEGER,
    skip_rate REAL,
    leads INTEGER,                  -- B2B-заявки, только ручной ввод
    metrics_source TEXT,            -- manual / api / business_discovery
    metrics_updated_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(account_id, ig_media_id),
    UNIQUE(account_id, permalink)
);

-- История публичных метрик (для конкурентов и для динамики своих роликов).
CREATE TABLE IF NOT EXISTS media_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    media_id INTEGER NOT NULL REFERENCES media(id) ON DELETE CASCADE,
    captured_at TEXT NOT NULL,
    views INTEGER,
    likes INTEGER,
    comments INTEGER,
    source TEXT NOT NULL
);

-- Результат последней проверки доступности метрики через API.
CREATE TABLE IF NOT EXISTS metric_availability (
    metric TEXT NOT NULL,
    scope TEXT NOT NULL,
    status TEXT NOT NULL,           -- available / unavailable / error
    detail TEXT,
    checked_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (metric, scope)
);

CREATE TABLE IF NOT EXISTS references_lib (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    author TEXT,
    source_url TEXT,
    rights_note TEXT,               -- откуда видео и на каком основании хранится
    video_path TEXT,
    duration_sec REAL,
    width INTEGER,
    height INTEGER,
    transcript TEXT,
    transcript_segments TEXT,       -- JSON
    transcript_source TEXT,         -- whisper / upload / none
    keyframes TEXT,                 -- JSON [{t, path, kind}]
    cuts TEXT,                      -- JSON [t,...]
    structure TEXT,                 -- JSON: автоматический разбор
    analysis_md TEXT,               -- разбор Claude
    tags TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS scripts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    topic TEXT,
    format TEXT,
    hook_type TEXT,
    reference_ids TEXT,             -- JSON
    body_md TEXT,
    storyboard TEXT,                -- JSON
    generator TEXT,                 -- template / claude
    status TEXT NOT NULL DEFAULT 'draft',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS content_plan (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day TEXT NOT NULL,
    slot INTEGER NOT NULL,
    topic TEXT,
    format TEXT,
    hook_type TEXT,
    title TEXT,
    script_id INTEGER REFERENCES scripts(id) ON DELETE SET NULL,
    media_id INTEGER REFERENCES media(id) ON DELETE SET NULL,
    status TEXT NOT NULL DEFAULT 'planned', -- planned / scripted / filmed / edited / published
    notes TEXT,
    UNIQUE(day, slot)
);

CREATE TABLE IF NOT EXISTS publication_drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id INTEGER REFERENCES content_plan(id) ON DELETE SET NULL,
    script_id INTEGER REFERENCES scripts(id) ON DELETE SET NULL,
    video_path TEXT,
    caption TEXT,
    cover_note TEXT,
    scheduled_for TEXT,
    status TEXT NOT NULL DEFAULT 'draft', -- draft / pending_approval / approved / rejected / changes_requested
    reviewer_comment TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    decided_at TEXT
);

CREATE TABLE IF NOT EXISTS draft_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id INTEGER NOT NULL REFERENCES publication_drafts(id) ON DELETE CASCADE,
    from_status TEXT,
    to_status TEXT NOT NULL,
    comment TEXT,
    at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,             -- rules / claude
    title TEXT NOT NULL,
    body_md TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS oauth_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,         -- meta_user / meta_page
    subject TEXT NOT NULL,          -- id пользователя или страницы
    token_encrypted BLOB NOT NULL,
    scopes TEXT,
    expires_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(provider, subject)
);

CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

DEFAULT_COMPETITORS = ["aironzak", "di.sukharev"]


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = path or settings.db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    for username in DEFAULT_COMPETITORS:
        conn.execute(
            "INSERT OR IGNORE INTO accounts (username, is_own, discovery_status) VALUES (?, 0, 'not_checked')",
            (username,),
        )
    conn.commit()


@contextmanager
def tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def rows(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, tuple(params)).fetchall()]


def row(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> dict | None:
    r = conn.execute(sql, tuple(params)).fetchone()
    return dict(r) if r else None


def kv_get(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    r = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def kv_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    conn.commit()


def get_or_create_account(conn: sqlite3.Connection, username: str, is_own: bool = False) -> int:
    username = normalize_username(username)
    r = conn.execute("SELECT id, is_own FROM accounts WHERE username=?", (username,)).fetchone()
    if r:
        if is_own and not r["is_own"]:
            conn.execute("UPDATE accounts SET is_own=1 WHERE id=?", (r["id"],))
            conn.commit()
        return r["id"]
    cur = conn.execute(
        "INSERT INTO accounts (username, is_own, discovery_status) VALUES (?, ?, 'not_checked')",
        (username, 1 if is_own else 0),
    )
    conn.commit()
    return cur.lastrowid


def own_account(conn: sqlite3.Connection) -> dict | None:
    return row(conn, "SELECT * FROM accounts WHERE is_own=1 ORDER BY id LIMIT 1")


def normalize_username(username: str) -> str:
    u = username.strip()
    for prefix in ("https://www.instagram.com/", "https://instagram.com/", "instagram.com/"):
        if u.lower().startswith(prefix):
            u = u[len(prefix):]
    return u.strip("/").lstrip("@").lower()
