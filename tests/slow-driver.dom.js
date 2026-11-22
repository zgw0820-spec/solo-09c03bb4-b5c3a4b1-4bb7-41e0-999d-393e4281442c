// jsdom drill for the slow driver: sets a per-step delay, clicks confirm, and
// while the request is still in flight observes partial layout + pending
// steps, then has a second "page" attempt conflicting actions and checks the
// busy refusal.  Runs the real app.js against a live server on $BASE_URL.
const fs = require("fs");
const path = require("path");
const { JSDOM } = require("/tmp/node_modules/jsdom");

const BASE = process.env.BASE_URL || "http://127.0.0.1:8000";
const APP = path.resolve(__dirname, "../app");

function assert(cond, message) {
  if (!cond) throw new Error("断言失败：" + message);
  console.log("  ✓ " + message);
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function makePage(html, appJs) {
  const dom = new JSDOM(html, {
    url: BASE + "/", runScripts: "outside-only", pretendToBeVisual: true,
  });
  const { window } = dom;
  const timers = new Set();
  window.fetch = async (url, opts = {}) => {
    const res = await fetch(new URL(url, BASE), {
      method: opts.method || "GET",
      headers: opts.headers,
      body: opts.body,
    });
    const text = await res.text();
    return {
      ok: res.ok, status: res.status,
      json: async () => JSON.parse(text), text: async () => text,
    };
  };
  window.setTimeout = setTimeout;
  window.clearTimeout = clearTimeout;
  // Track intervals so the page's live poll can be stopped before exit.
  window.setInterval = (fn, ms) => {
    const id = setInterval(fn, ms);
    timers.add(id);
    return id;
  };
  window.clearInterval = (id) => {
    timers.delete(id);
    clearInterval(id);
  };
  window.console = console;
  window.eval("window.__h = {};\n" +
    appJs.replace("boot();", "window.__h.boot = boot; window.__h.act = act;"));
  window.__stopTimers = () => { timers.forEach(clearInterval); timers.clear(); };
  return { dom, window };
}

async function post(pathname, body) {
  const res = await fetch(new URL(pathname, BASE), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  const text = await res.text();
  let json = null;
  try { json = JSON.parse(text); } catch (_) {}
  return { status: res.status, json, text };
}

async function main() {
  const html = fs.readFileSync(path.join(APP, "templates/index.html"), "utf8");
  const appJs = fs.readFileSync(path.join(APP, "static/app.js"), "utf8");
  const page = makePage(html, appJs);

  await post("/api/layout/reset", null);
  await post("/api/driver/delay", { step_delay_ms: 0 });
  await page.window.__h.boot();
  await sleep(250);
  const doc = page.window.document;
  const q = (s) => doc.querySelector(s);
  const qa = (s) => Array.from(doc.querySelectorAll(s));

  console.log("慢驱动场景 1：网页设置单步延迟");
  q("#driver-delay").value = "400";
  q("#driver-delay-btn").click();
  await sleep(350);
  assert(q("#driver-delay-current").textContent.includes("400"), "页面显示当前延迟 400 ms");
  assert(q("#toast").textContent.includes("单步延迟已设为 400"), "设置成功提示");

  console.log("慢驱动场景 2：规划并确认（驱动逐步执行）");
  qa(".spec-select").forEach((sel) => { sel.value = "1g.10gb"; });
  q("#plan-btn").click();
  await sleep(400);
  assert(q(".plan-box.ok"), "方案可行");
  const confirmBtn = qa("#batch-actions button").find((b) => b.textContent.includes("确认方案"));
  assert(confirmBtn, "存在确认方案按钮");
  confirmBtn.click();

  // While the confirm request is in flight, the app polls /api/state.
  let sawBusy = false;
  let sawPartial = false;
  let sawPending = false;
  let sawBannerText = false;
  let sawControlsDisabled = false;
  const deadline = Date.now() + 8000;
  while (Date.now() < deadline) {
    await sleep(120);
    if (!q("#busy-banner").hidden) {
      const bannerText = q("#busy-text").textContent;
      if (bannerText.includes("驱动正在执行") && bannerText.includes("会被拒绝")) {
        sawBannerText = true;
      }
      const text = q("#batch-summary").textContent;
      if (/执行中：已完成 \d+\/3 项/.test(text)) {
        sawBusy = true;
        if (/已完成 [12]\/3 项/.test(text)) sawPartial = true;
        if (text.includes("待完成步骤")) sawPending = true;
      }
      if (q("#reset-btn").disabled && q("#plan-btn").disabled &&
          Array.from(qa("#batch-actions button")).every((b) => b.disabled)) {
        sawControlsDisabled = true;
      }
    }
    const pill = q("#batch-pill").textContent;
    if (pill.includes("已交付") || pill.includes("执行失败")) break;
  }
  assert(sawBusy, "驱动执行中页面显示进度 已完成 n/3");
  assert(sawPartial, "刷新时能看到部分生效的进度（完成 1 或 2 项）");
  assert(sawPending, "刷新时展示尚未完成的步骤");
  assert(sawBannerText, "执行中显示忙提示条并说明冲突会被拒绝");
  assert(sawControlsDisabled, "执行中本页操作按钮被禁用");

  // Wait for delivery and confirm busy clears.
  const doneDeadline = Date.now() + 8000;
  while (Date.now() < doneDeadline && !q("#batch-pill").textContent.includes("已交付")) {
    await sleep(150);
  }
  assert(q("#batch-pill").textContent.includes("已交付"), "慢驱动结束后交付");
  await sleep(300);
  assert(q("#busy-banner").hidden === true, "结束后忙提示条消失");
  assert(q("#batch-summary").textContent.includes("与方案一致"), "汇总包含一致性");
  assert(q("#batch-summary").textContent.includes("是"), "最终与方案一致");
  assert(q("#reset-btn").disabled === false, "结束后按钮恢复可点");

  console.log("慢驱动场景 3：第二个页面在驱动执行中刷新 + 冲突操作被拒");
  // Reset delay to 350 and start a fresh batch driven directly over HTTP so
  // the first tab is free to poll.
  await post("/api/layout/reset", null);
  await post("/api/driver/delay", { step_delay_ms: 350 });
  await post("/api/plan", { new_slices: [{ spec: "1g.10gb" }, { spec: "1g.10gb" }] });
  const confirmPromise = post("/api/batch/confirm");
  // Give the driver a moment to commit the first step.
  await sleep(450);
  const live = await fetch(BASE + "/api/state").then((r) => r.json());
  assert(live.busy === true, "另一页面刷新看到 busy=true");
  assert(live.batch.driver.running === true, "另一页面看到 driver.running=true");
  assert(live.batch.driver.done >= 1 && live.batch.driver.done < live.batch.driver.total,
    `另一页面看到部分完成 ${live.batch.driver.done}/${live.batch.driver.total}`);
  const changed = live.batch.actual.remaining_removals.some((r) => r.state === "removed");
  const missing = live.batch.actual.created.some((c) => c.state === "missing");
  assert(changed, "刷新看到已生效的拆除");
  assert(missing, "刷新看到尚未完成的创建");

  const conflicts = [
    ["/api/batch/confirm", null, "重复执行"],
    ["/api/batch/retry", null, "重试"],
    ["/api/batch/abandon", null, "放弃"],
    ["/api/external/add-idle", { spec: "1g.10gb", interval: { start: 6, end: 6 } }, "外部拆建（新增）"],
    ["/api/external/remove", { slice_id: "idle-1" }, "外部拆建（移除）"],
    ["/api/layout/reset", null, "重置布局"],
    ["/api/layout/import", live.default_layout, "导入布局"],
  ];
  for (const [pathname, body, label] of conflicts) {
    const res = await post(pathname, body);
    assert(res.status === 409, `${label} 返回 409`);
    assert(String(res.json.detail).includes("驱动正在执行"), `${label} 提示忙：${res.json.detail.slice(0, 40)}`);
  }

  const after = await confirmPromise;
  assert(after.status === 200, "原确认请求正常完成");
  assert(after.json.batch.state === "delivered", "驱动完成后交付");
  const finalState = await fetch(BASE + "/api/state").then((r) => r.json());
  assert(finalState.busy === false, "完成后 busy 复位");
  assert(finalState.batch.actual.layout_matches_plan === true, "最终实际布局与方案一致");
  // The refused external add must not have landed.
  assert(!finalState.layout.slices.some((s) => s.id === "ext-1"), "被拒的外部新增没有生效");

  console.log("慢驱动场景 4：改卡后报错 → 刷新看到变化与原因 → 重试补齐");
  await post("/api/layout/reset", null);
  await post("/api/driver/delay", { step_delay_ms: 120 });
  await post("/api/fault", { mode: "after_apply", skip: 1, count: 1, message: "NVML: device busy" });
  await post("/api/plan", { new_slices: [{ spec: "1g.10gb" }, { spec: "1g.10gb" }] });
  const failed = await post("/api/batch/confirm");
  assert(failed.json.batch.state === "failed", "批次失败");
  assert(failed.json.batch.error.includes("device busy"), "保留驱动错误原因");
  const fv = failed.json.batch.driver;
  assert(fv.running === false && fv.done >= 1 && fv.done < fv.total,
    `刷新可见已生效 ${fv.done}/${fv.total} 项`);
  const removedNow = failed.json.batch.actual.remaining_removals[0].state === "removed";
  const stillMissing = failed.json.batch.actual.created.some((c) => c.state === "missing");
  assert(removedNow, "刷新看到已生效的拆除");
  assert(stillMissing, "刷新看到仍未完成的创建");
  assert(failed.json.batch.actual.layout_matches_plan === false, "失败后布局与方案不一致");

  // The page itself renders the failure and exposes retry.
  await page.window.__h.act("/api/layout/reset", {});
  await sleep(100);
  qa(".spec-select").forEach((sel) => { sel.value = "1g.10gb"; });
  q("#driver-delay").value = "80";
  q("#driver-delay-btn").click();
  await sleep(200);
  q("#plan-btn").click();
  await sleep(300);
  await post("/api/fault", { mode: "after_apply", skip: 1, count: 1, message: "NVML: device busy" });
  qa("#batch-actions button").find((b) => b.textContent.includes("确认方案")).click();
  await sleep(900);
  assert(q("#batch-pill").textContent.includes("执行失败"), "页面显示执行失败");
  assert(q("#batch-summary").textContent.includes("device busy"), "页面展示失败原因");
  assert(q("#batch-summary").textContent.includes("实际已建") || q("#batch-summary").textContent.includes("尚未建成"),
    "页面展示实际已建/未建成");
  const retryBtn = qa("#batch-actions button").find((b) => b.textContent.includes("重试"));
  assert(retryBtn, "存在重试按钮");
  retryBtn.click();
  const retryDeadline = Date.now() + 8000;
  while (Date.now() < retryDeadline && !q("#batch-pill").textContent.includes("已交付")) {
    await sleep(150);
  }
  assert(q("#batch-pill").textContent.includes("已交付"), "重试后交付");
  assert(q("#batch-summary").textContent.includes("是"), "重试后与方案一致");

  // Clean up knobs for later runs.
  await post("/api/driver/delay", { step_delay_ms: 0 });
  await post("/api/fault/clear", null);
  page.window.__stopTimers();
  page.dom.window.close();
  console.log("\n慢驱动 jsdom 演练全部通过。");
}

main().catch((e) => { console.error("\n测试失败：", e.message); process.exit(1); });
