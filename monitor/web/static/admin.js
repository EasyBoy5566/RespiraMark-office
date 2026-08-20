/* 管理頁（/admin，僅 admin 角色；前端分工見 CLAUDE.md §5）
 *
 * 版面是「一台裝置一列」的對齊表格，不是各自為政的卡片——本頁的核心動作是
 * 掃一排機器找出哪台不對勁，欄位跨列對齊在同一條垂直線上才掃得動。
 * 每台裝置是一個 <tbody>，內含主列與展開列：
 * - 主列：連線燈 / 機台編號 / 床號 / 財編 / 呼吸器狀態 / 連線時長 /
 *         CPU / 記憶體 / 溫度 / 磁碟 / 「詳細」展開鈕
 * - 展開列：全部操作按鈕（財編、CSV、警報紀錄、移除）＋ 版本、剩餘空間、
 *         開機時長、降頻旗標 ＋ 系統狀態趨勢圖（RMSys 共用繪圖，首次展開才建立）
 * - 離線整列轉灰（.dev-row.offline），比單獨一顆燈更難忽略
 *
 * 資料來源與儀表板相同：WebSocket /ws（本頁忽略波形等臨床訊息，只用
 * snapshot / link / sys / status / device_meta / device_removed）。
 */
"use strict";

const connEl = document.getElementById("conn");
const devTable = document.getElementById("devTable");     // 各裝置的 tbody 掛在這裡
const devTableWrap = document.getElementById("devTableWrap");
const devEmpty = document.getElementById("devEmpty");

// 展開列要橫跨主列的全部欄位；改欄位數時這裡跟著改（值 = thead 的 th 數）
const DETAIL_COLSPAN = 12;

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

/** 本次連線時長。伺服器只給 connected_at 這個原始時刻，格式化是前端的事
 *  （CLAUDE.md §5）；以瀏覽器本機時間相減，因此依賴兩端時鐘同步（NTP）。 */
function linkDuration(dev) {
  if (!dev.online || !dev.connectedAt) return "—";
  return RMSys.fmtUptime(Math.max(0, Date.now() / 1000 - dev.connectedAt));
}

/** snapshot 與 link 共用：只在欄位存在時覆寫，離線的 link 不帶這兩欄，
 *  沿用舊值即可（離線時 linkDuration 本來就顯示 —）。 */
function applyLinkMeta(dev, m) {
  if (m.connected_at !== undefined) dev.connectedAt = m.connected_at || 0;
  if (m.app_version !== undefined) dev.appVersion = m.app_version || "";
}

// ── 設備卡片 ─────────────────────────────────────────────────────
function ensureDev(id) {
  let dev = devices.get(id);
  if (dev) return dev;
  dev = { id, online: false, sysHist: [], lastSys: null,
          bed: "", ward: "", asset: "",   // 裝置清冊（devices.json）；ward 由伺服器推導
          connectedAt: 0, appVersion: "",   // 本次連線起點與 Pi 軟體版本（見 PROTOCOL.md）
          card: null, chips: {}, trendOpen: false, trendChans: null,
          histFetched: false, removeBtn: null, trendBtn: null };
  buildCard(dev);
  devices.set(id, dev);
  sortGrid();
  applyFilter();               // 新卡片也要遵守目前的搜尋條件
  return dev;
}

