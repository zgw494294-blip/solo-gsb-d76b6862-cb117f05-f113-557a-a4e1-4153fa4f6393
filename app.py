"""SQLite 事务隔离实验室 —— Flask 后端。

每个实验对应一个独立的 WAL 数据库（单行整数值 100）。
页面左右两栏（甲 / 乙）各绑定一条长期独立连接，事务完全由显式 SQL 控制；
第三条连接只读已提交值。状态刷新不执行任何 SELECT，
避免在 DEFERRED 事务中提前建立读快照。

写操作同样遵守这一约束：事务尚未建立读快照时，「加上」的边界检查
基于 observer 连接看到的已提交值在 Python 中完成，UPDATE 本身再以
checked_add 函数做最终精确判定，绝不先在本连接上 SELECT —— 否则
首次写锁冲突（SQLITE_BUSY）后本事务会留下读快照，对方提交释放写锁后
重试即被误报为 SQLITE_BUSY_SNAPSHOT。

每栏另有一个独立的只读开关（默认关闭）：开启即在**该栏原长期连接**上
真实执行 PRAGMA query_only=1，不换连接、不结束当前事务，也不先读任何值
（不建立读快照）；关闭执行 PRAGMA query_only=0。开启后读值、BEGIN、
COMMIT、ROLLBACK 都照常（COMMIT 仍会提交此前写入，ROLLBACK 仍撤销），
加法按钮也保持可用，但事务内未越界的加法会被 SQLite 如实拒绝为
SQLITE_READONLY(8)，语句失败不撤销此前修改；关闭后可在原事务继续加法。
提交 / 回滚不重置开关，开关状态只保存在内存并随状态接口返回。两栏开关
互相独立，乙的写锁冲突仍如实报 SQLITE_BUSY(5)。

WAL 检查点（PASSIVE / TRUNCATE）不由甲、乙两栏的连接执行，而是每次
用一条全新的独立连接真实执行 PRAGMA wal_checkpoint，busy_timeout=0：
读事务阻止回收时立即返回 busy=1，不等待。各写连接均已关闭自动检查点
（PRAGMA wal_autocheckpoint=0），检查点时机完全由手动按钮 / HTTP 控制。
检查点绝不代替两栏提交、回滚，也不替换连接；状态刷新只读取内存中
最近一次检查点结果的缓存，不执行任何 SQL、不触发检查点。

提交时间线按事务汇总：仅「至少一次加法成功且 COMMIT 成功」的事务产生
一条记录，按实际提交顺序从 1 编号，含栏位、首次成功加法前的值、提交后
的值与按执行顺序排列的成功增量（沿用整数无损编码）。首次成功加法前的值
由 checked_add 在 UPDATE 语句内捕获（事务自身视图），提交后的值 =
该值 + 全部成功增量之和（首次成功加法已持有写锁直至提交，期间无其他
连接能修改该行），绝不事后读取别栏新值作为本次结果。失败加法不计入；
回滚、失败提交、纯读事务不记录；未提交内容不可见；提交重试成功只记
一次。COMMIT 与记录追加在同一临界区（timeline_lock）完成，编号顺序与
实际提交顺序严格一致。采集、查询（GET /api/experiments/<id>/timeline）
与轮询（/status 的 timeline 字段）都只读内存，不在两栏连接上额外读值、
不提前建立快照，也不改变最近一次显式读值；时间线无需跨重启保存。

删除实验（DELETE /api/experiments/<id>）先在内存门控上标记删除开始，
并等待此前已进入的请求全部正常完成；删除开始后该实验的操作、状态、
已提交值与检查点新请求一律返回 410。等待按实验独立，不阻塞其他实验。
之后才回滚两栏未提交事务、关闭甲、乙长期连接与 observer，再移除
数据库及 -wal、-shm 文件——此时不存在在途请求或在途检查点连接，
检查点不可能在删除后重新创建文件。本次运行中已删除 ID 仍返回 410，
未知 ID 返回 404。

脚本逐步执行：页面与 HTTP 可装载 1～50 步的 JSON 数组（POST
/api/experiments/<id>/script），每步含 side（a/b）与 op
（begin/read/add/commit/rollback），add 另含 value，沿用与手工加法
完全相同的整数校验（parse_int、±2⁶² 上限、大整数十进制字符串）。
整份校验、非法整份拒绝（400），任何已存在脚本都不受影响。仅当两栏
均无事务且当前没有有效（active）脚本时才能装载——装载在 script_lock
内同时持两栏 conn_lock 复查 in_transaction，与手工 BEGIN 严格互斥。
脚本只保存在内存，生成独立脚本 ID；查询（GET …/script 及 /status 的
script 字段）只读内存，返回脚本 ID、从 1 开始的当前步号、全部步骤与
逐次执行记录，绝不在两栏连接上额外读值。

脚本有效（active）期间，后端拒绝两栏的全部手工事务操作与只读切换
（409）；查询、已提交值、时间线、检查点与删除仍照常可用。推进
（…/script/advance）在该步指定栏的**原长期连接**上调用同一个 run_op
真实执行：成功才把步号前移；失败不代为回滚、步号停在原处，可重试或
跳过（…/script/skip）。每次尝试都追加一条记录：步号、栏位、操作、
增量、success/failure 结果与原始 SQLite 错误名 / 错误码（拒绝类错误
名 / 码为 null）；跳过单独记录 skipped，不执行任何 SQL。脚本走完全部
步骤置为 finished，或经 …/script/cancel 取消置为 cancelled 后，手工
操作立即恢复；系统既不自动提交也不自动回滚，脚本结束时仍开启的事务
原样保留（因此若要再装载脚本，需先手工提交 / 回滚）。

推进、跳过、取消都必须携带脚本 ID 与所见步号（{script_id, step}，
类型不符 400）；同一实验的脚本动作经 script_lock 串行核对并执行：
脚本 ID 与当前不符（旧脚本的过期请求，包括已被新脚本替换后）、脚本
已结束 / 取消、步号与当前步不一致，一律 409，不执行、不产生记录、
不影响新脚本。
"""

import os
import re
import shutil
import sqlite3
import threading
import time
import uuid

