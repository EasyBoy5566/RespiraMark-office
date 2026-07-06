/* 登入頁邏輯：只負責顯示錯誤訊息（表單本身走原生 POST /login）
 * 錯誤代碼由伺服器以 query string 帶回：?err=1 帳密錯誤、?err=lock 嘗試過多 */
"use strict";

const params = new URLSearchParams(location.search);
const errEl = document.getElementById("loginErr");
if (params.get("err") === "lock") {
  errEl.textContent = "嘗試次數過多，帳號來源已暫時鎖定，請約 10 分鐘後再試";
  errEl.classList.remove("hidden");
} else if (params.has("err")) {
  errEl.textContent = "帳號或密碼錯誤";
  errEl.classList.remove("hidden");
}
