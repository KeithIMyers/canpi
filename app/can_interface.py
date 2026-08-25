"""
CAN Interface Abstraction Layer.

Provides:
- Per-user capture sessions (multi-user safe)
- Virtual CAN9 simulator that generates test frames
- Real socketcan capture on can0 / can1
- Thread-safe frame queues
"""

import can
import threading
import queue
import time
import random
import struct
from typing import List, Dict, Any, Optional
from collections import defaultdict, deque


def msg_to_dict(msg, interface: str = "") -> Dict[str, Any]:
    """Convert a python-can Message (or simulated frame) to a dict."""
    return {
        "timestamp": msg.timestamp if hasattr(msg, 'timestamp') else time.time(),
        "interface": interface or getattr(msg, 'channel', ''),
        "arbitration_id": hex(msg.arbitration_id),
        "dlc": msg.dlc,
        "data": msg.data.hex(),
        "is_fd": getattr(msg, 'is_fd', False),
    }


class VirtualCAN9Simulator(threading.Thread):
    """Generates realistic-looking CAN frames on the virtual can9 interface."""

    def __init__(self, output_queue: queue.Queue, stop_event: threading.Event):
        super().__init__(daemon=True)
        self._queue = output_queue
        self._stop = stop_event

        # ── Engine state machine ─────────────────────────────────────
        self._rpm = 800.0
        self._speed = 0.0
        self._coolant = 75.0
        self._throttle = 0.0
        self._fuel = 85.0
        self._voltage = 13.8
        self._oil_pressure = 350.0
        self._engine_load = 15.0
        self._intake_temp = 25.0
        self._maf_flow = 3.5
        self._boost = 101.3           # atmospheric kPa at idle
        self._exhaust_temp = 200.0
        self._fuel_pressure = 350.0
        self._timing_advance = 10.0
        self._gear = 0
        self._trans_temp = 60.0
        self._odometer = 42315.7      # km
        self._fuel_consumption = 0.8  # L/h at idle
        self._ambient_temp = 22.0
        self._brake_pressure = 0.0
        self._dtc_count = 0
        self._tick = 0

    def _simulate_driving(self):
        """Simulate realistic correlated driving behavior."""
        self._tick += 1

        # Driving cycle: idle → accelerate → cruise → decelerate → repeat
        cycle = (self._tick % 600)  # 60-second cycle at 10Hz

        if cycle < 50:        # idle
            target_rpm = 800
            target_throttle = 0
            self._gear = 0
        elif cycle < 150:     # accelerating
            phase = (cycle - 50) / 100
            target_rpm = 800 + 3200 * phase
            target_throttle = 30 + 50 * phase
            self._gear = min(int(phase * 5) + 1, 6)
        elif cycle < 400:     # cruising
            target_rpm = 2200 + random.uniform(-100, 100)
            target_throttle = 25 + random.uniform(-5, 5)
            self._gear = 5
        elif cycle < 500:     # decelerating
            phase = (cycle - 400) / 100
            target_rpm = 2200 - 1400 * phase
            target_throttle = max(0, 25 - 30 * phase)
            self._gear = max(1, 5 - int(phase * 4))
        else:                 # back to idle
            target_rpm = 800
            target_throttle = 0
            self._gear = 0

        # Smooth transitions
        self._rpm += (target_rpm - self._rpm) * 0.1 + random.uniform(-20, 20)
        self._rpm = max(600, min(7000, self._rpm))

        self._throttle += (target_throttle - self._throttle) * 0.15 + random.uniform(-1, 1)
        self._throttle = max(0, min(100, self._throttle))

        # Derived values with realistic correlations
        self._speed = max(0, (self._rpm - 800) * 0.04 * max(self._gear, 0.5))
        self._speed = min(220, self._speed + random.uniform(-0.5, 0.5))

        self._engine_load = self._throttle * 0.85 + random.uniform(-2, 2)
        self._engine_load = max(0, min(100, self._engine_load))

        # Coolant warms up slowly, stabilizes
        target_coolant = 85 + self._engine_load * 0.15
        self._coolant += (target_coolant - self._coolant) * 0.005 + random.uniform(-0.1, 0.1)
        self._coolant = max(40, min(115, self._coolant))

        self._oil_pressure = 150 + self._rpm * 0.06 + random.uniform(-10, 10)
        self._oil_pressure = max(100, min(700, self._oil_pressure))

        self._maf_flow = self._rpm * self._engine_load * 0.0005 + random.uniform(-0.5, 0.5)
        self._maf_flow = max(1, min(500, self._maf_flow))

        self._boost = 101.3 + max(0, self._throttle - 30) * 3 + random.uniform(-2, 2)
        self._boost = max(80, min(280, self._boost))

        self._exhaust_temp = 150 + self._engine_load * 5 + self._rpm * 0.05
        self._exhaust_temp += random.uniform(-5, 5)
        self._exhaust_temp = max(100, min(850, self._exhaust_temp))

        self._fuel_pressure = 300 + self._throttle * 3 + random.uniform(-5, 5)
        self._fuel_pressure = max(200, min(700, self._fuel_pressure))

        self._timing_advance = 10 + (self._rpm - 800) * 0.005 - self._engine_load * 0.1
        self._timing_advance += random.uniform(-0.5, 0.5)
        self._timing_advance = max(-10, min(45, self._timing_advance))

        self._intake_temp = self._ambient_temp + self._boost * 0.03 + random.uniform(-0.3, 0.3)
        self._intake_temp = max(-30, min(70, self._intake_temp))

        target_trans = 60 + self._speed * 0.15 + self._engine_load * 0.2
        self._trans_temp += (target_trans - self._trans_temp) * 0.003 + random.uniform(-0.1, 0.1)
        self._trans_temp = max(30, min(150, self._trans_temp))

        self._fuel_consumption = 0.8 + self._throttle * 0.3 + self._rpm * 0.001
        self._fuel_consumption += random.uniform(-0.1, 0.1)
        self._fuel_consumption = max(0.5, min(40, self._fuel_consumption))

        self._fuel -= self._fuel_consumption * 0.00001  # very slow drain
        self._fuel = max(0, min(100, self._fuel))

        self._odometer += self._speed * 0.0000278  # speed * (0.1s / 3600)

        self._voltage = 13.8 + random.uniform(-0.15, 0.15) - self._engine_load * 0.005
        self._voltage = max(11.5, min(14.8, self._voltage))

        self._brake_pressure = max(0, (100 - self._throttle) * 0.3) if cycle > 400 else 0
        self._brake_pressure += random.uniform(-0.5, 0.5)
        self._brake_pressure = max(0, min(180, self._brake_pressure))

        self._ambient_temp += random.uniform(-0.02, 0.02)
        self._ambient_temp = max(-20, min(50, self._ambient_temp))

        # Occasional DTC simulation
        if self._tick % 3000 == 0:
            self._dtc_count = random.choice([0, 0, 0, 0, 1, 2])

    def run(self):
        while not self._stop.is_set():
            self._simulate_driving()

            # Build all frames
            frames = [
                # Core engine
                (0x100, struct.pack(">HHxxxx", int(self._rpm),
                                    int(self._rpm * 0.95))),
                (0x200, struct.pack(">HHxxxx", int(self._speed * 100),
                                    int(self._speed))),
                (0x300, struct.pack(">hxx", int(self._coolant * 10))),
                (0x400, struct.pack(">Hxx", int(self._throttle * 10))),
                (0x500, struct.pack(">Hxx", int(self._fuel * 10))),
                (0x600, struct.pack(">Hxx", int(self._voltage * 100))),
                (0x700, struct.pack(">Hxx", int(self._oil_pressure))),
                # Extended engine
                (0x110, struct.pack(">Hxx", int(self._engine_load * 10))),
                (0x120, struct.pack(">hxx", int(self._intake_temp * 10))),
                (0x130, struct.pack(">Hxx", int(self._maf_flow * 100))),
                (0x140, struct.pack(">Hxx", int(self._boost * 10))),
                (0x150, struct.pack(">Hxx", int(self._exhaust_temp))),
                (0x160, struct.pack(">Hxx", int(self._fuel_pressure))),
                (0x170, struct.pack(">hxx", int(self._timing_advance * 10))),
                # Transmission / vehicle
                (0x210, struct.pack(">BxhH", int(self._gear),
                                    int(self._trans_temp * 10), 0)),
                (0x220, struct.pack(">Ixxxx", int(self._odometer * 10))),
                (0x230, struct.pack(">Hxx", int(self._fuel_consumption * 100))),
                # Environment / brakes
                (0x310, struct.pack(">hxx", int(self._ambient_temp * 10))),
                (0x320, struct.pack(">Hxx", int(self._brake_pressure * 10))),
                # Diagnostics
                (0x7E0, struct.pack(">Bxxxxxxx", self._dtc_count)),
            ]

            for arb_id, data in frames:
                if self._stop.is_set():
                    break
                dlc = len(data)
                msg = can.Message(
                    arbitration_id=arb_id,
                    data=data[:dlc],
                    is_extended_id=False,
                    timestamp=time.time(),
                )
                self._queue.put(msg_to_dict(msg, interface="can9"))

            self._stop.wait(timeout=0.1)  # ~10 Hz update rate


