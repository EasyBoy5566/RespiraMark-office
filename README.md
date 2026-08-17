# RespiraMark Office — 中央監視儀表板

接收多台 RespiraMark Pi 的即時波形與參數，在瀏覽器顯示多床儀表板。

```
Pi #1..#5 ──TCP 8765──▶ 本伺服器 ──http://本機:8080──▶ 瀏覽器（電腦/手機皆可）
```

跨平台：Windows / macOS / Linux 皆可執行，僅相依 Python 3.9+ 與 aiohttp。

## 快速開始

```bash
python -m pip install -r requirements.txt
python main.py              # Windows 也可直接雙擊 start_server.bat
```

啟動後開瀏覽器進 `http://localhost:8080`。畫面出現「等待裝置連線…」即正常。

### 沒有硬體也能看效果（模擬器）

另開幾個終端機：

```bash
python tools/fake_pi.py --device fake-01 --patient TEST001
python tools/fake_pi.py --device fake-02 --patient TEST002 --rr 22
python tools/fake_pi.py --device fake-03 --patient TEST003 --alarms
```

連本機 `127.0.0.1`／`localhost`、未傳 `--token` 且已有 `devices.json` 時，
模擬器會自動登記測試裝置，為同次執行的所有模擬裝置產生一組共用臨時 token；
檔案中只保存雜湊，不保存明文，也不會覆寫原有的正式裝置。跨機器測試不會自動登記，請先由伺服器端
`tools/make_device.py` 建立測試裝置，再以 `--token` 傳入該次顯示的 token。

儀表板即時出現對應床位卡片：三條波形 + 通氣模式 + 設定值（有警報時卡片頂端顯示紅色警報列）。**點擊卡片放大**可看所有量測值。

### 自動化測試（改完程式 push 前必跑）

```bash
python -m unittest discover -s tests -v   # hub 單元測試（純標準庫，秒級）
python tests/smoke_test.py                # 端對端冒煙測試
```

冒煙測試自動完成「啟伺服器（測試 port）→ 跑兩台 fake_pi → 驗證 token 與 WS 廣播 → 關閉」，兩者全過才 push。

## 真實 Pi 接入

1. 伺服器啟動時會印出本機區網 IP，例如 `192.168.0.50`
2. 在 Pi 的 `RespiraMark-pi/` 目錄：
   ```bash
   cp telemetry.json.example telemetry.json
   nano telemetry.json     # server_host 填上面那個 IP
   ```
3. Pi 啟動監測頁後就會自動連入；沒接呼吸器時只會顯示連線狀態，接上就有波形

## Windows 防火牆（第一次跑必做）

其他裝置連不進來，多半是這個。以系統管理員開 PowerShell：

```powershell
New-NetFirewallRule -DisplayName "RespiraMark ingest" -Direction Inbound -Protocol TCP -LocalPort 8765 -Action Allow
New-NetFirewallRule -DisplayName "RespiraMark web"    -Direction Inbound -Protocol TCP -LocalPort 8080 -Action Allow
```

醫院 VM 部署時 `web_port` 是 443，該台請改放行 443（`-LocalPort 443`）；院內防火牆的
放行則走資訊室「系統建置說明」申請表的「網路訪問權限」欄——服務埠填 **443 ＋ 8765**，
無線網段勾 **csh-device-s**（Pi 上傳）與 **csh-staff-s**（護理站看板）。

## 設定

複製 `config.json.example` 為 `config.json` 修改（沒有此檔則用預設值）：

