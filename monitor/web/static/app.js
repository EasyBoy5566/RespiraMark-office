/* RespiraMark Office 儀表板（前端唯一邏輯檔，分工規範見 CLAUDE.md §5）
 * - WebSocket 接收各 Pi 的波形/參數/警報，自動重連
 * - 播放引擎：取樣率由「發送端時間戳」估計（不受網路抖動影響）；
 *   緩衝偏離目標時以 ±15% 微調播放速率，永不暫停 → 波形連續
 * - 繪圖：每幀把消耗的樣本合成單一路徑繪製（增量 + 擦除條）
 * - 小卡片：三條波形 + 模式 + 設定值 + 警報列；點開顯示所有量測值
 * - 深/淺色主題：顏色一律讀 style.css 的 CSS 變數，切換時從歷史重播波形
 */
"use strict";

// ── 常數（沿用 Pi 端 WaveformConfig；繪圖顏色由 refreshThemeColors 填入）──
const CHANNELS = [
  { key: "p", label: "Paw cmH₂O", colorVar: "--c-pressure", color: "", min: -5,  max: 45,   zero: 0 },
  { key: "f", label: "Flow L/min", colorVar: "--c-flow",     color: "", min: -50, max: 50,   zero: 0 },
  { key: "v", label: "Vol mL",     colorVar: "--c-volume",   color: "", min: 0,   max: 1000, zero: 0 },
];
// Pi 系統狀態指標（趨勢圖與卡片小字列共用）。warn/crit = 變黃/變紅門檻。
// 溫度門檻依 Pi 5：~80°C 起降頻；使用率/磁碟接近滿載才示警。
const SYS_METRICS = [
  { key: "cpu",      label: "CPU",  unit: "%",  min: 0, max: 100, warn: 85, crit: 95 },
  { key: "mem",      label: "記憶體", unit: "%", min: 0, max: 100, warn: 85, crit: 95 },
  { key: "temp",     label: "溫度",  unit: "°C", min: 0, max: 90,  warn: 70, crit: 80 },
  { key: "disk_pct", label: "磁碟",  unit: "%",  min: 0, max: 100, warn: 85, crit: 95 },
];
const SYS_HIST_MAX = 1000;  // 前端每台裝置保留的 sys 樣本上限（趨勢圖用）

const WINDOW_SEC = 15;      // 波形視窗寬度（秒）
const GAP_SEC    = 0.5;     // 擦除條寬度（秒）
const TARGET_BUF = 1.2;     // 目標緩衝深度（秒）：吸收院內/訪客網路的卡頓（以延遲換連續）
const MAX_BUF    = 4.0;     // 緩衝上限（秒）：分頁背景太久直接跳到最新
const RATE_WIN   = 6.0;     // 秒，取樣率估計的滑動視窗（用發送端 ts）
let TRIG_COLOR = "";        // 依主題由 refreshThemeColors() 填入
let GRID_COLOR = "";

const grid = document.getElementById("grid");
const overlay = document.getElementById("overlay");
const emptyHint = document.getElementById("empty");
const connEl = document.getElementById("conn");

const devices = new Map();   // device_id -> Dev

// ── 主題（深色預設 / 淺色；選擇存 localStorage）──────────────────
const themeBtn = document.getElementById("themeToggle");

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function refreshThemeColors() {
  GRID_COLOR = cssVar("--grid");
  TRIG_COLOR = cssVar("--trig");
  for (const ch of CHANNELS) ch.color = cssVar(ch.colorVar);
}

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  try { localStorage.setItem("rm-theme", theme); } catch (e) { /* 私密瀏覽等，忽略 */ }
  themeBtn.textContent = theme === "light" ? "🌙 深色" : "☀ 淺色";
  refreshThemeColors();
  devices.forEach(setupCanvases);   // 用新顏色從歷史重播波形
  devices.forEach(redrawSysIfBig);  // 趨勢圖顏色也隨主題重畫
}

function redrawSysIfBig(dev) {
  if (dev.big) { setupSysCharts(dev); drawSysCharts(dev); }
}

themeBtn.addEventListener("click", () =>
  applyTheme(document.documentElement.dataset.theme === "light" ? "dark" : "light"));

let initTheme = "dark";
try { initTheme = localStorage.getItem("rm-theme") || "dark"; } catch (e) { /* 同上 */ }
applyTheme(initTheme);