from flask import Flask, jsonify, render_template, request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("LAB_DATA_DIR", os.path.join(BASE_DIR, "data"))
MAX_LOG_ENTRIES = 200
MAX_ADD_ABS = 2 ** 62  # 单次增量上限
INT64_MIN = -(2 ** 63)  # SQLite 整数下限，累加结果不得越界（含负向）
INT64_MAX = 2 ** 63 - 1  # SQLite 整数上限，越界会被 SQLite 静默转成 REAL
OVERFLOW_MESSAGE = "累加结果超出有符号 64 位整数范围，本次未修改"
JS_SAFE_INT_MAX = 2 ** 53 - 1  # Number.MAX_SAFE_INTEGER，超出则用十进制字符串传输

INT_PATTERN = re.compile(r"[+-]?[0-9]+")

VALID_OPS = {"begin", "read", "add", "commit", "rollback"}
MAX_SCRIPT_STEPS = 50  # 单个脚本的步数上限：1～50 步
OP_LABELS = {
    "begin": "BEGIN DEFERRED",
    "read": "读值",
    "commit": "COMMIT",
    "rollback": "ROLLBACK",
}

# 允许手动触发的检查点模式：SQL 片段直接拼入 PRAGMA，必须走白名单
VALID_CHECKPOINT_MODES = {"PASSIVE", "TRUNCATE"}
WAL_SUFFIX = "-wal"
SHM_SUFFIX = "-shm"

app = Flask(__name__)


def json_int(value):
    """JSON 无损编码整数：安全整数范围内返回数字，超出则用十进制字符串。"""
    if value is None:
        return None
    if -JS_SAFE_INT_MAX <= value <= JS_SAFE_INT_MAX:
        return value
    return str(value)


def parse_int(raw):
    """解析增量：接受 JSON 整数或十进制字符串（大整数以字符串传输），否则返回 None。"""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str) and INT_PATTERN.fullmatch(raw.strip()):
        return int(raw.strip())
    return None


class PanelState:
    """某一栏的展示状态。last_read / log 记录在内存中，刷新状态不触碰 SQLite。"""

    def __init__(self):
        self.last_read = None
        self.log = []
        # 当前事务是否已建立读快照：显式读值后为 True，
        # 成功写入后也为 True；BEGIN 与 COMMIT/ROLLBACK 后重置。
        self.tx_has_snapshot = False
        # 本栏只读开关（PRAGMA query_only）：默认关闭，只保存在内存。
        # 开关在原长期连接上生效，不换连接、不结束事务；
        # COMMIT / ROLLBACK 不重置它，两栏各自独立。
        self.readonly = False
        # 当前事务内已成功、尚未提交的加法：[(checked_add 捕获的行值, 增量), ...]
        # 仅供提交时间线使用：COMMIT 成功才生成记录；ROLLBACK / 隐式回滚丢弃；
        # 不随状态接口返回（未提交内容不可见）。
        self.tx_adds = []

    def add_log(self, text):
        self.log.append("%s  %s" % (time.strftime("%H:%M:%S"), text))
        if len(self.log) > MAX_LOG_ENTRIES:
            del self.log[: len(self.log) - MAX_LOG_ENTRIES]


