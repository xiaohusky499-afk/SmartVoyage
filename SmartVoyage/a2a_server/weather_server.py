import json
import asyncio

from python_a2a import A2AServer, run_server, AgentCard, AgentSkill, TaskStatus, TaskState

from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
from langchain.agents import create_tool_calling_agent, AgentExecutor

from SmartVoyage.config import Config
from SmartVoyage.create_logger import logger
from SmartVoyage.utils.http_client import (
    no_proxy_async_client,
    no_proxy_mcp_client,
    no_proxy_sync_client,
)
from SmartVoyage.remote_mcp import load_remote_tools
from datetime import datetime
import pytz

conf = Config()

# 初始化LLM
# http_client / http_async_client 固定直连，不读系统代理（详见 utils/http_client.py）
llm = ChatOpenAI(
    model=conf.model_name,
    base_url=conf.base_url,
    api_key=conf.api_key,
    temperature=conf.temperature,
    http_client=no_proxy_sync_client(),
    http_async_client=no_proxy_async_client()
)

async def query_weather(conversation: str) -> dict:
    """通过 LangChain Agent + MCP Tools 执行天气查询"""
    try:
        tools = await load_remote_tools("amap")

        # 定义 Agent 的系统 Prompt
        prompt = ChatPromptTemplate.from_messages([
            ("system", """你是一个高德真实数据天气助手，能够调用天气查询工具。
你需要仔细分析用户的问题，从中提取城市和时间信息，然后调用天气查询工具。
如果用户提供的信息不足以查询天气（如缺少城市或日期），则向用户追问。不能自己编造参数。
当前日期是{current_date}。"""),
            ("human", "{input}"),
            ("placeholder", "{agent_scratchpad}"),
        ])

        # 创建基于工具调用的 Agent
        agent = create_tool_calling_agent(llm, tools, prompt)

        # 创建 Agent 执行器
        agent_executor = AgentExecutor(agent=agent, tools=tools, verbose=False)

        # 获取当前日期，注入到 Prompt 中
        current_date = datetime.now(pytz.timezone('Asia/Shanghai')).strftime('%Y-%m-%d')

        # 执行 Agent
        response = await agent_executor.ainvoke({
            "input": conversation,
            "current_date": current_date
        })

        return {"status": "success", "message": response["output"]}

    except ExceptionGroup as eg:
        first_exc = eg.exceptions[0] if eg.exceptions else eg
        logger.error(f"天气 MCP 查询出错：{first_exc}")
        return {"status": "error", "message": f"天气 MCP 查询出错：{first_exc}"}
    except BaseException as e:
        logger.error(f"天气 MCP 查询出错：{str(e)}")
        return {"status": "error", "message": f"天气 MCP 查询出错：{str(e)}"}

# agentserver的简历, 这里的AgentCard 和 AgentSkill 都是一个描述信息,没有具体占用端口,启动服务
agent_card = AgentCard(
    name="WeatherQueryAssistant",
    description="基于LangChain提供天气查询服务的助手",
    url="http://localhost:5005",
    version="1.0.0",
    capabilities={"streaming": True, "memory": True},
    skills=[
        AgentSkill(
            name="query weather",
            description="查询天气数据，支持自然语言输入",
            examples=["北京 2025-07-30 天气", "上海未来3天天气如何", "明天天气怎么样"]
        )
    ]
)


class WeatherQueryServer(A2AServer):
    """天气查询 A2A 服务器"""

    def __init__(self):
        super().__init__(agent_card=agent_card)

    def handle_task(self, task):
        # 提取输入
        content = (task.message or {}).get("content", {})
        conversation = content.get("text", "") if isinstance(content, dict) else ""
        logger.info(f"用户查询: {conversation}")

        try:
            # 调用 MCP 查询
            weather_result = asyncio.run(query_weather(conversation))
            logger.info(f"MCP 查询返回: {weather_result}")

            # 根据结果设置任务状态
            if weather_result.get("status") == "success":
                result_text = weather_result.get("message", "")

                # 检查是否是追问消息
                if "请提供" in result_text or "请确认" in result_text:
                    task.status = TaskStatus(
                        state=TaskState.INPUT_REQUIRED,
                        message={"role": "agent", "content": {"text": result_text}}
                    )
                else:
                    # 结果放到产物
                    task.artifacts = [{"parts": [{"type": "text", "text": result_text}]}]
                    task.status = TaskStatus(state=TaskState.COMPLETED)

            elif weather_result.get("status") == "error":
                task.status = TaskStatus(
                    state=TaskState.FAILED,
                    message={"role": "agent", "content": {"text": weather_result.get("message", "查询失败，请重试。")}}
                )
            else:
                task.status = TaskStatus(
                    state=TaskState.FAILED,
                    message={"role": "agent", "content": {"text": "查询失败，请重试或提供更多细节。"}}
                )

            return task

        except Exception as e:
            logger.error(f"查询失败: {str(e)}")
            task.status = TaskStatus(
                state=TaskState.FAILED,
                message={"role": "agent", "content": {"text": f"查询失败: {str(e)} 请重试。"}}
            )
            return task

if __name__ == "__main__":
    weather_server = WeatherQueryServer()
    print("\n=== 服务器信息 ===")
    print(f"名称: {weather_server.agent_card.name}")
    print(f"描述: {weather_server.agent_card.description}")
    print("\n技能:")
    for skill in weather_server.agent_card.skills:
        print(f"- {skill.name}: {skill.description}")
    run_server(weather_server, host="127.0.0.1", port=5005)