// ── 裝置物件與卡片 ───────────────────────────────────────────────
function ensureDev(id) {
  let dev = devices.get(id);
  if (dev) return dev;
  dev = {
    id,
    queue: [],            // 待播樣本 {p,f,v,trig}
    rate: 100,            // 估計取樣率 Hz（由發送端 ts 計算）
    tsWin: [],            // [{t: 發送端ts, n: 樣本數}] 滑動視窗
    acc: 0,               // 消耗速率的小數累積
    pos: 0,               // 掃描位置（樣本數）
    chans: [],            // {canvas,ctx,valEl,w,h,prevY,hist[]} × 3
    valThrottle: 0,
    big: false,
    card: null,
    sysHist: [],          // Pi 系統狀態樣本 {ts,cpu,mem,temp,disk_pct,...}
    sysChans: [],         // 趨勢圖 {canvas,ctx,w,h} × SYS_METRICS
    sysFetched: false,    // 展開時是否已抓過歷史（避免重複抓）
  };
  buildCard(dev);
  devices.set(id, dev);
  emptyHint.classList.add("hidden");
  sortGrid();
  return dev;
}

function buildCard(dev) {
  const card = document.createElement("div");
  card.className = "card";
  card.dataset.device = dev.id;
  card.innerHTML = `
    <div class="card-head">
      <span class="dev-name"></span>
      <span class="patient"></span>
      <span class="spacer"></span>
      <span class="status-group" title="Pi 與呼吸器之間的序列埠連線狀態">
        <span class="status-tag">呼吸器</span>
        <span class="vent-status">—</span>
      </span>
      <span class="status-group" title="這台 Pi 與中央伺服器之間的網路連線狀態">
        <span class="status-tag">伺服器</span>
        <span class="link-status off">● 離線</span>
      </span>
      <button class="close-btn hidden">✕ 關閉</button>
    </div>
    <div class="alarm-bar hidden"></div>
    <div class="sys-strip" title="這台 Pi 的系統狀態（CPU／記憶體／溫度／磁碟）"></div>
    <div class="waves"></div>
    <div class="strip-cap">設定值</div>
    <div class="param-strip"></div>
    <div class="detail">
      <div><h3>量測值</h3><div class="kv-table measured"></div></div>
      <div>
        <h3>設定值</h3><div class="kv-table settings"></div>
        <h3>模式</h3><div class="mode-line" style="font-size:14px"></div>
        <div class="dev-info-line"></div>
      </div>
      <div class="sys-trend">
        <h3>Pi 系統狀態趨勢（近段）</h3>
        <div class="sys-info-line"></div>
        <div class="sys-charts"></div>
      </div>
    </div>`;
  card.querySelector(".dev-name").textContent = dev.id;

  const waves = card.querySelector(".waves");
  for (const ch of CHANNELS) {
    const row = document.createElement("div");
    row.className = "wave-row";
    row.innerHTML = `<canvas></canvas>
      <span class="wave-label ${ch.key}">${ch.label}</span>
      <span class="wave-val ${ch.key}">--</span>`;
    waves.appendChild(row);
    dev.chans.push({ canvas: row.querySelector("canvas"),
                     valEl: row.querySelector(".wave-val"),
                     ctx: null, w: 0, h: 0, prevY: null, hist: [] });
  }

  // 系統狀態趨勢小圖（放大檢視才會被畫；每個指標一格）
  const sysCharts = card.querySelector(".sys-charts");
  for (const metric of SYS_METRICS) {
    const cell = document.createElement("div");
    cell.className = "sys-chart";
    cell.innerHTML = `<div class="sys-chart-head">
        <span class="sys-chart-label">${metric.label}</span>
        <span class="sys-chart-val ${metric.key}">--</span>
      </div><canvas></canvas>`;
    sysCharts.appendChild(cell);
    dev.sysChans.push({ canvas: cell.querySelector("canvas"),
                        valEl: cell.querySelector(".sys-chart-val"),
                        ctx: null, w: 0, h: 0 });
  }

  card.addEventListener("click", () => { if (!dev.big) expand(dev); });
  card.querySelector(".close-btn").addEventListener("click", (e) => {
    e.stopPropagation();
    collapse(dev);
  });

  dev.card = card;
  grid.appendChild(card);
  setupCanvases(dev);
}

