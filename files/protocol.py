"""
protocol.py — Wristband 2501 BLE Protocol
Handles packet building, CRC calculation and response parsing.
"""

import struct
from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime


# ─── BLE UUIDs ────────────────────────────────────────────────────────────────
SERVICE_UUID  = "0000fff0-0000-1000-8000-00805f9b34fb"
TX_UUID       = "0000fff6-0000-1000-8000-00805f9b34fb"  # App → Device
RX_UUID       = "0000fff7-0000-1000-8000-00805f9b34fb"  # Device → App

# ─── Command bytes ────────────────────────────────────────────────────────────
CMD_SET_TIME            = 0x01
CMD_GET_TIME            = 0x41
CMD_SET_USER_INFO       = 0x02
CMD_GET_USER_INFO       = 0x42
CMD_GET_BATTERY         = 0x13
CMD_GET_MAC             = 0x22
CMD_GET_VERSION         = 0x27
CMD_REAL_TIME_STEPS     = 0x09
CMD_HEALTH_MEASURE      = 0x28
CMD_GET_TOTAL_STEPS     = 0x51
CMD_GET_STEP_DETAIL     = 0x52
CMD_GET_SLEEP           = 0x53
CMD_GET_HEART_RATE      = 0x54
CMD_GET_SINGLE_HR       = 0x55
CMD_GET_BLOOD_OXYGEN    = 0x66
CMD_GET_HRV             = 0x56
CMD_GET_EXERCISE        = 0x5C
CMD_GET_PPI             = 0x64
CMD_SPORT_MODE          = 0x19
CMD_FACTORY_RESET       = 0x12
CMD_MCU_RESET           = 0x2E


# ─── CRC ──────────────────────────────────────────────────────────────────────
def calc_crc(data: bytes) -> int:
    """Sum first 15 bytes, take lower 8 bits."""
    return sum(data[:15]) & 0xFF


def build_packet(cmd: int, payload: bytes = b"") -> bytes:
    """
    Build a 16-byte packet: [cmd][payload padded to 14 bytes][CRC]
    """
    if len(payload) > 14:
        raise ValueError(f"Payload too long: {len(payload)} bytes (max 14)")
    padded = payload.ljust(14, b"\x00")
    packet = bytes([cmd]) + padded
    crc = calc_crc(packet)
    return packet + bytes([crc])


def verify_packet(data: bytes) -> bool:
    """
    Verify CRC of any packet (fixed 16-byte or variable-length).
    The CRC is always the last byte = sum(all preceding bytes) & 0xFF.
    """
    if len(data) < 2:
        return False
    expected = sum(data[:-1]) & 0xFF
    ok = data[-1] == expected
    if not ok:
        import logging
        logging.getLogger("wristband").debug(
            f"CRC fail: got {data[-1]:#04x}, expected {expected:#04x}, "
            f"len={len(data)}, cmd={data[0]:#04x}"
        )
    return ok


def is_error_response(cmd_sent: int, data: bytes) -> bool:
    """Error responses have bit7 set on the command byte."""
    return bool(data[0] & 0x80)


