import asyncio
import json
import re
import uuid
from datetime import datetime

import mysql
import pytz
from langchain_openai import ChatOpenAI
from python_a2a import AgentNetwork, A2AClient, Message, TextContent, MessageRole, Task

from SmartVoyage.a2a_server.ticket_server import conf
from SmartVoyage.create_logger import logger
from SmartVoyage.main_prompts import SmartVoyagePrompts
from SmartVoyage.memory import ConversationMemory
from SmartVoyage.utils.http_client import no_proxy_async_client, no_proxy_sync_client


class ChatService:
    def __init__(self):
        # 配置 A2A 代理网络
        self.agent_urls = {
            "WeatherQueryAssistant": "http://localhost:5005",
            "TicketAssistant": "http://localhost:5006",
            "TripAssistant": "http://localhost:5007"
        }
        self.agent_network = AgentNetwork(name="旅行助手网络")
        # 注册子代理，使用自定义超时（120秒）
        self.agent_network.add("WeatherQueryAssistant", A2AClient("http://localhost:5005", timeout=120))
        self.agent_network.add("TicketAssistant", A2AClient("http://localhost:5006", timeout=120))
        self.agent_network.add("TripAssistant", A2AClient("http://localhost:5007", timeout=120))

        # 初始化大模型连接
        # http_client / http_async_client 固定直连，不读系统代理。
        # 这里还顺带绕开了 langchain_openai 的 @lru_cache 客户端缓存 —— 否则进程启动时
        # 会把当时的代理地址固化下来，代理退出后表现为 "Connection error."
        # 详见 utils/http_client.py
        self.llm = ChatOpenAI(
            model=conf.model_name,
            api_key=conf.api_key,
            base_url=conf.base_url,
            temperature=0.1,
            http_client=no_proxy_sync_client(),
            http_async_client=no_proxy_async_client()
        )

        # 初始化记忆管理器
        self.memory = ConversationMemory(short_term_limit=10)
        self.messages = []
        self.conversation_history = ""
        self._available_tools_text = ""
        # API 流式接口使用它把有限的执行阶段推送给前端，不暴露模型思维过程或原始工具数据。
        self.progress_callback = None

        # 初始化数据库连接，加载持久化记忆
        self._init_db_and_load_memory()

    #从数据库中加载记忆
    def _init_db_and_load_memory(self):
        """初始化数据库连接，并从数据库加载持久化记忆"""
        try:
            db_conn = mysql.connector.connect(
                host=conf.host, user=conf.user,
                password=conf.password, database=conf.database
            )
            self.memory.set_db_connection(db_conn)

            # 从数据库恢复记忆数据
            self.memory.load_profile_from_db()  #用户偏好
            self.memory.load_entities_from_db() #历史实体
            self.memory.load_messages_from_db() #短期记忆

            # 同步短期消息到 messages 列表
            for msg in self.memory.short_term_messages:
                self.messages.append({"role": msg["role"], "content": msg["content"]})
        except Exception as e:
            logger.warning(f"数据库连接初始化失败: {e}")

    #意图识别模块
    def _emit_progress(self, stage: str, message: str):
        callback = self.progress_callback
        if callback:
            callback({"stage": stage, "message": message})

    def intent_agent(self, user_input: str):
        """分析用户输入，判断用户想做什么"""
        chain = SmartVoyagePrompts.intent_prompt() | self.llm

        current_date = datetime.now(pytz.timezone('Asia/Shanghai')).strftime('%Y-%m-%d')

        intent_response = chain.invoke({
            "conversation_history": self.memory.get_short_term_text(),
            "query": user_input,
            "current_date": current_date,
            "user_profile": self.memory.get_profile_text(),
            "task_context": json.dumps(self.memory.current_task, ensure_ascii=False)
        }).content.strip()

        # 清理可能的 ```json 代码块标记
        intent_response = re.sub(r'^```json\s*|\s*```$', '', intent_response).strip()
        intent_output = json.loads(intent_response)

        intents = intent_output.get("intents", [])
        user_queries = intent_output.get("user_queries", {})
        follow_up_message = intent_output.get("follow_up_message", "")
        # 复合旅行需求默认补充景点查询，避免模型只识别出票务/保险而漏掉目的地推荐。
        travel_request = re.search(r"(去|到|前往).{0,12}(旅游|旅行|游玩|旅游团)|旅游团", user_input)
        if travel_request and "attraction" not in intents and "out_of_scope" not in intents:
            intents.append("attraction")
            user_queries.setdefault(
                "attraction",
                f"请基于以下行程目的地推荐真实景点，至少列出3个景点名称、地址和推荐理由：{user_input}",
            )
        # 对交通预订和租车等高价值服务做确定性兜底，避免意图模型只返回 order 或漏掉 car_rental。
        if re.search(r"(订|预订|购买|买).{0,5}(火车票|高铁票|动车票)|火车票|高铁票|动车票", user_input):
            if "train" not in intents:
                intents.append("train")
            user_queries.setdefault("train", f"查询{user_input}中的真实火车/高铁班次和票价")
        if re.search(r"(租车|租一辆车|自驾|车辆)", user_input) and "car_rental" not in intents:
            intents.append("car_rental")
            user_queries.setdefault("car_rental", f"查询{user_input}中的北京租车服务、门店、车型和价格信息")
        # 只有明确询问天气时才调用天气服务，不能因为出现出行日期就臆测天气需求。
        if "weather" in intents and not re.search(
            r"天气|气温|温度|下雨|降雨|晴|阴|多云|风力|预报", user_input
        ):
            intents.remove("weather")
            user_queries.pop("weather", None)
        #意图[是一个列表]  改写后的用户query字典{意图:改写后的query,意图2:改写后的query}, 追问内容:缺少一些内容,大模型的追问
        return intents, user_queries, follow_up_message

    #是否需要跳过任务编排, 1.独立意图, 2.是查询类意图 都可以跳过编排
    def _should_skip_planning(self, intents: list) -> bool:
        """判断是否可以跳过 planning_agent，直接执行"""
        # 规则1：单意图直接跳过
        if len(intents) <= 1:
            return True

        # 规则2：多意图但全部为独立查询类，无需分步规划
        independent_intents = {"weather", "flight", "train", "concert", "attraction",
                               "car_rental", "tour_group", "insurance", "trip_order"}
        for intent in intents:
            if intent not in independent_intents:
                return False
        return True

    async def react_loop(self, steps: list, user_queries: dict) -> str:
        """
        ReAct 循环：按规划步骤逐步执行，直接行动（Action），跳过 Thought 推理

        执行流程：
        1. 按依赖关系分组步骤
        2. 无依赖的步骤并行执行
        3. 记录每步结果（Observation）
        4. 最终汇总生成连贯回复

        优化：省略 Thought LLM 调用（plan 已确定动作，Thought 无额外决策价值），
        省去每步 1 次 LLM 调用，多步骤场景下显著减少响应时间。

        参数：
            steps (list): 计划步骤列表，如：
                [{"step": 1, "action": "...", "intent": "flight", "depends_on": 0},
                 {"step": 2, "action": "...", "intent": "weather", "depends_on": 0}]
            user_queries (dict): 改写后的查询字典

        返回值：
            str: 综合所有步骤结果的最终回复
        """
        observations = []  # 存储每个步骤的执行结果（Observation）
        step_results = []  # 存储每个步骤的详细结果（步骤号、描述、结果）

        # 按依赖关系分组：depends_on 值相同的步骤可以并行
        from collections import OrderedDict
        dep_groups = OrderedDict()
        for step in steps:
            dep = step.get("depends_on", 0)
            dep_groups.setdefault(dep, []).append(step)

        logger.info(
            f"ReAct 开始: 步骤数={len(steps)}, 分组={len(dep_groups)}, "
            f"计划={json.dumps([s.get('intent', '?') for s in steps], ensure_ascii=False)}"
        )

        # 逐组执行，组内并行
        for dep_key, group_steps in dep_groups.items():
            self._emit_progress(
                "plan",
                f"正在执行第 {min(s.get('step', 0) for s in group_steps)} 组任务（{len(group_steps)} 项）",
            )
            # 组内步骤并行执行 Action
            if len(group_steps) > 1:
                logger.info(f"并行执行 {len(group_steps)} 个无依赖步骤 (depends_on={dep_key})")
                tasks = [self.execute_step(s, user_queries) for s in group_steps]
                #并行
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for cur_step, result in zip(group_steps, results):
                    step_num = cur_step.get("step", 0)
                    step_desc = cur_step.get("description", cur_step.get("action", ""))
                    if isinstance(result, Exception):
                        result = f"执行失败：{result}"
                    observations.append(result)
                    step_results.append({"step": step_num, "description": step_desc, "result": result})
                    logger.info(f"ReAct 步骤 {step_num} 结果: {result[:100]}...")
            else:
                # 单步骤，直接执行
                cur_step = group_steps[0]
                step_num = cur_step.get("step", 0)
                step_desc = cur_step.get("description", cur_step.get("action", ""))
                result = await self.execute_step(cur_step, user_queries)
                observations.append(result)
                step_results.append({"step": step_num, "description": step_desc, "result": result})
                logger.info(f"ReAct 步骤 {step_num} 结果: {result[:100]}...")

        # 无步骤可执行
        if not observations:
            return "抱歉，没有可执行的查询步骤，请换个说法再试。"

        # 只有一个结果时无需汇总，省一次大模型调用
        if len(observations) == 1:
            return observations[0]

        # 多步骤：汇总成一条连贯回复
        all_observations = "\n\n".join(
            f"步骤{r['step']}（{r['description']}）: {r['result']}" for r in step_results
        )
        user_query = self.messages[-1]["content"] if self.messages else ""

        try:
            chain = SmartVoyagePrompts.react_summary_prompt() | self.llm
            return chain.invoke({
                "query": user_query,
                "all_observations": all_observations
            }).content.strip()
        except Exception as e:
            # 汇总失败则退回拼接原始结果，保证用户至少能看到查询内容
            logger.warning(f"ReAct 结果汇总失败，返回原始结果: {e}")
            return "\n\n".join(observations)

    #执行每一步任务
    async def execute_step(self, step: dict, user_queries: dict) -> str:
        """
        执行 ReAct 计划中的单个步骤（Action）。

        参数：
            step (dict): 单个计划步骤，如
                {"step": 1, "action": "查询上海机票", "intent": "flight", "depends_on": 0}
            user_queries (dict): 意图识别改写后的查询字典

        返回值：
            str: 该步骤的执行结果（Observation）
        """
        intent = step.get("intent", "")
        step_desc = step.get("description", step.get("action", ""))

        try:
            if intent:
                # 优先用改写后的查询，缺失则退回步骤描述
                query_str = user_queries.get(intent) or step_desc
                return await self._call_agent_intent(intent, query_str)

            # 步骤没标注意图：交给大模型按景点推荐逻辑生成
            logger.warning(f"步骤 {step.get('step', 0)} 缺少 intent，退回大模型直接回答")
            return await self._call_agent_intent("attraction", step_desc)
        except Exception as e:
            logger.error(f"步骤 {step.get('step', 0)} 执行失败: {e}")
            return f"步骤执行失败：{e}"

    async def _call_agent_intent(self, intent: str, query_str: str) -> str:
        """统一调用 agent 的底层逻辑"""
        agent_name = conf.intent.get(intent)

        # 所有业务查询都必须经过远程 MCP 子代理。
        if agent_name:
            # 提取关键实体到记忆
            if intent in ["flight", "train", "concert", "car_rental", "tour_group", "insurance"]:
                self.memory.extract_entities(intent, query_str)
                self.memory.update_task_context({"type": intent, "query": query_str})

            agent = self.agent_network.get_agent(agent_name)
            labels = {
                "weather": "天气",
                "train": "火车票",
                "flight": "航班",
                "attraction": "景点",
                "tour_group": "旅游团",
                "insurance": "旅行保险",
                "concert": "演唱会",
                "car_rental": "租车",
            }
            self._emit_progress("query", f"正在查询{labels.get(intent, intent)}真实服务")

            # 构建消息并发送任务
            chat_history = (
                "历史对话仅用于补充缺失条件，不要重复执行旧问题。\n"
                + self.memory.get_short_term_text()
                + f"\n\n当前子任务类型：{intent}\n"
                + "当前请求（必须优先处理，只回答这个子任务，不要回答其他服务）：\n"
                + query_str
            )
            msg = Message(content=TextContent(text=chat_history), role=MessageRole.USER)
            task = Task(id="task-" + str(uuid.uuid4()), message=msg.to_dict())

            raw_response = await asyncio.get_event_loop().run_in_executor(
                None, lambda: asyncio.run(agent.send_task_async(task))
            )

            # 解析响应
            if raw_response.status.state == 'completed' and raw_response.artifacts:
                agent_result = raw_response.artifacts[0]['parts'][0]['text']
            else:
                agent_result = raw_response.status.message.get('content', {}).get('text', '查询失败')
            self._emit_progress("query", f"{labels.get(intent, intent)}服务已返回结果，正在整理")

            # 根据代理类型总结结果
            if agent_name == "WeatherQueryAssistant":
                chain = SmartVoyagePrompts.summarize_weather_prompt() | self.llm
                return chain.invoke({"query": query_str, "raw_response": agent_result}).content.strip()
            elif agent_name in ("TicketAssistant", "TicketQueryAssistant"):
                chain = SmartVoyagePrompts.summarize_ticket_prompt() | self.llm
                return chain.invoke({"query": query_str, "raw_response": agent_result}).content.strip()
            elif agent_name == "TripAssistant":
                chain = SmartVoyagePrompts.summarize_trip_prompt() | self.llm
                return chain.invoke({
                    "intent": intent,
                    "query": query_str,
                    "raw_response": agent_result,
                }).content.strip()
            else:
                return agent_result

        else:
            return "暂不支持此意图。"

    async def chat(self, user_input: str) -> str:
        """处理用户输入的主方法"""
        # 保存到记忆
        self.memory.add_message("user", user_input)
        self.messages.append({"role": "user", "content": user_input})

        try:
            self._emit_progress("intent", "正在识别需求和出行条件")
            # 意图识别
            intents, user_queries, follow_up_message = self.intent_agent(user_input)
            self._emit_progress("intent", f"已识别：{'、'.join(intents) if intents else '待补充信息'}")

            # 处理特殊情况
            if "out_of_scope" in intents:
                response = follow_up_message
            elif follow_up_message != "" and not any(
                intent in intents for intent in ("weather", "flight", "train", "concert", "attraction", "tour_group", "insurance", "car_rental")
            ):
                response = follow_up_message
            else:
                # 当前没有真实下单接口：如果同一请求已经包含 train/flight 查询，
                # 跳过 order，先返回可核验的班次，并明确告知不能代下单。
                execution_intents = list(intents)
                if "order" in execution_intents and any(
                    intent in execution_intents for intent in ("train", "flight", "concert")
                ):
                    execution_intents.remove("order")
                # 任务规划：启发式判断
                if self._should_skip_planning(execution_intents):
                    plan = {"need_plan": False, "reason": "任务简单，可直接执行", "steps": []}
                else:
                    self._emit_progress("plan", "正在生成旅行计划")
                    plan = _call_planning(self, execution_intents, user_queries)

                if plan.get("need_plan"):
                    # 复杂任务：进入 ReAct 循环
                    response = await self.react_loop(plan.get("steps", []), user_queries)
                else:
                    # 简单任务：直接执行
                    async def run_intent(intent):
                        # 改写缺失时退回原始输入，不能传 {}（会把 dict 塞进 prompt）
                        query_str = user_queries.get(intent) or user_input
                        return await self._call_agent_intent(intent, query_str)

                    # 这些查询彼此没有数据依赖，必须并行执行，避免一次旅行咨询串行等待数分钟。
                    results = await asyncio.gather(
                        *(run_intent(intent) for intent in execution_intents),
                        return_exceptions=True,
                    )
                    intent_results = [
                        {
                            "intent": intent,
                            "result": f"查询失败：{result}" if isinstance(result, Exception) else result,
                        }
                        for intent, result in zip(execution_intents, results)
                    ]
                    responses = [item["result"] for item in intent_results]

                    # 多服务旅行请求不能只并列展示结果，必须再经过一次专业的按天行程编排。
                    needs_itinerary = (
                        "attraction" in execution_intents
                        and len(set(execution_intents) & {
                            "train", "flight", "car_rental", "tour_group", "insurance", "attraction"
                        }) >= 2
                    ) or bool(re.search(r"每天|按天|行程|计划安排|几天|旅游攻略", user_input))
                    if needs_itinerary:
                        self._emit_progress("summary", "正在把真实查询结果整理成每日行程")
                        # 汇总只接收各服务的有限上下文，避免远程 MCP 原始长结果导致模型连接被关闭。
                        service_results = "\n\n".join(
                            f"【{item['intent']}】\n{item['result'][:7000]}" for item in intent_results
                        )
                        try:
                            chain = SmartVoyagePrompts.trip_plan_summary_prompt() | self.llm
                            response = chain.invoke({
                                "query": user_input,
                                "intents": json.dumps(execution_intents, ensure_ascii=False),
                                "service_results": service_results,
                            }).content.strip()
                        except Exception as e:
                            logger.warning(f"按天行程汇总失败，尝试精简上下文重试: {e}")
                            try:
                                compact_results = "\n\n".join(
                                    f"【{item['intent']}】\n{item['result'][:2500]}" for item in intent_results
                                )
                                response = chain.invoke({
                                    "query": user_input,
                                    "intents": json.dumps(execution_intents, ensure_ascii=False),
                                    "service_results": compact_results,
                                }).content.strip()
                            except Exception as retry_error:
                                logger.warning(f"精简上下文汇总仍失败，返回原始结果: {retry_error}")
                                response = "\n\n".join(responses)
                    else:
                        response = "\n\n".join(responses)

                if "order" in intents and "order" not in execution_intents:
                    response += "\n\n说明：当前系统可以查询真实班次，但没有接入实名购票和支付接口，暂不能代为下单。"
                if follow_up_message:
                    response += f"\n\n补充信息：{follow_up_message}"

            # 保存回复到记忆
            self.memory.add_message("assistant", response)
            self.messages.append({"role": "assistant", "content": response})
            return response

        except Exception as e:
            return f"处理失败：{str(e)}。请重试。"


    # 获取 agent card
    def get_agent_cards(self) -> list:
        """
        获取所有子代理的 AgentCard 信息，供 CLI 的 cards 命令和 /api/agents 使用。

        某个代理未启动时不抛异常，标记 reachable=False 返回。
        """
        cards = []
        for name, url in self.agent_urls.items():
            try:
                card = self.agent_network.get_agent_card(name)
                cards.append({
                    "name": getattr(card, "name", name),
                    "description": getattr(card, "description", ""),
                    "url": getattr(card, "url", url),
                    "version": getattr(card, "version", ""),
                    "reachable": True,
                    "skills": [
                        {
                            "name": s.name,
                            "description": s.description,
                            "examples": list(getattr(s, "examples", []) or []),
                        }
                        for s in (getattr(card, "skills", []) or [])
                    ],
                })
            except Exception as e:
                logger.warning(f"获取代理卡片失败 {name} ({url}): {e}")
                cards.append({
                    "name": name, "description": "", "url": url, "version": "",
                    "reachable": False, "skills": [], "error": str(e),
                })
        return cards

    def get_memory_state(self) -> dict:
        """获取当前记忆状态快照，供 CLI 的 memory 命令和 /api/memory 使用"""
        return {
            "short_term_messages": self.memory.short_term_messages,
            "user_profile": self.memory.user_profile,
            "current_task": self.memory.current_task,
            "entity_history": self.memory.entity_history,
        }

    def update_user_profile(self, profile: dict):
        """更新用户偏好（会同步持久化到数据库）"""
        self.memory.update_profile(profile)

    def clear_memory(self):
        """
        清空全部记忆，供 /api/memory/clear 使用。

        memory.clear() 会连数据库一起清，这里额外清掉 self.messages —— 它是
        ChatService 自己维护的一份副本，不清会导致下一轮对话又把旧上下文带进 prompt。
        """
        self.memory.clear()
        self.messages = []