function sortGrid() {
  [...grid.children]
    .sort((a, b) => a.dataset.device.localeCompare(b.dataset.device))
    .forEach((el) => grid.appendChild(el));
}

// ── 放大 / 還原 ──────────────────────────────────────────────────
function expand(dev) {
  dev.big = true;
  dev.card.classList.add("big");
  dev.card.querySelector(".close-btn").classList.remove("hidden");
  overlay.appendChild(dev.card);
  overlay.classList.remove("hidden");
  setupCanvases(dev);
  fetchSysHistory(dev).then(() => { setupSysCharts(dev); drawSysCharts(dev); });
}

/** 展開時抓一次伺服器記憶體歷史補齊趨勢圖；之後由即時 sys 續接 */
function fetchSysHistory(dev) {
  return fetch(`/history/${encodeURIComponent(dev.id)}`)
    .then((r) => (r.ok ? r.json() : null))
    .then((data) => {
      const fetched = (data && data.samples) || [];
      if (fetched.length) {
        // 併入抓取期間到達的更新樣本（依 ts 去重），避免漏掉最新幾筆
        const lastTs = fetched[fetched.length - 1].ts || 0;
        const newer = dev.sysHist.filter((s) => (s.ts || 0) > lastTs);
        dev.sysHist = fetched.concat(newer);
        if (dev.sysHist.length > SYS_HIST_MAX)
          dev.sysHist.splice(0, dev.sysHist.length - SYS_HIST_MAX);
      }
      dev.sysFetched = true;
    })
    .catch(() => { dev.sysFetched = true; });   // 抓不到就用即時累積的資料
}

function collapse(dev) {
  dev.big = false;
  dev.card.classList.remove("big");
  dev.card.querySelector(".close-btn").classList.add("hidden");
  overlay.classList.add("hidden");
  grid.appendChild(dev.card);
  sortGrid();
  setupCanvases(dev);
}

// ── Canvas 初始化與重繪（尺寸改變時從歷史重播）───────────────────
function setupCanvases(dev) {
  const dpr = window.devicePixelRatio || 1;
  dev.pos = 0;
  for (let i = 0; i < CHANNELS.length; i++) {
    const c = dev.chans[i];
    const cssW = c.canvas.clientWidth || 400;
    const cssH = c.canvas.clientHeight || 84;
    c.canvas.width = Math.round(cssW * dpr);
    c.canvas.height = Math.round(cssH * dpr);
    c.ctx = c.canvas.getContext("2d");
    c.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    c.w = cssW; c.h = cssH;
    c.prevY = null;
    c.ctx.clearRect(0, 0, c.w, c.h);
    drawZeroLine(c, CHANNELS[i], 0, c.w);
  }
  // 從歷史重播，畫面不留白
  const n = dev.chans[0].hist.length;
  if (n) {
    const hist = dev.chans.map((c) => c.hist);
    const samples = new Array(n);
    for (let s = 0; s < n; s++) {
      samples[s] = { p: hist[0][s], f: hist[1][s], v: hist[2][s], trig: false };
    }
    dev.chans.forEach((c) => (c.hist = []));
    drawSamples(dev, samples);
  }
}

function yOf(ch, c, val) {
  const r = (val - ch.min) / (ch.max - ch.min);
  return c.h - Math.max(0, Math.min(1, r)) * c.h;
}

function drawZeroLine(c, ch, x0, x1) {
  const y = yOf(ch, c, ch.zero);
  c.ctx.strokeStyle = GRID_COLOR;
  c.ctx.lineWidth = 1;
  c.ctx.beginPath();
  c.ctx.moveTo(x0, y + 0.5);
  c.ctx.lineTo(x1, y + 0.5);
  c.ctx.stroke();
}