class Experiment:
    def __init__(self, exp_id):
        self.id = exp_id
        self.db_path = os.path.join(DATA_DIR, "exp_%s.db" % exp_id)
        self._init_db()
        # 两栏各自的长期连接：isolation_level=None 进入自动提交模式，
        # 事务完全由显式 BEGIN/COMMIT/ROLLBACK 控制；
        # busy_timeout=0 让写锁冲突立即报错而不是等待。
        # 两个写连接均关闭自动检查点（wal_autocheckpoint=0），
        # 使 WAL 回收只由手动 PASSIVE / TRUNCATE 检查点触发。
        # overflow_flags 供连接上的 checked_add 函数在判定 64 位溢出时置位，
        # 以便把溢出与普通 SQLite 错误区分开，沿用既有的拒绝响应。
        # add_base_slots 供 checked_add 在 UPDATE 语句内暂存本次成功加法
        # 基于的行值（事务自身视图），run_op 在语句成功后读槽记入本事务
        # 待提交增量，供提交时间线使用；全程不执行任何额外读取。
        self.overflow_flags = {"a": [False], "b": [False]}
        self.add_base_slots = {"a": [None], "b": [None]}
        self.conns = {
            "a": self._open_conn(
                self.overflow_flags["a"], self.add_base_slots["a"], autockpt=False
            ),
            "b": self._open_conn(
                self.overflow_flags["b"], self.add_base_slots["b"], autockpt=False
            ),
        }
        self.conn_locks = {"a": threading.Lock(), "b": threading.Lock()}
        # 第三条连接：只读已提交值
        self.observer = self._open_conn()
        self.observer_lock = threading.Lock()
        self.panels = {"a": PanelState(), "b": PanelState()}
        # 检查点状态只保存在内存：状态刷新只读这份缓存，不触发检查点
        self.checkpoint_lock = threading.Lock()
        self.checkpoint_last = None
        self.checkpoint_log = []
        # 删除准入控制：gate 同时保护 active_calls / deleting / deleted。
        # 删除开始时先置 deleting 并等待已准入请求（active_calls）全部完成，
        # 此后新请求一律 410；拆除连接与文件期间不可能有请求触碰它们。
        # 门控按实验独立，等待删除绝不阻塞其他实验。
        self.gate = threading.Condition(threading.Lock())
        self.active_calls = 0
        self.deleting = False
        self.deleted = False
        # 提交时间线：只保存在内存，无需跨重启保留。
        # timeline_lock 保证「COMMIT 成功 → 追加记录」在同一临界区完成，
        # 记录编号顺序与实际提交顺序严格一致；查询只读内存快照，
        # 不触碰甲、乙长期连接与 observer。
        self.timeline_lock = threading.Lock()
        self.timeline = []
        # 脚本逐步执行状态：只保存在内存，无需跨重启保留。
        # script_lock 串行化「装载 / 推进 / 跳过 / 取消」与手工操作的
        # active 互斥复查；同实验的脚本动作先在该锁上核对脚本 ID 与步号，
        # 再真实执行，过期请求一律 409，不会触碰到新脚本。
        # 手工操作 / 只读切换路由在「复查 + 执行」整段持有这把锁，
        # 锁内读标志只能用 _script_active_locked()——threading.Lock
        # 不可重入，锁内再次获取会让请求永久卡住，并占住准入名额，
        # 使随后的脚本查询、推进与删除一并阻塞。
        self.script_lock = threading.Lock()
        self.script = None  # None 或 {id, steps, cursor, records, status}

    def _init_db(self):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                "CREATE TABLE kv (id INTEGER PRIMARY KEY CHECK (id = 1),"
                " val INTEGER NOT NULL)"
            )
            conn.execute("INSERT INTO kv (id, val) VALUES (1, 100)")
            conn.commit()
        finally:
            conn.close()

    def _open_conn(self, overflow_flag=None, add_base_slot=None, autockpt=True):
        conn = sqlite3.connect(
            self.db_path, timeout=0, isolation_level=None, check_same_thread=False
        )
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=0")
        if not autockpt:
            # 关闭本连接提交时的自动检查点：检查点时机完全交由
            # 手动 PASSIVE / TRUNCATE 按钮（HTTP）控制。
            conn.execute("PRAGMA wal_autocheckpoint=0")
        if overflow_flag is not None:
            # checked_add 在 UPDATE 内部做最终的 64 位边界判定。
            # SQLite 对整数溢出不报错而会静默存成 REAL，必须在写入前拦下：
            # 溢出时置位并抛异常终止本次语句，行不会被修改，事务保持开启。
            # 函数只做纯计算，不执行任何读取，不会影响快照与加锁顺序。
            def checked_add(current, delta):
                result = current + delta
                if not INT64_MIN <= result <= INT64_MAX:
                    overflow_flag[0] = True
                    raise OverflowError("累加结果超出有符号 64 位整数范围")
                if add_base_slot is not None:
                    # 暂存本次成功加法基于的行值（UPDATE 语句内的事务
                    # 自身视图），供提交时间线记录「首次成功加法前的值」；
                    # 纯计算不执行任何读取，不影响快照与加锁顺序。
                    add_base_slot[0] = current
                return result

            conn.create_function("checked_add", 2, checked_add)
        return conn

    def panel_state(self, side):
        # 只读取内存状态与连接标志位，绝不执行 SELECT，
        # 避免为 DEFERRED 事务提前建立读快照。
        panel = self.panels[side]
        return {
            "in_transaction": self.conns[side].in_transaction,
            "readonly": panel.readonly,
            "last_read": json_int(panel.last_read),
            "log": list(panel.log),
        }

    def committed_value(self):
        with self.observer_lock:
            row = self.observer.execute(
                "SELECT val FROM kv WHERE id = 1"
            ).fetchone()
            return row[0]

    def append_timeline(self, side, tx_adds):
        """COMMIT 成功后追加一条时间线记录；调用方必须持有 timeline_lock，
        使「COMMIT → 追加」原子完成，记录顺序与实际提交顺序一致。

        before 取自本事务首次成功加法时 checked_add 在 UPDATE 语句内捕获的
        行值（事务自身视图）；after = before + 全部成功增量之和——首次成功
        加法已持有写锁直至提交，期间无其他连接能修改该行，因此提交后的值
        无需、也绝不允许事后读取别栏新值得到。
        """
        before = tx_adds[0][0]
        after = before
        deltas = []
        for _base, delta in tx_adds:
            after += delta
            deltas.append(delta)
        self.timeline.append(
            {
                "seq": len(self.timeline) + 1,
                "side": side,
                "before": json_int(before),
                "after": json_int(after),
                "deltas": [json_int(delta) for delta in deltas],
            }
        )

    def timeline_records(self):
        # 只读内存中的时间线：不执行任何 SQL、不触碰两栏连接与 observer，
        # 不提前建立快照，也不改变最近一次显式读值
        with self.timeline_lock:
            return [dict(record) for record in self.timeline]

    @staticmethod
    def validate_steps(raw):
        """整份校验脚本：必须是 1～50 步的 JSON 数组。

        每步必须是对象，含 side ∈ {a,b} 与 op ∈ VALID_OPS；add 另含
        合法整数 value（沿用手工加法的 parse_int 与 ±2⁶² 上限，大整数
        以十进制字符串接受）。任何不合法都返回错误消息，调用方以 400
        整份拒绝——绝不部分装载。
        """
        if not isinstance(raw, list):
            return "body must be a JSON array of steps"
        if not raw:
            return "script must contain 1..%d steps" % MAX_SCRIPT_STEPS
        if len(raw) > MAX_SCRIPT_STEPS:
            return "script must contain at most %d steps" % MAX_SCRIPT_STEPS
        for index, step in enumerate(raw):
            where = "step %d" % (index + 1)
            if not isinstance(step, dict):
                return "%s must be an object" % where
            side = step.get("side")
            if side not in ("a", "b"):
                return "%s: side must be 'a' or 'b'" % where
            op = step.get("op")
            if op not in VALID_OPS:
                return "%s: op must be one of begin/read/add/commit/rollback" % where
            if op == "add":
                value = parse_int(step.get("value"))
                if value is None:
                    return "%s: value must be an integer" % where
                if abs(value) > MAX_ADD_ABS:
                    return "%s: value too large" % where
        return None

    def load_script(self, raw):
        """装载新脚本。调用方已完成整份校验。

        仅当两栏均无事务且没有 active 脚本时才允许；在 script_lock 内
        同时持两栏 conn_lock 复查 in_transaction，与手工 BEGIN 严格互斥，
        不会出现「检查时无事务、装载时已 BEGIN」的窗口。成功后旧脚本的
        查看记录被新脚本替换，旧脚本的在途动作因 ID 不符一律 409。
        """
        with self.script_lock:
            if self.script is not None and self.script["status"] == "active":
                return None, {
                    "error": "an active script already exists; cancel it first"
                }
            with self.conn_locks["a"], self.conn_locks["b"]:
                if self.conns["a"].in_transaction or self.conns["b"].in_transaction:
                    return None, {
                        "error": "both connections must be out of transaction "
                        "before loading a script"
                    }
                self.script = {
                    "id": uuid.uuid4().hex[:12],
                    # add 的 value 规范化为 Python int（大整数十进制字符串
                    # 同样解析），保证 run_op 拿到的与手工接口完全一致
                    "steps": [
                        dict(step, value=parse_int(step["value"]))
                        if step["op"] == "add"
                        else dict(step)
                        for step in raw
                    ],
                    "cursor": 0,  # 尚未执行的步骤下标（从 0 开始；步号 = cursor + 1）
                    "records": [],
                    "status": "active",  # active / finished / cancelled
                }
                return self.script_view(self.script), None

    def _script_record(self, step_no, step, result, skipped=False):
        """构造一条执行记录。result 为 run_op 的返回（跳过则为 None）。

        value 仅 add 出现，沿用 json_int 无损编码；失败如实记录 run_op
        给出的原始错误名 / 错误码（拒绝类为 null）；跳过不执行任何 SQL。
        """
        record = {
            "step": step_no,
            "side": step["side"],
            "op": step["op"],
        }
        if step["op"] == "add":
            record["value"] = json_int(step["value"])
        if skipped:
            record["result"] = "skipped"
            record["error"] = None
        else:
            record["result"] = "success" if result["ok"] else "failure"
            record["error"] = None if result["ok"] else dict(result["error"])
        return record

    def _script_step_public(self, step_no, step):
        item = {"step": step_no, "side": step["side"], "op": step["op"]}
        if step["op"] == "add":
            item["value"] = json_int(step["value"])
        return item

    def script_view(self, script):
        """脚本视图（只读内存，绝不执行任何 SQL、不在两栏连接上读值）。

        active 时 step 为从 1 开始的当前（下一步）步号；finished 全部
        走完，step 为 None；cancelled 在取消的步号处停止（该步未执行）。
        """
        if script is None:
            return None
        total = len(script["steps"])
        if script["status"] == "active":
            step = script["cursor"] + 1
        elif script["status"] == "cancelled":
            step = min(script["cursor"] + 1, total)
        else:
            step = None
        return {
            "id": script["id"],
            "status": script["status"],
            "step": step,
            "total": total,
            "steps": [
                self._script_step_public(index + 1, step)
                for index, step in enumerate(script["steps"])
            ],
            "records": [dict(record) for record in script["records"]],
        }

    def script_state(self):
        with self.script_lock:
            return self.script_view(self.script)

    def _check_script_action(self, script, script_id, step_no):
        """在 script_lock 内核对推进 / 跳过 / 取消请求。

        无脚本、脚本 ID 与当前不符（旧脚本的过期请求，含已被新脚本替换）、
        脚本已结束 / 取消、步号不是当前步，一律视为过期 / 无效请求；
        返回错误字符串（HTTP 409），不执行、不记录、不影响新脚本。
        """
        if script is None or script["id"] != script_id:
            return "script id is stale; current script differs"
        if script["status"] != "active":
            return "script is %s; no more actions accepted" % script["status"]
        if step_no != script["cursor"] + 1:
            return "stale step: current step is %d" % (script["cursor"] + 1)
        return None

    def advance_script(self, script_id, step_no):
        """在原连接真实执行当前步。成功才前移；失败停在原步，可重试 / 跳过。"""
        with self.script_lock:
            script = self.script
            stale = self._check_script_action(script, script_id, step_no)
            if stale is not None:
                return self.script_view(script), False, {"error": stale}
            step = script["steps"][script["cursor"]]
            # run_op 只取该栏自己的 conn_lock 与 timeline_lock，
            # 与已持有的 script_lock 不构成环；真实在该栏原长期连接执行。
            result = run_op(self, step["side"], step["op"], step.get("value"))
            script["records"].append(
                self._script_record(step_no, step, result)
            )
            if result["ok"]:
                # 成功才前移；失败不代为回滚，事务与步号都停在原处
                script["cursor"] += 1
                if script["cursor"] >= len(script["steps"]):
                    script["status"] = "finished"
            # 失败：原样保留错误名 / 码，不自动提交或回滚
            return self.script_view(script), result["ok"], None

    def skip_script(self, script_id, step_no):
        """跳过当前步：只记 skipped，不执行任何 SQL、不改变事务状态。"""
        with self.script_lock:
            script = self.script
            stale = self._check_script_action(script, script_id, step_no)
            if stale is not None:
                return self.script_view(script), False, {"error": stale}
            step = script["steps"][script["cursor"]]
            script["records"].append(
                self._script_record(step_no, step, None, skipped=True)
            )
            script["cursor"] += 1
            if script["cursor"] >= len(script["steps"]):
                script["status"] = "finished"
            return self.script_view(script), True, None

    def cancel_script(self, script_id, step_no):
        """取消脚本：取消的步号处停止；不提交、不回滚，手工操作随即恢复。"""
        with self.script_lock:
            script = self.script
            stale = self._check_script_action(script, script_id, step_no)
            if stale is not None:
                return self.script_view(script), False, {"error": stale}
            script["status"] = "cancelled"
            return self.script_view(script), True, None

    def script_active(self):
        # 手工事务操作与只读切换的准入检查（script_lock 内读标志）
        with self.script_lock:
            return self._script_active_locked()

    def _script_active_locked(self):
        # 调用方必须已持有 script_lock。手工操作 / 只读切换路由在
        # 「复查 + 执行」整段持有同一把锁（见路由注释），这里绝不能再
        # 获取：threading.Lock 不可重入，重复获取会让请求永久卡住，
        # 占住准入名额并阻塞随后的脚本查询、推进与删除。
        return self.script is not None and self.script["status"] == "active"

    def checkpoint_state(self):
        # 只读内存缓存：不执行任何 SQL、不触发检查点
        return {"last": self.checkpoint_last, "log": list(self.checkpoint_log)}

    def add_checkpoint_log(self, text):
        self.checkpoint_log.append("%s  %s" % (time.strftime("%H:%M:%S"), text))
        if len(self.checkpoint_log) > MAX_LOG_ENTRIES:
            del self.checkpoint_log[: len(self.checkpoint_log) - MAX_LOG_ENTRIES]

    def wal_size(self):
        # 操作后读取 WAL 文件字节数；文件不存在时记 0
        try:
            return os.path.getsize(self.db_path + WAL_SUFFIX)
        except OSError:
            return 0

    def admit_call(self):
        """请求准入：实验存活则计入 active_calls 并返回 True。

        删除开始后（deleting/deleted）所有新请求一律拒绝，由调用方返回 410。
        """
        with self.gate:
            if self.deleting or self.deleted:
                return False
            self.active_calls += 1
            return True

    def finish_call(self):
        # 请求结束（含异常）：归还名额并唤醒删除者。调用方在 finally 中调用，
        # 此时已退出 run_op / run_checkpoint 的资源临界区（conn_lock、
        # observer_lock、checkpoint_lock 均已释放），因此这里只触碰门控，
        # 不会与 teardown 取资源锁形成锁序环。
        with self.gate:
            self.active_calls -= 1
            if self.active_calls <= 0:
                self.gate.notify_all()

    def begin_delete(self):
        """等待此前已进入的请求全部正常完成，并把删除标记为已完成。

        必须在 claim_or_wait_delete 返回 True 之后调用。等待期间其他实验的
        请求完全不受影响（各自持有自己的 gate）。结束时必须 notify_all，
        唤醒所有在 claim 之后、完成之前进入等待的并发 DELETE。
        """
        with self.gate:
            while self.active_calls > 0:
                self.gate.wait()
            self.deleted = True
            self.gate.notify_all()

    def claim_or_wait_delete(self):
        """在同一段门控临界区内争取删除权或等待删除完成。

        必须原子完成「检查 deleting → 标记 / 进入等待」，否则失败者在
        claim 与 wait 之间释放门控，获胜者可能已收尾并 notify_all，
        迟到的等待者将永久阻塞。

        返回 True 表示本请求获得删除执行权（已标记 deleting）；
        返回 False 表示删除已结束（或在等待期间结束），调用方应返回 410。
        """
        with self.gate:
            if not self.deleting:
                self.deleting = True
                self.gate.notify_all()
                return True
            while not self.deleted:
                self.gate.wait()
            return False

    def teardown(self):
        """删除的拆除阶段：必须在 begin_delete 之后调用。

        先回滚两栏未提交事务，再依次关闭甲、乙长期连接与 observer；
        全部关闭后才移除数据库及 WAL、SHM 文件。此时不可能有在途请求
        或在途检查点连接，因此检查点不会在删除后重新创建文件。
        """
        for side in ("a", "b"):
            with self.conn_locks[side]:
                conn = self.conns[side]
                try:
                    if conn.in_transaction:
                        # 系统只在此时代为回滚未提交事务
                        conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                conn.close()
        with self.observer_lock:
            self.observer.close()
        for suffix in ("", WAL_SUFFIX, SHM_SUFFIX):
            path = self.db_path + suffix
            try:
                os.remove(path)
            except FileNotFoundError:
                pass


