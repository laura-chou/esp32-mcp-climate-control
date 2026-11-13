# esp32-mcp-climate-control

ESP32 + MCP + Ollama 本機氣候控制系統。

本 repo 目前實作了架構圖中 **「本機電腦（PC）傳輸層」** 的部分：
FastAPI 伺服器（Port 8801），負責接收 ESP32 上報的**多模態空間感知**資料
（DHT11 溫溼度 + PIR 人體動態 + MQ-135 空氣品質）、
以 SSE 即時推播給 MCP 核心 / `climate_agent.py`，並提供風扇指令的下行通道。

```
ESP32 ──HTTP POST /api/climate──▶ FastAPI ──SSE /api/stream──▶ MCP / climate_agent
   ▲                                                                  │
   └────GET /api/fan/command (輪詢 / long-poll)◀──POST /api/fan/command┘
```

## 專案結構

```
.
├── main.py                     # 進入點：python main.py（監聽 0.0.0.0:8801）
├── climate_agent.py            # 環境代理人：訂閱 SSE →（規則決策 + Ollama 顧問）→ 回推指令
├── requirements.txt
├── pytest.ini
├── server/
│   ├── app.py                  # FastAPI 應用與所有路由
│   ├── config.py               # 環境變數設定（Settings）
│   ├── models.py               # Pydantic 資料模型（讀值 / 風扇指令）
│   ├── state.py                # ClimateHub：狀態儲存 + SSE 事件匯流排
│   └── rules.py                # 規則式風扇決策（AI 代理人失效時的後備）
├── examples/
│   ├── esp32_simulator.py      # 模擬 ESP32：上報資料 + 輪詢指令
│   └── mcp_agent_consumer.py   # 精簡版 MCP 消費者（佔位決策，示範用）
├── esp32/                      # ⚠️ ESP32 端 MicroPython 韌體（放到開發板上）
│   ├── main.py                 # 讀 DHT11 + PIR + MQ-135 → 上報 → 輪詢指令 → 控制繼電器
│   └── config.py               # WiFi、PC IP、腳位設定（要先編輯）
└── tests/
    ├── test_transport.py       # pytest 測試（含真實 SSE 串流）
    └── test_climate_agent.py   # 代理人決策 / 解析 / 後備 / 防抖測試
```

## 安裝與執行

```bash
python -m venv .venv && source .venv/bin/activate   # 選用
pip install -r requirements.txt

# 啟動伺服器（Port 8801）
python main.py
# 或： uvicorn server.app:app --host 0.0.0.0 --port 8801
```

互動式 API 文件： <http://127.0.0.1:8801/docs>

## API 一覽

| 方法 | 路徑 | 用途 |
| --- | --- | --- |
| `POST` | `/api/climate` | ESP32 上報溫溼度（JSON） |
| `GET`  | `/api/stream` | SSE 串流，推播 `reading` / `fan_command` 事件 |
| `GET`  | `/api/fan/command` | ESP32 取得風扇指令（`wait=1` 為 long-poll） |
| `POST` | `/api/fan/command` | MCP / `climate_agent.py` 下達風扇指令 |
| `POST` | `/api/fan/auto` | 把裝置交還規則引擎（解除代理人接管） |
| `GET`  | `/api/health` | 健康檢查 |
| `GET`  | `/api/status` | 系統快照（裝置、最新讀值、風扇狀態） |

### 1. 上報資料 — `POST /api/climate`

```bash
curl -X POST http://127.0.0.1:8801/api/climate \
  -H 'Content-Type: application/json' \
  -d '{"device_id":"esp32-climate-01","dht11_temperature":31.2,"dht11_humidity":55.0,"pir_motion":true,"mq_triggered":false}'
```

回應會附帶目前應套用的風扇指令，ESP32 可在同一次往返就取得結果：

```json
{
  "status": "ok",
  "reading": {"device_id": "esp32-climate-01", "dht11_temperature": 31.2, "pir_motion": true, "mq_triggered": false, "...": "..."},
  "fan_command": {"command": "ON", "reason": "temperature crossed threshold (T=31.2C, H=55.0%)", "source": "rule-engine", "seq": 1}
}
```

