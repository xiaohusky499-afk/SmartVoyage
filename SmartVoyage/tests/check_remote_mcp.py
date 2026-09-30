"""Verify that every configured business MCP exposes at least one tool."""

import asyncio
import time

from SmartVoyage.remote_mcp import load_remote_tools


async def main() -> None:
    started = time.perf_counter()
    tools = await load_remote_tools(
        "12306",
        "amap",
        "variflight",
        "flight_fare",
        "bing_cn",
        "fetch",
        "viator",
        "hellosafe",
    )
    names = sorted({tool.name for tool in tools})
    if not names:
        raise RuntimeError("No remote MCP tools were loaded")
    first_elapsed = time.perf_counter() - started
    started = time.perf_counter()
    cached_tools = await load_remote_tools(
        "12306", "amap", "variflight", "flight_fare", "bing_cn", "fetch", "viator", "hellosafe"
    )
    print(f"loaded {len(names)} remote tools; first={first_elapsed:.1f}s cached={time.perf_counter() - started:.1f}s")
    assert len(cached_tools) == len(tools)
    print(", ".join(names))


if __name__ == "__main__":
    asyncio.run(main())
