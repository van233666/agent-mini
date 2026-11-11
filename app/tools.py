# -*- coding: utf-8 -*-
"""工具注册表 —— Agent 的"手脚"

对应真项目：ultimind-backend/src/qwenpaw/tool_calls/ + src/qwenpaw/agents/tools/

大白话：
    模型只会"说"，不会"做"。工具就是给它配的手脚：
    模型说"我要调用 get_current_time"，程序真的去取时间，再把结果还给模型。

关键设计（和真项目一致）：
    1. 只认登记过的名字（白名单）——模型胡说一个工具名，直接拒绝；
    2. 每个工具都声明参数 —— 模型知道该传什么，程序也能校验缺参数。
"""
import json
import re
import random
from datetime import datetime
from pathlib import Path
import urllib.parse
import urllib.request

# 笔记文件放在 agent-mini/data/notes.json
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
NOTES_FILE = DATA_DIR / "notes.json"
TRASH_FILE = DATA_DIR / "notes-trash.json"

TOOLS = {}


def tool(name, description, parameters):
    """登记一个工具。parameters 形如 [{"name":"expression","type":"string","desc":"..."}]"""

    def deco(fn):
        TOOLS[name] = {
            "name": name,
            "description": description,
            "parameters": parameters,
            "fn": fn,
        }
        return fn

    return deco


# ── 工具 1：查当前时间 ─────────────────────────────────────────────
@tool("get_current_time", "获取当前的日期、时间和星期", [])
def get_current_time():
    now = datetime.now()
    weekday = "周" + "一二三四五六日"[now.weekday()]
    return {"now": now.strftime("%Y-%m-%d %H:%M:%S"), "weekday": f"{weekday}"}


# ── 工具 2：计算器 ────────────────────────────────────────────────
@tool(
    "calculator",
    "计算一个算术表达式，例如 23*17+9、128/4、(5+3)*2",
    [{"name": "expression", "type": "string", "desc": "只包含数字和 + - * / ( ) 的表达式"}],
)
def calculator(expression: str):
    # 安全校验：只允许数字和四则运算符号，防止被塞进恶意代码
    if not re.fullmatch(r"[0-9\.\+\-\*/\(\)\s%]+", expression or ""):
        raise ValueError("表达式里只能有数字和 + - * / ( )")
    result = eval(expression, {"__builtins__": {}}, {})  # 空命名空间，不能执行任何函数
    return {"expression": expression, "result": result}


