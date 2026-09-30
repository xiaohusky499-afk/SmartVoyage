"""
需求：用 Python 执行 data_init/ 下的 .sql 脚本
默认按顺序跑两个文件：create_all_tables.sql（建库建表）→ insert_may01_data.sql（灌数据）

思路：
1. 读取 .sql 文件，剥掉注释后按分号切成一条条独立语句
2. 用 root 连接 MySQL（不指定 database，因为脚本自己要建库）
3. 多个文件复用同一条连接，逐条 execute，打印每条语句的执行情况
4. 收尾查一次 SHOW TABLES + 每张表的行数，确认建表和灌数据都对得上

⚠️ create_all_tables.sql 第一句是 DROP DATABASE IF EXISTS travel_rag，
   会把整个库连数据一起删掉，且不可恢复。所以本脚本默认要交互确认，
   确认过再执行；非交互批跑时用 --yes 跳过。

⚠️ 跑之前先把业务应用停掉。原因见下面第 3 步的 LOCK_WAIT_TIMEOUT 注释：
   应用连接池里挂着未提交的连接时，DROP DATABASE 会被元数据锁挡住，
   而本机等锁上限是一年，表现出来就是"脚本卡住不动"。

用法（在仓库根目录）：
    # 建表 + 灌数据，一条龙
    PYTHONPATH=. python SmartVoyage/docker/data_init/run_sql_file.py

    # 只跑指定文件（可以给多个，按给定顺序执行）
    PYTHONPATH=. python SmartVoyage/docker/data_init/run_sql_file.py insert_may01_data.sql

    # 非交互批跑，跳过确认
    PYTHONPATH=. python SmartVoyage/docker/data_init/run_sql_file.py --yes
"""

import os
import sys

import mysql.connector

# todo: 第0步：要三层向上才是仓库根目录
#       data_init -> docker -> SmartVoyage -> prepare_smart_voyage（根）
#       和 init_tour_group_rag.py 保持同一套路径写法
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..')))

from SmartVoyage.config import Config

conf = Config()

# 脚本所在目录，.sql 就在旁边，用绝对路径拼，避免依赖当前工作目录
HERE = os.path.dirname(os.path.abspath(__file__))

# todo: 第1步：默认按顺序跑两个文件，顺序不能反
# create_all_tables.sql 会 DROP DATABASE 重建整个库，
# 所以它必须在灌数据之前跑 —— 反过来的话刚插的数据会被连库一起删掉。
DEFAULT_SQL_FILES = [
    'create_all_tables.sql',    # 建库 + 建 9 张表
    'insert_may01_data.sql',    # 灌 5 月 1 日假期的演示数据
]

# todo: 第2步：为什么不用 config.py 里的 smart_yoyage
# conf.user 是业务账号，只有 travel_rag 库内的权限，
# 而建表脚本头两句是 DROP DATABASE / CREATE DATABASE —— 库级操作，业务账号做不了，
# 会报 Access denied。所以这里单独用 root，密码取 docker-compose.yml 的 MYSQL_ROOT_PASSWORD。
ROOT_USER = 'root'
ROOT_PASSWORD = os.getenv('MYSQL_ROOT_PASSWORD', '')

# todo: 第3步：这一行是"脚本卡住不返回"的解药
# 现象：跑 create_all_tables.sql 时，第一条 DROP DATABASE 就没有下文，一直挂着。
# 原因：本机 MySQL 的 @@lock_wait_timeout 是 31536000 秒（整整一年），
#      而 @@autocommit 是 0。业务应用的连接池里
#      只要有连接 SELECT 过这些表却没 commit，它就一直握着表的元数据锁（MDL）。
#      DROP DATABASE 要拿排他 MDL，只能排队等 —— 等一年，看起来就是"卡死"。
# 解法：把本会话的等锁上限压到 10 秒，拿不到就直接报错退出（见第 9 步的 1205 处理），
#      让人知道"去把应用停掉"，而不是干等到天亮还以为脚本写错了。
LOCK_WAIT_TIMEOUT = 10


