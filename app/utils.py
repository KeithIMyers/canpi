"""
Utility classes for USB drive management and PAMAS S50P monitoring.
"""

import os
import threading
import time
import random
import subprocess
from typing import Dict, List, Optional


class USBManager:
    """Detect and manage USB drives mounted under /media/usb."""

    MOUNT_BASE = "/media/usb"

    # Device prefixes that are NOT removable USB storage
    _INTERNAL_PREFIXES = ('/dev/mmcblk', '/dev/nvme', '/dev/loop')

    @staticmethod
    def _parse_usb_mounts() -> Dict[str, str]:
        """Parse /proc/mounts and return {mount_point: device} for USB-like devices
        mounted under MOUNT_BASE."""
        usb_mounts = {}
        base = os.path.realpath(USBManager.MOUNT_BASE)
        try:
            with open('/proc/mounts', 'r') as f:
                for line in f:
                    parts = line.split()
                    if len(parts) < 2:
                        continue
                    device, mount_point = parts[0], parts[1]
                    # Only consider paths under our mount base
                    real_mp = os.path.realpath(mount_point)
                    if real_mp != base and not real_mp.startswith(base + '/'):
                        continue
                    # Skip non-block devices (overlay, tmpfs, etc.)
                    if not device.startswith('/dev/'):
                        continue
                    # Skip internal storage (SD card, NVMe, loop)
                    if any(device.startswith(p) for p in USBManager._INTERNAL_PREFIXES):
                        continue
                    # This is a real removable device (e.g. /dev/sda1)
                    usb_mounts[real_mp] = device
        except OSError:
            pass
        return usb_mounts

    @staticmethod
    def detect_drives() -> List[Dict]:
        """Return list of detected USB mount points with free space info."""
        drives = []
        base = USBManager.MOUNT_BASE
        if not os.path.isdir(base):
            return drives

        usb_mounts = USBManager._parse_usb_mounts()
        if not usb_mounts:
            return drives

        for path, device in usb_mounts.items():
            try:
                st = os.statvfs(path)
                free_mb = (st.f_bavail * st.f_frsize) / (1024 * 1024)
                total_mb = (st.f_blocks * st.f_frsize) / (1024 * 1024)
                drives.append({
                    "path": path,
                    "device": device,
                    "free_mb": round(free_mb, 1),
                    "total_mb": round(total_mb, 1),
                    "writable": os.access(path, os.W_OK),
                })
            except OSError:
                continue
        return drives

    @staticmethod
    def is_writable(path: str) -> bool:
        """Check that path is under the allowed USB mount base and writable."""
        real = os.path.realpath(path)
        if not real.startswith(os.path.realpath(USBManager.MOUNT_BASE)):
            return False
        return os.access(real, os.W_OK)

    @staticmethod
    def get_log_path(drive_path: str) -> str:
        """Return the log directory on the given drive, creating it if needed."""
        log_dir = os.path.join(drive_path, "logs")
        os.makedirs(log_dir, exist_ok=True)
        return log_dir


class PAMASDevice:
    """Represents one Modbus slave on an RS485 bus."""

    def __init__(self, device_id: int, port: str, slave_id: int):
        self.device_id = device_id
        self.port = port
        self.slave_id = slave_id
        self.connected = False
        self.last_reading: Optional[Dict] = None
        self.last_update: float = 0
        self.last_error: Optional[str] = None