# ── 工具 3：保存一条笔记 ──────────────────────────────────────────
@tool(
    "save_note",
    "把一条笔记保存到本地文件，用户说'记一下/记笔记/帮我记'时使用",
    [
        {"name": "content", "type": "string", "desc": "要保存的笔记内容"},
        {"name": "priority", "type": "string", "desc": "重要程度：high=重要，normal=普通"},
    ],
)
def save_note(content: str, priority: str = "normal"):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    notes = _load_notes()
    p = "high" if str(priority).lower() in ("high", "重要", "紧急") else "normal"
    def _norm(s):
        return "".join(ch for ch in str(s) if ch.isalnum()).lower()

    key = _norm(content)
    for n in notes:
        if _norm(n.get("content", "")) == key:
            return {"saved": False, "duplicated": True, "content": n.get("content"), "total": len(notes)}

    notes.append({"content": content, "priority": p, "time": datetime.now().strftime("%Y-%m-%d %H:%M")})
    NOTES_FILE.write_text(json.dumps(notes, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"saved": True, "content": content, "priority": p, "total": len(notes)}



# ── 工具 4：查看笔记 ──────────────────────────────────────────────
@tool("list_notes", "列出已经保存过的所有笔记（重要的排前面）", [])
def list_notes():
    notes = _load_notes()
    notes.sort(key=lambda n: (n.get("priority") == "high", n.get("time", "")), reverse=True)
    return {"count": len(notes), "items": notes[:10]}


# ── 工具 6：删除一条笔记（破坏性操作：先预览，再确认）────────────
@tool(
    "delete_note",
    "删除一条已保存的笔记。这是破坏性操作：第一次调用必须传 confirm=false，只做匹配预览并把匹配到的内容告诉用户；等用户明确同意后，再传 confirm=true 真正删除。匹配到多条时，必须在 confirm=true 的调用里同时传 pick 指明删第几条",
    [
        {"name": "content", "type": "string", "desc": "要删除的笔记内容或关键词"},
        {"name": "confirm", "type": "string", "desc": "false=只预览匹配结果；true=真正删除"},
        {"name": "pick", "type": "string", "desc": "只删其中一条时必填：删第几条（按预览里的 no）"},
    ],
)

def delete_note(content: str, confirm: str = "false", pick: str = ""):
    def _norm2(s):
        return "".join(ch for ch in str(s) if ch.isalnum()).lower()

    key = _norm2(content)
    notes = _load_notes()
    hits = [n for n in notes if key and key in _norm2(n.get("content", ""))]
    if not hits:
        return {"deleted": False, "reason": "没有找到匹配的笔记", "query": content, "total": len(notes)}
    numbered = [{"no": i + 1, "content": n.get("content", ""), "time": n.get("time", "")}
                for i, n in enumerate(hits)]
    if str(confirm).lower() not in ("true", "1", "yes", "是", "确认"):
        return {"deleted": False, "need_confirm": True, "matches": numbered,
                "hint": "匹配到多条时，确认阶段必须传 pick 指明删第几条", "total": len(notes)}
    if len(hits) > 1:
        p = str(pick).strip()
        if not p.isdigit() or not (1 <= int(p) <= len(hits)):
            return {"deleted": False, "need_pick": True, "matches": numbered,
                    "reason": f"匹配到 {len(hits)} 条，必须用 pick 指定删第几条（1-{len(hits)}），我不会替你猜",
                    "total": len(notes)}
        hits = [hits[int(p) - 1]]

    kept = [n for n in notes if n not in hits]
    trash = _load_trash()
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    for n in hits:
        item = dict(n)
        item["deleted_at"] = now
        trash.append(item)
    NOTES_FILE.write_text(json.dumps(kept, ensure_ascii=False, indent=2), encoding="utf-8")
    TRASH_FILE.write_text(json.dumps(trash, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"deleted": True, "removed": [n.get("content", "") for n in hits],
            "total": len(kept), "trash_total": len(trash)}




# ── 工具 7：从回收站恢复笔记（安全操作，不需要二次确认）──────────
@tool(
    "restore_note",
    "从回收站把之前删掉的笔记恢复回来。用户说'恢复/撤销删除/找回'时使用；不知道回收站里有什么时，content 传空字符串即可查看清单",
    [{"name": "content", "type": "string", "desc": "要恢复的笔记内容或关键词；空字符串=只查看回收站清单"}],
)

def restore_note(content: str, pick: str = ""):
    def _norm2(s):
        return "".join(ch for ch in str(s) if ch.isalnum()).lower()

    trash = _load_trash()
    if not trash:
        return {"restored": False, "reason": "回收站是空的"}
    key = _norm2(content)
    if not key:
        return {"restored": False, "trash": [{"no": i + 1, "content": t.get("content"), "deleted_at": t.get("deleted_at")} for i, t in enumerate(trash)]}
    hits = [t for t in trash if key in _norm2(t.get("content", ""))]
    if not hits:
        return {"restored": False, "reason": "回收站里没有匹配的笔记", "query": content, "trash_total": len(trash)}
    if len(hits) > 1:
        p = str(pick).strip()
        if not p.isdigit() or not (1 <= int(p) <= len(hits)):
            return {"restored": False, "need_pick": True,
                    "matches": [{"no": i + 1, "content": h.get("content", "")} for i, h in enumerate(hits)],
                    "reason": f"回收站里匹配到 {len(hits)} 条，必须用 pick 指定恢复第几条（1-{len(hits)}）",
                    "trash_total": len(trash)}
        hits = [hits[int(p) - 1]]
    kept_trash = [t for t in trash if t not in hits]
    notes = _load_notes()
    for t in hits:
        notes.append({k: v for k, v in t.items() if k != "deleted_at"})
    NOTES_FILE.write_text(json.dumps(notes, ensure_ascii=False, indent=2), encoding="utf-8")
    TRASH_FILE.write_text(json.dumps(kept_trash, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"restored": True, "restored_items": [t.get("content", "") for t in hits],
            "total": len(notes), "trash_total": len(kept_trash)}




# ── 工具 8：彻底删除回收站里的笔记（不可恢复，必须说口令）──────────
@tool(
    "purge_trash",
    "彻底删除回收站里的笔记，删了就找不回来。这是最高危险等级：先让用户看清单，再让用户原样说出这句话：我确认永久删除。confirm 只认这句话，传 true/yes 一律拒绝执行",
    [
        {"name": "content", "type": "string", "desc": "要彻底删除哪一条（关键词）；空字符串=清空整个回收站"},
        {"name": "pick", "type": "string", "desc": "只删其中一条时填编号（按清单里的 no）；清空整个回收站时留空"},
        {"name": "confirm", "type": "string", "desc": "必须是这句话：我确认永久删除"},
    ],
)
def purge_trash(content: str, confirm: str = "", pick: str = ""):
    def _norm3(s):
        return "".join(ch for ch in str(s) if ch.isalnum()).lower()

    PASS_PHRASE = "我确认永久删除"
    trash = _load_trash()
    if not trash:
        return {"purged": False, "reason": "回收站是空的，没什么可清的"}

    key = _norm3(content)
    hits = [t for t in trash if (not key) or key in _norm3(t.get("content", ""))]
    numbered = [{"no": i + 1, "content": t.get("content", ""), "deleted_at": t.get("deleted_at")}
                for i, t in enumerate(hits)]
    if not hits:
        return {"purged": False, "reason": "回收站里没有匹配的笔记", "query": content, "trash_total": len(trash)}

    # 第一道闸门：口令不对，一律不执行（true/yes 都不算）
    if str(confirm).strip() != PASS_PHRASE:
        return {"purged": False, "need_passphrase": True, "would_purge": numbered,
                "reason": f"这是不可恢复的操作。请让用户原样回复「{PASS_PHRASE}」，传 true/yes 一律不执行",
                "trash_total": len(trash)}

    # 第二道闸门：按关键词删、且匹配到多条时，必须说清删第几条
    if key and len(hits) > 1:
        p = str(pick).strip()
        if not p.isdigit() or not (1 <= int(p) <= len(hits)):
            return {"purged": False, "need_pick": True, "matches": numbered,
                    "reason": f"匹配到 {len(hits)} 条，必须用 pick 指定删第几条（1-{len(hits)}）",
                    "trash_total": len(trash)}
        hits = [hits[int(p) - 1]]

    kept = [t for t in trash if t not in hits]
    TRASH_FILE.write_text(json.dumps(kept, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"purged": True, "removed": [t.get("content", "") for t in hits],
            "trash_total": len(kept), "note": "已永久删除，不可恢复"}



# ── 工具 9：查天气（走真实网络，会慢也会失败）────────────────────
WEATHER_TEXT = {0: "晴", 1: "晴间多云", 2: "多云", 3: "阴", 45: "雾", 48: "雾凇",
                51: "小毛雨", 53: "毛毛雨", 55: "密毛雨", 61: "小雨", 63: "中雨", 65: "大雨",
                71: "小雪", 73: "中雪", 75: "大雪", 80: "阵雨", 81: "强阵雨", 82: "暴阵雨",
                95: "雷阵雨", 96: "雷阵雨伴冰雹", 99: "强雷暴伴冰雹"}


def _http_json(url, timeout=5):
    """发一个 GET 请求，把返回的 JSON 变成字典。超时就抛异常，绝不无限等待"""
    req = urllib.request.Request(url, headers={"User-Agent": "agent-mini/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


@tool(
    "get_weather",
    "查询某个城市现在的天气。这个工具要走两次真实网络请求，比本地工具慢很多，也可能失败",
    [{"name": "city", "type": "string", "desc": "城市名，例如 广州、北京、上海；不填默认广州"}],
)
def get_weather(city: str = "广州"):
    name = (city or "广州").strip()

    # 第 1 次网络请求：城市名 → 经纬度
    q = urllib.parse.urlencode({"name": name, "count": 1, "language": "zh", "format": "json"})
    geo = _http_json("https://geocoding-api.open-meteo.com/v1/search?" + q, timeout=3)
    if not geo.get("results"):
        return {"ok": False, "error": f"找不到这个城市：{name}"}
    lat = geo["results"][0]["latitude"]
    lon = geo["results"][0]["longitude"]

    # 第 2 次网络请求：经纬度 → 当前天气
    q2 = urllib.parse.urlencode({"latitude": lat, "longitude": lon,
                                 "current": "temperature_2m,weather_code",
                                 "timezone": "Asia/Shanghai"})
    w = _http_json("https://api.open-meteo.com/v1/forecast?" + q2, timeout=3)

    cur = w.get("current") or {}
    code = cur.get("weather_code")
    return {"ok": True, "city": geo["results"][0].get("name") or name,
            "temperature": cur.get("temperature_2m"), "unit": "°C",
            "weather": WEATHER_TEXT.get(code, f"天气代码{code}"),
            "at": cur.get("time"), "source": "open-meteo"}

# ── 工具 5：掷骰子 ───────────────────────────────────────────────
@tool("roll_dice", "掷一个六面骰子，返回 1 到 6 之间的随机数", [])
def roll_dice():
    return {"result": random.randint(1, 6)}

def _load_notes():
    if not NOTES_FILE.exists():
        return []
    try:
        return json.loads(NOTES_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []

def _load_trash():
    if not TRASH_FILE.exists():
        return []
    try:
        return json.loads(TRASH_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def describe():
    """把工具清单交给模型/前端看（不含函数本体）"""
    return [
        {"name": t["name"], "description": t["description"], "parameters": t["parameters"]}
        for t in TOOLS.values()
    ]


def call(name, args):
    """执行工具：白名单 → 校验参数 → 真正执行"""
    if name not in TOOLS:
        raise ValueError(f"没有这个工具：{name}（白名单里只有：{', '.join(TOOLS)}）")
    t = TOOLS[name]
    args = args or {}
    for p in t["parameters"]:
        if p["name"] not in args:
            raise ValueError(f"缺少参数 {p['name']}（{p['desc']}）")
    return t["fn"](**{p["name"]: args[p["name"]] for p in t["parameters"]})



# ══════════ 工具检索：只把"可能用得到"的工具给模型看 ══════════
TOOL_GROUPS = {
    "note": ["save_note", "list_notes", "delete_note", "restore_note", "purge_trash"],
    "time": ["get_current_time"],
    "calc": ["calculator"],
    "fun": ["roll_dice"],
    "net": ["get_weather"],
}

GROUP_WORDS = {
    "note": ["记", "笔记", "备忘", "删", "回收站", "恢复", "清空", "忘了"],
    "time": ["几点", "时间", "日期", "星期", "今天", "现在", "多久"],
    "calc": ["算", "计算", "多少", "加", "减", "乘", "除", "等于", "+", "-", "*", "/", "%"],
    "fun": ["骰子", "随机", "运气", "抽"],
    "net": ["天气", "气温", "下雨", "温度", "几度", "晴"],
}


def scope_for(message):
    """按一句话挑出可能用到的工具（粗糙版"工具检索"）。

    一个都没命中 → 退回全部（保底，宁可多花钱也别让模型少一只手）。
    """
    hit = set()
    for group, words in GROUP_WORDS.items():
        if any(w in message for w in words):
            hit.add(group)

    names = {n for g in hit for n in TOOL_GROUPS.get(g, [])}
    if not names:
        return describe()

    return [d for d in describe() if d["name"] in names]