`dht11_temperature`（0~50 °C）與 `dht11_humidity`（20~90 %，DHT11 實體量測範圍）超出範圍會回 `422`。
`device_id` 可省略，預設為 `esp32-climate-01`。

**多模態欄位**（皆可省略，預設 `false`，因此舊韌體仍相容）：

| 欄位 | 型別 | 說明 |
| --- | --- | --- |
| `pir_motion` | bool | PIR 偵測到人體動態 |
| `mq_triggered` | bool | MQ-135 空氣品質 DO 已觸發（超過門檻） |

**規則引擎的多模態決策優先序**（`server/rules.py`）：

1. **空氣品質優先**：`mq_triggered=true` → 一律開風扇通風（即使涼爽、無人）。
2. **舒適帶（含遲滯）**：`T ≥ 28 °C` 或 `H ≥ 70 %` 開；`T ≤ 26 °C` 且 `H ≤ 65 %` 才關。
3. **PIR 節能**：無人（`pir_motion=false`）且已在舒適帶 → 傾向關閉省電；
   但「過熱／過濕」與「空氣品質」仍會優先開啟。

### 2. SSE 串流 — `GET /api/stream`

MCP 核心與 `climate_agent.py` 訂閱此端點。連線後會先收到一筆 `snapshot`（目前狀態），
之後每當有新讀值或新指令就即時推播：

```
event: snapshot
data: {"seq": 3, "readings": {...}}

event: reading
data: {"device_id": "esp32-climate-01", "dht11_temperature": 30.0, "dht11_humidity": 75.0, "pir_motion": true, "mq_triggered": false, "...": "..."}

event: fan_command
data: {"device_id": "esp32-climate-01", "command": "ON", "reason": "...", "source": "climate_agent", "seq": 4}
```

```bash
curl -N http://127.0.0.1:8801/api/stream
```

伺服器會定期送出 keep-alive 註解訊框，避免代理伺服器逾時斷線。

### 3. 風扇指令通道 — `GET /api/fan/command`

ESP32 兩種取用方式都支援：

```bash
# (a) 單純輪詢：立即回傳目前指令
curl "http://127.0.0.1:8801/api/fan/command?device_id=esp32-climate-01"

# (b) long-poll：若沒有比 since_seq 更新的指令，最多等待 25 秒
curl "http://127.0.0.1:8801/api/fan/command?device_id=esp32-climate-01&since_seq=3&wait=1"
```

建議 firmware 記錄上次收到的 `seq`，下次帶入 `since_seq`，就能在指令變更時立即被喚醒。

### 4. 代理人下達指令 — `POST /api/fan/command`

由 MCP / `climate_agent.py`（Ollama 推理後）呼叫：

```bash
curl -X POST http://127.0.0.1:8801/api/fan/command \
  -H 'Content-Type: application/json' \
  -d '{"command":"ON","reason":"agent: T=31C H=72% -> cool/dry","device_id":"esp32-climate-01"}'
```

`device_id` 省略時會廣播給所有已知裝置。代理人下達指令後，規則引擎會暫時退讓
（預設 60 秒，`AGENT_OVERRIDE_TTL_S`），避免兩者互相搶控；可用
`POST /api/fan/auto?device_id=...` 立即交還控制權。

## ESP32 端（MicroPython）

`esp32/` 是要放到開發板上的韌體，**不是**在電腦上跑的。

### 硬體接線

| 元件 | ESP32 腳位 |
| --- | --- |
| DHT11 資料腳 | GPIO21 |
| PIR 人體感測（數位輸出） | GPIO14 |
| MQ-135 空氣品質 DO（數位輸出） | GPIO27 |
| 繼電器 INL（左） | GPIO22 |
| 繼電器 INR（右） | GPIO23 |

兩個繼電器由同一道指令同步開關。多數廉價繼電器板是**低電位觸發**（LOW = 開）；
若你的板子是**高電位觸發**，把 `config.py` 的 `RELAY_ACTIVE_LOW` 改成 `False`。

