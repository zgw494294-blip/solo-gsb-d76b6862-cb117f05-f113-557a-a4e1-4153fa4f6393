"use strict";

// 验证每栏只读开关（PRAGMA query_only）的前端行为：
// 独立开关、请求期间禁用本栏、布尔请求体、状态同步、删除冻结 / 404 /
// 迟到响应隔离与新建重置。响应顺序完全可控，不依赖真实时间与网络。
// 运行：node tests/readonly.test.js

const {
  loadApp,
  flush,
  bootOldLive,
  settleNewCreated,
} = require("./harness");

let passed = 0;
let failed = 0;

function assertEq(actual, expected, message) {
  if (actual !== expected) {
    throw new Error(`${message}（期望 ${expected}，实际 ${actual}）`);
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

// 本栏全部可操作元素（5 个按钮 + 加法输入框 + 只读复选框）
function sideEnabled(ctx, side) {
  const enabled = ["begin", "read", "add", "commit", "rollback"].every(
    (op) => ctx.opButton(side, op).disabled === false
  );
  return (
    enabled &&
    ctx.el(`add-input-${side}`).disabled === false &&
    ctx.el(`ro-toggle-${side}`).disabled === false
  );
}
function sideDisabled(ctx, side) {
  const disabled = ["begin", "read", "add", "commit", "rollback"].every(
    (op) => ctx.opButton(side, op).disabled === true
  );
  return (
    disabled &&
    ctx.el(`add-input-${side}`).disabled === true &&
    ctx.el(`ro-toggle-${side}`).disabled === true
  );
}

async function main() {
  // 初始：两栏开关关闭、文案为关闭；状态中的 readonly 同步到文案与复选框
  await test("状态中的 readonly 同步到文案与复选框", async () => {
    const ctx = loadApp("?exp=OLD");
    await bootOldLive(ctx);
    assertEq(ctx.el("ro-a").textContent, "关闭", "甲默认显示关闭");
    assertEq(ctx.el("ro-toggle-a").checked, false, "甲复选框默认不勾");
    assertEq(ctx.el("ro-b").textContent, "关闭", "乙默认显示关闭");

    const before = ctx.calls.length;
    ctx.el("ro-toggle-a").change(true);
    await flush();
    const call = ctx.calls
      .slice(before)
      .find((c) => c.path.includes("/conn/a/readonly") && c.method === "POST");
    if (!call) throw new Error("应发出甲栏只读开关请求");
    assertEq(call.body, JSON.stringify({ enabled: true }), "请求体必须是布尔 true");

    call.settled = true;
    call.resolve({
      status: 200,
      ok: true,
      json: async () => ({
        ok: true,
        error: null,
        state: {
          in_transaction: false,
          readonly: true,
          last_read: null,
          log: ["只读模式：开启 → 成功"],
        },
      }),
    });
    await flush();
    assertEq(ctx.el("ro-a").textContent, "开启", "甲显示开启");
    assertEq(ctx.el("ro-toggle-a").checked, true, "甲复选框已勾选");
    assertEq(ctx.el("ro-a").classList.contains("on"), true, "开启时加 on 样式");
  });

  // 请求期间禁用本栏全部控件，另一栏不受影响；结束后恢复
  await test("开关请求进行中禁用本栏、另一栏照常；完成后恢复", async () => {
    const ctx = loadApp("?exp=OLD");
    await bootOldLive(ctx);

    ctx.el("ro-toggle-b").change(true);
    await flush();
    if (!sideDisabled(ctx, "b")) throw new Error("请求中乙栏应全部禁用");
    if (!sideEnabled(ctx, "a")) throw new Error("乙请求不得禁用甲栏");

    ctx.settle("/OLD/conn/b/readonly", 200, {
      ok: true,
      error: null,
      state: { in_transaction: false, readonly: true, last_read: null, log: [] },
    });
    await flush();
    if (!sideEnabled(ctx, "b")) throw new Error("完成后乙栏应恢复可用");
  });

  // 两栏独立：甲开启不影响乙的请求与状态
  await test("两栏开关互相独立", async () => {
    const ctx = loadApp("?exp=OLD");
    await bootOldLive(ctx);
    const before = ctx.calls.length;

    ctx.el("ro-toggle-a").change(true);
    await flush();
    const aCall = ctx.calls
      .slice(before)
      .find((c) => c.path.includes("/conn/a/readonly"));
    aCall.settled = true;
    aCall.resolve({
      status: 200,
      ok: true,
      json: async () => ({
        ok: true,
        error: null,
        state: { in_transaction: false, readonly: true, last_read: null, log: [] },
      }),
    });
    await flush();

    assertEq(ctx.el("ro-a").textContent, "开启", "甲开启");
    assertEq(ctx.el("ro-b").textContent, "关闭", "乙仍关闭");
    assertEq(ctx.el("ro-toggle-b").checked, false, "乙复选框不勾");

    // 甲在只读时，乙栏仍可发起自己的开关请求（路径落在 b）
    const before2 = ctx.calls.length;
    ctx.el("ro-toggle-b").change(true);
    await flush();
    const bCall = ctx.calls
      .slice(before2)
      .find((c) => c.path.includes("/OLD/conn/b/readonly"));
    if (!bCall) throw new Error("乙应能独立发起开关请求");
    assertEq(bCall.body, JSON.stringify({ enabled: true }), "乙请求体为布尔");
  });

  // 瞬时请求失败：复选框回退、显示错误、控件恢复
  await test("开关请求网络失败：复选框回退、错误显示在本栏", async () => {
    const ctx = loadApp("?exp=OLD");
    await bootOldLive(ctx);
    ctx.el("ro-toggle-a").change(true);
    await flush();
    const call = ctx.findCall("/OLD/conn/a/readonly", "POST");
    call.settled = true;
    call.reject(new TypeError("network down"));
    await flush();
    assertEq(ctx.el("ro-toggle-a").checked, false, "失败后复选框回退");
    assertEq(ctx.el("error-a").hidden, false, "本栏显示错误");
    if (!sideEnabled(ctx, "a")) throw new Error("失败后本栏恢复可用");
    assertEq(ctx.el("ro-a").textContent, "关闭", "文案保持关闭");
  });

  // 410：冻结，旧控件禁用、轮询停止，与其它旧操作规则一致
  await test("开关请求 410：冻结旧实验", async () => {
    const ctx = loadApp("?exp=OLD");
    await bootOldLive(ctx);
    ctx.el("ro-toggle-a").change(true);
    await flush();
    ctx.settle("/OLD/conn/a/readonly", 410, {});
    await flush();
    assertEq(ctx.app().expFrozen, true, "410 后冻结");
    assertEq(ctx.app().timerCount, 0, "轮询停止");
    assertEq(ctx.el("deleted-note").hidden, false, "显示冻结提示");
    if (!sideDisabled(ctx, "a") || !sideDisabled(ctx, "b")) {
      throw new Error("两栏都应禁用");
    }
  });

  // 404：沿用旧语义自动新建；新建成功后开关重置为关闭
  await test("开关请求 404：自动新建，新实验开关重置", async () => {
    const ctx = loadApp("?exp=OLD");
    await bootOldLive(ctx);
    ctx.el("ro-toggle-a").change(true);
    await flush();
    // 请求在途时复选框为勾选
    assertEq(ctx.el("ro-toggle-a").checked, true, "请求中复选框为新状态");
    ctx.settle("/OLD/conn/a/readonly", 404, {});
    await flush();
    await settleNewCreated(ctx);
    assertEq(ctx.app().expId, "NEW", "404 后切到 NEW");
    assertEq(ctx.el("ro-toggle-a").checked, false, "新实验复选框重置为不勾");
    assertEq(ctx.el("ro-a").textContent, "关闭", "新实验文案为关闭");
  });

  // 迟到响应：开关在途期间新建，旧 200 不得改变新页面
  await test("旧开关 200 在新建成功后收尾：不改新页面只读状态", async () => {
    const ctx = loadApp("?exp=OLD");
    await bootOldLive(ctx);
    ctx.el("ro-toggle-a").change(true);
    await flush();
    const oldCall = ctx.findCall("/OLD/conn/a/readonly", "POST");

    ctx.el("new-exp").click();
    await flush();
    await settleNewCreated(ctx);
    assertEq(ctx.app().expId, "NEW", "已切到 NEW");

    oldCall.settled = true;
    oldCall.resolve({
      status: 200,
      ok: true,
      json: async () => ({
        ok: true,
        error: null,
        state: { in_transaction: true, readonly: true, last_read: 105, log: ["OLD-ro"] },
      }),
    });
    await flush();
    assertEq(ctx.app().expId, "NEW", "迟到响应后仍为 NEW");
    assertEq(ctx.el("ro-a").textContent, "关闭", "迟到只读=true 不得覆盖新页面");
    assertEq(ctx.el("ro-toggle-a").checked, false, "新复选框不被迟到响应勾选");
  });

  // 新建 / 冻结期间点击开关不发请求
  await test("冻结页操作复选框不发请求", async () => {
    const ctx = loadApp("?exp=OLD");
    await flush();
    ctx.settle("/OLD/status", 410, {});
    await flush();
    assertEq(ctx.app().expFrozen, true, "前置：已冻结");
    const before = ctx.calls.length;
    ctx.el("ro-toggle-b").change(true);
    await flush();
    assertEq(ctx.calls.length, before, "冻结时不得发开关请求");
    assertEq(ctx.el("ro-toggle-b").checked, false, "冻结复选框保持不勾");
  });

  // 放行一次轮询：状态轮询（本栏只读为 ro）+ 已提交值轮询
  async function settlePoll(ctx, side, ro) {
    ctx.tickTimers();
    await flush();
    ctx.settle("/OLD/status", 200, {
      id: "OLD",
      a: {
        in_transaction: false,
        readonly: side === "a" ? ro : false,
        last_read: null,
        log: [],
      },
      b: {
        in_transaction: false,
        readonly: side === "b" ? ro : false,
        last_read: null,
        log: [],
      },
      checkpoint: { last: null, log: [] },
    });
    await flush();
    ctx.settle("/OLD/committed", 200, { committed: 111 });
    await flush();
  }

  // 关键交错：勾选开启 → 请求在途 → 轮询先确认 readonly=true（文案变开启）
  // → 开关请求失败。失败后复选框与文案必须一致地停在“最近一次服务端确认”
  // 的模式（开启），错误显示在本栏；随后即使轮询返回完全相同的状态
  // （JSON 未变、日志 / 读值都没变），本地偏差也能被再次纠正。
  for (const side of ["a", "b"]) {
    const other = side === "a" ? "b" : "a";
    await test(`交错失败：轮询先确认开启后开关请求失败（${side} 栏）`, async () => {
      const ctx = loadApp("?exp=OLD");
      await bootOldLive(ctx);

      ctx.el(`ro-toggle-${side}`).change(true);
      await flush();
      assertEq(ctx.el(`ro-${side}`).textContent, "开启", "请求中乐观文案为开启");
      assertEq(ctx.el(`ro-toggle-${side}`).checked, true, "请求中复选框勾选");
      if (!sideDisabled(ctx, side)) throw new Error("请求期间本栏应禁用");
      if (!sideEnabled(ctx, other)) throw new Error("请求期间另一栏应可操作");
      const toggleCall = ctx.findCall(`/OLD/conn/${side}/readonly`, "POST");

      // 轮询先确认：服务端模式已是开启
      await settlePoll(ctx, side, true);
      assertEq(ctx.el(`ro-${side}`).textContent, "开启", "轮询确认后文案开启");
      assertEq(ctx.el(`ro-toggle-${side}`).checked, true, "轮询确认后复选框勾选");

      // 开关请求随后网络失败
      toggleCall.settled = true;
      toggleCall.reject(new TypeError("network down"));
      await flush();

      // 修复前的矛盾：复选框退回关闭、文案仍为开启
      assertEq(ctx.el(`ro-${side}`).textContent, "开启", "失败后按最近确认恢复：文案开启");
      assertEq(
        ctx.el(`ro-toggle-${side}`).checked,
        true,
        "失败后按最近确认恢复：复选框勾选，与文案一致"
      );
      assertEq(ctx.el(`error-${side}`).hidden, false, "错误显示在本栏");
      if (!sideEnabled(ctx, side)) throw new Error("失败后本栏恢复可用");
      if (!sideEnabled(ctx, other)) throw new Error("另一栏始终可操作");

      // 人为制造本地偏差（修复前相同状态轮询无法纠正）：同模式轮询应纠正
      ctx.el(`ro-toggle-${side}`).checked = false;
      ctx.el(`ro-${side}`).textContent = "关闭";
      await settlePoll(ctx, side, true);
      assertEq(
        ctx.el(`ro-toggle-${side}`).checked,
        true,
        "相同状态轮询也能把复选框纠正回开启"
      );
      assertEq(ctx.el(`ro-${side}`).textContent, "开启", "相同状态轮询把文案纠正回开启");
    });
  }

  // 失败时期间没有新确认：恢复原已确认模式（两栏各覆盖 开启→取消 与 关闭→勾选）
  const rollbackCases = [
    { side: "a", confirmed: true, attempt: false },
    { side: "b", confirmed: false, attempt: true },
  ];
  for (const c of rollbackCases) {
    await test(
      `无新确认的失败回退：${c.side} 栏已确认${c.confirmed ? "开启" : "关闭"}，` +
        `尝试${c.attempt ? "开启" : "关闭"}失败后恢复`,
      async () => {
        const ctx = loadApp("?exp=OLD");
        await bootOldLive(ctx);

        // 先用一次轮询把本栏带到“已确认模式”
        await settlePoll(ctx, c.side, c.confirmed);
        assertEq(ctx.el(`ro-${c.side}`).textContent, c.confirmed ? "开启" : "关闭", "前置文案");
        assertEq(
          ctx.el(`ro-toggle-${c.side}`).checked,
          c.confirmed,
          "前置复选框"
        );

        ctx.el(`ro-toggle-${c.side}`).change(c.attempt);
        await flush();
        assertEq(
          ctx.el(`ro-${c.side}`).textContent,
          c.attempt ? "开启" : "关闭",
          "请求期间文案与勾选一致（乐观）"
        );
        // 请求期间不放行任何轮询：没有新的服务端确认
        const toggleCall = ctx.findCall(`/OLD/conn/${c.side}/readonly`, "POST");
        assertEq(
          toggleCall.body,
          JSON.stringify({ enabled: c.attempt }),
          "请求体为目标布尔值"
        );
        toggleCall.settled = true;
        toggleCall.reject(new TypeError("network down"));
        await flush();

        // 恢复点击前的已确认模式，勾选与文案一致
        assertEq(
          ctx.el(`ro-toggle-${c.side}`).checked,
          c.confirmed,
          "失败后复选框恢复原已确认模式"
        );
        assertEq(
          ctx.el(`ro-${c.side}`).textContent,
          c.confirmed ? "开启" : "关闭",
          "失败后文案恢复原已确认模式，与复选框一致"
        );
        assertEq(ctx.el(`error-${c.side}`).hidden, false, "错误显示在本栏");
        if (!sideEnabled(ctx, c.side)) throw new Error("失败后本栏恢复可用");

        // 相同状态轮询（仍是原模式）同样能纠正本地偏差
        ctx.el(`ro-toggle-${c.side}`).checked = c.attempt;
        ctx.el(`ro-${c.side}`).textContent = c.attempt ? "开启" : "关闭";
        await settlePoll(ctx, c.side, c.confirmed);
        assertEq(
          ctx.el(`ro-toggle-${c.side}`).checked,
          c.confirmed,
          "相同状态轮询纠正复选框"
        );
        assertEq(
          ctx.el(`ro-${c.side}`).textContent,
          c.confirmed ? "开启" : "关闭",
          "相同状态轮询纠正文案"
        );
      }
    );
  }

  console.log(`\n${passed} 通过，${failed} 失败`);
  process.exit(failed === 0 ? 0 : 1);
}

main();
