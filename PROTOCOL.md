# RespiraMark 遙測協議 v1

兩段傳輸，格式皆為 JSON：

```
Pi (respiramark-pi)  ──TCP 8765, JSON Lines──▶  彙整伺服器  ──WebSocket /ws──▶  瀏覽器儀表板
```

- 所有訊息都帶 `"type"` 欄位；**收到不認識的 type 一律忽略並記 log**（向前相容）。
- `hello` 帶協議版本 `"v": 1`；新增欄位不改版號，破壞性變更才進版。

## 傳輸安全（TLS）與登入

**TLS 加密（兩段皆可啟用）**：伺服器 `config.json` 設定 `tls_cert` / `tls_key`（憑證由 `tools/make_certs.py` 一鍵產生：自建 CA + 伺服器憑證）後，TCP ingest 與網頁**同時**改為加密（`https://` 與 `wss://`，port 不變）。Pi 端 `telemetry.json` 設 `"tls": true` 與 `"tls_ca"`（CA 憑證檔路徑）。

- 信任模型是 **CA 釘選**：Pi 與瀏覽器信任的是自建 CA（`ca.pem`），不是單張伺服器憑證。伺服器搬家/換 IP 時，用同一顆 CA 重簽伺服器憑證即可（`make_certs.py` 重跑一次），**Pi 端與瀏覽器端都不用動**。
- 兩端 TLS 設定必須一致：伺服器開了 TLS，未開 TLS 的 Pi 會連不上（反之亦然）。
- `ingest_token` 規則不變，與 TLS 疊加使用（TLS 管加密與伺服器身分，token 管「誰可以送資料」）。

**瀏覽器登入**（`auth_enabled`，預設啟用）：`/`、`/ws`、`/history/*`、`/api/me` 皆需登入 session（HttpOnly cookie；閒置逾時 `session_idle_minutes`，0 = 不逾時）。帳號存伺服器 `accounts.json`（**不進 git**；`tools/make_user.py` 建立，密碼僅存 PBKDF2 雜湊），角色分 `viewer`（看板）與 `admin`（含日後管理頁）。驗證為可抽換介面（`monitor/web/auth.py`），日後可改接醫院 AD/LDAP 而不動登入頁與 session 邏輯。連續登入失敗會暫時鎖定該來源 IP。

| 端點 | 說明 |
|---|---|
| `GET /login` | 登入頁（未登入存取 `/` 會被導向此頁） |
| `POST /login` | 表單欄位 `username` / `password`；成功 → 設 session cookie 並導向 `/`；失敗 → 導回 `/login?err=1`（鎖定中 `?err=lock`） |
| `POST /logout` | 登出（清除 session）並導向 `/login` |
| `GET /api/me` | 目前登入者 `{"auth":true,"username":...,"role":...}`；未登入回 401（前端以此偵測 session 過期並導回登入頁） |

**管理頁（僅 `admin` 角色）**：`/admin` 與 `/api/admin/*` 需要 admin session；viewer 存取 `/admin` 會被導回 `/`、存取 `/api/admin/*` 回 403。未登入存取 `/admin` 導向 `/login`。登入未啟用（開發模式）時不設限。

| 端點 | 說明 |
|---|---|
| `GET /admin` | 設備維護管理頁：所有裝置健康總表 + sys 趨勢圖 |
| `GET /api/admin/accounts` | 帳號唯讀清單 `{"users":[{"username":...,"role":...}]}`（不含密碼雜湊；建立/刪除仍用 `tools/make_user.py`） |
| `DELETE /api/admin/devices/{device}` | 移除**離線**裝置（清出儀表板版面；裝置重新連上會自動回來）。線上裝置回 409、未知裝置回 404 |
| `GET /api/admin/syslog/{device}` | 下載該裝置的長期 sys CSV（`sys_<裝置>.csv`）；無檔案（未啟用落地或尚無資料）回 404 |

## 第一段：Pi → 伺服器（TCP，每行一個 JSON，`\n` 結尾）

連線後第一則必須是 `hello`，否則伺服器斷線。

**存取驗證**：伺服器 `config.json` 設定了 `ingest_token`（非空字串）時，`hello` 必須帶 `token` 欄位且值相符，否則伺服器記 log 後直接斷線；伺服器未設定則忽略此欄位。裝置數達 `max_devices` 上限時，新裝置的 `hello` 一律拒絕（既有裝置重連不受影響）。未啟用 TLS 時 token 走明文 TCP，僅用於院內網隔離閒雜裝置；啟用 TLS（見「傳輸安全」）後 token 才受加密保護。

| type | 時機 | 欄位 |
|---|---|---|
| `hello` | 連線後第一則 | `v` 協議版本、`device` 機台編號（hostname）、`patient` 病人代碼、`token` 存取權杖（見上）、`ts` |
| `wave` | 每 ~150ms 一批 | `p`/`f`/`v` 等長陣列（壓力 cmH₂O、流量 L/min、容積 mL）、`trig` 觸發樣本的 index 陣列、`ts` |
| `params` | 慢數據輪詢後（~5s） | `mode` 通氣模式、`features` 附加功能、`settings` 設定值 dict、`measured` 量測值 dict、`ts` |
| `status` | 呼吸器連線狀態變化 | `state`（connected / connecting / disconnected）、`msg` 顯示文字、`ts` |
| `device_info` | 取得設備 ID 後 | `info`：`id`/`name`/`revision`/`medibus`、`ts` |
| `alarm` | 警報狀態變化時（**全量**） | `alarms` 陣列，每項 `{prio, code, cp, text}`；prio 1~31（31 最高，同 MEDIBUS）；`cp` 為來源 codepage（`1`=MEDIBUS 27H、`2`=MEDIBUS 2EH）——同一 `code` 在不同 codepage 意義不同，office 端依 `(cp, code)` 查對照表決定臨床分級與顯示全名（見「警報分級」一節）；**空陣列 = 全部解除**、`ts` |
| `sys` | Pi 自身系統狀態（每 ~5s） | 見下表，各欄皆可為 `null`（該項當下取不到）、`ts` |
| `ping` | 閒置 ≥2s 心跳 | `ts` |