PIR 模組多為**高電位觸發**（有動態 = HIGH，`PIR_ACTIVE_HIGH = True`）；
MQ-135 模組的 DO 多為**低電位觸發**（超過門檻 = LOW，`MQ_ACTIVE_LOW = True`）。
兩者都可在 `esp32/config.py` 調整，韌體會依設定轉成邏輯值後再上報
（`pir_motion`、`mq_triggered` 一律是「事件發生 = `true`」）。

### 部署步驟

1. 編輯 `esp32/config.py`：
   - `WIFI_SSID` / `WIFI_PASSWORD`
   - `SERVER_HOST` 改成**跑 FastAPI 那台電腦的區網 IP**（例如 `192.168.0.100`）
   - 若伺服器有設 `CLIMATE_AUTH_TOKEN`，把相同值填進 `DEVICE_TOKEN`

2. 把兩個檔案上傳到 ESP32（以 `mpremote` 為例）：
   ```bash
   mpremote connect /dev/ttyUSB0 fs cp esp32/config.py :config.py
   mpremote connect /dev/ttyUSB0 fs cp esp32/main.py   :main.py
   mpremote connect /dev/ttyUSB0 reset
   ```

3. ESP32 開機後會自動執行：連 Wi-Fi → 每 5 秒讀 DHT11 + PIR + MQ-135 並 `POST /api/climate` →
   依回應與 `GET /api/fan/command` 的指令切換繼電器。

> ⚠️ `SERVER_HOST` 不能填 `192.168.0.0` —— 那是網段位址，不是可連線的主機。
> 要用電腦的實際 IP（Windows 用 `ipconfig`，Linux/macOS 用 `ip addr` 查）。

### 讀值防呆（DHT11）

DHT11 的實體量測範圍為 **溫度 0~50 °C、濕度 20~90 %**。上電初期、線路鬆脫或
讀取時序干擾常會產生範圍外的亂碼；韌體在 `read_sensor()` 會先過濾（`reading_is_valid()`），
超出範圍或 `NaN` 的讀值直接丟棄、重試，**不會上報**，因此也不會在後端觸發 `422`。
範圍常數定義在 `esp32/config.py`（`DHT11_TEMP_MIN_C` 等），與 `server/models.py`
的驗證範圍一致。

### 通訊協定（與 PC 端一致）

上報 body：
```json
{"device_id": "esp32-climate-01", "dht11_temperature": 31.2, "dht11_humidity": 55.0, "pir_motion": true, "mq_triggered": false}
```
取得指令：`GET /api/fan/command?device_id=...&since_seq=...` → `{"command": "ON"|"OFF", ...}`

## 端到端試跑

開三個終端機：

```bash
python main.py                         # 1. 伺服器
python climate_agent.py                # 2. 環境代理人（需先啟動 Ollama）
python examples/esp32_simulator.py     # 3. 模擬 ESP32 上報
```

沒有 Ollama 時，可用 `examples/mcp_agent_consumer.py` 取代第 2 步（佔位決策）。

## 環境代理人（climate_agent.py）

對應架構圖的 `climate_agent.py`。它訂閱 `/api/stream`，每收到一筆 `reading`
就依 `--mode` 決定風扇 ON/OFF，再 `POST /api/fan/command` 回推：

```
/api/stream ──reading──▶ climate_agent ──prompt──▶ Ollama
                              │
                              └──POST /api/fan/command──▶ FastAPI ──▶ ESP32
```

```bash
# 前置：Ollama 執行中，且已拉取模型
ollama serve &
ollama pull qwen2.5-coder:3b

python climate_agent.py --model qwen2.5-coder:3b
```

### 為什麼預設不是「讓模型決定」？

小模型（如 `qwen2.5-coder:3b`）**不擅長精確的數值比較**，會出現兩種典型症狀：

1. **理由幻覺**：讀值明明是 T=28.0 / H=68.0，卻生成「humidity is below 65%」
   這種與數據矛盾的說明——因為 LLM 是在「生成聽起來合理的句子」，不是真的在比大小。
