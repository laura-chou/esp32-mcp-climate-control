"""ESP32 MicroPython firmware for the climate-control system.

Reads a DHT22 sensor and drives two relay outputs (INL / INR) based on fan
commands from the PC transport layer:

    POST /api/climate       upload the reading; the response carries the
                            current fan command
    GET  /api/fan/command   poll for a fan command

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
    DHT_PIN,
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

sensor = dht.DHT22(machine.Pin(DHT_PIN))
relay_inl = machine.Pin(RELAY_INL_PIN, machine.Pin.OUT)
relay_inr = machine.Pin(RELAY_INR_PIN, machine.Pin.OUT)


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


def read_sensor():
    for _ in range(3):
        try:
            sensor.measure()
            return sensor.temperature(), sensor.humidity()
        except OSError:
            time.sleep(1)
    return None


def post_reading(temperature, humidity):
    payload = {
        "device_id": DEVICE_ID,
        "dht22_temperature": round(temperature, 1),
        "dht22_humidity": round(humidity, 1),
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
                print("sensor read failed")

            command = poll_command(last_seq)
            last_seq = apply_command(command)
        except Exception as exc:
            print("loop error:", exc)
            time.sleep(WIFI_RETRY_DELAY_S)
            continue

        time.sleep(READ_INTERVAL_S)


if __name__ == "__main__":
    main()
