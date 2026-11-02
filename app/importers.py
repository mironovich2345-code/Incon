"""Ручной импорт данных — резервный способ (основной источник — Instagram API).

Используется, пока API не подключён, для метрик, которых нет в API (заявки, удержание из
приложения), и для конкурентов, недоступных через Business Discovery.

Поддерживаются CSV с заголовками на русском или английском. Пустая ячейка =
метрика неизвестна (NULL). Ноль записывается только если в файле явно стоит 0.
"""
from __future__ import annotations

import csv
import io
import re
import sqlite3
from datetime import date, datetime

from . import classify
from .db import get_or_create_account, normalize_username

# Синонимы колонок: наш ключ -> возможные заголовки (в нижнем регистре).
OWN_REEL_COLUMNS = {
    "ig_media_id": ["ig_media_id", "media_id", "post id", "id публикации", "id"],
    "permalink": ["permalink", "ссылка", "link", "url"],
    "published_at": ["published_at", "publish time", "дата публикации", "дата", "date", "время публикации"],
    "caption": ["caption", "description", "описание", "подпись", "текст"],
    "duration_sec": ["duration_sec", "duration (sec)", "длительность", "длительность (сек)"],
    "format": ["format", "формат"],
    "topic": ["topic", "тема"],
    "hook_type": ["hook_type", "тип хука", "хук тип"],
    "hook_text": ["hook_text", "хук", "hook"],
    "views": ["views", "просмотры", "plays"],
    "reach": ["reach", "охват"],
    "likes": ["likes", "лайки", "отметки нравится"],
    "comments": ["comments", "комментарии"],
    "shares": ["shares", "репосты", "поделились"],
    "saved": ["saved", "saves", "сохранения"],
    "total_interactions": ["total_interactions", "interactions", "взаимодействия"],
    "avg_watch_time_sec": ["avg_watch_time_sec", "average watch time", "среднее время просмотра", "avg watch time (sec)"],
    "total_watch_time_sec": ["total_watch_time_sec", "watch time", "время просмотра"],
    "follows": ["follows", "подписки", "новые подписчики"],
    "profile_visits": ["profile_visits", "profile visits", "посещения профиля", "переходы в профиль"],
    "skip_rate": ["skip_rate", "skip rate", "доля пропусков"],
    "leads": ["leads", "заявки", "b2b заявки", "лиды"],
}

COMPETITOR_COLUMNS = {
    "username": ["username", "аккаунт", "account"],
    "permalink": ["permalink", "ссылка", "link", "url"],
    "published_at": ["published_at", "дата публикации", "дата", "date"],
    "caption": ["caption", "подпись", "описание", "текст"],
    "media_type": ["media_type", "тип"],
    "views": ["views", "просмотры", "view_count"],
    "likes": ["likes", "лайки", "like_count"],
    "comments": ["comments", "комментарии", "comments_count"],
    "captured_at": ["captured_at", "дата сбора", "снято"],
    "format": ["format", "формат"],
    "topic": ["topic", "тема"],
    "hook_type": ["hook_type", "тип хука"],
}

ACCOUNT_DAILY_COLUMNS = {
    "day": ["day", "date", "дата", "день"],
    "followers_count": ["followers_count", "followers", "подписчики"],
    "reach": ["reach", "охват"],
    "views": ["views", "просмотры"],
    "profile_views": ["profile_views", "просмотры профиля"],
    "accounts_engaged": ["accounts_engaged", "вовлечённые аккаунты", "вовлеченные аккаунты"],
    "total_interactions": ["total_interactions", "взаимодействия"],
    "follows": ["follows", "подписки", "новые подписчики"],
    "unfollows": ["unfollows", "отписки"],
}


class ImportErrorWithRow(ValueError):
    pass


def _map_headers(headers: list[str], spec: dict[str, list[str]]) -> dict[str, str]:
    norm = {h.strip().lower().lstrip("﻿"): h for h in headers}
    mapping: dict[str, str] = {}
    for key, aliases in spec.items():
        for a in aliases:
            if a in norm:
                mapping[key] = norm[a]
                break
    return mapping


