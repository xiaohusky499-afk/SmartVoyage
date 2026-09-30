from datetime import datetime
import json
import mysql.connector
import pytz

from SmartVoyage.create_logger import logger

class ConversationMemory:
    """管理对话记忆的类，包括短期对话、用户偏好和任务上下文"""
    #初始化: 初始化 短期记忆,用户偏好,当前任务,历史实体, 链接对象
    def __init__(self, short_term_limit: int = 10):
        self.short_term_messages = []   # 短期对话，最多保留10条
        self.user_profile = {}          # 用户偏好，如 {"seat_type": "二等座"}
        self.current_task = {}          # 当前任务上下文
        self.short_term_limit = short_term_limit #限定短期记忆要保留几轮对话
        self.entity_history = []        # 历史提取的关键实体，最多20条
        self._db_conn = None            # 数据库连接（由 ChatService 注入）

    #创建数据库连接
    def set_db_connection(self, db_conn):
        """设置数据库连接（由 ChatService 注入）"""
        self._db_conn = db_conn

    #添加短期记忆
    def add_message(self, role: str, content: str):
        """添加消息到短期记忆，并持久化到数据库"""
        self.short_term_messages.append({
            "role": role,
            "content": content,
            "timestamp": datetime.now(pytz.timezone('Asia/Shanghai')).strftime('%H:%M:%S')
        })
        # 超过限制则移除最旧的消息
        if len(self.short_term_messages) > self.short_term_limit:
            #如果超过规定的轮次,就只取得近的short_term_limit 更新到short_term_messages
            self.short_term_messages = self.short_term_messages[-self.short_term_limit:]
        self.save_messages_to_db() #将短期记忆存入到数据库中

    #取短期记忆的方法
    def get_short_term_text(self) -> str:
        """获取短期对话的文本格式，用于注入到 Prompt 中"""
        lines = []
        for msg in self.short_term_messages:
            role_label = "User" if msg["role"] == "user" else "Assistant"
            lines.append(f"{role_label}: {msg['content']}")
        return '\n'.join(lines) #返回短期记忆字符串, 作用就是拼接prompt

    #更行用户的偏好,并写入数据库
    def update_profile(self, profile_update: dict):
        """更新用户偏好，并持久化到数据库"""
        self.user_profile.update(profile_update)
        self.save_profile_to_db()

    #更新任务上下文
    def update_task_context(self, task_update: dict):
        """更新当前任务上下文"""
        self.current_task.update(task_update)
    #抽取历史任务的实体
    def extract_entities(self, intent_type: str, query: str):
        """从查询中提取关键实体到历史"""
        self.entity_history.append({
            "type": intent_type,
            "query": query,
            "timestamp": datetime.now(pytz.timezone('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M:%S')
        })
        if len(self.entity_history) > 20:
            self.entity_history = self.entity_history[-20:]
        self.save_entity_to_db(intent_type, query)

    def clear(self):
        """清空所有记忆，同时清空数据库数据"""
        self.short_term_messages = []
        self.user_profile = {}
        self.current_task = {}
        self.entity_history = []
        self.clear_all_from_db()

    #定义持久化层
    #第一方法 save_messages_to_db

    # ==================== 数据库持久化 ====================
    def _ensure_db(self) -> bool:
        """
        确保数据库连接可用，断开则自动重连。

        返回值：True 表示连接可用，False 表示无连接（未注入或重连失败），
        调用方据此直接跳过持久化，不影响内存中的记忆。
        """
        if self._db_conn is None:
            return False
        try:
            self._db_conn.ping(reconnect=True, attempts=3, delay=1)
            return True
        except Exception as e:
            logger.warning(f"数据库连接不可用，记忆将不再持久化: {e}")
            self._db_conn = None
            return False

    def save_messages_to_db(self):
        """将短期对话覆盖写入数据库（先删除旧数据，再写入当前列表）"""
        if self._db_conn is None:
            return
        self._ensure_db()
        cursor = self._db_conn.cursor()
        cursor.execute("DELETE FROM short_term_messages")
        for i, msg in enumerate(self.short_term_messages):
            cursor.execute(
                "INSERT INTO short_term_messages (role, content, message_time, message_order) "
                "VALUES (%s, %s, %s, %s)",
                (msg["role"], msg["content"], msg["timestamp"], i)
            )
        self._db_conn.commit()
        cursor.close()


    #写入偏好表
    def save_profile_to_db(self):
        """将用户偏好持久化到数据库（UPSERT）"""
        if self._db_conn is None:
            return
        self._ensure_db()
        cursor = self._db_conn.cursor()
        for key, value in self.user_profile.items():
            cursor.execute(
                "INSERT INTO user_profiles (profile_key, profile_value) "
                "VALUES (%s, %s) ON DUPLICATE KEY UPDATE profile_value = %s",
                (key, str(value), str(value))
            )
        self._db_conn.commit()
        cursor.close()

    #从内存中获取偏好数据,为啥不从mysql中获取呢? 后面讲
    def get_profile_text(self) -> str:
        """获取用户偏好的文本描述"""
        if not self.user_profile:
            return "无已知的用户偏好"
        items = [f"{k}: {v}" for k, v in self.user_profile.items()]
        return "，".join(items)

    def save_entity_to_db(self, intent_type: str, query):
        """将一次查询的关键实体追加写入 query_history 表"""
        if not self._ensure_db():
            return
        # query 可能是 dict（意图识别改写后的结构化查询），统一序列化成字符串存储
        if isinstance(query, (dict, list)):
            query_content = json.dumps(query, ensure_ascii=False)
        else:
            query_content = str(query)

        cursor = self._db_conn.cursor()
        cursor.execute(
            "INSERT INTO query_history (intent_type, query_content, query_time) "
            "VALUES (%s, %s, %s)",
            (intent_type, query_content,
             datetime.now(pytz.timezone('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M:%S'))
        )
        self._db_conn.commit()
        cursor.close()

    def clear_all_from_db(self):
        """清空数据库中所有记忆数据"""
        if self._db_conn is None:
            return
        self._ensure_db()
        cursor = self._db_conn.cursor()
        cursor.execute("DELETE FROM short_term_messages")
        cursor.execute("DELETE FROM query_history")
        cursor.execute("DELETE FROM user_profiles")
        self._db_conn.commit()
        cursor.close()

    #从数据库中加载记忆
    def load_messages_from_db(self):
        """从数据库加载短期对话消息（按 message_order 正序还原）"""
        if not self._ensure_db():
            return
        cursor = self._db_conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT role, content, message_time FROM short_term_messages "
            "ORDER BY message_order"
        )
        rows = cursor.fetchall()
        cursor.close()

        # 注意 key 用 timestamp（对齐 add_message / get_short_term_text 的读法），
        # 不是数据库列名 message_time
        messages = [
            {"role": row["role"], "content": row["content"], "timestamp": row["message_time"]}
            for row in rows
        ]
        # 直接赋值而非走 add_message，避免加载过程反过来触发 save_messages_to_db 覆写
        self.short_term_messages = messages[-self.short_term_limit:]

    #从数据中读取实体信息表加载实体
    def load_entities_from_db(self):
        """从数据库加载最近 20 条关键实体（按时间正序还原到 entity_history）"""
        if not self._ensure_db():
            return
        cursor = self._db_conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT intent_type, query_content, query_time FROM query_history "
            "ORDER BY query_time DESC, id DESC LIMIT 20"
        )
        rows = cursor.fetchall()
        cursor.close()

        # 查询时倒序取最近 20 条，这里反转回时间正序
        self.entity_history = [
            {
                "type": row["intent_type"],
                "query": row["query_content"],
                "timestamp": row["query_time"].strftime('%Y-%m-%d %H:%M:%S')
                if hasattr(row["query_time"], "strftime") else str(row["query_time"])
            }
            for row in reversed(rows)
        ]

    #加载用户偏好信息
    def load_profile_from_db(self):
        """从数据库加载用户偏好"""
        if self._db_conn is None:
            return
        self._ensure_db()
        cursor = self._db_conn.cursor(dictionary=True)
        cursor.execute("SELECT profile_key, profile_value FROM user_profiles")
        rows = cursor.fetchall()
        cursor.close()
        self.user_profile = {row["profile_key"]: row["profile_value"] for row in rows}

