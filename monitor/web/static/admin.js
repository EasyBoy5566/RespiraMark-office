/* 管理頁（/admin，僅 admin 角色；前端分工見 CLAUDE.md §5）
 * - 設備健康總表：卡片格線（視覺語言比照主儀表板 .card），每張卡是連線 /
 *   CPU / 記憶體 / 溫度 / 磁碟 / 降頻 / 開機時長
 * - 點「趨勢」在卡片內就地展開系統狀態趨勢圖（RMSys 共用繪圖，與儀表板同一套門檻）
 * - 移除離線裝置（DELETE /api/admin/devices）、匯出七天 sys CSV、帳號唯讀清單
 * - 連線狀態（呼吸器序列埠 status / 伺服器 link）顯示在卡片標題列（儀表板不再顯示）
 * - 資料來源與儀表板相同：WebSocket /ws（本頁忽略波形等臨床訊息，只用 sys/link/status）
 */
"use strict";

const connEl = document.getElementById("conn");
const devGrid = document.getElementById("devGrid");
const devEmpty = document.getElementById("devEmpty");

const devices = new Map();   // device_id -> Dev

// ── 主題（與儀表板共用同一個 localStorage 設定）──────────────────
const themeBtn = document.getElementById("themeToggle");

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  try { localStorage.setItem("rm-theme", theme); } catch (e) { /* 私密瀏覽等，忽略 */ }
  themeBtn.textContent = theme === "light" ? "🌙 深色" : "☀ 淺色";
  redrawOpenTrends();          // 展開中的趨勢圖顏色隨主題重畫
}

themeBtn.addEventListener("click", () =>
  applyTheme(document.documentElement.dataset.theme === "light" ? "dark" : "light"));

let initTheme = "dark";
try { initTheme = localStorage.getItem("rm-theme") || "dark"; } catch (e) { /* 同上 */ }
applyTheme(initTheme);

// ── 數值格式化 ───────────────────────────────────────────────────
const isMissing = (v) => v === null || v === undefined || v === "";
const fmtGB = (v) => (isMissing(v) ? "—" : `${Math.round(v * 10) / 10} GB`);

// ── 設備卡片 ─────────────────────────────────────────────────────
function ensureDev(id) {
  let dev = devices.get(id);
  if (dev) return dev;
  dev = { id, online: false, sysHist: [], lastSys: null,
          bed: "", asset: "",          // 裝置清冊（devices.json），只有這頁能改
          card: null, chips: {}, trendOpen: false, trendChans: null,
          histFetched: false, removeBtn: null, trendBtn: null };
  buildCard(dev);
  devices.set(id, dev);
  sortGrid();
  applyFilter();               // 新卡片也要遵守目前的搜尋條件
  return dev;
}

function buildCard(dev) {
  const card = document.createElement("div");
  card.className = "admin-card";
  card.dataset.device = dev.id;
  card.innerHTML = `
    <div class="admin-card-head">
      <span class="dev-name"></span>
      <span class="dev-ids"></span>
      <span class="spacer"></span>
      <span class="status-group" title="Pi 與呼吸器之間的序列埠連線狀態">
        <span class="status-tag">呼吸器</span>
        <span class="vent-status">—</span>
      </span>
      <span class="status-group" title="這台 Pi 與中央伺服器之間的網路連線狀態">
        <span class="status-tag">伺服器</span>
        <span class="link-status off">離線</span>
      </span>
    </div>
    <div class="metric-grid"></div>
    <div class="admin-card-info"></div>
    <div class="admin-card-foot">
      <button type="button" class="admin-btn meta-btn">床號／財編</button>
      <button type="button" class="admin-btn trend-btn">趨勢 ▾</button>
      <button type="button" class="admin-btn">下載 CSV</button>
      <button type="button" class="admin-btn">警報紀錄</button>
      <button type="button" class="admin-btn danger">移除</button>
    </div>
    <div class="admin-trend hidden">
      <div class="sys-info-line"></div>
      <div class="sys-charts"></div>
    </div>`;
  renderDevIdentity(dev, card);

  const grid = card.querySelector(".metric-grid");
  for (const metric of RMSys.METRICS) {
    const chip = document.createElement("div");
    chip.className = "metric-chip";
    chip.innerHTML = `<div class="metric-label">${metric.label}</div>
                       <div class="metric-val">—</div>`;
    grid.appendChild(chip);
    dev.chips[metric.key] = chip.querySelector(".metric-val");
  }

  const [metaBtn, trendBtn, csvBtn, alarmBtn, removeBtn] =
    card.querySelectorAll(".admin-card-foot button");
  metaBtn.addEventListener("click", () => editMeta(dev));
  metaBtn.title = "設定這台機器的床號與呼吸器財編（看板以床號顯示）";
  trendBtn.addEventListener("click", () => toggleTrend(dev));
  csvBtn.addEventListener("click", () => downloadCsv(dev.id));
  csvBtn.title = "從 SQLite 匯出這台機器最近 7 天的系統狀態 CSV";
  alarmBtn.addEventListener("click", () => downloadAlarmLog(dev.id));
  alarmBtn.title = "下載這台機器最近 7 天的警報歷史（由 SQLite 匯出 CSV）";
  removeBtn.addEventListener("click", () => removeDevice(dev.id));
  dev.trendBtn = trendBtn;
  dev.removeBtn = removeBtn;

  dev.card = card;
  devGrid.appendChild(card);
  renderCard(dev);
}

