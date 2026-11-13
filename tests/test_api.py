# -*- coding: utf-8 -*-
"""agent-mini 接口自动化用例

覆盖四类：
    契约   —— 健康检查 / 工具清单 / trace 字段 / 记忆接口的返回结构
    边界   —— 空输入、2000 字上限、2001 字越界
    并发   —— 同会话限流（串行与并发两种打法）、换会话隔离
    负向   —— 编造工具名、缺必填参数、破坏性操作口令校验

设计原则：断言只依赖真实行为，不假设"应该有"的东西。
跑法：在 agent-mini 根目录执行  .venv\\Scripts\\python.exe -m pytest -v
"""
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from app import main, tools


# ══════════════════ 一、契约：接口返回结构对不对 ══════════════════

def test_health_reports_alive(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["version"]


def test_tool_registry_contract(client):
    """9 个工具，每个必须有 name / description / parameters，名字不重复"""
    r = client.get("/api/tools")
    assert r.status_code == 200
    registry = r.json()["tools"]

    assert len(registry) == 9
    names = [t["name"] for t in registry]
    assert len(names) == len(set(names)), "工具名重复了"

    for t in registry:
        assert t["name"]
        assert t["description"]
        assert isinstance(t["parameters"], list)

    assert set(names) == {
        "get_current_time", "calculator", "save_note", "list_notes", "delete_note",
        "restore_note", "purge_trash", "roll_dice", "get_weather",
    }


def test_scope_narrows_to_one_tool():
    """按意图裁剪工具集：问时间只带时间工具，这是 1017 → 208 tok 的来源"""
    scope = tools.scope_for("现在几点了")
    assert [d["name"] for d in scope] == ["get_current_time"]


def test_scope_falls_back_to_all():
    """一个关键词都没命中 → 退回全部，宁可多花钱也不让模型少一只手"""
    assert len(tools.scope_for("随便聊两句")) == 9


def test_memory_endpoint_shape(client, sid):
    r = client.get(f"/api/memory/{sid}")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"limit", "count", "messages"}


def test_trace_contract(client, sid):
    """每轮请求必须落一条 trace，字段齐全，否则回归时没有断言依据"""
    client.post("/api/chat", json={"message": "现在几点了", "session_id": sid})

    traces = client.get("/api/traces?limit=5").json()["traces"]
    assert traces, "没有落 trace"

    latest = traces[0]
    assert {"rounds", "tools", "total_ms", "tokens", "created_at"} <= set(latest)
    assert latest["rounds"] >= 1
    assert latest["tools"] == "get_current_time"
    assert latest["created_at"]


# ══════════════════ 二、边界：输入长度与空值 ══════════════════

def test_empty_message_rejected(client, sid):
    r = client.post("/api/chat", json={"message": "   ", "session_id": sid})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "BAD_INPUT"


def test_over_limit_message_rejected(client, sid):
    r = client.post("/api/chat", json={"message": "啊" * (main.MAX_INPUT_CHARS + 1),
                                       "session_id": sid})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "BAD_INPUT"


def test_boundary_exactly_at_limit_passes(client, sid):
    """正好 2000 字应当放行 —— 边界值上一次成功、下一次失败"""
    r = client.post("/api/chat", json={"message": "字" * main.MAX_INPUT_CHARS,
                                       "session_id": sid})
    assert r.status_code == 200


# ══════════════════ 三、并发：限流维度与隔离 ══════════════════

def test_rate_limit_serial_same_session(client, sid):
    """同一会话 10 秒内最多 2 次，第 3 次必须被拦，且是 429 不是 500"""
    codes = []
    for _ in range(3):
        r = client.post("/api/chat", json={"message": "现在几点了", "session_id": sid})
        codes.append((r.status_code, r.json().get("error", {}).get("code")))

    assert codes[:2] == [(200, None), (200, None)]
    assert codes[2] == (429, "RATE_LIMITED")


def test_rate_limit_is_per_session_not_global(client, sid):
    """A 会话被限流后，B 会话必须仍然畅通 —— 否则限流会误伤正常用户"""
    for _ in range(3):
        client.post("/api/chat", json={"message": "现在几点了", "session_id": sid})

    r = client.post("/api/chat", json={"message": "现在几点了", "session_id": sid + "-b"})
    assert r.status_code == 200