def run_op(exp, side, op, value):
    """在指定栏的长期连接上真实执行一个操作，返回 {ok, error, state}。"""
    panel = exp.panels[side]
    conn = exp.conns[side]
    label = "加上 %s" % value if op == "add" else OP_LABELS.get(op, op)

    def reject(message):
        panel.add_log("%s → 拒绝：%s" % (label, message))
        return {
            "ok": False,
            "error": {"name": None, "code": None, "message": message},
            "state": exp.panel_state(side),
        }

    with exp.conn_locks[side]:
        try:
            if op == "begin":
                if conn.in_transaction:
                    return reject("事务已开启，不能重复 BEGIN")
                conn.execute("BEGIN DEFERRED")
                # 新事务从空的待记录增量开始（正常已为空，防御性重置）
                panel.tx_adds = []
                panel.tx_has_snapshot = False
                panel.add_log("BEGIN DEFERRED → 成功，事务已开启")
            elif op == "read":
                row = conn.execute("SELECT val FROM kv WHERE id = 1").fetchone()
                panel.last_read = row[0]
                panel.tx_has_snapshot = conn.in_transaction
                scope = "事务内" if conn.in_transaction else "事务外（自动提交）"
                panel.add_log("读值（%s）→ %s" % (scope, row[0]))
            elif op == "add":
                if not conn.in_transaction:
                    return reject("禁止事务外修改：请先 BEGIN DEFERRED")
                overflow_message = OVERFLOW_MESSAGE
                if panel.tx_has_snapshot:
                    # 本事务已有读快照（显式读值或此前写过）：
                    # 直接在事务内读当前值做精确边界检查，行为与单事务内
                    # 可见性完全一致。快照过时导致的写冲突仍由 SQLite
                    # 如实报 SQLITE_BUSY_SNAPSHOT，不做任何自动处理。
                    current = conn.execute(
                        "SELECT val FROM kv WHERE id = 1"
                    ).fetchone()[0]
                    if not INT64_MIN <= current + value <= INT64_MAX:
                        return reject(overflow_message)
                else:
                    # 关键路径：事务尚无读快照（BEGIN 后第一次操作就是写）。
                    # 绝不能先在本连接 SELECT —— 那会建立读快照：若对方正
                    # 持有写锁，本次 UPDATE 拿到 SQLITE_BUSY 后快照已留下，
                    # 对方提交后重试就会被误报为 SQLITE_BUSY_SNAPSHOT。
                    # 这里用 observer 看到的已提交值做无锁预判：仅当
                    # 「已提交值 + 增量」本身就越界时直接拒绝，从而保持
                    # 溢出拒绝不获取写锁、不影响另一栏的原有行为。
                    committed = exp.committed_value()
                    if not INT64_MIN <= committed + value <= INT64_MAX:
                        return reject(overflow_message)
                # 最终精确判定放在 UPDATE 内的 checked_add：
                # 无快照预判与真实写入之间若对方恰好提交了别的值，
                # 仍以语句内计算为准，保证永远不会静默写成 REAL。
                # 纯写语句在写锁被占时返回 SQLITE_BUSY 且不建立读快照，
                # 因此对方提交/回滚释放写锁后可在同一事务直接重试成功。
                exp.overflow_flags[side][0] = False
                exp.add_base_slots[side][0] = None
                conn.execute(
                    "UPDATE kv SET val = checked_add(val, ?) WHERE id = 1",
                    (value,),
                )
                # checked_add 已在 UPDATE 语句内捕获本次加法基于的行值
                # （事务自身视图），记入本事务待提交的增量列表，供提交
                # 时间线使用；不执行任何额外读取，不影响快照与加锁顺序。
                base = exp.add_base_slots[side][0]
                if base is not None:
                    panel.tx_adds.append((base, value))
                panel.tx_has_snapshot = True
                panel.add_log("加上 %s → 成功（尚未提交）" % value)
            elif op == "commit":
                if not conn.in_transaction:
                    return reject("当前没有开启的事务")
                if panel.tx_adds:
                    # 本事务有成功加法：COMMIT 与记录追加在同一临界区完成，
                    # 时间线编号顺序与实际提交顺序严格一致；COMMIT 失败时
                    # 异常直接抛出，不记录、tx_adds 保留，事务仍开启，
                    # 之后重试 COMMIT 成功只记录一次。
                    with exp.timeline_lock:
                        conn.execute("COMMIT")
                        exp.append_timeline(side, panel.tx_adds)
                    panel.tx_adds = []
                else:
                    # 纯读事务（或只有失败加法）：照常提交，不产生记录
                    conn.execute("COMMIT")
                panel.tx_has_snapshot = False
                panel.add_log("COMMIT → 成功，事务已提交")
            elif op == "rollback":
                if not conn.in_transaction:
                    return reject("当前没有开启的事务")
                conn.execute("ROLLBACK")
                # 回滚不产生时间线记录，丢弃本事务待记录的增量
                panel.tx_adds = []
                panel.tx_has_snapshot = False
                panel.add_log("ROLLBACK → 成功，事务已回滚")
            else:
                return reject("未知操作：%s" % op)
        except sqlite3.Error as exc:
            # 个别错误会让 SQLite 隐式回滚整个事务：若事务已不存在，
            # 丢弃待记录的增量，已被撤销的写入绝不进入时间线
            if not conn.in_transaction:
                panel.tx_adds = []
            # checked_add 置位溢出标志时，这不是锁冲突而是越界拒绝：
            # 语句已终止、行未修改、事务保持开启，沿用既有拒绝响应。
            if exp.overflow_flags[side][0]:
                exp.overflow_flags[side][0] = False
                return reject(overflow_message)
            # 写锁冲突等 SQLite 错误：如实上报错误名与错误码，
            # 不做任何自动回滚，失败的事务保持开启。
            name = getattr(exc, "sqlite_errorname", None) or "SQLITE_ERROR"
            code = getattr(exc, "sqlite_errorcode", None)
            panel.add_log("%s → 失败 [%s (%s)] %s" % (label, name, code, exc))
            return {
                "ok": False,
                "error": {"name": name, "code": code, "message": str(exc)},
                "state": exp.panel_state(side),
            }
    return {"ok": True, "error": None, "state": exp.panel_state(side)}


