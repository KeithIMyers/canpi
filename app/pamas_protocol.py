"""
Serial protocol layer for PAMAS S50/S50P online particle counters.

What we know from the PAMAS S50 user manual (20160719) and PAMAS FAQ:
  - Data transmission is RS485, point-to-point: ONE counter per RS485
    adapter. Multi-device setups need one USB-RS485 adapter per counter.
  - The counter streams its results after each measurement cycle; PAMAS's
    own POV/PCT software decodes them. The logical wire format is NOT
    publicly documented.
  - The instrument reports particle counts in 8 size channels
    (4/6/10/14/21/28/38/70 um(c)) and ISO 4406 cleanliness codes derived
    from the 4/6/14 um(c) channels. Flow range is 5-50 ml/min.

Because the wire format is undocumented, this module:
  1. Listens passively on the port, rotating through common baud rates
     until bytes arrive.
  2. Attempts to parse ASCII result lines (ISO codes like "18/16/13",
     rows of channel counts, key=value pairs).
  3. If a full baud sweep stays silent, probes once for a Modbus RTU
     slave (some units ship with fieldbus options).
  4. Always keeps a raw capture ring buffer so the true format can be
     inspected live via the /pamas/raw endpoint and the parser finalized
     against real traffic.
"""

import math
import re
import threading
import time
from typing import Dict, List, Optional

try:
    import serial  # pyserial
except ImportError:  # pragma: no cover
    serial = None

# 8 size channels per the S50 technical specification, in um(c)
CHANNEL_SIZES_UM = [4, 6, 10, 14, 21, 28, 38, 70]

# Baud rates to cycle through while listening for traffic. 9600 8N1 is
# the classic RS485 default; the true S50 rate is unconfirmed publicly.
BAUD_CANDIDATES = [9600, 19200, 38400, 57600, 115200]

# How long to listen on one baud rate before rotating (seconds). The S50
# only transmits after a measurement cycle completes, so this must be
# comfortably longer than a cycle gap.
BAUD_DWELL_S = 30.0

RAW_CAPTURE_MAX = 8192  # bytes kept per device for protocol inspection

ISO_CODE_RE = re.compile(r'\b(\d{1,2})\s*/\s*(\d{1,2})\s*/\s*(\d{1,2})\b')
NUMBER_RE = re.compile(r'[-+]?\d+(?:[.,]\d+)?')


def iso4406_class(count_per_100ml: float) -> Optional[int]:
    """ISO 4406 class for a particle count per 100 ml.

    Formula from the S50 manual Appendix C:
        class(x) = log2(x * 1.024) + 1   (e.g. 9600 p/100ml -> 14.263)
    """
    if count_per_100ml is None or count_per_100ml <= 0:
        return None
    return max(0, int(math.floor(math.log2(count_per_100ml * 1.024) + 1)))


def iso4406_count(iso_class: float) -> float:
    """Inverse of iso4406_class: particles per 100 ml for a given class."""
    return (2.0 ** (iso_class - 1)) / 1.024