class PAMASManager:
    """
    Manages PAMAS S50P fuel quality monitoring via RS485/Modbus-RTU.

    Auto-detect only considers likely external RS485 adapters by default.
    Built-in Pi console UARTs are ignored unless explicitly configured with
    PAMAS_PORTS. One RS485 port can host multiple Modbus slave IDs.
    """

    AUTO_PATTERNS = [
        '/dev/serial/by-id/*',
        '/dev/ttyUSB*',
        '/dev/ttyACM*',
    ]
    MANUAL_PATTERNS = AUTO_PATTERNS + [
        '/dev/ttyAMA*',
        '/dev/ttyS*',
    ]
    DEFAULT_SLAVE_IDS = list(range(1, 11))  # scan IDs 1-10 by default
    DEFAULT_BAUDRATE = 9600
    POLL_INTERVAL_SEC = 2.0
    WATCH_INTERVAL_SEC = 5.0
    MODBUS_REGISTER = 0x0000
    MODBUS_REGISTER_COUNT = 10

    def __init__(self):
        self._devices: Dict[str, PAMASDevice] = {}
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._watcher_thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._simulate = True
        self._active_ports: List[str] = []
        self._auto_mode = True
        self._mode_label = 'idle'
        self._last_scan: List[Dict] = []
        self._explicit_ports = self._parse_ports(os.environ.get('PAMAS_PORTS', ''))
        self._slave_ids = self._parse_slave_ids(os.environ.get('PAMAS_SLAVE_IDS', ''))
        self._baudrate = self._parse_int(
            os.environ.get('PAMAS_BAUDRATE'), self.DEFAULT_BAUDRATE
        )
        self._last_logged_errors: Dict[str, str] = {}

    # Port scanning

    @staticmethod
    def _parse_ports(value: str) -> List[str]:
        return [p.strip() for p in value.split(',') if p.strip()]

    @classmethod
    def _parse_slave_ids(cls, value: str) -> List[int]:
        ids = []
        for part in value.split(','):
            part = part.strip()
            if not part:
                continue
            try:
                slave_id = int(part, 0)
            except ValueError:
                continue
            if 1 <= slave_id <= 247 and slave_id not in ids:
                ids.append(slave_id)
        return ids or list(cls.DEFAULT_SLAVE_IDS)

    @staticmethod
    def _parse_int(value: Optional[str], default: int) -> int:
        try:
            return int(value) if value else default
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _is_console_uart(path: str) -> bool:
        real = os.path.realpath(path)
        name = os.path.basename(real)
        return name.startswith('ttyAMA') or name.startswith('ttyS')

    @staticmethod
    def _is_available(path: str) -> bool:
        return os.path.exists(path) and os.access(path, os.R_OK | os.W_OK)

    @classmethod
    def _expand_patterns(cls, patterns: List[str]) -> List[str]:
        import glob
        found = []
        for pat in patterns:
            found.extend(glob.glob(pat))
        return sorted(found)

    @classmethod
    def scan_ports(cls, include_manual: bool = False) -> List[Dict]:
        """Detect serial ports that are plausible PAMAS RS485 adapters."""
        patterns = cls.MANUAL_PATTERNS if include_manual else cls.AUTO_PATTERNS
        ports = []
        seen_real = set()
        for path in cls._expand_patterns(patterns):
            real = os.path.realpath(path)
            if real in seen_real:
                continue
            seen_real.add(real)
            ignored = False
            reason = ''
            if not cls._is_available(real):
                ignored = True
                reason = 'not readable/writable'
            elif not include_manual and cls._is_console_uart(real):
                ignored = True
                reason = 'built-in UART ignored in auto mode'
            if not ignored:
                ports.append({'path': path, 'real_path': real})
            elif include_manual:
                ports.append({'path': path, 'real_path': real, 'ignored': True, 'reason': reason})
        return ports

    def _configured_or_detected_ports(self) -> List[str]:
        if self._explicit_ports:
            return [p for p in self._explicit_ports if self._is_available(p)]
        return [p['path'] for p in self.scan_ports(include_manual=False)]

    # Auto-detect watcher

    def start_watcher(self):
        """Start the background device watcher that auto-starts monitoring."""
        if self._watcher_thread and self._watcher_thread.is_alive():
            return
        self._stop.clear()
        self._watcher_thread = threading.Thread(target=self._watch_loop, daemon=True)
        self._watcher_thread.start()

    def _watch_loop(self):
        """Periodically scan for RS485 adapters; auto-start/restart as needed."""
        while not self._stop.is_set():
            if self._auto_mode:
                self._last_scan = self.scan_ports(include_manual=True)
                new = sorted(self._configured_or_detected_ports())
                current = sorted(self._active_ports)

                if new != current:
                    self._internal_stop()
                    self._internal_start(new or None)
                elif not self.is_running:
                    self._internal_start(new or None)

            self._stop.wait(timeout=self.WATCH_INTERVAL_SEC)

    # Internal start/stop

    def _internal_start(self, ports: Optional[List[str]]):
        """Start monitoring without changing auto_mode."""
        if self._thread and self._thread.is_alive():
            return

        self._stop_poll = threading.Event()

        with self._lock:
            self._devices.clear()

            if ports:
                self._simulate = False
                self._active_ports = list(ports)
                self._mode_label = (
                    f"real ({len(ports)} port{'s' if len(ports) != 1 else ''}, "
                    f"slave IDs {','.join(str(i) for i in self._slave_ids)})"
                )
                for port in ports:
                    for slave_id in self._slave_ids:
                        key = f"{port}:{slave_id}"
                        self._devices[key] = PAMASDevice(
                            device_id=slave_id, port=port, slave_id=slave_id
                        )
                self._log(
                    f"monitoring PAMAS ports={ports} slave_ids={self._slave_ids} "
                    f"baudrate={self._baudrate}"
                )
            else:
                self._simulate = True
                self._active_ports = []
                self._mode_label = 'simulation'
                for slave_id in (1, 2):
                    key = f"simulation:{slave_id}"
                    self._devices[key] = PAMASDevice(
                        device_id=slave_id, port='simulation', slave_id=slave_id
                    )
                self._log("no RS485 adapter detected; using PAMAS simulation")

        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()

    def _internal_stop(self):
        """Stop polling without changing auto_mode."""
        if hasattr(self, '_stop_poll'):
            self._stop_poll.set()
        if self._thread:
            self._thread.join(timeout=6)
            self._thread = None
        with self._lock:
            self._devices.clear()
        self._active_ports = []

    # Public API

    def start(self, ports: Optional[List[str]] = None):
        """Manually start monitoring. Disables auto-detect."""
        self._auto_mode = False
        self._internal_stop()
        self._internal_start(ports)

    def stop(self):
        """Stop manual override and re-enable auto-detect."""
        self._internal_stop()
        self._auto_mode = True
        self._mode_label = 'auto-detect'

    def get_telemetry(self) -> List[Dict]:
        """Return latest readings from all monitored devices."""
        with self._lock:
            results = []
            for dev in self._devices.values():
                entry = {
                    "device_id": dev.device_id,
                    "slave_id": dev.slave_id,
                    "port": dev.port,
                    "connected": dev.connected,
                    "last_update": dev.last_update,
                    "last_error": dev.last_error,
                }
                if dev.last_reading:
                    entry.update(dev.last_reading)
                results.append(entry)
            return sorted(results, key=lambda d: (d["port"], d["slave_id"]))

    def get_status(self) -> Dict:
        """Return current PAMAS manager status."""
        return {
            'running': self.is_running,
            'auto_mode': self._auto_mode,
            'mode': self._mode_label,
            'simulate': self._simulate,
            'active_ports': list(self._active_ports),
            'device_count': len(self._devices),
            'slave_ids': list(self._slave_ids),
            'baudrate': self._baudrate,
            'configured_ports': list(self._explicit_ports),
            'last_scan': list(self._last_scan),
        }

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _poll_loop(self):
        stop_event = self._stop_poll if hasattr(self, '_stop_poll') else self._stop
        while not stop_event.is_set() and not self._stop.is_set():
            if self._simulate:
                for dev in list(self._devices.values()):
                    self._simulate_reading(dev)
            else:
                # Group devices by port so we open one serial connection per port.
                port_groups: Dict[str, List[PAMASDevice]] = {}
                for dev in list(self._devices.values()):
                    port_groups.setdefault(dev.port, []).append(dev)
                for port, devices in port_groups.items():
                    if stop_event.is_set():
                        break
                    self._poll_port(port, devices, stop_event)
            stop_event.wait(timeout=self.POLL_INTERVAL_SEC)

    def _simulate_reading(self, dev: PAMASDevice):
        """Generate simulated PAMAS S50P fuel quality data."""
        with self._lock:
            dev.connected = True
            dev.last_update = time.time()
            dev.last_error = None
            dev.last_reading = {
                "fuel_type": random.choice(["Diesel", "Gasoline", "Jet-A1"]),
                "quality_index": round(random.uniform(85, 100), 1),
                "particle_count_4um": random.randint(100, 5000),
                "particle_count_6um": random.randint(50, 2000),
                "particle_count_14um": random.randint(10, 500),
                "water_content_ppm": round(random.uniform(10, 200), 1),
                "temperature_c": round(random.uniform(15, 35), 1),
                "flow_rate_ml_min": round(random.uniform(50, 500), 1),
                "iso_class": random.choice(["18/16/13", "19/17/14", "17/15/12"]),
                "status": "SIMULATED",
            }

    def _poll_port(self, port: str, devices: List[PAMASDevice], stop_event: threading.Event):
        """Open one serial connection and poll all slave IDs on this port."""
        from pymodbus.client import ModbusSerialClient
        client = None
        try:
            client = ModbusSerialClient(
                port=port,
                baudrate=self._baudrate,
                parity='N',
                stopbits=1,
                bytesize=8,
                timeout=1,
            )
            if not client.connect():
                for dev in devices:
                    self._mark_error(dev, "unable to open serial port")
                return
            for dev in devices:
                if stop_event.is_set():
                    break
                self._read_slave(client, dev)
        except Exception as exc:
            for dev in devices:
                self._mark_error(dev, str(exc))
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass

    def _read_slave(self, client, dev: PAMASDevice):
        """Query one Modbus slave using an already-open client."""
        try:
            result = client.read_holding_registers(
                address=self.MODBUS_REGISTER,
                count=self.MODBUS_REGISTER_COUNT,
                slave=dev.slave_id,
            )
            if result.isError():
                self._mark_error(dev, f"Modbus error: {result}")
                return
            regs = result.registers
            if len(regs) < self.MODBUS_REGISTER_COUNT:
                self._mark_error(dev, f"short register read: {len(regs)}")
                return
            with self._lock:
                dev.connected = True
                dev.last_update = time.time()
                dev.last_error = None
                dev.last_reading = self._decode_registers(regs)
        except Exception as exc:
            self._mark_error(dev, str(exc))

    def _decode_registers(self, regs: List[int]) -> Dict:
        """Decode the current placeholder PAMAS register map."""
        fuel_types = ["Unknown", "Diesel", "Gasoline", "Jet-A1"]
        return {
            "fuel_type": fuel_types[min(max(regs[0], 0), len(fuel_types) - 1)],
            "quality_index": regs[1] / 10.0,
            "particle_count_4um": regs[2],
            "particle_count_6um": regs[3],
            "particle_count_14um": regs[4],
            "water_content_ppm": regs[5] / 10.0,
            "temperature_c": regs[6] / 10.0,
            "flow_rate_ml_min": regs[7] / 10.0,
            "iso_class": f"{regs[8] >> 8}/{regs[8] & 0xFF}/{regs[9] & 0xFF}",
            "status": "OK",
            "raw_registers": regs,
        }

    def _mark_error(self, dev: PAMASDevice, message: str):
        with self._lock:
            dev.connected = False
            dev.last_error = message
        key = f"{dev.port}:{dev.slave_id}"
        if self._last_logged_errors.get(key) != message:
            self._last_logged_errors[key] = message
            self._log(f"{key} {message}")

    @staticmethod
    def _log(message: str):
        print(f"[PAMAS] {message}", flush=True)


# Module-level singleton
pamas_manager = PAMASManager()
