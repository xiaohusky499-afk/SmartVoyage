"""统一构造「不走系统代理」的 httpx 客户端。

为什么需要这个模块
------------------
本机开着系统代理（如 Clash 的 127.0.0.1:7890）时，会踩到这个坑：

    httpx.HTTPStatusError: Server error '502 Bad Gateway'
    for url 'http://127.0.0.1:8002/mcp'

现象很反直觉 —— curl 打同一个地址是 200，Python 却 502，MCP 服务端日志里
根本看不到这条请求（说明请求没到服务）。

原因：httpx 默认 trust_env=True，会读 Windows 注册表里的 ProxyServer，
但它**不实现** ProxyOverride 绕过逻辑。注册表的 ProxyOverride 里明明写了
`127.*` 和 `<local>`，IE / curl / requests 都会遵守，httpx 完全无视。
于是发往 127.0.0.1 的请求也被塞给代理，代理拒绝转发到回环地址，返回 502。

那个 502 不可能来自 MCP 服务端：mcp/server/streamable_http.py 只会返回
200/202/400/404/405/406/409/415/500，没有 502 分支。响应头里没有 `server`
字段也能佐证 —— 那是代理自己吐的错误页。

同一进程内的 A/B 实测（代理开启状态）：

    trust_env=True  且无 NO_PROXY  -> 502，响应头无 server
    trust_env=True  且设 NO_PROXY  -> 200，server: uvicorn
    trust_env=False                -> 200，server: uvicorn

排查时可以这样确认（别写成 >>> 开头，否则 PyCharm 会当 doctest 跑）：
    python -c "import urllib.request; print(urllib.request.getproxies())"
    # 有输出 {'http': 'http://127.0.0.1:7890', ...} 就说明系统代理开着

为什么用 trust_env=False 而不是依赖 NO_PROXY 环境变量：环境变量只在通过
main.py 启动时才设得上（见 main.py 顶部的兜底），单独 `python -m` 跑某个
server、或跑 tests 时就没有了。写在代码里才对启动方式免疫。
"""

from typing import Any

import httpx

__all__ = ["no_proxy_mcp_client", "no_proxy_sync_client", "no_proxy_async_client"]


def no_proxy_mcp_client(
    headers: dict[str, str] | None = None,
    timeout: httpx.Timeout | None = None,
    auth: httpx.Auth | None = None,
) -> httpx.AsyncClient:
    """给 mcp SDK 用的 httpx 客户端工厂，绕开系统代理。

    传给 streamablehttp_client(url, httpx_client_factory=no_proxy_mcp_client)。

    签名必须是 (headers, timeout, auth) 这三个参数，这是 mcp SDK 的
    McpHttpClientFactory 协议要求（mcp/shared/_httpx_utils.py），少一个会 TypeError。

    除了 trust_env=False，其余默认值与 SDK 的 create_mcp_http_client 保持一致
    （follow_redirects=True、默认 30s 超时），避免行为漂移。
    """
    kwargs: dict[str, Any] = {
        "follow_redirects": True,
        # 关键：不读环境变量和系统代理设置，直连 127.0.0.1。
        # 删掉这一行就会踩回文件头写的那个 502 坑。
        "trust_env": False,
        "timeout": httpx.Timeout(30.0) if timeout is None else timeout,
    }
    if headers is not None:
        kwargs["headers"] = headers
    if auth is not None:
        kwargs["auth"] = auth

    return httpx.AsyncClient(**kwargs)


def no_proxy_sync_client() -> httpx.Client:
    """给 ChatOpenAI 的 http_client 用，固定直连。

    dashscope 是国内服务，本来就不需要代理。实测走代理和直连都是 200，
    固定直连可以彻底摆脱代理开关状态的影响。

    另一个作用是绕开 langchain_openai 的客户端缓存：
    langchain_openai/chat_models/_client_utils.py 里的 _cached_sync_httpx_client
    带 @lru_cache，进程启动时会把当时的代理地址固化进 mounts 并缓存下来。
    代理进程之后退出了，缓存里那个客户端还指着已经死掉的端口，表现为
    "Connection error."，而且此时关掉系统代理也没用。显式传 http_client
    就不会走那个缓存。
    """
    return httpx.Client(trust_env=False)


def no_proxy_async_client() -> httpx.AsyncClient:
    """给 ChatOpenAI 的 http_async_client 用，固定直连。

    同步和异步两个都要传：langchain_openai 对同步/异步分别回落到各自的
    缓存工厂，只传一个，另一条路径仍会走系统代理。
    """
    return httpx.AsyncClient(trust_env=False)
