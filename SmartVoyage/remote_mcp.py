"""Remote MCP connections used by the production SmartVoyage agents."""

import asyncio
import logging
import os
from urllib.parse import quote

from dotenv import load_dotenv
from langchain_mcp_adapters.client import MultiServerMCPClient

from SmartVoyage.config import project_root
from SmartVoyage.utils.http_client import no_proxy_mcp_client

load_dotenv(os.path.join(project_root, ".env"))

# httpx INFO 日志会把高德 query key 和远程 MCP URL 打到日志中。
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("mcp.client.streamable_http").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# 只缓存远程工具定义和客户端连接；景点、票价、天气、保险报价等业务结果仍每次实时查询。
_tool_cache = {}
_client_cache = {}


def _amap_url() -> str:
    configured = os.getenv("MCP_AMAP_URL")
    if configured:
        return configured
    key = os.getenv("AMAP_MAPS_API_KEY")
    if not key:
        raise RuntimeError("缺少 AMAP_MAPS_API_KEY")
    return f"https://mcp.amap.com/mcp?key={quote(key, safe='')}"


def _connections() -> dict:
    return {
        "12306": {
            "transport": "streamable_http",
            "url": os.getenv(
                "MCP_12306_URL",
                "https://mcp.api-inference.modelscope.net/6b91a45c582e42/mcp",
            ),
        },
        "amap": {
            "transport": "streamable_http",
            "url": _amap_url(),
        },
        "variflight": {
            "transport": "streamable_http",
            "url": os.getenv(
                "MCP_VARIFLIGHT_URL",
                "https://ai.variflight.com/servers/aviation/mcp",
            ),
            "headers": {"X-API-Key": os.getenv("VARIFLIGHT_API_KEY", "")},
        },
        "flight_fare": {
            "transport": "streamable_http",
            "url": os.getenv(
                "MCP_FLIGHT_FARE_URL",
                "https://mcp.api-inference.modelscope.net/3bec36b405f543/mcp",
            ),
        },
        "bing_cn": {
            "transport": "streamable_http",
            "url": os.getenv(
                "MCP_BING_CN_URL",
                "https://mcp.api-inference.modelscope.net/a864048ef20345/mcp",
            ),
        },
        "fetch": {
            "transport": "streamable_http",
            "url": os.getenv(
                "MCP_FETCH_URL",
                "https://mcp.api-inference.modelscope.net/a5cc00db7dea46/mcp",
            ),
        },
        "viator": {
            "transport": "streamable_http",
            "url": os.getenv(
                "MCP_VIATOR_URL",
                "https://exp-app-mcp.prod.ep.viator.com/mcp",
            ),
        },
        "hellosafe": {
            "transport": "streamable_http",
            "url": os.getenv("HELLOSAFE_MCP_URL", ""),
            "headers": {
                "Authorization": f"Bearer {os.getenv('HELLOSAFE_MCP_TOKEN', '')}"
            },
        },
    }


def create_remote_mcp_client(*server_names: str) -> MultiServerMCPClient:
    """Create a client containing only the remote tools needed by one agent."""
    connections = _connections()
    selected = {}
    for name in server_names:
        if name not in connections:
            raise ValueError(f"未知远程 MCP: {name}")
        if name == "hellosafe" and not connections[name]["url"]:
            raise RuntimeError("缺少 HELLOSAFE_MCP_URL")
        selected[name] = dict(connections[name])
        if name == "viator":
            selected[name]["httpx_client_factory"] = no_proxy_mcp_client
    return MultiServerMCPClient(selected)


async def load_remote_tools(*server_names: str):
    """Discover tools from remote MCP servers for an A2A request."""
    if not server_names:
        return []

    # A2A 的 Flask 适配层会为每个任务创建新的 asyncio 事件循环。
    # 不能用 id(loop) 作缓存键：关闭后的 loop id 可能被新 loop 复用。
    loop = asyncio.get_running_loop()
    for closed_loop in list(_tool_cache):
        if closed_loop.is_closed():
            _tool_cache.pop(closed_loop, None)
            _client_cache.pop(closed_loop, None)
    loop_tools = _tool_cache.setdefault(loop, {})
    loop_clients = _client_cache.setdefault(loop, {})

    tools = []
    failures = []
    for name in server_names:
        if name in loop_tools:
            tools.extend(loop_tools[name])
            continue
        last_error = None
        for attempt in range(2):
            try:
                # 逐个发现，避免一个不稳定的上游让整组 MCP 一起失败。
                client = create_remote_mcp_client(name)
                discovered = await client.get_tools()
                loop_clients[name] = client
                loop_tools[name] = discovered
                tools.extend(discovered)
                last_error = None
                break
            except Exception as exc:
                last_error = exc
                if attempt == 0:
                    await asyncio.sleep(1)
        if last_error is not None:
            failures.append(f"{name}: {type(last_error).__name__}")

    if failures:
        logger.warning("部分远程 MCP 暂时不可用：%s", ", ".join(failures))
    if not tools:
        raise RuntimeError("所有远程 MCP 均暂时不可用")
    return tools
