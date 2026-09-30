"""
需求：定义SmartVoyage项目中使用的各种提示模板，用于不同场景的对话处理

什么是 Prompt Template（提示模板）？
    提示模板是一种可复用的文本模板，其中包含固定内容和可变变量（用 {变量名} 表示）。
    例如：模板 "你好，{name}！" 在填入 name="张三" 后会变成 "你好，张三！"。

为什么使用模板类管理？
    1. 集中管理：所有 prompt 定义在同一个文件中，方便查找和修改
    2. 可复用：同一个模板可以被多处调用
    3. 参数化：通过变量注入不同上下文，避免字符串拼接

本项目中的 Prompt 分类：
    1. 意图识别类：intent_prompt —— 识别用户想做什么
    2. 结果总结类：summarize_weather_prompt、summarize_ticket_prompt —— 将原始数据转化为友好回复
    3. 内容生成类：attraction_prompt —— 直接生成景点推荐
    4. 任务规划类：planning_prompt —— 判断任务复杂度并生成执行计划（Planning + ReAct 架构）
    5. ReAct推理类：react_prompt、react_summary_prompt —— 逐步推理和最终汇总
"""

from langchain_core.prompts import ChatPromptTemplate  # LangChain 的聊天提示模板类


class SmartVoyagePrompts:
    """
    SmartVoyage 提示模板管理类

    这个类定义了系统中所有用到的 Prompt 模板，每个模板都是一个静态方法，
    返回一个 ChatPromptTemplate 对象。

    使用方式：
        prompt = SmartVoyagePrompts.intent_prompt()  # 获取意图识别模板
        chain = prompt | llm                         # 组装成处理链
        result = chain.invoke({"query": "北京天气"})  # 调用并传入变量
    """



