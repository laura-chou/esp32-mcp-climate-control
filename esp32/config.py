"""ESP32 configuration - EDIT THIS FILE, then copy it to the board.

Copy both ``config.py`` and ``main.py`` onto the ESP32 (e.g. with mpremote):

    mpremote connect /dev/ttyUSB0 fs cp esp32/config.py :config.py
    mpremote connect /dev/ttyUSB0 fs cp esp32/main.py   :main.py
"""

# --- Wi-Fi -----------------------------------------------------------------
WIFI_SSID = "YOUR_WIFI_SSID"
WIFI_PASSWORD = "YOUR_WIFI_PASSWORD"

# --- PC transport layer ----------------------------------------------------
# NOTE: 192.168.0.0 is a NETWORK address and is NOT reachable. Replace it with
# the LAN IP of the computer running the FastAPI server (e.g. 192.168.0.100).
# Find it on the PC with:  ipconfig (Windows) / ifconfig | ip addr (Linux/macOS)
SERVER_HOST = "192.168.0.0"
SERVER_PORT = 8801

DEVICE_ID = "esp32-climate-01"

# Set this to the same value as CLIMATE_AUTH_TOKEN on the server, or leave it
# as None when the server runs without a token.
DEVICE_TOKEN = None

# --- Pins ------------------------------------------------------------------
DHT_PIN = 21          # DHT11 data pin
RELAY_INL_PIN = 22    # left relay / fan
RELAY_INR_PIN = 23    # right relay / fan
PIR_PIN = 14          # PIR motion sensor (digital input)
MQ_PIN = 27           # MQ-135 air-quality DO (digital trigger input)

# Most PIR modules drive HIGH while motion is detected.
PIR_ACTIVE_HIGH = True

# Most MQ-135 breakout boards pull DO LOW when the gas threshold is exceeded.
MQ_ACTIVE_LOW = True

# Most cheap relay boards are active-LOW (a LOW signal turns the relay on).
# Set to False if your board is active-HIGH.
RELAY_ACTIVE_LOW = True

# --- Sensor validity window (DHT11) ----------------------------------------
# DHT11 physical measurement range. Reads outside this window are almost always
# bit errors / disconnected-sensor noise; they are dropped on the device so a
# bad value never reaches the server (and never triggers a 422 there).
# Keep these in sync with server/models.py.
DHT11_TEMP_MIN_C = 0.0
DHT11_TEMP_MAX_C = 50.0
DHT11_HUMIDITY_MIN = 20.0
DHT11_HUMIDITY_MAX = 90.0

# --- Timing ----------------------------------------------------------------
READ_INTERVAL_S = 5       # seconds between sensor uploads
WIFI_RETRY_DELAY_S = 5    # seconds to wait before retrying a failed request