| 欄位 | 預設 | 說明 |
|---|---|---|
| `ingest_port` | 8765 | Pi 連入的 TCP port |
| `web_port` | 8080 | 瀏覽器網頁 port。預設值是未啟用 TLS 的開發環境用；**醫院部署填 443**（HTTPS 慣用埠，`config.json.example` 已是此值），且必須同時設定 `tls_cert`/`tls_key`，否則等於在 443 上跑明文 |
| `offline_timeout` | 5.0 | 幾秒沒資料判定 Pi 離線 |
| `ingest_token` | （空） | 單一共用存取權杖（`devices.json` 不存在時的退回模式）；設定後 Pi 端 telemetry.json 的 `token` 必須一致才能連入。空字串 = 不驗證（僅限開發環境，**部署前務必設定**） |
| `devices_file` | `devices.json` | 每台裝置獨立權杖檔（配對流程或 `tools/make_device.py` 建立，見下）；存在時優先於 `ingest_token`，**建議部署醫院前改用此模式** |
| `pair_enabled` | true | 是否開放裝置配對端點（見下「新增一台 Pi」）；false = 一律用 `tools/make_device.py` 手動核發 |
| `pair_ttl` | 600.0 | 配對申請的有效秒數（未核可或核可後未領取皆適用） |
| `pair_max_pending` | 5 | 同時待核可的申請數上限 |
| `max_devices` | 16 | 裝置數上限，超過即拒絕新裝置 |
| `ingest_max_conns` | 64 | 同時 TCP 連線數上限，超過拒絕新連線 |
| `ingest_hello_timeout` | 10.0 | 連線後幾秒沒收到合法 hello 就斷線 |
| `ingest_idle_timeout` | 60.0 | hello 通過後幾秒沒資料就斷線（Pi 每 2 秒 ping，留有餘裕） |
| `max_viewers` | 50 | 同時瀏覽器觀看端數上限，超過拒絕新連線（503） |
| `log_dir` | `logs` | 全部執行期日誌的統一根目錄；server/audit 文字日誌及 alarm/sys SQLite 都放在此目錄內 |
| `log_retention_days` | 190 | `server.log`／`audit.log` 每日輪替後的保留天數；資通系統防護基準要求正式系統日誌保留至少 6 個月，低於 180 會被拉回下限並於啟動時警告 |
| `sys_db_path` | `sys_logs/sys_history.sqlite3` | 相對 `log_dir` 的系統狀態 SQLite；所有機台共用，匯出時才產生 CSV |
| `sys_persist_interval` | 60.0 | 系統狀態寫入 SQLite 的節流秒數，不影響即時畫面 |
| `sys_retention_days` | 7 | 系統狀態保存天數 |
| `alarm_db_path` | `alarm_logs/alarm_history.sqlite3` | 相對 `log_dir` 的警報歷史 SQLite；畫面與匯出仍可依機台獨立查詢 |
| `alarm_retention_days` | 7 | 已結束警報保留天數；仍在作用中的警報不會因超過期限被刪除 |

執行期檔案集中於 `logs/`：`server.log`、`audit.log`、`alarm_logs/alarm_history.sqlite3`、`sys_logs/sys_history.sqlite3`。警報與系統狀態都只保存本機最近 7 天且**不納入備份**；兩者皆不保存病人代碼、波形或量測值。Sys／Alarm CSV 都只在管理員下載時即時產生，伺服器端不留匯出檔。

### 新增一台 Pi：裝置配對（建議做法）

不需要人工複製貼上 token：

1. **Pi 端**：設定頁按「與伺服器配對」（若還沒填位址會先請你輸入本伺服器 IP），螢幕出現 6 位確認碼。
2. **伺服器端**：用 admin 帳號開 `/admin`，「裝置配對申請」區塊會在幾秒內出現該台，**核對確認碼與 Pi 螢幕一致**後按「核可」。
3. Pi 自動領取 token 寫入自己的 `telemetry.json` 並立刻開始連線——管理員全程不會看到 token。

配對完成後，在管理頁按該台的「財編」登記它對應的**呼吸器財產編號**（只顯示在管理頁，供盤點與報修）。**床號不在此設定**——規劃由財編向院內系統查詢後自動帶入；在那之前看板卡片顯示機台編號，帶入後會自動改以床號為標題並依床號排序。

同一台重複配對即換發新 token（核可頁會顯示警告，舊 token 於該台下次重連時失效；床號、財編與備註都會保留）。
申請 10 分鐘未處理即失效，Pi 端可重新申請。沒有真機時可用
`python tools/fake_pi.py --pair --device pi-new` 演練這個畫面。

⚠️ **第一次核可會建立 `devices.json`**，伺服器隨即從「單一共用 `ingest_token`」切換成
「每台獨立 token」模式，原本用共用 token 的舊裝置下次重連會被拒絕——請一併配對或用下面的
`make_device.py` 補登記。另外，伺服器啟用 TLS 時 Pi 端配對客戶端連不上（只支援明文
HTTP），該情境請走下面的手動流程。

### 手動核發權杖（fallback：TLS 環境、或配對流程不可用時）

```bash
python tools/make_device.py --device pi-icu-01 --note "ICU 3床"   # 產生新 token（只顯示一次）
python tools/make_device.py --list                                # 列出裝置（不顯示 token）
python tools/make_device.py --disable --device pi-icu-01          # 懷疑外洩時先停用
```

把顯示的 token 複製到該台 Pi 的 `telemetry.json` 的 `token` 欄位。`devices.json` 一旦存在，伺服器就改用這個模式；外洩或懷疑外洩時只需停用/換發該台，不影響其他 Pi（範本見 `devices.json.example`）。

## 升級相依套件

`requirements.txt` 鎖定確切版本（不用 `>=`），避免醫院機器裝到跟開發環境不同的版本、行為不可重現。要升級時：