def split_statements(sql_text: str) -> list:
    """
    把整份 .sql 文本切成一条条可单独执行的语句

    参数：
        sql_text (str): .sql 文件的全部内容

    返回值：
        list: 语句列表，已去掉注释和空白

    todo: 第4步：为什么不能直接 sql_text.split(';')
    因为分号可能出现在字符串字面量里（比如 COMMENT='含;分号的说明'），
    粗暴 split 会把一条语句劈成两半。这里做一个最小的状态机：
    逐字符扫描，记住"当前是否在引号内"，只有在引号外的分号才算语句边界。
    同理，-- 和 # 注释也只在引号外才算注释。
    """
    statements = []
    buf = []            # 当前语句的字符缓冲
    quote = None        # 当前所处的引号类型：' 或 " 或 ` ，None 表示在引号外
    i = 0
    n = len(sql_text)

    while i < n:
        ch = sql_text[i]

        # ---------- 引号内：只关心怎么出去 ----------
        if quote:
            buf.append(ch)
            if ch == '\\' and quote in ("'", '"'):
                # 反斜杠转义，把下一个字符原样吞掉，防止 \' 被误判为引号结束
                if i + 1 < n:
                    buf.append(sql_text[i + 1])
                    i += 2
                    continue
            elif ch == quote:
                quote = None
            i += 1
            continue

        # ---------- 引号外 ----------
        # 行注释：-- 或 #，直接跳到行尾
        if sql_text.startswith('--', i) or ch == '#':
            nl = sql_text.find('\n', i)
            i = n if nl == -1 else nl + 1
            continue

        # 块注释 /* ... */
        if sql_text.startswith('/*', i):
            end = sql_text.find('*/', i + 2)
            i = n if end == -1 else end + 2
            continue

        # 进入引号
        if ch in ("'", '"', '`'):
            quote = ch
            buf.append(ch)
            i += 1
            continue

        # 语句边界
        if ch == ';':
            stmt = ''.join(buf).strip()
            if stmt:
                statements.append(stmt)
            buf = []
            i += 1
            continue

        buf.append(ch)
        i += 1

    # 文件末尾可能最后一条语句没写分号
    tail = ''.join(buf).strip()
    if tail:
        statements.append(tail)

    return statements


def brief(stmt: str, width: int = 60) -> str:
    """把语句压成一行短摘要，用于打印进度"""
    one_line = ' '.join(stmt.split())
    return one_line if len(one_line) <= width else one_line[:width] + '...'


def confirm(plans: list) -> bool:
    """
    执行前的确认

    参数：
        plans (list): [(文件名, 语句列表), ...]，按执行顺序排列

    返回值：
        bool: True 表示继续执行

    todo: 第6步：破坏性操作要先问一句
    DROP DATABASE / DROP TABLE 是删数据，删完没有回退。
    所以先把所有文件里的破坏性语句汇总列给人看，确认了再动手。
    注意这里只问**一次**，不是每个文件问一遍 —— 建表和灌数据是一个整体，
    在中间叫停会留下一个建好表但没数据的空库，比压根没跑更难排查。
    """
    print(f"\n目标数据库：{conf.host}（账号 {ROOT_USER}）")
    print(f"即将按顺序执行 {len(plans)} 个文件：")

    destructive = []
    for name, statements in plans:
        hits = [s for s in statements
                if s.upper().startswith(('DROP DATABASE', 'DROP TABLE', 'TRUNCATE'))]
        destructive.extend(hits)
        print(f"  {name}：{len(statements)} 条语句，破坏性 {len(hits)} 条")

    if destructive:
        print(f"\n以下 {len(destructive)} 条语句会删除数据，且无法恢复：")
        for s in destructive:
            print(f"  - {brief(s)}")

    print("\n提示：跑之前请先停掉业务应用，否则 DROP DATABASE 会被元数据锁挡住（见第 3 步）")

    if '--yes' in sys.argv:
        print("已带 --yes，跳过确认")
        return True

    answer = input("\n确认执行？输入 yes 继续，其它任意输入取消：").strip().lower()
    return answer == 'yes'