def test_rate_limit_concurrent_same_session(client, sid):
    """3 个请求同时打进来：放行 2 个、拒绝 1 个（实测行为）"""
    def hit(_):
        r = client.post("/api/chat", json={"message": "现在几点了", "session_id": sid})
        return r.status_code

    with ThreadPoolExecutor(max_workers=3) as pool:
        codes = list(pool.map(hit, range(3)))

    assert codes.count(200) == 2
    assert codes.count(429) == 1


# ══════════════════ 四、流式：事件序列与护栏 ══════════════════

def test_stream_event_sequence(client, sid):
    """NDJSON 逐块解析：每块都是合法 JSON，序列首尾固定，工具三兄弟成组出现"""
    r = client.post("/api/chat/stream", json={"message": "现在几点了", "session_id": sid})
    assert r.status_code == 200

    lines = [line for line in r.text.splitlines() if line.strip()]
    events = [json.loads(line) for line in lines]      # 解析失败会直接抛，等于断言"每块合法"
    kinds = [e["kind"] for e in events]

    assert kinds[0] == "user"
    assert kinds[1] == "scope"
    assert kinds[-1] == "answer"                        # 必须以回答收尾，不能断在半路
    assert "tool_call" in kinds and "tool_result" in kinds

    call_idx = kinds.index("tool_call")
    assert kinds[call_idx + 1] == "tool_result"
    assert events[call_idx]["name"] == "get_current_time"


def test_stream_guard_returns_400(client, sid):
    """回归用例：流式接口的护栏必须在响应开始前生效

    历史：护栏原先写在 gen() 生成器内部，抛错时响应头已发出，
    客户端拿到 200 + 空响应（RuntimeError: response already started）。
    修复：把 _guard 提到 chat_stream 函数体、返回 StreamingResponse 之前。
    """
    r = client.post("/api/chat/stream", json={"message": "啊" * (main.MAX_INPUT_CHARS + 500),
                                              "session_id": sid})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "BAD_INPUT"


def test_stream_guard_rate_limited_returns_429(client, sid):
    """同一会话连打 3 次流式：前 2 次放行，第 3 次必须 429 而不是 200 空响应"""
    for _ in range(2):
        r = client.post("/api/chat/stream", json={"message": "现在几点了", "session_id": sid})
        assert r.status_code == 200

    r = client.post("/api/chat/stream", json={"message": "现在几点了", "session_id": sid})
    assert r.status_code == 429
    assert r.json()["error"]["code"] == "RATE_LIMITED"


# ══════════════════ 五、负向：工具白名单与参数校验 ══════════════════

def test_unknown_tool_is_rejected():
    """模型编造工具名 → 必须被白名单拦住，而不是静默执行或抛未知异常"""
    with pytest.raises(ValueError) as err:
        tools.call("rm_rf_everything", {})
    assert "没有这个工具" in str(err.value)


def test_missing_required_arg_is_rejected():
    """缺必填参数 → 报出缺哪个参数，模型才能自己补齐再重试"""
    with pytest.raises(ValueError) as err:
        tools.call("calculator", {})
    assert "缺少参数" in str(err.value)


def test_purge_requires_exact_passphrase(tmp_path, monkeypatch):
    """不可逆删除必须原样口令：传 true 一律不执行，原样口令才真删

    回收站指向临时文件，不碰你真实的 data/trash.json。
    """
    trash = tmp_path / "trash.json"
    trash.write_text(
        json.dumps([{"content": "待删的测试笔记", "deleted_at": "2026-11-13 12:00:00"}],
                   ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(tools, "TRASH_FILE", trash)

    # 第一道闸门：口令不对 → 拒绝，且数据必须还在
    refused = tools.call("purge_trash", {"content": "", "pick": "", "confirm": "true"})
    assert refused["purged"] is False
    assert refused["need_passphrase"] is True
    assert "我确认永久删除" in refused["reason"]
    assert json.loads(trash.read_text(encoding="utf-8")), "口令不对却把数据删了"

    # 第二道闸门：原样口令 → 真删
    done = tools.call("purge_trash", {"content": "", "pick": "", "confirm": "我确认永久删除"})
    assert done["purged"] is True
    assert json.loads(trash.read_text(encoding="utf-8")) == []
