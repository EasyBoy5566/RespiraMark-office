# RespiraMark 遙測協議 v1

兩段傳輸，格式皆為 JSON：

```
Pi (respiramark-pi)  ──TCP 8765, JSON Lines──▶  彙整伺服器  ──WebSocket /ws──▶  瀏覽器儀表板
```

- 所有訊息都帶 `"type"` 欄位；**收到不認識的 type 一律忽略並記 log**（向前相容）。
- `hello` 帶協議版本 `"v": 1`；新增欄位不改版號，破壞性變更才進版。

## 第一段：Pi → 伺服器（TCP，每行一個 JSON，`\n` 結尾）

連線後第一則必須是 `hello`，否則伺服器斷線。

| type | 時機 | 欄位 |
|---|---|---|
| `hello` | 連線後第一則 | `v` 協議版本、`device` 機台編號（hostname）、`patient` 病人代碼、`ts` |
| `wave` | 每 ~150ms 一批 | `p`/`f`/`v` 等長陣列（壓力 cmH₂O、流量 L/min、容積 mL）、`trig` 觸發樣本的 index 陣列、`ts` |
| `params` | 慢數據輪詢後（~5s） | `mode` 通氣模式、`features` 附加功能、`settings` 設定值 dict、`measured` 量測值 dict、`ts` |
| `status` | 呼吸器連線狀態變化 | `state`（connected / connecting / disconnected）、`msg` 顯示文字、`ts` |
| `device_info` | 取得設備 ID 後 | `info`：`id`/`name`/`revision`/`medibus`、`ts` |
| `ping` | 閒置 ≥2s 心跳 | `ts` |

伺服器超過 `offline_timeout`（預設 5 秒）沒收到任何訊息 → 判定該裝置離線。

## 第二段：伺服器 → 瀏覽器（WebSocket `/ws`，每則一個 JSON）

Pi 的訊息原樣轉發，外加 `"device"` 欄位標記來源。伺服器另外產生兩種：

| type | 說明 |
|---|---|
| `snapshot` | 瀏覽器剛連上時送一次：`devices` 陣列，每台含 `device`/`patient`/`online` 與最新的 `status`/`device_info`/`params` |
| `link` | Pi 與伺服器的連線狀態：`online` true/false（上線時附 `patient`）。注意這與 `status`（Pi 與呼吸器的串口狀態）是兩件事 |

## 版本相容規則

- Pi 與伺服器版本不必同步更新：未知 type / 未知欄位皆忽略。
- `hello.v` 與伺服器支援版本不合時，伺服器仍接收，但在 log 與儀表板顯示警告。
