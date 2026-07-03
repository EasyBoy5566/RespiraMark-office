/* RespiraMark Office 儀表板
 * - WebSocket 接收各 Pi 的波形/參數，自動重連
 * - 自適應抖動緩衝：批次到達 → 以估計取樣率平滑消耗 → 60fps 掃描式繪圖
 * - 點擊卡片放大檢視（完整量測/設定表）
 */
"use strict";

// ── 常數（沿用 Pi 端 WaveformConfig）─────────────────────────────
const CHANNELS = [
  { key: "p", label: "Paw cmH₂O", color: "#4FC3F7", min: -5,  max: 45,   zero: 0 },
  { key: "f", label: "Flow L/min", color: "#00E5FF", min: -50, max: 50,   zero: 0 },
  { key: "v", label: "Vol mL",     color: "#76FF03", min: 0,   max: 1000, zero: 0 },
];
const WINDOW_SEC = 15;      // 波形視窗寬度（秒）
const GAP_SEC    = 0.5;     // 擦除條寬度（秒）
const TARGET_BUF = 0.45;    // 目標緩衝深度（秒）：吸收 Wi-Fi 抖動
const MAX_BUF    = 2.0;     // 緩衝上限（秒）：分頁背景太久直接跳到最新
const TRIG_COLOR = "rgba(255, 234, 0, 0.55)";
const GRID_COLOR = "#24364F";

// 卡片參數列顯示的重點項目：[measured 鍵, 單位]
const STRIP_KEYS = [
  ["PIP", "mbar"], ["PEEP", "mbar"], ["VT", "mL"], ["RR", "/min"],
  ["MVe", "L/min"], ["FiO2", "%"], ["etCO2", "mmHg"],
];

const grid = document.getElementById("grid");
const overlay = document.getElementById("overlay");
const emptyHint = document.getElementById("empty");
const connEl = document.getElementById("conn");

const devices = new Map();   // device_id -> Dev

