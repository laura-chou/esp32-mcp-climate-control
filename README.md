# esp32-mcp-climate-control

![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-8801-009688)
![Ollama](https://img.shields.io/badge/Ollama-11434-black)
![License](https://img.shields.io/badge/license-MIT-green)

**ESP32 + FastAPI + Ollama 智慧風扇控制**：ESP32 上報 DHT11 溫溼度、PIR 人體動態與 MQ-135
空氣品質，由本機 FastAPI 傳輸層串流，交給「規則引擎 + LLM」混合決策後回推風扇指令。

## System Architecture / 系統架構

```mermaid
flowchart LR
    subgraph ESP32["邊緣端：ESP32 (MicroPython)"]
        DHT[DHT11 溫溼度]
        PIR[PIR 人體感測]
        MQ[MQ-135 空氣品質]
        FW["esp32/main.py<br/>讀值 + 輪詢指令"]
        FAN["L9110 風扇驅動<br/>INL/INR"]
        DHT --> FW
        PIR --> FW
        MQ --> FW
        FW -->|set_fan| FAN
    end

    subgraph PC["本機電腦：Python / FastAPI / Ollama"]
        API["FastAPI<br/>Port 8801"]
        HUB["ClimateHub<br/>狀態 + SSE 事件匯流排"]
        RULES["server/rules.py<br/>確定性規則引擎"]
        AGENT["climate_agent.py<br/>混合決策代理人"]
        OLLAMA["Ollama<br/>Port 11434"]
        API --- HUB
        HUB --- RULES
        HUB -->|SSE reading| AGENT
        AGENT -->|prompt| OLLAMA
    end

    FW -->|"1. POST /api/climate"| API
    AGENT -->|"2. POST /api/fan/command"| API
    API -->|"3. GET /api/fan/command (輪詢 / long-poll)"| FW
```

資料流：ESP32 上報讀值 → FastAPI 存進 `ClimateHub` → 規則引擎先算出初步指令 →
透過 SSE 推播 `reading` 事件 → `climate_agent.py` 混合決策後 `POST /api/fan/command` 回推 →
ESP32 以輪詢 / long-poll 取得指令並驅動風扇。

## Quick Start / 快速開始

```bash
pip install -r requirements.txt

# 1. 啟動傳輸層伺服器（Port 8801）
python main.py

# 2. 啟動環境代理人（需先執行 Ollama 並拉取模型）
ollama serve &
ollama pull qwen2.5-coder:3b
python climate_agent.py --model qwen2.5-coder:3b

# 3.（選用）沒有 Ollama 時，改用佔位決策的示範消費者
python examples/mcp_agent_consumer.py
```

互動式 API 文件：<http://127.0.0.1:8801/docs>

### ESP32 接線表

| 元件 | ESP32 腳位 | 備註 |
| --- | --- | --- |
| DHT11 溫溼度 | GPIO 21 | 量測範圍 0~50 °C / 20~90 % |
| PIR 人體感測 | GPIO 14 | 高電位觸發（`PIR_ACTIVE_HIGH = True`） |
| MQ-135 空氣品質 | GPIO 27 | 低電位觸發（`MQ_ACTIVE_LOW = True`） |
| L9110 風扇 | GPIO 22 / 23 | INL / INR，`INL=0/INR=1` 運轉、`INL=0/INR=0` 停止 |

將 `esp32/config.py` 的 `SERVER_HOST` 改成跑 FastAPI 那台電腦的區網 IP，
再以 `mpremote` 把 `esp32/config.py` 與 `esp32/main.py` 上傳到開發板即可。

## API Summary / API 一覽

| 方法 | 路徑 | 用途 |
| --- | --- | --- |
| `POST` | `/api/climate` | ESP32 上報讀值（JSON） |
| `GET`  | `/api/stream` | SSE 串流，推播 `snapshot` / `reading` / `fan_command` 事件 |
| `GET`  | `/api/fan/command` | ESP32 取得風扇指令（`wait=1` 為 long-poll） |
| `POST` | `/api/fan/command` | MCP / `climate_agent.py` 下達風扇指令 |
| `POST` | `/api/fan/auto` | 把裝置交還規則引擎（解除代理人接管） |
| `GET`  | `/api/health` | 健康檢查 |
| `GET`  | `/api/status` | 系統快照（裝置、最新讀值、風扇狀態） |

## Core Design / 核心亮點

- **安全底線**：`mq_triggered=true` 一律由程式碼直接強制開風扇通風，不呼叫 LLM；
  模型若在極端氣候或「需要降溫且有人在場」時想關風扇，安全封套也會強制改回 `ON`
  （過冷不算極端，風扇無法加熱）。
- **遲滯（hysteresis）**：`T ≥ 28 °C` 或 `H ≥ 70 %` 開；`T ≤ 26 °C` 且 `H ≤ 65 %` 才關，
  避免在門檻邊緣反覆切換。
- **防抖護欄**：非緊急切換需間隔 `--min-switch-seconds`（預設 30 秒）；MQ 觸發與極端氣候
  視為緊急、可立即切換。
- **自動降級（Fallback）**：Ollama 離線、逾時或回傳無效 JSON 時，自動退回 `server/rules.py`
  的規則引擎決策，`source` 標示為 `rule-engine`，確保無 AI 時仍穩定運作。
