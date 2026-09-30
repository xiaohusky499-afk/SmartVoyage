"""用 requests 逐个探 api_server.py 的接口，看通不通

不用测试框架，就是普通脚本，跑完打一份表格。

前置条件：
1. 先起 API 服务：python SmartVoyage/api_server.py        （8088）
2. /api/chat 那两个接口要走完整 Agent 链路，得先起三个 MCP 服务
   （8001 票务 / 8002 天气 / 8003 行程）和三个 A2A 服务（5005/5006/5007），
   而且会真花 DashScope 的 token

运行：
    cd "D:\\我的备课\\prepare_smart_voyage"
    $env:PYTHONPATH = "."
    python -X utf8 SmartVoyage/tests/check_api_server.py

只想跳过花钱的那两个 chat 接口：
    python -X utf8 SmartVoyage/tests/check_api_server.py --skip-chat
"""
import json
import sys

import requests

BASE = "http://127.0.0.1:8088"

# 每条结果是 (接口, 结论, 说明)
results = []


def record(name: str, ok: bool, detail: str):
    results.append((name, "通过" if ok else "不正常", detail))
    print(f"  {'通过' if ok else '不正常'} - {detail}")


def check_get_agents():
    """GET /api/agents —— 取三个子代理的 AgentCard"""
    name = "GET /api/agents"
    print(f"\n[{name}] 获取代理卡片信息")
    try:
        r = requests.get(f"{BASE}/api/agents", timeout=30)
    except Exception as e:
        record(name, False, f"请求失败：{type(e).__name__}: {e}")
        return

    if r.status_code != 200:
        record(name, False, f"HTTP {r.status_code}，body: {r.text[:200]}")
        return

    body = r.json()
    data = body.get("data")
    if body.get("status") != "success" or not isinstance(data, list):
        record(name, False, f"响应结构不对：{str(body)[:200]}")
        return

    # get_agent_cards 里代理没起时会标 reachable=False，不算接口坏
    reachable = [c.get("name") for c in data if c.get("reachable")]
    down = [c.get("name") or c.get("url") for c in data if not c.get("reachable")]
    detail = f"HTTP 200，返回 {len(data)} 个代理，可达 {len(reachable)} 个"
    if down:
        detail += f"（未启动：{', '.join(map(str, down))}）"
    record(name, True, detail)


def check_get_memory():
    """GET /api/memory —— 取记忆状态"""
    name = "GET /api/memory"
    print(f"\n[{name}] 获取记忆状态")
    try:
        r = requests.get(f"{BASE}/api/memory", timeout=30)
    except Exception as e:
        record(name, False, f"请求失败：{type(e).__name__}: {e}")
        return

    if r.status_code != 200:
        record(name, False, f"HTTP {r.status_code}，body: {r.text[:200]}")
        return

    body = r.json()
    if body.get("status") != "success":
        record(name, False, f"响应结构不对：{str(body)[:200]}")
        return
    record(name, True, f"HTTP 200，data 类型 {type(body.get('data')).__name__}")


def check_update_profile():
    """POST /api/memory/profile —— 更新用户偏好"""
    name = "POST /api/memory/profile"
    print(f"\n[{name}] 更新用户偏好")
    payload = {"profile": {"喜好": "纯玩无购物", "预算": "中等"}}
    try:
        r = requests.post(f"{BASE}/api/memory/profile", json=payload, timeout=30)
    except Exception as e:
        record(name, False, f"请求失败：{type(e).__name__}: {e}")
        return

    if r.status_code != 200:
        record(name, False, f"HTTP {r.status_code}，body: {r.text[:200]}")
        return

    body = r.json()
    if body.get("status") != "success":
        record(name, False, f"响应结构不对：{str(body)[:200]}")
        return
    record(name, True, f"HTTP 200，message: {body.get('message')}")

