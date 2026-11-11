# -*- coding: utf-8 -*-
"""真模型接口 —— 兼容所有 OpenAI 格式的服务（DeepSeek / 通义千问 / 智谱 等）

对应真项目：ultimind-backend/src/qwenpaw/providers/（模型提供方抽象层）

大白话：
    真模型 = 一个会看语义、能自己决定"要不要调工具"的远程服务。
    我们只需要把"对话历史 + 工具清单"发过去，它回：
      - 要么 一句最终回答
      - 要么 "我要调用 xxx 工具，参数是 {...}"

用法（第 7 天做）：
    把 config.example.json 复制成 config.json，填上 base_url / model / api_key。
    没填或填错 → 自动退回规则大脑，不影响使用。
"""
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

CONFIG_FILE = Path(__file__).resolve().parent.parent / "config.json"

DEFAULT_SYSTEM = (
    "你是 agent-mini，一个本地运行的小型 AI 助手。"
    "回答要简短、直接、像一个靠谱的助理。"
    "能调用工具时优先调用工具，不要凭空编造时间、计算结果或笔记内容。"
)


def load_config():
    if not CONFIG_FILE.exists():
        return {}
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def configured():
    c = load_config()
    key = (c.get("api_key") or "").strip()
    return key not in ("", "在这里填你的 API Key")

def _is_retryable(e):
    """判断这个错值不值得重试：网络抖动值得，Key 错/参数错不值得"""
    if isinstance(e, urllib.error.HTTPError):
        return e.code >= 500 or e.code == 429     # 服务器错误、限流 → 可以再试
    return isinstance(e, (urllib.error.URLError, TimeoutError, ConnectionError))


def _post_json(req, retries=3, base_wait=0.8):
    """发 POST 请求，网络类错误自动重试；间隔 0.8s、1.6s（指数退避，别死循环砸人家服务器）"""
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            last_error = e
            if not _is_retryable(e) or attempt == retries:
                print(f"[llm] 第 {attempt} 次失败，不重试：{type(e).__name__} {e}")
                raise
            wait = base_wait * (2 ** (attempt - 1))
            print(f"[llm] 第 {attempt} 次失败（{type(e).__name__}），{wait:.1f}s 后重试")
            time.sleep(wait)
    raise last_error

def decide(history, tools_desc):
    """让真模型做一次决策（返回格式和 mock_llm.decide 完全一样）"""
    c = load_config()
    base_url = (c.get("base_url") or "https://api.deepseek.com").rstrip("/")
    url = base_url + "/chat/completions"

    messages = [{"role": "system", "content": c.get("system_prompt") or DEFAULT_SYSTEM}]
    for m in history:
        if m["role"] == "tool":
            messages.append({"role": "user", "content": f"[工具 {m['name']} 的执行结果] {m['content']}"})
        else:
            messages.append({"role": m["role"], "content": m["content"]})

    payload = {
        "model": c.get("model") or "deepseek-chat",
        "messages": messages,
        "temperature": 0.3,
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t["description"],
                    "parameters": {
                        "type": "object",
                        "properties": {
                            p["name"]: {"type": p["type"], "description": p["desc"]}
                            for p in t["parameters"]
                        },
                        "required": [p["name"] for p in t["parameters"]],
                    },
                },
            }
            for t in tools_desc
        ],
        "tool_choice": "auto",
    }

    req = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + c.get("api_key", ""),
        },
        method="POST",
    )
    data = _post_json(req)

    msg = data["choices"][0]["message"]

    if msg.get("tool_calls"):
        tc = msg["tool_calls"][0]
        try:
            args = json.loads(tc["function"].get("arguments") or "{}")
        except Exception:
            args = {}
        return {
            "type": "tool_call",
            "name": tc["function"]["name"],
            "args": args,
            "thought": (msg.get("content") or "").strip() or f"模型决定调用 {tc['function']['name']}",
            "usage": data.get("usage") or {},
        }

    return {"type": "final", "content": (msg.get("content") or "").strip() or "（模型没有返回内容）",
            "thought": "", "usage": data.get("usage") or {}}