# ==================== 意图识别 ====================

    @staticmethod
    def intent_prompt():
        """
        意图识别提示模板 —— 让大模型分析用户输入，判断用户想做什么

        输入变量：
            - user_profile: 用户偏好（如"二等座"、"经济舱"）
            - task_context: 当前任务上下文（如之前查过什么）
            - conversation_history: 对话历史（最近几轮对话）
            - query: 用户本次输入

        输出格式（JSON）：
            {
                "intents": ["weather", "flight"],           # 识别到的意图列表
                "user_queries": {"weather": "...", ...},    # 改写后的查询（可能结合历史补充信息）
                "follow_up_message": ""                     # 追问消息（意图不明确时使用）
            }

        支持的意图类型：
            weather / flight / train / concert / order / car_rental / tour_group / insurance / trip_order / attraction / out_of_scope
        """
        return ChatPromptTemplate.from_template(
"""
系统提示：
角色：您是一个专业的旅行意图识别专家，
任务：基于用户查询、对话历史和用户偏好，识别其意图，用于调用专门的agent server来执行；为方便后续的agent server处理，可以基于对话历史对用户查询进行改写，使问题更明确。
严格遵守规则：
- 支持意图：['weather' (天气查询), 'flight' (机票查询), 'train' (高铁/火车票查询), 'concert' (演唱会票查询), 'order' (票务预定), 'car_rental' (租车查询), 'tour_group' (旅游团查询), 'insurance' (保险查询), 'trip_order' (行程预订), 'attraction' (景点推荐)] 或其组合（如 ['weather', 'flight']）。如果意图是旅行相关，但是非常复杂的返回意图 'complex'。如果意图超出范围，返回意图 'out_of_scope'。
- 注意票务预定和票务查询要区分开，涉及到订票时则为order，只是查询则为flight、train或concert。
- **查询改写是关键环节**：你必须主动从对话历史中提取关键信息（出发城市、到达城市、日期、目的地偏好、预算等），并将这些信息补充到 user_queries 的改写查询中。即使当前查询很简短（如"好的"、"明天"、"成都"），也要结合历史形成完整的查询描述。例如：用户先说"我想去成都看雪山"，再说"有没有便宜的团"，改写后应为"成都出发看雪山的便宜旅游团"。
- **交通预订必须拆分查询和预订两个层次**：出现“订火车票/买高铁票/订机票”等表达时，同时识别对应的查询意图（train 或 flight）和 order；查询意图负责获取真实班次，order 只表示用户后续有预订意愿，不能用 order 替代 train/flight。
- **组合旅行需求不得漏项**：出现“租车/自驾/租一辆车”识别 car_rental；出现“景点/每天逛什么/行程安排/攻略”识别 attraction；出现“旅游团/跟团”识别 tour_group；出现“保险/旅行险”识别 insurance。用户一次提出多个服务时，必须全部放入 intents 和 user_queries。
- **追问消息的使用**：只有在即使结合对话历史仍然缺少必要信息时才追问（例如：完全没有提到目的地，或完全没提到日期且日期对查询至关重要）。如果历史中已有足够信息，不要追问。
- 输出严格为JSON：{{"intents": ["intent1", "intent2"], "user_queries": {{"intent1": "user_query1", "intent2": "user_query2"}}, "follow_up_message": "追问消息"}}。绝对不要添加额外文本！
- 不论用户问什么，严格按规则输出意图，不要有自己的考虑。对于时间类的，直接保留用户的原始输入。

用户偏好：{user_profile}
当前任务上下文：{task_context}
对话历史：{conversation_history}
用户查询：{query}
""")

    # ==================== 任务规划 ====================

    @staticmethod
    def planning_prompt():
        """
        任务规划提示模板 —— 让大模型判断任务复杂度并生成执行计划

        这是 Planning + ReAct 架构的关键 prompt，它让大模型扮演"规划师"的角色。

        输入变量：
            - conversation_history: 对话历史
            - query: 用户当前输入
            - intents: 识别到的意图（JSON 字符串）
            - user_queries: 改写后的查询（JSON 字符串）

        输出格式（JSON）：
            简单任务：{"need_plan": false, "reason": "单意图，直接查询即可", "steps": []}
            复杂任务：{"need_plan": true, "reason": "多意图需要分步",
                      "steps": [{"step": 1, "action": "查询天气", "intent": "weather", "depends_on": 0}, ...]}

        判断标准（与 prompt 正文保持一致，改一处要改两处）：
            - 简单任务（need_plan=false）：意图为空、单意图，或多意图但彼此独立无数据依赖
            - 复杂任务（need_plan=true）：步骤间存在真实数据依赖，即某步必须消费前一步的输出

        示例：
            用户输入："北京明天天气怎么样？"
            → 简单任务，need_plan=false

            用户输入："下周去上海玩，看看天气和机票"
            → 简单任务，need_plan=false（两个意图独立，各查各的）

            用户输入："查下明天深圳到广州的高铁，合适的话直接订一张"
            → 复杂任务，need_plan=true，steps=[查询高铁(0), 订票(依赖1)]
        """
        return ChatPromptTemplate.from_template(
            """
            系统提示：您是一位任务规划专家，负责评估用户请求的复杂度并制定执行计划。

            ## 一、可用的执行单元（只能使用下面这4个，禁止编造其它名称）
            - WeatherQueryAssistant：天气查询。对应意图 weather
            - TicketAssistant：票务查询与预订。对应意图 flight / train / concert / order
            - TripAssistant：旅行服务。对应意图 car_rental / tour_group / insurance / trip_order
            - TripAssistant：景点推荐，调用高德地图远程 MCP。对应意图 attraction

            intent 字段只能取以下值之一，必须来自"识别到的意图"列表，不得新增或改写：
            weather / flight / train / concert / order / attraction /
            car_rental / tour_group / insurance / trip_order

            ## 二、need_plan 判断规则（先判断，再决定是否拆解）
            返回 need_plan=false（steps 必须为空数组 []）的情况：
            1. 意图列表为空
            2. 只有 1 个意图
            3. 多个意图但彼此独立：每个意图各查各的，谁都不需要等别人的结果

            返回 need_plan=true 的情况，仅限于：
            - 存在真实的数据依赖：某一步必须拿到前一步的结果才能执行
              典型场景：先查车次/航班，再用查到的班次去下单（order / trip_order 依赖前置查询）

            注意：意图多 ≠ 需要规划。"看看天气和机票"是两个独立查询，need_plan=false。

            ## 三、depends_on 规则
            - depends_on=0 表示无前置依赖，可立即执行；同为 0 的步骤会被并行执行
            - 只有当本步骤真的需要消费前一步的输出时，才填写那一步的 step 序号
            - 以下都不构成依赖，必须填 0：
              * 天气 → 景点（景点推荐不需要天气数据）
              * 机票 → 保险（保险查询不需要航班数据）
              * 机票 → 天气、天气 → 机票（互不相关）
            - 真正构成依赖的：查询类 → 预订类（order / trip_order 需要先拿到可选班次或行程）
            - 不要制造链式依赖来表达"执行顺序"，顺序由 step 序号表达，依赖只表达数据流

            ## 四、字段说明
            - step: 步骤序号，从 1 开始连续递增
            - action: 具体动作，写明调用哪个执行单元做什么（如"调用WeatherQueryAssistant查询北京天气"）
            - intent: 对应意图，取值见第一节
            - depends_on: 依赖的前置步骤序号，无依赖填 0

            ## 五、示例
            意图 ["weather"] → {{"need_plan": false, "reason": "单意图，直接查询即可", "steps": []}}

            意图 ["weather", "flight"]（"看看天气和机票"）
            → {{"need_plan": false, "reason": "两个意图互相独立，无数据依赖，可直接分别查询", "steps": []}}

            意图 ["train", "order"]（"查高铁，合适就订一张"）
            → {{"need_plan": true, "reason": "订票需要先拿到车次查询结果，存在数据依赖",
                 "steps": [{{"step": 1, "action": "调用TicketAssistant查询明天深圳到广州的高铁班次", "intent": "train", "depends_on": 0}},
                           {{"step": 2, "action": "调用TicketAssistant根据查到的班次完成订票", "intent": "order", "depends_on": 1}}]}}

            对话历史：{conversation_history}
            当前用户查询：{query}
            识别到的意图：{intents}
            用户查询改写：{user_queries}

            输出严格为JSON，不要包含 markdown 代码块标记，不要添加任何解释文本。
            need_plan=false 时格式：{{"need_plan": false, "reason": "原因", "steps": []}}
            need_plan=true  时格式：{{"need_plan": true, "reason": "原因", "steps": [{{"step": 1, "action": "...", "intent": "...", "depends_on": 0}}]}}
            """)

    # ==================== 结果总结 ====================

    @staticmethod
    def summarize_weather_prompt():
        """
        天气结果总结提示模板 —— 将天气 agent 返回的原始数据转化为用户友好的天气描述

        输入变量：
            - query: 用户查询（如"北京明天天气"）
            - raw_response: 天气 agent 返回的原始数据

        使用场景：
            原始数据可能是 JSON 格式或结构化数据，直接展示给用户不好看，
            所以用这个 prompt 让大模型翻译成自然语言。

        示例输出：
            "根据最新数据，北京2025-07-31的天气预报为晴天，气温25-32度，湿度45%，东南风2级..."
        """
        return ChatPromptTemplate.from_template(
            """
            系统提示：您是一位专业的天气预报员，以生动、准确的风格总结天气信息。基于查询和结果：
            - 核心描述点：城市、日期、温度范围、天气描述、湿度、风向、降水等。
            - 如果结果为空或者意思为需要补充数据，则委婉提示"未找到数据，请确认城市/日期"
            - 语气：专业预报，如"根据最新数据，北京2025-07-31的天气预报为..."。
            - 保持中文，100-150字。
            - 如果查询无关，返回"请提供天气相关查询。"
        
            查询：{query}
            结果：{raw_response}
            """)

    @staticmethod
    def summarize_ticket_prompt():
        """
        票务结果总结提示模板 —— 将票务 agent 返回的原始数据转化为用户友好的票务推荐

        输入变量：
            - query: 用户查询（如"北京到上海机票"）
            - raw_response: 票务 agent 返回的原始数据

        使用场景：
            和天气总结类似，将结构化的票务数据（航班号、价格、时间等）
            翻译成顾问式的推荐语言。

        示例输出：
            "为您推荐北京到上海的机票选项：MU5101航班，起飞时间08:30，经济舱价格1280元，余票充足..."
        """
        return ChatPromptTemplate.from_template(
            """
            系统提示：您是一位专业的旅行顾问，以热情、精确的风格总结票务信息。基于查询和结果：
            - 核心描述点：出发/到达、时间、类型、价格、剩余座位等。
            - 如果结果为空或者意思为需要补充数据，则委婉提示"未找到数据，请确认或修改条件"
            - 语气：顾问式，如"为您推荐北京到上海的机票选项..."。
            - 保持中文，100-150字。
            - 如果查询无关，返回"请提供票务相关查询。"
        
        
            查询：{query}
            结果：{raw_response}
            """)

    # ==================== 内容生成 ====================

    @staticmethod
    def attraction_prompt():
        """
        景点推荐提示模板 —— 让大模型直接生成景点推荐内容

        输入变量：
            - query: 用户查询（如"推荐几个北京景点"）
            - weather_info: 目的地天气信息（可选，为空时不展示天气相关描述）

        特点：
            景点推荐不需要调用外部 agent，大模型本身就有足够的知识来生成推荐。
            当提供 weather_info 时，应基于真实天气给出出行建议；
            当 weather_info 为空时，不得虚构任何天气描述。
        """
        return ChatPromptTemplate.from_template(
            """
            系统提示：您是一位旅行专家，基于用户查询生成景点推荐。规则：
            - 推荐3-5个景点，包含描述、理由、注意事项。
            - 基于槽位：城市、偏好。
            - 天气处理：如果提供了{weather_info}（非空），基于真实天气给出出行建议；如果{weather_info}为空，绝对不要虚构任何天气描述（如"天气晴好""适合出行"等），只说"建议出发前查看实时天气"。
            - 语气：热情推荐，如"推荐您在北京探索故宫..."。
            - 备注：内容生成，仅供参考。
            - 保持中文，150-250字。
    
            查询：{query}
            目的地天气：{weather_info}
            """)

    # ==================== ReAct 结果汇总 ====================

    @staticmethod
    def react_summary_prompt():
        """
        ReAct 多步骤结果汇总提示模板 —— 把各步骤的 Observation 合并成一段连贯回复

        输入变量：
            - query: 用户当前输入（原始问题）
            - all_observations: 各步骤执行结果的拼接文本，形如
                "步骤1（查询机票）: ...\n\n步骤2（查询天气）: ..."

        使用场景：
            react_loop 执行完多个步骤后，每步都有独立结果。直接拼接给用户会很割裂，
            这里让大模型按用户原始问题的角度重新组织成一段完整回复。
            单步骤时 react_loop 会跳过此 prompt，省一次大模型调用。

        注意：
            步骤结果里可能有"执行失败：..."的条目（子代理超时等），
            必须如实告知用户哪部分没查到，不能凭空补内容。

        示例输出：
            "为您查到深圳到北京的机票和当地天气：MU5101航班08:30起飞，经济舱1280元；
             北京明天多云转晴，18-26℃，适合出行。景点推荐部分暂时没能查到，建议稍后再试。"
        """
        return ChatPromptTemplate.from_template(
            """
            系统提示：您是一位专业的旅行顾问，负责把多个查询步骤的结果整合成一段连贯的回复。规则：
            - 以用户的原始问题为主线组织内容，按用户关心的顺序表述，不要机械罗列"步骤1、步骤2"。
            - 只使用结果中已有的信息，绝对不要虚构、补充或推测任何数据（航班号、价格、天气数值等）。
            - 如果某步骤结果是"执行失败"或为空，如实说明该部分未查询成功，并建议用户稍后重试，不要跳过不提。
            - 各部分之间用自然的过渡衔接，让回复读起来像一次完整的答复。
            - 结尾可给一句简短的出行建议或提示，但不得引入新的事实。
            - 语气：顾问式、热情但克制。保持中文，200-350字。

            用户问题：{query}
            各步骤执行结果：
            {all_observations}
            """)

    @staticmethod
    def summarize_trip_prompt():
        """保留景点、旅游团和保险等行程服务的独立结果。"""
        return ChatPromptTemplate.from_template(
            """
            系统提示：你是旅行顾问，负责整理远程 MCP 返回的行程服务结果。
            - 必须保留结果中已有的景点名称、地址、价格、日期和推荐理由，不得改写成泛泛建议。
            - 如果结果包含景点，使用“景点推荐”小节，至少保留 3 个景点（如果原始结果有 3 个）。
            - 旅游团和保险分别使用独立小节；某一服务失败时明确说明，不要用常识补齐。
            - 只使用结果中已有信息，不虚构库存、价格、天气或订单状态。
            - 中文输出，结构清晰，优先回答用户指定的目的地和日期。
            - 只整理“当前子任务类型”对应的内容，不能把其他子任务的景点、旅游团或保险重复带入。

            当前子任务类型：{intent}
            查询：{query}
            远程服务结果：{raw_response}
            """)

    @staticmethod
    def trip_plan_summary_prompt():
        """把多个真实服务结果编排成按天的完整旅行方案。"""
        return ChatPromptTemplate.from_template(
            """
            系统提示：你是一名资深旅行产品经理和行程规划师，负责把远程 MCP 返回的真实结果整理成可执行、可核验的旅行方案。

            ## 输出目标
            用户需要的是完整方案，不是几个服务结果的简单拼接。必须围绕用户的出发地、目的地、出行日期和交通方式，组织成一份按天安排的计划。

            ## 必须遵守的事实边界
            1. 只使用“远程服务结果”中已经返回的事实。不得编造景点门票、车次、价格、库存、保险保费、租车价格或营业时间。
            2. 远程结果没有门票费用时，明确写“远程服务未返回门票价格，请以景区官方渠道为准”，不能用常识估价。
            3. 旅游团、保险、租车或车票查询失败/需要补充资料时，必须单独说明缺失原因和下一步需要用户提供的信息，不能跳过。
            4. 不把“查询到”写成“已预订”。本系统只能提供查询和推荐，除非结果明确返回订单成功，否则不得声称已经购票、订团、投保或租车。
            5. 景点必须优先使用高德返回的真实景点名称、地址和推荐信息；不要用模型记忆替换远程结果。

            ## 固定输出结构
            ### 1. 行程概览
            写明日期、深圳到北京的交通方式、已查询到的车票信息，以及本方案包含的旅游团、保险、租车和景点服务。

            ### 2. 每日行程安排
            按用户日期逐日列出（例如“9月10日｜第1天”）。每天至少包含：
            - 上午 / 下午 / 晚上的景点或活动；
            - 每个景点的名称、地址和推荐理由（只能使用真实结果）；
            - 景点之间的合理游览顺序和交通建议；
            - 门票费用：有真实价格就写价格和来源，没有就明确标记“未返回”；
            - 当天的注意事项或需要预约的事项（仅限结果中明确提到的内容）。
            如果真实结果中的景点数量不足以覆盖每天，不要补造景点，明确标注“可用真实景点信息不足”。

            ### 3. 旅游服务结果
            分别列出：旅游团、旅行保险、租车。每一项保留真实产品/方案名称、价格、日期、适用条件、链接或下一步要求；没有返回的字段写“未返回”。

            ### 4. 待确认事项
            集中列出无法由当前远程服务确认的信息，例如门票实时价格、保险报价所需的居住国/年龄、租车驾驶人信息等。

            ## 表达要求
            - 使用中文，专业、清晰、克制；优先使用 Markdown 标题、表格和列表。
            - 不要输出“步骤1/步骤2”，不要重复同一景点或同一服务结果。
            - 先给结论和完整计划，再说明限制和待确认事项。

            用户原始需求：{query}
            已识别意图：{intents}
            远程服务结果：
            {service_results}
            """)