// numeric 讓 RCC-2 排在 RCC-10 前面，而不是一般字串順序的 1、10、2。
const collate = (a, b) =>
  a.localeCompare(b, undefined, { numeric: true, sensitivity: "base" });

function sortGrid() {
  [...devGrid.children]
    // 與看板一致：依床號排序，未指定床號的排在最後（改以機台編號互相排序）
    .sort((a, b) => {
      const bedA = a.dataset.bed || "";
      const bedB = b.dataset.bed || "";
      if (!bedA !== !bedB) return bedA ? -1 : 1;
      return collate(bedA, bedB) || collate(a.dataset.device, b.dataset.device);
    })
    .forEach((el) => devGrid.appendChild(el));
}

// ── 裝置清冊：床號與呼吸器財編（PROTOCOL.md「裝置床號與財編」）────────
// 床號是看板卡片的標題；財編對應實體呼吸器，供盤點與報修，刻意只在本頁顯示。
function renderDevIdentity(dev, card) {
  const el = card || dev.card;
  const name = el.querySelector(".dev-name");
  name.textContent = dev.bed || dev.id;
  name.classList.toggle("unassigned", !dev.bed);
  // 標題已經是床號時，機台編號與財編改放旁邊，管理員仍看得到完整身分
  const ids = [dev.bed ? dev.id : null, dev.asset ? `財編 ${dev.asset}` : null]
    .filter(Boolean).join(" · ");
  el.querySelector(".dev-ids").textContent = ids;
  el.dataset.bed = dev.bed;
}

function applyMeta(dev, m) {
  dev.bed = m.bed || "";
  dev.asset = m.asset || "";
  renderDevIdentity(dev);
}

function editMeta(dev) {
  const bed = prompt(`${dev.id} 的床號（留空 = 未指定）`, dev.bed);
  if (bed === null) return;
  const asset = prompt(`${dev.id} 的呼吸器財編（留空 = 未指定）`, dev.asset);
  if (asset === null) return;
  fetch(`/api/admin/devices/${encodeURIComponent(dev.id)}/meta`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ bed: bed.trim(), asset: asset.trim() }),
  })
    .then((r) => {
      // 成功時不用動畫面：伺服器會廣播 device_meta，看板與本頁一起更新
      if (!r.ok) return r.json().then((j) => alert(j.error || "設定失敗"));
    })
    .catch(() => alert("設定失敗：無法連線伺服器"));
}

// 搜尋：床號、機台編號、財編任一符合就顯示。財編是實體機器上的標籤，
// 用它就能查出這台呼吸器現在在哪一床。
const deviceFilter = document.getElementById("devFilter");

function applyFilter() {
  const q = (deviceFilter.value || "").trim().toLowerCase();
  let shown = 0;
  for (const dev of devices.values()) {
    const hit = !q || [dev.bed, dev.id, dev.asset]
      .some((v) => (v || "").toLowerCase().includes(q));
    dev.card.classList.toggle("hidden", !hit);
    if (hit) shown++;
  }
  devEmpty.classList.toggle("hidden", shown > 0);
  devEmpty.textContent = devices.size && !shown
    ? "沒有符合的裝置" : "等待裝置連線";
}

deviceFilter.addEventListener("input", applyFilter);