#以下是测试代码

def _connect_db():
    """建立数据库连接（调试用，正式代码里由 ChatService 注入）"""
    from SmartVoyage.config import Config

    conf = Config()
    db_conn = mysql.connector.connect(
        host=conf.host, user=conf.user,
        password=conf.password, database=conf.database
    )
    print(f"已连接数据库: {conf.user}@{conf.host}/{conf.database}")
    return db_conn


def _dump_profile_table(db_conn):
    """打印 user_profiles 表的全部内容"""
    cursor = db_conn.cursor()
    cursor.execute("SELECT profile_key, profile_value, updated_at FROM user_profiles ORDER BY profile_key")
    rows = cursor.fetchall()
    cursor.close()
    print(f"表中共 {len(rows)} 行:")
    for key, value, updated_at in rows:
        print(f"  {key:12s} = {value:12s} (updated_at={updated_at})")


def test_profile():
    """测试 save_profile_to_db() 与 get_profile_text()"""
    memory = ConversationMemory()
    db_conn = _connect_db()
    memory.set_db_connection(db_conn)

    # 1. 空偏好时的兜底文案
    print("\n===== 1. 空偏好 =====")
    print(f"get_profile_text() -> {memory.get_profile_text()}")

    # 2. 首次写入：走 INSERT 分支
    print("\n===== 2. 首次写入偏好 =====")
    memory.update_profile({"seat_type": "二等座", "departure_time": "上午", "city": "上海"})
    print(f"get_profile_text() -> {memory.get_profile_text()}")
    _dump_profile_table(db_conn)

    # 3. 改同一个 key + 加一个新 key：验证 ON DUPLICATE KEY UPDATE 是覆盖而不是插重复行
    print("\n===== 3. 更新已有 key（seat_type 改一等座）+ 新增 key =====")
    memory.update_profile({"seat_type": "一等座", "budget": 1500})
    print(f"get_profile_text() -> {memory.get_profile_text()}")
    _dump_profile_table(db_conn)
    print("说明: seat_type 应为一等座且总行数为 4，说明 UPSERT 生效、没有插入重复行")

    # # 4. 非字符串值：save_profile_to_db 里用 str(value) 转换后入库
    # print("\n===== 4. 非字符串值入库检查 =====")
    # cursor = db_conn.cursor()
    # cursor.execute("SELECT profile_value FROM user_profiles WHERE profile_key = 'budget'")
    # print(f"budget 内存中类型={type(memory.user_profile['budget']).__name__}, 库里存的={cursor.fetchone()[0]!r}")
    # cursor.close()

    db_conn.close()


