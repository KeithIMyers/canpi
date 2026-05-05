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
    """Represents a single PAMAS S50P fuel quality device."""

    def __init__(self, device_id: int, port: str):
        self.device_id = device_id
        self.port = port
        self.connected = False
        self.last_reading: Optional[Dict] = None
        self.last_update: float = 0


class PAMASManager:
    """
    Manages PAMAS S50P fuel quality monitoring via RS485/Modbus-RTU.

    Auto-detects serial devices on startup and continuously watches for
    hotplugged RS485 adapters. Falls back to simulation mode when no
    real devices are found.
    """

    # Glob patterns for serial/RS485 devices
    SERIAL_PATTERNS = [
        '/dev/ttyUSB*', '/dev/ttyACM*',
        '/dev/ttyAMA*', '/dev/ttyS*',
        '/dev/serial/by-id/*',
    ]

    def __init__(self):
        self._devices: Dict[int, PAMASDevice] = {}
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._watcher_thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._simulate = True
        self._active_ports: List[str] = []
        self._auto_mode = True  # True = auto-detect, False = manual override
        self._mode_label = 'idle'

    # ── Port scanning ────────────────────────────────────────────────

    @staticmethod
    def scan_ports() -> List[Dict]:
        """Detect available serial/TTY devices."""
        import glob
        found = set()
        for pat in PAMASManager.SERIAL_PATTERNS:
            found.update(glob.glob(pat))
        ports = []
        seen_real = set()
        for p in sorted(found):
            real = os.path.realpath(p)
            if real not in seen_real:
                seen_real.add(real)
                ports.append({'path': p, 'real_path': real})
        return ports

    # ── Auto-detect watcher ──────────────────────────────────────────

    def start_watcher(self):
        """Start the background device watcher that auto-starts monitoring."""
        if self._watcher_thread and self._watcher_thread.is_alive():
            return
        self._stop.clear()
        self._watcher_thread = threading.Thread(target=self._watch_loop, daemon=True)
        self._watcher_thread.start()

    def _watch_loop(self):
        """Periodically scan for serial devices; auto-start/restart as needed."""
        while not self._stop.is_set():
            if self._auto_mode:
                detected = [p['path'] for p in self.scan_ports()]
                current = sorted(self._active_ports)
                new = sorted(detected)

                if new != current:
                    # Devices changed — restart with new set
                    self._internal_stop()
                    if new:
                        self._internal_start(new)
                    else:
                        # No real devices — run simulation
                        self._internal_start(None)
                elif not self.is_running:
                    # Not running yet — start with whatever we have
                    if new:
                        self._internal_start(new)
                    else:
                        self._internal_start(None)

            self._stop.wait(timeout=5.0)

    # ── Internal start/stop (no auto_mode change) ───────────────────

    def _internal_start(self, ports: Optional[List[str]]):
        """Start monitoring without changing auto_mode."""
        if self._thread and self._thread.is_alive():
            return

        self._stop_poll = threading.Event()

        if ports:
            self._simulate = False
            self._active_ports = list(ports)
            self._mode_label = f'real ({len(ports)} device{"s" if len(ports) != 1 else ""})'
            with self._lock:
                self._devices.clear()
                for i, port in enumerate(ports):
                    self._devices[i] = PAMASDevice(device_id=i + 1, port=port)
        else:
            self._simulate = True
            self._active_ports = []
            self._mode_label = 'simulation'
            with self._lock:
                self._devices.clear()
                for i in range(2):
                    self._devices[i] = PAMASDevice(device_id=i + 1, port=f"/dev/ttyS{i}")

        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()

    def _internal_stop(self):
        """Stop polling without changing auto_mode."""
        if hasattr(self, '_stop_poll'):
            self._stop_poll.set()
        if self._thread:
            self._thread.join(timeout=3)
            self._thread = None
        with self._lock:
            self._devices.clear()
        self._active_ports = []

    # ── Public API ───────────────────────────────────────────────────

    def start(self, ports: Optional[List[str]] = None):
        """Manually start monitoring. Disables auto-detect."""
        self._auto_mode = False
        self._internal_stop()
        self._internal_start(ports)

    def stop(self):
        """Manually stop monitoring. Re-enables auto-detect."""
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
                    "port": dev.port,
                    "connected": dev.connected,
                    "last_update": dev.last_update,
                }
                if dev.last_reading:
                    entry.update(dev.last_reading)
                results.append(entry)
            return results

    def get_status(self) -> Dict:
        """Return current PAMAS manager status."""
        return {
            'running': self.is_running,
            'auto_mode': self._auto_mode,
            'mode': self._mode_label,
            'simulate': self._simulate,
            'active_ports': list(self._active_ports),
            'device_count': len(self._devices),
        }

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _poll_loop(self):
        stop_event = self._stop_poll if hasattr(self, '_stop_poll') else self._stop
        while not stop_event.is_set() and not self._stop.is_set():
            for dev in list(self._devices.values()):
                if self._simulate:
                    self._simulate_reading(dev)
                else:
                    self._real_reading(dev)
            stop_event.wait(timeout=2.0)

    def _simulate_reading(self, dev: PAMASDevice):
        """Generate simulated PAMAS S50P fuel quality data."""
        with self._lock:
            dev.connected = True
            dev.last_update = time.time()
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
                "status": "OK",
            }

    def _real_reading(self, dev: PAMASDevice):
        """Read from a real PAMAS S50P device via Modbus-RTU."""
        try:
            from pymodbus.client import ModbusSerialClient
            client = ModbusSerialClient(
                port=dev.port,
                baudrate=9600,
                parity='N',
                stopbits=1,
                bytesize=8,
                timeout=2,
            )
            if not client.connect():
                with self._lock:
                    dev.connected = False
                return

            # Read holding registers (addresses are device-specific)
            result = client.read_holding_registers(
                address=0x0000, count=10, slave=dev.device_id
            )
            if result.isError():
                with self._lock:
                    dev.connected = False
                client.close()
                return

            regs = result.registers
            with self._lock:
                dev.connected = True
                dev.last_update = time.time()
                dev.last_reading = {
                    "fuel_type": ["Unknown", "Diesel", "Gasoline", "Jet-A1"][
                        min(regs[0], 3)
                    ],
                    "quality_index": regs[1] / 10.0,
                    "particle_count_4um": regs[2],
                    "particle_count_6um": regs[3],
                    "particle_count_14um": regs[4],
                    "water_content_ppm": regs[5] / 10.0,
                    "temperature_c": regs[6] / 10.0,
                    "flow_rate_ml_min": regs[7] / 10.0,
                    "iso_class": f"{regs[8]>>8}/{regs[8]&0xFF}/{regs[9]&0xFF}",
                    "status": "OK",
                }
            client.close()

        except Exception:
            with self._lock:
                dev.connected = False


# Module-level singleton
pamas_manager = PAMASManager()
