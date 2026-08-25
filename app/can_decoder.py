"""
CAN Frame Decoder — translates raw CAN frames into human-readable parameters.

Supports:
- Custom signal definitions (CanPi internal format, matching the can9 simulator)
- OBD-II PID decoding (standard 0x7E8 responses)
- J1939 PGN decoding (heavy equipment)
- DTC (Diagnostic Trouble Code) parsing
- Bus health metrics (load, error rate, per-ID frequency)

Each decoded signal has: name, value, unit, min, max, warning thresholds.
"""

import struct
import time
import threading
from typing import Dict, List, Optional, Any
from collections import defaultdict


class Signal:
    """A single decoded CAN signal definition."""
    __slots__ = ('name', 'unit', 'min_val', 'max_val', 'warn_low', 'warn_high',
                 'icon', 'category', 'format_str')

    def __init__(self, name: str, unit: str = '', min_val: float = 0,
                 max_val: float = 100, warn_low: float = None,
                 warn_high: float = None, icon: str = '',
                 category: str = 'engine', format_str: str = '.1f'):
        self.name = name
        self.unit = unit
        self.min_val = min_val
        self.max_val = max_val
        self.warn_low = warn_low
        self.warn_high = warn_high
        self.icon = icon
        self.category = category
        self.format_str = format_str


# ── Signal definitions for the CanPi can9 simulator ──────────────────

