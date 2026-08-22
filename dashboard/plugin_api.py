"""session-importer plugin backend.

Lists Claude Code and OpenCode sessions on disk and imports them into
Hermes as resumable plain-text sessions via hermes_cli.foreign_sessions
(Claude) plus a native OpenCode converter (OpenCode stores its history in
a SQLite DB, not JSONL, so the shipped claude/codex parsers don't apply).

Dedup: every Hermes session imported from a foreign tool carries
origin_json.imported_from.{tool, path, foreign_session_id}. /list indexes
those keys at list-time so the pane can badge already-imported sessions.

v1.1: /list uses a cheap SQL pass (first text part per message — no full
parse, no per-session connections); full parse happens only at import or
/view. Per-session parse failures surface in errors[] instead of being
silently skipped. imported_index() is cached with a short TTL and reads
origin_json in one SQL pass.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter

# Make the hermes-agent codebase importable when the gateway runs us from
# another working directory.
_HERMES_AGENT = Path.home() / ".hermes" / "hermes-agent"
if str(_HERMES_AGENT) not in sys.path:
    sys.path.insert(0, str(_HERMES_AGENT))

router = APIRouter()

OPENCODE_DB = Path.home() / ".local" / "share" / "opencode" / "opencode.db"

_INDEX_TTL_SECONDS = 30.0
_index_cache: Optional[Dict[str, Dict[str, str]]] = None
_index_cached_at = 0.0

# ── OpenCode → normalized turns (same contract as foreign_sessions) ──────


def _oc_flatten_parts(parts: List[Dict[str, Any]]) -> Tuple[str, List[str]]:
    """Flatten one OpenCode message's parts to text + tool summaries."""
    texts: List[str] = []
    tools: List[str] = []
    for part in parts:
        ptype = part.get("type")
        if ptype == "text":
            t = part.get("text")
            if isinstance(t, str) and t.strip():
                texts.append(t.strip())
        elif ptype == "tool":
            name = part.get("tool") or "tool"
            tools.append(f"[ran tool: {name}]")
        # reasoning / step-start / step-finish / others: skipped
    return "\n\n".join(texts), tools


def _merge_turns(raw_turns: List[Tuple[str, str]]) -> List[Dict[str, str]]:
    merged: List[Dict[str, str]] = []
    for role, text in raw_turns:
        text = text.strip()
        if not text:
            continue
        if merged and merged[-1]["role"] == role:
            merged[-1]["content"] += "\n\n" + text
        else:
            merged.append({"role": role, "content": text})
    if merged and merged[0]["role"] == "assistant":
        merged.insert(0, {
            "role": "user",
            "content": "(imported conversation begins with an assistant reply)",
        })
    return merged


def _first_user_line(turns: List[Tuple[str, str]]) -> Optional[str]:
    for role, text in turns:
        if role == "user":
            line = text.strip().splitlines()[0].strip()
            if line:
                return line[:120]
    return None


def parse_opencode_session(db_path: Path, session_id: str) -> Dict[str, Any]:
    """Parse one OpenCode session from its SQLite store into normalized turns."""
    turns: List[Tuple[str, str]] = []
    cwd: Optional[str] = None
    title: Optional[str] = None
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        row = con.execute(
            "SELECT directory, title FROM session WHERE id = ?", (session_id,)
        ).fetchone()
        if row:
            cwd, title = row[0], row[1]
        msgs = con.execute(
            "SELECT id, data FROM message WHERE session_id = ? ORDER BY time_created, id",
            (session_id,),
        ).fetchall()
        for mid, mdata in msgs:
            m = json.loads(mdata)
            role = m.get("role")
            parts = [
                json.loads(pdata)
                for (pdata,) in con.execute(
                    "SELECT data FROM part WHERE message_id = ? ORDER BY time_created, id",
                    (mid,),
                )
                if pdata
            ]
            has_text_part = any(
                isinstance(p, dict) and p.get("type") == "text" for p in parts
            )
            text, tools = _oc_flatten_parts(parts)
            if role not in ("user", "assistant"):
                continue
            if role == "user":
                if not has_text_part or not text:
                    continue  # synthetic payload (diff summary etc.), never imported
                turns.append((role, text))
            else:
                chunks = ([text] if text else []) + tools
                if chunks:
                    turns.append(("assistant", "\n\n".join(chunks)))
    finally:
        con.close()
    return {
        "turns": _merge_turns(turns),
        "cwd": cwd,
        "title_guess": title or _first_user_line(turns),
        "session_id": session_id,
    }