/** 把一批樣本畫上去：先算 x 座標與掃描回捲分段，再每通道以單一路徑繪製 */
function drawSamples(dev, samples) {
  const n = samples.length;
  if (!n) return;
  const w = dev.chans[0].w;
  const pps = w / (WINDOW_SEC * dev.rate);             // 每樣本的像素寬
  const gapPx = Math.max(6, GAP_SEC * dev.rate * pps); // 擦除條寬

  // 1) 算出所有樣本的 x 與「掃描回捲」分段
  const xs = new Array(n);
  const segs = [];                     // [{start, end}]（含端點）
  const wrapAtFirst = dev.pos * pps >= w;   // 第一個樣本就回捲 → 不與上一幀銜接
  let segStart = 0;
  let pos = dev.pos;
  for (let i = 0; i < n; i++) {
    let x = pos * pps;
    if (x >= w) {                      // 掃描到底 → 回左端，切新段
      pos = 0; x = 0;
      if (i > 0) segs.push({ start: segStart, end: i - 1 });
      segStart = i;
    }
    xs[i] = x;
    pos++;
  }
  segs.push({ start: segStart, end: n - 1 });
  dev.pos = pos;

  // 2) 每通道：清擦除條 → 補零線 → 單一路徑畫線 → 更新歷史
  for (let ci = 0; ci < CHANNELS.length; ci++) {
    const ch = CHANNELS[ci], c = dev.chans[ci], ctx = c.ctx;
    for (let si = 0; si < segs.length; si++) {
      const seg = segs[si];
      const x0 = xs[seg.start];
      const x1 = Math.min(xs[seg.end] + gapPx, c.w);
      ctx.clearRect(x0, 0, x1 - x0, c.h);
      drawZeroLine(c, ch, x0, x1);
      ctx.strokeStyle = ch.color;
      ctx.lineWidth = 2;
      ctx.lineJoin = "round";
      ctx.beginPath();
      let started = false;
      // 第一段且非回捲起點 → 與上一幀的最後一點銜接，線才連續
      if (si === 0 && c.prevY !== null && !wrapAtFirst) {
        ctx.moveTo(xs[seg.start] - pps, c.prevY);
        started = true;
      }
      for (let i = seg.start; i <= seg.end; i++) {
        const y = yOf(ch, c, samples[i][ch.key]);
        if (!started) { ctx.moveTo(xs[i], y); started = true; }
        else ctx.lineTo(xs[i], y);
        c.prevY = y;
      }
      ctx.stroke();
    }
    // 歷史（resize/放大時重播用），保留一個視窗的量
    // 用 floor 而非 ceil：確保保留樣本數不超過畫面寬度對應的樣本數，
    // 否則重播（resize/切換主題）後掃描位置會提前一點點回捲，波形每次都往左偏。
    for (let i = 0; i < n; i++) c.hist.push(samples[i][ch.key]);
    const cap = Math.floor(WINDOW_SEC * dev.rate);
    if (c.hist.length > cap) c.hist.splice(0, c.hist.length - cap);
  }

  // 3) Trigger 標記：底部半透明小三角（畫在壓力圖）
  const pctx = dev.chans[0].ctx, ph = dev.chans[0].h;
  for (let i = 0; i < n; i++) {
    if (!samples[i].trig) continue;
    pctx.fillStyle = TRIG_COLOR;
    pctx.beginPath();
    pctx.moveTo(xs[i], ph - 9);
    pctx.lineTo(xs[i] - 5, ph - 1);
    pctx.lineTo(xs[i] + 5, ph - 1);
    pctx.closePath();
    pctx.fill();
  }
}

// ── Pi 系統狀態：門檻分級、卡片小字列、趨勢圖 ────────────────────
/** 回傳 ""｜"warn"｜"crit"（null/無資料 → ""） */
function sysLevel(metric, value) {
  if (value === null || value === undefined || value === "") return "";
  if (value >= metric.crit) return "crit";
  if (value >= metric.warn) return "warn";
  return "";
}

/** 卡片常駐小字列：CPU / 溫度 / 記憶體 / 磁碟，異常變黃/紅；降頻另標紅旗 */
function renderSysStrip(dev, m) {
  const strip = dev.card.querySelector(".sys-strip");
  strip.innerHTML = "";
  strip.classList.remove("hidden");
  for (const metric of SYS_METRICS) {
    const v = m[metric.key];
    const chip = document.createElement("span");
    chip.className = `sys-chip ${sysLevel(metric, v)}`;
    const txt = v === null || v === undefined || v === "" ? "—" : Math.round(v);
    chip.textContent = `${metric.label} ${txt}${v === null ? "" : metric.unit}`;
    strip.appendChild(chip);
  }
  // 過熱/欠壓降頻旗標（"0x0" = 正常；非 0 才顯示，且一律紅）
  if (m.throttled && m.throttled !== "0x0") {
    const flag = document.createElement("span");
    flag.className = "sys-chip crit";
    flag.textContent = "⚠ 降頻/欠壓";
    flag.title = `vcgencmd get_throttled = ${m.throttled}`;
    strip.appendChild(flag);
  }
}