CANPI_SIGNALS = {
    # arb_id -> {signal_key -> (Signal, decode_fn)}
    0x100: {
        'rpm': (
            Signal('Engine RPM', 'rpm', 0, 8000, warn_low=500, warn_high=6500,
                   icon='bi-speedometer', category='engine', format_str='.0f'),
            lambda data: struct.unpack('>H', data[0:2])[0]
        ),
    },
    0x200: {
        'speed': (
            Signal('Vehicle Speed', 'km/h', 0, 250, warn_high=200,
                   icon='bi-speedometer2', category='vehicle', format_str='.0f'),
            lambda data: struct.unpack('>H', data[2:4])[0]
        ),
    },
    0x300: {
        'coolant_temp': (
            Signal('Coolant Temperature', '°C', 0, 150, warn_low=50, warn_high=105,
                   icon='bi-thermometer-half', category='engine', format_str='.1f'),
            lambda data: struct.unpack('>h', data[0:2])[0] / 10.0
        ),
    },
    0x400: {
        'throttle': (
            Signal('Throttle Position', '%', 0, 100,
                   icon='bi-arrow-up-right', category='engine', format_str='.1f'),
            lambda data: struct.unpack('>H', data[0:2])[0] / 10.0
        ),
    },
    0x500: {
        'fuel_level': (
            Signal('Fuel Level', '%', 0, 100, warn_low=10,
                   icon='bi-fuel-pump', category='vehicle', format_str='.1f'),
            lambda data: struct.unpack('>H', data[0:2])[0] / 10.0
        ),
    },
    0x600: {
        'battery_voltage': (
            Signal('Battery Voltage', 'V', 8, 16, warn_low=11.5, warn_high=15.0,
                   icon='bi-battery-charging', category='electrical', format_str='.2f'),
            lambda data: struct.unpack('>H', data[0:2])[0] / 100.0
        ),
    },
    0x700: {
        'oil_pressure': (
            Signal('Oil Pressure', 'kPa', 0, 800, warn_low=100, warn_high=700,
                   icon='bi-droplet-fill', category='engine', format_str='.0f'),
            lambda data: struct.unpack('>H', data[0:2])[0]
        ),
    },
    # Extended simulator IDs
    0x110: {
        'engine_load': (
            Signal('Engine Load', '%', 0, 100,
                   icon='bi-bar-chart-fill', category='engine', format_str='.1f'),
            lambda data: struct.unpack('>H', data[0:2])[0] / 10.0
        ),
    },
    0x120: {
        'intake_temp': (
            Signal('Intake Air Temp', '°C', -40, 80, warn_high=60,
                   icon='bi-wind', category='engine', format_str='.1f'),
            lambda data: struct.unpack('>h', data[0:2])[0] / 10.0
        ),
    },
    0x130: {
        'maf_flow': (
            Signal('MAF Air Flow', 'g/s', 0, 650,
                   icon='bi-wind', category='engine', format_str='.1f'),
            lambda data: struct.unpack('>H', data[0:2])[0] / 100.0
        ),
    },
    0x140: {
        'boost_pressure': (
            Signal('Turbo Boost', 'kPa', 0, 300, warn_high=250,
                   icon='bi-arrow-up-circle', category='engine', format_str='.1f'),
            lambda data: struct.unpack('>H', data[0:2])[0] / 10.0
        ),
    },
    0x150: {
        'exhaust_temp': (
            Signal('Exhaust Gas Temp', '°C', 0, 900, warn_high=800,
                   icon='bi-fire', category='engine', format_str='.0f'),
            lambda data: struct.unpack('>H', data[0:2])[0]
        ),
    },
    0x160: {
        'fuel_pressure': (
            Signal('Fuel Rail Pressure', 'kPa', 0, 800,
                   icon='bi-fuel-pump', category='engine', format_str='.0f'),
            lambda data: struct.unpack('>H', data[0:2])[0]
        ),
    },
    0x170: {
        'timing_advance': (
            Signal('Timing Advance', '°', -20, 60,
                   icon='bi-clock-history', category='engine', format_str='.1f'),
            lambda data: struct.unpack('>h', data[0:2])[0] / 10.0
        ),
    },
    0x210: {
        'gear': (
            Signal('Current Gear', '', 0, 8,
                   icon='bi-gear-wide-connected', category='transmission', format_str='.0f'),
            lambda data: data[0]
        ),
        'trans_temp': (
            Signal('Transmission Temp', '°C', 0, 200, warn_high=120,
                   icon='bi-thermometer-sun', category='transmission', format_str='.1f'),
            lambda data: struct.unpack('>h', data[2:4])[0] / 10.0
        ),
    },
    0x220: {
        'odometer': (
            Signal('Odometer', 'km', 0, 999999,
                   icon='bi-signpost', category='vehicle', format_str='.1f'),
            lambda data: struct.unpack('>I', data[0:4])[0] / 10.0
        ),
    },
    0x230: {
        'fuel_consumption': (
            Signal('Fuel Consumption', 'L/h', 0, 50,
                   icon='bi-droplet', category='vehicle', format_str='.2f'),
            lambda data: struct.unpack('>H', data[0:2])[0] / 100.0
        ),
    },
    0x310: {
        'ambient_temp': (
            Signal('Ambient Temperature', '°C', -40, 60,
                   icon='bi-cloud-sun', category='environment', format_str='.1f'),
            lambda data: struct.unpack('>h', data[0:2])[0] / 10.0
        ),
    },
    0x320: {
        'brake_pressure': (
            Signal('Brake Pressure', 'bar', 0, 200,
                   icon='bi-sign-stop', category='vehicle', format_str='.1f'),
            lambda data: struct.unpack('>H', data[0:2])[0] / 10.0
        ),
    },
    0x7E0: {
        'dtc_count': (
            Signal('Active DTCs', '', 0, 255,
                   icon='bi-exclamation-triangle', category='diagnostics', format_str='.0f'),
            lambda data: data[0]
        ),
    },
}