# ==================== 本地调试入口 ====================

def test_init():
    """测试 __init__ 时有没有把数据库里的记忆加载进来"""
    print("=" * 60)
    print("测试 1: ChatService 初始化与记忆加载")
    print("=" * 60)

    service = ChatService()

    # 用例1：数据库连接是否注入成功（注入失败会被 except 吞掉，只打 warning）
    print("\n[用例1] 数据库连接是否注入")
    injected = service.memory._db_conn is not None
    print(f"  memory._db_conn is not None -> {injected}")
    if not injected:
        print("  数据库连接失败，后面的加载用例都会是空的（检查 MySQL 是否启动）")

    # 用例2：短期对话是否从 short_term_messages 表还原
    print("\n[用例2] 短期对话 short_term_messages")
    print(f"  条数: {len(service.memory.short_term_messages)}")
    for msg in service.memory.short_term_messages:
        print(f"    {msg['role']:9s} [{msg['timestamp']}] {msg['content']}")

    # 用例3：用户偏好是否从 user_profiles 表还原
    print("\n[用例3] 用户偏好 user_profile")
    print(f"  字典: {service.memory.user_profile}")
    print(f"  get_profile_text() -> {service.memory.get_profile_text()}")

    # 用例4：历史实体是否从 query_history 表还原
    print("\n[用例4] 历史实体 entity_history")
    print(f"  条数: {len(service.memory.entity_history)}")
    for ent in service.memory.entity_history[-5:]:
        print(f"    {ent['type']:12s} [{ent['timestamp']}] {ent['query']}")

    # 用例5：messages 列表是否和短期记忆同步（__init__ 里那个 for 循环）
    print("\n[用例5] messages 与 short_term_messages 是否同步")
    print(f"  len(messages)={len(service.messages)}, len(short_term_messages)={len(service.memory.short_term_messages)}")
    print(f"  数量一致 -> {len(service.messages) == len(service.memory.short_term_messages)}")

    return service