def run_set_readonly(exp, side, enabled):
    """在该栏**原长期连接**上真实切换 PRAGMA query_only。

    不更换连接、不 COMMIT / ROLLBACK 当前事务，也不执行任何读取
    （不会为 DEFERRED 事务建立读快照）；开关状态只保存在 PanelState。
    开启后 SQLite 对写语句返回 SQLITE_READONLY(8)，此前写入与未提交
    事务都原样保留；COMMIT / ROLLBACK 不重置开关。
    """
    panel = exp.panels[side]
    conn = exp.conns[side]
    label = "只读模式：开启" if enabled else "只读模式：关闭"
    with exp.conn_locks[side]:
        try:
            conn.execute("PRAGMA query_only=%d" % (1 if enabled else 0))
        except sqlite3.Error as exc:
            name = getattr(exc, "sqlite_errorname", None) or "SQLITE_ERROR"
            code = getattr(exc, "sqlite_errorcode", None)
            panel.add_log("%s → 失败 [%s (%s)] %s" % (label, name, code, exc))
            return {
                "ok": False,
                "error": {"name": name, "code": code, "message": str(exc)},
                "state": exp.panel_state(side),
            }
        panel.readonly = enabled
        panel.add_log(
            "%s → 成功（原连接、当前事务不变；提交 / 回滚不重置本开关）" % label
        )
    return {"ok": True, "error": None, "state": exp.panel_state(side)}