2. **邊界震盪**：28.0 °C 剛好卡在 `temp_on_c=28`，模型每次取樣都可能翻面，且
   本身沒有遲滯（hysteresis），於是上一秒 ON、下一秒 OFF。

因此本專案把**「決策」與「說明」分開**：

- **決策**交給確定性的 `server/rules.py`（含遲滯，永不與數字矛盾）。
- **理由**一律由程式依真實讀值生成（`Agent._describe`），模型寫的自由文字
  **絕不會**成為正式理由。

### 三種模式（`--mode`）

| 模式 | 誰決定 ON/OFF | 是否呼叫模型 | 適用 |
| --- | --- | --- | --- |
| `advisor`（預設） | 規則引擎 | 是（僅顧問，記錄建議與是否同意） | 小模型；保留 LLM 觀察性 |
| `rules` | 規則引擎 | 否 | 最穩定、零延遲；不想依賴 LLM |
| `llm` | 模型 | 是 | 換成能力較強的模型時 |

```bash
python climate_agent.py --mode advisor   # 預設：規則決策 + 模型顧問
python climate_agent.py --mode rules     # 純規則，不呼叫模型
python climate_agent.py --mode llm --model qwen2.5:14b   # 讓較強模型決策
```

`advisor` 模式的輸出會同時顯示模型建議與是否同意，方便觀察：

```
[DECIDE] esp32-climate-01 T=28.0 H=68.0 motion=True mq=False -> ON (rule-engine: temperature crossed threshold (T=28.0C, H=68.0%)) | llm DISAGREES: OFF (temperature is above comfort range)
```

### 多模態決策

代理人的提示詞與決策都吃三個維度（`SYSTEM_PROMPT` + `build_messages`）：

1. **舒適帶**：24–27 °C、40–60 %。
2. **空氣品質（MQ-135）**：`mq_triggered=true` 時優先開風扇通風。
3. **佔用／節能（PIR）**：`pir_motion=false`（無人）時傾向關閉省電，
   除非明顯過熱／過濕或空氣品質差。

`build_messages()` 會把 `pir_motion` 與 `mq_triggered` 帶進 user prompt。

> ⚠️ **MQ-135 安全聯鎖**：`mq_triggered=true` 是**安全事件**，在**所有模式**
> （含 `llm`）都由規則引擎直接強制開風扇通風，模型與舒適帶都不能推翻它。

常用參數：

| 參數 | 預設 | 說明 |
| --- | --- | --- |
| `--base` | `http://127.0.0.1:8801` | 傳輸層位址 |
| `--ollama` | `http://127.0.0.1:11434` | Ollama 位址 |
| `--model` | `qwen2.5-coder:3b` | 模型名稱 |
| `--mode` | `advisor` | `advisor` / `rules` / `llm` |
| `--token` | 無 | 對應伺服器的 `CLIMATE_AUTH_TOKEN` |
| `--min-switch-seconds` | `15` | 非緊急情況下兩次切換的最短間隔（防抖） |
| `--dry-run` | 關 | 只印決策、不推送指令 |

**設計重點**

- **決策與說明分離**：`rules` / `advisor` 模式由 `server/rules.py` 決策；
  正式 `reason` 由程式依真實數字生成，杜絕「理由與數據矛盾」。
- **多模態**：`pir_motion` 帶入節能判斷、`mq_triggered` 帶入通風判斷；
  正式理由也會反映這些來源（例如 `MQ-135 air quality threshold exceeded`）。
- **MQ-135 安全聯鎖**：`mq_triggered=true` 在所有模式都由規則引擎強制通風，
  且被視為緊急事件，不受防抖護欄阻擋。
- **模型僅顧問**：`advisor` 仍呼叫模型並記錄其建議（含 `agrees` / `DISAGREES`），
  但模型的 command 不會成為正式指令。
- **`llm` 模式的雙重保險**：即使讓模型決策，正式理由仍由程式生成；解析失敗或
  連線錯誤時自動退回規則引擎。
