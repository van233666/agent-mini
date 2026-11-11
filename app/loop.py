# -*- coding: utf-8 -*-
"""Agent 循环 —— 整个 AI Agent 的心脏

对应真项目：ultimind-backend/src/qwenpaw/loop/ + tool_calls/（关键路径）

大白话，只有三步，循环着来：
    1. 想：把"用户说的话 + 之前工具的结果 + 工具清单"交给大脑，问它下一步干什么
    2. 做：它说要调工具，程序就去执行
    3. 看：把工具的结果塞回对话历史，再问一次
    ……直到它说"我要给出最终回答"。

这也叫 ReAct（Reason + Act）。真项目的多智能体、MCP、Skill 全都长在这条循环上。
"""
import json
import time


from . import llm, memory, mock_llm, tools

MAX_ROUNDS = 4   # 最多循环几轮，防止死循环（真项目也有类似的轮数/预算上限）
KEEP_RECENT = 6      # 压缩后保留最近几条原话
COMPRESS_AT = 14     # 历史超过这个条数就压一次


def _maybe_compress(history):
    """历史太长就压：把前面一大段压成一条摘要，只留最近几条原话。

    返回 (新历史, 被压掉的条数, 摘要字数)
    """
    if not llm.configured() or len(history) <= COMPRESS_AT:
        return history, 0, 0

    old = history[:-KEEP_RECENT]
    lines = [f"{m.get('role')}：{m.get('content', '')}" for m in old]
    try:
        summary = llm.summarize("\n".join(lines))
    except Exception as e:
        print(f"[loop] 摘要失败，跳过压缩：{e}")
        return history, 0, 0

    new_history = [{"role": "user", "content": "【之前对话的摘要】" + summary}] + history[-KEEP_RECENT:]
    return new_history, len(old), len(summary)

def _save_trace(session_id, user_message, answer, rounds, steps, total_ms):
    """从 steps 里算出体检数据并落库；写失败不影响主流程"""
    try:
        tools_used = ",".join(s.get("name", "") for s in steps if s.get("kind") == "tool_call")
        tokens = sum((s.get("tokens") or 0) for s in steps if s.get("kind") in ("think", "answer"))
        memory.save_trace(session_id, user_message, answer, rounds, tools_used, total_ms, tokens)
    except Exception as e:
        print(f"[loop] 写 trace 失败：{e}")


def _save_trace_stream(session_id, user_message, answer, rounds, tools_used, tok_sum, total_ms):
    """流式版：数据是边跑边攒的，直接传进来"""
    try:
        memory.save_trace(session_id, user_message, answer, rounds, ",".join(tools_used), total_ms, tok_sum)
    except Exception as e:
        print(f"[loop] 写 trace 失败：{e}")


def run(user_message, session_id="default"):
    steps = []
    history = memory.get(session_id)
    history, squashed, sum_len = _maybe_compress(history)
    if squashed:
        steps.append({"kind": "compress",
                      "text": f"历史太长，把前面 {squashed} 条压成了 {sum_len} 字摘要"})

    history.append({"role": "user", "content": user_message})

    steps.append({"kind": "user", "text": user_message})
    scope = tools.scope_for(user_message)      # 这次只带这些工具给模型
    steps.append({"kind": "scope",
                  "text": f"本次给模型看 {len(scope)} 个工具：" + "、".join(d["name"] for d in scope)})


    brain_name = "真模型" if llm.configured() else "规则大脑"
    brain = llm.decide if llm.configured() else mock_llm.decide
    t_all = time.perf_counter()          # 整次请求的起点

    for round_no in range(1, MAX_ROUNDS + 1):
        # ── ① 想 ──
        t0 = time.perf_counter()
        try:
            decision = brain(history, scope)

        except Exception as e:
            # 真模型挂了 → 降级到规则大脑（熔断兜底，服务不死）
            steps.append({"kind": "fallback", "text": f"真模型调用失败（{e}），已降级到规则大脑继续"})
            brain = mock_llm.decide
            brain_name = "规则大脑(降级)"
            decision = mock_llm.decide(history, scope)

        think_ms = int((time.perf_counter() - t0) * 1000)   # 这一轮"想"花了多少毫秒

        thought = (decision.get("thought") or "").strip()
        if thought:
            steps.append({"kind": "think", "text": thought, "round": round_no, "ms": think_ms,
                          "tokens": (decision.get("usage") or {}).get("prompt_tokens")})


        # ── ② 做 ──
        if decision.get("type") == "tool_call":
            name = decision.get("name", "")
            args = decision.get("args") or {}
            steps.append({"kind": "tool_call", "name": name, "args": args, "round": round_no})

            t1 = time.perf_counter()
            try:
                result = tools.call(name, args)
            except Exception as e:
                result = {"error": str(e)}
            tool_ms = int((time.perf_counter() - t1) * 1000)  # 工具本身花了多少毫秒

            steps.append({"kind": "tool_result", "name": name, "result": result,
                          "round": round_no, "ms": tool_ms})

            # ── ③ 看：把结果塞回历史，进入下一轮 ──
            history.append({"role": "tool", "name": name, "content": json.dumps(result, ensure_ascii=False)})
            continue

        # ── 它说可以回答了 ──
        answer = decision.get("content") or "（没有内容）"
        total_ms = int((time.perf_counter() - t_all) * 1000)   # 整次请求总耗时
        steps.append({"kind": "answer", "text": answer, "round": round_no,
                      "brain": brain_name, "ms": total_ms,
                      "tokens": (decision.get("usage") or {}).get("prompt_tokens")})
        history.append({"role": "assistant", "content": answer})
        memory.save(session_id, history)
        _save_trace(session_id, user_message, answer, round_no, steps, total_ms)

        return steps, answer

    # 超过最大轮数，强制收尾
    answer = f"（已经循环了 {MAX_ROUNDS} 轮还没得出结论，先停在这里。可以换个说法再试。）"
    total_ms = int((time.perf_counter() - t_all) * 1000)
    steps.append({"kind": "answer", "text": answer, "round": MAX_ROUNDS,
                  "brain": brain_name, "ms": total_ms})
    history.append({"role": "assistant", "content": answer})
    memory.save(session_id, history)
    _save_trace(session_id, user_message, answer, MAX_ROUNDS, steps, total_ms)
    return steps, answer



