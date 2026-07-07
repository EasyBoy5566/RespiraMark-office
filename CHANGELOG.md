# CHANGELOG

## v0.9.0 — 醫院前基線（2026-07-07）

進醫院資訊室談伺服器架設之前的基線版本。已具備：

- 三層架構（transport/domain/web），composition root 在 `main.py`
- TLS 加密（自建 CA + 憑證，`tools/make_certs.py`），憑證缺失時拒絕啟動
- 瀏覽器登入（PBKDF2-SHA256 密碼雜湊、伺服器端 session、viewer/admin 角色、登入失敗 IP 鎖定）
- 管理頁 `/admin`：設備健康總表、CPU/記憶體/溫度/磁碟/降頻趨勢、移除離線裝置、sys CSV 下載、帳號唯讀清單
- 多床即時儀表板：三波形（抖動緩衝平滑播放）、通氣模式、設定值、量測值、**三級警報分色**（依 MEDIBUS.X IFU 分級對照表）
- Pi 系統健康遙測（CPU/記憶體/溫度/磁碟/降頻/開機時長），CSV 落地並節流
- 深/淺色主題、自動重連
- 開發配套：`tools/fake_pi.py` 模擬器、hub 單元測試、涵蓋 TLS/登入/角色/管理功能的端對端冒煙測試
- `IMPROVEMENT_PLAN.md`：醫院上線前完整資安與功能評估、分階段工作清單（Phase 0～5）

已知風險與後續計畫詳見 `IMPROVEMENT_PLAN.md`。下一步：Phase 1 資安強化（每台 Pi 獨立 token、連線防護、訊息驗證、安全標頭、session 強化、審計日誌等）。
