/* RespiraMark Office 儀表板（護理站看板；前端分工見 CLAUDE.md §5）
 * - WebSocket 接收各 Pi 的波形/參數/警報，自動重連
 * - 播放引擎：取樣率由「發送端時間戳」估計（不受網路抖動影響）；
 *   緩衝偏離目標時以 ±15% 微調播放速率，永不暫停 → 波形連續
 * - 繪圖：每幀把消耗的樣本合成單一路徑繪製（增量 + 擦除條）；
 *   放大/縮回/換主題時從歷史重播並接續原掃描位置（不從左端重畫）
 * - 小卡片：三條波形（統一單色）+ 設定值；警報以半透明橫幅疊在最上方(Paw)波形上（最嚴重一則＋其餘計數）；點開顯示完整警報清單與所有量測值
 * - 連線狀態（呼吸器/伺服器）移到管理頁 /admin 顯示；本頁 Pi 離線以紅框呈現
 * - 警報音：呼吸器發出 level 1/2/3 警報時播放 Web Audio 合成音；右上角靜音鈕按一次靜音 2 分鐘
 * - 主題：深色 / 淺色 / 監視器(mono，近純黑 CMS 風格)三選一；顏色一律讀 style.css 的 CSS 變數，切換時從歷史重播波形
 * - 登入顯示在 auth.js；Pi 機器健康狀態只在管理頁（/admin）顯示，本頁忽略 sys
 */
"use strict";

// ── 常數（沿用 Pi 端 WaveformConfig；繪圖顏色由 refreshThemeColors 填入）──
// name = 波形名稱（小卡總覽只顯示這個）；unit = 單位（只在放大檢視顯示，見 style.css .wl-unit）
const CHANNELS = [
  { key: "p", name: "Paw",  unit: "cmH₂O", colorVar: "--c-wave", color: "", min: -5,  max: 45,   zero: 0 },
  { key: "f", name: "Flow", unit: "L/min", colorVar: "--c-wave", color: "", min: -50, max: 50,   zero: 0 },
  { key: "v", name: "Vol",  unit: "mL",    colorVar: "--c-wave", color: "", min: 0,   max: 1000, zero: 0 },
];

const WINDOW_SEC = 20;      // 波形視窗寬度（秒）
const GAP_SEC    = 0.5;     // 擦除條寬度（秒）
const TARGET_BUF = 1.2;     // 目標緩衝深度（秒）：吸收院內/訪客網路的卡頓（以延遲換連續）
const MAX_BUF    = 4.0;     // 緩衝上限（秒）：分頁背景太久直接跳到最新
const RATE_WIN   = 6.0;     // 秒，取樣率估計的滑動視窗（用發送端 ts）
const MUTE_SEC   = 120;     // 靜音鈕：按一次靜音的秒數（再按一次取消）
let TRIG_COLOR = "";        // 依主題由 refreshThemeColors() 填入
const LOOP_BREATHS = 3;
let GRID_COLOR = "";

const LOOP_TYPES = {
  pv: { label: "P-V LOOP", xKey: "p", xLabel: "Paw (cmH₂O)", xMin: -5, xMax: 45,
        yKey: "v", yLabel: "Volume (mL)", yMin: 0, yMax: 1000 },
  fv: { label: "F-V LOOP", xKey: "v", xLabel: "Volume (mL)", xMin: 0, xMax: 1000,
        yKey: "f", yLabel: "Flow (L/min)", yMin: -50, yMax: 50 },
  pf: { label: "P-F LOOP", xKey: "p", xLabel: "Paw (cmH₂O)", xMin: -5, xMax: 45,
        yKey: "f", yLabel: "Flow (L/min)", yMin: -50, yMax: 50 },
};
let selectedLoopType = "pv";
try {
  const savedLoopType = localStorage.getItem("rm-loop-type");
  if (savedLoopType && LOOP_TYPES[savedLoopType]) selectedLoopType = savedLoopType;
} catch (e) { /* Private browsing: keep default. */ }

const grid = document.getElementById("grid");
const overlay = document.getElementById("overlay");
const emptyHint = document.getElementById("empty");
const connEl = document.getElementById("conn");
const autoHideHeader = document.querySelector("header.auto-hide");

const devices = new Map();   // device_id -> Dev

// ── 主題（深色預設 / 淺色 / 監視器；選擇存 localStorage）──────────────
// 「監視器模式」(mono) 是獨立主題：近純黑底、磚格貼齊只用 1px 線分隔（樣式見 style.css
// [data-theme="mono"]）。切回深/淺色即完全復原。明暗鈕在 dark↔light 間切換，並會退出 mono。
const themeBtn = document.getElementById("themeToggle");
const monoBtn = document.getElementById("monoToggle");

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
  monoBtn.classList.toggle("active", theme === "mono");   // 監視器模式啟用中 → 選單項目highlight
  refreshThemeColors();
  devices.forEach(setupCanvases);   // 用新顏色從歷史重播波形
  devices.forEach(drawLoop);
  relayoutParamStrips();            // 主題會改變卡片邊框／間距 → 重新判斷設定列能否完整顯示
}

// 明暗鈕：dark↔light 間切換（在 mono 時點它會退出到 light）
themeBtn.addEventListener("click", () =>
  applyTheme(document.documentElement.dataset.theme === "light" ? "dark" : "light"));
// 監視器鈕：切到 mono；已在 mono 時再按一次退回 dark
monoBtn.addEventListener("click", () =>
  applyTheme(document.documentElement.dataset.theme === "mono" ? "dark" : "mono"));

