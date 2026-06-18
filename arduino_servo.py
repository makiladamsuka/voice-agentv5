"""Minimal ESP32 serial transport for Voice Agent V5 head pan/tilt servos."""

from __future__ import annotations

from dataclasses import dataclass
import os
import re
import time
from typing import Optional, Tuple

try:
    import serial
except ImportError:  # pragma: no cover - runtime dependency on the Pi
    serial = None  # type: ignore

DEFAULT_PORTS = ("/dev/ttyUSB0", "/dev/ttyUSB1", "/dev/ttyACM0")
BOOT_CPD = 1.0
READY_TIMEOUT_SEC = 5.0
ACK_TIMEOUT_SEC = 1.0
BASE_MOVE_TIMEOUT_SEC = 15.0
MIN_SEND_INTERVAL_SEC = 0.02
SERVO_SEND_MIN_DEG = 0.06
SERVO_SEND_HZ = 25.0
SERVO_ANGLE_QUANTUM_DEG = 0.2
_SERVO_ACK_RE = re.compile(r"^OK\s+P(-?\d+)\s+T(-?\d+)\s*$")
_BASE_ACK_RE = re.compile(r"^OK\s+B(-?\d+(?:\.\d+)?)\s*$")
_OK_C_RE = re.compile(r"^OK\s+C(-?\d+(?:\.\d+)?)\s*$")
_OK_E_RE = re.compile(r"^OK\s+E(-?\d+)\s*$")
_BASE_STATUS_RE = re.compile(
    r"^POS\s+(-?\d+)\s+DEG\s+(-?\d+(?:\.\d+)?)\s+CPD\s+(-?\d+(?:\.\d+)?)\s+BUSY\s+([01])\s*$"
)


@dataclass(frozen=True)
class BaseStatus:
    encoder_count: int
    degrees: float
    counts_per_degree: float
    busy: bool


def _quantize_servo_angle(deg: float, quantum: float = SERVO_ANGLE_QUANTUM_DEG) -> float:
    q = quantum
    return round(deg / q) * q


def resolve_port(port: str) -> list[str]:
    if port:
        if os.path.exists(port):
            return [port]
        fallbacks = [p for p in DEFAULT_PORTS if os.path.exists(p)]
        if fallbacks:
            print(f"Configured serial port {port} not found; using {fallbacks[0]} instead")
            return [fallbacks[0]]
        return [port]
    return [p for p in DEFAULT_PORTS if os.path.exists(p)]