def test_intent_agent():
    """测试 intent_agent 的意图识别与查询改写（真实调用大模型）"""
    print("=" * 60)
    print("测试 2: intent_agent 意图识别")
    print("=" * 60)

    service = ChatService()

    cases = [
        ("单意图-天气", "明天深圳天气怎么样"),
        ("单意图-高铁", "帮我查下明天深圳到广州的高铁"),
        ("多意图-天气+机票", "下周去上海玩，看看天气和机票"),
        ("订票意图-order", "帮我把这趟高铁订了"),
        ("超出范围-out_of_scope", "帮我写一段快速排序的代码"),
        ("信息不全-应该触发追问", "我想订票"),
        ("依赖历史改写-简短输入", "成都"),
    ]

    for name, query in cases:
        print(f"\n[{name}] 输入: {query}")
        try:
            intents, user_queries, follow_up = service.intent_agent(query)
            print(f"  intents        : {intents}")
            print(f"  user_queries   : {json.dumps(user_queries, ensure_ascii=False)}")
            print(f"  follow_up      : {follow_up or '(无追问)'}")
            print(f"  跳过编排?      : {service._should_skip_planning(intents)}")
        except json.JSONDecodeError as e:
            # 大模型偶尔会返回非纯 JSON，这里单独标记出来
            print(f"  JSON 解析失败: {e}")
        except Exception as e:
            print(f"  调用失败: {type(e).__name__}: {e}")


