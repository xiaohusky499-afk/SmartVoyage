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

#实现普通方法,初始化mcp client   获取mcp的工具 ,初始化langchain的agent(llm, tools, prompt) agent_excute
#作用,用mcp_client和mcpServer简历连接,获取到tools_list 把工具列表对给大模型,让大模型选择用哪个工具,完成相应
# 的任务
async def query_tickets(conversation: str) -> dict:
    """通过 LangChain Agent + MCP Tools 执行票务查询或预定"""
    try:
        tools = await load_remote_tools(
            "12306", "variflight", "flight_fare", "bing_cn", "fetch"
        )

        prompt = ChatPromptTemplate.from_messages([
            ("system", """你是一个真实数据票务助手，只能使用远程 MCP 工具返回的信息。
你能够查询火车票、航班班次和低价机票；演唱会只通过中文搜索和网页抓取查询公开信息。
你需要仔细分析用户的问题，从中提取必要的参数（出发城市、到达城市、日期、座位类型等），然后调用对应的工具。
如果用户提供的信息不足以提取到调用工具所有必要参数，则向用户追问。不能自己编造参数。
当前日期是{current_date}。"""),
            ("human", "{input}"),
            ("placeholder", "{agent_scratchpad}"),
        ])

        agent = create_tool_calling_agent(llm, tools, prompt)
        agent_executor = AgentExecutor(agent=agent, tools=tools, verbose=False)

        current_date = datetime.now(pytz.timezone('Asia/Shanghai')).strftime('%Y-%m-%d')

        response = await agent_executor.ainvoke({
            "input": conversation,
            "current_date": current_date
        })

        return {"status": "success", "message": response["output"]}

    except ExceptionGroup as eg:
        first_exc = eg.exceptions[0] if eg.exceptions else eg
        logger.error(f"票务 MCP 查询出错：{first_exc}")
        return {"status": "error", "message": f"票务 MCP 查询出错：{first_exc}"}
    except BaseException as e:
        logger.error(f"票务 MCP 查询出错：{str(e)}")
        return {"status": "error", "message": f"票务 MCP 查询出错：{str(e)}"}

agent_card = AgentCard(
    name="TicketAssistant",
    description="基于远程 MCP 提供真实票务查询服务的统一助手",
    url="http://localhost:5006",
    version="1.0.0",
    capabilities={"streaming": True, "memory": True},
    skills=[
        AgentSkill(
            name="query train tickets",
            description="查询火车票信息",
            examples=["北京到上海的高铁 2025-10-28"]
        ),
        AgentSkill(
            name="query flight tickets",
            description="查询航班机票信息",
            examples=["上海到北京的机票 10月28日"]
        ),
        AgentSkill(
            name="query concert tickets",
            description="查询演唱会门票信息",
            examples=["刀郎北京演唱会门票"]
        ),
    ]
)

#定义tiketServer:a2aServer
class TicketQueryServer(A2AServer):
    """票务查询和预定 A2A 服务器"""

    def __init__(self):
        super().__init__(agent_card=agent_card)

    def handle_task(self, task):
        content = (task.message or {}).get("content", {})
        conversation = content.get("text", "") if isinstance(content, dict) else ""
        logger.info(f"用户查询: {conversation}")

        try:
            ticket_result = asyncio.run(query_tickets(conversation))
            logger.info(f"MCP 查询返回: {ticket_result}")

            if ticket_result.get("status") == "success":
                result_text = ticket_result.get("message", "")
                if "请提供" in result_text or "请确认" in result_text:
                    task.status = TaskStatus(
                        state=TaskState.INPUT_REQUIRED,
                        message={"role": "agent", "content": {"text": result_text}}
                    )
                else:
                    task.artifacts = [{"parts": [{"type": "text", "text": result_text}]}]
                    task.status = TaskStatus(state=TaskState.COMPLETED)

            elif ticket_result.get("status") == "error":
                task.status = TaskStatus(
                    state=TaskState.FAILED,
                    message={"role": "agent", "content": {"text": ticket_result.get("message", "查询失败，请重试。")}}
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
#启动服务
if __name__ == "__main__":
    ticket_server = TicketQueryServer()
    print("\n=== 服务器信息 ===")
    print(f"名称: {ticket_server.agent_card.name}")
    print(f"描述: {ticket_server.agent_card.description}")
    print("\n技能:")
    for skill in ticket_server.agent_card.skills:
        print(f"- {skill.name}: {skill.description}")
    run_server(ticket_server, host="127.0.0.1", port=5006)