def run_checkpoint(exp, mode):
    """用全新的独立连接真实执行 PRAGMA wal_checkpoint(<mode>)。

    busy_timeout=0：读事务阻止回收时立即返回 busy=1，绝不等待。
    检查点不替代两栏提交 / 回滚，也不触碰甲、乙的长期连接。
    """
    mode = (mode or "").upper()
    if mode not in VALID_CHECKPOINT_MODES:
        return {
            "ok": False,
            "error": {"name": None, "code": None, "message": "unknown checkpoint mode"},
            "state": exp.checkpoint_state(),
        }
    with exp.checkpoint_lock:
        # 每次一条独立连接：不用甲、乙或 observer，避免把检查点和
        # 两栏的事务状态、读快照绑定在一起。该连接不是最后一个连接，
        # 关闭时不会触发默认检查点；busy_timeout=0 使 busy 立即返回。
        ckpt_conn = sqlite3.connect(exp.db_path, timeout=0, isolation_level=None)
        try:
            ckpt_conn.execute("PRAGMA busy_timeout=0")
            try:
                row = ckpt_conn.execute(
                    "PRAGMA wal_checkpoint(%s)" % mode
                ).fetchone()
            except sqlite3.Error as exc:
                name = getattr(exc, "sqlite_errorname", None) or "SQLITE_ERROR"
                code = getattr(exc, "sqlite_errorcode", None)
                result = {
                    "mode": mode,
                    "busy": None,
                    "log_frames": None,
                    "checkpointed": None,
                    "wal_size": exp.wal_size(),
                }
                exp.checkpoint_last = result
                exp.add_checkpoint_log(
                    "%s → 失败 [%s (%s)] %s" % (mode, name, code, exc)
                )
                return {
                    "ok": False,
                    "error": {"name": name, "code": code, "message": str(exc)},
                    "state": exp.checkpoint_state(),
                }
        finally:
            ckpt_conn.close()
        # SQLite 原样返回三项：busy、WAL 总帧数 log、已回写帧数 checkpointed
        busy, log_frames, checkpointed = row[0], row[1], row[2]
        result = {
            "mode": mode,
            "busy": busy,
            "log_frames": log_frames,
            "checkpointed": checkpointed,
            "wal_size": exp.wal_size(),
        }
        exp.checkpoint_last = result
        if busy:
            tail = "检查点被并发读事务阻止（busy=1）"
        elif checkpointed < log_frames:
            tail = "回写未完成（%d/%d 帧）" % (checkpointed, log_frames)
        else:
            tail = "回写完成（%d/%d 帧）" % (checkpointed, log_frames)
        exp.add_checkpoint_log(
            "%s → busy=%d，WAL 总帧数=%d，已回写帧数=%d，WAL=%d 字节，%s"
            % (mode, busy, log_frames, checkpointed, result["wal_size"], tail)
        )
    return {"ok": True, "error": None, "state": exp.checkpoint_state(), **result}


