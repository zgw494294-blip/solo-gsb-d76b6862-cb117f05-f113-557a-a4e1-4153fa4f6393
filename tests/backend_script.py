"""后端脚本状态机验证（无需 Flask）：用最小 stub 导入 app.py，
直接在真实 SQLite 上驱动 Experiment 的装载 / 推进 / 跳过 / 取消 / 互斥。
"""
import sys, types, json, os, tempfile

# ---- 最小 Flask stub ----
flask = types.ModuleType("flask")
class _App:
    def __init__(self, name): self.routes = {}
    def _reg(self, path, **kw):
        def deco(fn):
            for p in (path if isinstance(path, list) else [path]):
                self.routes[p] = fn
            return fn
        return deco
    get = lambda s, p, **k: s._reg(p, **k)
    post = lambda s, p, **k: s._reg(p, **k)
    delete = lambda s, p, **k: s._reg(p, **k)
    def run(self, **k): pass
flask.Flask = _App
flask.jsonify = lambda *a, **k: ("json", a, k)
flask.render_template = lambda *a, **k: "html"
class _Req:
    def __init__(self): self._json = None
    def get_json(self, silent=False): return self._json
flask.request = _Req()
sys.modules["flask"] = flask

_tmp = tempfile.mkdtemp()
os.environ["LAB_DATA_DIR"] = _tmp
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")
import app as A

check_eq = None