def run_one_file(cursor, name: str, statements: list) -> tuple:
    """
    在已有连接上执行一个文件的所有语句

    参数：
        cursor: 已打开的 MySQL cursor
        name (str): 文件名，仅用于打印
        statements (list): 该文件切好的语句列表

    返回值：
        tuple: (成功条数, 失败条数)

    todo: 第8步：为什么多个文件共用一个 cursor
    因为 USE travel_rag 是**连接级**的状态。create_all_tables.sql 里的 USE
    选中了库，insert_may01_data.sql 开头虽然也有一句 USE，但只要连接不断，
    库的选择就一直有效。共用连接还省掉了重复握手。
    """
    ok, failed = 0, 0
    total = len(statements)
    print(f"\n===== {name}（{total} 条语句）=====")

    for idx, stmt in enumerate(statements, 1):
        try:
            cursor.execute(stmt)
            # SELECT/SHOW 之类会留着结果集不取走，下一条 execute 会报
            # "Unread result found"，所以主动清一遍
            if cursor.with_rows:
                cursor.fetchall()
            ok += 1
            # INSERT 语句报告影响行数，建表语句只报 OK
            suffix = ''
            if cursor.rowcount and cursor.rowcount > 0 and stmt.upper().startswith('INSERT'):
                suffix = f"（{cursor.rowcount} 行）"
            print(f"  [{idx}/{total}] OK   {brief(stmt)}{suffix}")
        except mysql.connector.Error as e:
            failed += 1
            print(f"  [{idx}/{total}] FAIL {brief(stmt)}")
            print(f"       → {e}")
            # todo: 第9步：拿不到锁就别硬扛
            # 1205 是 lock wait timeout。这说明有别的连接压着元数据锁，
            # 继续往下跑只会一条条全部超时，不如立刻抛出去让人先停应用。
            if e.errno == 1205:
                raise RuntimeError(
                    "拿不到表锁，说明还有连接占着这些表。"
                    "先停掉业务应用再重跑本脚本。"
                ) from e

    print(f"  小结：成功 {ok} 条，失败 {failed} 条")
    return ok, failed


def execute_sql_files(sql_paths: list):
    """
    连接 MySQL，按顺序执行多个 .sql 文件

    参数：
        sql_paths (list): .sql 文件的绝对路径列表，按执行顺序排列
    """
    # todo: 第5步：先把所有文件都读好切好，再动数据库
    # 万一第二个文件有语法问题或者根本读不出来，在这一步就能发现，
    # 而不是等库已经删掉了才发现 —— 那时就只剩一个空库了。
    plans = []
    for path in sql_paths:
        with open(path, encoding='utf-8') as f:
            statements = split_statements(f.read())
        if not statements:
            print(f"跳过 {os.path.basename(path)}：里面没有可执行的语句")
            continue
        plans.append((os.path.basename(path), statements))

    if not plans:
        print("没有任何可执行的语句")
        return

    if not confirm(plans):
        print("已取消，未执行任何语句")
        return

    # todo: 第7步：连接时不要指定 database
    # create_all_tables.sql 的第一句就是 DROP DATABASE travel_rag，
    # 如果连接时 database='travel_rag'，删库之后当前连接就悬空了，
    # 后面的 USE travel_rag 之前的语句会报 "No database selected"。
    # 不指定 database，让脚本里的 CREATE DATABASE / USE 自己接管。
    conn = mysql.connector.connect(
        host=conf.host,
        user=ROOT_USER,
        password=ROOT_PASSWORD,
        charset='utf8mb4',
        collation='utf8mb4_unicode_ci',
    )
    print(f"\n已连接 MySQL: {conf.host}")

    cursor = conn.cursor()
    # 见上面 LOCK_WAIT_TIMEOUT 的注释：把等锁上限压到 10 秒，避免"卡死"
    cursor.execute(f"SET SESSION lock_wait_timeout = {LOCK_WAIT_TIMEOUT}")
    cursor.execute(f"SET SESSION innodb_lock_wait_timeout = {LOCK_WAIT_TIMEOUT}")

    total_ok, total_failed = 0, 0

    try:
        for name, statements in plans:
            ok, failed = run_one_file(cursor, name, statements)
            total_ok += ok
            total_failed += failed

        # DDL 在 MySQL 里是隐式提交的，但 insert_may01_data.sql 全是 INSERT，
        # 而本机 @@autocommit 是 0，不 commit 数据就白插了
        conn.commit()
        print(f"\n全部执行完毕：成功 {total_ok} 条，失败 {total_failed} 条")

        report(cursor)
    finally:
        cursor.close()
        conn.close()