// 一台裝置 = 一個 tbody（主列 + 展開列）。用真正的表格而不是各自為政的卡片，
// 是因為這頁的核心動作是「掃一排機器找出哪台不對勁」——CPU／溫度這些數字必須
// 跨列對齊在同一條垂直線上才掃得動。主列只留掃描要看的欄位，次要資訊與全部
// 操作都收進展開列，避免每一列都塞滿按鈕。
function buildCard(dev) {
  const body = document.createElement("tbody");
  body.className = "dev-row";
  body.dataset.device = dev.id;
  body.innerHTML = `
    <tr class="dev-main">
      <td class="col-dot"><span class="link-dot"></span></td>
      <td><span class="dev-name"></span></td>
      <td><span class="dev-ward">--</span></td>
      <td><span class="dev-bed">--</span></td>
      <td><span class="dev-asset">--</span></td>
      <td><span class="vent-status">—</span></td>
      <td class="col-num link-dur">—</td>
      ${RMSys.METRICS.map((m) =>
        `<td class="col-num metric-val" data-k="${m.key}">—</td>`).join("")}
      <td class="col-act">
        <button type="button" class="admin-btn detail-btn" aria-expanded="false">詳細 ▾</button>
      </td>
    </tr>
    <tr class="dev-detail hidden">
      <td colspan="${DETAIL_COLSPAN}">
        <div class="detail-actions">
          <button type="button" class="admin-btn">財編</button>
          <button type="button" class="admin-btn">下載 CSV</button>
          <button type="button" class="admin-btn">警報紀錄</button>
          <button type="button" class="admin-btn danger">移除</button>
        </div>
        <div class="sys-info-line"></div>
        <div class="sys-charts"></div>
      </td>
    </tr>`;
  renderDevIdentity(dev, body);

  for (const metric of RMSys.METRICS) {
    dev.chips[metric.key] = body.querySelector(`.metric-val[data-k="${metric.key}"]`);
  }

  const detailBtn = body.querySelector(".detail-btn");
  detailBtn.addEventListener("click", () => toggleTrend(dev));
  detailBtn.title = "展開版本、剩餘空間、開機時長、降頻旗標與趨勢圖";
  dev.trendBtn = detailBtn;

  const [metaBtn, csvBtn, alarmBtn, removeBtn] =
    body.querySelectorAll(".detail-actions button");
  metaBtn.addEventListener("click", () => editAsset(dev));
  metaBtn.title = "設定這台機器對應的呼吸器財編（床號之後由財編自動帶入）";
  csvBtn.addEventListener("click", () => downloadCsv(dev.id));
  csvBtn.title = "從 SQLite 匯出這台機器最近 7 天的系統狀態 CSV";
  alarmBtn.addEventListener("click", () => downloadAlarmLog(dev.id));
  alarmBtn.title = "下載這台機器最近 7 天的警報歷史（由 SQLite 匯出 CSV）";
  removeBtn.addEventListener("click", () => removeDevice(dev.id));
  dev.removeBtn = removeBtn;

  dev.card = body;
  devTable.appendChild(body);
  renderCard(dev);
}

// numeric 讓 RCC-2 排在 RCC-10 前面，而不是一般字串順序的 1、10、2。
const collate = (a, b) =>
  a.localeCompare(b, undefined, { numeric: true, sensitivity: "base" });

// 依機台編號排序：本頁第一個欄位就是機台編號，排序跟著它走最好找。
// （看板是護理站用的，那邊依床號排序，見 app.js sortGrid。）
// 只挑 tbody 重排——devTable 是 <table>，它的子節點還包含 thead，
// 一起排會把標題列排到中間去。
function sortGrid() {
  [...devTable.querySelectorAll("tbody")]
    .sort((a, b) => collate(a.dataset.device, b.dataset.device))
    .forEach((el) => devTable.appendChild(el));
}

// ── 裝置清冊：床號與呼吸器財編（PROTOCOL.md「裝置床號與財編」）────────
// 財編對應實體呼吸器，是這裡唯一要人工填的欄位；床號不開放手動設定——
// 它之後要由財編向院內系統查詢後自動帶入，開放人工填只會多一份會過期的來源。
// 管理頁是管「機器」的，標題一律用機台編號（看板才以床號為標題）；
// 床號與財編各自是一個看得到的欄位，沒有值就顯示 --，不用去猜是誰沒設定。
// 單位由伺服器從床號推導後隨 snapshot／device_meta 送來（PROTOCOL.md「單位」），
// 前端不自己算——那是業務規則，看板與本頁各寫一份遲早會不一致。
function renderDevIdentity(dev, card) {
  const el = card || dev.card;
  el.querySelector(".dev-name").textContent = dev.id;
  for (const [sel, value] of [[".dev-ward", dev.ward], [".dev-bed", dev.bed],
                              [".dev-asset", dev.asset]]) {
    const field = el.querySelector(sel);
    field.textContent = value || "--";
    field.classList.toggle("unassigned", !value);
  }
  el.dataset.bed = dev.bed;
}

