"""ESP32 MicroPython firmware for the climate-control system.

Reads a DHT11 sensor plus a PIR motion sensor and an MQ-135 air-quality
(digital output) sensor, and drives two relay outputs (INL / INR) based on fan
commands from the PC transport layer:

    POST /api/climate       upload the reading; the response carries the
                            current fan command
    GET  /api/fan/command   poll for a fan command

Readings outside the DHT11 physical range are treated as sensor noise and
dropped before upload, so a garbled value never reaches the server.

Edit config.py first, then copy both files onto the board.
"""

import json
import time

import dht
import machine
import network
import urequests

from config import (
    DEVICE_ID,
    DEVICE_TOKEN,
    DHT11_HUMIDITY_MAX,
    DHT11_HUMIDITY_MIN,
    DHT11_TEMP_MAX_C,
    DHT11_TEMP_MIN_C,
    DHT_PIN,
    MQ_ACTIVE_LOW,
    MQ_PIN,
    PIR_ACTIVE_HIGH,
    PIR_PIN,
    READ_INTERVAL_S,
    RELAY_ACTIVE_LOW,
    RELAY_INL_PIN,
    RELAY_INR_PIN,
    SERVER_HOST,
    SERVER_PORT,
    WIFI_PASSWORD,
    WIFI_RETRY_DELAY_S,
    WIFI_SSID,
)

sensor = dht.DHT11(machine.Pin(DHT_PIN))
relay_inl = machine.Pin(RELAY_INL_PIN, machine.Pin.OUT)
relay_inr = machine.Pin(RELAY_INR_PIN, machine.Pin.OUT)
pir_pin = machine.Pin(PIR_PIN, machine.Pin.IN)
mq_pin = machine.Pin(MQ_PIN, machine.Pin.IN)


def base_url():
    return "http://{}:{}".format(SERVER_HOST, SERVER_PORT)


def headers():
    h = {"Content-Type": "application/json"}
    if DEVICE_TOKEN:
        h["X-Device-Token"] = DEVICE_TOKEN
    return h


def connect_wifi():
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    if not wlan.isconnected():
        print("connecting to Wi-Fi:", WIFI_SSID)
        wlan.connect(WIFI_SSID, WIFI_PASSWORD)
        while not wlan.isconnected():
            time.sleep(1)
    print("Wi-Fi connected:", wlan.ifconfig())
    return wlan


def relay_level(on):
    if RELAY_ACTIVE_LOW:
        return 0 if on else 1
    return 1 if on else 0


def set_fan(on):
    level = relay_level(on)
    relay_inl.value(level)
    relay_inr.value(level)


def reading_is_valid(temperature, humidity):
    """Reject DHT11 bit errors / disconnected-sensor noise.

    The DHT11 only reports 0-50 C and 20-90 % RH; anything outside that window
    (or a NaN) is a bad read and must not be uploaded.
    """

    if temperature is None or humidity is None:
        return False
    if not DHT11_TEMP_MIN_C <= temperature <= DHT11_TEMP_MAX_C:
        return False
    if not DHT11_HUMIDITY_MIN <= humidity <= DHT11_HUMIDITY_MAX:
        return False
    return True


def read_sensor():
    """Return a validated ``(temperature, humidity)`` or ``None``."""

    for _ in range(3):
        try:
            sensor.measure()
            temperature = sensor.temperature()
            humidity = sensor.humidity()
        except OSError:
            time.sleep(1)
            continue

        if reading_is_valid(temperature, humidity):
            return temperature, humidity

        print("discarding out-of-range read: T={} C H={} %".format(temperature, humidity))
        time.sleep(1)

    return None


def read_motion():
    """Return True while the PIR sensor reports motion."""

    level = bool(pir_pin.value())
    return level if PIR_ACTIVE_HIGH else not level


def read_air_quality():
    """Return True when the MQ-135 DO pin signals the gas threshold is exceeded."""

    level = bool(mq_pin.value())
    return (not level) if MQ_ACTIVE_LOW else level


def post_reading(temperature, humidity):
    payload = {
        "device_id": DEVICE_ID,
        "dht11_temperature": round(temperature, 1),
        "dht11_humidity": round(humidity, 1),
        "pir_motion": read_motion(),
        "mq_triggered": read_air_quality(),
    }
    response = urequests.post(
        base_url() + "/api/climate",
        data=json.dumps(payload),
        headers=headers(),
    )
    try:
        return response.json()
    finally:
        response.close()


def poll_command(since_seq):
    url = "{}/api/fan/command?device_id={}&since_seq={}".format(
        base_url(), DEVICE_ID, since_seq
    )
    response = urequests.get(url, headers=headers())
    try:
        return response.json()
    finally:
        response.close()


def apply_command(command):
    state = command.get("command")
    set_fan(state == "ON")
    print("fan ->", state, "|", command.get("reason"), "| seq", command.get("seq"))
    return command.get("seq", 0)


def main():
    connect_wifi()
    set_fan(False)

    last_seq = 0
    while True:
        try:
            reading = read_sensor()
            if reading is not None:
                ack = post_reading(reading[0], reading[1])
                last_seq = apply_command(ack["fan_command"])
            else:
                print("no valid sensor reading; skipping upload")

            command = poll_command(last_seq)
            last_seq = apply_command(command)
        except Exception as exc:
            print("loop error:", exc)
            time.sleep(WIFI_RETRY_DELAY_S)
            continue

        time.sleep(READ_INTERVAL_S)


if __name__ == "__main__":
    main()
