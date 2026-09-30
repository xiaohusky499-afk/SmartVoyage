"""Start the three local A2A services used by the web application."""

from __future__ import annotations

import atexit
import os
import socket
import subprocess
import sys
import time

SERVICES = (
    ("weather A2A", "SmartVoyage.a2a_server.weather_server", 5005),
    ("ticket A2A", "SmartVoyage.a2a_server.ticket_server", 5006),
    ("trip A2A", "SmartVoyage.a2a_server.trip_server", 5007),
)
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
LOG_DIR = os.path.join(ROOT, "SmartVoyage", "logs")
processes: list[tuple[subprocess.Popen, object]] = []


def port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.3)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def start_service(name: str, module: str, port: int) -> None:
    if port_open(port):
        print(f"{name} already running on {port}")
        return
    os.makedirs(LOG_DIR, exist_ok=True)
    log_file = open(os.path.join(LOG_DIR, f"{module.rsplit('.', 1)[-1]}.log"), "a", encoding="utf-8")
    process = subprocess.Popen(
        [sys.executable, "-m", module],
        cwd=ROOT,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )
    processes.append((process, log_file))
    for _ in range(80):
        if port_open(port):
            print(f"{name} ready on {port}")
            return
        if process.poll() is not None:
            break
        time.sleep(0.5)
    print(f"{name} did not become ready; check SmartVoyage/logs")


def stop_services() -> None:
    for process, log_file in processes:
        if process.poll() is None:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
            else:
                process.terminate()
        log_file.close()


if __name__ == "__main__":
    atexit.register(stop_services)
    for service in SERVICES:
        start_service(*service)
    print("A2A services are running. Press Ctrl+C to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
