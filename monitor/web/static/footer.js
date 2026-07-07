/* 免責聲明 + 版本號 — 共用模組（分工規範見 CLAUDE.md §5）
 * 三頁共用（儀表板/管理頁/登入頁）：footer 文字固定寫在各 html，
 * 這裡只負責把版本號填進去（讀 /healthz，免登入即可存取）。
 * IMPROVEMENT_PLAN.md W-301（免責聲明常駐）與 W-306（版本資訊顯示）。 */
"use strict";

fetch("/healthz")
  .then((r) => (r.ok ? r.json() : null))
  .then((d) => {
    const el = document.getElementById("verInfo");
    if (el && d && d.version) el.textContent = `v${d.version}`;
  })
  .catch(() => { /* 版本號顯示失敗不影響任何功能，靜默忽略 */ });
