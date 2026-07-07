# CHANGELOG

## v0.11.0 — Phase 2 完成：可無人值守運行（2026-07-07）

伺服器可自動啟動、自動復原、有完整日誌、有備份——對應 IMPROVEMENT_PLAN.md
Phase 2「伺服器可無人值守運行」的完成定義。

- 一般運行 log 落地 `logs/server.log`（W-201），與既有審計日誌並存
- `GET /healthz` 免登入健康檢查端點 + 集中版本號定義（W-203）
- 免責聲明「本畫面僅供觀察參考，不可作為臨床警報依據」三頁常駐顯示（W-301）
- 裝置離線提醒改高對比樣式（不再靠調暗，改紅框+紅底更醒目）+ 可選提示音（W-304）
- 給正式伺服器用的維運腳本：`tools/setup_service.ps1`（開機自動啟動+自動重啟）、
  `tools/lock_permissions.ps1`（鎖敏感檔案權限）、`tools/backup.ps1`（自動備份，
  不含 CA 私鑰）（W-202/204/205）
- README 補時間同步（NTP）與 ca.key 離線保存說明（W-206）
- `tools/soak_monitor.ps1` 補明確判讀門檻（W-207）

W-202/204 的腳本已寫好並用 `-WhatIf`/`-DryRun` 驗證過語法與行為，實際在正式
伺服器上套用（真的建立排程工作、真的鎖權限）留待 Phase 5 醫院移轉時執行。

## v0.10.0 — Phase 1 完成：F-01～F-15 資安強化（2026-07-07）

- **每台 Pi 獨立存取權杖**（`devices.json` + `tools/make_device.py`）：外洩只需
  停用/換發該台，不再牽連全部裝置（F-01，最主要項目）；未設定時退回舊版單一
  `ingest_token`，向後相容
- ingest 連線防護：同時連線數上限、hello/閒置逾時三重防護；訊息格式驗證
  （wave/params 陣列與鍵數上限）；WebSocket 觀看端數上限（F-02/F-03/F-15）
- token 比對改常數時間比較，防 timing attack（F-07）
- HTTP 安全標頭 middleware（CSP `default-src 'self'`、X-Frame-Options 等）；
  `/ws` 加 Origin 同源檢查（F-04/F-05/F-11）
- session 絕對逾時 + 定期掃除 + 總量上限；登入鎖定改雙鍵（IP+帳號 / IP 總量），
  避免共用電腦時一人打錯密碼連累其他人（F-08/F-09）
- PBKDF2 迭代數 20 萬→60 萬次，舊帳號/裝置零風險相容升級（F-10）
- 新增審計日誌 `logs/audit.log`，內建病人代碼/密碼/token 防呆（F-06）
- `requirements.txt` 鎖定確切版本；過程中意外發現並修正 cp950 語系 Windows
  下 pip 解析 requirements.txt 失敗的問題（F-12）

單元測試由 17 項擴充到 50 項，冒煙測試同步大幅擴充。F-13/F-16 需正式伺服器
環境才能執行，留待 Phase 2/5；F-14（CSRF）評估後決定暫不處理。

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