註：`alarm` 全鏈已實作——Pi 端隨慢數據輪詢（MEDIBUS 27H/2EH，約每 5 秒）取得警報，內容變化時全量送出；開發 UI 亦可用 `tools/fake_pi.py --alarms` 模擬。

### 警報分級（office 端呈現）

MEDIBUS 警報碼本身不含臨床嚴重度分級，且 codepage 1（27H）與 codepage 2（2EH）的
`code` 會重複但意義不同，因此 office 端用 `(cp, code)` 查對照表，決定：

| Level | 意義 | 顏色 |
|---|---|---|
| 1 | 危及生命 | 紅 |
| 2 | 可能危及生命 | 黃 |
| 3 | 不影響生命 | 淡藍 |

對照表（`(cp, code)` → `{level, name}`）維護在 `monitor/web/static/alarm_levels.js`，
由使用者依實際連接的呼吸器型號填寫（**不在 Pi 端**：好處是修改一次、所有連線中的 Pi
立刻套用，不需要改 Pi 端程式碼或重啟呼吸器監測程式）。查無對照的警報碼 → 預設
level 2、顯示裝置原始縮寫文字（`text`）。同一裝置多筆警報依 level 由高至低排序後
顯示在同一列；只有 level 1／2 存在時卡片才標示警示外框，level 3（不影響生命）只在
列表中以淡藍顯示，不觸發卡片警示外框。

### `sys`（Pi 系統健康狀態）

Pi 端在遙測背景執行緒每 ~5s 取樣一次（純標準庫讀 `/proc`、`/sys`、`vcgencmd`、`shutil`），與呼吸器連線與否無關。每欄取不到時送 `null`（非 Linux/非 Pi 環境多數欄位為 `null`，僅磁碟可得）。

| 欄位 | 意義 | 單位 |
|---|---|---|
| `cpu` | CPU 使用率（跨核心彙整，0~100） | % |
| `mem` | 記憶體使用率 | % |
| `temp` | CPU 溫度 | °C |
| `disk_pct` | 資料分割區使用率 | % |
| `disk_free` | 資料分割區剩餘空間 | GB |
| `throttled` | Pi 過熱/欠壓旗標（`vcgencmd get_throttled`）；`"0x0"` = 正常，非 0 = 曾降頻或欠壓 | 十六進位字串 |
| `uptime` | 開機至今時長（可用於偵測重開機） | 秒 |

`sys` 是**有狀態**類型：伺服器保留每台裝置最新一則供 snapshot，並在記憶體保留近段歷史（供儀表板趨勢圖，隨 Pi 送出頻率即時更新）＋附加寫入伺服器端 CSV（長期趨勢，供 Excel 事後分析；重開伺服器不丟）。CSV 寫入依 `sys_csv_interval`（預設 60 秒）節流，避免長期運行檔案過大——**只放慢 CSV，不影響即時畫面與記憶體歷史**。CSV **只含系統指標與機台編號，絕不含病人代碼**。開發可用 `tools/fake_pi.py`（預設即模擬 `sys`）產生資料。

伺服器超過 `offline_timeout`（預設 5 秒）沒收到任何訊息 → 判定該裝置離線。

## 第二段：伺服器 → 瀏覽器（WebSocket `/ws`，每則一個 JSON）

Pi 的訊息原樣轉發，外加 `"device"` 欄位標記來源。伺服器另外產生三種：

| type | 說明 |
|---|---|
| `snapshot` | 瀏覽器剛連上時送一次：`devices` 陣列，每台含 `device`/`patient`/`online` 與各「有狀態類型」（`status`/`device_info`/`params`/`alarm`/`sys`）的最新一則 |
| `link` | Pi 與伺服器的連線狀態：`online` true/false（上線時附 `patient`）。注意這與 `status`（Pi 與呼吸器的串口狀態）是兩件事 |
| `device_removed` | 管理員從管理頁移除離線裝置時廣播：`device` 機台編號。所有觀看端（儀表板與管理頁）應移除該裝置的畫面元素 |

`sys` 亦原樣轉發（加 `device`）。趨勢圖歷史另走 HTTP（不佔 WebSocket）：

| 端點 | 說明 |
|---|---|
| `GET /history/{device}` | 回傳該裝置記憶體中的 `sys` 近段歷史：`{"device":..., "samples":[{...,"ts":...}, ...]}`。瀏覽器展開某台裝置時抓一次補齊趨勢圖，之後由即時 `sys` 續接。未知裝置回傳空 `samples`。 |

## 版本相容規則

- Pi 與伺服器版本不必同步更新：未知 type / 未知欄位皆忽略。
- `hello.v` 與伺服器支援版本不合時，伺服器仍接收，但在 log 與儀表板顯示警告。
