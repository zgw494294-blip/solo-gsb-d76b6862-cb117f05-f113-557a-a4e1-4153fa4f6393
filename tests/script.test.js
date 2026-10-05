"use strict";

// 验证「脚本逐步执行」的前端行为：
// 装载（原文发送）、active 时两栏手工控件 / 只读开关锁定、推进 / 跳过 /
// 取消携带脚本 ID 与所见步号、步失败停步可重试、409（过期 / 互斥）本地
// 不改动并以服务端状态重核对、完成 / 取消后恢复手工操作（不自动提交 /
// 回滚）、创建禁用、删除冻结、旧脚本迟到响应隔离。
// 响应顺序完全可控，不依赖真实时间与网络。
// 运行：node tests/script.test.js

const {
  loadApp,
  flush,
  bootOldLive,
  settleNewCreated,
} = require("./harness");

let passed = 0;
let failed = 0;

function assert(cond, message) {
  if (!cond) throw new Error(message);
}
function assertEq(actual, expected, message) {
  if (actual !== expected) {
    throw new Error(`${message}（期望 ${JSON.stringify(expected)}，实际 ${JSON.stringify(actual)})`);
  }
}
async function test(name, fn) {
  try {
    await fn();
    passed++;
    console.log("  ✓ " + name);
  } catch (e) {
    failed++;
    console.error("  ✗ " + name);
    console.error("      " + e.message);
  }
}

// 两栏五个手工按钮 + 加法输入框 + 只读复选框的可用性
function sideManualEnabled(ctx, side) {
  const ops = ["begin", "read", "add", "commit", "rollback"].every(
    (op) => ctx.opButton(side, op).disabled === false
  );
  return (
    ops &&
    ctx.el(`add-input-${side}`).disabled === false &&
    ctx.el(`ro-toggle-${side}`).disabled === false
  );
}
function sideManualDisabled(ctx, side) {
  const ops = ["begin", "read", "add", "commit", "rollback"].every(
    (op) => ctx.opButton(side, op).disabled === true
  );
  return (
    ops &&
    ctx.el(`add-input-${side}`).disabled === true &&
    ctx.el(`ro-toggle-${side}`).disabled === true
  );
}

function scriptView(overrides = {}) {
  const total = overrides.total || 2;
  return {
    id: overrides.id || "S1",
    status: overrides.status || "active",
    step: overrides.step === undefined ? 1 : overrides.step,
    total,
    steps:
      overrides.steps ||
      Array.from({ length: total }, (_, i) => ({
        step: i + 1,
        side: i === 0 ? "a" : "b",
        op: i === 0 ? "begin" : "commit",
      })),
    records: overrides.records || [],
  };
}

async function settlePollWithScript(ctx, script) {
  // 放行一轮 status 轮询（带 script）；committed 一并放行
  ctx.tickTimers();
  await flush();
  ctx.settle("/OLD/status", 200, {
    id: "OLD",
    a: { in_transaction: false, readonly: false, last_read: null, log: [] },
    b: { in_transaction: false, readonly: false, last_read: null, log: [] },
    checkpoint: { last: null, log: [] },
    script,
  });
  await flush();
  ctx.settle("/OLD/committed", 200, { committed: 100 });
  await flush();
}

