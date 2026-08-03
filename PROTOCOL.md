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

**瀏覽器登入**（`auth_enabled`，預設啟用）：`/`、`/ws`、`/history/*`、`/api/me` 皆需登入 session（HttpOnly cookie；閒置逾時 `session_idle_minutes`，0 = 不逾時）。角色分 `viewer`（看板）與 `admin`（含管理頁）。驗證為可抽換介面（`monitor/web/auth.py` 的 `AuthManager` 依賴注入 authenticator），由 `config.json` 的 `auth_backend` 決定：

- `"local"`（預設）：帳號存伺服器 `accounts.json`（**不進 git**；`tools/make_user.py` 建立/刪除，密碼僅存 PBKDF2 雜湊）。本系統**不提供改密碼功能**（自助改密碼與管理員重設皆無），忘記密碼須管理員在伺服器重新執行 `tools/make_user.py`。
- `"ldap"`：登入時把帳密現場交給醫院 LDAP/AD 做一次 bind 驗證，密碼完全不落地本機，改密碼請走醫院 HIS 既有流程（見 `monitor/web/ldap_auth.py` 開頭說明）。角色（viewer/admin）仍由本機 `accounts.json` 的白名單決定（`password` 欄位此模式下不會被讀取），不是由 AD 群組決定。相關設定：`ldap_server`（如 `ldaps://ad.example.org`）、`ldap_bind_template`（如 `"{username}@example.org"`）、`ldap_use_ssl`、`ldap_ca`（院內 CA 根憑證檔——設定後 ldaps 連線以 `CERT_REQUIRED` 驗證 AD 伺服器憑證，防冒充；未設定則只加密不驗證並於啟動時警告，**正式環境必填**）、`ldap_timeout`。LDAP 連不上一律視為驗證失敗（fail closed），不會退回本機密碼比對。

連續登入失敗會暫時鎖定該來源 IP（兩種 backend 皆適用）。

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
| `PUT /api/admin/devices/{device}/meta` | 設定該裝置的**床號**與**呼吸器財編**（見「裝置床號與財編」一節） |
| `GET /api/admin/syslog/{device}` | 從 SQLite 即時匯出該裝置最近 7 天的 sys CSV（`sys_<裝置>.csv`）；無紀錄回 404 |
| `GET /api/admin/alarmlog/{device}` | 從 SQLite 即時匯出該裝置最近 7 天的警報 episode CSV（`alarm_<裝置>.csv`，含開始、結束、持續秒數、狀態及警報內容）；只記機台編號與警報內容，不記病人代碼；無紀錄回 404 |
| `GET /api/admin/pair/pending` | 待核可的裝置配對清單（見「裝置配對」一節） |
| `POST /api/admin/pair/{pair_id}/approve` | 核可配對：產生 token、寫入 `devices.json`（token 不回傳給管理員） |
| `POST /api/admin/pair/{pair_id}/deny` | 拒絕配對 |

**單床詳細監測 API（viewer/admin 均可使用）**：

| 端點 | 說明 |
|---|---|
| `GET /api/alarm-history/{device}?limit=50` | 查看該裝置最近警報 episode（新到舊，最多 100 筆），回傳 `episodes`；狀態為 `active`／`cleared`／`unknown`（離線或重啟時無法確認真正解除點），供單床詳細監測畫面顯示；不含病人代碼 |

## 裝置配對（HTTP :8080，取得 `hello` 用的 token）

新裝置佈建用：**Pi 端輸入伺服器位址送出配對申請 → 管理員在 `/admin` 核可 → Pi 自動領取 token 並寫入自己的 `telemetry.json`**，全程不需要人工複製貼上 token（`tools/make_device.py` 保留為 fallback）。走 HTTP :8080，**TCP 8765 的 ingest 協議完全不動**（它是純單向串流，不回傳任何資料）。