def report(cursor):
    """
    收尾自查

    todo: 第10步：别只看"没报错"
    建表脚本最典型的坑就是中间某条语法错误导致后面的表全没建出来
    （create_all_tables.sql 第 60-65 行的注释记着这个真实案例：
      少一个分号，9 张表只剩 3 张）。所以这里数一遍实际表数。
    加上灌数据以后还要看行数 —— 表建出来了但一行数据没有，
    对下游的 RAG 检索来说和没建一样。
    """
    # insert_may01_data.sql 只灌这 6 张表，另外 3 张（user_profiles /
    # query_history / short_term_messages）是运行时才写入的，空着是正常的
    DATA_TABLES = {'train_tickets', 'flight_tickets', 'concert_tickets',
                   'weather_data', 'car_rentals', 'insurances'}
    cursor.execute(f"SHOW TABLES FROM `{conf.database}`")
    tables = [row[0] for row in cursor.fetchall()]

    print(f"\n{conf.database} 现有 {len(tables)} 张表：")
    empty = []
    for t in tables:
        cursor.execute(f"SELECT COUNT(*) FROM `{conf.database}`.`{t}`")
        n = cursor.fetchone()[0]
        print(f"  - {t:24s} {n:6d} 行")
        if n == 0:
            empty.append(t)

    # 空表分两类：该有数据却空了（要查），和本来就该空的（正常）
    unexpected = [t for t in empty if t in DATA_TABLES]
    expected = [t for t in empty if t not in DATA_TABLES]

    if expected:
        print(f"\n以下表为空，属于正常：{'、'.join(expected)}")
        print("  它们由应用在运行时写入，灌数据脚本不碰")

    if unexpected:
        print(f"\n⚠️ 以下表本该有数据却是空的：{'、'.join(unexpected)}")
        print("  如果只跑了 create_all_tables.sql，补跑 insert_may01_data.sql 即可；")
        print("  如果两个都跑了，往上翻 FAIL 的语句看原因")


if __name__ == '__main__':
    # 位置参数给文件名（可以给多个），--yes 之类的开关跳过
    args = [a for a in sys.argv[1:] if not a.startswith('-')]
    sql_names = args if args else DEFAULT_SQL_FILES

    sql_paths = []
    for sql_name in sql_names:
        path = sql_name if os.path.isabs(sql_name) else os.path.join(HERE, sql_name)
        if not os.path.isfile(path):
            print(f"找不到文件：{path}")
            sys.exit(1)
        sql_paths.append(path)

    try:
        execute_sql_files(sql_paths)
    except mysql.connector.Error as e:
        # 顶层兜住连接类错误，打成可读文本而不是抛栈
        print(f"\n数据库连接失败：{e}")
        print("检查一下：容器起了吗（docker-compose ps）、3306 是否被本机 MySQL 占用、root 密码对不对")
    except Exception as e:
        print(f"\n执行出错：{e}")