function setupSysCharts(dev) {
  const dpr = window.devicePixelRatio || 1;
  for (const c of dev.sysChans) {
    const cssW = c.canvas.clientWidth || 300;
    const cssH = c.canvas.clientHeight || 64;
    c.canvas.width = Math.round(cssW * dpr);
    c.canvas.height = Math.round(cssH * dpr);
    c.ctx = c.canvas.getContext("2d");
    c.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    c.w = cssW; c.h = cssH;
  }
}

function drawSysCharts(dev) {
  for (let i = 0; i < SYS_METRICS.length; i++) drawSysChart(dev, i);
}

/** 單一指標的趨勢折線：門檻虛線（黃/紅）+ 資料線，缺值處斷線 */
function drawSysChart(dev, mi) {
  const metric = SYS_METRICS[mi], c = dev.sysChans[mi];
  if (!c.ctx) return;
  const ctx = c.ctx, w = c.w, h = c.h;
  const hist = dev.sysHist;
  ctx.clearRect(0, 0, w, h);

  const yOf = (val) => {
    const r = (val - metric.min) / (metric.max - metric.min);
    return h - Math.max(0, Math.min(1, r)) * h;
  };
  // 門檻參考線
  ctx.setLineDash([3, 3]);
  ctx.lineWidth = 1;
  for (const [lvl, cssCol] of [[metric.warn, "--amber"], [metric.crit, "--red"]]) {
    if (lvl <= metric.min || lvl >= metric.max) continue;
    ctx.strokeStyle = cssVar(cssCol);
    ctx.globalAlpha = 0.35;
    ctx.beginPath();
    ctx.moveTo(0, yOf(lvl) + 0.5);
    ctx.lineTo(w, yOf(lvl) + 0.5);
    ctx.stroke();
  }
  ctx.globalAlpha = 1;
  ctx.setLineDash([]);

  const n = hist.length;
  if (n) {
    const dx = n > 1 ? w / (n - 1) : 0;
    ctx.strokeStyle = cssVar("--sys-line");
    ctx.lineWidth = 2;
    ctx.lineJoin = "round";
    ctx.beginPath();
    let pen = false;
    for (let i = 0; i < n; i++) {
      const v = hist[i][metric.key];
      if (v === null || v === undefined || v === "") { pen = false; continue; }
      const x = i * dx, y = yOf(v);
      if (!pen) { ctx.moveTo(x, y); pen = true; }
      else ctx.lineTo(x, y);
    }
    ctx.stroke();
  }

  // 目前數值（依門檻上色）
  let latest = null;
  for (let i = n - 1; i >= 0; i--) {
    const v = hist[i][metric.key];
    if (v !== null && v !== undefined && v !== "") { latest = v; break; }
  }
  c.valEl.textContent = latest === null ? "—" : `${Math.round(latest)}${metric.unit}`;
  c.valEl.className = `sys-chart-val ${metric.key} ${sysLevel(metric, latest)}`;
}

// ── 播放迴圈：緩衝偏離目標 → 播放速率 ±15% 微調，永不暫停 ────────
let lastFrame = performance.now();
function frame(now) {
  const dt = Math.min((now - lastFrame) / 1000, 0.1);
  lastFrame = now;

  for (const dev of devices.values()) {
    const q = dev.queue;
    if (!q.length) { dev.acc = 0; continue; }
    // 分頁在背景太久 → 丟掉舊樣本直接追上
    if (q.length > MAX_BUF * dev.rate) {
      q.splice(0, q.length - Math.ceil(TARGET_BUF * dev.rate));
    }
    const target = TARGET_BUF * dev.rate;
    const speed = Math.max(0.85, Math.min(1.15, 1 + (q.length - target) / (target * 4)));
    dev.acc += dev.rate * speed * dt;
    let n = Math.min(Math.floor(dev.acc), q.length);
    if (n > 0) {
      dev.acc -= n;
      drawSamples(dev, q.splice(0, n));
      if (!q.length) dev.acc = 0;      // 消耗到空 → 歸零避免下次爆衝
    }
    // 每 ~10 幀更新一次即時數值
    if (++dev.valThrottle >= 10) {
      dev.valThrottle = 0;
      for (let i = 0; i < CHANNELS.length; i++) {
        const h = dev.chans[i].hist;
        if (h.length) dev.chans[i].valEl.textContent = h[h.length - 1].toFixed(1);
      }
    }
  }
  requestAnimationFrame(frame);
}
requestAnimationFrame(frame);