function applyMeta(dev, m) {
  dev.bed = m.bed || "";
  dev.ward = m.ward || "";
  dev.asset = m.asset || "";
  renderDevIdentity(dev);
}

function editAsset(dev) {
  const asset = prompt(`${dev.id} 對應的呼吸器財編（留空 = 未設定）`, dev.asset);
  if (asset === null) return;
  fetch(`/api/admin/devices/${encodeURIComponent(dev.id)}/meta`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ asset: asset.trim() }),   // 不送 bed → 床號保持原值
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
    const hit = !q || [dev.ward, dev.bed, dev.id, dev.asset]
      .some((v) => (v || "").toLowerCase().includes(q));
    dev.card.classList.toggle("hidden", !hit);
    if (hit) shown++;
  }
  devEmpty.classList.toggle("hidden", shown > 0);
  // 一列都沒有時連表格骨架一起收起來，不留一條孤零零的標題列
  devTableWrap.classList.toggle("hidden", shown === 0);
  devEmpty.textContent = devices.size && !shown
    ? "沒有符合的裝置" : "等待裝置連線";
}

deviceFilter.addEventListener("input", applyFilter);

function renderCard(dev) {
  const m = dev.lastSys || {};
  // 離線整列轉灰：這頁最該一眼看到的就是「哪台不在線上」
  dev.card.classList.toggle("offline", !dev.online);
  dev.card.querySelector(".link-dot").title =
    dev.online ? "已連上中央伺服器" : "與中央伺服器離線";

  const dur = dev.card.querySelector(".link-dur");
  dur.textContent = linkDuration(dev);
  dev.linkChip = dur;              // 供每 30 秒走動的計時器更新

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
    // col-num 要留著（欄位靠右對齊），只換警示等級
    el.className = `col-num metric-val ${RMSys.level(metric, v)}`;
  }

  dev.removeBtn.disabled = dev.online;
  dev.removeBtn.title = dev.online ? "線上裝置不可移除" : "從版面移除這台離線裝置";

  if (dev.trendOpen) {
    RMSys.drawCharts(dev.trendChans, dev.sysHist);
    renderTrendInfo(dev, m);
  }
}

// ── 展開列（次要資訊 + 操作 + 趨勢圖；每列各自的 canvas，首次展開才建立）──
function toggleTrend(dev) {
  const panel = dev.card.querySelector(".dev-detail");
  if (dev.trendOpen) {
    dev.trendOpen = false;
    panel.classList.add("hidden");
    dev.trendBtn.textContent = "詳細 ▾";
    dev.trendBtn.setAttribute("aria-expanded", "false");
    return;
  }
  dev.trendOpen = true;
  panel.classList.remove("hidden");
  dev.trendBtn.textContent = "詳細 ▴";
  dev.trendBtn.setAttribute("aria-expanded", "true");
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

// 主列只放掃描要看的欄位，其餘次要資訊集中在這裡（展開才看得到）
function renderTrendInfo(dev, m) {
  dev.card.querySelector(".dev-detail .sys-info-line").textContent =
    `版本 ${dev.appVersion || "—"}　剩餘空間 ${fmtGB(m.disk_free)}` +
    `　開機時長 ${RMSys.fmtUptime(m.uptime)}　降頻旗標 ${m.throttled || "—"}`;
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
      applyLinkMeta(dev, d);
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
    applyLinkMeta(dev, m);
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

// ── 連線時長走動 ─────────────────────────────────────────────────
// 這是相對時間，沒有新訊息也要自己往前走。fmtUptime 的解析度是分鐘，
// 30 秒更新一次已足夠；只改那一顆 chip 的文字，不重跑 renderCard
// （後者會連展開中的趨勢圖一起重畫）。
setInterval(() => {
  for (const dev of devices.values()) {
    if (dev.linkChip) dev.linkChip.textContent = linkDuration(dev);
  }
}, 30000);

// ── 時鐘 ─────────────────────────────────────────────────────────
setInterval(() => {
  const d = new Date();
  const z = (x) => String(x).padStart(2, "0");
  document.getElementById("clock").textContent =
    `${d.getFullYear()}/${z(d.getMonth() + 1)}/${z(d.getDate())}  ${z(d.getHours())}:${z(d.getMinutes())}:${z(d.getSeconds())}`;
}, 1000);