def test_should_skip_planning():
    """测试 _should_skip_planning 的分支判断（纯内存逻辑，不调用大模型）"""
    print("=" * 60)
    print("测试 3: _should_skip_planning 判断逻辑")
    print("=" * 60)

    service = ChatService()

    # (意图列表, 期望结果, 说明)
    cases = [
        ([],                              True,  "空列表：len<=1 命中规则1"),
        (["weather"],                     True,  "单意图：命中规则1直接跳过"),
        (["out_of_scope"],                True,  "单意图但超范围：仍命中规则1（先判长度）"),
        (["complex"],                     True,  "单意图 complex：同上，长度优先"),
        (["weather", "flight"],           True,  "多意图且全为独立查询类"),
        (["train", "concert", "insurance"], True, "三个独立查询类"),
        (["weather", "order"],            False, "含 order 非独立意图，需要编排"),
        (["weather", "complex"],          False, "含 complex，需要编排"),
        (["flight", "out_of_scope"],      False, "含 out_of_scope，不在白名单"),
        (["order", "trip_order"],         False, "order 不在白名单，trip_order 在"),
    ]

    passed = failed = 0
    for intents, expected, desc in cases:
        actual = service._should_skip_planning(intents)
        ok = actual == expected
        passed += ok
        failed += not ok
        print(f"  [{'PASS' if ok else 'FAIL'}] {str(intents):40s} -> {str(actual):5s} (期望 {expected})  {desc}")

    print(f"\n  合计: {passed} 通过, {failed} 失败")