class ArduinoServoLink:
    """USB serial link to v5 ESP32 PCA9685 head-servo firmware."""

    def __init__(self, port: str = "", baud: int = 115200):
        self._port_name = port
        self._baud = baud
        self._ser: Optional[serial.Serial] = None
        self._connected = False
        self._last_pan: Optional[float] = None
        self._last_tilt: Optional[float] = None
        self._last_base_ack: Optional[float] = None
        self._last_send_ts = 0.0
        self.servo_send_min_deg = SERVO_SEND_MIN_DEG
        self.servo_send_hz = SERVO_SEND_HZ
        self.servo_angle_quantum_deg = SERVO_ANGLE_QUANTUM_DEG
        self.base_command_scale = 1.0
        self.base_move_timeout_sec = BASE_MOVE_TIMEOUT_SEC
        self._error_logged = False

    @property
    def connected(self) -> bool:
        return self._connected and self._ser is not None

    def _drain_rx(self) -> None:
        if self._ser is not None and self._ser.in_waiting:
            self._ser.read(self._ser.in_waiting)

    def _wait_for_ready(self, timeout_sec: float) -> bool:
        if self._ser is None:
            return False
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            line = self._ser.readline().decode("utf-8", errors="ignore").strip()
            if line == "READY" or line.startswith("FW head_servo"):
                return True
            time.sleep(0.02)
        return False

    def _handshake(self) -> bool:
        if self._ser is None:
            return False
        if self._wait_for_ready(1.2):
            return True
        self._ser.write(b"H\n")
        self._ser.flush()
        return self._wait_for_ready(READY_TIMEOUT_SEC)

    def connect(self) -> bool:
        if serial is None:
            print("pyserial not installed; install with: python -m pip install pyserial")
            return False

        ports = resolve_port(self._port_name)
        if not ports:
            print(f"No ESP32 serial ports found ({', '.join(DEFAULT_PORTS)}).")
            return False

        for port in ports:
            try:
                self._ser = serial.Serial(port, self._baud, timeout=0.12, write_timeout=1.0)
                time.sleep(0.4)
                if self._handshake():
                    self._connected = True
                    self._error_logged = False
                    self._drain_rx()
                    print(f"ESP32 head servo ready on {port}")
                    return True
                print(f"No READY from ESP32 on {port}")
                self.close(skip_home=True)
            except Exception as e:
                if not self._error_logged:
                    print(f"ESP32 serial connect failed ({port}): {e}")
                    self._error_logged = True
                self.close(skip_home=True)
        return False

    def _read_line_matching(self, timeout: float, *patterns: re.Pattern[str]) -> Optional[str]:
        if self._ser is None:
            return None
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = self._ser.readline().decode("utf-8", errors="ignore").strip()
            if not line:
                continue
            if line.startswith("ERR B"):
                print(line)
                self.write_base_stop()
                return line
            for pattern in patterns:
                if pattern.match(line):
                    return line
        return None

    def _read_ack(self, timeout: float = ACK_TIMEOUT_SEC) -> Optional[Tuple[int, int]]:
        line = self._read_line_matching(timeout, _SERVO_ACK_RE)
        if line is None:
            return None
        match = _SERVO_ACK_RE.match(line)
        if match:
            return int(match.group(1)), int(match.group(2))
        return None

    def _read_base_ack(self, timeout: Optional[float] = None) -> Optional[float]:
        if timeout is None:
            timeout = self.base_move_timeout_sec
        line = self._read_line_matching(timeout, _BASE_ACK_RE)
        if line is None or line.startswith("ERR B"):
            return None
        match = _BASE_ACK_RE.match(line)
        if match:
            return float(match.group(1))
        return None

    def send_line(
        self,
        payload: str,
        *,
        wait_ack: bool = False,
        wait_servo: bool = False,
        wait_base: bool = False,
        drain_after: bool = True,
    ) -> bool:
        if not self.connected or self._ser is None:
            return False
        try:
            self._drain_rx()
            self._ser.write(payload.encode("ascii"))
            if not payload.endswith("\n"):
                self._ser.write(b"\n")
            self._ser.flush()
            if wait_ack or wait_servo:
                if self._read_ack() is None:
                    return False
            if wait_base:
                self._last_base_ack = self._read_base_ack()
                if self._last_base_ack is None:
                    return False
            if drain_after and not (wait_ack or wait_servo or wait_base):
                self._drain_rx()
            return True
        except Exception as e:
            if not self._error_logged:
                print(f"ESP32 serial write failed: {e}")
                self._error_logged = True
            self._connected = False
            return False

    def configure_servo_stream(
        self,
        *,
        min_deg: float | None = None,
        send_hz: float | None = None,
        quantum_deg: float | None = None,
    ) -> None:
        if min_deg is not None:
            self.servo_send_min_deg = max(0.02, min_deg)
        if send_hz is not None:
            self.servo_send_hz = max(5.0, send_hz)
        if quantum_deg is not None:
            self.servo_angle_quantum_deg = max(0.05, quantum_deg)

    def write_angles(self, pan: float, tilt: float, *, force: bool = False, wait_ack: bool = False) -> bool:
        pan = _quantize_servo_angle(pan, self.servo_angle_quantum_deg)
        tilt = _quantize_servo_angle(tilt, self.servo_angle_quantum_deg)
        now = time.time()
        send_interval = 1.0 / max(1.0, self.servo_send_hz)
        moved = (
            self._last_pan is None
            or self._last_tilt is None
            or abs(pan - self._last_pan) >= self.servo_send_min_deg
            or abs(tilt - self._last_tilt) >= self.servo_send_min_deg
        )
        due = (now - self._last_send_ts) >= send_interval
        if not force and not (moved and due):
            return True
        ok = self.send_line(f"P{pan:.1f} T{tilt:.1f}", wait_servo=wait_ack)
        if ok:
            self._last_pan = pan
            self._last_tilt = tilt
            self._last_send_ts = now
        return ok

    def _scale_base_command(self, deg: float) -> float:
        return deg * self.base_command_scale

    def write_combined(
        self,
        pan: float,
        tilt: float,
        base_rel: float | None = None,
        *,
        wait_servo: bool = False,
        wait_base: bool = False,
    ) -> bool:
        pan = _quantize_servo_angle(pan, self.servo_angle_quantum_deg)
        tilt = _quantize_servo_angle(tilt, self.servo_angle_quantum_deg)
        parts = [f"P{pan:.1f}", f"T{tilt:.1f}"]
        if base_rel is not None and abs(base_rel) > 0.001:
            base_rel = self._scale_base_command(base_rel)
            sign = "+" if base_rel >= 0 else ""
            parts.append(f"B{sign}{base_rel:.1f}")
        ok = self.send_line(
            " ".join(parts),
            wait_servo=wait_servo or base_rel is not None,
            wait_base=wait_base and base_rel is not None,
        )
        if ok:
            self._last_pan = pan
            self._last_tilt = tilt
            self._last_send_ts = time.time()
        return ok

    def write_base_relative(self, deg: float, *, wait: bool = False) -> bool:
        deg = self._scale_base_command(deg)
        sign = "+" if deg >= 0 else ""
        return self.send_line(f"B{sign}{deg:.1f}", wait_base=wait, drain_after=not wait)

    def zero_base(self) -> bool:
        return self.send_line("Z")

    def write_base_stop(self) -> bool:
        return self.send_line("X", drain_after=False)

    def set_counts_per_degree(self, cpd: float) -> bool:
        ok = self.send_line(f"C{cpd:.4f}", drain_after=False)
        if not ok:
            return False
        line = self._read_line_matching(ACK_TIMEOUT_SEC, _OK_C_RE)
        return line is not None

    def set_encoder_sign(self, sign: float) -> bool:
        sign_val = -1.0 if sign < 0.0 else 1.0
        ok = self.send_line(f"E{sign_val:.0f}", drain_after=False)
        if not ok:
            return False
        line = self._read_line_matching(ACK_TIMEOUT_SEC, _OK_E_RE)
        return line is not None

    def is_calibrated(self) -> bool:
        st = self.query_status()
        return st is not None and abs(st.counts_per_degree - BOOT_CPD) > 0.05

    def query_status(self) -> Optional[BaseStatus]:
        if not self.send_line("?", drain_after=False):
            return None
        line = self._read_line_matching(ACK_TIMEOUT_SEC, _BASE_STATUS_RE)
        if line is None:
            return None
        match = _BASE_STATUS_RE.match(line)
        if not match:
            return None
        return BaseStatus(
            encoder_count=int(match.group(1)),
            degrees=float(match.group(2)),
            counts_per_degree=float(match.group(3)),
            busy=match.group(4) == "1",
        )

    def run_bench_sweep(self) -> bool:
        ok = self.send_line("S", drain_after=False)
        time.sleep(5.0)
        self._drain_rx()
        self._last_pan = None
        self._last_tilt = None
        return ok

    def close(self, *, home_pan: float | None = None, home_tilt: float | None = None, skip_home: bool = False) -> None:
        if self._ser is not None:
            try:
                if self._ser.is_open:
                    if not skip_home and home_pan is not None and home_tilt is not None:
                        self.send_line(f"P{home_pan:.1f} T{home_tilt:.1f}", wait_ack=True)
                        time.sleep(0.25)
                    self.write_base_stop()
                    self._ser.close()
            except Exception:
                pass
        self._ser = None
        self._connected = False
