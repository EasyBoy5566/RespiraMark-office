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
python tools/fake_pi.py --device pi-01 --patient TEST001
python tools/fake_pi.py --device pi-02 --patient TEST002 --rr 22
python tools/fake_pi.py --device pi-03 --patient TEST003 --alarms   # 含模擬警報
```

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

## 設定

複製 `config.json.example` 為 `config.json` 修改（沒有此檔則用預設值）：

| 欄位 | 預設 | 說明 |
|---|---|---|
| `ingest_port` | 8765 | Pi 連入的 TCP port |
| `web_port` | 8080 | 瀏覽器網頁 port |
| `offline_timeout` | 5.0 | 幾秒沒資料判定 Pi 離線 |
| `ingest_token` | （空） | Pi 連入的存取權杖；設定後 Pi 端 telemetry.json 的 `token` 必須一致才能連入。空字串 = 不驗證（僅限開發環境，**部署前務必設定**） |
| `max_devices` | 16 | 裝置數上限，超過即拒絕新裝置 |

## 換環境部署（家裡 → 醫院）

| 階段 | Pi 端 | 伺服器端 |
|---|---|---|
| 自家 Wi-Fi | telemetry.json 填筆電家用 IP | 本機跑 server.py + 防火牆放行 |
| 醫院 Wi-Fi（筆電） | 改 server_host 為筆電院內 IP | 同一台，不用動 |
| 醫院電腦 RT004 | 改 server_host 為 172.19.18.70 | 複製本資料夾過去、裝 Python + aiohttp、防火牆請資訊室協助放行 |

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

- **儀表板一直「等待裝置連線」**：先跑 `tools/fake_pi.py` 排除伺服器問題；再從 Pi 上 `curl http://<伺服器IP>:8080` 測連通（防火牆/網段隔離最常見）
- **波形卡頓**：正常顯示會落後真實時間約 0.5 秒（抖動緩衝）；持續卡頓多半是 Wi-Fi 訊號差
- **此畫面僅供觀察參考，不可作為臨床警報依據**
