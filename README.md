# esp32-mcp-climate-control

ESP32 + MCP + Ollama 本機氣候控制系統。

本 repo 目前實作了架構圖中 **「本機電腦（PC）傳輸層」** 的部分：
FastAPI 伺服器（Port 8801），負責接收 ESP32 上報的 DHT22 溫溼度資料、
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
│   └── mcp_agent_consumer.py   # 模擬 MCP 核心：訂閱 SSE 並回推指令
├── esp32/                      # ⚠️ ESP32 端 MicroPython 韌體（放到開發板上）
│   ├── main.py                 # 讀 DHT22 → 上報 → 輪詢指令 → 控制繼電器
│   └── config.py               # WiFi、PC IP、腳位設定（要先編輯）
└── tests/
    └── test_transport.py       # pytest 測試（含真實 SSE 串流）
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
  -d '{"device_id":"esp32-climate-01","dht22_temperature":31.2,"dht22_humidity":55.0}'
```

回應會附帶目前應套用的風扇指令，ESP32 可在同一次往返就取得結果：

```json
{
  "status": "ok",
  "reading": {"device_id": "esp32-climate-01", "dht22_temperature": 31.2, "...": "..."},
  "fan_command": {"command": "ON", "reason": "temperature crossed threshold (T=31.2C, H=55.0%)", "source": "rule-engine", "seq": 1}
}
```

`dht22_temperature`（-40~80 °C）與 `dht22_humidity`（0~100 %）超出範圍會回 `422`。
`device_id` 可省略，預設為 `esp32-climate-01`。

### 2. SSE 串流 — `GET /api/stream`

MCP 核心與 `climate_agent.py` 訂閱此端點。連線後會先收到一筆 `snapshot`（目前狀態），
之後每當有新讀值或新指令就即時推播：

```
event: snapshot
data: {"seq": 3, "readings": {...}}

event: reading
data: {"device_id": "esp32-climate-01", "dht22_temperature": 30.0, "dht22_humidity": 75.0, "...": "..."}

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
| DHT22 資料腳 | GPIO21 |
| 繼電器 INL（左） | GPIO22 |
| 繼電器 INR（右） | GPIO23 |

兩個繼電器由同一道指令同步開關。多數廉價繼電器板是**低電位觸發**（LOW = 開）；
若你的板子是**高電位觸發**，把 `config.py` 的 `RELAY_ACTIVE_LOW` 改成 `False`。

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

3. ESP32 開機後會自動執行：連 Wi-Fi → 每 5 秒讀 DHT22 並 `POST /api/climate` →
   依回應與 `GET /api/fan/command` 的指令切換繼電器。

> ⚠️ `SERVER_HOST` 不能填 `192.168.0.0` —— 那是網段位址，不是可連線的主機。
> 要用電腦的實際 IP（Windows 用 `ipconfig`，Linux/macOS 用 `ip addr` 查）。

### 通訊協定（與 PC 端一致）

上報 body：
```json
{"device_id": "esp32-climate-01", "dht22_temperature": 31.2, "dht22_humidity": 55.0}
```
取得指令：`GET /api/fan/command?device_id=...&since_seq=...` → `{"command": "ON"|"OFF", ...}`

## 端到端試跑

開三個終端機：

```bash
python main.py                         # 1. 伺服器
python examples/esp32_simulator.py     # 2. 模擬 ESP32 上報
python examples/mcp_agent_consumer.py  # 3. 模擬 MCP 核心（SSE 訂閱 + 回推指令）
```

## 設定（環境變數）

| 變數 | 預設 | 說明 |
| --- | --- | --- |
| `CLIMATE_HOST` / `CLIMATE_PORT` | `0.0.0.0` / `8801` | 綁定位址與埠 |
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

涵蓋：上報與規則決策、遲滯行為、輸入驗證、代理人覆寫、輪詢 / long-poll、
真實 SSE 串流（以 uvicorn 實跑）、token 驗證、健康與狀態端點。

## 後續整合（尚未實作）

- `climate_agent.py` 內的 `decide_with_placeholder_agent()` 改為實際呼叫 Ollama。
- 以 `mcp` SDK 將 `/api/stream` 與 `/api/fan/command` 包裝成 MCP tools。
- 需要跨行程 / 多實例時，把 `ClimateHub` 的記憶體匯流排換成 MQTT 或 Redis。