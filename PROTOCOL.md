# RespiraMark 遙測協議 v1

兩段傳輸，格式皆為 JSON：

```
Pi (respiramark-pi)  ──TCP 8765, JSON Lines──▶  彙整伺服器  ──WebSocket /ws──▶  瀏覽器儀表板
```

- 所有訊息都帶 `"type"` 欄位；**收到不認識的 type 一律忽略並記 log**（向前相容）。
- `hello` 帶協議版本 `"v": 1`；新增欄位不改版號，破壞性變更才進版。

## 第一段：Pi → 伺服器（TCP，每行一個 JSON，`\n` 結尾）

連線後第一則必須是 `hello`，否則伺服器斷線。

**存取驗證**：伺服器 `config.json` 設定了 `ingest_token`（非空字串）時，`hello` 必須帶 `token` 欄位且值相符，否則伺服器記 log 後直接斷線；伺服器未設定則忽略此欄位。裝置數達 `max_devices` 上限時，新裝置的 `hello` 一律拒絕（既有裝置重連不受影響）。token 走明文 TCP，僅用於院內網隔離閒雜裝置，不可視為對抗攻擊者的防線。

| type | 時機 | 欄位 |
|---|---|---|
| `hello` | 連線後第一則 | `v` 協議版本、`device` 機台編號（hostname）、`patient` 病人代碼、`token` 存取權杖（見上）、`ts` |
| `wave` | 每 ~150ms 一批 | `p`/`f`/`v` 等長陣列（壓力 cmH₂O、流量 L/min、容積 mL）、`trig` 觸發樣本的 index 陣列、`ts` |
| `params` | 慢數據輪詢後（~5s） | `mode` 通氣模式、`features` 附加功能、`settings` 設定值 dict、`measured` 量測值 dict、`ts` |
| `status` | 呼吸器連線狀態變化 | `state`（connected / connecting / disconnected）、`msg` 顯示文字、`ts` |
| `device_info` | 取得設備 ID 後 | `info`：`id`/`name`/`revision`/`medibus`、`ts` |
| `alarm` | 警報狀態變化時（**全量**） | `alarms` 陣列，每項 `{prio, code, text}`；prio 1~31（31 最高，同 MEDIBUS）；**空陣列 = 全部解除**、`ts` |
| `sys` | Pi 自身系統狀態（每 ~5s） | 見下表，各欄皆可為 `null`（該項當下取不到）、`ts` |
| `ping` | 閒置 ≥2s 心跳 | `ts` |

註：`alarm` 全鏈已實作——Pi 端隨慢數據輪詢（MEDIBUS 27H/2EH，約每 5 秒）取得警報，內容變化時全量送出；開發 UI 亦可用 `tools/fake_pi.py --alarms` 模擬。

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

Pi 的訊息原樣轉發，外加 `"device"` 欄位標記來源。伺服器另外產生兩種：

| type | 說明 |
|---|---|
| `snapshot` | 瀏覽器剛連上時送一次：`devices` 陣列，每台含 `device`/`patient`/`online` 與各「有狀態類型」（`status`/`device_info`/`params`/`alarm`/`sys`）的最新一則 |
| `link` | Pi 與伺服器的連線狀態：`online` true/false（上線時附 `patient`）。注意這與 `status`（Pi 與呼吸器的串口狀態）是兩件事 |

`sys` 亦原樣轉發（加 `device`）。趨勢圖歷史另走 HTTP（不佔 WebSocket）：

| 端點 | 說明 |
|---|---|
| `GET /history/{device}` | 回傳該裝置記憶體中的 `sys` 近段歷史：`{"device":..., "samples":[{...,"ts":...}, ...]}`。瀏覽器展開某台裝置時抓一次補齊趨勢圖，之後由即時 `sys` 續接。未知裝置回傳空 `samples`。 |

## 版本相容規則

- Pi 與伺服器版本不必同步更新：未知 type / 未知欄位皆忽略。
- `hello.v` 與伺服器支援版本不合時，伺服器仍接收，但在 log 與儀表板顯示警告。