def check_chat():
    """POST /api/chat —— 走完整 Agent 链路，慢且花 token"""
    name = "POST /api/chat"
    print(f"\n[{name}] 发消息取回复（会调 LLM，较慢）")
    payload = {"message": "你好，帮我看看成都明天的天气"}
    try:
        # 意图识别 + 规划 + 子代理调用，几十秒很正常
        r = requests.post(f"{BASE}/api/chat", json=payload, timeout=180)
    except Exception as e:
        record(name, False, f"请求失败：{type(e).__name__}: {e}")
        return

    if r.status_code != 200:
        record(name, False, f"HTTP {r.status_code}，body: {r.text[:300]}")
        return

    body = r.json()
    msg = body.get("message")
    if body.get("status") != "success" or not msg:
        record(name, False, f"响应结构不对：{str(body)[:200]}")
        return
    record(name, True, f"HTTP 200，回复 {len(msg)} 字：{msg[:60]}...")


def check_chat_stream():
    """POST /api/chat/stream —— SSE 伪流式"""
    name = "POST /api/chat/stream"
    print(f"\n[{name}] SSE 流式回复（会调 LLM，较慢）")
    payload = {"message": "成都明天天气怎么样"}
    try:
        r = requests.post(f"{BASE}/api/chat/stream", json=payload,
                          timeout=180, stream=True)
    except Exception as e:
        record(name, False, f"请求失败：{type(e).__name__}: {e}")
        return

    if r.status_code != 200:
        record(name, False, f"HTTP {r.status_code}，body: {r.text[:300]}")
        return

    ctype = r.headers.get("content-type", "")
    chunks, err, done = [], None, False
    for line in r.iter_lines(decode_unicode=True):
        if not line or not line.startswith("data: "):
            continue
        raw = line[len("data: "):]
        if raw == "[DONE]":
            done = True
            break
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            err = f"data 不是合法 JSON：{raw[:80]}"
            break
        # sse_generator 出错时下发的是 {"error": ...}
        if "error" in obj:
            err = f"服务端报错：{obj['error'][:200]}"
            break
        chunks.append(obj.get("content", ""))

    if err:
        record(name, False, err)
        return
    if not done:
        record(name, False, f"没收到 [DONE] 结束标记，只拿到 {len(chunks)} 个片段")
        return

    text = "".join(chunks)
    if not text:
        record(name, False, "收到 [DONE] 但内容是空的")
        return
    record(name, True,
           f"HTTP 200，content-type={ctype.split(';')[0]}，"
           f"{len(chunks)} 个片段共 {len(text)} 字：{text[:50]}...")


def main():
    skip_chat = "--skip-chat" in sys.argv

    print("=" * 70)
    print(f"探测 api_server.py 的接口：{BASE}")
    if skip_chat:
        print("（--skip-chat：跳过两个 chat 接口，不花 token）")
    print("=" * 70)

    # 先确认服务在不在，省得后面每个接口都报一遍连接失败
    try:
        requests.get(f"{BASE}/docs", timeout=10)
    except Exception as e:
        print(f"\n连不上 {BASE}：{type(e).__name__}: {e}")
        print("请先启动：python SmartVoyage/api_server.py")
        return 1

    # 不花钱的三个先跑
    check_get_agents()
    check_get_memory()
    check_update_profile()

    if skip_chat:
        print("\n[POST /api/chat]、[POST /api/chat/stream] 已跳过")
    else:
        check_chat()
        check_chat_stream()

    # 汇总
    print("\n" + "=" * 70)
    print("汇总")
    print("=" * 70)
    width = max(len(n) for n, _, _ in results)
    for name, verdict, detail in results:
        print(f"{name.ljust(width)}  {verdict}  {detail}")

    bad = [n for n, v, _ in results if v == "不正常"]
    print("-" * 70)
    print(f"共 {len(results)} 个接口，通过 {len(results) - len(bad)} 个，"
          f"不正常 {len(bad)} 个")
    if bad:
        print("不正常的：" + "、".join(bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
# 脚本用法：
#
#   python -X utf8 SmartVoyage/tests/check_api_server.py              # 全部 5 个
#   python -X utf8 SmartVoyage/tests/check_api_server.py --skip-chat   # 跳过花 token 的两个