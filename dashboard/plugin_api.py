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


def _oc_flatten_parts(parts: List[Dict[str, Any]]) -> Tuple[str, Dict[str, int]]:
    """Flatten one OpenCode message's parts to text + tool summaries.

    Returns (text, tool_counts) where tool_counts is a {tool_name: count}
    dict — the viewer renders it as a footer; import flattens it inline.
    """
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
            tools.append(name)
        # reasoning / step-start / step-finish / others: skipped
    counts: Dict[str, int] = {}
    for name in tools:
        counts[name] = counts.get(name, 0) + 1
    return "\n\n".join(texts), counts


def _merge_turns(
    raw_turns: List[Tuple[str, str]],
    tool_meta: Optional[List[Optional[Dict[str, int]]]] = None,
) -> List[Dict[str, Any]]:
    """Merge consecutive same-role turns; guarantee strict alternation.

    When ``tool_meta`` is given (parallel list), each merged turn aggregates
    the counts of every source turn folded into it — no post-hoc matching.
    """
    merged: List[Dict[str, Any]] = []
    for i, (role, text) in enumerate(raw_turns):
        text = text.strip()
        counts = tool_meta[i] if tool_meta else None
        if not text and not counts:
            continue
        if merged and merged[-1]["role"] == role:
            if text:
                merged[-1]["content"] += "\n\n" + text
            for name, n in (counts or {}).items():
                prev = merged[-1].get("tools") or {}
                prev[name] = prev.get(name, 0) + n
                merged[-1]["tools"] = prev
        else:
            entry: Dict[str, Any] = {"role": role, "content": text}
            if counts:
                entry["tools"] = dict(counts)
            merged.append(entry)
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


def parse_opencode_session(
    db_path: Path, session_id: str, with_tools: bool = False
) -> Dict[str, Any]:
    """Parse one OpenCode session from its SQLite store into normalized turns.

    With ``with_tools=True`` each turn dict gains ``tools: {name: count}``
    (assistant turns only) — used by the read-only /view route. Import keeps
    the flattened inline form.
    """
    turns: List[Tuple[str, str]] = []
    tool_meta: List[Optional[Dict[str, int]]] = []
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
                tool_meta.append(None)
            else:
                chunks = [text] if text else []
                if tools and not with_tools:
                    chunks += [f"[ran tool: {name}]" for name in tools]
                if chunks or (tools and with_tools):
                    turns.append(("assistant", "\n\n".join(chunks)))
                    tool_meta.append(dict(tools) if with_tools else None)
    finally:
        con.close()
    result_turns = _merge_turns(
        turns, tool_meta if with_tools else None
    )
    return {
        "turns": result_turns,
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
    return {
        "sessions": sessions[:500],
        "total": len(sessions),
        "errors": errors,
    }


@router.get("/counts")
async def counts() -> Dict[str, Any]:
    """Cheap count-only endpoint for the statusbar chip — no session rows."""
    total = 0
    fresh = 0
    idx = imported_index()
    for source in ("claude", "opencode"):
        try:
            if source == "claude":
                from hermes_cli.foreign_sessions import list_claude_sessions

                ids = [
                    (s.source, s.session_id, str(s.path))
                    for s in list_claude_sessions()
                ]
            else:
                con = sqlite3.connect(f"file:{OPENCODE_DB}?mode=ro", uri=True)
                try:
                    rows = con.execute(
                        "SELECT id FROM session WHERE time_archived IS NULL"
                    ).fetchall()
                finally:
                    con.close()
                ids = [("opencode", sid, f"opencode-db:{sid}") for (sid,) in rows]
            for src, fid, path in ids:
                # cheap turn-presence check skipped: sessions with no turns
                # are rare and the chip is approximate by design.
                total += 1
                if not (
                    idx.get(f"{src}:{fid}") or idx.get(f"{src}:path:{path}")
                ):
                    fresh += 1
        except Exception:
            continue
    return {"total": total, "fresh": fresh}


_TOOL_LINE_RE = None  # compiled lazily below


def _extract_tools_from_flat(content: str):
    """Split inline ``[ran tool: name]`` chunks out of a flattened turn.

    Returns (clean_text, {name: count}). Used by /view only — the viewer
    shows tool usage as a footer; import keeps the flattened inline form.
    """
    import re as _re

    global _TOOL_LINE_RE
    if _TOOL_LINE_RE is None:
        _TOOL_LINE_RE = _re.compile(r"\[ran tool: ([^\]]+)\]")
    counts: Dict[str, int] = {}
    for name in _TOOL_LINE_RE.findall(content):
        counts[name] = counts.get(name, 0) + 1
    clean = _TOOL_LINE_RE.sub("", content)
    clean = "\n\n".join(p for p in (s.strip() for s in clean.split("\n\n")) if p)
    return clean, (counts or None)


def _attach_claude_tools(jsonl_path: Path, turns: List[Dict[str, Any]]) -> None:
    """Attach ``tools: {name: count}`` to claude view turns by extracting the
    inline ``[ran tool: …]`` chunks the stock flattener emitted (exact counts,
    no second file walk needed)."""
    for turn in turns:
        if turn["role"] == "assistant":
            clean, counts = _extract_tools_from_flat(turn["content"])
            turn["content"] = clean
            if counts:
                turn["tools"] = counts


@router.get("/view")
async def view_session(source: str, path: str) -> Dict[str, Any]:
    """Read-only transcript view. Reuses the same normalizers as import —
    one parsing contract, two consumers. Turns may carry ``tools`` counts
    for the viewer's per-message tool footer (import output never does)."""
    try:
        if source == "opencode":
            sid = path.removeprefix("opencode-db:")
            parsed = parse_opencode_session(OPENCODE_DB, sid, with_tools=True)
        elif source == "claude":
            from pathlib import Path as _P

            from hermes_cli.foreign_sessions import parse_claude_session

            parsed = parse_claude_session(_P(path))
            _attach_claude_tools(_P(path), parsed["turns"])
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