# Standard OBD-II Mode 01 PID responses from ECU IDs 0x7E8-0x7EF.
# Data layout is usually single-frame ISO-TP: len, 0x41, PID, A, B, C, D...
OBD_PID_SIGNALS = {
    0x04: (
        'engine_load',
        Signal('Engine Load', '%', 0, 100,
               icon='bi-bar-chart-fill', category='engine', format_str='.1f'),
        lambda data: data[0] * 100.0 / 255.0,
        1,
    ),
    0x05: (
        'coolant_temp',
        Signal('Coolant Temperature', '°C', -40, 150, warn_high=105,
               icon='bi-thermometer-half', category='engine', format_str='.1f'),
        lambda data: data[0] - 40,
        1,
    ),
    0x0C: (
        'rpm',
        Signal('Engine RPM', 'rpm', 0, 8000, warn_low=500, warn_high=6500,
               icon='bi-speedometer', category='engine', format_str='.0f'),
        lambda data: ((data[0] * 256) + data[1]) / 4.0,
        2,
    ),
    0x0D: (
        'speed',
        Signal('Vehicle Speed', 'km/h', 0, 250, warn_high=200,
               icon='bi-speedometer2', category='vehicle', format_str='.0f'),
        lambda data: data[0],
        1,
    ),
    0x0F: (
        'intake_temp',
        Signal('Intake Air Temp', '°C', -40, 80, warn_high=60,
               icon='bi-wind', category='engine', format_str='.1f'),
        lambda data: data[0] - 40,
        1,
    ),
    0x10: (
        'maf_flow',
        Signal('MAF Air Flow', 'g/s', 0, 650,
               icon='bi-wind', category='engine', format_str='.1f'),
        lambda data: ((data[0] * 256) + data[1]) / 100.0,
        2,
    ),
    0x11: (
        'throttle',
        Signal('Throttle Position', '%', 0, 100,
               icon='bi-arrow-up-right', category='engine', format_str='.1f'),
        lambda data: data[0] * 100.0 / 255.0,
        1,
    ),
    0x2F: (
        'fuel_level',
        Signal('Fuel Level', '%', 0, 100, warn_low=10,
               icon='bi-fuel-pump', category='vehicle', format_str='.1f'),
        lambda data: data[0] * 100.0 / 255.0,
        1,
    ),
    0x42: (
        'battery_voltage',
        Signal('Control Module Voltage', 'V', 8, 16, warn_low=11.5, warn_high=15.0,
               icon='bi-battery-charging', category='electrical', format_str='.2f'),
        lambda data: ((data[0] * 256) + data[1]) / 1000.0,
        2,
    ),
}


# ── J1939 PGN decoding (heavy equipment, 29-bit extended IDs) ────────
#
# J1939 payloads are little-endian. A raw byte of 0xFB-0xFF (or high byte
# for 16-bit values) means "not available / error" — decode functions
# return None for those and the signal is skipped.

def _j1939_u16(data, i):
    return data[i] | (data[i + 1] << 8)