def _opencode_list_rows(limit: int) -> List[Dict[str, Any]]:
    """Cheap list-only pass: one SQL query, first text part per message.

    No full parse here — turn_count is the count of messages that would
    produce a turn (user-with-text + non-empty assistant), which is what
    the list UI needs. Full normalization happens only in parse_opencode_session.
    """
    if not OPENCODE_DB.is_file():
        return []
    con = sqlite3.connect(f"file:{OPENCODE_DB}?mode=ro", uri=True)
    try:
        rows = con.execute(
            """
            SELECT s.id, s.directory, s.title, s.time_updated,
                   COUNT(m.id) AS msg_count
            FROM session s
            LEFT JOIN message m ON m.session_id = s.id
                 AND json_extract(m.data, '$.role') IN ('user','assistant')
                 AND EXISTS (
                     SELECT 1 FROM part p
                     WHERE p.message_id = m.id
                       AND json_extract(p.data, '$.type') = 'text'
                       AND json_extract(p.data, '$.role') IS NULL
                 )
            WHERE s.time_archived IS NULL
            GROUP BY s.id
            ORDER BY s.time_updated DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    finally:
        con.close()
    out: List[Dict[str, Any]] = []
    for sid, directory, title, tupdated, msg_count in rows:
        out.append({
            "source": "opencode",
            "path": f"opencode-db:{sid}",
            "mtime_ms": tupdated,
            "cwd": directory,
            "title": title,
            "turn_count": msg_count,
            "session_id": sid,
        })
    return out


def list_opencode_sessions(
    limit: int = 200, errors: Optional[List[str]] = None
) -> List[Dict[str, Any]]:
    """Discover OpenCode sessions from its SQLite DB, newest first.

    Parse failures (schema drift etc.) are appended to ``errors`` rather
    than silently skipped, when ``errors`` is provided.
    """
    results: List[Dict[str, Any]] = []
    if not OPENCODE_DB.is_file():
        return results
    con = sqlite3.connect(f"file:{OPENCODE_DB}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT id, directory, title, time_updated FROM session "
            "WHERE time_archived IS NULL ORDER BY time_updated DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        con.close()
    for sid, directory, title, tupdated in rows:
        try:
            parsed = parse_opencode_session(OPENCODE_DB, sid)
        except Exception as exc:
            if errors is not None:
                errors.append(f"opencode {sid}: {exc}")
            continue
        if not parsed["turns"]:
            continue
        results.append({
            "source": "opencode",
            "path": f"opencode-db:{sid}",
            "mtime_ms": tupdated,
            "cwd": directory,
            "title": parsed["title_guess"],
            "turn_count": len(parsed["turns"]),
            "session_id": sid,
        })
    return results


def list_claude_sessions_meta(errors: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """Discover Claude Code sessions via the stock importer's lister.

    Note: ``turn_count`` here is the full-parse count; the OpenCode list
    pass counts candidate messages only, so the two providers' counts are
    not directly comparable (and may differ slightly from imported turns).
    """
    from hermes_cli.foreign_sessions import list_claude_sessions

    out: List[Dict[str, Any]] = []
    for s in list_claude_sessions():
        out.append({
            "source": "claude",
            "path": str(s.path),
            "mtime_ms": int(s.mtime * 1000),
            "cwd": s.cwd,
            "title": s.title_guess,
            "turn_count": s.turn_count,
            "session_id": s.session_id,
        })
    return out


# ── Dedup index (TTL-cached, one SQL pass) ───────────────────────────────


def imported_index(force: bool = False) -> Dict[str, Dict[str, str]]:
    """Map 'tool:foreign_session_id' (or 'tool:path:path') → hermes session info.

    Cached for _INDEX_TTL_SECONDS; origin_json is read in a single SQL pass
    instead of per-row get_session() calls.
    """
    global _index_cache, _index_cached_at
    now = time.monotonic()
    if not force and _index_cache is not None and (now - _index_cached_at) < _INDEX_TTL_SECONDS:
        return _index_cache

    from hermes_state import SessionDB

    db = SessionDB()
    index: Dict[str, Dict[str, str]] = {}
    # origin_json stores the DB-style tool name ("claude-code", "codex-cli");
    # normalize to the lister's source names so lookups match.
    _ALIAS = {"claude-code": "claude", "codex-cli": "codex"}
    try:
        # NOTE: `_conn` is SessionDB's private write connection — used here
        # for a single-pass origin_json scan. If an upstream refactor renames
        # it, this raises and /list degrades to errors[] visibly (never to a
        # wrong dedup result), which is the safe failure mode.
        rows = db._conn.execute(
            "SELECT id, title, origin_json FROM sessions "
            "WHERE origin_json LIKE '%imported_from%' "
            "ORDER BY started_at DESC LIMIT 1000"
        ).fetchall()
        for sid, title, raw in rows:
            if not raw:
                continue
            try:
                imp = json.loads(raw).get("imported_from", {})
            except Exception:
                continue
            fid = imp.get("foreign_session_id")
            path = imp.get("path")
            tool = _ALIAS.get(imp.get("tool"), imp.get("tool"))
            entry = {"hermes_id": sid, "title": title}
            if tool and fid:
                index[f"{tool}:{fid}"] = entry
            if tool and path:
                index[f"{tool}:path:{path}"] = entry
        _index_cache = index
        _index_cached_at = now
        return index
    finally:
        try:
            db.close()
        except Exception:
            pass


# ── Import ───────────────────────────────────────────────────────────────


def _import_opencode(session_id: str) -> str:
    """Import an OpenCode session using the same storage contract as
    import_foreign_session."""
    import uuid
    from datetime import datetime

    from hermes_state import SessionDB

    parsed = parse_opencode_session(OPENCODE_DB, session_id)
    turns = parsed["turns"]
    if not turns:
        raise ValueError(f"No user/assistant turns in opencode session {session_id}")

    first_user = _first_user_line(
        [(t["role"], t["content"]) for t in turns]
    ) or session_id
    first_user = first_user[:59] + "…" if len(first_user) > 60 else first_user
    title = f"Imported from OpenCode: {first_user}"

    new_id = (
        f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    )
    origin = {
        "imported_from": {
            "tool": "opencode",
            "path": f"opencode-db:{session_id}",
            "foreign_session_id": session_id,
        }
    }
    db = SessionDB()
    try:
        db.create_session(
            new_id,
            source="opencode",
            cwd=parsed.get("cwd"),
            origin_json=json.dumps(origin),
        )
        for turn in turns:
            db.append_message(new_id, turn["role"], turn["content"])
        try:
            db.set_session_title(new_id, title)
        except Exception:
            pass
        return new_id
    finally:
        global _index_cache
        _index_cache = None  # invalidate so the next /list sees the new import
        try:
            db.close()
        except Exception:
            pass


# ── Routes ───────────────────────────────────────────────────────────────


@router.get("/list")
async def list_all(source: str = "") -> Dict[str, Any]:
    """List foreign sessions cheaply.

    OpenCode rows come from the aggregate SQL pass (no full parse).
    Claude rows still go through the stock lister (it parses JSONL, but
    Claude Code stores one small file per session — acceptable; the TTL'd
    dedup cache removes the other hot path).
    """
    sessions: List[Dict[str, Any]] = []
    errors: List[str] = []
    if source in ("", "claude"):
        try:
            sessions.extend(list_claude_sessions_meta(errors=errors))
        except Exception as exc:
            errors.append(f"claude: {exc}")
    if source in ("", "opencode"):
        try:
            sessions.extend(_opencode_list_rows(200))
        except Exception as exc:
            errors.append(f"opencode: {exc}")
    sessions.sort(key=lambda s: s.get("mtime_ms") or 0, reverse=True)

    idx = imported_index()
    for s in sessions:
        key = s["source"]
        hit = idx.get(f"{key}:{s['session_id']}") or idx.get(
            f"{key}:path:{s['path']}"
        )
        s["imported"] = bool(hit)
        s["imported_hermes_id"] = hit["hermes_id"] if hit else None
    return {"sessions": sessions[:100], "errors": errors}


@router.get("/view")
async def view_session(source: str, path: str) -> Dict[str, Any]:
    """Read-only transcript view. Reuses the same normalizers as import —
    one parsing contract, two consumers."""
    try:
        if source == "opencode":
            sid = path.removeprefix("opencode-db:")
            parsed = parse_opencode_session(OPENCODE_DB, sid)
        elif source == "claude":
            from pathlib import Path as _P

            from hermes_cli.foreign_sessions import parse_claude_session

            parsed = parse_claude_session(_P(path))
        else:
            return {"ok": False, "error": f"unknown source {source!r}"}
        return {
            "ok": True,
            "title": parsed.get("title_guess"),
            "cwd": parsed.get("cwd"),
            "turns": parsed["turns"],
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@router.post("/import")
async def do_import(body: dict) -> Dict[str, Any]:
    src = (body.get("source") or "").strip().lower()
    path = body.get("path") or ""
    try:
        if src == "opencode":
            sid = path.removeprefix("opencode-db:")
            new_id = _import_opencode(sid)
        elif src == "claude":
            from hermes_cli.foreign_sessions import import_foreign_session

            new_id = import_foreign_session("claude", path)
            global _index_cache
            _index_cache = None  # covers both providers (opencode path clears its own)
        else:
            return {"ok": False, "error": f"unknown source {src!r}"}
        return {"ok": True, "session_id": new_id}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