- **防抖護欄**：除非讀值明顯偏離舒適帶（緊急，含 MQ 觸發），否則兩次切換需間隔
  `--min-switch-seconds`，避免抖動造成繼電器狂切。
- **斷線重連**：SSE 串流中斷時以指數退避自動重連。

## 結構化日誌

伺服器使用標準 `logging`（logger 名稱 `climate.transport`、`climate.hub`、
`climate.main`），啟動時即可從終端機追蹤資料流。日誌等級可用環境變數
`CLIMATE_LOG_LEVEL`（預設 `INFO`）調整。

| 事件 | 等級 | 內容 |
| --- | --- | --- |
| `POST /api/climate` | INFO | 來源 IP、`device_id`、溫溼度、控制模式（`auto` / `agent`）、最終風扇指令與來源 |
| 驗證失敗 `422` | WARNING | 逐欄列出錯誤欄位、輸入值、訊息與來源 IP（結構化 `detail`） |
| 未授權 `401` | WARNING | 來源 IP、方法、路徑、token 是缺漏或無效 |
| 網域錯誤 `ValueError` | ERROR | 來源 IP、方法、路徑與錯誤訊息 |
| `GET /api/stream` | INFO | 訂閱者連線建立 / 斷線，以及目前線上訂閱者總數 |
| `GET /api/fan/command` | INFO | 輪詢 / long-poll 結果（含逾時與被喚醒事件） |
| `POST /api/fan/command` | INFO | 來源 IP、目標裝置、指令、`source`、`reason` |
| `POST /api/fan/auto` | INFO | 重置裝置與是否原本為代理人控制 |
| 規則引擎決策 | INFO | 讀值、決策與原因；代理人覆寫期間會記錄「跳過規則引擎」 |

`422` 回應同時改為結構化格式，方便 ESP32 端或測試直接讀取錯誤欄位：

```json
{"status": "error", "detail": [{"field": "body.dht11_temperature", "input": 99.0, "message": "...", "type": "..."}]}
```

## 設定（環境變數）

| 變數 | 預設 | 說明 |
| --- | --- | --- |
| `CLIMATE_HOST` / `CLIMATE_PORT` | `0.0.0.0` / `8801` | 綁定位址與埠 |
| `CLIMATE_LOG_LEVEL` | `INFO` | 日誌等級（`DEBUG` / `INFO` / `WARNING` / `ERROR`） |
| `FAN_TEMP_ON_C` / `FAN_TEMP_OFF_C` | `28` / `26` | 溫度開 / 關閾值（含遲滯） |
| `FAN_HUMIDITY_ON` / `FAN_HUMIDITY_OFF` | `70` / `65` | 溼度開 / 關閾值 |
| `AGENT_OVERRIDE_TTL_S` | `60` | 代理人指令後暫停規則引擎的秒數 |
| `DEVICE_OFFLINE_AFTER_S` | `30` | 超過此秒數未上報視為離線 |
| `LONG_POLL_TIMEOUT_S` | `25` | long-poll 最長等待秒數 |
| `SSE_KEEPALIVE_S` | `15` | SSE keep-alive 間隔 |
| `CLIMATE_AUTH_TOKEN` | 未設定 | 設定後，變更類路由需帶 `X-Device-Token` 或 `?token=` |

## 測試

```bash
pytest
```

涵蓋：上報與規則決策、遲滯行為、多模態（PIR 節能 / MQ-135 通風）、輸入驗證、
代理人覆寫、輪詢 / long-poll、真實 SSE 串流（以 uvicorn 實跑）、token 驗證、
健康與狀態端點，以及 `climate_agent` 的決策模式（`advisor` / `rules` / `llm`）、
理由生成、多模態提示詞、JSON 解析與防抖護欄。

## 後續整合（尚未實作）

- 以 `mcp` SDK 將 `/api/stream` 與 `/api/fan/command` 包裝成 MCP tools。
- 需要跨行程 / 多實例時，把 `ClimateHub` 的記憶體匯流排換成 MQTT 或 Redis。