def run_stream(user_message, session_id="default"):
    """流式版 run：每发生一件事就交一条事件出去，页面能边收边画。

    事件 kind：user / live（正在蹦的字）/ think / tool_call / tool_result / answer / fallback
    """
    history = memory.get(session_id)
    history, squashed, sum_len = _maybe_compress(history)
    if squashed:
        yield {"kind": "compress",
               "text": f"历史太长，把前面 {squashed} 条压成了 {sum_len} 字摘要"}

    history.append({"role": "user", "content": user_message})
    yield {"kind": "user", "text": user_message}

    scope = tools.scope_for(user_message)
    yield {"kind": "scope",
           "text": f"本次给模型看 {len(scope)} 个工具：" + "、".join(d["name"] for d in scope)}


    brain_name = "真模型" if llm.configured() else "规则大脑"
    t_all = time.perf_counter()
    tok_sum = 0
    tools_used = []


    for round_no in range(1, MAX_ROUNDS + 1):
        t0 = time.perf_counter()
        decision = None
        try:
            if llm.configured():
                for kind, payload in llm.decide_stream(history, scope):

                    if kind == "delta":
                        yield {"kind": "live", "text": payload}
                    else:
                        decision = payload
            else:
                decision = mock_llm.decide(history, scope)

        except Exception as e:
            yield {"kind": "fallback", "text": f"真模型调用失败（{e}），已降级到规则大脑继续"}
            brain_name = "规则大脑(降级)"
            decision = mock_llm.decide(history, scope)

        think_ms = int((time.perf_counter() - t0) * 1000)

        if decision.get("type") == "tool_call":
            tok_sum += ((decision.get("usage") or {}).get("prompt_tokens") or 0)
            yield {"kind": "think", "text": decision.get("thought") or "", "round": round_no,
                   "ms": think_ms, "tokens": (decision.get("usage") or {}).get("prompt_tokens")}
            name = decision.get("name", "")
            args = decision.get("args") or {}
            tools_used.append(name)
            yield {"kind": "tool_call", "name": name, "args": args, "round": round_no}


            t1 = time.perf_counter()
            try:
                result = tools.call(name, args)
            except Exception as e:
                result = {"error": str(e)}
            yield {"kind": "tool_result", "name": name, "result": result, "round": round_no,
                   "ms": int((time.perf_counter() - t1) * 1000)}

            history.append({"role": "tool", "name": name, "content": json.dumps(result, ensure_ascii=False)})
            continue

        answer = decision.get("content") or "（没有内容）"
        total_ms = int((time.perf_counter() - t_all) * 1000)
        tok_sum += ((decision.get("usage") or {}).get("prompt_tokens") or 0)
        yield {"kind": "answer", "text": answer, "round": round_no, "brain": brain_name,
               "ms": total_ms,
               "tokens": (decision.get("usage") or {}).get("prompt_tokens")}
        history.append({"role": "assistant", "content": answer})
        memory.save(session_id, history)
        _save_trace_stream(session_id, user_message, answer, round_no, tools_used, tok_sum, total_ms)
        return


    answer = f"（已经循环了 {MAX_ROUNDS} 轮还没得出结论，先停在这里。可以换个说法再试。）"
    yield {"kind": "answer", "text": answer, "round": MAX_ROUNDS, "brain": brain_name,
           "ms": int((time.perf_counter() - t_all) * 1000)}
    history.append({"role": "assistant", "content": answer})
    memory.save(session_id, history)
    _save_trace_stream(session_id, user_message, answer, MAX_ROUNDS, tools_used, tok_sum,
                       int((time.perf_counter() - t_all) * 1000))