// ── 裝置物件 ─────────────────────────────────────────────────────
function ensureDev(id) {
  let dev = devices.get(id);
  if (dev) return dev;
  dev = {
    id,
    queue: [],            // 待播樣本 {p,f,v,trig}
    rate: 75,             // 估計取樣率 Hz（EMA）
    lastArrival: 0,
    acc: 0,               // 消耗速率的小數累積
    pos: 0,               // 掃描位置（樣本數）
    chans: [],            // {ctx,w,h,prevY,hist:[]} × 3
    valThrottle: 0,
    big: false,
    card: null,
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
      <span class="mode-badge hidden"></span>
      <span class="spacer"></span>
      <span class="vent-status">—</span>
      <span class="link-status off">● Pi 離線</span>
      <button class="close-btn hidden">✕ 關閉</button>
    </div>
    <div class="waves"></div>
    <div class="param-strip"></div>
    <div class="detail">
      <div><h3>量測值</h3><div class="kv-table measured"></div></div>
      <div>
        <h3>設定值</h3><div class="kv-table settings"></div>
        <h3>模式</h3><div class="mode-line" style="font-size:14px"></div>
        <div class="dev-info-line"></div>
      </div>
    </div>`;
  card.querySelector(".dev-name").textContent = dev.id;

  const waves = card.querySelector(".waves");
  for (const ch of CHANNELS) {
    const row = document.createElement("div");
    row.className = "wave-row";
    row.innerHTML = `<canvas></canvas>
      <span class="wave-label ${ch.key}">${ch.label}</span>
      <span class="wave-val" style="color:${ch.color}">--</span>`;
    waves.appendChild(row);
    dev.chans.push({ canvas: row.querySelector("canvas"),
                     valEl: row.querySelector(".wave-val"),
                     ctx: null, w: 0, h: 0, prevY: null, hist: [] });
  }

  const strip = card.querySelector(".param-strip");
  for (const [k, u] of STRIP_KEYS) {
    const chip = document.createElement("div");
    chip.className = "pchip";
    chip.innerHTML = `<div class="k">${k}</div><div class="val" data-k="${k}">--</div><div class="u">${u}</div>`;
    strip.appendChild(chip);
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
  const hist = dev.chans[0].hist;
  if (hist.length) {
    const start = dev.chans.map((c) => c.hist.slice());
    dev.chans.forEach((c) => (c.hist = []));
    for (let s = 0; s < hist.length; s++) {
      drawSample(dev, {
        p: start[0][s], f: start[1][s], v: start[2][s], trig: false,
      }, true);
    }
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

function drawSample(dev, s, replay) {
  const pxPerSample = dev.chans[0].w / (WINDOW_SEC * dev.rate);
  let x = dev.pos * pxPerSample;
  if (x >= dev.chans[0].w) {           // 掃描到底 → 回到左端
    dev.pos = 0; x = 0;
    dev.chans.forEach((c) => (c.prevY = null));
  }
  const gapPx = Math.max(4, GAP_SEC * dev.rate * pxPerSample);

  for (let i = 0; i < CHANNELS.length; i++) {
    const ch = CHANNELS[i], c = dev.chans[i];
    const val = s[ch.key];
    // 擦除條 + 補回零線
    c.ctx.clearRect(x + 1, 0, gapPx, c.h);
    drawZeroLine(c, ch, x + 1, Math.min(x + 1 + gapPx, c.w));
    // 波形線段
    const y = yOf(ch, c, val);
    if (c.prevY !== null) {
      c.ctx.strokeStyle = ch.color;
      c.ctx.lineWidth = 2;
      c.ctx.beginPath();
      c.ctx.moveTo(x - pxPerSample, c.prevY);
      c.ctx.lineTo(x, y);
      c.ctx.stroke();
    }
    c.prevY = y;
    // 歷史（重繪用），保留一個視窗的量
    c.hist.push(val);
    const cap = Math.ceil(WINDOW_SEC * dev.rate);
    if (c.hist.length > cap) c.hist.splice(0, c.hist.length - cap);
  }
  // Trigger 標記：底部半透明小三角（畫在壓力圖）
  if (s.trig && !replay) {
    const c = dev.chans[0].ctx, h = dev.chans[0].h;
    c.fillStyle = TRIG_COLOR;
    c.beginPath();
    c.moveTo(x, h - 9);
    c.lineTo(x - 5, h - 1);
    c.lineTo(x + 5, h - 1);
    c.closePath();
    c.fill();
  }
  dev.pos++;
}

// ── 播放迴圈：自適應消耗緩衝 ─────────────────────────────────────
let lastFrame = performance.now();
function frame(now) {
  const dt = Math.min((now - lastFrame) / 1000, 0.1);
  lastFrame = now;

  for (const dev of devices.values()) {
    const q = dev.queue;
    if (!q.length) continue;
    // 分頁在背景太久 → 丟掉舊樣本直接追上
    const maxLen = MAX_BUF * dev.rate;
    if (q.length > maxLen) q.splice(0, q.length - Math.ceil(TARGET_BUF * dev.rate));
    // 基礎消耗 = 取樣率；再依緩衝深度微調（比例控制）
    const target = TARGET_BUF * dev.rate;
    dev.acc += dev.rate * dt + (q.length - target) * 0.06;
    let n = Math.floor(dev.acc);
    if (n <= 0) continue;
    dev.acc -= n;
    n = Math.min(n, q.length);
    for (let i = 0; i < n; i++) drawSample(dev, q.shift(), false);
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
  const now = performance.now() / 1000;
  const n = m.p.length;
  if (dev.lastArrival) {
    const gap = now - dev.lastArrival;
    if (gap > 0.02 && gap < 2.0) {
      const inst = n / gap;
      if (inst > 10 && inst < 500) dev.rate = dev.rate * 0.9 + inst * 0.1;
    }
  }
  dev.lastArrival = now;
  const trigSet = new Set(m.trig || []);
  for (let i = 0; i < n; i++) {
    dev.queue.push({ p: m.p[i], f: m.f[i], v: m.v[i], trig: trigSet.has(i) });
  }
}

function onLink(dev, m) {
  const el = dev.card.querySelector(".link-status");
  if (m.online) {
    dev.card.classList.remove("pi-offline");
    el.className = "link-status on";
    el.textContent = "● Pi 連線";
    if (m.patient !== undefined) setPatient(dev, m.patient);
  } else {
    dev.card.classList.add("pi-offline");
    el.className = "link-status off";
    el.textContent = "● Pi 離線";
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
  // 模式徽章
  const badge = dev.card.querySelector(".mode-badge");
  const mode = (m.mode || "") + (m.features || []).join("");
  if (mode) { badge.textContent = mode; badge.classList.remove("hidden"); }
  dev.card.querySelector(".mode-line").textContent = mode || "—";
  // 參數列
  const measured = m.measured || {};
  for (const [k] of STRIP_KEYS) {
    const el = dev.card.querySelector(`.param-strip .val[data-k="${k}"]`);
    el.textContent = measured[k] !== undefined ? measured[k] : "--";
  }
  // 放大檢視的完整表
  fillTable(dev.card.querySelector(".kv-table.measured"), measured);
  fillTable(dev.card.querySelector(".kv-table.settings"), m.settings || {});
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

function dispatch(m) {
  if (m.type === "snapshot") {
    for (const d of m.devices || []) {
      const dev = ensureDev(d.device);
      onLink(dev, { online: d.online, patient: d.patient });
      if (d.status) onStatus(dev, d.status);
      if (d.params) onParams(dev, d.params);
      if (d.device_info) onDeviceInfo(dev, d.device_info);
    }
    return;
  }
  if (!m.device) return;
  const dev = ensureDev(m.device);
  switch (m.type) {
    case "wave":        onWave(dev, m); break;
    case "link":        onLink(dev, m); break;
    case "status":      onStatus(dev, m); break;
    case "params":      onParams(dev, m); break;
    case "device_info": onDeviceInfo(dev, m); break;
    default: break;     // 未知類型：忽略（向前相容）
  }
}

// ── WebSocket（自動重連）─────────────────────────────────────────
function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onopen = () => {
    connEl.textContent = "● 已連線";
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
  resizeTimer = setTimeout(() => devices.forEach(setupCanvases), 200);
});

// ── 時鐘 ─────────────────────────────────────────────────────────
setInterval(() => {
  const d = new Date();
  const z = (x) => String(x).padStart(2, "0");
  document.getElementById("clock").textContent =
    `${d.getFullYear()}/${z(d.getMonth() + 1)}/${z(d.getDate())}  ${z(d.getHours())}:${z(d.getMinutes())}:${z(d.getSeconds())}`;
}, 1000);