def _num(value: str | None) -> float | None:
    if value is None:
        return None
    v = value.strip().replace(" ", "").replace(" ", "")
    if v in {"", "-", "—", "n/a", "na", "нет", "null", "none"}:
        return None
    v = v.replace(",", ".")
    mult = 1
    if v.lower().endswith(("k", "к", "тыс")):
        mult = 1000
        v = re.sub(r"(k|к|тыс)$", "", v, flags=re.I)
    elif v.lower().endswith(("m", "м", "млн")):
        mult = 1_000_000
        v = re.sub(r"(m|м|млн)$", "", v, flags=re.I)
    v = v.rstrip("%")
    try:
        return float(v) * mult
    except ValueError as exc:
        raise ValueError(f"не число: {value!r}") from exc


def _int(value: str | None) -> int | None:
    n = _num(value)
    return None if n is None else int(round(n))


def parse_datetime(value: str | None) -> str | None:
    if not value or not value.strip():
        return None
    v = value.strip()
    fmts = ["%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
            "%d.%m.%Y %H:%M", "%d.%m.%Y", "%m/%d/%Y %H:%M", "%m/%d/%Y"]
    for f in fmts:
        try:
            dt = datetime.strptime(v.replace("+0000", "+00:00").replace("Z", "+00:00") if "%z" in f else v, f)
            return dt.replace(tzinfo=None).isoformat(timespec="minutes")
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00")).replace(tzinfo=None).isoformat(timespec="minutes")
    except ValueError as exc:
        raise ValueError(f"не дата: {value!r}") from exc


def _reader(content: str | bytes) -> csv.DictReader:
    if isinstance(content, bytes):
        for enc in ("utf-8-sig", "cp1251"):
            try:
                content = content.decode(enc)
                break
            except UnicodeDecodeError:
                continue
    sample = content[:2048]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    return csv.DictReader(io.StringIO(content), dialect=dialect)


def _apply_classification(rec: dict, caption: str | None) -> None:
    if rec.get("format") and rec.get("topic") and rec.get("hook_type"):
        rec["classification_source"] = "manual"
        return
    c = classify.classify_text(caption)
    any_manual = any(rec.get(k) for k in ("format", "topic", "hook_type"))
    rec.setdefault("format", None)
    rec["format"] = rec.get("format") or c.format
    rec["topic"] = rec.get("topic") or c.topic
    rec["hook_type"] = rec.get("hook_type") or c.hook_type
    rec["hook_text"] = rec.get("hook_text") or c.hook_text
    rec["classification_source"] = "manual+rules" if any_manual else "rules"


API_SOURCES = {"api", "business_discovery"}


def upsert_media(conn: sqlite3.Connection, account_id: int, rec: dict, source: str) -> int:
    """Вставить/обновить публикацию.

    * Поиск существующей записи — по ig_media_id ИЛИ permalink (ролик мог прийти сначала из CSV,
      потом из API).
    * None не затирает уже известные значения.
    * Основной источник — API. Ручной импорт поверх данных API только заполняет пустые поля
      и не меняет metrics_source, поэтому CSV остаётся резервным способом.
    """
    if not rec.get("ig_media_id") and not rec.get("permalink"):
        raise ValueError("нужен ig_media_id или permalink")
    existing = None
    if rec.get("ig_media_id"):
        existing = conn.execute("SELECT id, metrics_source FROM media WHERE account_id=? AND ig_media_id=?",
                                (account_id, rec["ig_media_id"])).fetchone()
    if not existing and rec.get("permalink"):
        existing = conn.execute("SELECT id, metrics_source FROM media WHERE account_id=? AND permalink=?",
                                (account_id, rec["permalink"])).fetchone()
    cols = [k for k, v in rec.items() if v is not None]
    now = datetime.now().isoformat(timespec="seconds")
    if existing:
        if not cols:
            return existing["id"]
        if source == "manual" and existing["metrics_source"] in API_SOURCES:
            sets = ", ".join(f"{c}=COALESCE({c}, ?)" for c in cols)
            conn.execute(f"UPDATE media SET {sets} WHERE id=?", [rec[c] for c in cols] + [existing["id"]])
        else:
            sets = ", ".join(f"{c}=?" for c in cols)
            conn.execute(f"UPDATE media SET {sets}, metrics_source=?, metrics_updated_at=? WHERE id=?",
                         [rec[c] for c in cols] + [source, now, existing["id"]])
        return existing["id"]
    cols_all = cols + ["account_id", "metrics_source", "metrics_updated_at"]
    vals = [rec[c] for c in cols] + [account_id, source, now]
    cur = conn.execute(f"INSERT INTO media ({', '.join(cols_all)}) VALUES ({', '.join('?' * len(cols_all))})", vals)
    return cur.lastrowid