#线下课件上没有的方法planning_agent
def planning_agent(self, intents: list, user_queries: dict) -> dict:
    """
    任务规划：直接进行任务的拆解

    注意：在调用此方法前会先通过 _should_skip_planning 做启发式判断，
    只有复杂任务才会进入此方法，省去不必要的 LLM 调用。

    参数：
        intents (list): 识别到的意图列表，如 ["weather", "flight", "attraction"]
        user_queries (dict): 改写后的查询字典，如 {"weather": "...", "flight": "..."}

    返回值：
        dict: 规划结果，格式为：
            - 简单任务：{"need_plan": false, "reason": "单意图，直接查询即可", "steps": []}
            - 复杂任务：{"need_plan": true, "reason": "多意图需要分步执行",
                        "steps": [{"step": 1, "action": "...", "intent": "weather", "depends_on": 0}, ...]}
    """
    #planning_prompt 这是这个方法的核心
    chain = SmartVoyagePrompts.planning_prompt() | self.llm

    planning_response = chain.invoke({
        "conversation_history": self.memory.get_short_term_text(),
        "query": self.messages[-1]["content"] if self.messages else "",
        "intents": json.dumps(intents, ensure_ascii=False),
        "user_queries": json.dumps(user_queries, ensure_ascii=False)
    }).content.strip()
    logger.info(f"规划响应: {planning_response}")

    # 去掉 markdown 代码块包裹（```json ... ``` 或 ``` ... ```）
    planning_response = re.sub(r'^```(?:json)?\s*|\s*```$', '', planning_response).strip()
    plan = json.loads(planning_response)
    return _sanitize_plan(plan, intents)