class PAMASSerialReader:
    """Reads one PAMAS counter on one serial port (point-to-point link)."""

    def __init__(self, port: str, baud: Optional[int] = None):
        self.port = port
        self._fixed_baud = baud
        self._baud_idx = 0
        self.baud = baud or BAUD_CANDIDATES[0]
        self._ser = None
        self._line_buf = bytearray()
        self._capture = bytearray()
        self._capture_lock = threading.Lock()
        self._baud_started = time.monotonic()
        self._sweeps_completed = 0
        self._modbus_probed = False
        self._modbus_mode = False
        self._modbus_slave = 1
        self.state = 'connecting'   # connecting|listening|receiving|modbus|error
        self.last_error: Optional[str] = None
        self.last_data_time: float = 0

    # ── Port lifecycle ───────────────────────────────────────────────

    def _open(self) -> bool:
        if serial is None:
            self.state = 'error'
            self.last_error = 'pyserial not installed'
            return False
        if self._ser is not None and self._ser.is_open:
            return True
        try:
            self._ser = serial.Serial(
                port=self.port,
                baudrate=self.baud,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=0,  # non-blocking reads; poll() is called periodically
            )
            self.state = 'listening'
            self.last_error = None
            self._baud_started = time.monotonic()
            return True
        except Exception as e:
            self._ser = None
            self.state = 'error'
            self.last_error = str(e)
            return False

    def close(self):
        if self._ser is not None:
            try:
                self._ser.close()
            except Exception:
                pass
            self._ser = None

    @property
    def is_open(self) -> bool:
        return self._ser is not None and self._ser.is_open

    # ── Baud rotation ────────────────────────────────────────────────

    def _rotate_baud(self):
        """Advance to the next candidate baud rate and reopen the port."""
        self.close()
        self._baud_idx = (self._baud_idx + 1) % len(BAUD_CANDIDATES)
        if self._baud_idx == 0:
            self._sweeps_completed += 1
        self.baud = BAUD_CANDIDATES[self._baud_idx]
        self._open()

    # ── Raw capture ──────────────────────────────────────────────────

    def _record(self, data: bytes):
        with self._capture_lock:
            self._capture.extend(data)
            if len(self._capture) > RAW_CAPTURE_MAX:
                del self._capture[:len(self._capture) - RAW_CAPTURE_MAX]

    def raw_capture(self) -> Dict:
        """Hex + ASCII views of recent raw bytes, for protocol discovery."""
        with self._capture_lock:
            data = bytes(self._capture)
        return {
            'port': self.port,
            'baud': self.baud,
            'state': self.state,
            'last_error': self.last_error,
            'bytes_captured': len(data),
            'hex': data[-1024:].hex(' '),
            'ascii': data[-1024:].decode('ascii', errors='replace'),
        }

    # ── Parsing ──────────────────────────────────────────────────────

    def _parse_line(self, raw_line: bytes) -> Optional[Dict]:
        """Best-effort parse of one ASCII line from the counter."""
        text = raw_line.decode('ascii', errors='replace').strip()
        if not text:
            return None

        reading: Dict = {'protocol': 'ascii', 'raw_line': text}
        found = False

        m = ISO_CODE_RE.search(text)
        numeric_text = text
        if m:
            reading['iso_class'] = f'{m.group(1)}/{m.group(2)}/{m.group(3)}'
            found = True
            # Don't let the ISO code digits pollute the channel counts
            numeric_text = text[:m.start()] + ' ' + text[m.end():]

        nums = [float(n.replace(',', '.')) for n in NUMBER_RE.findall(numeric_text)]
        if len(nums) >= len(CHANNEL_SIZES_UM):
            # Assume the first 8 numeric fields are the channel counts in
            # ascending size order (4..70 um(c)) — VERIFY against real
            # traffic via /pamas/raw before trusting.
            channels = nums[:len(CHANNEL_SIZES_UM)]
            reading['channel_counts'] = {
                f'{size}um': count
                for size, count in zip(CHANNEL_SIZES_UM, channels)
            }
            reading['counts_unverified'] = True
            found = True

        return reading if found else None

    def _drain_lines(self) -> Optional[Dict]:
        """Split buffered bytes into lines; return the last parseable reading."""
        reading = None
        while True:
            # Accept LF, CR, or CRLF line endings
            idx_n = self._line_buf.find(b'\n')
            idx_r = self._line_buf.find(b'\r')
            candidates = [i for i in (idx_n, idx_r) if i != -1]
            if not candidates:
                break
            idx = min(candidates)
            line = bytes(self._line_buf[:idx])
            del self._line_buf[:idx + 1]
            parsed = self._parse_line(line)
            if parsed:
                reading = parsed
        # Guard against a format with no line endings flooding the buffer
        if len(self._line_buf) > 2048:
            del self._line_buf[:len(self._line_buf) - 2048]
        return reading

    # ── Modbus probe (fallback for units with fieldbus option) ───────

    def _probe_modbus(self) -> Optional[Dict]:
        """One-shot probe for a Modbus RTU slave on this port."""
        self.close()
        try:
            from pymodbus.client import ModbusSerialClient
        except ImportError:
            return None
        for baud in (9600, 19200):
            try:
                client = ModbusSerialClient(
                    port=self.port, baudrate=baud, parity='N',
                    stopbits=1, bytesize=8, timeout=2,
                )
                if not client.connect():
                    continue
                result = client.read_holding_registers(
                    address=0, count=16, slave=self._modbus_slave)
                client.close()
                if result is not None and not result.isError():
                    self._modbus_mode = True
                    self.baud = baud
                    self.state = 'modbus'
                    return self._modbus_reading(result.registers)
            except Exception as e:
                self.last_error = str(e)
        return None

    def _modbus_reading(self, registers: List[int]) -> Dict:
        # The S50P register map is not publicly documented — expose the
        # raw registers so the mapping can be worked out from real data.
        return {
            'protocol': 'modbus',
            'modbus_registers': list(registers),
            'status': 'Modbus slave detected — register map unverified',
        }

    def _poll_modbus(self) -> Optional[Dict]:
        try:
            from pymodbus.client import ModbusSerialClient
            client = ModbusSerialClient(
                port=self.port, baudrate=self.baud, parity='N',
                stopbits=1, bytesize=8, timeout=2,
            )
            if not client.connect():
                return None
            result = client.read_holding_registers(
                address=0, count=16, slave=self._modbus_slave)
            client.close()
            if result is not None and not result.isError():
                return self._modbus_reading(result.registers)
        except Exception as e:
            self.last_error = str(e)
        return None

    # ── Main poll (called every ~2s by PAMASManager) ─────────────────

    def poll(self) -> Optional[Dict]:
        """Service the port; return a new reading dict or None."""
        if self._modbus_mode:
            return self._poll_modbus()

        if not self._open():
            return None

        try:
            waiting = self._ser.in_waiting
            data = self._ser.read(waiting) if waiting else b''
        except Exception as e:
            self.last_error = str(e)
            self.state = 'error'
            self.close()
            return None

        if data:
            self.state = 'receiving'
            self.last_data_time = time.time()
            self._record(data)
            self._line_buf.extend(data)
            return self._drain_lines()

        # Silence handling: rotate bauds; after one full silent sweep,
        # try a Modbus probe once, then keep sweeping passively.
        if self._fixed_baud is None and self.state != 'receiving':
            if time.monotonic() - self._baud_started > BAUD_DWELL_S:
                if self._sweeps_completed >= 1 and not self._modbus_probed:
                    self._modbus_probed = True
                    reading = self._probe_modbus()
                    if reading:
                        return reading
                    self._open()  # resume passive listening
                else:
                    self._rotate_baud()
        return None