class CaptureSession:
    """
    A per-user capture session that reads from one or more CAN interfaces.
    Frames go into a shared queue (for SSE) and a private buffer (for CSV export).
    """

    def __init__(self, session_id: str, interfaces: List[str],
                 broadcast_queue: queue.Queue):
        self.session_id = session_id
        self.interfaces = interfaces
        self._broadcast = broadcast_queue
        # Ring buffer: a J1939 bus at ~350 frames/s would grow an
        # unbounded list by GBs/day and OOM the Pi. 100k frames is
        # ~5 min of full-rate traffic for CSV export.
        self._buffer: deque = deque(maxlen=100_000)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._threads: List[threading.Thread] = []
        self._sim: Optional[VirtualCAN9Simulator] = None
        self._running = False

    def start(self):
        self._running = True
        for iface in self.interfaces:
            if iface == "can9":
                internal_q = queue.Queue()
                self._sim = VirtualCAN9Simulator(internal_q, self._stop)
                self._sim.start()
                t = threading.Thread(target=self._relay_sim, args=(internal_q,), daemon=True)
                t.start()
                self._threads.append(t)
            else:
                t = threading.Thread(target=self._capture_real, args=(iface,), daemon=True)
                t.start()
                self._threads.append(t)

    def stop(self):
        self._stop.set()
        self._running = False
        for t in self._threads:
            t.join(timeout=2)

    def get_buffer(self) -> List[Dict]:
        with self._lock:
            return list(self._buffer)

    def clear_buffer(self):
        with self._lock:
            self._buffer.clear()

    @property
    def is_running(self) -> bool:
        return self._running

    def _push_frame(self, frame: Dict):
        with self._lock:
            self._buffer.append(frame)
        try:
            self._broadcast.put_nowait(frame)
        except queue.Full:
            pass

    def _relay_sim(self, sim_q: queue.Queue):
        from .can_decoder import can_decoder
        while not self._stop.is_set():
            try:
                frame = sim_q.get(timeout=0.5)
                decoded = can_decoder.decode_frame(frame)
                if decoded:
                    frame['decoded'] = decoded
                self._push_frame(frame)
            except queue.Empty:
                continue

    def _capture_real(self, iface: str):
        try:
            bus = can.interface.Bus(bustype='socketcan', channel=iface,
                                   receive_own_messages=False)
        except (can.CanError, OSError):
            return
        while not self._stop.is_set():
            try:
                msg = bus.recv(timeout=0.5)
                if msg is not None:
                    # Buffer for CSV export only; the InterfaceMonitor
                    # already decodes + broadcasts real frames to SSE.
                    frame = msg_to_dict(msg, interface=iface)
                    with self._lock:
                        self._buffer.append(frame)
            except Exception:
                continue
        bus.shutdown()


