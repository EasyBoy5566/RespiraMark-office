/* Pi 系統健康狀態 — 共用模組（分工規範見 CLAUDE.md §5）
 * 指標定義（門檻）、分級上色、趨勢圖繪製、歷史抓取。
 * 管理頁（admin.js）使用；主儀表板不顯示機器狀態，因此 index.html 不載入本檔。
 * 全域命名空間：RMSys（原生 JS，無模組系統） */
"use strict";

const RMSys = (() => {

  // 指標門檻：warn 變黃、crit 變紅。溫度依 Pi 5：~80°C 起降頻；
  // 使用率/磁碟接近滿載才示警。
  const METRICS = [
    { key: "cpu",      label: "CPU",  unit: "%",  min: 0, max: 100, warn: 85, crit: 95 },
    { key: "mem",      label: "記憶體", unit: "%", min: 0, max: 100, warn: 85, crit: 95 },
    { key: "temp",     label: "溫度",  unit: "°C", min: 0, max: 90,  warn: 60, crit: 80 },
    { key: "disk_pct", label: "磁碟",  unit: "%",  min: 0, max: 100, warn: 85, crit: 95 },
  ];
  const HIST_MAX = 1000;   // 前端每台裝置保留的 sys 樣本上限（趨勢圖用）

  function cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  /** 回傳 ""｜"warn"｜"crit"（null/無資料 → ""） */
  function level(metric, value) {
    if (value === null || value === undefined || value === "") return "";
    if (value >= metric.crit) return "crit";
    if (value >= metric.warn) return "warn";
    return "";
  }

  function fmtUptime(sec) {
    if (sec === null || sec === undefined || sec === "") return "—";
    const d = Math.floor(sec / 86400), h = Math.floor((sec % 86400) / 3600),
          mi = Math.floor((sec % 3600) / 60);
    return d > 0 ? `${d}天${h}時` : h > 0 ? `${h}時${mi}分` : `${mi}分`;
  }

  /** 把一則 sys 樣本推入歷史（守住上限） */
  function push(hist, m) {
    hist.push(m);
    if (hist.length > HIST_MAX) hist.shift();
  }

  /** 抓伺服器記憶體歷史（展開趨勢時補齊）；失敗回傳空陣列，由即時資料續接 */
  function fetchHistory(deviceId) {
    return fetch(`/history/${encodeURIComponent(deviceId)}`)
      .then((r) => (r.ok ? r.json() : null))
      .then((data) => (data && data.samples) || [])
      .catch(() => []);
  }

  /** 併入抓取期間到達的更新樣本（依 ts 去重），避免漏掉最新幾筆 */
  function mergeHistory(fetched, live) {
    if (!fetched.length) return live;
    const lastTs = fetched[fetched.length - 1].ts || 0;
    const merged = fetched.concat(live.filter((s) => (s.ts || 0) > lastTs));
    if (merged.length > HIST_MAX) merged.splice(0, merged.length - HIST_MAX);
    return merged;
  }

  /** 在 container 內建立每個指標一格的趨勢圖，回傳 chans 陣列（供 setup/draw） */
  function buildCharts(container) {
    container.innerHTML = "";
    const chans = [];
    for (const metric of METRICS) {
      const cell = document.createElement("div");
      cell.className = "sys-chart";
      cell.innerHTML = `<div class="sys-chart-head">
          <span class="sys-chart-label">${metric.label}</span>
          <span class="sys-chart-val ${metric.key}">--</span>
        </div><canvas></canvas>`;
      container.appendChild(cell);
      chans.push({ canvas: cell.querySelector("canvas"),
                   valEl: cell.querySelector(".sys-chart-val"),
                   ctx: null, w: 0, h: 0 });
    }
    return chans;
  }

  /** Canvas 尺寸初始化（版面/主題改變後呼叫，之後再 drawCharts） */
  function setupCharts(chans) {
    const dpr = window.devicePixelRatio || 1;
    for (const c of chans) {
      const cssW = c.canvas.clientWidth || 300;
      const cssH = c.canvas.clientHeight || 64;
      c.canvas.width = Math.round(cssW * dpr);
      c.canvas.height = Math.round(cssH * dpr);
      c.ctx = c.canvas.getContext("2d");
      c.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      c.w = cssW; c.h = cssH;
    }
  }

  function drawCharts(chans, hist) {
    for (let i = 0; i < METRICS.length; i++) drawChart(METRICS[i], chans[i], hist);
  }

  /** 單一指標的趨勢折線：門檻虛線（黃/紅）+ 資料線，缺值處斷線 */
  function drawChart(metric, c, hist) {
    if (!c.ctx) return;
    const ctx = c.ctx, w = c.w, h = c.h;
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
    c.valEl.className = `sys-chart-val ${metric.key} ${level(metric, latest)}`;
  }

  return { METRICS, HIST_MAX, cssVar, level, fmtUptime, push,
           fetchHistory, mergeHistory, buildCharts, setupCharts, drawCharts };
})();