function renderCard(dev) {
  const m = dev.lastSys || {};
  const link = dev.card.querySelector(".link-status");
  link.textContent = dev.online ? "連線" : "離線";
  link.className = `link-status ${dev.online ? "on" : "off"}`;

  // Pi 離線後呼吸器連線狀態已不可信（斷線前的舊資料），清空避免顯示矛盾；
  // 重新上線時 Pi 會再送 status，屆時 onStatus 會補回
  if (!dev.online) {
    const vent = dev.card.querySelector(".vent-status");
    vent.className = "vent-status";
    vent.textContent = "—";
  }

  for (const metric of RMSys.METRICS) {
    const v = m[metric.key];
    const el = dev.chips[metric.key];
    el.textContent = isMissing(v) ? "—" : `${Math.round(v)}${metric.unit}`;
    el.className = `metric-val ${RMSys.level(metric, v)}`;
  }

  const thrOk = !m.throttled || m.throttled === "0x0";
  const info = dev.card.querySelector(".admin-card-info");
  info.innerHTML = "";
  const addLine = (label, val, crit) => {
    const line = document.createElement("span");
    line.className = crit ? "info-chip crit" : "info-chip";
    line.textContent = `${label} ${val}`;
    info.appendChild(line);
  };
  addLine("剩餘空間", fmtGB(m.disk_free));
  addLine("開機", RMSys.fmtUptime(m.uptime));
  addLine("降頻", isMissing(m.throttled) ? "—" : (thrOk ? "正常" : `⚠ ${m.throttled}`), !thrOk);

  dev.removeBtn.disabled = dev.online;
  dev.removeBtn.title = dev.online ? "線上裝置不可移除" : "從版面移除這台離線裝置";

  if (dev.trendOpen) {
    RMSys.drawCharts(dev.trendChans, dev.sysHist);
    renderTrendInfo(dev, m);
  }
}

// ── 趨勢圖（就地展開在卡片內；每張卡各自的 canvas，首次展開才建立）──
function toggleTrend(dev) {
  const panel = dev.card.querySelector(".admin-trend");
  if (dev.trendOpen) {
    dev.trendOpen = false;
    panel.classList.add("hidden");
    dev.trendBtn.textContent = "趨勢 ▾";
    return;
  }
  dev.trendOpen = true;
  panel.classList.remove("hidden");
  dev.trendBtn.textContent = "趨勢 ▴";
  if (!dev.trendChans) {
    dev.trendChans = RMSys.buildCharts(panel.querySelector(".sys-charts"));
  }
  RMSys.setupCharts(dev.trendChans);
  RMSys.drawCharts(dev.trendChans, dev.sysHist);
  renderTrendInfo(dev, dev.lastSys || {});

  if (!dev.histFetched) {
    dev.histFetched = true;
    RMSys.fetchHistory(dev.id).then((fetched) => {
      if (!devices.has(dev.id)) return;      // 抓回來前裝置已被移除
      dev.sysHist = RMSys.mergeHistory(fetched, dev.sysHist);
      if (dev.trendOpen) RMSys.drawCharts(dev.trendChans, dev.sysHist);
    });
  }
}

function renderTrendInfo(dev, m) {
  dev.card.querySelector(".admin-trend .sys-info-line").textContent =
    `剩餘空間 ${fmtGB(m.disk_free)}　開機時長 ${RMSys.fmtUptime(m.uptime)}` +
    `　降頻旗標 ${m.throttled || "—"}`;
}

/** 主題/視窗尺寸改變 → 所有展開中的趨勢圖重設 canvas 並全量重畫 */
function redrawOpenTrends() {
  for (const dev of devices.values()) {
    if (!dev.trendOpen) continue;
    RMSys.setupCharts(dev.trendChans);
    RMSys.drawCharts(dev.trendChans, dev.sysHist);
  }
}

// ── 管理操作（移除裝置 / 下載 CSV）──────────────────────────────
function removeDevice(id) {
  if (!confirm(`確定移除離線裝置 ${id}？\n（只是清出版面；裝置重新連上會自動回來）`)) return;
  fetch(`/api/admin/devices/${encodeURIComponent(id)}`, { method: "DELETE" })
    .then((r) => {
      // 成功時不用動畫面：伺服器會廣播 device_removed，走 onDeviceRemoved
      if (!r.ok) return r.json().then((j) => alert(j.error || "移除失敗"));
    })
    .catch(() => alert("移除失敗：無法連線伺服器"));
}

function downloadFile(url, filename) {
  fetch(url)
    .then((r) => {
      if (!r.ok) return r.json().then((j) => { alert(j.error || "下載失敗"); return null; });
      return r.blob();
    })
    .then((blob) => {
      if (!blob) return;
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = filename;
      a.click();
      URL.revokeObjectURL(a.href);
    })
    .catch(() => alert("下載失敗：無法連線伺服器"));
}