J1939_PGN_SIGNALS = {
    # pgn -> {signal_key -> (Signal, decode_fn(data8) -> value|None)}
    0xF004: {  # EEC1 — Electronic Engine Controller 1
        'rpm': (
            Signal('Engine RPM', 'rpm', 0, 3000, warn_high=2600,
                   icon='bi-speedometer', category='engine', format_str='.0f'),
            lambda d: _j1939_u16(d, 3) * 0.125 if d[4] <= 0xFA else None  # SPN 190
        ),
        'engine_load': (
            Signal('Engine Load', '%', 0, 125,
                   icon='bi-bar-chart-fill', category='engine', format_str='.0f'),
            lambda d: d[2] if d[2] <= 0xFA else None  # SPN 92
        ),
    },
    0xF003: {  # EEC2
        'throttle': (
            Signal('Throttle Position', '%', 0, 100,
                   icon='bi-arrow-up-right', category='engine', format_str='.1f'),
            lambda d: d[1] * 0.4 if d[1] <= 0xFA else None  # SPN 91
        ),
    },
    0xFEF1: {  # CCVS — Cruise Control/Vehicle Speed
        'speed': (
            Signal('Vehicle Speed', 'km/h', 0, 250,
                   icon='bi-speedometer2', category='vehicle', format_str='.1f'),
            lambda d: _j1939_u16(d, 1) / 256.0 if d[2] <= 0xFA else None  # SPN 84
        ),
    },
    0xFEEE: {  # ET1 — Engine Temperature 1
        'coolant_temp': (
            Signal('Coolant Temperature', '°C', -40, 150, warn_high=105,
                   icon='bi-thermometer-half', category='engine', format_str='.0f'),
            lambda d: d[0] - 40 if d[0] <= 0xFA else None  # SPN 110
        ),
    },
    0xFEEF: {  # EFL/P1 — Engine Fluid Level/Pressure 1
        'oil_pressure': (
            Signal('Oil Pressure', 'kPa', 0, 1000, warn_low=100,
                   icon='bi-droplet-half', category='engine', format_str='.0f'),
            lambda d: d[3] * 4 if d[3] <= 0xFA else None  # SPN 100
        ),
    },
    0xFEF2: {  # LFE — Fuel Economy (Liquid)
        'fuel_rate': (
            Signal('Fuel Rate', 'L/h', 0, 100,
                   icon='bi-fuel-pump', category='engine', format_str='.2f'),
            lambda d: _j1939_u16(d, 0) * 0.05 if d[1] <= 0xFA else None  # SPN 183
        ),
    },
    0xFEF6: {  # IC1 — Inlet/Exhaust Conditions 1
        'boost_pressure': (
            Signal('Boost Pressure', 'kPa', 0, 500,
                   icon='bi-wind', category='engine', format_str='.0f'),
            lambda d: d[1] * 2 if d[1] <= 0xFA else None  # SPN 102
        ),
        'intake_temp': (
            Signal('Intake Air Temp', '°C', -40, 150,
                   icon='bi-wind', category='engine', format_str='.0f'),
            lambda d: d[2] - 40 if d[2] <= 0xFA else None  # SPN 105
        ),
    },
    0xFEF7: {  # VEP1 — Vehicle Electrical Power 1
        'battery_voltage': (
            Signal('Battery Voltage', 'V', 0, 32,
                   icon='bi-battery-charging', category='electrical', format_str='.2f'),
            lambda d: _j1939_u16(d, 4) * 0.05 if d[5] <= 0xFA else None  # SPN 168
        ),
    },
    0xFEE5: {  # HOURS — Engine Hours
        'engine_hours': (
            Signal('Engine Hours', 'h', 0, 100000,
                   icon='bi-clock-history', category='diagnostics', format_str='.1f'),
            lambda d: ((d[0] | d[1] << 8 | d[2] << 16 | d[3] << 24) * 0.05
                       if d[3] <= 0xFA else None)  # SPN 247
        ),
    },
    0xFEFC: {  # DD — Dash Display
        'fuel_level': (
            Signal('Fuel Level', '%', 0, 100, warn_low=10,
                   icon='bi-fuel-pump-fill', category='vehicle', format_str='.1f'),
            lambda d: d[1] * 0.4 if d[1] <= 0xFA else None  # SPN 96
        ),
    },
    0xFEF5: {  # AMB — Ambient Conditions
        'ambient_temp': (
            Signal('Ambient Temperature', '°C', -40, 60,
                   icon='bi-thermometer', category='environment', format_str='.1f'),
            lambda d: (_j1939_u16(d, 3) * 0.03125 - 273
                       if d[4] <= 0xFA else None)  # SPN 171
        ),
    },
}