def import_own_reels(conn: sqlite3.Connection, content: str | bytes, own_username: str) -> dict:
    reader = _reader(content)
    mapping = _map_headers(reader.fieldnames or [], OWN_REEL_COLUMNS)
    if "permalink" not in mapping and "ig_media_id" not in mapping:
        raise ImportErrorWithRow("В файле нет колонки permalink или ig_media_id")
    account_id = get_or_create_account(conn, own_username, is_own=True)
    imported, errors = 0, []
    for i, raw in enumerate(reader, start=2):
        try:
            g = lambda k: raw.get(mapping[k]) if k in mapping else None  # noqa: E731
            rec = {
                "ig_media_id": (g("ig_media_id") or "").strip() or None,
                "permalink": (g("permalink") or "").strip() or None,
                "published_at": parse_datetime(g("published_at")),
                "caption": g("caption"),
                "duration_sec": _num(g("duration_sec")),
                "format": (g("format") or "").strip() or None,
                "topic": (g("topic") or "").strip() or None,
                "hook_type": (g("hook_type") or "").strip() or None,
                "hook_text": (g("hook_text") or "").strip() or None,
                "media_type": "VIDEO",
                "media_product_type": "REELS",
                "views": _int(g("views")),
                "reach": _int(g("reach")),
                "likes": _int(g("likes")),
                "comments": _int(g("comments")),
                "shares": _int(g("shares")),
                "saved": _int(g("saved")),
                "total_interactions": _int(g("total_interactions")),
                "follows": _int(g("follows")),
                "profile_visits": _int(g("profile_visits")),
                "skip_rate": _num(g("skip_rate")),
                "leads": _int(g("leads")),
            }
            avg = _num(g("avg_watch_time_sec"))
            tot = _num(g("total_watch_time_sec"))
            rec["avg_watch_time_ms"] = avg * 1000 if avg is not None else None
            rec["total_watch_time_ms"] = tot * 1000 if tot is not None else None
            _apply_classification(rec, rec["caption"])
            media_id = upsert_media(conn, account_id, rec, "manual")
            conn.execute("INSERT INTO media_snapshots(media_id, captured_at, views, likes, comments, source) VALUES (?,?,?,?,?, 'manual')",
                         (media_id, datetime.now().isoformat(timespec="seconds"), rec["views"], rec["likes"], rec["comments"]))
            imported += 1
        except ValueError as exc:
            errors.append(f"строка {i}: {exc}")
    conn.commit()
    return {"imported": imported, "errors": errors, "columns": mapping}