def _sanitize_plan(plan: dict, intents: list) -> dict:
    """
    校正 planning_agent 的输出，兜住大模型的偶发错误。

    prompt 里的约束是软约束，模型仍可能：
    1. 编造不存在的 intent（如 hotel），导致 execute_step 拿不到 agent
    2. 标出虚假依赖（如 attraction 依赖 weather），让本可并行的步骤退化成串行
    3. depends_on 指向自身或后面的步骤，造成依赖环
    4. 单意图 / 全独立意图场景仍返回 need_plan=true

    参数：
        plan (dict): 大模型返回的原始计划
        intents (list): 意图识别的结果，作为 intent 白名单

    返回值：
        dict: 校正后的计划
    """
    # 只有查询类 → 预订类才是真实数据依赖，其余一律压平为 0
    booking_intents = {"order", "trip_order"}

    steps = plan.get("steps") or []
    allowed = set(intents)
    cleaned = []

    for step in steps:
        intent = step.get("intent", "")
        # 丢弃不在意图识别结果里的步骤，避免下游拿不到 agent
        if intent not in allowed:
            logger.warning(f"规划步骤 intent 越界，已丢弃: {intent}（允许: {sorted(allowed)}）")
            continue
        cleaned.append(dict(step))

    # 重排 step 序号，保持连续；同时建立旧序号到新序号的映射，用于修正 depends_on
    old_to_new = {}
    for new_num, step in enumerate(cleaned, start=1):
        old_to_new[step.get("step")] = new_num
        step["step"] = new_num

    for step in cleaned:
        dep = step.get("depends_on", 0)
        # 非预订类意图不接受任何依赖
        if step.get("intent") not in booking_intents:
            if dep:
                logger.info(f"步骤{step['step']}({step.get('intent')}) 依赖已压平: {dep} -> 0")
            step["depends_on"] = 0
            continue

        new_dep = old_to_new.get(dep, 0)
        # 依赖必须指向前面的步骤，否则视为无依赖
        if new_dep >= step["step"]:
            logger.warning(f"步骤{step['step']} 依赖非法({dep})，已置 0")
            new_dep = 0
        step["depends_on"] = new_dep

    # 无真实依赖时不需要走 ReAct，交给简单分支并行处理
    has_real_dep = any(s.get("depends_on", 0) for s in cleaned)
    if not cleaned:
        return {"need_plan": False, "reason": plan.get("reason", "无有效步骤"), "steps": []}
    if not has_real_dep:
        return {
            "need_plan": False,
            "reason": plan.get("reason", "各意图相互独立，无需分步规划"),
            "steps": [],
        }

    plan["need_plan"] = True
    plan["steps"] = cleaned
    return plan