function downloadCsv(id) {
  downloadFile(`/api/admin/syslog/${encodeURIComponent(id)}`, `sys_${id}.csv`);
}

function downloadAlarmLog(id) {
  downloadFile(`/api/admin/alarmlog/${encodeURIComponent(id)}`, `alarm_${id}.csv`);
}

// ── 裝置配對申請（PROTOCOL.md「裝置配對」）───────────────────────
// 配對是偶發的人工佈建動作，不值得為它新增 WebSocket 訊息型別去動
// snapshot/廣播契約——輪詢即可，且分頁不在前景時就停掉。
const PAIR_POLL_MS = 5000;
const pairSection = document.getElementById("pairSection");
const pairList = document.getElementById("pairList");
const pairEmpty = document.getElementById("pairEmpty");
let pairTimer = null;
let pairEnabled = true;         // 伺服器回 404（配對未啟用）後就不再嘗試
const pairBusy = new Set();     // 已按下核可/拒絕、等待伺服器回應的 pair_id

function renderPending(items) {
  pairSection.classList.remove("hidden");
  pairEmpty.classList.toggle("hidden", items.length > 0);
  pairList.innerHTML = "";
  for (const p of items) {
    const row = document.createElement("div");
    row.className = "pair-row";

    const main = document.createElement("div");
    main.className = "pair-main";
    const name = document.createElement("span");
    name.className = "pair-dev";
    name.textContent = p.device_id;
    main.appendChild(name);
    if (p.renew) {
      const badge = document.createElement("span");
      badge.className = "pair-badge";
      badge.textContent = "已配對過 · 核可將換發";
      main.appendChild(badge);
    }
    const meta = document.createElement("span");
    meta.className = "pair-meta";
    meta.textContent = `來自 ${p.ip}` + (p.note ? ` · ${p.note}` : "") +
      ` · 剩餘 ${Math.max(0, Math.round(p.expires_in / 60))} 分`;
    main.appendChild(meta);
    row.appendChild(main);

    const code = document.createElement("span");
    code.className = "pair-code";
    code.textContent = p.code;
    row.appendChild(code);

    const acts = document.createElement("div");
    acts.className = "pair-acts";
    const ok = document.createElement("button");
    ok.className = "admin-btn";
    ok.type = "button";
    ok.textContent = "核可";
    ok.disabled = pairBusy.has(p.pair_id);
    ok.addEventListener("click", () => approvePair(p));
    const no = document.createElement("button");
    no.className = "admin-btn danger";
    no.type = "button";
    no.textContent = "拒絕";
    no.disabled = pairBusy.has(p.pair_id);
    no.addEventListener("click", () => denyPair(p));
    acts.append(ok, no);
    row.appendChild(acts);

    pairList.appendChild(row);
  }
}

function pairAction(p, action, failMsg) {
  pairBusy.add(p.pair_id);
  fetch(`/api/admin/pair/${encodeURIComponent(p.pair_id)}/${action}`, { method: "POST" })
    .then((r) => (r.ok ? null : r.json().then((j) => alert(j.error || failMsg))))
    .catch(() => alert(`${failMsg}：無法連線伺服器`))
    .then(() => {
      pairBusy.delete(p.pair_id);
      pollPending();               // 立刻重抓：處理過的申請會從清單消失
    });
}

function approvePair(p) {
  const warn = p.renew
    ? `\n\n⚠ ${p.device_id} 先前已配對過，核可會換發新 token，舊的立即失效。`
    : "";
  if (!confirm(`核可 ${p.device_id}？\n請先確認該 Pi 螢幕顯示的確認碼是 ${p.code}。${warn}`)) return;
  pairAction(p, "approve", "核可失敗");
}

function denyPair(p) {
  if (!confirm(`拒絕 ${p.device_id} 的配對申請？`)) return;
  pairAction(p, "deny", "拒絕失敗");
}

function pollPending() {
  fetch("/api/admin/pair/pending")
    .then((r) => {
      if (r.status === 404) {          // 伺服器未啟用配對 → 本頁不顯示這個區塊
        pairEnabled = false;
        stopPairPolling();
        return null;
      }
      return r.ok ? r.json() : null;
    })
    .then((data) => { if (data) renderPending(data.pending || []); })
    .catch(() => { /* 連線問題由 WebSocket 的連線指示呈現，這裡靜默重試 */ });
}

