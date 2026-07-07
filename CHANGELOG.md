# CHANGELOG

## v1.1.0 — LDAP/AD 帳號整合上線、移除本機改密碼、Header 選單改版（2026-07-07）

你確認要用院內 LDAP/AD 取代本機密碼自助管理（W-307 提前實作，不再是選做），
並要求把 header 一整排按鈕收整成下拉選單。

- **LDAP/AD 帳號驗證**（W-307）：新增 `monitor/web/ldap_auth.py`
  （`LdapAuthenticator`），`monitor/web/auth.py` 的 `AuthManager` 改為依賴注入
  （不再寫死 `LocalAuthenticator`），`config.json` 新增 `auth_backend`
  （`local`/`ldap`）等欄位。**預設仍是 `local`**，等資訊室提供實際 LDAP 連線
  資訊後只需改設定檔即可切換，不用改程式碼。新增相依套件 `ldap3==2.9.1`
  （僅 `auth_backend=ldap` 時需要安裝，已於本次獲你核准）
- **移除密碼自助管理功能**（W-303 整組移除）：自助改密碼（`POST /api/password`）
  與管理員重設密碼（`POST /api/admin/reset-password/{username}`）連同前端
  「改密碼」按鈕/彈窗全部拿掉——密碼管理方式改為單純依 `auth_backend`：
  `local` 模式由管理員在伺服器執行 `tools/make_user.py`，`ldap` 模式密碼完全
  交給院內 HIS/AD
- **Header 改成帳號下拉選單**：深淺色切換、離線提示音、設備管理、登出等
  「動作類」項目收整到點帳號名稱展開的下拉選單，取代一整排按鈕
- 單元測試新增 `tests/test_ldap_auth.py`（8 項，用 `ldap3` 內建 `MOCK_SYNC`
  策略模擬 LDAP 目錄，不需要真的院內環境即可驗證 bind 成功/失敗/未知帳號/
  不在白名單/空密碼等情況），總數達 68 項；`tests/smoke_test.py` 移除已不存在
  端點的測試段，其餘全數通過

## v1.0.0-rc1 — Phase 3 完成：功能補強（2026-07-07）

對應 IMPROVEMENT_PLAN.md Phase 3「功能補強」完成定義（G-01/G-02/G-04/G-05
關閉）。**注意**：本版包含的密碼自助管理功能（W-303）已於後續 v1.1.0 移除，
詳見上方說明。

- 免責聲明「本畫面僅供觀察參考，不可作為臨床警報依據」三頁常駐顯示（W-301）
- 警報事件歷史記錄：`domain/alarm_log.py` 記錄出現/解除時間、代碼、優先級到
  CSV（只記機台編號，不記病人代碼），管理頁可下載（W-302）
- 密碼自助管理：登入者自助改密碼、admin 可重設任一帳號密碼（W-303，
  **已於 v1.1.0 移除**）
- 裝置離線醒目提醒改高對比樣式 + 可選提示音（W-304）
- `/healthz` 健康檢查、版本號顯示（W-306）
- 正式伺服器維運腳本：`tools/setup_service.ps1`（開機自動啟動+自動重啟）、
  `tools/lock_permissions.ps1`（鎖敏感檔案權限）、`tools/backup.ps1`（自動
  備份，不含 CA 私鑰）、時間同步（NTP）文件

單元測試累計達 60 項，冒煙測試同步擴充涵蓋警報歷史、密碼自助管理等新端點。
W-305（歷史波形回放）依計畫維持選做、不做。

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
