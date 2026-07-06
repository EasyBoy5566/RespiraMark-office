/* 登入狀態顯示與登出 — 共用模組（分工規範見 CLAUDE.md §5）
 * 儀表板（index.html）與管理頁（admin.html）共用：
 * - 顯示登入者帳號、登出按鈕（登入未啟用時 /api/me 回 auth:false → 皆隱藏）
 * - #adminLink（只有儀表板有）僅 admin 角色顯示
 * 全域命名空間：RMAuth；RMAuth.me 是 /api/me 的 Promise（admin.js 用來確認角色） */
"use strict";

const RMAuth = (() => {
  const whoEl = document.getElementById("who");
  const logoutBtn = document.getElementById("logoutBtn");
  const adminLink = document.getElementById("adminLink");   // 管理頁沒有此元素

  const me = fetch("/api/me")
    .then((r) => (r.ok ? r.json() : null))
    .catch(() => null);

  me.then((m) => {
    if (m && m.username) {
      whoEl.textContent = `帳號 ${m.username}`;
      logoutBtn.classList.remove("hidden");
    }
    if (adminLink && m && m.role === "admin") adminLink.classList.remove("hidden");
  });

  logoutBtn.addEventListener("click", () =>
    fetch("/logout", { method: "POST" })
      .then(() => { location.href = "/login"; })
      .catch(() => { location.href = "/login"; }));

  return { me };
})();
