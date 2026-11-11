# -*- coding: utf-8 -*-
"""会话记忆 —— 让 Agent 记得你刚才说过什么

对应真项目：ultimind-backend/src/qwenpaw/sessions/ + memory_context

第 16 天升级：从内存搬进 SQLite（data/memory.db），关掉服务也不会忘。
"""
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

DB_FILE = Path(__file__).resolve().parent.parent / "data" / "memory.db"
MAX_MESSAGES = 40    # 只留最近 40 条


def _conn():
    """打开数据库；第一次运行会自动建库、建表、建索引"""
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_FILE)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS messages (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role       TEXT NOT NULL,
            name       TEXT,
            content    TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_session ON messages(session_id, id)")
    return conn


def get(session_id):
    """把某个会话的历史按时间顺序读出来，格式和原来完全一样"""
    with closing(_conn()) as conn:
        rows = conn.execute(
            "SELECT role, name, content FROM messages WHERE session_id = ? ORDER BY id ASC",
            (session_id,),
        ).fetchall()

    history = []
    for role, name, content in rows:
        msg = {"role": role, "content": content}
        if name:
            msg["name"] = name
        history.append(msg)
    return history


def save(session_id, history):
    """整段重写：先删这个会话的旧记录，再写入最近 MAX_MESSAGES 条"""
    with closing(_conn()) as conn:
        conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for m in history[-MAX_MESSAGES:]:
            conn.execute(
                "INSERT INTO messages (session_id, role, name, content, created_at) VALUES (?,?,?,?,?)",
                (session_id, m.get("role", "user"), m.get("name"), m.get("content", ""), now),
            )
        conn.commit()


def clear(session_id="default"):
    """清空某个会话的历史"""
    with closing(_conn()) as conn:
        conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
        conn.commit()


def list_sessions():
    """列出所有会话：id、消息条数、最近更新时间（最近聊过的排前面）"""
    with closing(_conn()) as conn:
        rows = conn.execute(
            "SELECT session_id, COUNT(*) AS n, MAX(created_at) AS last "
            "FROM messages GROUP BY session_id ORDER BY last DESC"
        ).fetchall()
    return [{"session_id": r[0], "count": r[1], "last": r[2]} for r in rows]


TRACE_SQL = """
CREATE TABLE IF NOT EXISTS traces (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    user_msg   TEXT,
    answer     TEXT,
    rounds     INTEGER,
    tools      TEXT,
    total_ms   INTEGER,
    tokens     INTEGER,
    created_at TEXT NOT NULL
)
"""


def save_trace(session_id, user_msg, answer, rounds, tools_used, total_ms, tokens):
    """把一次请求的体检数据写进库"""
    with closing(_conn()) as conn:
        conn.execute(TRACE_SQL)
        conn.execute(
            "INSERT INTO traces (session_id, user_msg, answer, rounds, tools, total_ms, tokens, created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (session_id, (user_msg or "")[:200], (answer or "")[:500], rounds,
             tools_used or "", total_ms or 0, tokens or 0,
             datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )
        conn.commit()


def list_traces(limit=20):
    """把最近的 trace 读出来（新的在前）"""
    with closing(_conn()) as conn:
        conn.execute(TRACE_SQL)
        rows = conn.execute(
            "SELECT session_id, user_msg, rounds, tools, total_ms, tokens, created_at"
            " FROM traces ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    keys = ["session_id", "user_msg", "rounds", "tools", "total_ms", "tokens", "created_at"]
    return [dict(zip(keys, r)) for r in rows]
