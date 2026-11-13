# -*- coding: utf-8 -*-
"""agent-mini 入口 —— 网页和程序之间的接线口

对应真项目：ultimind-backend/src/qwenpaw/app/（FastAPI 路由层，412 个接口）
我们这个迷你版只有 4 个接口，够用：
    GET  /            打开聊天页面
    GET  /api/health  健康检查（是不是活着、用哪个大脑）
    GET  /api/tools   工具清单
    POST /api/chat    发一句话，拿回"思考过程 + 最终回答"
"""
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import json
from pathlib import Path


from . import llm, loop, memory, tools

BASE_DIR = Path(__file__).resolve().parent
app = FastAPI(title="agent-mini", description="UltiMind 的 1% 迷你版")

app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
class GuardError(Exception):
    """用户输入不合格（太长、太频繁），不是服务器坏了"""
    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.status_code = status_code


MAX_INPUT_CHARS = 2000        # 一句话最长 2000 字，超了直接拒
RATE_WINDOW_SEC = 10          # 同一个会话，10 秒内最多发 2 次
RATE_LIMIT = 2
_last_calls = {}              # {session_id: [时间戳, ...]}


def _guard(text, session_id):
    """三道闸：长度、频率、空内容。过不了就抛 ValueError，交给统一错误信封"""
    import time as _t

    text = (text or "").strip()
    if not text:
        raise GuardError("说的话是空的")
    if len(text) > MAX_INPUT_CHARS:
        raise GuardError(f"输入太长（{len(text)} 字），上限 {MAX_INPUT_CHARS} 字")

    now = _t.time()
    recent = [t for t in _last_calls.get(session_id, []) if now - t < RATE_WINDOW_SEC]
    if len(recent) >= RATE_LIMIT:
        raise GuardError(f"发太快了：{RATE_WINDOW_SEC} 秒内最多 {RATE_LIMIT} 次，缓一下", 429)

    recent.append(now)
    _last_calls[session_id] = recent
    return text

class ChatIn(BaseModel):
    message: str
    session_id: str = "default"

class ClearIn(BaseModel):
    session_id: str = "default"


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "brain": "真模型" if llm.configured() else "规则大脑(mock)",
        "version": "0.1.0",
    }


@app.get("/api/tools")
def tool_list():
    return {"tools": tools.describe()}


@app.post("/api/chat")
def chat(body: ChatIn):
    text = _guard(body.message, body.session_id)
    steps, answer = loop.run(text, body.session_id)

    return {"steps": steps, "answer": answer}


@app.post("/api/clear")
def clear(body: ClearIn):
    memory.clear(body.session_id)
    return {"ok": True}

@app.get("/api/sessions")
def sessions():
    return {"sessions": memory.list_sessions()}

@app.post("/api/chat/stream")
def chat_stream(body: ChatIn):
    """流式接口：一边跑一边往外推，一行一个 JSON（这种格式叫 NDJSON）

    注意：护栏必须在返回 StreamingResponse 之前跑完。
    如果放进生成器里，等它抛错时响应头已经发出去了，客户端只会拿到 200 + 空响应，
    既不是 400 也不是 429，调用方没法判断是自己输入有问题还是服务挂了。
    """
    from fastapi.responses import StreamingResponse

    text = _guard(body.message, body.session_id)

    def gen():
        for ev in loop.run_stream(text, body.session_id):
            yield json.dumps(ev, ensure_ascii=False) + "\n"

    return StreamingResponse(gen(), media_type="application/x-ndjson")


@app.get("/api/traces")
def traces(limit: int = 20):
    """查最近 N 次请求的体检数据（新的在前）"""
    return {"traces": memory.list_traces(limit)}



@app.get("/api/memory/{session_id}")
def show_memory(session_id: str):
    h = memory.get(session_id)
    return {"limit": memory.MAX_MESSAGES, "count": len(h), "messages": h}


@app.get("/")
def index():
    return FileResponse(BASE_DIR / "static" / "index.html")

@app.exception_handler(GuardError)
async def guard_error(request: Request, exc: GuardError):
    """输入不合格 → 400 / 限流 → 429，别都算成 500"""
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "ok": False,
            "error": {"code": "BAD_INPUT" if exc.status_code == 400 else "RATE_LIMITED",
                      "type": "GuardError", "message": str(exc)},
        },
    )