def main():
    """本地调试入口：建立数据库连接并注入，验证短期记忆的读写与持久化"""
    from SmartVoyage.config import Config

    conf = Config()
    # short_term_limit 设为 4，方便观察超出限制后旧消息被丢弃
    memory = ConversationMemory(short_term_limit=4)

    # 关键一步：手动建连接并注入，否则 save_messages_to_db() 会直接 return，数据库里不会有数据
    db_conn = mysql.connector.connect(
        host=conf.host, user=conf.user,
        password=conf.password, database=conf.database
    )
    memory.set_db_connection(db_conn)
    print(f"已连接数据库: {conf.user}@{conf.host}/{conf.database}")

    conversation = [
        ("user", "我想订一张明天去上海的高铁票"),
        ("assistant", "好的，请问您希望的出发时间段是？"),
        ("user", "上午出发，二等座"),
        ("assistant", "已为您筛选出上午出发的二等座车次"),
        ("user", "帮我订最早的那班"),
        ("assistant", "已锁定 G1 次列车，请确认支付"),
    ]

    for role, content in conversation:
        memory.add_message(role, content)
        print(f"add_message({role}) -> 当前条数: {len(memory.short_term_messages)}")

    print("\n===== get_short_term_text() =====")
    print(memory.get_short_term_text())
    #
    # print("\n===== 原始列表（含 timestamp）=====")
    # print(json.dumps(memory.short_term_messages, ensure_ascii=False, indent=2))

    # # 反查数据库，确认真的落库了
    # print("\n===== 数据库中的 short_term_messages =====")
    # cursor = db_conn.cursor()
    # cursor.execute(
    #     "SELECT message_order, role, content, message_time "
    #     "FROM short_term_messages ORDER BY message_order"
    # )
    # for order, role, content, msg_time in cursor.fetchall():
    #     print(f"[{order}] {role:9s} {msg_time}  {content}")
    # cursor.close()
    db_conn.close()


if __name__ == '__main__':
    # main()
    test_profile()