fails = []
def check(name, cond, extra=""):
    print(("  ok  " if cond else "  FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))
    if not cond: fails.append(name)

# 校验
v = A.Experiment.validate_steps
check("非数组拒绝", v({"a":1}) is not None)
check("空数组拒绝", v([]) is not None)
check("51步拒绝", v([{"side":"a","op":"begin"}]*51) is not None)
check("50步通过", v([{"side":"a","op":"begin"}]*50) is None)
check("1步通过", v([{"side":"b","op":"read"}]) is None)
check("坏side", v([{"side":"c","op":"begin"}]) is not None)
check("坏op", v([{"side":"a","op":"nope"}]) is not None)
check("add缺value", v([{"side":"a","op":"add"}]) is not None)
check("add非整数", v([{"side":"a","op":"add","value":1.5}]) is not None)
check("add布尔拒绝", v([{"side":"a","op":"add","value":True}]) is not None)
check("add超2^62", v([{"side":"a","op":"add","value":2**62+1}]) is not None)
check("add=2^62通过", v([{"side":"a","op":"add","value":2**62}]) is None)
check("add大整数字符串通过", v([{"side":"a","op":"add","value":"9007199254740993"}]) is None)
check("非对象step拒绝", v(["begin"]) is not None)
check("begin带多余字段宽容", v([{"side":"a","op":"begin","value":1}]) is None)

exp = A.Experiment("test1")

# 装载 README 示例：甲 begin/+5, 乙 begin/+7(撞锁), 甲commit, 乙+7, 乙commit
steps = [
    {"side":"a","op":"begin"},
    {"side":"a","op":"add","value":5},
    {"side":"b","op":"begin"},
    {"side":"b","op":"add","value":7},
    {"side":"a","op":"commit"},
    {"side":"b","op":"add","value":7},
    {"side":"b","op":"commit"},
]
view, rej = exp.load_script(steps)
check("装载成功", rej is None)
sid = view["id"]
check("步号从1开始", view["step"] == 1 and view["status"] == "active", str(view["step"]))

# active 时手工互斥
check("active 标记手工锁定", exp.script_active() is True)

# 手工 run_op 不直接被 Experiment 拦（路由层拦），这里模拟路由检查：
# 直接调 run_op 甲 begin 应该报"事务已开启"——脚本第1步已 begin? 尚未推进，
# 所以应成功开启第二个 begin? 不，脚本还没执行，连接空闲；这只是状态机测试，
# 跳过手工 run_op，聚焦脚本流。

# 过期 ID / 错步号
_, ok, r = exp.advance_script("WRONG", 1)
check("错误脚本ID拒绝(409语义)", r is not None and ok is False, str(r))
_, ok, r = exp.advance_script(sid, 2)
check("错误步号拒绝", r is not None)
check("过期拒绝无记录", len(exp.script_state()["records"]) == 0)

def adv(step, expect_ok=True):
    vw, ok, r = exp.advance_script(sid, step)
    assert r is None, r
    return vw, ok

vw, ok = adv(1)  # 甲 begin
check("步1 甲begin成功", ok and vw["step"] == 2)
vw, ok = adv(2)  # 甲 +5
check("步2 甲+5成功", ok and vw["step"] == 3)
vw, ok = adv(3)  # 乙 begin
check("步3 乙begin成功", ok and vw["step"] == 4)
vw, ok = adv(4)  # 乙 +7 撞甲写锁
recs = vw["records"]
check("步4 乙+7失败", ok is False and vw["step"] == 4)
check("记录失败原始错误名SQLITE_BUSY", recs[-1]["error"] and recs[-1]["error"]["name"] == "SQLITE_BUSY", str(recs[-1]))
check("错误码5", recs[-1]["error"]["code"] == 5)
check("失败结果failure", recs[-1]["result"] == "failure")
# 甲仍未提交：乙事务保持开启，系统未代回滚
check("失败不代回滚：乙仍在事务", exp.conns["b"].in_transaction)
check("失败后甲仍持锁(在事务)", exp.conns["a"].in_transaction)

# 再次推进同一步号 4：仍撞锁（甲未提交）
vw, ok, r = exp.advance_script(sid, 4)
check("重试步4仍失败停住", r is None and ok is False and vw["step"] == 4)
check("重试也产生记录", len(vw["records"]) == 5)

# 跳过步4
vw, ok, r = exp.skip_script(sid, 4)
check("跳过步4成功", r is None and ok and vw["step"] == 5)
check("跳过记录skipped", vw["records"][-1]["result"] == "skipped" and vw["records"][-1]["error"] is None)

# 跳过错误步号
_, ok, r = exp.skip_script(sid, 4)
check("跳过错步号拒绝", r is not None)

vw, ok = adv(5)  # 甲 commit
check("步5 甲commit成功", ok and vw["step"] == 6)
check("甲提交落地=105", exp.committed_value() == 105, str(exp.committed_value()))
# 时间线记录一条
tl = exp.timeline_records()
check("时间线1条 100->105 [5]", len(tl) == 1 and tl[0]["before"] == 100 and tl[0]["after"] == 105 and tl[0]["deltas"] == [5], str(tl))

vw, ok = adv(6)  # 乙在原事务 +7（写锁已释放，无快照，应成功）
check("步6 乙原事务+7成功", ok and vw["step"] == 7)
vw, ok = adv(7)  # 乙 commit
check("步7 乙commit成功 脚本finished", ok and vw["status"] == "finished" and vw["step"] is None)
check("finished后不锁手工", exp.script_active() is False)
check("最终已提交112", exp.committed_value() == 112, str(exp.committed_value()))
tl = exp.timeline_records()
check("时间线2条 105->112 [7]", len(tl) == 2 and tl[1]["before"] == 105 and tl[1]["after"] == 112)

# 完成后过期动作
_, ok, r = exp.advance_script(sid, 8)
check("finished后推进拒绝", r is not None)
_, ok, r = exp.cancel_script(sid, 8)
check("finished后取消拒绝", r is not None)

# 完成后两栏均无事务：可以装载新脚本
view2, rej = exp.load_script([{"side":"a","op":"read"}])
check("完成且无事务:装载成功", rej is None)
sid2 = view2["id"]
check("新脚本ID不同于旧", sid2 != sid)
# 旧脚本的过期请求不得影响新脚本
_, ok, r = exp.advance_script(sid, 1)
check("旧ID动作不执行不影响新脚本", r is not None)
check("新脚本仍停在步1", exp.script_state()["step"] == 1)

# 取消：在第1步停止；不自动提交/回滚（read 不开写事务；另测 begin 后取消）
vw, ok, r = exp.cancel_script(sid2, 1)
check("取消成功", ok and vw["status"] == "cancelled" and vw["step"] == 1)
check("取消后手工恢复", exp.script_active() is False)

# 取消不自动回滚：begin 后取消，事务仍开着
v3, rej = exp.load_script([{"side":"a","op":"begin"},{"side":"a","op":"read"}])
check("装载begin脚本", rej is None)
exp.advance_script(v3["id"], 1)
exp.cancel_script(v3["id"], 2)
check("取消不自动回滚：甲事务仍开启", exp.conns["a"].in_transaction is True)
# 此时有未结事务：再装载被拒
v4, rej = exp.load_script([{"side":"a","op":"read"}])
check("有事务时装载拒绝", rej is not None)
# 手工提交（脚本已取消，手工解锁）提交后可装载
A.run_op(exp, "a", "commit", None)
v5, rej = exp.load_script([{"side":"a","op":"read"}])
check("手工收尾事务后可再装载", rej is None)

print()
print(f"{len(fails)} 失败" if fails else "后端脚本状态机全部通过")
sys.exit(1 if fails else 0)