# 实验注册表：仅保存在内存中，无需跨重启保留。
# 删除后的对象不从这里摘除，而是以墓碑（deleted=True）保留：
# 任何准入请求都返回 410，并供并发 DELETE 等待者等待收尾结束。
experiments = {}
experiments_lock = threading.Lock()
# 本次运行中已经删除的实验 ID 集合（与注册表墓碑保持一致）
deleted_experiments = set()


def admit_experiment(exp_id):
    """解析请求目标：

    返回 (exp, response)。存活并准入成功时 response 为 None；
    本次运行中删除过的 ID 返回 410；未知 ID 返回 404。
    准入成功的请求必须在 finally 中调用 exp.finish_call()。
    """
    with experiments_lock:
        exp = experiments.get(exp_id)
        was_deleted = exp_id in deleted_experiments
    if exp is not None:
        # active_calls 在独立的实验门控上自增，不持有 experiments_lock
        if exp.admit_call():
            return exp, None
        return None, (jsonify({"error": "experiment gone"}), 410)
    if was_deleted:
        return None, (jsonify({"error": "experiment gone"}), 410)
    return None, (jsonify({"error": "experiment not found"}), 404)


@app.get("/")
def index():
    return render_template("index.html")


@app.post("/api/experiments")
def create_experiment():
    exp_id = uuid.uuid4().hex[:12]
    exp = Experiment(exp_id)
    with experiments_lock:
        experiments[exp_id] = exp
        # 全新 ID，不会与已删除集合冲突；保险起见若被复用则移除旧墓碑
        deleted_experiments.discard(exp_id)
    return jsonify({"id": exp_id})


@app.delete("/api/experiments/<exp_id>")
def delete_experiment(exp_id):
    # 争取删除执行权与立墓碑在同一段注册表临界区内原子完成：
    # 并发请求不可能落入「未标记」窗口而拿到 404。
    # 删除后的实验对象保留在注册表中（墓碑），等待者可等待其收尾。
    with experiments_lock:
        exp = experiments.get(exp_id)
        if exp is None:
            if exp_id in deleted_experiments:
                # 本次运行中已删除的 ID：再次删除仍返回 410
                return jsonify({"error": "experiment gone"}), 410
            return jsonify({"error": "experiment not found"}), 404
        deleted_experiments.add(exp_id)

    # 原子地争取删除权，或等待另一请求的删除收尾结束（随后返回 410）。
    # 检查与等待在同一门控临界区内完成，不会漏掉获胜者的 notify。
    if not exp.claim_or_wait_delete():
        return jsonify({"error": "experiment gone"}), 410

    # 等待此前已进入的请求全部正常完成；
    # 删除开始后该实验的操作、状态、已提交值及检查点新请求均在准入处返回 410。
    exp.begin_delete()
    # 回滚未提交事务、关闭长期及临时连接、移除数据库与 WAL/SHM 文件。
    # 删除等待与拆除都按实验独立，不会阻塞其他实验。
    exp.teardown()
    # 注意：不从注册表摘除——删除后的对象以墓碑形式保留（准入一律 410），
    # 同时保证并发 DELETE 等待者持有的对象引用始终有效。
    return jsonify({"id": exp_id, "deleted": True})


@app.get("/api/experiments/<exp_id>/status")
def experiment_status(exp_id):
    exp, response = admit_experiment(exp_id)
    if exp is None:
        return response
    try:
        # 两栏状态、检查点状态与提交时间线均来自内存缓存，
        # 不执行任何 SQL、不触发检查点、不在两栏连接上读值
        return jsonify(
            {
                "id": exp.id,
                "a": exp.panel_state("a"),
                "b": exp.panel_state("b"),
                "checkpoint": exp.checkpoint_state(),
                "timeline": exp.timeline_records(),
                "script": exp.script_state(),
            }
        )
    finally:
        exp.finish_call()


@app.get("/api/experiments/<exp_id>/timeline")
def experiment_timeline(exp_id):
    exp, response = admit_experiment(exp_id)
    if exp is None:
        return response
    try:
        # 只读内存中的提交时间线：不执行任何 SQL、不触碰两栏连接
        return jsonify({"timeline": exp.timeline_records()})
    finally:
        exp.finish_call()


def parse_script_action(data):
    """解析推进 / 跳过 / 取消请求体：必须且只能给出正确类型的
    script_id（字符串）与 step（整数，布尔不算整数）。"""
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("script_id"), str)
        or not isinstance(data.get("step"), int)
        or isinstance(data.get("step"), bool)
    ):
        return None, (jsonify({"error": "script_id (string) and step (integer) required"}), 400)
    return (data["script_id"], data["step"]), None


@app.get("/api/experiments/<exp_id>/script")
def experiment_script_get(exp_id):
    exp, response = admit_experiment(exp_id)
    if exp is None:
        return response
    try:
        # 只读内存中的脚本状态与执行记录：不执行任何 SQL、不额外读两栏值
        return jsonify({"script": exp.script_state()})
    finally:
        exp.finish_call()