def _call_planning(service, intents, user_queries):
    """
    调用 planning_agent。

    planning_agent 现在定义在模块级（不在 class 缩进内），但签名带 self，
    所以只能当普通函数用。这里做个兼容：以后缩进回类里也不用改测试。
    """
    if hasattr(service, "planning_agent"):
        return service.planning_agent(intents, user_queries)
    return planning_agent(service, intents, user_queries)


def test_planning_agent():
    """测试 planning_agent 的任务拆解（真实调用大模型）"""
    print("=" * 60)
    print("测试 4: planning_agent 任务规划")
    print("=" * 60)

    service = ChatService()

    # (说明, 最后一条用户输入, intents, user_queries)
    # 注意 planning_agent 内部取的是 self.messages[-1]["content"] 当 query，
    # 所以每个用例都要先把当前输入塞进 messages，否则 query 是空字符串。
    cases = [
        (
            "单意图-天气（期望 need_plan=false）",
            "明天深圳天气怎么样",
            ["weather"],
            {"weather": "深圳明天的天气"},
        ),
        (
            "双意图-无依赖（天气+机票）",
            "下周去上海玩，看看天气和机票",
            ["weather", "flight"],
            {"weather": "上海下周的天气", "flight": "深圳到上海下周的机票"},
        ),
        (
            "三意图-有依赖（机票→天气→景点）",
            "帮我查深圳到北京的机票，再看下那边天气，推荐几个景点",
            ["flight", "weather", "attraction"],
            {
                "flight": "深圳到北京的机票",
                "weather": "北京的天气",
                "attraction": "北京的景点推荐",
            },
        ),
        (
            "查询+预订混合（order 依赖前置查询）",
            "查下明天深圳到广州的高铁，合适的话直接订一张",
            ["train", "order"],
            {"train": "明天深圳到广州的高铁", "order": "预订明天深圳到广州的高铁票"},
        ),
        (
            "完整行程规划-多步强依赖",
            "国庆想去成都玩三天，机票酒店景点保险都帮我安排下",
            ["flight", "attraction", "insurance", "trip_order"],
            {
                "flight": "国庆深圳到成都的机票",
                "attraction": "成都三天的景点推荐",
                "insurance": "国庆成都行程的旅游保险",
                "trip_order": "国庆成都三天行程预订",
            },
        ),
        (
            "空意图-边界用例",
            "随便聊聊",
            [],
            {},
        ),
    ]

    for name, last_input, intents, user_queries in cases:
        print(f"\n[{name}]")
        print(f"  当前输入 : {last_input}")
        print(f"  intents  : {intents}")

        # 模拟真实链路：当前用户输入已进 messages
        service.messages.append({"role": "user", "content": last_input})

        # 对照一下启发式判断：正式链路里 skip=True 就不会走到 planning_agent
        skip = service._should_skip_planning(intents)
        print(f"  _should_skip_planning -> {skip}"
              f"{'（正式链路会跳过规划，这里强制调用看结果）' if skip else '（正式链路会进入规划）'}")

        try:
            plan = _call_planning(service, intents, user_queries)
            print(f"  need_plan : {plan.get('need_plan')}")
            print(f"  reason    : {plan.get('reason')}")
            steps = plan.get("steps", [])
            if steps:
                print(f"  steps({len(steps)}):")
                for s in steps:
                    print(f"    step{s.get('step')} [{s.get('intent')}] "
                          f"depends_on={s.get('depends_on')}  {s.get('action')}")
            else:
                print("  steps     : (空)")
        except json.JSONDecodeError as e:
            print(f"  JSON 解析失败（大模型返回了非纯 JSON）: {e}")
        except Exception as e:
            print(f"  调用失败: {type(e).__name__}: {e}")


class _FakeMsg:
    """模拟 LLM 返回的消息对象，只需要 .content"""
    def __init__(self, content):
        self.content = content


def _make_mock_service(fail_intents=()):
    """
    构造一个被 mock 过的 ChatService：
      - execute_step 换成假实现，不走 A2A 子代理、不调大模型
      - llm 换成假 Runnable，react_loop 最后的汇总也不联网

    fail_intents: 这些意图对应的步骤会抛异常，用来测异常分支
    """
    from langchain_core.runnables import RunnableLambda

    service = ChatService()
    call_order = []   # 记录实际执行顺序，用来验证分组与并行

    async def fake_execute_step(step: dict, user_queries: dict) -> str:
        intent = step.get("intent", "")
        num = step.get("step", 0)
        call_order.append(num)
        if intent in fail_intents:
            raise RuntimeError(f"模拟 {intent} 子代理超时")
        # 模拟一点耗时，让并行/串行的差异体现在耗时上
        await asyncio.sleep(0.3)
        query = user_queries.get(intent, "(无改写查询)")
        return f"[MOCK-{intent}] 针对「{query}」的查询结果"

    service.execute_step = fake_execute_step
    service.llm = RunnableLambda(lambda _pv: _FakeMsg("【MOCK汇总】已把各步骤结果合并成一段连贯回复"))
    return service, call_order