class CANDecoder:
    """
    Decodes raw CAN frames into named parameter values using signal definitions.
    Thread-safe — maintains latest values for all decoded signals.
    """

    def __init__(self):
        self._values: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._frame_counts: Dict[str, int] = defaultdict(int)
        self._error_count = 0
        self._total_frames = 0
        self._last_reset = time.time()
        self._history: Dict[str, List] = defaultdict(lambda: [])
        self._max_history = 120  # 2 minutes at 1Hz

    def decode_frame(self, frame: Dict) -> List[Dict]:
        """
        Decode a raw CAN frame dict and update internal state.
        Returns list of decoded signal dicts.
        """
        self._total_frames += 1
        arb_id_str = frame.get('arbitration_id', '0x0')
        try:
            arb_id = int(arb_id_str, 16)
        except (ValueError, TypeError):
            return []

        self._frame_counts[arb_id_str] = self._frame_counts.get(arb_id_str, 0) + 1

        data_hex = frame.get('data', '')
        try:
            data = bytes.fromhex(data_hex)
        except (ValueError, TypeError):
            return []

        obd_decoded = self._decode_obd_response(arb_id, data, frame)
        if obd_decoded:
            return obd_decoded

        j1939_decoded = self._decode_j1939(arb_id, data, frame)
        if j1939_decoded:
            return j1939_decoded

        signals = CANPI_SIGNALS.get(arb_id, {})
        decoded = []

        for key, (signal, decode_fn) in signals.items():
            try:
                value = decode_fn(data)
                entry = {
                    'key': key,
                    'name': signal.name,
                    'value': value,
                    'formatted': f'{value:{signal.format_str}}',
                    'unit': signal.unit,
                    'min': signal.min_val,
                    'max': signal.max_val,
                    'warn_low': signal.warn_low,
                    'warn_high': signal.warn_high,
                    'icon': signal.icon,
                    'category': signal.category,
                    'format': signal.format_str,
                    'timestamp': frame.get('timestamp', time.time()),
                    'interface': frame.get('interface', ''),
                    'status': self._calc_status(value, signal),
                }
                with self._lock:
                    self._values[key] = entry
                    # Update history for trended signals
                    if key in ('rpm', 'speed', 'coolant_temp', 'boost_pressure',
                               'exhaust_temp', 'engine_load', 'fuel_consumption'):
                        hist = self._history[key]
                        hist.append({'t': entry['timestamp'], 'v': value})
                        if len(hist) > self._max_history:
                            hist.pop(0)

                decoded.append(entry)
            except (struct.error, IndexError, TypeError):
                self._error_count += 1
                continue

        return decoded

    def _decode_obd_response(self, arb_id: int, data: bytes, frame: Dict) -> List[Dict]:
        if not (0x7E8 <= arb_id <= 0x7EF) or len(data) < 4:
            return []

        payload_len = data[0] & 0x0F
        mode = data[1]
        pid = data[2]
        if payload_len < 3 or mode != 0x41 or pid not in OBD_PID_SIGNALS:
            return []

        key, signal, decode_fn, needed = OBD_PID_SIGNALS[pid]
        payload = data[3:]
        if len(payload) < needed:
            return []

        try:
            value = decode_fn(payload)
        except (IndexError, TypeError, ZeroDivisionError):
            self._error_count += 1
            return []

        entry = {
            'key': key,
            'name': signal.name,
            'value': value,
            'formatted': f'{value:{signal.format_str}}',
            'unit': signal.unit,
            'min': signal.min_val,
            'max': signal.max_val,
            'warn_low': signal.warn_low,
            'warn_high': signal.warn_high,
            'icon': signal.icon,
            'category': signal.category,
            'format': signal.format_str,
            'timestamp': frame.get('timestamp', time.time()),
            'interface': frame.get('interface', ''),
            'source': f'OBD-II ECU {hex(arb_id)} PID {hex(pid)}',
            'status': self._calc_status(value, signal),
        }
        with self._lock:
            self._values[key] = entry
            if key in ('rpm', 'speed', 'coolant_temp', 'engine_load', 'fuel_level'):
                hist = self._history[key]
                hist.append({'t': entry['timestamp'], 'v': value})
                if len(hist) > self._max_history:
                    hist.pop(0)
        return [entry]

    def _decode_j1939(self, arb_id: int, data: bytes, frame: Dict) -> List[Dict]:
        """Decode J1939 broadcast PGNs from 29-bit extended IDs."""
        if arb_id <= 0x7FF or len(data) < 8:
            return []

        # 29-bit ID: priority(3) | PGN(18) | source address(8).
        # For PDU1 (PF < 240) the low PGN byte is a destination address.
        pgn = (arb_id >> 8) & 0x3FFFF
        if ((pgn >> 8) & 0xFF) < 240:
            pgn &= 0x3FF00
        signals = J1939_PGN_SIGNALS.get(pgn)
        if not signals:
            return []

        source_addr = arb_id & 0xFF
        decoded = []
        for key, (signal, decode_fn) in signals.items():
            try:
                value = decode_fn(data)
            except (IndexError, TypeError):
                self._error_count += 1
                continue
            if value is None:  # J1939 "not available"
                continue
            entry = {
                'key': key,
                'name': signal.name,
                'value': value,
                'formatted': f'{value:{signal.format_str}}',
                'unit': signal.unit,
                'min': signal.min_val,
                'max': signal.max_val,
                'warn_low': signal.warn_low,
                'warn_high': signal.warn_high,
                'icon': signal.icon,
                'category': signal.category,
                'format': signal.format_str,
                'timestamp': frame.get('timestamp', time.time()),
                'interface': frame.get('interface', ''),
                'source': f'J1939 PGN 0x{pgn:X} SA 0x{source_addr:02X}',
                'status': self._calc_status(value, signal),
            }
            with self._lock:
                self._values[key] = entry
                if key in ('rpm', 'speed', 'coolant_temp', 'boost_pressure',
                           'engine_load', 'fuel_rate'):
                    hist = self._history[key]
                    hist.append({'t': entry['timestamp'], 'v': value})
                    if len(hist) > self._max_history:
                        hist.pop(0)
            decoded.append(entry)
        return decoded

    def _calc_status(self, value, signal: Signal) -> str:
        if signal.warn_high is not None and value > signal.warn_high:
            return 'danger'
        if signal.warn_low is not None and value < signal.warn_low:
            return 'danger'
        mid_range = (signal.max_val - signal.min_val) * 0.05 if signal.max_val > signal.min_val else 0
        if signal.warn_high is not None and value > signal.warn_high - mid_range:
            return 'warning'
        if signal.warn_low is not None and value < signal.warn_low + mid_range:
            return 'warning'
        return 'normal'

    def get_all_values(self) -> Dict[str, Dict]:
        with self._lock:
            return dict(self._values)

    def get_values_by_category(self) -> Dict[str, List[Dict]]:
        categories: Dict[str, List] = defaultdict(list)
        with self._lock:
            for entry in self._values.values():
                categories[entry['category']].append(entry)
        return dict(categories)

    def get_history(self, key: str) -> List[Dict]:
        with self._lock:
            return list(self._history.get(key, []))

    def get_bus_stats(self) -> Dict:
        elapsed = max(time.time() - self._last_reset, 1)
        return {
            'total_frames': self._total_frames,
            'frames_per_sec': round(self._total_frames / elapsed, 1),
            'decode_errors': self._error_count,
            'unique_ids': len(self._frame_counts),
            'uptime_sec': round(elapsed, 0),
            'id_frequencies': dict(self._frame_counts),
        }

    def get_signal_definitions(self) -> List[Dict]:
        """Return all known signal definitions for the UI to render gauge panels."""
        defs = []
        for arb_id, signals in sorted(CANPI_SIGNALS.items()):
            for key, (signal, _) in signals.items():
                defs.append({
                    'key': key,
                    'arb_id': hex(arb_id),
                    'name': signal.name,
                    'unit': signal.unit,
                    'min': signal.min_val,
                    'max': signal.max_val,
                    'warn_low': signal.warn_low,
                    'warn_high': signal.warn_high,
                    'icon': signal.icon,
                    'category': signal.category,
                })
        for pgn, signals in sorted(J1939_PGN_SIGNALS.items()):
            for key, (signal, _) in signals.items():
                defs.append({
                    'key': key,
                    'arb_id': f'J1939 PGN 0x{pgn:X}',
                    'name': signal.name,
                    'unit': signal.unit,
                    'min': signal.min_val,
                    'max': signal.max_val,
                    'warn_low': signal.warn_low,
                    'warn_high': signal.warn_high,
                    'icon': signal.icon,
                    'category': signal.category,
                })
        for pid, (key, signal, _, _) in sorted(OBD_PID_SIGNALS.items()):
            defs.append({
                'key': key,
                'arb_id': f'0x7e8 PID {hex(pid)}',
                'name': signal.name,
                'unit': signal.unit,
                'min': signal.min_val,
                'max': signal.max_val,
                'warn_low': signal.warn_low,
                'warn_high': signal.warn_high,
                'icon': signal.icon,
                'category': signal.category,
            })
        return defs

    def reset(self):
        with self._lock:
            self._values.clear()
            self._frame_counts.clear()
            self._history.clear()
            self._error_count = 0
            self._total_frames = 0
            self._last_reset = time.time()


# Module-level singleton
can_decoder = CANDecoder()