async function main() {
  // 前置：无脚本时两栏手工可用、装载按钮可用、推进 / 跳过 / 取消禁用
  await test("无脚本：手工可用，只能装载", async () => {
    const ctx = loadApp("?exp=OLD");
    await bootOldLive(ctx);
    if (!sideManualEnabled(ctx, "a") || !sideManualEnabled(ctx, "b")) {
      throw new Error("无脚本时两栏手工应可用");
    }
    assertEq(ctx.el("script-load").disabled, false, "装载可用");
    assertEq(ctx.el("script-example").disabled, false, "示例可用");
    assertEq(ctx.el("script-advance").disabled, true, "推进禁用");
    assertEq(ctx.el("script-skip").disabled, true, "跳过禁用");
    assertEq(ctx.el("script-cancel").disabled, true, "取消禁用");
  });

  // 装载：原文作为请求体（大整数不经 JS Number），成功后锁定两栏手工
  await test("装载成功：原文发送、两栏手工与只读开关锁定、动作按钮切换", async () => {
    const ctx = loadApp("?exp=OLD");
    await bootOldLive(ctx);
    const text = '[\n  {"side":"a","op":"begin"},\n  {"side":"b","op":"add","value":9007199254740993}\n]';
    ctx.el("script-input").value = text;
    ctx.el("script-load").click();
    await flush();

    // 请求体必须是原文（逐字节），Content-Type 为 JSON
    const call = ctx.findCall("/OLD/script", "POST");
    assert(!!call, "应发出装载请求");
    assertEq(call.body, text, "请求体为文本框原文，不经 Number 转换");

    // 在途：两栏手工全禁用、全部脚本按钮禁用
    if (!sideManualDisabled(ctx, "a") || !sideManualDisabled(ctx, "b")) {
      throw new Error("脚本请求在途时两栏手工应禁用");
    }
    for (const id of ["script-load", "script-advance", "script-skip", "script-cancel"]) {
      assertEq(ctx.el(id).disabled, true, `在途时 ${id} 禁用`);
    }

    const sv = scriptView({
      id: "S1",
      status: "active",
      step: 1,
      total: 2,
      steps: [
        { step: 1, side: "a", op: "begin" },
        { step: 2, side: "b", op: "add", value: "9007199254740993" },
      ],
    });
    call.settled = true;
    call.resolve({ status: 200, ok: true, json: async () => ({ ok: true, script: sv }) });
    await flush();

    // active：两栏手工与只读仍锁定；推进 / 跳过 / 取消可用、装载禁用
    if (!sideManualDisabled(ctx, "a") || !sideManualDisabled(ctx, "b")) {
      throw new Error("active 时两栏手工应锁定");
    }
    assertEq(ctx.el("script-load").disabled, true, "active 时装载禁用");
    assertEq(ctx.el("script-input").disabled, true, "active 时输入框禁用");
    assertEq(ctx.el("script-example").disabled, true, "active 时示例禁用");
    assertEq(ctx.el("script-advance").disabled, false, "推进可用");
    assertEq(ctx.el("script-skip").disabled, false, "跳过可用");
    assertEq(ctx.el("script-cancel").disabled, false, "取消可用");
    assert(ctx.el("script-state").textContent.includes("当前第 1 / 2 步"), "状态显示当前步号");
  });

  // active 时点击手工按钮 / 只读开关不发请求（前端遵守锁定）
  await test("active 时手工操作与只读开关点击不发请求", async () => {
    const ctx = loadApp("?exp=OLD");
    await bootOldLive(ctx);
    await settlePollWithScript(ctx, scriptView({ id: "S1", step: 2, total: 3 }));
    if (!sideManualDisabled(ctx, "a")) throw new Error("前置：甲栏应锁定");

    const before = ctx.calls.length;
    ctx.opButton("a", "begin").click();
    ctx.opButton("b", "commit").click();
    ctx.el("ro-toggle-a").change(true);
    await flush();
    assertEq(ctx.calls.length, before, "锁定期间不得发出手工 / 只读请求");
    assertEq(ctx.el("ro-toggle-a").checked, false, "只读复选框不得被勾上");
    // 检查点不被脚本锁定
    ctx.el("ckpt-passive").click();
    await flush();
    assert(!!ctx.findCall("/OLD/checkpoint/PASSIVE", "POST"), "检查点仍应可用");
    ctx.settle("/OLD/checkpoint/PASSIVE", 200, {
      ok: true, error: null, state: { last: null, log: [] },
    });
    await flush();
  });

  // 推进：携带脚本 ID 与所见步号；成功前移
  await test("推进携带脚本 ID / 步号，成功后步号前移", async () => {
    const ctx = loadApp("?exp=OLD");
    await bootOldLive(ctx);
    const steps = [
      { step: 1, side: "a", op: "begin" },
      { step: 2, side: "a", op: "add", value: 5 },
      { step: 3, side: "b", op: "begin" },
      { step: 4, side: "b", op: "commit" },
    ];
    await settlePollWithScript(ctx, scriptView({ id: "S9", step: 3, total: 4, steps }));

    ctx.el("script-advance").click();
    await flush();
    const call = ctx.findCall("/OLD/script/advance", "POST");
    assert(!!call, "应发出推进请求");
    assertEq(call.body, JSON.stringify({ script_id: "S9", step: 3 }), "推进请求体");

    call.settled = true;
    call.resolve({
      status: 200,
      ok: true,
      json: async () => ({
        ok: true,
        script: scriptView({
          id: "S9", step: 4, total: 4, steps,
          records: [
            { step: 1, side: "a", op: "begin", result: "success", error: null },
            { step: 2, side: "a", op: "add", value: 5, result: "success", error: null },
            { step: 3, side: "b", op: "begin", result: "success", error: null },
          ],
        }),
      }),
    });
    await flush();
    assert(ctx.el("script-state").textContent.includes("当前第 4 / 4 步"), "步号前移到 4");
    const recs = ctx.el("script-records")._children;
    assertEq(recs.length, 3, "渲染三条记录");
    assert(recs.some((li) => li.textContent.includes("加上 5") && li.textContent.includes("成功")), "成功增量记录");
    // 推进后顺带刷新已提交值
    assert(!!ctx.findCall("/OLD/committed"), "推进后应刷新已提交值");
    ctx.settle("/OLD/committed", 200, { committed: 105 });
    await flush();
  });

  // 推进一步但该步失败（SQLITE_BUSY）：ok=false，步号停在原处可重试；
  // 记录显示原始错误名 / 码
  await test("步失败：不前移、可重试，记录原始错误名 / 码", async () => {
    const ctx = loadApp("?exp=OLD");
    await bootOldLive(ctx);
    await settlePollWithScript(ctx, scriptView({
      id: "S2", step: 4, total: 7,
      steps: [
        { step: 1, side: "a", op: "begin" },
        { step: 2, side: "a", op: "add", value: 5 },
        { step: 3, side: "b", op: "begin" },
        { step: 4, side: "b", op: "add", value: 7 },
        { step: 5, side: "a", op: "commit" },
        { step: 6, side: "b", op: "add", value: 7 },
        { step: 7, side: "b", op: "commit" },
      ],
    }));

    ctx.el("script-advance").click();
    await flush();
    const call = ctx.findCall("/OLD/script/advance", "POST");
    call.settled = true;
    call.resolve({
      status: 200,
      ok: true,
      json: async () => ({
        ok: false, // 该步执行失败，但请求本身正常
        script: scriptView({
          id: "S2", step: 4, total: 7,
          records: [{
            step: 4, side: "b", op: "add", value: 7,
            result: "failure",
            error: { name: "SQLITE_BUSY", code: 5, message: "database is locked" },
          }],
          steps: [
            { step: 1, side: "a", op: "begin" },
            { step: 2, side: "a", op: "add", value: 5 },
            { step: 3, side: "b", op: "begin" },
            { step: 4, side: "b", op: "add", value: 7 },
            { step: 5, side: "a", op: "commit" },
            { step: 6, side: "b", op: "add", value: 7 },
            { step: 7, side: "b", op: "commit" },
          ],
        }),
      }),
    });
    await flush();

    // 仍停在第 4 步（active），手工仍锁定；错误进记录（红字）而非顶部弹框
    assert(ctx.el("script-state").textContent.includes("当前第 4 / 7 步"), "失败后停在第 4 步");
    assertEq(ctx.el("script-advance").disabled, false, "仍可重试");
    assertEq(ctx.el("error-script").hidden, true, "步失败不在脚本错误框弹错");
    const recs = ctx.el("script-records")._children;
    assertEq(recs.length, 1, "一条失败记录");
    assert(recs[0].textContent.includes("SQLITE_BUSY"), "记录原始错误名");
    assert(recs[0].textContent.includes("错误码 5"), "记录原始错误码");
    assertEq(recs[0].classList.contains("log-error"), true, "失败记录标红");

    // 重试：仍带同样的步号
    ctx.el("script-advance").click();
    await flush();
    const retry = ctx.findCall("/OLD/script/advance", "POST");
    assertEq(retry.body, JSON.stringify({ script_id: "S2", step: 4 }), "重试仍带第 4 步");
  });

  // 跳过：携带步号，记录 skipped（黄色），前移
  await test("跳过本步：不执行、记 skipped 并前移", async () => {
    const ctx = loadApp("?exp=OLD");
    await bootOldLive(ctx);
    await settlePollWithScript(ctx, scriptView({ id: "S3", step: 4, total: 5 }));
    ctx.el("script-skip").click();
    await flush();
    const call = ctx.findCall("/OLD/script/skip", "POST");
    assertEq(call.body, JSON.stringify({ script_id: "S3", step: 4 }), "跳过请求体");
    call.settled = true;
    call.resolve({
      status: 200, ok: true,
      json: async () => ({
        ok: true,
        script: scriptView({
          id: "S3", step: 5, total: 5,
          records: [
            { step: 4, side: "b", op: "add", value: 7, result: "skipped", error: null },
          ],
        }),
      }),
    });
    await flush();
    const recs = ctx.el("script-records")._children;
    assert(recs.some((li) => li.textContent.includes("跳过")), "渲染跳过");
    assert(recs.some((li) => li.classList.contains("script-skipped")), "跳过记录黄色样式");
    assert(ctx.el("script-state").textContent.includes("当前第 5 / 5 步"), "跳过之后前移");
  });

  // 409 过期（步号已变 / 脚本 ID 不符）：不本地改动、显示错误、
  // 立即拉服务端状态重核对
  await test("推进 409（过期请求）：显示错误并以服务端状态重核对", async () => {
    const ctx = loadApp("?exp=OLD");
    await bootOldLive(ctx);
    await settlePollWithScript(ctx, scriptView({ id: "S4", step: 2, total: 3 }));

    ctx.el("script-advance").click();
    await flush();
    const call = ctx.findCall("/OLD/script/advance", "POST");
    call.settled = true;
    call.resolve({
      status: 409, ok: false,
      json: async () => ({ error: "stale step: current step is 3" }),
    });
    await flush();
    assertEq(ctx.el("error-script").hidden, false, "显示 409 错误");
    assert(ctx.el("error-script").textContent.includes("stale step"), "错误文案来自后端");
    // 立即重新拉状态
    const statusCalls = ctx.calls.filter(
      (c) => c.path.includes("/OLD/status") && !c.settled
    );
    assert(statusCalls.length >= 1, "409 后应立即重拉状态");
    // 服务端状态显示已经是第 3 步：本地被纠正
    ctx.settle("/OLD/status", 200, {
      id: "OLD",
      a: { in_transaction: false, readonly: false, last_read: null, log: [] },
      b: { in_transaction: false, readonly: false, last_read: null, log: [] },
      checkpoint: { last: null, log: [] },
      script: scriptView({ id: "S4", step: 3, total: 3 }),
    });
    await flush();
    assert(ctx.el("script-state").textContent.includes("当前第 3 / 3 步"), "以服务端状态为准");
  });

  // 完成：手工操作恢复，装载可用；推进 / 跳过 / 取消禁用
  await test("脚本完成：恢复手工操作（不自动提交 / 回滚由后端状态体现）", async () => {
    const ctx = loadApp("?exp=OLD");
    await bootOldLive(ctx);
    await settlePollWithScript(ctx, scriptView({ id: "S5", step: 1, total: 1, status: "active" }));
    ctx.el("script-advance").click();
    await flush();
    const call = ctx.findCall("/OLD/script/advance", "POST");
    call.settled = true;
    call.resolve({
      status: 200, ok: true,
      json: async () => ({
        ok: true,
        script: scriptView({
          id: "S5", step: null, total: 1, status: "finished",
          records: [{ step: 1, side: "a", op: "begin", result: "success", error: null }],
        }),
      }),
    });
    await flush();
    assertEq(ctx.el("script-state").classList.contains("finished"), true, "完成样式");
    assert(ctx.el("script-state").textContent.includes("已完成"), "显示已完成");
    assertEq(ctx.el("script-load").disabled, false, "完成后可装载新脚本");
    assertEq(ctx.el("script-advance").disabled, true, "完成后推进禁用");
    assertEq(ctx.el("script-skip").disabled, true, "完成后跳过禁用");
    assertEq(ctx.el("script-cancel").disabled, true, "完成后取消失用");
    assertEq(ctx.el("script-input").disabled, false, "输入框恢复");
    if (!sideManualEnabled(ctx, "a") || !sideManualEnabled(ctx, "b")) {
      throw new Error("完成后两栏手工应恢复");
    }
  });

  // 取消：在取消步号停止，手工恢复；不自动提交 / 回滚
  await test("取消脚本：停于当前步、恢复手工操作", async () => {
    const ctx = loadApp("?exp=OLD");
    await bootOldLive(ctx);
    await settlePollWithScript(ctx, scriptView({ id: "S6", step: 3, total: 5 }));
    ctx.el("script-cancel").click();
    await flush();
    const call = ctx.findCall("/OLD/script/cancel", "POST");
    assertEq(call.body, JSON.stringify({ script_id: "S6", step: 3 }), "取消请求体");
    call.settled = true;
    call.resolve({
      status: 200, ok: true,
      json: async () => ({
        ok: true,
        script: scriptView({ id: "S6", step: 3, total: 5, status: "cancelled" }),
      }),
    });
    await flush();
    assert(ctx.el("script-state").textContent.includes("已取消"), "显示已取消");
    assert(ctx.el("script-state").textContent.includes("停于第 3 步"), "显示停于第 3 步");
    assertEq(ctx.el("script-state").classList.contains("cancelled"), true, "取消样式");
    if (!sideManualEnabled(ctx, "a") || !sideManualEnabled(ctx, "b")) {
      throw new Error("取消后两栏手工应恢复");
    }
  });

  // 装载 409（两栏有事务 / 已有有效脚本）：显示后端错误、不本地装载
  await test("装载 409（互斥）：不装载、显示错误", async () => {
    const ctx = loadApp("?exp=OLD");
    await bootOldLive(ctx);
    ctx.el("script-input").value = '[{"side":"a","op":"begin"}]';
    ctx.el("script-load").click();
    await flush();
    const call = ctx.findCall("/OLD/script", "POST");
    call.settled = true;
    call.resolve({
      status: 409, ok: false,
      json: async () => ({ error: "both connections must be out of transaction before loading a script" }),
    });
    await flush();
    assertEq(ctx.el("error-script").hidden, false, "显示互斥错误");
    assert(ctx.el("script-state").textContent.includes("尚未装载脚本"), "本地不得变成已装载");
    assertEq(ctx.el("script-load").disabled, false, "装载按钮仍可用");
  });

  // 旧脚本动作的迟到 200 响应（在途期间新建实验）：不影响新页面
  await test("脚本动作在途时新建：旧 200 不影响新实验脚本区", async () => {
    const ctx = loadApp("?exp=OLD");
    await bootOldLive(ctx);
    await settlePollWithScript(ctx, scriptView({ id: "OLD-S", step: 1, total: 2 }));
    ctx.el("script-advance").click();
    await flush();
    const oldCall = ctx.findCall("/OLD/script/advance", "POST");

    ctx.el("new-exp").click();
    await flush();
    await settleNewCreated(ctx);
    assertEq(ctx.app().expId, "NEW", "已切到 NEW");

    oldCall.settled = true;
    oldCall.resolve({
      status: 200, ok: true,
      json: async () => ({
        ok: true,
        script: scriptView({
          id: "OLD-S", step: 2, total: 2,
          records: [{ step: 1, side: "a", op: "begin", result: "success", error: null }],
        }),
      }),
    });
    await flush();
    assertEq(ctx.app().expId, "NEW", "迟到响应后仍为 NEW");
    assert(ctx.el("script-state").textContent.includes("尚未装载脚本"), "新实验脚本区不被旧响应覆盖");
    assertEq(ctx.el("script-records")._children.length, 1, "仅占位提示一条");
    assertEq(ctx.el("script-records")._children[0].classList.contains("script-empty"), true, "占位提示");
  });

  // 服务端轮询带回一个不同的新脚本（旧脚本动作过期）：UI 立即跟随新脚本
  await test("轮询带回新脚本 ID：旧脚本本地状态被替换，动作按新 ID 发出", async () => {
    const ctx = loadApp("?exp=OLD");
    await bootOldLive(ctx);
    await settlePollWithScript(ctx, scriptView({ id: "OLD-S", step: 1, total: 2 }));
    await settlePollWithScript(ctx, scriptView({ id: "NEW-S", step: 1, total: 1, status: "active" }));
    assert(ctx.el("script-state").textContent.includes("NEW-S"), "显示新脚本 ID");
    ctx.el("script-advance").click();
    await flush();
    const call = ctx.findCall("/OLD/script/advance", "POST");
    assertEq(call.body, JSON.stringify({ script_id: "NEW-S", step: 1 }), "按新脚本 ID / 步号发请求");
  });

  // 创建进行中：脚本控件全部禁用；冻结后同样禁用
  await test("新建进行中 / 410 冻结：脚本控件禁用", async () => {
    const ctx = loadApp("?exp=OLD");
    await bootOldLive(ctx);
    ctx.el("new-exp").click();
    await flush();
    for (const id of ["script-load", "script-example", "script-advance", "script-skip", "script-cancel"]) {
      assertEq(ctx.el(id).disabled, true, `创建中 ${id} 禁用`);
    }
    ctx.settle("/api/experiments", 500, {}, "POST", { exact: true });
    await flush();

    // 410 冻结
    ctx.settle("/OLD/status", 410, {});
    await flush();
    assertEq(ctx.app().expFrozen, true, "已冻结");
    for (const id of ["script-load", "script-advance", "script-cancel"]) {
      assertEq(ctx.el(id).disabled, true, `冻结时 ${id} 禁用`);
    }
    const before = ctx.calls.length;
    ctx.el("script-load").click();
    ctx.el("script-advance").click();
    ctx.el("script-cancel").click();
    await flush();
    assertEq(ctx.calls.length, before, "冻结时点击脚本按钮不发请求");
  });

  // 示例按钮：填入 README 示例文本，不发请求；active 时禁用
  await test("示例按钮填入写锁跳过脚本", async () => {
    const ctx = loadApp("?exp=OLD");
    await bootOldLive(ctx);
    ctx.el("script-example").click();
    await flush();
    const parsed = JSON.parse(ctx.el("script-input").value);
    assertEq(parsed.length, 7, "示例 7 步");
    assertEq(parsed[3].side, "b", "第 4 步乙");
    assertEq(parsed[3].op, "add", "第 4 步乙加法（撞锁后人工跳过）");
    assertEq(parsed[4].op, "commit", "第 5 步甲提交");
    assertEq(parsed[5].op, "add", "第 6 步乙再次加法");
  });

  console.log(`\n${passed} 通过，${failed} 失败`);
  process.exit(failed === 0 ? 0 : 1);
}

main();
