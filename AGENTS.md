# CanPi Project – Research & Implementation Guide

## 1. Overview
- **Goal**: Build a multi‑user, offline‑first application that runs in Docker on a Raspberry Pi 5.
- **Core Components**:
  - CAN‑bus interfaces: `can0`, `can1`, and a virtual `can9` for testing.
  - Role‑based access (admin, regular users) with per‑user CAN interface selection.
  - Real‑time dashboard with charts/graphs.
  - Live‑capture & CSV export.
  - REST API + API‑key management.
  - SQLite internal DB (no recording by default).
  - USB‑drive based data logging (external storage only).
  - PAMAS S50P fuel‑quality box monitoring via RS485 (up to 4 devices).

## 2. Hardware Integration
### 2‑Channel CAN‑BUS‑FD Shield (MCP2518FD)
- **Reference**: https://wiki.seeedstudio.com/2-Channel-CAN-BUS-FD-Shield-for-Raspberry-Pi/
- **Key Points**:
  - Uses MCP2518FD controller for CAN‑FD on two channels.
  - Connects to Raspberry Pi via SPI and GPIO for interrupt/ready line.
  - Requires kernel modules: `can`, `can_raw`, `can_fd`, `mcp2518fd`.
  - Configurable bitrates (e.g., 1 Mbps, 2 Mbps, 5 Mbps) and data phases.
- **Setup Steps** (to be documented):
  1. Install Seeed Studio drivers (`can-utils`, `socketcan`).
  2. Enable CAN interfaces (`ip link set can0 up type can bitrate 500000`) etc.
  3. Verify with `cansend can0 1A1#1234567890ABCDEF`.

### PAMAS S50P via RS485
- **Protocol**: Modbus‑RTU over RS485 (typically 9600 bps, 8N1).
- **Multiple Devices**: Use RS485 hub with addressable IDs; implement a daisy‑chain or multi‑master scheme.
- **Integration**: Provide a Python library (e.g., `pymodbus`) that can open several serial ports (`/dev/ttyS0`, `/dev/ttyS1`, …) and parse fuel‑quality registers.

## 3. Docker Environment for Raspberry Pi 5
- **Base Image**: `arm64v8/python:3.11-slim` (or `arm64v8/python:3.11` for full stdlib).
- **Dockerfile Highlights**:
  ```Dockerfile
  FROM arm64v8/python:3.11-slim
  RUN apt-get update && apt-get install -y \
      can-utils iproute2 \
      && rm -rf /var/lib/apt/lists/*
  WORKDIR /app
  COPY requirements.txt .
  RUN pip install --no-cache-dir -r requirements.txt
  COPY . .
  CMD ["python", "main.py"]
  ```
- **docker‑compose.yml** (excerpt):
  ```yaml
  services:
    canpi-web:
      build: .
      network_mode: host               # required for direct CAN access
      cap_add:
        - NET_ADMIN
      devices:
        - /dev/can0:/dev/can0
        - /dev/can1:/dev/can1
        - /dev/can9:/dev/can9
      volumes:
        - /media/usb:/media/usb          # USB drive mount point
      environment:
        - CAN_INTERFACES=can0,can1,can9
  ```

## 4. Multi‑User Architecture
- **User Model** (SQLite):
  ```sql
  CREATE TABLE users (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      username TEXT UNIQUE NOT NULL,
      password_hash TEXT NOT NULL,
      role TEXT CHECK(role IN ('admin','user')) NOT NULL,
      allowed_canifs TEXT                 -- comma‑separated list e.g. "can0,can1"
  );
  ```
- **Authentication**: Flask‑Login or FastAPI `OAuth2PasswordBearer`.
- **Authorization**: Middleware checks `allowed_canifs` against requested CAN interfaces.

## 5. Admin Interface
- **Features**:
  - Create / edit / delete user accounts.
  - Assign one or more CAN interfaces (`can0`, `can1`, `can9`) to a user.
  - Generate / revoke API keys per user.
- **UI Tech**: Modern front‑end framework (e.g., React, Vue, or Svelte) bundled with a lightweight server (e.g., Flask‑templates or FastAPI + Jinja2).

## 6. Real‑Time Dashboard
- **Charting Library**: `Chart.js` or `ECharts` for dynamic graphs.
- **Data Flow**:
  1. Background worker reads from CAN sockets (`socketcan`).
  2. Parses frames, pushes to a Redis or in‑memory store.
  3. Front‑end polls via WebSocket (or Server‑Sent Events) for updates.
- **Console View**: Monospace textarea that streams raw CAN frames.

## 7. Live‑Capture & CSV Export
- **Capture Logic**:
  - When “Start Capture” is pressed, a background task begins storing raw frames in a temporary buffer.
  - On “Stop”, the buffer is flushed to a CSV file (`timestamp,can_id,data`).
- **Export**: Provide a download endpoint (`/capture/export`) that streams the CSV file.

## 8. REST API
- **Endpoints**:
  - `GET /api/v1/can/{interface}` – real‑time frame stream (Server‑Sent Events).
  - `POST /api/v1/capture/start` – begin recording.
  - `POST /api/v1/capture/stop` – stop and return CSV.
  - `POST /api/v1/keys` – create new API key (admin only).
- **Security**: API keys stored hashed; each key has a scope limited to the user’s allowed CAN interfaces.

## 9. Data Recorder (External USB Logging)
- **Mount Point**: `/media/usb` (auto‑detect via udev rule or manual mount).
- **Write Path**: `/media/usb/logs/<timestamp>.csv`.
- **UI Control**: Dropdown listing detected USB drives; only this path is permitted for logging.
- **Safety**: Prevent writing to `/home` or `/var` (SD card) to avoid filling up the internal storage.

## 10. PAMAS S50P Integration
- **Serial Setup**: `/dev/ttyS0`, `/dev/ttyS1`, … with RS485 driver (`rs485` kernel module).
- **Modbus Mapping**:
  - Read holding registers for fuel‑quality metrics.
  - Example: Register `0x0001` = fuel type, `0x0002` = quality index.
- **Parallel Support**: Maintain a pool of serial connections; each can be queried independently and results aggregated for the dashboard.

## 11. Development Workflow
1. **Prototype** each CAN interface with `cansend` / `cansim`.
2. **Docker Compose** to spin up the app and a separate `socketcan` network for testing.
3. **Mock** PAMAS S50P using a serial loopback to validate parsing logic.
4. **Iterate** UI components with hot‑reload (e.g., `npm run dev`).
5. **Testing**: Use `pytest` + `pytest‑asyncio` for API and CAN‑bus logic.

## 12. References & Research Notes
- **SeeedStudio MCP2518FD Shield**: https://wiki.seeedstudio.com/2-Channel-CAN-BUS-FD-Shield-for-Raspberry-Pi/
- **SocketCAN**: https://www.kernel.org/doc/html/latest/network/can.html
- **Python‑Can**: https://python-can.readthedocs.io/
- **FastAPI Documentation**: https://fastapi.tiangolo.com/
- **Chart.js**: https://www.chartjs.org/
- **Modbus RTU**: https://modbus.org/specs.php
- **USB Auto‑Mount**: `/etc/udev/rules.d/99-usb.rules`

---

*This document will be updated as implementation progresses. Feel free to open issues or pull requests to add missing details.*