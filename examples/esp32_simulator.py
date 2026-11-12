"""ESP32 simulator: exercises the same endpoints the real device uses.

Run the server first, then:

    python examples/esp32_simulator.py

It POSTs a synthetic DHT11 reading every few seconds and polls
``/api/fan/command`` (with long-polling) to display the fan state.
"""

from __future__ import annotations

import argparse
import random
import time

import httpx

DEFAULT_BASE = "http://127.0.0.1:8801"


def poll_command(client: httpx.Client, base: str, device_id: str, since_seq: int) -> dict:
    resp = client.get(
        f"{base}/api/fan/command",
        params={"device_id": device_id, "since_seq": since_seq, "wait": 1},
        timeout=60.0,
    )
    resp.raise_for_status()
    return resp.json()


def main() -> None:
    parser = argparse.ArgumentParser(description="ESP32 transport-layer simulator")
    parser.add_argument("--base", default=DEFAULT_BASE)
    parser.add_argument("--device-id", default="esp32-climate-01")
    parser.add_argument("--interval", type=float, default=5.0)
    parser.add_argument("--token", default=None, help="Value for X-Device-Token")
    args = parser.parse_args()

    headers = {"X-Device-Token": args.token} if args.token else {}
    last_seq = 0

    with httpx.Client(headers=headers) as client:
        while True:
            temperature = round(random.uniform(24.0, 33.0), 1)
            humidity = round(random.uniform(40.0, 85.0), 1)
            payload = {
                "device_id": args.device_id,
                "dht11_temperature": temperature,
                "dht11_humidity": humidity,
            }
            try:
                resp = client.post(f"{args.base}/api/climate", json=payload, timeout=10.0)
                resp.raise_for_status()
                ack = resp.json()
                fan = ack["fan_command"]["command"]
                print(
                    f"[POST] T={temperature:>4.1f}C H={humidity:>4.1f}% "
                    f"-> fan={fan} ({ack['fan_command']['reason']})"
                )
                last_seq = ack["fan_command"]["seq"]
            except httpx.HTTPError as exc:
                print(f"[POST] failed: {exc}")

            # Demonstrate the long-poll channel the firmware uses.
            try:
                cmd = poll_command(client, args.base, args.device_id, last_seq)
                last_seq = cmd["seq"]
                print(f"[POLL] fan={cmd['command']} seq={cmd['seq']} src={cmd['source']}")
            except httpx.HTTPError as exc:
                print(f"[POLL] failed: {exc}")

            time.sleep(args.interval)


if __name__ == "__main__":
    main()
