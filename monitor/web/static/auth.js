/* 登入狀態顯示、登出、帳號選單 — 共用模組（分工規範見 CLAUDE.md §5）
 * 儀表板（index.html）與管理頁（admin.html）共用：
 * - 顯示登入者帳號、登出按鈕（登入未啟用時 /api/me 回 auth:false → 皆隱藏）
 * - #adminLink（只有儀表板有）僅 admin 角色顯示
 * - 帳號選單：點 #who 開關 #userMenu 下拉選單（深淺色切換、離線提示音、
 *   設備管理、登出等「動作類」按鈕都收在裡面，見 index.html/admin.html
 *   的 header 結構；主題/離線提示音的實際切換邏輯仍在 app.js/admin.js，
 *   這裡只負責選單本身的開關）
 * 全域命名空間：RMAuth；RMAuth.me 是 /api/me 的 Promise（admin.js 用來確認角色） */
"use strict";

const RMAuth = (() => {
  const whoBtn = document.getElementById("who");
  const userMenu = document.getElementById("userMenu");
  const logoutBtn = document.getElementById("logoutBtn");
  const adminLink = document.getElementById("adminLink");   // 管理頁沒有此元素

  const me = fetch("/api/me")
    .then((r) => (r.ok ? r.json() : null))
    .catch(() => null);

  me.then((m) => {
    if (m && m.username) {
      whoBtn.textContent = `帳號 ${m.username} ▾`;
      logoutBtn.classList.remove("hidden");
    } else {
      whoBtn.textContent = "選單 ▾";
    }
    if (adminLink && m && m.role === "admin") adminLink.classList.remove("hidden");
  });

  logoutBtn.addEventListener("click", () =>
    fetch("/logout", { method: "POST" })
      .then(() => { location.href = "/login"; })
      .catch(() => { location.href = "/login"; }));

  // ── 帳號選單開關 ─────────────────────────────────────────────────
  whoBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    userMenu.classList.toggle("hidden");
  });
  document.addEventListener("click", (e) => {
    if (!userMenu.classList.contains("hidden")
        && !userMenu.contains(e.target) && e.target !== whoBtn) {
      userMenu.classList.add("hidden");
    }
  });

  return { me };
})();