// ── 訊息處理 ─────────────────────────────────────────────────────
function onWave(dev, m) {
  const nSamples = m.p.length;
  // 取樣率估計：用「發送端時間戳」的滑動視窗（不受網路到達抖動影響）
  const t = typeof m.ts === "number" ? m.ts : Date.now() / 1000;
  dev.tsWin.push({ t, n: nSamples });
  while (dev.tsWin.length > 3 && t - dev.tsWin[0].t > RATE_WIN) dev.tsWin.shift();
  if (dev.tsWin.length >= 3) {
    const span = t - dev.tsWin[0].t;
    if (span >= 0.5) {
      let cnt = 0;
      for (let i = 1; i < dev.tsWin.length; i++) cnt += dev.tsWin[i].n;
      const r = cnt / span;
      if (r >= 20 && r <= 300) dev.rate = r;
    }
  }
  const trigSet = new Set(m.trig || []);
  for (let i = 0; i < nSamples; i++) {
    dev.queue.push({ p: m.p[i], f: m.f[i], v: m.v[i], trig: trigSet.has(i) });
  }
}

function onLink(dev, m) {
  const el = dev.card.querySelector(".link-status");
  if (m.online) {
    dev.card.classList.remove("pi-offline");
    el.className = "link-status on";
    el.textContent = "已連線";
    if (m.patient !== undefined) setPatient(dev, m.patient);
  } else {
    dev.card.classList.add("pi-offline");
    el.className = "link-status off";
    el.textContent = "離線";
    // Pi 離線後呼吸器連線狀態已不可信（斷線前的舊資料），清空避免顯示矛盾
    const vent = dev.card.querySelector(".vent-status");
    vent.className = "vent-status";
    vent.textContent = "—";
  }
}

function setPatient(dev, patient) {
  dev.card.querySelector(".patient").textContent = patient ? `病歷號: ${patient}` : "";
}

function onStatus(dev, m) {
  const el = dev.card.querySelector(".vent-status");
  el.textContent = m.msg || m.state;
  el.className = `vent-status ${m.state || ""}`;
}

function onParams(dev, m) {
  const mode = (m.mode || "") + (m.features || []).join("");
  dev.card.querySelector(".mode-line").textContent = mode || "—";

  // 通氣模式本質上也是一種設定值（跟 PEEP、RR 一樣是醫護會查看的呼吸器設定），
  // 放在設定值清單最前面（小卡片格子與放大檢視的設定值表格都會顯示）
  const settings = mode ? { Mode: mode, ...(m.settings || {}) } : (m.settings || {});

  // 小卡片參數列 = 設定值（動態依收到的項目建立）
  const strip = dev.card.querySelector(".param-strip");
  strip.innerHTML = "";
  for (const [k, v] of Object.entries(settings)) {
    const chip = document.createElement("div");
    chip.className = "pchip";
    chip.innerHTML = `<div class="k"></div><div class="val"></div>`;
    chip.querySelector(".k").textContent = k;
    chip.querySelector(".val").textContent = v;
    strip.appendChild(chip);
  }
  // 放大檢視：所有量測值 + 設定值
  fillTable(dev.card.querySelector(".kv-table.measured"), m.measured || {});
  fillTable(dev.card.querySelector(".kv-table.settings"), settings);
}

function onAlarm(dev, m) {
  // 全量更新：alarms 為目前所有警報（空陣列 = 解除），依優先級高→低排序
  const alarms = (m.alarms || []).slice().sort((a, b) => (b.prio || 0) - (a.prio || 0));
  const bar = dev.card.querySelector(".alarm-bar");
  bar.innerHTML = "";
  if (alarms.length) {
    for (const a of alarms) {
      const item = document.createElement("span");
      item.className = "alarm-item";
      item.textContent = `⚠ ${a.text || a.code || "ALARM"}`;
      bar.appendChild(item);
    }
    bar.classList.remove("hidden");
    dev.card.classList.add("alarming");
  } else {
    bar.classList.add("hidden");
    dev.card.classList.remove("alarming");
  }
}