```
Pi ──POST /api/pair/request──▶ 伺服器（產生 pair_id + 6 位確認碼）
Pi ──GET  /api/pair/poll/{pair_id}（每 3 秒）──▶ 等待核可
                管理員在 /admin 核對「Pi 螢幕上的 6 位碼」與「網頁上的 6 位碼」一致 → 核可
Pi ◀── {"status":"approved", "token": ...} 一次性領取 → 寫 telemetry.json → 以新 token 連 8765
```

- **6 位確認碼是給人核對用的，不是憑證**：它防的是「管理員誤核可到別台裝置」。真正的能力憑證是 `pair_id`（高熵隨機字串），只有發出申請的那台 Pi 知道。
- **token 只會在 poll 回應中明文出現一次**：伺服器回傳的同時就刪除整筆配對記錄，之後再 poll 一律得到 `expired`。伺服器只保留 PBKDF2 雜湊（與 `tools/make_device.py` 相同格式）。
- **重複配對 = 換發**：同一 `device_id` 已存在時，待核可清單會標示 `renew`，管理頁顯示警告。核可後舊 token 立即失效，但**既有的 TCP 連線不會被中斷**（`hello` 當時已驗證通過），該裝置下次重連時才會用到新 token。
- 配對狀態只存在伺服器記憶體（不落地），伺服器重啟時全部失效，Pi 端顯示「配對已失效，請重新配對」。
- ⚠️ **第一次核可會建立 `devices.json`**，伺服器隨即從「單一共用 `ingest_token`」切換成「每台獨立 token」模式（見下方「存取驗證」）。原本靠共用 token 連線的裝置**下次重連時會被拒絕**，必須逐台配對或用 `tools/make_device.py` 補登記。已在連線中的裝置不受影響（`hello` 當時已驗證通過）。
- **TLS 啟用時 Pi 配不了對**：伺服器端點照常運作，但目前 Pi 端的配對客戶端只講明文 HTTP（純標準庫，也還沒有院內 CA 憑證），連不上 HTTPS。伺服器啟動時會記一則警告，此情境的新裝置請改用 `tools/make_device.py` 手動核發。
- `config.json` 的 `pair_enabled` 設為 `false` 時，以下所有端點都不註冊（回 404）。

| 端點 | 權限 | 說明 |
|---|---|---|
| `POST /api/pair/request` | 免登入 | Pi 送出配對申請 |
| `GET /api/pair/poll/{pair_id}` | 免登入 | Pi 查詢結果並領取 token |
| `GET /api/admin/pair/pending` | admin | 待核可清單 |
| `POST /api/admin/pair/{pair_id}/approve` | admin | 核可 |
| `POST /api/admin/pair/{pair_id}/deny` | admin | 拒絕 |

### `POST /api/pair/request`

請求：`{"device_id": "raspberrypi-01", "note": "ICU 3床"}`
`device_id` 必填，格式 `^[A-Za-z0-9._-]{1,64}$`（Pi 端預設送 hostname）；`note` 選填，上限 64 字。

回應 200：

```json
{"pair_id": "9cV...（隨機字串）", "code": "123456", "expires_in": 600, "poll_interval": 3}
```

- 400 `{"error": "..."}`：JSON 格式錯誤或 `device_id` 不合格式。
- 429 `{"error": "配對請求已達上限，請稍後再試"}`：待核可筆數已達 `pair_max_pending`。
- 同一來源 IP 已有待核可申請時，**舊申請直接作廢**、換發新的（仍回 200），避免有人連按累積佔位。

### `GET /api/pair/poll/{pair_id}`

一律回 200，以 `status` 表示狀態：

| status | 回應 | 意義 |
|---|---|---|
| `pending` | `{"status": "pending"}` | 等待管理員處理 |
| `approved` | `{"status": "approved", "device_id": ..., "token": "...", "server_port": 8765}` | **僅出現一次**，Pi 應立刻寫入設定 |
| `denied` | `{"status": "denied"}` | 管理員拒絕（可重複查到，直到 TTL 過期） |
| `expired` | `{"status": "expired"}` | 逾時、已領取、`pair_id` 不存在、或伺服器重啟過 |

未知 `pair_id` 與過期回應相同（不區分），避免洩漏某筆配對是否存在。