# ─── Packet builders ──────────────────────────────────────────────────────────
def pkt_set_time(dt: datetime = None, timezone_minutes: int = -300) -> bytes:
    """
    Set device time. timezone_minutes: e.g. UTC-5 = -300, UTC+8 = 480.
    """
    if dt is None:
        dt = datetime.now()

    def to_bcd(n: int) -> int:
        return ((n // 10) << 4) | (n % 10)

    year  = to_bcd(dt.year % 100)
    month = to_bcd(dt.month)
    day   = to_bcd(dt.day)
    hour  = to_bcd(dt.hour)
    minute = to_bcd(dt.minute)
    second = to_bcd(dt.second)

    tz = timezone_minutes & 0xFFFF
    t1 = tz & 0xFF
    t2 = (tz >> 8) & 0xFF

    payload = bytes([year, month, day, hour, minute, second,
                     0x00, 0x00, 0x00, t1, t2, 0x00, 0x00, 0x00])
    return build_packet(CMD_SET_TIME, payload)


def pkt_get_time() -> bytes:
    return build_packet(CMD_GET_TIME)


# ─── Auto measurement commands (0x2A / 0x2B) ──────────────────────────────────
def to_bcd(n: int) -> int:
    return ((n // 10) << 4) | (n % 10)

def pkt_set_auto_measure(
    sensor: int,          # 1=heart rate, 2=SpO2, 4=HRV
    enable: bool,
    start_hour: int = 0,  # 24h
    start_min: int = 0,
    end_hour: int = 23,
    end_min: int = 59,
    days: int = 0b1111110,  # Mon–Sat by default (bits 1-6), 0x7F = all week
    interval_minutes: int = 30,
) -> bytes:
    """
    Command 0x2A — Set automatic measurement schedule.

    sensor: 1=HR, 2=SpO2, 4=HRV  (matches II field in SDK)
    days bitmask:
        bit0=Sunday, bit1=Monday, bit2=Tuesday, bit3=Wednesday,
        bit4=Thursday, bit5=Friday, bit6=Saturday
    interval_minutes: how often to measure (Work Mode 2)
    """
    mode = 0x02  # Interval working mode during time period
    if not enable:
        mode = 0x00

    sh = to_bcd(start_hour)
    sm = to_bcd(start_min)
    eh = to_bcd(end_hour)
    em = to_bcd(end_min)

    # GG HH = interval in minutes, low byte first
    iv_lo = interval_minutes & 0xFF
    iv_hi = (interval_minutes >> 8) & 0xFF

    payload = bytes([mode, sh, sm, eh, em, days & 0x7F, iv_lo, iv_hi, sensor & 0xFF,
                     0x00, 0x00, 0x00, 0x00, 0x00])
    return build_packet(0x2A, payload)


def pkt_get_auto_measure(sensor: int) -> bytes:
    """Command 0x2B — Read current auto-measure schedule. sensor: 1=HR, 2=SpO2, 4=HRV"""
    return build_packet(0x2B, bytes([sensor]))


def pkt_get_battery() -> bytes:
    return build_packet(CMD_GET_BATTERY, bytes([0x99]))


def pkt_get_mac() -> bytes:
    return build_packet(CMD_GET_MAC)


def pkt_get_version() -> bytes:
    return build_packet(CMD_GET_VERSION)


def pkt_real_time_steps(enable: bool = True) -> bytes:
    return build_packet(CMD_REAL_TIME_STEPS, bytes([1 if enable else 0, 0x00]))


def pkt_start_heart_rate() -> bytes:
    """Start real-time heart rate measurement."""
    return build_packet(CMD_HEALTH_MEASURE, bytes([0x02, 0x01, 0x00]))


def pkt_stop_heart_rate() -> bytes:
    return build_packet(CMD_HEALTH_MEASURE, bytes([0x02, 0x00, 0x00]))


def pkt_start_spo2() -> bytes:
    """Start real-time SpO2 (blood oxygen) measurement."""
    return build_packet(CMD_HEALTH_MEASURE, bytes([0x03, 0x01, 0x00]))


def pkt_stop_spo2() -> bytes:
    return build_packet(CMD_HEALTH_MEASURE, bytes([0x03, 0x00, 0x00]))


def pkt_start_hrv() -> bytes:
    return build_packet(CMD_HEALTH_MEASURE, bytes([0x01, 0x01, 0x00]))


def pkt_get_ppi(mode: int = 0, id_high: int = 0, id_low: int = 0) -> bytes:
    """
    Command 0x64 — Get PPI (Peak-to-Peak Interval) data.
    mode: 0=latest, 1=at location, 2=continue, 99=delete
    Each record contains up to 56 intervals in milliseconds — one per heartbeat.
    """
    return build_packet(CMD_GET_PPI, bytes([mode, id_high, id_low]))


def pkt_get_total_steps() -> bytes:
    return build_packet(CMD_GET_TOTAL_STEPS, bytes([0x00]))


def pkt_get_step_detail(mode: int = 0, id_high: int = 0, id_low: int = 0) -> bytes:
    """mode: 0=latest, 1=at location, 2=continue"""
    return build_packet(CMD_GET_STEP_DETAIL, bytes([mode, id_high, id_low]))


def pkt_get_sleep(mode: int = 0, id_high: int = 0, id_low: int = 0) -> bytes:
    """mode: 0=latest, 1=at location, 2=continue"""
    return build_packet(CMD_GET_SLEEP, bytes([mode, id_high, id_low]))


def pkt_get_heart_rate_data(mode: int = 0, id_high: int = 0, id_low: int = 0) -> bytes:
    """mode: 0=latest, 1=at location, 2=continue"""
    return build_packet(CMD_GET_HEART_RATE, bytes([mode, id_high, id_low]))


def pkt_get_single_heart_rate(mode: int = 0x00, record_id: int = 0) -> bytes:
    """
    Command 0x55 — Read one scheduled heart-rate record.

    mode:
        0x00 = start history download (firmware streams newest first)
        0x01 = start at the specified relative position
        0x99 = delete stored records

    Positions are transmitted little-endian as ID1, ID2. On the observed
    firmware they are not durable IDs: the newest record is position zero.
    """
    if mode not in (0x00, 0x01, 0x99):
        raise ValueError(f"Unsupported 0x55 mode: {mode:#04x}")
    if not 0 <= record_id <= 0xFFFF:
        raise ValueError(f"Record ID out of range: {record_id}")

    id1 = record_id & 0xFF
    id2 = (record_id >> 8) & 0xFF
    return build_packet(CMD_GET_SINGLE_HR, bytes([mode, id1, id2]))


def pkt_get_blood_oxygen(mode: int = 0, id_high: int = 0, id_low: int = 0) -> bytes:
    """mode: 0=latest, 1=at location, 2=continue"""
    return build_packet(CMD_GET_BLOOD_OXYGEN, bytes([mode, id_high, id_low]))


def pkt_get_hrv(mode: int = 0, id_high: int = 0, id_low: int = 0) -> bytes:
    """mode: 0=latest, 1=at location, 2=continue"""
    return build_packet(CMD_GET_HRV, bytes([mode, id_high, id_low]))


# ─── Response parsers ─────────────────────────────────────────────────────────
def from_bcd(b: int) -> int:
    return ((b >> 4) * 10) + (b & 0x0F)


@dataclass
class BatteryResponse:
    level: int  # 0-100%


@dataclass
class TimeResponse:
    year: int; month: int; day: int
    hour: int; minute: int; second: int
    weekday: int
    timezone_minutes: int

    def as_datetime(self) -> str:
        return f"20{self.year:02d}-{self.month:02d}-{self.day:02d} {self.hour:02d}:{self.minute:02d}:{self.second:02d}"


@dataclass
class RealtimeStepsResponse:
    steps: int
    calories_kcal: float
    distance_km: float
    motion_time_seconds: int
    fast_motion_minutes: int
    heart_rate: int
    spo2: int


@dataclass
class HealthMeasureResponse:
    measure_type: int   # 1=CardioAnalysis, 2=HR, 3=SpO2
    heart_rate: int
    spo2: int
    # Fields only populated in mode 0x01 (CardioAnalysis), after ~30s warmup
    # Empirically verified: b04=unknown_index, b05=fatigue, b06=systolic_bp, b07=diastolic_bp
    unknown_b04: int = 0   # value=58 in test — purpose unclear, possibly stress index
    fatigue: int = 0        # 0-100 scale, b05
    systolic_bp: int = 0    # mmHg, b06 — e.g. 119
    diastolic_bp: int = 0   # mmHg, b07 — e.g. 64

    @property
    def type_name(self) -> str:
        return {
            1: "CardioAnalysis (HR+fatigue+BP)",
            2: "Heart Rate",
            3: "SpO2"
        }.get(self.measure_type, "Unknown")

    @property
    def bp_ready(self) -> bool:
        """True once the device has delivered BP/fatigue results (after ~30s)."""
        return self.systolic_bp > 0 and self.diastolic_bp > 0


@dataclass
class TotalStepsRecord:
    day_index: int
    date: str
    steps: int
    motion_time_seconds: int
    distance_km: float
    calories_kcal: float
    fast_motion_minutes: int


@dataclass
class StepDetailRecord:
    record_id: int
    timestamp: str
    steps: int
    calories_cal: int
    distance_km: float
    per_minute: list


@dataclass
class SleepRecord:
    record_id: int
    timestamp: str
    length: int
    quality: list   # per-minute sleep quality values


@dataclass
class HeartRateRecord:
    record_id: int
    timestamp: str
    values: list    # up to 15 per-minute values


@dataclass
class SingleHeartRateRecord:
    record_id: int
    timestamp: str
    heart_rate: int


@dataclass
class BloodOxygenRecord:
    record_id: int
    timestamp: str
    spo2: int


@dataclass
class HRVRecord:
    record_id: int
    timestamp: str
    # D1 — HRV value as computed by the device firmware (units/scale unknown,
    #       empirically observed range 31-59, consistent with ms but unconfirmed)
    hrv_device: int
    # D2 — empirically identical to fatigue in every record; SDK marks as "empty"
    # D3 — HR in bpm at moment of measurement; SDK marks as "empty" but populated
    heart_rate: int
    fatigue: int        # D4 — fatigue index, observed range 43-57
    systolic_bp: int    # D5 — mmHg, e.g. 112-119
    diastolic_bp: int   # D6 — mmHg, e.g. 62-64


@dataclass
class PPIRecord:
    record_id: int
    timestamp: str
    group_count: int        # total records in this group (CC field)
    group_item_id: int      # this item's index within the group (ID field)
    intervals_ms: list      # list of RR intervals in milliseconds
    # Derived metrics computed from intervals
    mean_rr_ms: float       # average interval
    min_rr_ms: int
    max_rr_ms: int
    rmssd_ms: float         # root mean square of successive differences (HRV proxy)


def parse_battery(data: bytes) -> Optional[BatteryResponse]:
    if len(data) < 16 or is_error_response(CMD_GET_BATTERY, data):
        return None
    return BatteryResponse(level=data[1])


def parse_time(data: bytes) -> Optional[TimeResponse]:
    if len(data) < 16 or is_error_response(CMD_GET_TIME, data):
        return None
    d = data
    tz = d[11] | (d[12] << 8)
    if tz > 32767:
        tz -= 65536
    return TimeResponse(
        year=from_bcd(d[1]), month=from_bcd(d[2]), day=from_bcd(d[3]),
        hour=from_bcd(d[4]), minute=from_bcd(d[5]), second=from_bcd(d[6]),
        weekday=d[7], timezone_minutes=tz
    )


def parse_realtime_steps(data: bytes) -> Optional[RealtimeStepsResponse]:
    if len(data) < 16 or data[0] != CMD_REAL_TIME_STEPS:
        return None
    steps    = data[1] | (data[2] << 8) | (data[3] << 16) | (data[4] << 24)
    cal_raw  = data[5] | (data[6] << 8) | (data[7] << 16) | (data[8] << 24)
    dist_raw = data[9] | (data[10] << 8) | (data[11] << 16) | (data[12] << 24)
    time_s   = data[13] | (data[14] << 8)  # simplified; full parse uses 4 bytes
    return RealtimeStepsResponse(
        steps=steps,
        calories_kcal=round(cal_raw / 100, 2),
        distance_km=round(dist_raw / 100, 2),
        motion_time_seconds=time_s,
        fast_motion_minutes=0,
        heart_rate=0,
        spo2=0
    )


def parse_health_measure(data: bytes) -> Optional[HealthMeasureResponse]:
    if len(data) < 16 or data[0] != CMD_HEALTH_MEASURE:
        return None
    measure_type = data[1]
    heart_rate   = data[2]
    b03          = data[3]
    b04          = data[4]
    b05          = data[5]
    b06          = data[6]
    b07          = data[7]

    if measure_type == 0x02:
        # HR-only mode: SpO2 field is always 0
        return HealthMeasureResponse(
            measure_type=measure_type,
            heart_rate=heart_rate,
            spo2=0,
        )

    elif measure_type == 0x03:
        # SpO2 mode: b03=SpO2, HR is also valid (byproduct of pulse detection)
        return HealthMeasureResponse(
            measure_type=measure_type,
            heart_rate=heart_rate,
            spo2=b03,
        )

    elif measure_type == 0x01:
        # CardioAnalysis mode: device streams HR continuously, then after ~30s
        # delivers a point-in-time result with fatigue + blood pressure.
        # Empirically verified layout (2026-03-11):
        #   b02 = heart_rate (bpm) — continuous, varies each packet
        #   b03 = always 0 (SpO2 not measured in this mode)
        #   b04 = unknown index (value=58) — possibly stress or HRV index
        #   b05 = fatigue index 0-100 (value=47 = moderate fatigue)
        #   b06 = systolic BP in mmHg (value=119)
        #   b07 = diastolic BP in mmHg (value=64)
        # Note: b02 is NOT real HRV in ms — it is HR in bpm despite SDK label
        return HealthMeasureResponse(
            measure_type=measure_type,
            heart_rate=heart_rate,
            spo2=0,
            unknown_b04=b04,
            fatigue=b05,
            systolic_bp=b06,
            diastolic_bp=b07,
        )

    return HealthMeasureResponse(
        measure_type=measure_type,
        heart_rate=heart_rate,
        spo2=0,
    )


def _parse_timestamp(data: bytes, offset: int) -> str:
    """
    Parse YY MM DD HH mm SS from offset.
    All bytes are BCD encoded (e.g. 0x26 = year 26 = 2026, not decimal 38).
    """
    def bcd(b): return ((b >> 4) * 10) + (b & 0x0F)
    d = data
    return (f"20{bcd(d[offset]):02d}-{bcd(d[offset+1]):02d}-{bcd(d[offset+2]):02d} "
            f"{bcd(d[offset+3]):02d}:{bcd(d[offset+4]):02d}:{bcd(d[offset+5]):02d}")


def parse_total_steps(data: bytes) -> Optional[TotalStepsRecord]:
    if len(data) < 27 or data[0] != CMD_GET_TOTAL_STEPS:
        return None
    idx   = data[1]
    date  = f"20{data[2]:02d}-{data[3]:02d}-{data[4]:02d}"
    steps = data[5] | (data[6] << 8) | (data[7] << 16) | (data[8] << 24)
    t_sec = data[9] | (data[10] << 8) | (data[11] << 16) | (data[12] << 24)
    dist  = (data[13] | (data[14] << 8) | (data[15] << 16) | (data[16] << 24)) / 100
    cal   = (data[17] | (data[18] << 8) | (data[19] << 16) | (data[20] << 24)) / 100
    fast  = data[25] | (data[26] << 8)
    return TotalStepsRecord(idx, date, steps, t_sec, dist, cal, fast)


def parse_step_detail(data: bytes) -> Optional[StepDetailRecord]:
    if len(data) < 25 or data[0] != CMD_GET_STEP_DETAIL:
        return None
    rid  = data[1] | (data[2] << 8)
    ts   = _parse_timestamp(data, 3)
    s    = data[9] | (data[10] << 8)
    k    = data[11] | (data[12] << 8)
    d    = (data[13] | (data[14] << 8)) / 100
    pm   = list(data[15:25])
    return StepDetailRecord(rid, ts, s, k, d, pm)


def parse_sleep(data: bytes) -> Optional[SleepRecord]:
    if len(data) < 11 or data[0] != CMD_GET_SLEEP:
        return None
    rid = data[1] | (data[2] << 8)
    ts  = _parse_timestamp(data, 3)
    ln  = data[9]
    quality = list(data[10:10 + ln])
    return SleepRecord(rid, ts, ln, quality)


def parse_heart_rate_data(data: bytes) -> Optional[HeartRateRecord]:
    if len(data) < 21 or data[0] != CMD_GET_HEART_RATE:
        return None
    rid    = data[1] | (data[2] << 8)
    ts     = _parse_timestamp(data, 3)
    values = [v for v in data[9:24] if v > 0]
    return HeartRateRecord(rid, ts, values)


SINGLE_HR_RECORD_SIZE = 10


def parse_single_heart_rate(
    data: bytes,
    offset: int = 0,
) -> Optional[SingleHeartRateRecord]:
    """
    Parse one scheduled HR record:
    [0x55][ID1][ID2][YY][MM][DD][HH][mm][SS][HR].

    These records do not include a per-record CRC.
    """
    if len(data) < offset + SINGLE_HR_RECORD_SIZE:
        return None
    d = data[offset:offset + SINGLE_HR_RECORD_SIZE]
    if d[0] != CMD_GET_SINGLE_HR:
        return None
    if any(
        (value >> 4) > 9 or (value & 0x0F) > 9
        for value in d[3:9]
    ):
        return None

    try:
        timestamp = datetime(
            2000 + from_bcd(d[3]),
            from_bcd(d[4]),
            from_bcd(d[5]),
            from_bcd(d[6]),
            from_bcd(d[7]),
            from_bcd(d[8]),
        ).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None

    return SingleHeartRateRecord(
        record_id=d[1] | (d[2] << 8),
        timestamp=timestamp,
        heart_rate=d[9],
    )


def parse_single_heart_rate_multi(data: bytes) -> list:
    """Extract all concatenated 10-byte 0x55 records from a notification."""
    records = []
    offset = 0
    while offset + SINGLE_HR_RECORD_SIZE <= len(data):
        if data[offset] != CMD_GET_SINGLE_HR:
            offset += 1
            continue
        record = parse_single_heart_rate(data, offset)
        if record:
            records.append(record)
            offset += SINGLE_HR_RECORD_SIZE
        else:
            offset += 1
    return records


def parse_blood_oxygen(data: bytes) -> Optional[BloodOxygenRecord]:
    if len(data) < 10 or data[0] != CMD_GET_BLOOD_OXYGEN:
        return None
    rid  = data[1] | (data[2] << 8)
    ts   = _parse_timestamp(data, 3)
    spo2 = data[9]
    return BloodOxygenRecord(rid, ts, spo2)


# Each 0x56 record is exactly 15 bytes — no per-record CRC.
# The device concatenates all records into one BLE packet (same pattern as PPI).
# Layout verified empirically 2026-03-11:
#   [0x56][ID1][ID2][YY][MM][DD][HH][mm][SS][D1][D2][D3][D4][D5][D6]
#    0     1    2    3   4   5   6   7   8   9   10  11  12  13  14
#   D1=hrv_device  D2=fatigue_copy(empty per SDK)  D3=heart_rate(empty per SDK)
#   D4=fatigue     D5=systolic_bp                  D6=diastolic_bp
HRV_RECORD_SIZE = 15


def parse_hrv(data: bytes, offset: int = 0) -> Optional[HRVRecord]:
    """Parse one 15-byte HRV record at given offset."""
    if len(data) < offset + HRV_RECORD_SIZE:
        return None
    d = data[offset:]
    if d[0] != CMD_GET_HRV or d[1] == 0xFF:
        return None
    rid = d[1] | (d[2] << 8)
    ts  = _parse_timestamp(d, 3)
    return HRVRecord(
        record_id=rid,
        timestamp=ts,
        hrv_device=d[9],    # D1 — device HRV value
        heart_rate=d[11],   # D3 — HR in bpm (SDK says empty, device populates it)
        fatigue=d[12],      # D4 — fatigue index
        systolic_bp=d[13],  # D5 — mmHg
        diastolic_bp=d[14], # D6 — mmHg
    )


def parse_hrv_multi(data: bytes) -> list:
    """
    Extract ALL HRV records from a single BLE packet.
    The device concatenates 15-byte records without separators,
    terminated by [0x56, 0xFF].
    """
    records = []
    offset = 0
    while offset + HRV_RECORD_SIZE <= len(data):
        if data[offset] != CMD_GET_HRV:
            offset += 1
            continue
        if data[offset + 1] == 0xFF:  # terminator
            break
        rec = parse_hrv(data, offset)
        if rec:
            records.append(rec)
        offset += HRV_RECORD_SIZE
    return records


# Each PPI record inside a BLE packet is exactly 115 bytes:
# [cmd(1)][ID1(1)][ID2(1)][timestamp(6)][CC(1)][group_item(1)][intervals(104)] = 115 bytes
# The device sends 52 intervals (104 bytes) per record, not the 56 (112 bytes) the SDK
# mentions as maximum. No per-record CRC — records are concatenated to fill BLE MTU.
PPI_RECORD_SIZE = 115
PPI_INTERVAL_BYTES = 104   # 52 x 2-byte little-endian intervals


def _parse_single_ppi(data: bytes, offset: int = 0) -> Optional[PPIRecord]:
    """Parse one 123-byte PPI record starting at `offset`."""
    if len(data) < offset + PPI_RECORD_SIZE:
        return None
    if data[offset] != CMD_GET_PPI:
        return None

    d           = data[offset:]
    rid         = d[1] | (d[2] << 8)
    ts          = _parse_timestamp(d, 3)
    group_count = d[9]
    group_item  = d[10]

    # 112 bytes of interval data starting at byte 11 = 56 x 2-byte little-endian values
    raw_pairs = d[11:11 + PPI_INTERVAL_BYTES]
    intervals = []
    for i in range(0, PPI_INTERVAL_BYTES - 1, 2):
        val = raw_pairs[i] | (raw_pairs[i + 1] << 8)
        if val == 0:
            break
        # Valid RR interval: 300–2000ms (30–200 bpm)
        if 300 <= val <= 2000:
            intervals.append(val)

    if not intervals:
        return None

    mean_rr = round(sum(intervals) / len(intervals), 1)
    min_rr  = min(intervals)
    max_rr  = max(intervals)

    if len(intervals) >= 2:
        diffs = [intervals[i+1] - intervals[i] for i in range(len(intervals) - 1)]
        rmssd = round((sum(d*d for d in diffs) / len(diffs)) ** 0.5, 1)
    else:
        rmssd = 0.0

    return PPIRecord(
        record_id=rid,
        timestamp=ts,
        group_count=group_count,
        group_item_id=group_item,
        intervals_ms=intervals,
        mean_rr_ms=mean_rr,
        min_rr_ms=min_rr,
        max_rr_ms=max_rr,
        rmssd_ms=rmssd,
    )


def parse_ppi(data: bytes) -> Optional[PPIRecord]:
    """
    Parse PPI packet from command 0x64.

    The device packs multiple 123-byte records into one BLE notification
    (no per-packet CRC). This function returns the FIRST record only.
    Use parse_ppi_multi() to extract all records from a single packet.
    """
    return _parse_single_ppi(data, offset=0)


def parse_ppi_multi(data: bytes) -> list:
    """
    Extract ALL PPI records from a single BLE packet.
    The device concatenates records without separators — each is exactly
    123 bytes starting with 0x64.
    """
    records = []
    offset = 0
    while offset + PPI_RECORD_SIZE <= len(data):
        if data[offset] != CMD_GET_PPI:
            offset += 1
            continue
        rec = _parse_single_ppi(data, offset)
        if rec:
            records.append(rec)
        offset += PPI_RECORD_SIZE
    return records