def import_competitor_posts(conn: sqlite3.Connection, content: str | bytes, default_username: str | None = None) -> dict:
    reader = _reader(content)
    mapping = _map_headers(reader.fieldnames or [], COMPETITOR_COLUMNS)
    if "permalink" not in mapping:
        raise ImportErrorWithRow("В файле нет колонки permalink")
    imported, errors = 0, []
    for i, raw in enumerate(reader, start=2):
        try:
            g = lambda k: raw.get(mapping[k]) if k in mapping else None  # noqa: E731
            username = normalize_username(g("username") or default_username or "")
            if not username:
                raise ValueError("не указан username")
            account_id = get_or_create_account(conn, username)
            rec = {
                "permalink": (g("permalink") or "").strip() or None,
                "published_at": parse_datetime(g("published_at")),
                "caption": g("caption"),
                "media_type": (g("media_type") or "").strip().upper() or None,
                "views": _int(g("views")),
                "likes": _int(g("likes")),
                "comments": _int(g("comments")),
                "format": (g("format") or "").strip() or None,
                "topic": (g("topic") or "").strip() or None,
                "hook_type": (g("hook_type") or "").strip() or None,
            }
            if rec["media_type"] in {"REELS", "REEL"}:
                rec["media_type"], rec["media_product_type"] = "VIDEO", "REELS"
            _apply_classification(rec, rec["caption"])
            media_id = upsert_media(conn, account_id, rec, "manual")
            captured = parse_datetime(g("captured_at")) or datetime.now().isoformat(timespec="seconds")
            conn.execute("INSERT INTO media_snapshots(media_id, captured_at, views, likes, comments, source) VALUES (?,?,?,?,?, 'manual')",
                         (media_id, captured, rec["views"], rec["likes"], rec["comments"]))
            imported += 1
        except ValueError as exc:
            errors.append(f"строка {i}: {exc}")
    conn.commit()
    return {"imported": imported, "errors": errors, "columns": mapping}


def import_account_daily(conn: sqlite3.Connection, content: str | bytes, own_username: str) -> dict:
    reader = _reader(content)
    mapping = _map_headers(reader.fieldnames or [], ACCOUNT_DAILY_COLUMNS)
    if "day" not in mapping:
        raise ImportErrorWithRow("В файле нет колонки day/дата")
    account_id = get_or_create_account(conn, own_username, is_own=True)
    imported, errors = 0, []
    for i, raw in enumerate(reader, start=2):
        try:
            g = lambda k: raw.get(mapping[k]) if k in mapping else None  # noqa: E731
            day = (parse_datetime(g("day")) or "")[:10]
            if not day:
                raise ValueError("пустая дата")
            vals = {k: _int(g(k)) for k in ("reach", "views", "profile_views", "accounts_engaged", "total_interactions", "follows", "unfollows")}
            conn.execute(
                """INSERT INTO account_daily(account_id, day, reach, views, profile_views, accounts_engaged, total_interactions, follows, unfollows, source)
                   VALUES (?,?,?,?,?,?,?,?,?, 'manual')
                   ON CONFLICT(account_id, day, source) DO UPDATE SET reach=excluded.reach, views=excluded.views,
                     profile_views=excluded.profile_views, accounts_engaged=excluded.accounts_engaged,
                     total_interactions=excluded.total_interactions, follows=excluded.follows, unfollows=excluded.unfollows""",
                (account_id, day, *vals.values()),
            )
            fc = _int(g("followers_count"))
            if fc is not None:
                record_followers(conn, account_id, fc, "manual", day)
            imported += 1
        except ValueError as exc:
            errors.append(f"строка {i}: {exc}")
    conn.commit()
    return {"imported": imported, "errors": errors, "columns": mapping}


def record_followers(conn: sqlite3.Connection, account_id: int, followers: int | None, source: str,
                     day: str | None = None, follows_count: int | None = None, media_count: int | None = None) -> None:
    day = day or date.today().isoformat()
    conn.execute(
        """INSERT INTO account_snapshots(account_id, captured_on, followers_count, follows_count, media_count, source)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(account_id, captured_on, source) DO UPDATE SET followers_count=excluded.followers_count,
             follows_count=COALESCE(excluded.follows_count, follows_count), media_count=COALESCE(excluded.media_count, media_count)""",
        (account_id, day, followers, follows_count, media_count, source),
    )
    conn.commit()