### `GET /api/admin/pair/pending`

```json
{"pending": [{"pair_id": "...", "device_id": "raspberrypi-01", "code": "123456",
              "ip": "172.19.18.55", "note": "ICU 3床", "renew": true,
              "age_s": 42.0, "expires_in": 558.0}]}
```

`renew` 為 `true` 表示 `devices.json` 已有同名裝置，核可即換發。此端點供管理頁定期輪詢，**不寫審計日誌**。

### `POST /api/admin/pair/{pair_id}/approve` / `deny`

- approve 200：`{"approved": {"device_id": "...", "renew": false}}`——**token 不回傳給管理員**，只留給該台 Pi 領取。
- deny 200：`{"denied": {"device_id": "..."}}`。
- 404 `{"error": "配對不存在或已過期"}`；409 `{"error": "此配對已處理"}`（另一位管理員已處理）；approve 另有 500 `{"error": "devices.json 寫入失敗"}`（此時該筆維持待核可，可直接重試）。

已核可但 Pi 一直沒來領取時，該筆在 TTL 後失效，但 `devices.json` 中的裝置項目會留著（無人知道其 token，形同停用）；重新配對即覆蓋換發。

## 裝置床號與財編

儀表板卡片以**床號**為主要標題，**呼吸器財編**供盤點與報修對應實體機器。兩者都**只存在伺服器**（`devices.json` 中該裝置的項目）——Pi 端完全不參與，也不需要知道自己在哪一床。

- **財編**由管理員在 `/admin` 登記，是唯一需要人工填的欄位。
- **床號目前不開放人工設定**：規劃由財編向院內系統（HIS／資產系統）查詢後自動帶入，屆時由伺服器端寫入同一個欄位。在自動帶入實作之前，床號一律是空字串，儀表板顯示機台編號。管理 API 仍接受 `bed`（供自動帶入使用），但管理頁不提供這個輸入欄位——多一個人工來源只會多一份會過期的資料。
- 一台呼吸器配一台 Pi，呼吸器隨病人移動，Pi 也跟著走；因此「機台編號 → 財編」是穩定的綁定。
- 床號只用於**顯示與排序**，不涉及病人身分：病人代碼仍由床邊輸入（見 `hello` 的 `patient`）。
- 尚未指定床號的裝置，儀表板退回顯示機台編號（以較淡的樣式標示未指定），排序時排在已指定床號的裝置之後。
- ⚠️ 只有**已登記在 `devices.json` 的裝置**才能設定床號與財編。若伺服器目前是「單一共用 `ingest_token`」模式（`devices.json` 不存在），設定會被拒絕（回 409）——否則會憑空建出該檔案、把伺服器切換成逐台驗證模式，導致所有既有裝置重連被拒。此時請先完成裝置配對。

`devices.json` 中每台裝置的欄位：

| 欄位 | 說明 |
|---|---|
| `device_id` | 機台編號（Pi 的 hostname），`hello` 用來識別 |
| `token_hash` | 存取權杖的 PBKDF2 雜湊 |
| `enabled` | 是否啟用；false 時該裝置一律拒絕連線 |
| `note` | 管理員備註（自由文字） |
| `bed` | 床號，例 `RCC-01`；空字串 = 未帶入（目前一律為空，見上） |
| `asset` | 呼吸器財編；空字串 = 未設定 |

### `PUT /api/admin/devices/{device}/meta`

請求：`{"bed": "RCC-01", "asset": "A-123456"}`
兩個欄位都選填（省略者保留原值，傳空字串則清除），上限各 32 字元。

- 200：`{"device": ..., "bed": ..., "asset": ...}`，同時對所有觀看端廣播 `device_meta`。
- 404 `{"error": "裝置未登記"}`：`devices.json` 中沒有這台（尚未配對）。
- 409 `{"error": "..."}`：伺服器尚未啟用逐台裝置驗證（見上方警告）。
- 500 `{"error": "devices.json 寫入失敗"}`。

## 第一段：Pi → 伺服器（TCP，每行一個 JSON，`\n` 結尾）

