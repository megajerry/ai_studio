"""Per-session Spokesman conversation memory (ADR-0026; migration 0018).

Fixes the AMNESIA bug: the converse loop used to rebuild the model prompt as
``[system, current_text]`` every turn, so the Spokesman could never remember what
was said one message ago. This module is the store that lets it: guarded writers
that record each turn and read back a BOUNDED slice of recent history to thread
into the prompt.

INVARIANT 6 (body-free event log): a turn ``content`` is a dialogue BODY and lives
ONLY in the ``spokesman_conversations`` table — it is NEVER placed on an event
payload (the event log keeps only char counts). This mirrors how
``spokesman_prep_cache`` and ``decisions`` keep bodies DB-local.

ADR-0013 (context discipline): :func:`recent_turns` is bounded by BOTH a turn
count AND a character budget so history can never grow without limit; the oldest
turns are dropped first.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg.rows import dict_row

logger = logging.getLogger(__name__)

ROLE_HUMAN = "human"
ROLE_SPOKESMAN = "spokesman"
_ROLES = frozenset({ROLE_HUMAN, ROLE_SPOKESMAN})

#: Default history bound — recent turns threaded into the prompt (ADR-0013).
DEFAULT_TURN_LIMIT = 12
#: Default character budget across the threaded history (ADR-0013). Rough token
#: proxy (~4 chars/token) — a few thousand tokens of context, hard-capped.
DEFAULT_MAX_CHARS = 8000
#: Per-turn body cap on write so one giant paste can't blow the whole budget.
MAX_TURN_CHARS = 4000


@dataclass(frozen=True)
class Turn:
    """One recorded dialogue turn (``human`` or ``spokesman``)."""

    role: str
    content: str

    def as_message(self) -> dict[str, str]:
        """Render as a chat message dict for the model prompt.

        ``human`` → ``user``; ``spokesman`` → ``assistant``.
        """
        model_role = "user" if self.role == ROLE_HUMAN else "assistant"
        return {"role": model_role, "content": self.content}


def record_turn(
    conn: psycopg.Connection,
    session_key: str,
    role: str,
    content: str,
) -> None:
    """Persist one dialogue turn (guarded, parameterized write).

    The body lives only in this row (invariant 6). ``role`` must be ``human`` or
    ``spokesman``; ``content`` is truncated to :data:`MAX_TURN_CHARS`. A blank
    ``session_key`` or ``content`` is a no-op (nothing to remember).
    """
    session_key = (session_key or "").strip()
    role = (role or "").strip().lower()
    content = (content or "").strip()
    if not session_key or not content or role not in _ROLES:
        return
    content = content[:MAX_TURN_CHARS]
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO spokesman_conversations (session_key, role, content, workstream)
            VALUES (%s, %s, %s, %s)
            """,
            (session_key, role, content, "productivity"),
        )
    if not conn.autocommit:
        conn.commit()


def recent_turns(
    conn: psycopg.Connection,
    session_key: str,
    *,
    limit: int = DEFAULT_TURN_LIMIT,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> list[Turn]:
    """Return this session's recent turns, OLDEST-FIRST, bounded (ADR-0013).

    Bounded by BOTH ``limit`` (turn count) and ``max_chars`` (total body chars):
    we read the last ``limit`` rows for the session by ``seq`` (append order),
    then drop the OLDEST turns until the running char total fits ``max_chars``.
    Turns from OTHER sessions are never returned (``WHERE session_key = %s``).
    """
    session_key = (session_key or "").strip()
    if not session_key:
        return []
    limit = max(0, int(limit))
    if limit == 0:
        return []
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT role, content
            FROM spokesman_conversations
            WHERE session_key = %s
            ORDER BY seq DESC
            LIMIT %s
            """,
            (session_key, limit),
        )
        rows = cur.fetchall()
    if not conn.autocommit:
        conn.commit()

    # rows are newest-first; walk newest→oldest keeping within the char budget,
    # then reverse to oldest-first for the prompt.
    kept: list[Turn] = []
    used = 0
    for row in rows:
        content = str(row["content"] or "")
        role = str(row["role"] or "")
        if role not in _ROLES:
            continue
        used += len(content)
        if kept and used > max_chars:
            break  # budget hit — older turns dropped (newest kept)
        kept.append(Turn(role=role, content=content))
    kept.reverse()
    return kept


def history_messages(
    conn: Any,
    session_key: str,
    *,
    limit: int = DEFAULT_TURN_LIMIT,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> list[dict[str, str]]:
    """Degrade-safe convenience: recent turns as prompt messages, oldest-first.

    Returns ``[]`` (no history) if the DB is unavailable or the read fails — the
    converse loop then simply replies without memory rather than crashing (the
    whole point of this surface is that it works when other things are down).
    """
    if conn is None:
        return []
    try:
        return [t.as_message() for t in recent_turns(
            conn, session_key, limit=limit, max_chars=max_chars
        )]
    except Exception:  # noqa: BLE001 - table absent mid-migrate / DB hiccup
        logger.warning("recent_turns failed; continuing with no history")
        return []
