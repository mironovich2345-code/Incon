"""Поиск Instagram-аккаунта после входа через Facebook Login for Business / Unified Login.

Ответы Meta различаются в зависимости от типа токена (USER / SYSTEM_USER), наличия
Facebook-страницы и того, выдан ли доступ к конкретным активам или ко всем.
Поэтому используется несколько независимых способов, ни один не считается обязательным:

  1. granular_scopes.target_ids из debug_token — у ЛЮБОГО instagram-разрешения
     (instagram_basic, instagram_manage_insights, instagram_business_* …).
     По документации Meta target_ids не показываются, если разрешение выдано на все
     объекты, — поэтому их отсутствие не ошибка.
  2. GET /me/accounts → instagram_business_account у страниц (если страница есть).
  3. GET /{page-id}?fields=instagram_business_account — для страниц из target_ids
     разрешений pages_* (если /me/accounts пуст или недоступен).
  4. GET /me/businesses и бизнесы из target_ids business_management →
     GET /{business-id}/instagram_business_accounts и /{business-id}/instagram_accounts.
     Это пробы: если ребро недоступно для токена, фиксируется ошибка, вход продолжается.

Каждый найденный ID проверяется запросом профиля GET /{ig-id}?fields=id,username.

Результат каждой пробы сохраняется в обезличенном протоколе: путь-шаблон (без ID),
HTTP-статус, код ошибки Meta, число найденных объектов. ID, имена, токены и коды OAuth
в протокол не попадают.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from .client import CallResult, GraphClient, scrub

_LONG_NUM = re.compile(r"\b\d{5,}\b")
_HANDLE = re.compile(r"@[A-Za-z0-9._]{2,}")


def anonymize(text: str | None) -> str | None:
    """Убирает из текста ошибки токены, длинные числа (ID) и @имена."""
    if text is None:
        return None
    return _HANDLE.sub("@<user>", _LONG_NUM.sub("<id>", scrub(str(text))))[:300]


@dataclass
class Probe:
    method: str            # человекочитаемое имя способа
    endpoint: str          # шаблон пути без ID, например GET /{page-id}?fields=instagram_business_account
    http_status: int | None
    ok: bool
    found: int = 0
    error_code: int | None = None
    error_subcode: int | None = None
    error_type: str | None = None
    error_kind: str | None = None
    message: str | None = None
    note: str | None = None


@dataclass
class Candidate:
    ig_id: str
    source: str
    page_id: str | None = None
    page_token: str | None = None
    username: str = ""
    verified: bool = False


@dataclass
class DiscoveryResult:
    candidates: list[Candidate] = field(default_factory=list)
    probes: list[Probe] = field(default_factory=list)

    def public_probes(self) -> list[dict]:
        return [asdict(p) for p in self.probes]


def _probe_from(method: str, endpoint: str, r: CallResult, found: int = 0, note: str | None = None) -> Probe:
    e = r.error or {}
    return Probe(method=method, endpoint=endpoint, http_status=r.http_status, ok=r.ok, found=found,
                 error_code=e.get("code"), error_subcode=e.get("subcode"), error_type=e.get("type"),
                 error_kind=e.get("kind"), message=anonymize(e.get("message")), note=note)


def granular_summary(debug_data: dict) -> list[dict]:
    """Обезличенная сводка granular_scopes: имя разрешения и число target_ids (без самих ID)."""
    out = []
    for g in (debug_data or {}).get("granular_scopes") or []:
        ids = g.get("target_ids")
        out.append({"scope": g.get("scope"), "target_ids": len(ids) if ids else 0})
    return out


def _targets(debug_data: dict, predicate) -> list[str]:
    ids: list[str] = []
    for g in (debug_data or {}).get("granular_scopes") or []:
        if predicate(g.get("scope") or ""):
            for t in g.get("target_ids") or []:
                if str(t) not in ids:
                    ids.append(str(t))
    return ids


def discover(client: GraphClient, debug_data: dict, max_objects: int = 10) -> DiscoveryResult:
    res = DiscoveryResult()
    seen: dict[str, Candidate] = {}

    def add(ig_id, source, page_id=None, page_token=None, username=""):
        ig_id = str(ig_id)
        if ig_id in seen:
            c = seen[ig_id]
            if page_token and not c.page_token:  # токен страницы полезнее — сохраняем
                c.page_id, c.page_token = page_id, page_token
            return
        c = Candidate(ig_id, source, page_id, page_token, (username or "").lower())
        seen[ig_id] = c
        res.candidates.append(c)

    # 1. granular_scopes — любые instagram-разрешения
    ig_targets = _targets(debug_data, lambda s: s.startswith("instagram"))
    has_ig_scope = any((g.get("scope") or "").startswith("instagram") for g in (debug_data or {}).get("granular_scopes") or [])
    for t in ig_targets:
        add(t, "debug_token.granular_scopes")
    res.probes.append(Probe("granular_scopes (instagram_*)", "GET /debug_token", 200, True, found=len(ig_targets),
                            note=None if ig_targets else ("instagram-разрешения выданы без target_ids (доступ ко всем объектам)"
                                                          if has_ig_scope else "instagram-разрешений в granular_scopes нет")))

    # 2. /me/accounts
    r = client.call("/me/accounts", fields="id,instagram_business_account{id,username},access_token", limit=50)
    pages = (r.data or {}).get("data", []) if r.ok else []
    linked = [p for p in pages if p.get("instagram_business_account")]
    for p in linked:
        iga = p["instagram_business_account"]
        add(iga["id"], "me/accounts", p.get("id"), p.get("access_token"), iga.get("username", ""))
    res.probes.append(_probe_from("Страницы пользователя", "GET /me/accounts", r, found=len(linked),
                                  note=f"страниц: {len(pages)}" if r.ok else None))

    # 3. страницы из target_ids разрешений pages_*
    page_ids = [pid for pid in _targets(debug_data, lambda s: s.startswith("pages_"))
                if pid not in {p.get("id") for p in pages}][:max_objects]
    for pid in page_ids:
        r = client.call(f"/{pid}", fields="instagram_business_account{id,username}")
        iga = (r.data or {}).get("instagram_business_account") if r.ok else None
        if iga:
            add(iga["id"], "page.instagram_business_account", pid, None, iga.get("username", ""))
        res.probes.append(_probe_from("Страница из granular_scopes", "GET /{page-id}?fields=instagram_business_account",
                                      r, found=1 if iga else 0))

    # 4. бизнес-портфолио
    business_ids = _targets(debug_data, lambda s: s == "business_management")
    r = client.call("/me/businesses", fields="id", limit=25)
    if r.ok:
        for b in (r.data or {}).get("data", []):
            if b.get("id") and str(b["id"]) not in business_ids:
                business_ids.append(str(b["id"]))
    res.probes.append(_probe_from("Бизнес-портфолио пользователя", "GET /me/businesses", r,
                                  found=len((r.data or {}).get("data", [])) if r.ok else 0))
    for bid in business_ids[:max_objects]:
        for edge in ("instagram_business_accounts", "instagram_accounts"):
            r = client.call(f"/{bid}/{edge}", fields="id,username", limit=50)
            items = (r.data or {}).get("data", []) if r.ok else []
            for it in items:
                if it.get("id"):
                    add(it["id"], f"business.{edge}", username=it.get("username", ""))
            res.probes.append(_probe_from("Instagram-аккаунты бизнес-портфолио", f"GET /{{business-id}}/{edge}", r,
                                          found=len(items)))

    # Проверка каждого кандидата чтением профиля
    for c in res.candidates[:max_objects]:
        r = client.call(f"/{c.ig_id}", fields="id,username")
        if r.ok:
            c.verified = True
            c.username = (r.data.get("username") or c.username or "").lower()
        res.probes.append(_probe_from("Проверка профиля найденного аккаунта", "GET /{ig-user-id}?fields=id,username", r,
                                      found=1 if r.ok else 0, note=f"источник: {c.source}"))
    return res
