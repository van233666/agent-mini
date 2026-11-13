# -*- coding: utf-8 -*-
"""pytest 公共装置：隔离数据库、关掉真模型、清空限流状态

为什么要这三件事：
1. 隔离数据库 —— 测试写自己的临时 memory.db，不污染 data/memory.db 里你的真实会话
2. 关掉真模型 —— monkeypatch llm.configured，让 Agent 走规则大脑，测试不联网、不花钱、结果稳定
3. 清空限流状态 —— main._last_calls 是模块级全局变量，不清理会让用例之间互相限流
"""
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """带隔离环境的 HTTP 客户端（进程内直连 FastAPI，不启端口）"""
    from fastapi.testclient import TestClient

    from app import llm, main, memory

    monkeypatch.setattr(memory, "DB_FILE", tmp_path / "memory.db")
    monkeypatch.setattr(llm, "configured", lambda: False)
    main._last_calls.clear()

    with TestClient(main.app, raise_server_exceptions=False) as c:
        yield c

    main._last_calls.clear()


@pytest.fixture()
def sid():
    """每个用例一个独立会话 id，避免会话历史互相影响"""
    return "test-" + uuid.uuid4().hex[:8]