def test_react_loop():
    """用 mock 数据测 react_loop 执行编排好的任务（不联网、不调大模型）"""
    print("=" * 60)
    print("测试 5: react_loop 执行编排好的步骤（mock 数据）")
    print("=" * 60)

    # (说明, steps, user_queries, 失败意图)
    cases = [
        (
            "单步骤：只有一个 observation，应直接返回不走汇总",
            [{"step": 1, "action": "查询深圳天气", "intent": "weather", "depends_on": 0}],
            {"weather": "深圳明天的天气"},
            (),
        ),
        (
            "两步无依赖：depends_on 都为 0，应并行",
            [{"step": 1, "action": "查询上海天气", "intent": "weather", "depends_on": 0},
             {"step": 2, "action": "查询到上海机票", "intent": "flight", "depends_on": 0}],
            {"weather": "上海下周天气", "flight": "深圳到上海下周机票"},
            (),
        ),
        (
            "三步链式依赖：0→1→2，应串行且顺序为 1,2,3",
            [{"step": 1, "action": "查高铁", "intent": "train", "depends_on": 0},
             {"step": 2, "action": "订票", "intent": "order", "depends_on": 1},
             {"step": 3, "action": "买保险", "intent": "insurance", "depends_on": 2}],
            {"train": "明天深圳到广州高铁", "order": "预订该趟高铁",
             "insurance": "该行程的旅游保险"},
            (),
        ),
        (
            "混合分组：两步并行后接一步依赖",
            [{"step": 1, "action": "查机票", "intent": "flight", "depends_on": 0},
             {"step": 2, "action": "查天气", "intent": "weather", "depends_on": 0},
             {"step": 3, "action": "推荐景点", "intent": "attraction", "depends_on": 1}],
            {"flight": "深圳到北京机票", "weather": "北京天气", "attraction": "北京景点推荐"},
            (),
        ),
        (
            "并行组中一步抛异常：应被 return_exceptions 捕获成文本",
            [{"step": 1, "action": "查机票", "intent": "flight", "depends_on": 0},
             {"step": 2, "action": "查天气", "intent": "weather", "depends_on": 0}],
            {"flight": "深圳到北京机票", "weather": "北京天气"},
            ("weather",),
        ),
        (
            "空步骤列表：应返回兜底文案",
            [],
            {},
            (),
        ),
        (
            "步骤缺少 intent 字段：走 user_queries 缺失退路",
            [{"step": 1, "action": "帮我看看有什么好玩的", "depends_on": 0},
             {"step": 2, "action": "查天气", "intent": "weather", "depends_on": 0}],
            {"weather": "北京天气"},
            (),
        ),
    ]

    import time
    for name, steps, user_queries, fail_intents in cases:
        print(f"\n[{name}]")
        print(f"  steps: {json.dumps([{'s': s.get('step'), 'i': s.get('intent', '(无)'), 'dep': s.get('depends_on')} for s in steps], ensure_ascii=False)}")

        service, call_order = _make_mock_service(fail_intents)
        # react_loop 汇总时会取 self.messages[-1]["content"] 当 query
        service.messages.append({"role": "user", "content": "（测试用当前输入）"})

        t0 = time.time()
        try:
            result = asyncio.run(service.react_loop(steps, user_queries))
            elapsed = time.time() - t0
            print(f"  执行顺序: {call_order}")
            print(f"  耗时    : {elapsed:.2f}s (每步 mock 0.3s，并行则总耗时接近 0.3s)")

            # 多步骤本该走 react_summary_prompt 汇总成一段话。
            # 若返回值里没有 mock 汇总标记，说明汇总失败退回了原始结果拼接。
            if len(steps) > 1:
                if "【MOCK汇总】" in result:
                    print("  汇总    : 走了 react_summary_prompt 汇总")
                else:
                    print("  汇总    : 未汇总，退回原始结果拼接（汇总分支异常被 except 吞掉）")
            print(f"  返回    : {result}")
        except Exception as e:
            elapsed = time.time() - t0
            print(f"  抛异常  : {type(e).__name__}: {e}")
            print(f"  执行顺序: {call_order}（耗时 {elapsed:.2f}s）")


if __name__ == '__main__':
    #测试init方法,查看记忆是否被加载到内存了
    # test_init()
    # test_intent_agent()
    # test_should_skip_planning()
    test_planning_agent()
    # test_react_loop()
    # planning_agent
    # 写一个测试用例, 最好包含多换个例子, 测一下这个方法, 在main函数中执行


    # 直接写一些mock 数据 测试react_loop 执行编好的任务.写一个测试方法测一下这个流程