let initTheme = "dark";
try { initTheme = localStorage.getItem("rm-theme") || "dark"; } catch (e) { /* 同上 */ }
applyTheme(initTheme);

// ── 版面欄數（2–8 欄，選擇存 localStorage）───────────────────────
// 密度依欄數自動分級：欄數越多卡片越精簡（波形與設定值字級逐級縮小），
// 好讓一頁塞得下 ~20 床；分級門檻與各級樣式見 style.css 的 main#grid[data-density]。
// 設定值依「實際卡片寬度 × 該模式的參數數量」決定顯示或整列隱藏，永不折成第二列。
// 警報/床號/波形不論密度一律保留；完整資料點卡片放大仍看得到。
const colsSelect = document.getElementById("colsSelect");
const DEFAULT_COLS = 5;
const MIN_COLS = 2, MAX_COLS = 8;
let currentCols = DEFAULT_COLS;

function densityTier(n) {
  return n <= 3 ? "full" : n <= 5 ? "mid" : "compact";
}

function applyCols(n) {
  n = Math.max(MIN_COLS, Math.min(MAX_COLS, n | 0));
  currentCols = n;
  grid.style.setProperty("--cols", n);
  grid.dataset.density = densityTier(n);
  colsSelect.value = String(n);
  try { localStorage.setItem("rm-cols", n); } catch (e) { /* 私密瀏覽等，忽略 */ }
  syncGridPlaceholders();          // 欄數改變時重新補齊末列空位
  devices.forEach(setupCanvases);   // 欄寬/波形高度改變 → 依新尺寸從歷史重畫
  relayoutParamStrips();            // 欄寬改變 → 重排單列設定值，過窄時整列隱藏
}

let headerDismissTimer = null;
function dismissAutoHideHeader() {
  colsSelect.blur();                // select 的焦點不可繼續把狀態列鎖在展開狀態
  if (!autoHideHeader) return;
  clearTimeout(headerDismissTimer);
  autoHideHeader.classList.add("is-dismissed");
  // 收合動畫結束後解除強制狀態，讓下次碰到頂端時仍可正常展開。
  headerDismissTimer = setTimeout(() => {
    autoHideHeader.classList.remove("is-dismissed");
    headerDismissTimer = null;
  }, 220);
}

colsSelect.addEventListener("change", () => {
  applyCols(parseInt(colsSelect.value, 10));
  dismissAutoHideHeader();
});

let initCols = DEFAULT_COLS;
try { initCols = parseInt(localStorage.getItem("rm-cols"), 10) || DEFAULT_COLS; } catch (e) { /* 同上 */ }
applyCols(initCols);

// ── 呼吸器警報音（呼吸器發出 level 1/2/3 警報時鳴響；靜音鈕在卡片右上角）──
// 三個等級皆由 alarm_synth.js 的 Web Audio 音符表即時合成，不載入音檔。
// 瀏覽器自動播放政策要求
// 使用者先與頁面互動過才可出聲，故以首次點擊/按鍵解鎖共用 AudioContext；
// 解鎖前只有視覺警示（狀態列警報 + 卡片警示外框），不影響任何功能。
let audioCtx = null;
const ALARM_SILENCE_GAP_SEC = 2.5;
let activeAlarmPlayback = null;
let nextAlarmPlayAt = 0;

function unlockAudio() {
  try {
    if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    if (audioCtx.state === "suspended") audioCtx.resume().catch(() => { /* 等待下次互動 */ });
  } catch (e) { /* 不支援 Web Audio → 維持純視覺警示 */ }
}
window.addEventListener("pointerdown", unlockAudio);
window.addEventListener("keydown", unlockAudio);

function playEmergencyFallback(level, at) {
  // alarm_synth.js 若因部署遺漏而未載入，仍以 Web Audio 單音避免完全靜默。
  const durations = { 1: 0.6, 2: 0.4, 3: 0.2 };
  const duration = durations[level] || durations[3];
  const osc = audioCtx.createOscillator();
  const gain = audioCtx.createGain();
  osc.connect(gain);
  gain.connect(audioCtx.destination);
  osc.frequency.value = level === 3 ? 785 : 988;
  gain.gain.setValueAtTime(0.18, at);
  gain.gain.setTargetAtTime(0.0001, at + duration - 0.03, 0.01);
  osc.start(at);
  osc.stop(at + duration);
  osc.onended = () => { osc.disconnect(); gain.disconnect(); };
  return {
    duration,
    endAt: at + duration,
    stop() {
      osc.onended = null;
      try { osc.stop(); } catch (e) { /* 已自然結束 */ }
      try { osc.disconnect(); } catch (e) { /* 已斷開 */ }
      try { gain.disconnect(); } catch (e) { /* 已斷開 */ }
    },
  };
}

function stopActiveAlarmSound() {
  if (!activeAlarmPlayback) return;
  const playback = activeAlarmPlayback;
  activeAlarmPlayback = null;
  playback.stop();
}

function playAlarmSound(level, at) {
  if (window.RMAlarmSynth && typeof window.RMAlarmSynth.play === "function") {
    return window.RMAlarmSynth.play(audioCtx, level, at);
  }
  return playEmergencyFallback(level, at);
}

function isMuted(dev) {
  return Date.now() < dev.muteUntil;
}