def _tool_schema(tools_desc):
    """把工具清单转成接口要求的格式"""
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": {
                    "type": "object",
                    "properties": {p["name"]: {"type": p["type"], "description": p["desc"]} for p in t["parameters"]},
                    "required": [p["name"] for p in t["parameters"]],
                },
            },
        }
        for t in tools_desc
    ]


def decide_stream(history, tools_desc):
    """流式版 decide：一边生成一边把新文字交出来。

    用法（它是个生成器）：
        for kind, payload in decide_stream(history, tools_desc):
            kind == "delta" → payload 是刚蹦出来的这一小段文字
            kind == "done"  → payload 是完整结果，格式和 decide() 一样
    """
    c = load_config()
    url = (c.get("base_url") or "https://api.deepseek.com").rstrip("/") + "/chat/completions"

    messages = [{"role": "system", "content": c.get("system_prompt") or DEFAULT_SYSTEM}]
    for m in history:
        if m["role"] == "tool":
            messages.append({"role": "user", "content": f"[工具 {m['name']} 的执行结果] {m['content']}"})
        else:
            messages.append({"role": m["role"], "content": m["content"]})

    payload = {
        "model": c.get("model") or "deepseek-chat",
        "messages": messages,
        "temperature": 0.3,
        "stream": True,                  # 关键开关：不攒完再发，边算边发
        "tools": _tool_schema(tools_desc),
        "tool_choice": "auto",
    }

    req = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + c.get("api_key", "")},
        method="POST",
    )

    t0 = time.perf_counter()
    text_parts = []      # 文字碎片，最后拼成完整回答
    calls = {}           # 工具调用也是碎片发来的，按 index 自己拼
    usage = {}

    with urllib.request.urlopen(req, timeout=60) as resp:
        for raw in resp:                                  # 一行一行读，不是一次性读完
            line = raw.decode("utf-8", "ignore").strip()
            if not line or not line.startswith("data:"):
                continue
            body = line[5:].strip()
            if body == "[DONE]":
                break
            try:
                obj = json.loads(body)
            except Exception:
                continue
            if obj.get("usage"):
                usage = obj["usage"]
            for ch in obj.get("choices") or []:
                delta = ch.get("delta") or {}
                if delta.get("content"):
                    text_parts.append(delta["content"])
                    yield ("delta", delta["content"])     # 立刻交出去，让人先看见
                for tc in delta.get("tool_calls") or []:
                    slot = calls.setdefault(tc.get("index", 0), {"id": "", "name": "", "args": ""})
                    if tc.get("id"):
                        slot["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        slot["name"] += fn["name"]
                    if fn.get("arguments"):
                        slot["args"] += fn["arguments"]

    content = "".join(text_parts)
    ms = int((time.perf_counter() - t0) * 1000)

    if calls:
        first = calls[sorted(calls)[0]]
        try:
            args = json.loads(first["args"] or "{}")
        except Exception:
            args = {}
        yield ("done", {"type": "tool_call", "name": first["name"], "args": args,
                        "thought": content.strip() or f"模型决定调用 {first['name']}",
                        "usage": usage, "ms": ms})
        return

    yield ("done", {"type": "final",
                    "content": content.strip() or "（模型没有返回内容）",
                    "thought": "", "usage": usage, "ms": ms})



def summarize(text):
    """把一段长对话压成一小段摘要（只发文字、不带工具，用最少 token 干这件事）"""
    c = load_config()
    url = (c.get("base_url") or "https://api.deepseek.com").rstrip("/") + "/chat/completions"

    payload = {
        "model": c.get("model") or "deepseek-chat",
        "messages": [
            {"role": "system", "content": "你是对话摘要器。把下面的对话压成不超过 120 字的中文摘要，"
                                          "必须保留：用户的名字或身份、正在做的事、明确说过的偏好和得出的结论。"
                                          "不要编造对话里没有的内容，不要写客套话。"},
            {"role": "user", "content": text},
        ],
        "temperature": 0.2,
    }

    req = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + c.get("api_key", "")},
        method="POST",
    )
    data = _post_json(req)
    return (data["choices"][0]["message"]["content"] or "").strip()
