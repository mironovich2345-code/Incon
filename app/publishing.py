"""Подготовка публикаций и согласование. Автопубликация не реализована.

Статусы и допустимые переходы:
  draft -> pending_approval
  pending_approval -> approved | rejected | changes_requested
  changes_requested -> pending_approval
  rejected -> draft
  approved -> draft (отозвать согласование для правок)

«approved» означает только «можно публиковать вручную». Никаких вызовов
Instagram Content Publishing API в проекте нет.
"""
from __future__ import annotations

import sqlite3

TRANSITIONS = {
    "draft": {"pending_approval"},
    "pending_approval": {"approved", "rejected", "changes_requested"},
    "changes_requested": {"pending_approval"},
    "rejected": {"draft"},
    "approved": {"draft"},
}

STATUS_LABELS = {
    "draft": "Черновик",
    "pending_approval": "На утверждении",
    "approved": "Утверждено (публикуется вручную)",
    "rejected": "Отклонено",
    "changes_requested": "Нужны правки",
}

CAPTION_LIMIT = 2200  # лимит подписи Instagram
HASHTAG_LIMIT = 30


class TransitionError(ValueError):
    pass


def validate(draft: dict) -> list[str]:
    problems = []
    caption = draft.get("caption") or ""
    if not caption.strip():
        problems.append("Пустая подпись")
    if len(caption) > CAPTION_LIMIT:
        problems.append(f"Подпись длиннее {CAPTION_LIMIT} символов ({len(caption)})")
    if caption.count("#") > HASHTAG_LIMIT:
        problems.append(f"Больше {HASHTAG_LIMIT} хэштегов")
    if not draft.get("video_path"):
        problems.append("Не прикреплён смонтированный ролик")
    return problems


def transition(conn: sqlite3.Connection, draft_id: int, to_status: str, comment: str | None = None) -> None:
    r = conn.execute("SELECT * FROM publication_drafts WHERE id=?", (draft_id,)).fetchone()
    if not r:
        raise TransitionError("Черновик не найден")
    cur = r["status"]
    if to_status not in TRANSITIONS.get(cur, set()):
        raise TransitionError(f"Переход {cur} → {to_status} недопустим")
    if to_status == "pending_approval":
        problems = validate(dict(r))
        if problems:
            raise TransitionError("Нельзя отправить на утверждение: " + "; ".join(problems))
    decided = to_status in {"approved", "rejected", "changes_requested"}
    decided_sql = ", decided_at=datetime('now')" if decided else ""
    conn.execute(
        "UPDATE publication_drafts SET status=?, reviewer_comment=COALESCE(?, reviewer_comment),"
        " updated_at=datetime('now')" + decided_sql + " WHERE id=?",
        (to_status, comment, draft_id),
    )
    conn.execute("INSERT INTO draft_events(draft_id, from_status, to_status, comment) VALUES (?,?,?,?)",
                 (draft_id, cur, to_status, comment))
    conn.commit()