function startPairPolling() {
  if (pairTimer !== null || !pairEnabled) return;
  pollPending();
  pairTimer = setInterval(pollPending, PAIR_POLL_MS);
}

function stopPairPolling() {
  if (pairTimer === null) return;
  clearInterval(pairTimer);
  pairTimer = null;
}

document.addEventListener("visibilitychange", () => {
  if (document.hidden) stopPairPolling();
  else startPairPolling();
});
startPairPolling();

// ── 帳號唯讀清單 ─────────────────────────────────────────────────
const ROLE_LABEL = { admin: "管理員", viewer: "檢視（看板）" };

fetch("/api/admin/accounts")
  .then((r) => (r.ok ? r.json() : null))
  .then((data) => {
    const tb = document.querySelector("#accTable tbody");
    tb.innerHTML = "";
    const users = (data && data.users) || [];
    for (const u of users) {
      const tr = tb.insertRow();
      tr.insertCell().textContent = u.username;
      tr.insertCell().textContent = ROLE_LABEL[u.role] || u.role;
    }
    if (!users.length) {
      const tr = tb.insertRow();
      const td = tr.insertCell();
      td.colSpan = 2;
      td.textContent = "（無帳號，或登入功能未啟用）";
    }
  })
  .catch(() => {});

// ── 訊息處理（只關心 link / sys / device_removed；其餘忽略）──────
function onDeviceRemoved(id) {
  const dev = devices.get(id);
  if (!dev) return;
  dev.card.remove();
  devices.delete(id);
  applyFilter();
}

function onSys(dev, m) {
  dev.lastSys = m;
  RMSys.push(dev.sysHist, m);
  renderCard(dev);
}

function onStatus(dev, m) {
  const el = dev.card.querySelector(".vent-status");
  el.textContent = m.msg || m.state || "—";
  el.className = `vent-status ${m.state || ""}`;
}

function dispatch(m) {
  if (m.type === "snapshot") {
    for (const d of m.devices || []) {
      const dev = ensureDev(d.device);
      applyMeta(dev, d);
      dev.online = !!d.online;
      if (d.sys) onSys(dev, d.sys);
      else renderCard(dev);
      if (d.status && dev.online) onStatus(dev, d.status);
    }
    sortGrid();
    applyFilter();
    return;
  }
  if (m.type === "device_removed") { onDeviceRemoved(m.device); return; }
  if (m.type === "device_meta") {
    const dev = devices.get(m.device);
    if (dev) { applyMeta(dev, m); sortGrid(); applyFilter(); }
    return;
  }
  if (!m.device) return;
  if (m.type === "link") {
    const dev = ensureDev(m.device);
    dev.online = !!m.online;
    renderCard(dev);
  } else if (m.type === "sys") {
    onSys(ensureDev(m.device), m);
  } else if (m.type === "status") {
    onStatus(ensureDev(m.device), m);
  }
  // 其餘類型（wave/params/alarm…）：管理頁不顯示，忽略
}

// ── WebSocket（自動重連；與儀表板同一套 session 過期偵測）────────
function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onopen = () => {
    connEl.textContent = "連線";
    connEl.className = "conn online";
  };
  ws.onmessage = (ev) => {
    try { dispatch(JSON.parse(ev.data)); } catch (e) { console.error(e); }
  };
  ws.onclose = () => {
    connEl.textContent = "斷線重連中";
    connEl.className = "conn offline";
    // session 過期（閒置逾時/被登出）→ 導回登入頁，而不是無限重連失敗
    fetch("/api/me")
      .then((r) => {
        if (r.status === 401) location.href = "/login";
        else setTimeout(connect, 2000);
      })
      .catch(() => setTimeout(connect, 2000));
  };
  ws.onerror = () => ws.close();
}
connect();

// ── 視窗尺寸改變 → 展開中的趨勢圖重畫 ────────────────────────────
let resizeTimer = null;
window.addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(redrawOpenTrends, 200);
});

// ── 時鐘 ─────────────────────────────────────────────────────────
setInterval(() => {
  const d = new Date();
  const z = (x) => String(x).padStart(2, "0");
  document.getElementById("clock").textContent =
    `${d.getFullYear()}/${z(d.getMonth() + 1)}/${z(d.getDate())}  ${z(d.getHours())}:${z(d.getMinutes())}:${z(d.getSeconds())}`;
}, 1000);
