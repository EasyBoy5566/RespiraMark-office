/* 管理頁（/admin，僅 admin 角色；前端分工見 CLAUDE.md §5）
 * - 設備健康總表：卡片格線（視覺語言比照主儀表板 .card），每張卡是連線 /
 *   CPU / 記憶體 / 溫度 / 磁碟 / 降頻 / 開機時長
 * - 點「趨勢」在卡片內就地展開系統狀態趨勢圖（RMSys 共用繪圖，與儀表板同一套門檻）
 * - 移除離線裝置（DELETE /api/admin/devices）、下載長期 sys CSV、帳號唯讀清單
 * - 資料來源與儀表板相同：WebSocket /ws（本頁忽略波形等臨床訊息，只用 sys/link）
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
          card: null, chips: {}, trendOpen: false, trendChans: null,
          histFetched: false, removeBtn: null, trendBtn: null };
  buildCard(dev);
  devices.set(id, dev);
  devEmpty.classList.add("hidden");
  sortGrid();
  return dev;
}

function buildCard(dev) {
  const card = document.createElement("div");
  card.className = "admin-card";
  card.dataset.device = dev.id;
  card.innerHTML = `
    <div class="admin-card-head">
      <span class="dev-name"></span>
      <span class="spacer"></span>
      <span class="link-status off">● 離線</span>
    </div>
    <div class="metric-grid"></div>
    <div class="admin-card-info"></div>
    <div class="admin-card-foot">
      <button type="button" class="admin-btn trend-btn">趨勢 ▾</button>
      <button type="button" class="admin-btn">下載 CSV</button>
      <button type="button" class="admin-btn danger">移除</button>
    </div>
    <div class="admin-trend hidden">
      <div class="sys-info-line"></div>
      <div class="sys-charts"></div>
    </div>`;
  card.querySelector(".dev-name").textContent = dev.id;

  const grid = card.querySelector(".metric-grid");
  for (const metric of RMSys.METRICS) {
    const chip = document.createElement("div");
    chip.className = "metric-chip";
    chip.innerHTML = `<div class="metric-label">${metric.label}</div>
                       <div class="metric-val">—</div>`;
    grid.appendChild(chip);
    dev.chips[metric.key] = chip.querySelector(".metric-val");
  }

  const [trendBtn, csvBtn, removeBtn] = card.querySelectorAll(".admin-card-foot button");
  trendBtn.addEventListener("click", () => toggleTrend(dev));
  csvBtn.addEventListener("click", () => downloadCsv(dev.id));
  csvBtn.title = "下載這台機器的長期系統狀態紀錄（伺服器端 CSV）";
  removeBtn.addEventListener("click", () => removeDevice(dev.id));
  dev.trendBtn = trendBtn;
  dev.removeBtn = removeBtn;

  dev.card = card;
  devGrid.appendChild(card);
  renderCard(dev);
}

function sortGrid() {
  [...devGrid.children]
    .sort((a, b) => a.dataset.device.localeCompare(b.dataset.device))
    .forEach((el) => devGrid.appendChild(el));
}

function renderCard(dev) {
  const m = dev.lastSys || {};
  const link = dev.card.querySelector(".link-status");
  link.textContent = dev.online ? "● 連線" : "● 離線";
  link.className = `link-status ${dev.online ? "on" : "off"}`;

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

function downloadCsv(id) {
  fetch(`/api/admin/syslog/${encodeURIComponent(id)}`)
    .then((r) => {
      if (!r.ok) return r.json().then((j) => { alert(j.error || "下載失敗"); return null; });
      return r.blob();
    })
    .then((blob) => {
      if (!blob) return;
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = `sys_${id}.csv`;
      a.click();
      URL.revokeObjectURL(a.href);
    })
    .catch(() => alert("下載失敗：無法連線伺服器"));
}

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
  if (!devices.size) devEmpty.classList.remove("hidden");
}

function onSys(dev, m) {
  dev.lastSys = m;
  RMSys.push(dev.sysHist, m);
  renderCard(dev);
}

function dispatch(m) {
  if (m.type === "snapshot") {
    for (const d of m.devices || []) {
      const dev = ensureDev(d.device);
      dev.online = !!d.online;
      if (d.sys) onSys(dev, d.sys);
      else renderCard(dev);
    }
    return;
  }
  if (m.type === "device_removed") { onDeviceRemoved(m.device); return; }
  if (!m.device) return;
  if (m.type === "link") {
    const dev = ensureDev(m.device);
    dev.online = !!m.online;
    renderCard(dev);
  } else if (m.type === "sys") {
    onSys(ensureDev(m.device), m);
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
