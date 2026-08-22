"""session-importer plugin backend.

Lists Claude Code and OpenCode sessions on disk and imports them into
Hermes as resumable plain-text sessions via hermes_cli.foreign_sessions
(Claude) plus a native OpenCode converter (OpenCode stores its history in
a SQLite DB, not JSONL, so the shipped claude/codex parsers don't apply).

Dedup: every Hermes session imported from a foreign tool carries
origin_json.imported_from.{tool, path, foreign_session_id}. /list indexes
those keys at list-time so the pane can badge already-imported sessions.
"""

from __future__ import annotations

import json
import sqlite3
import sys
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


def list_opencode_sessions(limit: int = 200) -> List[Dict[str, Any]]:
    """Discover OpenCode sessions from its SQLite DB, newest first."""
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
        except Exception:
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


def list_claude_sessions_meta() -> List[Dict[str, Any]]:
    """Discover Claude Code sessions via the stock importer's lister."""
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


# ── Dedup index ──────────────────────────────────────────────────────────


def imported_index() -> Dict[str, Dict[str, str]]:
    """Map 'tool:foreign_session_id' (or 'tool:path:path') → hermes session info."""
    from hermes_state import SessionDB

    db = SessionDB()
    index: Dict[str, Dict[str, str]] = {}
    # origin_json stores the DB-style tool name ("claude-code", "codex-cli");
    # normalize to the lister's source names so lookups match.
    _ALIAS = {"claude-code": "claude", "codex-cli": "codex"}
    try:
        rows = db.list_sessions_rich(
            limit=500, compact_rows=True, include_hidden=True,
            include_children=True,
        )
        for row in rows:
            raw = None
            try:
                full = db.get_session(row["id"]) or {}
                raw = full.get("origin_json")
            except Exception:
                raw = None
            if not raw:
                continue
            try:
                imp = json.loads(raw).get("imported_from", {})
            except Exception:
                continue
            fid = imp.get("foreign_session_id")
            path = imp.get("path")
            tool = _ALIAS.get(imp.get("tool"), imp.get("tool"))
            entry = {"hermes_id": row["id"], "title": row.get("title")}
            if tool and fid:
                index[f"{tool}:{fid}"] = entry
            if tool and path:
                index[f"{tool}:path:{path}"] = entry
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
        try:
            db.close()
        except Exception:
            pass


# ── Routes ───────────────────────────────────────────────────────────────


@router.get("/list")
async def list_all(source: str = "") -> Dict[str, Any]:
    sessions: List[Dict[str, Any]] = []
    errors: List[str] = []
    if source in ("", "claude"):
        try:
            sessions.extend(list_claude_sessions_meta())
        except Exception as exc:
            errors.append(f"claude: {exc}")
    if source in ("", "opencode"):
        try:
            sessions.extend(list_opencode_sessions())
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
        else:
            return {"ok": False, "error": f"unknown source {src!r}"}
        return {"ok": True, "session_id": new_id}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