function fillTable(el, obj) {
  el.innerHTML = "";
  for (const [k, v] of Object.entries(obj)) {
    const row = document.createElement("div");
    row.className = "row";
    row.innerHTML = `<span class="k"></span><span class="v"></span>`;
    row.querySelector(".k").textContent = k;
    row.querySelector(".v").textContent = v;
    el.appendChild(row);
  }
  if (!el.children.length) el.innerHTML = '<div class="row"><span class="k">（無資料）</span></div>';
}

function onDeviceInfo(dev, m) {
  const i = m.info || {};
  dev.card.querySelector(".dev-info-line").textContent =
    `設備: ${i.name || "—"}  ID:${i.id || "—"}  Rev:${i.revision || "—"}  MEDIBUS:${i.medibus || "—"}`;
}

function onSys(dev, m) {
  renderSysStrip(dev, m);
  dev.sysHist.push(m);
  if (dev.sysHist.length > SYS_HIST_MAX) dev.sysHist.shift();
  // 放大檢視中才需重畫趨勢圖與明細（收合時只更新小字列即可）
  if (dev.big) {
    const fmt = (v, u) => (v === null || v === undefined || v === "" ? "—" : `${v}${u}`);
    dev.card.querySelector(".sys-info-line").textContent =
      `剩餘空間 ${fmt(m.disk_free, " GB")}　開機時長 ${fmtUptime(m.uptime)}` +
      `　降頻旗標 ${m.throttled || "—"}`;
    drawSysCharts(dev);
  }
}

function fmtUptime(sec) {
  if (sec === null || sec === undefined || sec === "") return "—";
  const d = Math.floor(sec / 86400), h = Math.floor((sec % 86400) / 3600),
        mi = Math.floor((sec % 3600) / 60);
  return d > 0 ? `${d}天${h}時` : h > 0 ? `${h}時${mi}分` : `${mi}分`;
}

// 訊息類型 → 處理函式（新增有狀態類型：加一行 + 寫 onXxx，snapshot 自動生效）
const MSG_HANDLERS = {
  wave: onWave, link: onLink, status: onStatus,
  params: onParams, device_info: onDeviceInfo, alarm: onAlarm, sys: onSys,
};
const SNAPSHOT_KEYS = ["status", "params", "device_info", "alarm", "sys"];

function dispatch(m) {
  if (m.type === "snapshot") {
    for (const d of m.devices || []) {
      const dev = ensureDev(d.device);
      onLink(dev, { online: d.online, patient: d.patient });
      for (const k of SNAPSHOT_KEYS) {
        if (d[k]) MSG_HANDLERS[k](dev, d[k]);
      }
    }
    return;
  }
  if (!m.device) return;
  const handler = MSG_HANDLERS[m.type];
  if (handler) handler(ensureDev(m.device), m);
  // 未知類型：忽略（向前相容）
}

// ── WebSocket（自動重連）─────────────────────────────────────────
function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onopen = () => {
    connEl.textContent = "伺服器已啟動";
    connEl.className = "conn online";
  };
  ws.onmessage = (ev) => {
    try { dispatch(JSON.parse(ev.data)); } catch (e) { console.error(e); }
  };
  ws.onclose = () => {
    connEl.textContent = "● 伺服器斷線，重連中…";
    connEl.className = "conn offline";
    setTimeout(connect, 2000);
  };
  ws.onerror = () => ws.close();
}
connect();

// ── 視窗尺寸改變 → 重設所有 canvas ──────────────────────────────
let resizeTimer = null;
window.addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => {
    devices.forEach(setupCanvases);
    devices.forEach(redrawSysIfBig);
  }, 200);
});

// ── 時鐘 ─────────────────────────────────────────────────────────
setInterval(() => {
  const d = new Date();
  const z = (x) => String(x).padStart(2, "0");
  document.getElementById("clock").textContent =
    `${d.getFullYear()}/${z(d.getMonth() + 1)}/${z(d.getDate())}  ${z(d.getHours())}:${z(d.getMinutes())}:${z(d.getSeconds())}`;
}, 1000);
