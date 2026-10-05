"""后端实际路由验证：用 Flask 测试客户端驱动真实 app.py 的全部相关路由
（真实线程、真实 script_lock / conn_lock / 准入门控、真实 SQLite），
不模拟任何准入或锁逻辑。

覆盖曾导致手工操作 / 只读切换永久卡住的缺陷及其全部约束：
  1. 无有效脚本时，手工 BEGIN 等操作与只读切换按原事务规则正常返回；
  2. 脚本 active 期间手工操作 / 只读切换及时返回 409 且不执行任何 SQL；
  3. 脚本查询、推进、跳过、取消与删除不被手工请求阻塞（准入名额必定归还）；
  4. finished / cancelled 后手工操作恢复，系统不自动提交 / 回滚；
  5. 手工 BEGIN 与脚本装载严格互斥（两种先后次序，含真实并发交错）；
  6. DELETE 等待在途手工请求，删除开始后新请求一律 410。

运行：python3 tests/backend_routes.py
"""
import os
import sys
import tempfile
import threading

os.environ["LAB_DATA_DIR"] = tempfile.mkdtemp(prefix="lab_routes_")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")

import app as A  # noqa: E402

A.app.testing = True

fails = []


def check(name, cond, extra=""):
    print(("  ok  " if cond else "  FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)


def client():
    return A.app.test_client()


def create_exp(c):
    r = c.post("/api/experiments")
    assert r.status_code == 200, r.status_code
    eid = r.get_json()["id"]
    return eid, "/api/experiments/%s" % eid


def op(c, base, side, name, value=None, status=200):
    body = {"op": name}
    if value is not None:
        body["value"] = value
    r = c.post("%s/conn/%s/op" % (base, side), json=body)
    assert r.status_code == status, (name, r.status_code, r.get_data(as_text=True))
    return r.get_json()


def readonly(c, base, side, enabled, status=200):
    r = c.post("%s/conn/%s/readonly" % (base, side), json={"enabled": enabled})
    assert r.status_code == status, (r.status_code, r.get_data(as_text=True))
    return r.get_json()


def status(c, base):
    r = c.get("%s/status" % base)
    assert r.status_code == 200
    return r.get_json()


def load(c, base, steps):
    return c.post("%s/script" % base, json=steps)


def advance(c, base, sid, step, status=200):
    r = c.post("%s/script/advance" % base, json={"script_id": sid, "step": step})
    assert r.status_code == status, (r.status_code, r.get_data(as_text=True))
    return r.get_json()


# ---------------------------------------------------------------------------
# 1. 无有效脚本：手工操作与只读切换按原事务规则执行（修复前全部永久卡住）
# ---------------------------------------------------------------------------
c = client()
_, base = create_exp(c)

r = op(c, base, "a", "begin")
check("无脚本 BEGIN 正常 200 并开启事务", r["ok"] and r["state"]["in_transaction"])
# 重复 BEGIN 是业务拒绝（ok=false），而非挂起、而非 409
r = op(c, base, "a", "begin")
check("无脚本重复 BEGIN 按原规则拒绝（不挂起、不是409）",
      r["ok"] is False and r["error"]["name"] is None)
r = op(c, base, "a", "add", 5)
check("无脚本事务内加法成功", r["ok"] and r["state"]["in_transaction"])
r = readonly(c, base, "a", True)
check("无脚本只读切换正常 200", r["ok"] and r["state"]["readonly"] is True)
# 只读下加法由 SQLite 如实拒绝 SQLITE_READONLY(8)，事务与 +5 保留
r = op(c, base, "a", "add", 7)
check("只读下加法报 SQLITE_READONLY(8) 且不撤销此前修改",
      r["ok"] is False and r["error"]["name"] == "SQLITE_READONLY"
      and r["error"]["code"] == 8 and r["state"]["in_transaction"])
# 另一栏完全不受影响：无脚本时可正常开启事务
r = op(c, base, "b", "begin")
check("无脚本时另一栏 BEGIN 正常", r["ok"] and r["state"]["in_transaction"])
op(c, base, "b", "rollback")
# 提交不重置只读开关；关闭只读
op(c, base, "a", "commit")
check("提交落地=105", c.get("%s/committed" % base).get_json()["committed"] == 105)
st = status(c, base)
check("提交后只读开关仍为开启（提交不重置）", st["a"]["readonly"] is True
      and st["a"]["in_transaction"] is False)
r = readonly(c, base, "a", False)
check("关闭只读正常", r["ok"] and r["state"]["readonly"] is False)
# 只读接口字段约定：非布尔 400
for payload in (1, 0, "true", None):
    rr = c.post("%s/conn/a/readonly" % base, json={"enabled": payload})
    check("非布尔 enabled 拒绝 400（%r）" % payload, rr.status_code == 400)
# op 字段约定：未知 op 400、未知 side 400
check("未知 op 返回 400",
      c.post("%s/conn/a/op" % base, json={"op": "nope"}).status_code == 400)
check("未知 side 返回 400",
      c.post("%s/conn/x/op" % base, json={"op": "begin"}).status_code == 400)

# 无脚本时脚本查询 / 状态查询立即返回 null，不挂起
check("无脚本 GET script 返回 null 且不挂起",
      c.get("%s/script" % base).get_json()["script"] is None)
check("无脚本 status 的 script 字段为 null", status(c, base)["script"] is None)


# ---------------------------------------------------------------------------
# 2. active 期间：手工操作 / 只读切换及时 409、不执行；查询/推进/检查点不阻塞
# ---------------------------------------------------------------------------
c = client()
_, base = create_exp(c)
steps = [
    {"side": "a", "op": "begin"},
    {"side": "a", "op": "add", "value": 5},
    {"side": "b", "op": "begin"},
    {"side": "b", "op": "add", "value": 7},   # 撞甲写锁
    {"side": "a", "op": "commit"},
    {"side": "b", "op": "add", "value": 7},
    {"side": "b", "op": "commit"},
]
r = load(c, base, steps)
check("装载成功", r.status_code == 200 and r.get_json()["ok"])
sid = r.get_json()["script"]["id"]

for name in ("begin", "read", "add", "commit", "rollback"):
    body = {"op": name}
    if name == "add":
        body["value"] = 1
    for side in ("a", "b"):
        rr = c.post("%s/conn/%s/op" % (base, side), json=body)
        check("active 期间 %s 栏 %s 返回 409" % (side, name), rr.status_code == 409)
for side in ("a", "b"):
    for enabled in (True, False):
        rr = c.post("%s/conn/%s/readonly" % (base, side), json={"enabled": enabled})
        check("active 期间 %s 栏只读切换返回 409" % side, rr.status_code == 409)

st = status(c, base)
check("409 不执行：两栏均无事务、只读均未开启",
      not st["a"]["in_transaction"] and not st["b"]["in_transaction"]
      and st["a"]["readonly"] is False and st["b"]["readonly"] is False
      and c.get("%s/committed" % base).get_json()["committed"] == 100)

# 查询、推进、检查点在手工 409 之后全部立即完成（名额已归还）
check("GET script 查询不被阻塞", c.get("%s/script" % base).status_code == 200)
r = advance(c, base, sid, 1)
check("步1 推进成功（不被手工请求阻塞）", r["ok"] and r["script"]["step"] == 2)
advance(c, base, sid, 2)
advance(c, base, sid, 3)
r = advance(c, base, sid, 4)
check("步4 撞锁失败、步号停在 4", r["ok"] is False and r["script"]["step"] == 4)
check("失败记录原始错误名 / 码",
      r["script"]["records"][-1]["error"]["name"] == "SQLITE_BUSY"
      and r["script"]["records"][-1]["error"]["code"] == 5)
# 失败不回滚：乙事务仍开启；此时手工操作依旧 409（脚本仍 active）
check("active 仍持续：手工 BEGIN 仍 409",
      c.post("%s/conn/b/op" % base, json={"op": "begin"}).status_code == 409)
r = c.post("%s/script/skip" % base, json={"script_id": sid, "step": 4})
check("跳过步4成功", r.status_code == 200 and r.get_json()["ok"]
      and r.get_json()["script"]["step"] == 5)
advance(c, base, sid, 5)
advance(c, base, sid, 6)
r = advance(c, base, sid, 7)
check("步7 后脚本 finished", r["ok"] and r["script"]["status"] == "finished"
      and r["script"]["step"] is None)
check("检查点在 active 期间照常可用（不被锁）",
      c.post("%s/checkpoint/PASSIVE" % base).status_code == 200)
check("最终已提交值 112",
      c.get("%s/committed" % base).get_json()["committed"] == 112)


# ---------------------------------------------------------------------------
# 3. finished / cancelled 后手工恢复；系统不自动提交 / 回滚
# ---------------------------------------------------------------------------
c = client()
_, base = create_exp(c)
r = load(c, base, [{"side": "a", "op": "begin"}, {"side": "a", "op": "add", "value": 9}])
sid = r.get_json()["script"]["id"]
advance(c, base, sid, 1)
advance(c, base, sid, 2)
# 脚本只有 2 步，再推进 409（finished）
check("finished 后推进 409",
      c.post("%s/script/advance" % base, json={"script_id": sid, "step": 3}).status_code == 409)
st = status(c, base)
check("finished：甲事务仍开启（不自动提交 / 回滚）",
      st["script"]["status"] == "finished" and st["a"]["in_transaction"])
# 手工恢复：原事务内继续加法并提交
r = op(c, base, "a", "add", 1)
check("finished 后手工加法在原事务成功", r["ok"])
op(c, base, "a", "commit")
check("手工提交落地=110", c.get("%s/committed" % base).get_json()["committed"] == 110)

# cancelled：取消不自动回滚；手工与只读切换都恢复
c = client()
_, base = create_exp(c)
r = load(c, base, [{"side": "b", "op": "begin"}, {"side": "b", "op": "read"}])
sid = r.get_json()["script"]["id"]
advance(c, base, sid, 1)
r = c.post("%s/script/cancel" % base, json={"script_id": sid, "step": 2})
check("取消成功", r.status_code == 200
      and r.get_json()["script"]["status"] == "cancelled")
st = status(c, base)
check("cancelled：乙事务仍开启（不自动回滚）", st["b"]["in_transaction"])
r = op(c, base, "b", "add", 3)
check("cancelled 后手工加法恢复", r["ok"])
r = readonly(c, base, "b", True)
check("cancelled 后只读切换恢复", r["ok"] and r["state"]["readonly"])
# 只读下提交仍提交此前写入
op(c, base, "b", "commit")
check("只读下提交落地=103", c.get("%s/committed" % base).get_json()["committed"] == 103)
# 有未结事务（上一脚本 finished 后两栏无事务的场景）装载 409：
# 这里用仍开启事务的新场景验证
c = client()
_, base = create_exp(c)
r = load(c, base, [{"side": "a", "op": "begin"}])
sid = r.get_json()["script"]["id"]
advance(c, base, sid, 1)  # finished，事务留着
r = load(c, base, [{"side": "a", "op": "read"}])
check("有未结事务时装载返回 409", r.status_code == 409)
op(c, base, "a", "rollback")
r = load(c, base, [{"side": "a", "op": "read"}])
check("手工收尾事务后装载成功", r.status_code == 200)


# ---------------------------------------------------------------------------
# 4. 手工 BEGIN 与装载互斥：确定性的两种先后次序
# ---------------------------------------------------------------------------
# 4a. BEGIN 先成功：装载拒绝（409）；回滚后可装载
c = client()
_, base = create_exp(c)
op(c, base, "a", "begin")
r = load(c, base, [{"side": "b", "op": "read"}])
check("BEGIN 先执行 → 装载 409", r.status_code == 409)
check("被拒装载不产生脚本", c.get("%s/script" % base).get_json()["script"] is None)
op(c, base, "a", "rollback")
r = load(c, base, [{"side": "b", "op": "read"}])
check("回滚后装载成功", r.status_code == 200)

# 4b. 装载先成功：手工 BEGIN 与只读切换立即拒绝（409）
c = client()
_, base = create_exp(c)
r = load(c, base, [{"side": "a", "op": "read"}])
sid = r.get_json()["script"]["id"]
check("装载先成功 → 手工 BEGIN 409",
      c.post("%s/conn/a/op" % base, json={"op": "begin"}).status_code == 409)
check("装载先成功 → 另一栏手工 BEGIN 409",
      c.post("%s/conn/b/op" % base, json={"op": "begin"}).status_code == 409)
check("装载先成功 → 只读切换 409",
      c.post("%s/conn/a/readonly" % base, json={"enabled": True}).status_code == 409)
st = status(c, base)
check("409 不执行：无事务、脚本仍停在步1",
      not st["a"]["in_transaction"] and not st["b"]["in_transaction"]
      and st["script"]["step"] == 1)
# 推进仍可正常完成
advance(c, base, sid, 1)
check("装载先胜场景：推进正常 finished",
      status(c, base)["script"]["status"] == "finished")


# ---------------------------------------------------------------------------
# 5. 真实并发交错：手工 BEGIN 与装载同时放行，恰一胜且永不并存、不卡死
# ---------------------------------------------------------------------------
def interleave_round(idx):
    cc = client()
    _, bb = create_exp(cc)
    barrier = threading.Barrier(2)
    out = {}

    def do_begin():
        barrier.wait()
        out["begin"] = cc.post("%s/conn/a/op" % bb, json={"op": "begin"})

    def do_load():
        barrier.wait()
        out["load"] = load(cc, bb, [{"side": "b", "op": "read"}])

    t1 = threading.Thread(target=do_begin)
    t2 = threading.Thread(target=do_load)
    t1.start(); t2.start()
    t1.join(5); t2.join(5)
    assert not t1.is_alive() and not t2.is_alive(), "第 %d 轮交错卡死" % idx
    bc, lc = out["begin"].status_code, out["load"].status_code
    assert (bc, lc) in ((200, 409), (409, 200)), (idx, bc, lc)
    st = cc.get("%s/status" % bb).get_json()
    if bc == 200:
        # BEGIN 胜：甲在事务；脚本不存在；后续装载继续 409
        assert st["a"]["in_transaction"] and st["script"] is None, idx
        assert load(cc, bb, [{"side": "b", "op": "read"}]).status_code == 409
        cc.post("%s/conn/a/op" % bb, json={"op": "rollback"})
    else:
        # 装载胜：两栏无事务、脚本 active；后续手工 BEGIN / 只读继续 409
        assert not st["a"]["in_transaction"] and st["script"]["status"] == "active", idx
        assert cc.post("%s/conn/a/op" % bb, json={"op": "begin"}).status_code == 409
        assert cc.post("%s/conn/b/readonly" % bb, json={"enabled": True}).status_code == 409
        s = st["script"]["id"]
        advance(cc, bb, s, 1)
        assert status(cc, bb)["script"]["status"] == "finished", idx
    # 名额归还：删除必定收尾
    assert cc.delete(bb).status_code == 200, idx
    assert cc.get("%s/status" % bb).status_code == 410, idx


for i in range(40):
    interleave_round(i)
check("40 轮 BEGIN/装载并发交错：恰一胜、无并存、无卡死、可删除", True)


# ---------------------------------------------------------------------------
# 6. 名额归还：active 期间并发手工 409 洪泛后，推进 / 查询 / 删除全部立即完成
# ---------------------------------------------------------------------------
c = client()
eid, base = create_exp(c)
r = load(c, base, [{"side": "a", "op": "read"}])
sid = r.get_json()["script"]["id"]
codes = []
lock = threading.Lock()


def hammer():
    local = []
    for _ in range(25):
        local.append(c.post("%s/conn/a/op" % base, json={"op": "begin"}).status_code)
        local.append(c.post("%s/conn/b/readonly" % base, json={"enabled": False}).status_code)
    with lock:
        codes.extend(local)


ths = [threading.Thread(target=hammer) for _ in range(4)]
for t in ths:
    t.start()
for t in ths:
    t.join(5)
check("409 洪泛无卡死且全部 409",
      not any(t.is_alive() for t in ths) and len(codes) == 200 and set(codes) == {409})
# 名额必定归还：推进、查询、删除都不再被阻塞
r = advance(c, base, sid, 1)
check("洪泛后推进正常", r["ok"] and r["script"]["status"] == "finished")
check("洪泛后 GET script 正常", c.get("%s/script" % base).status_code == 200)
check("洪泛后删除立即完成", c.delete(base).status_code == 200)
exp = A.experiments[eid]
check("门控名额归零", exp.active_calls == 0 and exp.deleted)
check("删除后手工请求 410",
      c.post("%s/conn/a/op" % base, json={"op": "begin"}).status_code == 410)


# ---------------------------------------------------------------------------
# 7. 在途手工请求与 DELETE：删除等待其完成，删除开始后新请求 410
# ---------------------------------------------------------------------------
c = client()
eid, base = create_exp(c)
exp = A.experiments[eid]
orig_run_op = A.run_op
inside = threading.Event()
proceed = threading.Event()


def slow_run_op(e, side, op_, value):
    if op_ == "begin":
        inside.set()
        proceed.wait(5)
    return orig_run_op(e, side, op_, value)


A.run_op = slow_run_op
begin_resp = {}
t = threading.Thread(
    target=lambda: begin_resp.update(r=c.post("%s/conn/a/op" % base, json={"op": "begin"}))
)
t.start()
assert inside.wait(5)
del_resp = {}
dt = threading.Thread(target=lambda: del_resp.update(r=c.delete(base)))
dt.start()
for _ in range(60):
    if exp.deleting:
        break
    threading.Event().wait(0.05)
check("删除已标记 deleting", exp.deleting)
check("删除开始后新手工请求 410",
      c.post("%s/conn/b/op" % base, json={"op": "begin"}).status_code == 410)
check("删除开始后只读切换 410",
      c.post("%s/conn/a/readonly" % base, json={"enabled": True}).status_code == 410)
dt.join(1)
check("在途手工请求完成前 DELETE 不收尾", dt.is_alive())
proceed.set()
t.join(5); dt.join(5)
check("在途请求与 DELETE 均正常结束",
    not t.is_alive() and not dt.is_alive()
    and begin_resp["r"].status_code == 200 and del_resp["r"].status_code == 200)
check("收尾后名额归零、数据库文件已移除",
      exp.active_calls == 0 and not os.path.exists(exp.db_path))
A.run_op = orig_run_op

print()
print("%d 失败" % len(fails) if fails else "后端实际路由测试全部通过")
sys.exit(1 if fails else 0)