@app.post("/api/experiments/<exp_id>/script")
def experiment_script_load(exp_id):
    exp, response = admit_experiment(exp_id)
    if exp is None:
        return response
    try:
        # 整份校验，非法整份拒绝（400），任何已有脚本都不受影响
        data = request.get_json(silent=True)
        invalid = Experiment.validate_steps(data if data is not None else None)
        if invalid is not None:
            return jsonify({"error": invalid}), 400
        view, rejected = exp.load_script(data)
        if rejected is not None:
            return jsonify(rejected), 409
        return jsonify({"ok": True, "script": view})
    finally:
        exp.finish_call()


@app.post("/api/experiments/<exp_id>/script/advance")
def experiment_script_advance(exp_id):
    exp, response = admit_experiment(exp_id)
    if exp is None:
        return response
    try:
        parsed, bad = parse_script_action(request.get_json(silent=True))
        if bad is not None:
            return bad
        script_id, step_no = parsed
        # 同实验串行核对并执行；过期请求（ID / 步号不符、已结束）409，
        # 不执行、不记录、不影响新脚本。
        view, ok, rejected = exp.advance_script(script_id, step_no)
        if rejected is not None:
            return jsonify(rejected), 409
        return jsonify({"ok": ok, "script": view})
    finally:
        exp.finish_call()


@app.post("/api/experiments/<exp_id>/script/skip")
def experiment_script_skip(exp_id):
    exp, response = admit_experiment(exp_id)
    if exp is None:
        return response
    try:
        parsed, bad = parse_script_action(request.get_json(silent=True))
        if bad is not None:
            return bad
        script_id, step_no = parsed
        view, ok, rejected = exp.skip_script(script_id, step_no)
        if rejected is not None:
            return jsonify(rejected), 409
        return jsonify({"ok": ok, "script": view})
    finally:
        exp.finish_call()


@app.post("/api/experiments/<exp_id>/script/cancel")
def experiment_script_cancel(exp_id):
    exp, response = admit_experiment(exp_id)
    if exp is None:
        return response
    try:
        parsed, bad = parse_script_action(request.get_json(silent=True))
        if bad is not None:
            return bad
        script_id, step_no = parsed
        view, ok, rejected = exp.cancel_script(script_id, step_no)
        if rejected is not None:
            return jsonify(rejected), 409
        return jsonify({"ok": ok, "script": view})
    finally:
        exp.finish_call()


@app.get("/api/experiments/<exp_id>/committed")
def experiment_committed(exp_id):
    exp, response = admit_experiment(exp_id)
    if exp is None:
        return response
    try:
        return jsonify({"committed": json_int(exp.committed_value())})
    finally:
        exp.finish_call()


@app.post("/api/experiments/<exp_id>/checkpoint")
@app.post("/api/experiments/<exp_id>/checkpoint/<mode>")
def experiment_checkpoint(exp_id, mode=None):
    exp, response = admit_experiment(exp_id)
    if exp is None:
        return response
    try:
        if mode is None:
            data = request.get_json(silent=True) or {}
            mode = data.get("mode")
        mode = (mode or "").upper()
        if mode not in VALID_CHECKPOINT_MODES:
            return jsonify({"error": "mode must be PASSIVE or TRUNCATE"}), 400
        return jsonify(run_checkpoint(exp, mode))
    finally:
        exp.finish_call()


@app.post("/api/experiments/<exp_id>/conn/<side>/op")
def experiment_op(exp_id, side):
    exp, response = admit_experiment(exp_id)
    if exp is None:
        return response
    try:
        if side not in ("a", "b"):
            return jsonify({"error": "unknown side"}), 400
        data = request.get_json(silent=True) or {}
        op = data.get("op")
        if op not in VALID_OPS:
            return jsonify({"error": "unknown op"}), 400
        # 脚本有效期间拒绝手工事务操作（查询 / 检查点 / 删除不受影响）。
        # 注意必须在**整段执行**持有 script_lock：仅在 run_op 之前检查会留下
        # TOCTOU 窗口——手工 BEGIN 实际执行前，另一线程可能已装载脚本成功
        # （装载在同一把锁内复查两栏无事务），从而破坏「有事务不能装载」
        # 不变量。锁序 script_lock → conn_lock（run_op 内部）与 load_script
        # 完全一致，不构成死锁环。锁内读标志只能用 _script_active_locked()：
        # script_lock 不可重入，重复获取会让请求永久卡住并占住准入名额。
        value = None
        if op == "add":
            value = parse_int(data.get("value"))
            if value is None:
                return jsonify({"error": "value must be an integer"}), 400
            if abs(value) > MAX_ADD_ABS:
                return jsonify({"error": "value too large"}), 400
        with exp.script_lock:
            if exp._script_active_locked():
                return (
                    jsonify({"error": "manual operations are locked while a script is active"}),
                    409,
                )
            return jsonify(run_op(exp, side, op, value))
    finally:
        exp.finish_call()


@app.post("/api/experiments/<exp_id>/conn/<side>/readonly")
def experiment_set_readonly(exp_id, side):
    exp, response = admit_experiment(exp_id)
    if exp is None:
        return response
    try:
        if side not in ("a", "b"):
            return jsonify({"error": "unknown side"}), 400
        data = request.get_json(silent=True)
        # 严格只接受 JSON 布尔：1/0、"true"、None 等一律拒绝，
        # get_json 解析失败（body 不是 JSON）时 data 为 None 同样拒绝。
        if not isinstance(data, dict) or not isinstance(data.get("enabled"), bool):
            return jsonify({"error": "enabled must be a boolean"}), 400
        # 脚本有效期间只读切换同样锁定（脚本必须沿两栏既定连接语义执行）。
        # 与手工操作一样在整段持 script_lock，避免「检查后、执行前恰好装载」；
        # 锁内同样只能用不重复取锁的 _script_active_locked() 读标志。
        with exp.script_lock:
            if exp._script_active_locked():
                return (
                    jsonify({"error": "readonly switch is locked while a script is active"}),
                    409,
                )
            return jsonify(run_set_readonly(exp, side, data["enabled"]))
    finally:
        exp.finish_call()


def prepare_data_dir():
    # 实验无需跨重启保留：启动时清空旧数据库文件
    if os.path.isdir(DATA_DIR):
        shutil.rmtree(DATA_DIR, ignore_errors=True)
    os.makedirs(DATA_DIR, exist_ok=True)


prepare_data_dir()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, threaded=True)