連線後第一則必須是 `hello`，否則伺服器斷線。

**存取驗證**：伺服器目錄下 `devices.json` 存在時，優先採用**每台裝置獨立 token**模式——`hello` 的 `device` 必須是該檔案中已登記且未停用的裝置，`token` 需與該裝置的雜湊相符（由「裝置配對」流程核發，或 `tools/make_device.py` 手動建立/換發；外洩或懷疑外洩時只需停用/換發該台，不影響其他裝置）。`devices.json` 不存在時退回**單一共用 token** 模式：`config.json` 設定了 `ingest_token`（非空字串）時，`hello` 必須帶 `token` 欄位且值相符。兩種模式驗證失敗都是記 log 後直接斷線，且不透露失敗原因（裝置不存在／被停用／token 錯誤皆同一句訊息）；伺服器兩者皆未設定則不驗證（僅限開發環境）。裝置數達 `max_devices` 上限時，新裝置的 `hello` 一律拒絕（既有裝置重連不受影響）。未啟用 TLS 時 token 走明文 TCP，僅用於院內網隔離閒雜裝置；啟用 TLS（見「傳輸安全」）後 token 才受加密保護。

**連線防護**（防範區網內異常/惡意連線耗盡資源）：同時 TCP 連線數超過 `ingest_max_conns`（預設 64）直接拒絕新連線；連線後 `ingest_hello_timeout`（預設 10 秒）內沒收到合法 `hello` 就斷線；`hello` 通過後 `ingest_idle_timeout`（預設 60 秒）內沒收到任何訊息也斷線（Pi 端本來就每 2 秒送 `ping`，此值留有充裕餘裕）。單行訊息上限 `MAX_LINE`（64KB）；`wave` 的 `p`/`f`/`v`/`trig` 需為等長數值陣列且不超過 2000 筆、`params` 的 `settings`/`measured` 鍵數不超過 200 且字串值不超過 200 字元，格式異常的訊息記 log 後直接捨棄（不斷線，視為裝置端偶發問題）。

| type | 時機 | 欄位 |
|---|---|---|
| `hello` | 連線後第一則 | `v` 協議版本、`device` 機台編號（hostname）、`patient` 病人代碼、`token` 存取權杖（見上）、`ts` |
| `wave` | 每 ~150ms 一批 | `p`/`f`/`v` 等長陣列（壓力 cmH₂O、流量 L/min、容積 mL）、`trig` 觸發樣本的 index 陣列、`ts` |
| `params` | 慢數據輪詢後（~5s） | `mode` 通氣模式、`features` 附加功能、`settings` 設定值 dict、`measured` 量測值 dict、`ts` |
| `status` | 呼吸器連線狀態變化 | `state`（connected / connecting / disconnected）、`msg` 顯示文字、`ts` |
| `device_info` | 取得設備 ID 後 | `info`：`id`/`name`/`revision`/`medibus`、`ts` |
| `alarm` | 警報狀態變化時（**全量**） | `alarms` 陣列，每項 `{prio, code, cp, text}`；prio 1~31（31 最高，同 MEDIBUS），office 端依 `prio` 範圍判級；`cp` 為來源 codepage（`1`=MEDIBUS 27H、`2`=MEDIBUS 2EH）——同一 `code` 在不同 codepage 意義不同，因此顯示全名仍依 `(cp, code)` 查表（見「警報分級」一節）；**空陣列 = 全部解除**、`ts` |
| `sys` | Pi 自身系統狀態（每 ~5s） | 見下表，各欄皆可為 `null`（該項當下取不到）、`ts` |
| `ping` | 閒置 ≥2s 心跳 | `ts` |

註：`alarm` 全鏈已實作——Pi 端隨慢數據輪詢（MEDIBUS 27H/2EH，約每 5 秒）取得警報，內容變化時全量送出；開發 UI 亦可用 `tools/fake_pi.py --alarms` 模擬。

### 警報分級（office 端呈現）

MEDIBUS 警報碼本身不固定代表臨床嚴重度；同一警報碼在不同 responder 上可能使用不同
優先值。office 端應以每次警報隨附的 `prio` 判級：

