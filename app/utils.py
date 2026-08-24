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
    """Represents a single PAMAS S50/S50P particle counter.

    The S50's RS485 link is point-to-point (per the user manual): one
    counter per serial port. Multi-counter setups use one USB-RS485
    adapter per unit.
    """

    def __init__(self, device_id: int, port: str):
        self.device_id = device_id
        self.port = port
        self.connected = False
        self.last_reading: Optional[Dict] = None
        self.last_update: float = 0
        self.reader = None  # PAMASSerialReader for real hardware


class PAMASManager:
    """
    Manages PAMAS S50/S50P particle counter monitoring via RS485.

    Auto-detects USB-RS485 adapters on startup and continuously watches
    for hotplug events; idles when none are present. Simulation only runs
    when explicitly selected from the dashboard ("Simulation" override).
    The wire protocol is handled by
    pamas_protocol.PAMASSerialReader, which listens passively, tries
    ASCII parsing, probes for Modbus, and records raw bytes for
    inspection (the S50 protocol is not publicly documented).
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
        """Detect available serial/TTY devices.

        Ports are flagged `usb: true` when they are hotplugged USB-serial
        adapters (the PAMAS S50 ships with a USB-RS485 adapter). Built-in
        UARTs (ttyAMA*/ttyS*, e.g. the Pi 5 debug UART ttyAMA10) always
        exist, so auto-detect ignores them; they remain selectable via
        manual override for RS485 HAT setups.
        """
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
                is_usb = real.startswith(('/dev/ttyUSB', '/dev/ttyACM'))
                ports.append({'path': p, 'real_path': real, 'usb': is_usb})
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
                # Auto-detect only latches onto USB-serial adapters;
                # built-in UARTs would otherwise pin us in "real" mode forever.
                detected = [p['path'] for p in self.scan_ports() if p['usb']]
                current = sorted(self._active_ports)
                new = sorted(detected)

                if new != current:
                    # Devices changed — restart with new set
                    self._internal_stop()
                    if new:
                        self._internal_start(new)
                    else:
                        # No real devices — idle (simulation is manual-only,
                        # via the dashboard's "Simulation" override)
                        self._mode_label = 'idle (no USB adapter)'
                elif not self.is_running and new:
                    # Not running yet — start with detected adapters
                    self._internal_start(new)
                elif not self.is_running:
                    self._mode_label = 'idle (no USB adapter)'

            self._stop.wait(timeout=5.0)

    # ── Internal start/stop (no auto_mode change) ───────────────────

    def _internal_start(self, ports: Optional[List[str]]):
        """Start monitoring without changing auto_mode."""
        if self._thread and self._thread.is_alive():
            return

        self._stop_poll = threading.Event()

        if ports:
            from .pamas_protocol import PAMASSerialReader
            fixed_baud = os.environ.get('PAMAS_BAUD')
            fixed_baud = int(fixed_baud) if fixed_baud else None
            self._simulate = False
            self._active_ports = list(ports)
            self._mode_label = f'real ({len(ports)} device{"s" if len(ports) != 1 else ""})'
            with self._lock:
                self._devices.clear()
                for i, port in enumerate(ports):
                    dev = PAMASDevice(device_id=i + 1, port=port)
                    dev.reader = PAMASSerialReader(port, baud=fixed_baud)
                    self._devices[i] = dev
        else:
            self._simulate = True
            self._active_ports = []
            self._mode_label = 'simulation (manual)'
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
            for dev in self._devices.values():
                if dev.reader is not None:
                    dev.reader.close()
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
                if dev.reader is not None:
                    entry["link_state"] = dev.reader.state
                    entry["baud"] = dev.reader.baud
                if dev.last_reading:
                    entry.update(dev.last_reading)
                results.append(entry)
            return results

    def get_raw_capture(self) -> List[Dict]:
        """Raw serial capture per device, for protocol discovery."""
        with self._lock:
            readers = [(d.device_id, d.reader) for d in self._devices.values()]
        results = []
        for device_id, reader in readers:
            if reader is None:
                results.append({'device_id': device_id, 'note': 'simulation — no raw data'})
            else:
                entry = reader.raw_capture()
                entry['device_id'] = device_id
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
        """Generate simulated PAMAS S50/S50P particle counter data.

        Mirrors what the real instrument reports: 8 size channels of
        particle counts (per 100 ml) and an ISO 4406 code derived from
        the 4/6/14 um(c) channels, plus flow rate (spec: 5-50 ml/min).
        """
        from .pamas_protocol import CHANNEL_SIZES_UM, iso4406_class, iso4406_count

        # Drift a base contamination level around ISO class 18 at 4 um(c),
        # with counts falling off toward the larger size channels.
        base_iso = random.uniform(16.5, 19.5)
        channel_counts = {}
        iso_per_channel = {}
        for i, size in enumerate(CHANNEL_SIZES_UM):
            ch_iso = base_iso - i * random.uniform(1.4, 2.2)
            count = max(0.0, iso4406_count(ch_iso) * random.uniform(0.9, 1.1))
            channel_counts[f'{size}um'] = round(count, 1)
            iso_per_channel[size] = iso4406_class(count)

        iso_class = "/".join(
            str(iso_per_channel[s] if iso_per_channel[s] is not None else 0)
            for s in (4, 6, 14)
        )

        with self._lock:
            dev.connected = True
            dev.last_update = time.time()
            dev.last_reading = {
                "protocol": "simulation",
                "iso_class": iso_class,
                "channel_counts": channel_counts,
                "flow_rate_ml_min": round(random.uniform(5, 50), 1),
                "status": "OK",
            }

    def _real_reading(self, dev: PAMASDevice):
        """Poll a real PAMAS counter through its serial reader."""
        reader = dev.reader
        if reader is None:
            from .pamas_protocol import PAMASSerialReader
            baud = os.environ.get('PAMAS_BAUD')
            reader = dev.reader = PAMASSerialReader(
                dev.port, baud=int(baud) if baud else None)

        reading = reader.poll()
        with self._lock:
            # "connected" means the serial port is open; whether the
            # counter is actually talking shows up in link_state/reading.
            dev.connected = reader.is_open or reader.state == 'modbus'
            if reading:
                dev.last_update = time.time()
                dev.last_reading = reading


# Module-level singleton
pamas_manager = PAMASManager()