1. 開一個乾淨的虛擬環境（`python -m venv .venv-test`），`pip install -r requirements.txt` 改成新版本號先裝裝看
2. 跑 `python -m unittest discover -s tests -v` 與 `python tests/smoke_test.py`，全過才繼續
3. 觀察至少一晚（`tools/soak_monitor.ps1`）確認記憶體/CPU 無異常
4. 全部沒問題才更新 `requirements.txt` 的版本號、上正式機

## 換環境部署（家裡 → 醫院）

| 階段 | Pi 端 | 伺服器端 |
|---|---|---|
| 自家 Wi-Fi | telemetry.json 填筆電家用 IP | 本機跑 server.py + 防火牆放行 |
| 醫院 Wi-Fi（筆電） | 改 server_host 為筆電院內 IP | 同一台，不用動 |
| 醫院電腦 RT004 | 改 server_host 為 172.19.18.70 | 複製本資料夾過去、裝 Python + aiohttp、防火牆請資訊室協助放行 |

## 正式伺服器維運工具（IMPROVEMENT_PLAN.md Phase 2）

以下都是**給正式伺服器用**的腳本，開發用電腦不要執行。每支都支援先預覽（`-WhatIf`
或 `-DryRun`）再正式套用，用法與細節見各腳本檔頭註解：

| 腳本 | 用途 |
|---|---|
| `tools/setup_service.ps1` | 註冊 Windows 工作排程器：開機自動啟動＋失敗 1 分鐘內自動重啟，取代手動雙擊 `start_server.bat` |
| `tools/lock_permissions.ps1` | 用 `icacls` 鎖定 accounts.json/devices.json/config.json/certs/logs 等敏感檔案，只留服務帳號與管理員可讀寫 |
| `tools/backup.ps1` | 有外部備份空間時，手動備份設定/帳號/裝置權杖/憑證與審計日誌；不含 `ca.key`，也不含 alarm/sys 七天歷史 SQLite |

**時間同步**：伺服器與每一台 Pi 都要跟院內同一個時間來源同步（NTP），否則警報/審計
日誌的時間戳記不可信、事後回溯對不上。部署時請跟資訊室確認院內 NTP 伺服器位址，
Windows 端用 `w32tm /query /status` 確認已同步，Pi 端用 `timedatectl` 確認。

**院內 CA 簽發憑證（部署醫院時建議走這條）**：若院方統一由資訊室簽發憑證（多數醫院
如此，到期前會公告提醒申請），直接把核發的伺服器憑證與私鑰掛到 `tls_cert`/`tls_key`，
並向資訊室索取**院內 CA 根憑證檔**（公開檔案）——發到每台 Pi 的 `telemetry.json`
（`tls_ca`）。申請伺服器憑證時用之後大家實際連的**內部 DNS 名稱**（本院定案為
`ventmonitor.csh.org.tw`；SAN 建議一併含 IP）。此模式不需要 `make_certs.py` 與
`ca.key`，下段僅適用自建 CA。

**ca.key（CA 發證私鑰）**：`tools/make_certs.py` 產生的 `certs/ca.key` 不應該長駐伺服器
——簽完伺服器憑證後複製兩份到離線 USB，然後從伺服器上刪除；下次要重簽憑證（例如換
IP）時再暫時取回，簽完立刻再次移除。被拿走等於能簽出任何受信任憑證，風險等同外洩
CA 本身。

## 專案結構（目錄即架構，詳見 CLAUDE.md）

| 路徑 | 用途 |
|---|---|
| `main.py` | 唯一進入點（組裝三層） |
| `monitor/transport/` | 傳輸層：TCP 接收 Pi 資料 |
| `monitor/domain/` | 領域層：裝置狀態管理與廣播（擴充功能掛這層） |
| `monitor/web/` | 呈現層：HTTP/WebSocket + `static/` 儀表板前端 |
| `monitor/config.py` | 設定載入（唯一讀 config.json 的地方） |
| `tools/fake_pi.py` | Pi 模擬器（純標準庫，真 Pi 上也能跑來測網路） |
| `tests/smoke_test.py` | 一鍵端對端冒煙測試（push 前必跑） |
| `PROTOCOL.md` | Pi ↔ 伺服器 ↔ 瀏覽器的資料格式契約 |

## 疑難排解

- **儀表板一直「等待裝置連線」**：本機先跑 `tools/fake_pi.py` 排除伺服器問題；再從 Pi 上 `curl http://<伺服器IP>:8080` 測連通（防火牆/網段隔離最常見）
- **波形卡頓**：正常顯示會落後真實時間約 0.5 秒（抖動緩衝）；持續卡頓多半是 Wi-Fi 訊號差
- **此畫面僅供觀察參考，不可作為臨床警報依據**