class InterfaceMonitor(threading.Thread):
    """Always-on reader for one real CAN interface.

    Runs from app boot, independent of capture sessions: decodes every
    frame (keeping gauges, /can/decoded, and bus stats live) and
    broadcasts to the SSE queue. Retries forever if the interface is
    absent or errors, so hotplugged/late interfaces recover on their own.
    """

    def __init__(self, iface: str, broadcast_queue: queue.Queue,
                 stop_event: threading.Event):
        super().__init__(daemon=True)
        self.iface = iface
        self._broadcast = broadcast_queue
        self._stop = stop_event

    def run(self):
        from .can_decoder import can_decoder
        while not self._stop.is_set():
            try:
                bus = can.interface.Bus(bustype='socketcan', channel=self.iface,
                                        receive_own_messages=False)
            except Exception:
                self._stop.wait(5.0)
                continue
            try:
                while not self._stop.is_set():
                    msg = bus.recv(timeout=0.5)
                    if msg is None:
                        continue
                    frame = msg_to_dict(msg, interface=self.iface)
                    decoded = can_decoder.decode_frame(frame)
                    if decoded:
                        frame['decoded'] = decoded
                    try:
                        self._broadcast.put_nowait(frame)
                    except queue.Full:
                        pass
            except Exception:
                pass
            finally:
                try:
                    bus.shutdown()
                except Exception:
                    pass
            self._stop.wait(5.0)


class CaptureManager:
    """
    Global singleton that manages all active capture sessions.
    There is one broadcast queue per user-session, and one per-interface
    real CAN reader.
    """

    def __init__(self):
        self._sessions: Dict[str, CaptureSession] = {}
        self._lock = threading.Lock()
        # Global broadcast queue that all SSE clients read from
        self._global_queue: queue.Queue = queue.Queue(maxsize=5000)
        self._monitors: Dict[str, InterfaceMonitor] = {}
        self._monitor_stop = threading.Event()

    def start_monitors(self, interfaces: List[str]):
        """Start always-on per-interface readers (idempotent)."""
        for iface in interfaces:
            if iface in self._monitors and self._monitors[iface].is_alive():
                continue
            mon = InterfaceMonitor(iface, self._global_queue, self._monitor_stop)
            mon.start()
            self._monitors[iface] = mon

    @property
    def global_queue(self) -> queue.Queue:
        return self._global_queue

    def start_session(self, session_id: str, interfaces: List[str]) -> CaptureSession:
        with self._lock:
            if session_id in self._sessions:
                self._sessions[session_id].stop()
            sess = CaptureSession(session_id, interfaces, self._global_queue)
            sess.start()
            self._sessions[session_id] = sess
            return sess

    def stop_session(self, session_id: str) -> Optional[CaptureSession]:
        with self._lock:
            sess = self._sessions.pop(session_id, None)
            if sess:
                sess.stop()
            return sess

    def get_session(self, session_id: str) -> Optional[CaptureSession]:
        return self._sessions.get(session_id)

    def active_count(self) -> int:
        return len(self._sessions)


# Module-level singleton
capture_manager = CaptureManager()