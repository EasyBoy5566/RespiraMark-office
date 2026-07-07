/* 登入狀態顯示、登出、自助改密碼 — 共用模組（分工規範見 CLAUDE.md §5）
 * 儀表板（index.html）與管理頁（admin.html）共用：
 * - 顯示登入者帳號、登出按鈕（登入未啟用時 /api/me 回 auth:false → 皆隱藏）
 * - #adminLink（只有儀表板有）僅 admin 角色顯示
 * - 自助改密碼（IMPROVEMENT_PLAN.md W-303）：#pwBtn 開啟 #pwModal，
 *   POST /api/password（驗舊密碼＋新密碼 ≥8 碼）；兩頁都要有同一組 DOM
 * 全域命名空間：RMAuth；RMAuth.me 是 /api/me 的 Promise（admin.js 用來確認角色） */
"use strict";

const RMAuth = (() => {
  const whoEl = document.getElementById("who");
  const logoutBtn = document.getElementById("logoutBtn");
  const adminLink = document.getElementById("adminLink");   // 管理頁沒有此元素
  const pwBtn = document.getElementById("pwBtn");
  const pwModal = document.getElementById("pwModal");
  const pwOld = document.getElementById("pwOld");
  const pwNew = document.getElementById("pwNew");
  const pwSubmit = document.getElementById("pwSubmit");
  const pwCancel = document.getElementById("pwCancel");
  const pwErr = document.getElementById("pwErr");

  const me = fetch("/api/me")
    .then((r) => (r.ok ? r.json() : null))
    .catch(() => null);

  me.then((m) => {
    if (m && m.username) {
      whoEl.textContent = `帳號 ${m.username}`;
      logoutBtn.classList.remove("hidden");
      if (pwBtn) pwBtn.classList.remove("hidden");
    }
    if (adminLink && m && m.role === "admin") adminLink.classList.remove("hidden");
  });

  logoutBtn.addEventListener("click", () =>
    fetch("/logout", { method: "POST" })
      .then(() => { location.href = "/login"; })
      .catch(() => { location.href = "/login"; }));

  // ── 自助改密碼 ───────────────────────────────────────────────────
  function openPwModal() {
    pwOld.value = ""; pwNew.value = "";
    pwErr.classList.add("hidden");
    pwModal.classList.remove("hidden");
    pwOld.focus();
  }
  function closePwModal() { pwModal.classList.add("hidden"); }

  function showPwErr(msg) {
    pwErr.textContent = msg;
    pwErr.classList.remove("hidden");
  }

  function submitPw() {
    if (pwNew.value.length < 8) { showPwErr("新密碼至少 8 個字元"); return; }
    const body = new URLSearchParams({ old_password: pwOld.value, new_password: pwNew.value });
    fetch("/api/password", { method: "POST", body })
      .then(async (r) => {
        if (r.ok) {
          closePwModal();
          alert("密碼已更新，下次登入請用新密碼。");
          return;
        }
        const j = await r.json().catch(() => ({}));
        showPwErr(j.error || "修改失敗");
      })
      .catch(() => showPwErr("修改失敗：無法連線伺服器"));
  }

  if (pwBtn) {
    pwBtn.addEventListener("click", openPwModal);
    pwCancel.addEventListener("click", closePwModal);
    pwSubmit.addEventListener("click", submitPw);
  }

  return { me };
})();