/** 右上角靜音鈕外觀：靜音中顯示剩餘倒數，時間到自動復歸（由下方 300ms 迴圈驅動） */
function updateMuteBtn(dev) {
  const left = dev.muteUntil - Date.now();
  if (left > 0) {
    const s = Math.ceil(left / 1000);
    dev.muteBtn.textContent = `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
    dev.muteBtn.classList.add("muted");
  } else {
    dev.muteBtn.textContent = "🔔";
    dev.muteBtn.classList.remove("muted");
  }
}

// 取所有「線上、未靜音、有警報」裝置中最嚴重、同級優先值最高的警報鳴響；
// 每段合成音完整播放後靜音 2.5 秒，屆時警報仍存在才重播。
// Pi 離線後警報資料已不可信，不列入鳴響（畫面仍以離線紅框提示）。
setInterval(() => {
  let selected = null;
  for (const dev of devices.values()) {
    updateMuteBtn(dev);
    const alarm = dev.soundAlarm;
    if (!dev.online || isMuted(dev) || !alarm) continue;
    if (!selected || alarm.level < selected.level
        || (alarm.level === selected.level && (alarm.prio || 0) > (selected.prio || 0))) {
      selected = alarm;
    }
  }
  if (!selected) {
    nextAlarmPlayAt = 0;
    stopActiveAlarmSound();
    return;
  }
  if (!audioCtx || audioCtx.state !== "running") return;   // 尚未解鎖
  const now = audioCtx.currentTime;
  if (activeAlarmPlayback && now < activeAlarmPlayback.endAt) return; // 讓整段合成音完整播完
  activeAlarmPlayback = null;
  if (now < nextAlarmPlayAt) return;
  try {
    const t = now + 0.02;
    activeAlarmPlayback = playAlarmSound(selected.level, t);
    // 2.5 秒從整段音色的尾音結束後才開始計算。
    nextAlarmPlayAt = activeAlarmPlayback.endAt + ALARM_SILENCE_GAP_SEC;
  } catch (e) { /* 靜默忽略 */ }
}, 300);

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
    online: false,        // Pi ↔ 伺服器連線（由 link 訊息維護）
    alarmLevel: 0,        // 目前最嚴重警報等級（0 = 無警報）
    soundAlarm: null,     // 目前拿來選擇警報等級的完整警報（含 cp/code/level/prio）
    muteUntil: 0,         // 靜音截止時間戳（ms）；按一次靜音 MUTE_SEC 秒，再按取消
    muteBtn: null,        // 右上角靜音鈕（buildCard 填入）
    loopStarted: false,
    loopCurrent: [],
    loopBreaths: [],
    loopCanvas: null,
    loopEmpty: null,
    loopSelect: null,
    alarmHistoryEl: null,
    alarmHistorySeq: 0,
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
      <span class="head-alarms"></span>
      <span class="spacer"></span>
      <button class="mute-btn" type="button"
              title="靜音警報2分鐘">🔔</button>
      <button class="close-btn hidden">✕</button>
    </div>
    <div class="monitor-main">
      <div class="waves"></div>
      <div class="strip-cap">設定值</div>
      <div class="param-strip"></div>
    </div>
    <div class="detail">
      <section class="detail-panel measured-panel">
        <h3>測量值</h3>
        <div class="measured-scroll"><div class="kv-table measured"></div></div>
        <div class="mode-summary">
          <span class="detail-label">模式</span><span class="mode-line">—</span>
        </div>
        <div class="dev-info-line"></div>
      </section>
    </div>
    <div class="detail-lower">
      <section class="detail-panel loop-panel">
        <div class="detail-panel-head">
          <h3>LOOP</h3>
          <select class="loop-select" aria-label="選擇 LOOP 類型">
            <option value="pv">P-V LOOP</option>
            <option value="fv">F-V LOOP</option>
            <option value="pf">P-F LOOP</option>
          </select>
        </div>
        <div class="loop-chart">
          <canvas></canvas>
          <div class="loop-empty">等待完整呼吸週期</div>
        </div>
        <div class="loop-legend"><span>最新</span><span>前 1 次</span><span>前 2 次</span></div>
      </section>
      <section class="detail-panel alarm-history-panel">
        <div class="detail-panel-head"><h3>警報紀錄</h3><span class="panel-meta">近 7 天 · 最近 50 次</span></div>
        <div class="alarm-history-list"><div class="panel-empty">尚無警報紀錄</div></div>
      </section>
      <section class="detail-panel prediction-panel">
        <div class="detail-panel-head"><h3>預測模組</h3><span class="panel-meta">預留介面</span></div>
        <div class="prediction-slots">
          <div class="prediction-slot">
            <div class="prediction-name">拔管成功率</div>
            <div class="prediction-state">模組尚未接入</div>
          </div>
          <div class="prediction-slot">
            <div class="prediction-name">呼吸不同步預測</div>
            <div class="prediction-state">模組尚未接入</div>
          </div>
        </div>
        <div class="prediction-note">目前不產生推估值</div>
      </section>
    </div>`;
  card.querySelector(".dev-name").textContent = dev.id;
  dev.muteBtn = card.querySelector(".mute-btn");
  dev.muteBtn.addEventListener("click", (e) => {
    e.stopPropagation();               // 不觸發卡片放大
    dev.muteUntil = isMuted(dev) ? 0 : Date.now() + MUTE_SEC * 1000;
    updateMuteBtn(dev);
  });

  const waves = card.querySelector(".waves");
  for (const ch of CHANNELS) {
    const row = document.createElement("div");
    row.className = "wave-row";
    row.innerHTML = `<canvas></canvas>
      <span class="wave-label ${ch.key}"><span class="wl-name">${ch.name}</span><span class="wl-unit"> ${ch.unit}</span></span>
      <span class="wave-val ${ch.key}">--</span>`;
    waves.appendChild(row);
    dev.chans.push({ canvas: row.querySelector("canvas"),
                     valEl: row.querySelector(".wave-val"),
                     ctx: null, w: 0, h: 0, prevY: null, hist: [] });
  }
  // 小卡警報：疊在波形區上方，從 Paw 頂端往下堆疊（多則會蓋到 Flow/Vol）；放大檢視改用 header 完整清單
  const alarmBand = document.createElement("div");
  alarmBand.className = "wave-alarm hidden";
  waves.appendChild(alarmBand);

  dev.loopCanvas = card.querySelector(".loop-chart canvas");
  dev.loopEmpty = card.querySelector(".loop-empty");
  dev.loopSelect = card.querySelector(".loop-select");
  dev.loopSelect.value = selectedLoopType;
  dev.loopSelect.addEventListener("change", (e) => {
    e.stopPropagation();
    selectedLoopType = dev.loopSelect.value;
    try { localStorage.setItem("rm-loop-type", selectedLoopType); } catch (err) { /* ignore */ }
    drawLoop(dev);
  });
  dev.loopSelect.addEventListener("click", (e) => e.stopPropagation());
  dev.alarmHistoryEl = card.querySelector(".alarm-history-list");

  card.addEventListener("click", () => { if (!dev.big) expand(dev); });
  card.querySelector(".close-btn").addEventListener("click", (e) => {
    e.stopPropagation();
    collapse(dev);
  });

  dev.card = card;
  grid.appendChild(card);
  if (paramCardResizeObserver) paramCardResizeObserver.observe(card);
  setupCanvases(dev);
}

function syncGridPlaceholders() {
  grid.querySelectorAll(".grid-placeholder").forEach((el) => el.remove());
  if (!devices.size) return;

  const missing = (currentCols - (devices.size % currentCols)) % currentCols;
  for (let i = 0; i < missing; i++) {
    const placeholder = document.createElement("div");
    placeholder.className = "grid-placeholder";
    placeholder.setAttribute("aria-hidden", "true");
    grid.appendChild(placeholder);
  }
}

function sortGrid() {
  [...grid.children]
    .filter((el) => el.classList.contains("card"))
    // 數字感知排序：device-1、device-2、device-10，而不是 device-1、device-10、device-2。
    .sort((a, b) => a.dataset.device.localeCompare(
      b.dataset.device, undefined, { numeric: true, sensitivity: "base" }))
    .forEach((el) => grid.appendChild(el));
  syncGridPlaceholders();
}

// ── 放大 / 還原 ──────────────────────────────────────────────────
function expand(dev) {
  dev.big = true;
  dev.card.classList.add("big");
  dev.card.querySelector(".close-btn").classList.remove("hidden");
  overlay.appendChild(dev.card);
  overlay.classList.remove("hidden");
  document.body.classList.add("detail-open");
  dev.loopSelect.value = selectedLoopType;
  loadAlarmHistory(dev);
  requestAnimationFrame(() => {
    setupCanvases(dev);
    drawLoop(dev);
    layoutParamStrip(dev.card.querySelector(".param-strip"));
  });
}

function collapse(dev) {
  dev.big = false;
  dev.card.classList.remove("big");
  dev.card.querySelector(".close-btn").classList.add("hidden");
  overlay.classList.add("hidden");
  document.body.classList.remove("detail-open");
  dev.alarmHistorySeq++;
  grid.appendChild(dev.card);
  sortGrid();
  setupCanvases(dev);
  requestAnimationFrame(() => layoutParamStrip(dev.card.querySelector(".param-strip")));
}

// ── Canvas 初始化與重繪（尺寸改變時從歷史重播）───────────────────
function setupCanvases(dev) {
  const dpr = window.devicePixelRatio || 1;
  const oldPos = dev.pos;    // 重播後接續原掃描位置用（放大/縮回/換主題不從頭畫）
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
  // 從歷史重播，畫面不留白。掃描位置接續 resize/放大前的位置：
  // 從「原位置往回推 n 個樣本」處起畫，重播完剛好回到原位置，
  // 波形繼續往前掃而不是從左端重新開始（畫到一半突然歸零很干擾判讀）
  const n = dev.chans[0].hist.length;
  if (n) {
    const period = WINDOW_SEC * dev.rate;    // 掃描一輪的樣本數
    let start = (oldPos - n) % period;
    if (start < 0) start += period;
    dev.pos = start;
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

// ── 呼吸 LOOP：以吸氣 trigger 到下一個 trigger 為一個完整週期 ──────
function recordLoopSamples(dev, samples) {
  let completed = false;
  const minSamples = Math.max(10, Math.floor(dev.rate * 0.25));
  const maxSamples = Math.max(200, Math.ceil(dev.rate * 20));

  for (const sample of samples) {
    if (sample.trig) {
      // 同一個 trigger 同時是上一個呼吸的終點、下一個呼吸的吸氣起點。
      // 因此完成的 LOOP 會嚴格涵蓋 trigger → 下一個 trigger，而不是任意切點。
      if (dev.loopStarted && dev.loopCurrent.length + 1 >= minSamples) {
        dev.loopBreaths.push(dev.loopCurrent.concat(sample));
        if (dev.loopBreaths.length > LOOP_BREATHS) dev.loopBreaths.shift();
        completed = true;
      }
      dev.loopCurrent = [sample];
      dev.loopStarted = true;
    } else if (dev.loopStarted) {
      dev.loopCurrent.push(sample);   // 沿用播放佇列物件，避免每台每秒再配置約 100 個物件
    }
    if (dev.loopCurrent.length > maxSamples) {
      // 週期異常過長時不可裁掉開頭，否則圖形會失去 trigger 起點。
      // 丟棄本圈並等待下一個 trigger，確保所有被畫出的 LOOP 都從吸氣起點開始。
      dev.loopCurrent = [];
      dev.loopStarted = false;
    }
  }
  if (completed) drawLoop(dev);
}

function loopTick(value) {
  return Math.abs(value) >= 100 ? String(Math.round(value)) : String(Math.round(value * 10) / 10);
}

function drawLoop(dev) {
  if (!dev || !dev.big || !dev.loopCanvas || !dev.loopCanvas.isConnected) return;
  const canvas = dev.loopCanvas;
  const cssW = canvas.clientWidth;
  const cssH = canvas.clientHeight;
  if (!cssW || !cssH) return;

  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.round(cssW * dpr);
  canvas.height = Math.round(cssH * dpr);
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, cssW, cssH);

  const cfg = LOOP_TYPES[selectedLoopType] || LOOP_TYPES.pv;
  const margin = { left: 54, right: 14, top: 12, bottom: 34 };
  const plotW = Math.max(1, cssW - margin.left - margin.right);
  const plotH = Math.max(1, cssH - margin.top - margin.bottom);
  const xOf = (value) => margin.left + (value - cfg.xMin) / (cfg.xMax - cfg.xMin) * plotW;
  const yOfLoop = (value) => margin.top + (cfg.yMax - value) / (cfg.yMax - cfg.yMin) * plotH;
  const labelColor = cssVar("--text-label") || "#8FA2BC";
  const lineColor = cssVar("--c-wave") || "#2F80ED";

  ctx.strokeStyle = GRID_COLOR;
  ctx.fillStyle = labelColor;
  ctx.lineWidth = 1;
  ctx.font = '10px "Segoe UI", sans-serif';
  ctx.textBaseline = "top";
  for (let i = 0; i <= 4; i++) {
    const x = margin.left + plotW * i / 4;
    const y = margin.top + plotH * i / 4;
    ctx.globalAlpha = (i === 0 || i === 4) ? 0.9 : 0.45;
    ctx.beginPath(); ctx.moveTo(x, margin.top); ctx.lineTo(x, margin.top + plotH); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(margin.left, y); ctx.lineTo(margin.left + plotW, y); ctx.stroke();
    ctx.globalAlpha = 1;
    const xv = cfg.xMin + (cfg.xMax - cfg.xMin) * i / 4;
    const yv = cfg.yMax - (cfg.yMax - cfg.yMin) * i / 4;
    ctx.textAlign = "center";
    ctx.fillText(loopTick(xv), x, margin.top + plotH + 5);
    ctx.textAlign = "right";
    ctx.fillText(loopTick(yv), margin.left - 7, y - 5);
  }

  // 零線比一般格線略亮，Flow 正負向與壓力零點更容易辨認。
  ctx.globalAlpha = 0.8;
  ctx.strokeStyle = GRID_COLOR;
  if (cfg.xMin < 0 && cfg.xMax > 0) {
    ctx.beginPath(); ctx.moveTo(xOf(0), margin.top); ctx.lineTo(xOf(0), margin.top + plotH); ctx.stroke();
  }
  if (cfg.yMin < 0 && cfg.yMax > 0) {
    ctx.beginPath(); ctx.moveTo(margin.left, yOfLoop(0)); ctx.lineTo(margin.left + plotW, yOfLoop(0)); ctx.stroke();
  }
  ctx.globalAlpha = 1;

  ctx.fillStyle = labelColor;
  ctx.font = '11px "Segoe UI", sans-serif';
  ctx.textAlign = "center";
  ctx.fillText(cfg.xLabel, margin.left + plotW / 2, cssH - 14);
  ctx.save();
  ctx.translate(13, margin.top + plotH / 2);
  ctx.rotate(-Math.PI / 2);
  ctx.fillText(cfg.yLabel, 0, 0);
  ctx.restore();

  const breaths = dev.loopBreaths.slice(-LOOP_BREATHS);
  dev.loopEmpty.classList.toggle("hidden", breaths.length > 0);
  const alphas = [0.22, 0.45, 1.0];
  ctx.save();
  ctx.beginPath();
  ctx.rect(margin.left, margin.top, plotW, plotH);
  ctx.clip();
  breaths.forEach((breath, index) => {
    if (!breath.length) return;
    const alphaIndex = LOOP_BREATHS - breaths.length + index;
    ctx.strokeStyle = lineColor;
    ctx.globalAlpha = alphas[alphaIndex];
    ctx.lineWidth = index === breaths.length - 1 ? 2.5 : 1.5;
    ctx.lineJoin = "round";
    ctx.beginPath();
    for (let i = 0; i < breath.length; i++) {
      const x = xOf(breath[i][cfg.xKey]);
      const y = yOfLoop(breath[i][cfg.yKey]);
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    }
    ctx.stroke();

    // 最新一圈以圓點標出 trigger（吸氣起始點），讓相位圖的起點清楚可辨。
    if (index === breaths.length - 1 && breath[0].trig) {
      ctx.fillStyle = TRIG_COLOR;
      ctx.globalAlpha = 1;
      ctx.beginPath();
      ctx.arc(xOf(breath[0][cfg.xKey]), yOfLoop(breath[0][cfg.yKey]), 4, 0, Math.PI * 2);
      ctx.fill();
    }
  });
  ctx.restore();
  ctx.globalAlpha = 1;
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
      const samples = q.splice(0, n);
      recordLoopSamples(dev, samples);
      drawSamples(dev, samples);
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
  // 文字版連線狀態（呼吸器/伺服器）在管理頁 /admin；本頁只以紅框呈現離線
  dev.online = !!m.online;
  dev.card.classList.toggle("pi-offline", !dev.online);
  if (m.online && m.patient !== undefined) setPatient(dev, m.patient);
}

function setPatient(dev, patient) {
  dev.card.querySelector(".patient").textContent = patient ? `病歷號: ${patient}` : "";
}

// ── 小卡設定值單列排版 ───────────────────────────────────────────
// 每個模式收到的 settings 數量不同（例如 PSV 可只有 3 個設定）；不可假設固定欄數。
// 先以最低可讀字級量出每個晶片真正需要的寬度，再依內容比例分配整列：
// Mode 或長標籤會自然取得較多空間。若所有晶片仍放不下，整列隱藏而非換列或顯示「…」。
const PARAM_FONT_TIERS = {
  full:    { keyMax: 11, valueMax: 15 },
  mid:     { keyMax: 10, valueMax: 13 },
  compact: { keyMax: 9,  valueMax: 11 },
};
const PARAM_KEY_MIN_PX = 8;
const PARAM_VALUE_MIN_PX = 9;
const PARAM_MODE_MIN_PX = 8;
const PARAM_WIDTH_SAFETY_PX = 2;   // 補償瀏覽器小數像素取整，避免剛好多 1px 又溢位

function paramFontTier(card) {
  if (card.classList.contains("big") || window.matchMedia("(max-width: 800px)").matches) {
    return PARAM_FONT_TIERS.full;
  }
  return PARAM_FONT_TIERS[grid.dataset.density] || PARAM_FONT_TIERS.full;
}

function renderedTextWidth(el) {
  if (!el.textContent) return 0;
  const range = document.createRange();
  range.selectNodeContents(el);
  const width = range.getBoundingClientRect().width;
  range.detach();
  return width;
}

function fitText(el, maxPx, minPx) {
  if (!el.clientWidth) return false;
  let size = maxPx;
  el.style.fontSize = `${size}px`;
  while (size > minPx && renderedTextWidth(el) > el.clientWidth + 0.25) {
    size = Math.max(minPx, size - 0.5);
    el.style.fontSize = `${size}px`;
  }
  return renderedTextWidth(el) <= el.clientWidth + 0.25;
}

function boxHorizontalSpace(el) {
  const style = getComputedStyle(el);
  return ["paddingLeft", "paddingRight", "borderLeftWidth", "borderRightWidth"]
    .reduce((sum, key) => sum + (parseFloat(style[key]) || 0), 0);
}

function minimumChipWidth(chip) {
  const key = chip.querySelector(".k");
  const value = chip.querySelector(".val");
  key.style.fontSize = `${PARAM_KEY_MIN_PX}px`;
  value.style.fontSize = `${chip.classList.contains("mode") ? PARAM_MODE_MIN_PX : PARAM_VALUE_MIN_PX}px`;
  return Math.ceil(
    Math.max(renderedTextWidth(key), renderedTextWidth(value))
    + boxHorizontalSpace(chip)
    + PARAM_WIDTH_SAFETY_PX
  );
}

function layoutExpandedParamStrip(strip, chips, tier) {
  strip.classList.remove("params-hidden");
  strip.setAttribute("aria-hidden", "false");
  strip.style.removeProperty("grid-template-columns");
  for (const chip of chips) {
    fitText(chip.querySelector(".k"), tier.keyMax, PARAM_KEY_MIN_PX);
    fitText(
      chip.querySelector(".val"),
      tier.valueMax,
      chip.classList.contains("mode") ? PARAM_MODE_MIN_PX : PARAM_VALUE_MIN_PX
    );
  }
}

function layoutParamStrip(strip) {
  if (!strip || !strip.isConnected) return false;
  const card = strip.closest(".card");
  const chips = [...strip.querySelectorAll(".pchip")];

  if (!chips.length) {
    strip.classList.add("params-hidden");
    strip.setAttribute("aria-hidden", "true");
    return false;
  }

  const tier = paramFontTier(card);
  if (card.classList.contains("big")) {
    layoutExpandedParamStrip(strip, chips, tier);
    return true;
  }

  // 先解除上次的隱藏與欄寬，才能取得這次實際卡片寬度。
  strip.classList.remove("params-hidden");
  strip.setAttribute("aria-hidden", "false");
  strip.style.removeProperty("grid-template-columns");

  const minimumWidths = chips.map(minimumChipWidth);
  const stripStyle = getComputedStyle(strip);
  const innerWidth = strip.clientWidth
    - (parseFloat(stripStyle.paddingLeft) || 0)
    - (parseFloat(stripStyle.paddingRight) || 0);
  const gap = parseFloat(stripStyle.columnGap) || 0;
  const trackSpace = innerWidth - gap * Math.max(0, chips.length - 1);
  const requiredSpace = minimumWidths.reduce((sum, width) => sum + width, 0);

  if (trackSpace + 0.5 < requiredSpace) {
    strip.classList.add("params-hidden");
    strip.setAttribute("aria-hidden", "true");
    return false;
  }

  // fr 比例採用各晶片的最低需求寬度；剩餘空間會依內容比例放大，且始終只有一列。
  strip.style.gridTemplateColumns = minimumWidths.map((width) => `${width}fr`).join(" ");
  const allTextFits = chips.every((chip) => {
    const keyFits = fitText(chip.querySelector(".k"), tier.keyMax, PARAM_KEY_MIN_PX);
    const valueFits = fitText(
      chip.querySelector(".val"),
      tier.valueMax,
      chip.classList.contains("mode") ? PARAM_MODE_MIN_PX : PARAM_VALUE_MIN_PX
    );
    return keyFits && valueFits;
  });

  if (!allTextFits) {
    strip.classList.add("params-hidden");
    strip.setAttribute("aria-hidden", "true");
    return false;
  }
  return true;
}

function relayoutParamStrips() {
  document.querySelectorAll(".param-strip").forEach(layoutParamStrip);
}

// 卡片寬度可能因欄數、視窗、主題邊框或放大／縮回而改變；直接觀察卡片，
// 不依賴固定的欄數門檻。只處理寬度變化，避免設定列顯示／隱藏造成高度回呼迴圈。
const observedParamCardWidths = new WeakMap();
const paramCardResizeObserver = typeof ResizeObserver === "function"
  ? new ResizeObserver((entries) => {
      for (const entry of entries) {
        const width = entry.contentRect.width;
        const previous = observedParamCardWidths.get(entry.target);
        if (previous !== undefined && Math.abs(previous - width) < 0.5) continue;
        observedParamCardWidths.set(entry.target, width);
        layoutParamStrip(entry.target.querySelector(".param-strip"));
      }
    })
  : null;

function onParams(dev, m) {
  // 主模式（VC-SIMV…）與特性旗標（/AF…）分開：放大檢視「模式」顯示完整（含特性），
  // 小卡設定值列只放主模式，避免欄數多時字太長折到第二行
  const modeBase = m.mode || "";
  const modeFull = modeBase + (m.features || []).join("");
  dev.card.querySelector(".mode-line").textContent = modeFull || "—";

  // 通氣模式本質上也是一種設定值（跟 PEEP、RR 一樣是醫護會查看的呼吸器設定），
  // 放在設定值清單最前面（小卡只放主模式，完整模式在放大檢視看得到）
  const settings = modeBase ? { Mode: modeBase, ...(m.settings || {}) } : { ...(m.settings || {}) };

  // VC（容積控制）類模式的 VTi 以 mL 呈現：MEDIBUS 給的是公升（如 0.450），
  // 臨床慣用 450 mL。已是 mL 量級（≥10）的值不動，避免重複換算。
  if (/\bVC/.test(modeBase) && settings.VTi !== undefined) {
    const litres = parseFloat(settings.VTi);
    if (!isNaN(litres) && litres < 10) settings.VTi = `${Math.round(litres * 1000)}`;
  }

  // 小卡片參數列 = 該模式實際收到的設定值（數量不固定）；Mode 也算一項，但依文字需求自動取得較寬欄位。
  const strip = dev.card.querySelector(".param-strip");
  strip.innerHTML = "";
  for (const [k, v] of Object.entries(settings)) {
    const chip = document.createElement("div");
    chip.className = k === "Mode" ? "pchip mode" : "pchip";
    chip.innerHTML = `<div class="k"></div><div class="val"></div>`;
    chip.querySelector(".k").textContent = k;
    chip.querySelector(".val").textContent = v;
    strip.appendChild(chip);
  }
  layoutParamStrip(strip);
  // 放大檢視：所有量測值（設定值不重複列表——小卡參數列在放大時仍看得到）
  fillTable(dev.card.querySelector(".kv-table.measured"), m.measured || {});
}

function compactAlarmTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  const now = new Date();
  const pad = (number) => String(number).padStart(2, "0");
  const clock = `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
  if (date.toDateString() === now.toDateString()) return clock;
  return `${pad(date.getMonth() + 1)}/${pad(date.getDate())} ${clock.slice(0, 5)}`;
}

function alarmDuration(seconds) {
  const value = Math.max(0, Math.round(Number(seconds) || 0));
  if (value < 60) return `${value} 秒`;
  if (value < 3600) return `${Math.floor(value / 60)}分${value % 60}秒`;
  const hours = Math.floor(value / 3600);
  const minutes = Math.floor((value % 3600) / 60);
  return `${hours}時${minutes}分`;
}

function renderAlarmHistory(dev, episodes) {
  const el = dev.alarmHistoryEl;
  if (!el) return;
  el.innerHTML = "";
  if (!episodes.length) {
    el.innerHTML = '<div class="panel-empty">最近 7 天尚無警報紀錄</div>';
    return;
  }
  for (const episode of episodes) {
    const alarm = Object.assign({}, episode, { prio: Number(episode.prio || 0) });
    const classified = RMAlarm.classify(alarm);
    const row = document.createElement("div");
    row.className = `alarm-history-row lvl-${classified.level} ${episode.status || "unknown"}`;
    row.title = `${classified.name}\n開始：${episode.started_at || "—"}\n` +
      `結束：${episode.ended_at || (episode.status === "active" ? "持續中" : "不明")}`;

    const time = document.createElement("span");
    time.className = "alarm-history-time";
    time.textContent = compactAlarmTime(episode.started_at);
    const body = document.createElement("span");
    body.className = "alarm-history-body";
    const name = document.createElement("span");
    name.className = "alarm-history-name";
    name.textContent = classified.name;
    const duration = document.createElement("span");
    duration.className = "alarm-history-duration";
    duration.textContent = alarmDuration(episode.duration_seconds);
    body.append(name, duration);
    const state = document.createElement("span");
    state.className = `alarm-history-event ${episode.status || "unknown"}`;
    state.textContent = episode.status === "active" ? "持續中" :
      episode.status === "cleared" ? "已解除" : "結束不明";
    row.append(time, body, state);
    el.appendChild(row);
  }
}

async function loadAlarmHistory(dev) {
  if (!dev.alarmHistoryEl) return;
  const seq = ++dev.alarmHistorySeq;
  dev.alarmHistoryEl.innerHTML = '<div class="panel-empty">讀取警報紀錄中…</div>';
  try {
    const response = await fetch(`/api/alarm-history/${encodeURIComponent(dev.id)}?limit=50`);
    if (response.status === 401) { location.href = "/login"; return; }
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    if (!dev.big || seq !== dev.alarmHistorySeq) return;
    renderAlarmHistory(dev, Array.isArray(data.episodes) ? data.episodes : []);
  } catch (e) {
    if (dev.big && seq === dev.alarmHistorySeq) {
      dev.alarmHistoryEl.innerHTML = '<div class="panel-empty error">警報紀錄讀取失敗</div>';
    }
  }
}

function onAlarm(dev, m) {
  // 全量更新：alarms 為目前所有警報（空陣列 = 解除）。
  // 依分級（RMAlarm，見 alarm_levels.js）由重到輕排序，同級再依 MEDIBUS 優先級高→低。
  // 兩處呈現：放大檢視在 header 顯示完整清單（各自分級上色）；小卡以半透明橫幅疊在
  // Paw 波形上，只放最嚴重一則＋其餘計數（避免擠在 header 折行）。
  // 嚴重度另以整張卡的警示外框＋header 底色呈現（.card.alarming-*）。
  const alarms = (m.alarms || [])
    .map((a) => Object.assign({}, a, RMAlarm.classify(a)))
    .sort((a, b) => (a.level - b.level) || ((b.prio || 0) - (a.prio || 0)));

  const box = dev.card.querySelector(".head-alarms");     // 放大檢視：完整清單
  const band = dev.card.querySelector(".wave-alarm");      // 小卡：疊在 Paw 上的橫幅
  box.innerHTML = "";
  band.innerHTML = "";
  band.className = "wave-alarm hidden";
  // 三級警報都會點亮整張卡；先清掉上一輪等級，避免警報降級／解除後殘留舊色。
  dev.card.classList.remove("alarming-1", "alarming-2", "alarming-3");
  dev.alarmLevel = 0;
  dev.soundAlarm = null;
  if (!alarms.length) {
    if (dev.big) loadAlarmHistory(dev);
    return;
  }

  // 放大檢視 header：完整清單（每則依自身分級上色）
  for (const a of alarms) {
    const item = document.createElement("span");
    item.className = `alarm-item lvl-${a.level}`;
    item.textContent = `${a.level === 3 ? "•" : "⚠"} ${a.name}`;
    box.appendChild(item);
  }

  const worst = alarms[0].level;      // 已依分級排序，第一筆就是目前最嚴重的等級

  // 小卡橫幅：每則警報各自一條色帶（依自身分級上色），從 Paw 頂端往下堆疊
  // （多則會往下排、蓋到 Flow/Vol 波形）；完整判讀仍可點開卡片
  band.className = "wave-alarm";
  for (const a of alarms) {
    const row = document.createElement("div");
    row.className = `band-row lvl-${a.level}`;
    row.textContent = `${a.level === 3 ? "•" : "⚠"} ${a.name}`;
    band.appendChild(row);
  }

  dev.alarmLevel = worst;
  dev.soundAlarm = alarms[0];          // 保留完整資料，供同級警報以 prio 決定先後
  if (worst >= 1 && worst <= 3) {
    dev.card.classList.add(`alarming-${worst}`);
  }
  if (dev.big) loadAlarmHistory(dev);
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

/** 管理員從管理頁移除離線裝置 → 這裡同步拿掉卡片 */
function onDeviceRemoved(id) {
  const dev = devices.get(id);
  if (!dev) return;
  if (dev.big) {
    overlay.classList.add("hidden");
    document.body.classList.remove("detail-open");
  }
  if (paramCardResizeObserver) paramCardResizeObserver.unobserve(dev.card);
  dev.card.remove();
  devices.delete(id);
  sortGrid();
  if (!devices.size) emptyHint.classList.remove("hidden");
}

// 訊息類型 → 處理函式（新增有狀態類型：加一行 + 寫 onXxx，snapshot 自動生效）
// 注意：sys（Pi 機器健康狀態）與 status（呼吸器連線狀態）只在管理頁顯示，
// 本頁刻意不註冊 → 走未知類型忽略
const MSG_HANDLERS = {
  wave: onWave, link: onLink,
  params: onParams, device_info: onDeviceInfo, alarm: onAlarm,
};
const SNAPSHOT_KEYS = ["params", "device_info", "alarm"];

function dispatch(m) {
  if (m.type === "snapshot") {
    for (const d of m.devices || []) {
      const dev = ensureDev(d.device);
      onLink(dev, { online: d.online, patient: d.patient });
      for (const k of SNAPSHOT_KEYS) {
        if (d[k]) MSG_HANDLERS[k](dev, d[k]);
      }
    }
    // 一次建很多床後，等格線完成再依每張卡的實際寬度統一排版。
    requestAnimationFrame(relayoutParamStrips);
    return;
  }
  if (m.type === "device_removed") { onDeviceRemoved(m.device); return; }
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

// ── 視窗尺寸改變 → 重設所有 canvas ──────────────────────────────
let resizeTimer = null;
window.addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => {
    devices.forEach(setupCanvases);
    devices.forEach(drawLoop);
    relayoutParamStrips();
  }, 200);
});

window.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  const expanded = [...devices.values()].find((dev) => dev.big);
  if (expanded) collapse(expanded);
});

// ── 時鐘 ─────────────────────────────────────────────────────────
setInterval(() => {
  const d = new Date();
  const z = (x) => String(x).padStart(2, "0");
  document.getElementById("clock").textContent =
    `${d.getFullYear()}/${z(d.getMonth() + 1)}/${z(d.getDate())}  ${z(d.getHours())}:${z(d.getMinutes())}:${z(d.getSeconds())}`;
}, 1000);
