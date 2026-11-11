# -*- coding: utf-8 -*-
"""规则大脑（mock）—— 零成本、不需要 API Key 的"假模型"

对应真项目：ultimind-backend 里"大模型故障时降级到规则兜底"的思路
         （你在 ua-mini 里也用过：LLM_FAULTY 时回复全走规则）

大白话：
    真模型是"看语义猜要干什么"，规则大脑是"看关键词决定要干什么"。
    第 1–6 天先用它，让你在不花钱、不联网的情况下看清 Agent 的整条流程；
    第 7 天换上真模型，对比两者的差别——这是最好的理解方式。

它只做两件事：
    1. 判断这条消息要不要调工具、调哪个、参数是什么；
    2. 如果上一步刚拿到工具结果，就把结果总结成一句人话回答。
"""
import re
from datetime import datetime

TIME_WORDS = ("几点", "时间", "日期", "星期", "今天几号")
NOTE_SAVE_WORDS = ("记一条", "记笔记", "帮我记", "记下", "保存笔记", "记住")
NOTE_VIEW_WORDS = ("笔记有哪些", "我的笔记", "看笔记", "列出笔记", "笔记清单", "有什么笔记")
CALC_WORDS = ("算", "计算", "等于", "次方")
ARITH_RUN = re.compile(r"[0-9][0-9\.\+\-\*/\(\)\s%]*$|[0-9\.\(\)][0-9\.\+\-\*/\(\)\s%]+")


def decide(history, tools_desc):


    last = history[-1]

    # ① 上一条是工具结果 → 把结果翻译成回答
    if last["role"] == "tool":
        return {"type": "final", "content": _summarize(last["name"], last["content"]), "thought": "拿到工具结果了，总结成回答"}

    msg = last["content"].strip()

    # ② 保存笔记（必须排在最前：'记一条笔记：明天9点复习' 里也含'9点'）
    for w in NOTE_SAVE_WORDS:
        if w in msg:
            content = msg
            for w2 in NOTE_SAVE_WORDS:
                content = content.replace(w2, "")
            content = content.lstrip("：: ，,。 ")
            for p in ("重要：", "重要", "紧急：", "紧急"):
                if content.startswith(p):
                    content = content[len(p):].lstrip("：: ，,。 ")
                    break
            if not content:
                return {"type": "final", "content": "你要记什么内容？说'记一条：<内容>'就行。",
                        "thought": "想记笔记但没说内容"}
            priority = "high" if ("重要" in msg or "紧急" in msg) else "normal"
            return {"type": "tool_call", "name": "save_note", "args": {"content": content, "priority": priority},
                    "thought": f"用户要记笔记（{priority}），调用 save_note 保存'{content}'"}

    # ③ 查看笔记
    for w in NOTE_VIEW_WORDS:
        if w in msg:
            return {"type": "tool_call", "name": "list_notes", "args": {}, "thought": "用户想查看笔记，调用 list_notes"}

    # ④ 时间
    for w in TIME_WORDS:
        if w in msg:
            return {"type": "tool_call", "name": "get_current_time", "args": {}, "thought": "用户在问时间，调用 get_current_time"}

    # ⑤ 计算
    if any(w in msg for w in CALC_WORDS):
        # 次方：支持"2的10次方"，也认"2的一千万次方"，但给指数设上限
        m = re.search(r"([0-9一二三四五六七八九十百千万]+)\s*的\s*([0-9一二三四五六七八九十百千万]+)\s*次方", msg)
        if m:
            base = _cn_to_num(m.group(1))
            exp = _cn_to_num(m.group(2))
            if exp is not None and exp > 1000:
                return {"type": "final",
                        "content": f"{base} 的 {exp} 次方太大了：我最多算到 1000 次方，再大就会把服务卡住（这叫执行预算）。",
                        "thought": "指数超出上限，直接拒绝，不调用工具"}
            if base is not None and exp is not None:
                expr = f"{base}**{exp}"
                return {"type": "tool_call", "name": "calculator", "args": {"expression": expr},
                        "thought": f"用户要算 {base} 的 {exp} 次方，调用 calculator 计算 {expr}"}

        expr = _extract_expression(msg)
        if expr:
            return {"type": "tool_call", "name": "calculator", "args": {"expression": expr}, "thought": f"用户在算数，调用 calculator 计算 {expr}"}
        return {"type": "final", "content": "没看清要算的式子，写成'算 23*17+9'这样就行。", "thought": "有'算'字但没找到算式"}

    # ⑤.5 掷骰子
    for w in ("掷骰子", "骰子", "掷个骰"):
        if w in msg:
            return {"type": "tool_call", "name": "roll_dice", "args": {}, "thought": "用户要掷骰子，调用 roll_dice"}


    # ⑥ 什么都没命中 → 直接回答（并告诉用户它能干什么）
    return {
        "type": "final",
        "content": (
            "我是 agent-mini 的规则大脑（不用 API Key 的版本）。\n"
            "我现在会：查时间、算数、记笔记、看笔记。\n"
            "试试说：现在几点了 / 算 23*17+9 / 记一条：明天9点复习简历 / 我的笔记有哪些"
        ),
        "thought": "关键词都没命中，直接回答",
    }

_CN_DIGIT = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNIT = {"十": 10, "百": 100, "千": 1000, "万": 10000}


def _cn_to_num(s):
    """把"一千万""十万""一百""三"转成数字；认不出来就返回 None"""
    if not s:
        return None
    if s.isdigit():
        return int(s)
    total, cur = 0, 0
    for ch in s:
        if ch in _CN_DIGIT:
            cur = _CN_DIGIT[ch]
        elif ch in _CN_UNIT:
            u = _CN_UNIT[ch]
            if u == 10000:
                total = (total + (cur or 1)) * 10000
            else:
                total += (cur or 1) * u
            cur = 0
        else:
            return None
    return total + cur


def _extract_expression(msg):
    """从'算一下 23*17+9'里把 23*17+9 抠出来"""
    candidates = ARITH_RUN.findall(msg)
    candidates = [c.strip() for c in candidates if any(ch.isdigit() for ch in c) and any(op in c for op in "+-*/")]
    if not candidates:
        return ""
    return max(candidates, key=len)


def _summarize(tool_name, result_json):
    """工具结果 → 一句人话"""
    import json

    try:
        r = json.loads(result_json) if isinstance(result_json, str) else result_json
    except Exception:
        r = {"raw": result_json}

    if tool_name == "get_current_time":
        return f"现在是 {r.get('now', '')}，{r.get('weekday', '')}。"
    if tool_name == "calculator":
        return f"{r.get('expression', '')} = {r.get('result', '')}"
    if tool_name == "save_note":
        if r.get("duplicated"):
            return f"这条已经记过了：{r.get('content', '')}，没有重复保存。"
        return f"记好了：{r.get('content', '')}（第 {r.get('total', '?')} 条）"
    if tool_name == "list_notes":
        items = r.get("items") or []
        if not items:
            return "你还没有任何笔记。"
        lines = [f"· {'【重要】' if i.get('priority') == 'high' else ''}{i.get('time', '')}  {i.get('content', '')}" for i
                 in items]
        return f"共 {r.get('count', 0)} 条，最近几条：\n" + "\n".join(lines)
    if tool_name == "roll_dice":
        return f"掷出了 {r.get('result', '?')} 点。"

    return f"工具 {tool_name} 返回：{json.dumps(r, ensure_ascii=False)}"