| Level | 意義 | 顏色 |
|---|---|---|
| 1 | 危及生命 | 紅 |
| 2 | 可能危及生命 | 黃 |
| 3 | 不影響生命 | 淡藍 |

| MEDIBUS `prio` | Office Level | MEDIBUS.X urgency |
|---|---|---|
| 25～31 | 1 | High |
| 11～24 | 2 | Medium |
| 1～10 | 3 | Low |

codepage 1（27H）與 codepage 2（2EH）的 `code` 會重複但意義不同，因此完整名稱仍以
`(cp, code)` 查 `monitor/web/static/alarm_levels.js` 的對照表。表內 `level` 只供舊歷史資料
缺少 `prio`、或收到超出 1～31 的異常值時備援；查無代碼且無有效 `prio` 時預設 level 2，
名稱則退回裝置原始縮寫文字（`text`）。同一裝置多筆警報依 level 由高至低排序，同級再依
`prio` 由高至低排序；level 1／2／3 都會套用各自顏色的卡片外框與警報列。

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

`sys` 是**有狀態**類型：伺服器保留每台裝置最新一則供 snapshot，並在記憶體保留近段歷史（供儀表板趨勢圖，隨 Pi 送出頻率即時更新）＋寫入 `logs/sys_logs/sys_history.sqlite3`（重開伺服器不丟，保存 7 天）。SQLite 寫入依 `sys_persist_interval`（預設 60 秒）節流，避免長期資料量過大——**只放慢落地，不影響即時畫面與記憶體歷史**。管理員下載時才從 SQLite 即時產生 CSV，伺服器端不保留匯出檔；資料只含系統指標與機台編號，絕不含病人代碼。開發可用 `tools/fake_pi.py`（預設即模擬 `sys`）產生資料。

伺服器超過 `offline_timeout`（預設 5 秒）沒收到任何訊息 → 判定該裝置離線。

## 第二段：伺服器 → 瀏覽器（WebSocket `/ws`，每則一個 JSON）

請求帶 `Origin` 標頭且與本站不同（跨站 WebSocket 挾持）時回 HTTP 403 拒絕；沒有 `Origin` 標頭則放行（相容非瀏覽器客戶端）。同時觀看端（WebSocket 連線）數超過 `max_viewers`（預設 50）時，新連線回 HTTP 503 拒絕。

Pi 的訊息原樣轉發，外加 `"device"` 欄位標記來源。伺服器另外產生三種：

| type | 說明 |
|---|---|
| `snapshot` | 瀏覽器剛連上時送一次：`devices` 陣列，每台含 `device`/`patient`/`online`/`bed`/`asset` 與各「有狀態類型」（`status`/`device_info`/`params`/`alarm`/`sys`）的最新一則 |
| `link` | Pi 與伺服器的連線狀態：`online` true/false（上線時附 `patient`）。注意這與 `status`（Pi 與呼吸器的串口狀態）是兩件事 |
| `device_removed` | 管理員從管理頁移除離線裝置時廣播：`device` 機台編號。所有觀看端（儀表板與管理頁）應移除該裝置的畫面元素 |
| `device_meta` | 管理員改了床號／財編時廣播：`device`、`bed`、`asset`。儀表板據此即時更新卡片標題與排序，不需重新整理 |

`sys` 亦原樣轉發（加 `device`）。趨勢圖歷史另走 HTTP（不佔 WebSocket）：

| 端點 | 說明 |
|---|---|
| `GET /history/{device}` | 回傳該裝置記憶體中的 `sys` 近段歷史：`{"device":..., "samples":[{...,"ts":...}, ...]}`。瀏覽器展開某台裝置時抓一次補齊趨勢圖，之後由即時 `sys` 續接。未知裝置回傳空 `samples`。 |

## 版本相容規則

- Pi 與伺服器版本不必同步更新：未知 type / 未知欄位皆忽略。
- `hello.v` 與伺服器支援版本不合時，伺服器仍接收，但在 log 與儀表板顯示警告。
