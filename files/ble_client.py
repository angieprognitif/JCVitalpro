"""
ble_client.py — Async BLE client for Wristband 2501
Uses the `bleak` library for Linux BLE communication.
"""

import asyncio
import logging
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Callable

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError

from protocol import (
    pkt_set_auto_measure, pkt_get_auto_measure,
    pkt_get_ppi,
    parse_ppi, parse_ppi_multi, PPIRecord,
    CMD_GET_PPI, PPI_RECORD_SIZE, PPI_INTERVAL_BYTES,
    build_packet,
    SERVICE_UUID, TX_UUID, RX_UUID,
    verify_packet, is_error_response,
    pkt_set_time, pkt_get_time, pkt_get_battery, pkt_get_mac, pkt_get_version,
    pkt_real_time_steps, pkt_start_heart_rate, pkt_stop_heart_rate,
    pkt_start_spo2, pkt_stop_spo2, pkt_start_hrv,
    pkt_get_total_steps, pkt_get_step_detail,
    pkt_get_sleep, pkt_get_heart_rate_data, pkt_get_single_heart_rate,
    pkt_get_blood_oxygen, pkt_get_hrv,
    parse_battery, parse_time, parse_realtime_steps, parse_health_measure,
    parse_total_steps, parse_step_detail, parse_sleep,
    parse_heart_rate_data, parse_single_heart_rate_multi,
    parse_blood_oxygen, parse_hrv, parse_hrv_multi,
    CMD_HEALTH_MEASURE, CMD_REAL_TIME_STEPS,
    CMD_GET_TOTAL_STEPS, CMD_GET_STEP_DETAIL, CMD_GET_SLEEP,
    CMD_GET_HEART_RATE, CMD_GET_SINGLE_HR,
    CMD_GET_BLOOD_OXYGEN, CMD_GET_HRV, HRV_RECORD_SIZE,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("logs/wristband.log"),
    ]
)
log = logging.getLogger("wristband")


class WristbandClient:
    """
    High-level async client for the 2501 wristband.

    Usage:
        async with WristbandClient(address="XX:XX:XX:XX:XX:XX") as wb:
            battery = await wb.get_battery()
            print(battery)
    """

    RESPONSE_TIMEOUT = 8.0  # seconds to wait for a device response
    SCHEDULED_HR_INTERVAL_MINUTES = 5
    SCHEDULED_HR_FIRST_WAIT_SECONDS = 60
    SCHEDULED_HR_POLL_SECONDS = 60
    SCHEDULED_HR_BURST_SILENCE_SECONDS = 0.75

    def __init__(self, address: str, data_dir: str = "data"):
        self.address   = address
        self.data_dir  = Path(data_dir)
        self.data_dir.mkdir(exist_ok=True)
        self._scheduled_hr_records_file = self.data_dir / "scheduled_hr_records.jsonl"
        self._scheduled_hr_state_file = self.data_dir / "scheduled_hr_state.json"
        self._scheduled_hr_keys: Optional[set[tuple[str, str]]] = None
        self._client: Optional[BleakClient] = None
        self._response_queue: asyncio.Queue = asyncio.Queue()
        self._notify_handlers: dict[int, Callable] = {}
        self._collecting = False

    # ── Connection ─────────────────────────────────────────────────────────────
    async def connect(self):
        log.info(f"Connecting to {self.address}...")
        self._client = BleakClient(self.address, timeout=15.0)
        await self._client.connect()
        await self._client.start_notify(RX_UUID, self._on_notify)
        log.info("Connected ✓  (notifications enabled)")

    async def disconnect(self):
        if self._client and self._client.is_connected:
            await self._client.stop_notify(RX_UUID)
            await self._client.disconnect()
            log.info("Disconnected.")

    async def _reconnect_until_connected(self, retry_seconds: int = 10) -> None:
        """Reconnect indefinitely; cancellation stops the retry loop."""
        attempt = 0
        while True:
            attempt += 1
            try:
                if self._client and self._client.is_connected:
                    return
                if self._client:
                    try:
                        await self._client.disconnect()
                    except Exception:
                        pass
                self._client = None
                log.info(f"Reconnecting to wristband (attempt {attempt})...")
                await self.connect()
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning(
                    f"Reconnect attempt {attempt} failed: {exc}. "
                    f"Retrying in {retry_seconds}s."
                )
                await asyncio.sleep(retry_seconds)

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *_):
        await self.disconnect()

    # ── Low-level send / receive ───────────────────────────────────────────────
    def _on_notify(self, _sender, data: bytearray):
        """Called whenever the device sends a notification."""
        raw = bytes(data)
        if not raw:
            return

        # Firmware 2501 streams 0x55 history as a burst of 10-byte records.
        # These packets have no packet CRC, and logging their full payload at
        # INFO produces thousands of bytes per query.
        if raw[0] == CMD_GET_SINGLE_HR:
            if len(raw) == 2 and raw[1] == 0xFF:
                log.info("← No scheduled HR data on device")
            else:
                record_count = len(parse_single_heart_rate_multi(raw))
                log.debug(
                    f"← RX [{len(raw)}b] cmd=0x55 "
                    f"records={record_count}"
                )
            self._response_queue.put_nowait(raw)
            return

        # Always log raw bytes at INFO so we can diagnose CRC issues
        expected_crc = sum(raw[:-1]) & 0xFF
        log.info(
            f"← RX [{len(raw)}b] cmd={raw[0]:#04x}  "
            f"crc_recv={raw[-1]:#04x}  crc_calc={expected_crc:#04x}  "
            f"raw={raw.hex(' ')}"
        )
        # 2-byte response: [cmd, 0xFF] = "no data available" — valid, not a CRC error
        if len(raw) == 2 and raw[1] == 0xFF:
            log.info(f"← No data on device for cmd={raw[0]:#04x}")
            self._response_queue.put_nowait(raw)
            return

        # Stored-data packets can contain concatenated records with no packet CRC.
        # Pass them directly to the response queue.
        if raw[0] == CMD_GET_PPI and len(raw) >= PPI_RECORD_SIZE:
            self._response_queue.put_nowait(raw)
            return
        if raw[0] == CMD_GET_HRV and len(raw) >= HRV_RECORD_SIZE:
            self._response_queue.put_nowait(raw)
            return
        if not verify_packet(raw):
            log.warning(
                f"CRC mismatch on cmd={raw[0]:#04x} len={len(raw)} — "
                f"got {raw[-1]:#04x}, expected {expected_crc:#04x}. "
                f"Full packet: {raw.hex(' ')}"
            )
            return
        cmd = raw[0]
        # For 0x28 (health measure) packets dispatch by (cmd, measure_type)
        # so HR (type=0x02) and SpO2 (type=0x03) go to their own handlers.
        if cmd == CMD_HEALTH_MEASURE and len(raw) > 1:
            key = (cmd, raw[1])
            if key in self._notify_handlers:
                self._notify_handlers[key](raw)
                return
        # Generic dispatch
        if cmd in self._notify_handlers:
            self._notify_handlers[cmd](raw)
        else:
            self._response_queue.put_nowait(raw)

    async def _send(self, packet: bytes) -> bytes:
        """Send a packet and wait for the response."""
        if not self._client or not self._client.is_connected:
            raise RuntimeError("Not connected")
        # Clear stale responses
        while not self._response_queue.empty():
            self._response_queue.get_nowait()

        log.debug(f"→ TX: {packet.hex(' ')}")
        await self._client.write_gatt_char(TX_UUID, packet, response=False)

        try:
            response = await asyncio.wait_for(
                self._response_queue.get(),
                timeout=self.RESPONSE_TIMEOUT
            )
            return response
        except asyncio.TimeoutError:
            raise TimeoutError(f"No response within {self.RESPONSE_TIMEOUT}s")

    # ── Device info ────────────────────────────────────────────────────────────
    async def get_battery(self) -> dict:
        raw = await self._send(pkt_get_battery())
        result = parse_battery(raw)
        if result:
            info = {"battery_percent": result.level, "timestamp": _now()}
            log.info(f"🔋 Battery: {result.level}%")
            return info
        return {"error": "Failed to read battery"}

    async def get_time(self) -> dict:
        raw = await self._send(pkt_get_time())
        result = parse_time(raw)
        if result:
            return {"device_time": result.as_datetime(), "weekday": result.weekday,
                    "timezone_minutes": result.timezone_minutes}
        return {"error": "Failed to read time"}

    async def sync_time(self, timezone_minutes: int = -300) -> bool:
        """Sync device time with current PC time. Default UTC-5 (Colombia)."""
        raw = await self._send(pkt_set_time(datetime.now(), timezone_minutes))
        ok = raw[0] == 0x01
        log.info(f"⏰ Time sync: {'OK' if ok else 'FAILED'}")
        return ok

    # ── Health data (stored records) ───────────────────────────────────────────
    async def get_all_heart_rate(self) -> list:
        """Read all stored heart rate records."""
        return await self._read_all_records(
            first_pkt=pkt_get_heart_rate_data(0),
            cont_pkt_fn=lambda: pkt_get_heart_rate_data(2),
            parser=parse_heart_rate_data,
            label="Heart Rate"
        )

    async def get_scheduled_heart_rate_history(self) -> list:
        """
        Read the complete 0x55 history burst.

        On the observed 2501 firmware, mode 0x00 streams the complete history
        automatically in 240-byte notifications. IDs are positions relative to
        the newest record and shift whenever a new measurement is added.
        """
        if not self._client or not self._client.is_connected:
            raise RuntimeError("Not connected")

        while not self._response_queue.empty():
            self._response_queue.get_nowait()

        packet = pkt_get_single_heart_rate(mode=0x00)
        log.debug(f"→ TX: {packet.hex(' ')}")
        await self._client.write_gatt_char(TX_UUID, packet, response=False)

        records = []
        packet_count = 0
        first_packet = True
        while True:
            timeout = (
                self.RESPONSE_TIMEOUT
                if first_packet
                else self.SCHEDULED_HR_BURST_SILENCE_SECONDS
            )
            try:
                raw = await asyncio.wait_for(
                    self._response_queue.get(),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                if first_packet:
                    raise TimeoutError(
                        f"No response for command 0x55 within "
                        f"{self.RESPONSE_TIMEOUT}s"
                    )
                break

            if not raw or raw[0] != CMD_GET_SINGLE_HR:
                continue
            first_packet = False
            if self._is_no_data(raw):
                break

            packet_count += 1
            records.extend(
                _dataclass_to_dict(record)
                for record in parse_single_heart_rate_multi(raw)
            )

        if records:
            log.info(
                f"0x55 history received: {len(records)} records in "
                f"{packet_count} packets; newest={records[0]['timestamp']}, "
                f"oldest={records[-1]['timestamp']}"
            )
        else:
            log.info("No scheduled HR record is available yet.")
        return records

    def _load_scheduled_hr_state(self) -> Optional[dict]:
        if not self._scheduled_hr_state_file.exists():
            return None
        try:
            state = json.loads(self._scheduled_hr_state_file.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            log.warning(f"Could not read scheduled HR checkpoint: {exc}")
            return None

        if state.get("device_address") != self.address:
            return None
        if not isinstance(state.get("last_timestamp"), str):
            return None
        return state

    def _load_scheduled_hr_keys(self) -> set[tuple[str, str]]:
        if self._scheduled_hr_keys is not None:
            return self._scheduled_hr_keys

        keys = set()
        if self._scheduled_hr_records_file.exists():
            try:
                with self._scheduled_hr_records_file.open() as records_file:
                    for line_number, line in enumerate(records_file, start=1):
                        try:
                            record = json.loads(line)
                            keys.add((
                                record["device_address"],
                                record["timestamp"],
                            ))
                        except (KeyError, TypeError, json.JSONDecodeError):
                            log.warning(
                                f"Ignoring malformed scheduled HR JSONL line "
                                f"{line_number}"
                            )
            except OSError as exc:
                log.warning(f"Could not load scheduled HR records: {exc}")

        self._scheduled_hr_keys = keys
        return keys

    def _write_scheduled_hr_state(self, record: dict) -> None:
        state = {
            "device_address": self.address,
            "last_timestamp": record["timestamp"],
            "source_record_id": record["record_id"],
            "updated_at": _now(),
        }
        temp_file = self._scheduled_hr_state_file.with_suffix(".json.tmp")
        with temp_file.open("w") as state_file:
            json.dump(state, state_file, indent=2)
            state_file.write("\n")
            state_file.flush()
            os.fsync(state_file.fileno())
        os.replace(temp_file, self._scheduled_hr_state_file)

    def _persist_scheduled_hr_record(self, record: dict) -> bool:
        """
        Persist one record before advancing the checkpoint.

        Returns True when a new JSONL line was appended, False when the record
        was already present. The checkpoint is updated in both cases.
        """
        key = (self.address, record["timestamp"])
        keys = self._load_scheduled_hr_keys()
        appended = key not in keys

        if appended:
            stored_record = {
                "device_address": self.address,
                "record_id": record["record_id"],
                "timestamp": record["timestamp"],
                "heart_rate": record["heart_rate"],
                "captured_at": _now(),
            }
            with self._scheduled_hr_records_file.open("a") as records_file:
                records_file.write(
                    json.dumps(stored_record, separators=(",", ":")) + "\n"
                )
                records_file.flush()
                os.fsync(records_file.fileno())
            keys.add(key)

        self._write_scheduled_hr_state(record)
        return appended

    async def collect_new_scheduled_heart_rate_records(
        self,
        minimum_timestamp: Optional[datetime] = None,
    ) -> list:
        """
        Read the 0x55 history burst and persist records newer than the checkpoint.

        Timestamp is the durable identity. The firmware recalculates IDs as
        relative positions (newest record is always ID 0), so IDs cannot be
        used to detect gaps across queries.
        """
        history = await self.get_scheduled_heart_rate_history()
        if not history:
            return []

        state = self._load_scheduled_hr_state()
        cutoff = minimum_timestamp
        if state is not None:
            try:
                cutoff = datetime.strptime(
                    state["last_timestamp"], "%Y-%m-%d %H:%M:%S"
                )
            except ValueError:
                log.warning("Invalid scheduled HR checkpoint timestamp.")
                return []

        parsed = {}
        for record in history:
            try:
                record_dt = datetime.strptime(
                    record["timestamp"], "%Y-%m-%d %H:%M:%S"
                )
            except ValueError:
                continue
            parsed.setdefault(record_dt, record)

        if not parsed:
            log.warning("0x55 history did not contain valid timestamps.")
            return []

        if cutoff is None:
            cutoff = max(parsed) - timedelta(microseconds=1)

        new_records = [
            (record_dt, record)
            for record_dt, record in parsed.items()
            if record_dt > cutoff
        ]
        new_records.sort(key=lambda item: item[0])

        collected = []
        for _, record in new_records:
            if self._persist_scheduled_hr_record(record):
                collected.append(record)
                log.info(
                    f"Scheduled HR [{record['timestamp']}] "
                    f"{record['heart_rate']} bpm "
                    f"(source ID {record['record_id']})"
                )

        if not collected:
            newest_timestamp = max(parsed).strftime("%Y-%m-%d %H:%M:%S")
            log.info(
                f"No new scheduled HR records; newest on device is "
                f"{newest_timestamp}."
            )
        return collected

    async def get_all_blood_oxygen(self) -> list:
        """Read all stored SpO2 records."""
        return await self._read_all_records(
            first_pkt=pkt_get_blood_oxygen(0),
            cont_pkt_fn=lambda: pkt_get_blood_oxygen(2),
            parser=parse_blood_oxygen,
            label="SpO2"
        )

    async def get_all_sleep(self) -> list:
        """Read all stored sleep records."""
        return await self._read_all_records(
            first_pkt=pkt_get_sleep(0),
            cont_pkt_fn=lambda: pkt_get_sleep(2),
            parser=parse_sleep,
            label="Sleep"
        )

    async def get_all_step_detail(self) -> list:
        """Read all stored step detail records."""
        return await self._read_all_records(
            first_pkt=pkt_get_step_detail(0),
            cont_pkt_fn=lambda: pkt_get_step_detail(2),
            parser=parse_step_detail,
            label="Step Detail"
        )

    async def get_total_activity(self) -> list:
        """Read daily totals (steps, distance, calories) for up to 30 days."""
        return await self._read_all_records(
            first_pkt=pkt_get_total_steps(),
            cont_pkt_fn=lambda: pkt_get_total_steps(),
            parser=parse_total_steps,
            label="Total Activity"
        )

    async def get_all_hrv(self) -> list:
        """
        Read all stored HRV records (0x56).
        The device sends all records concatenated in one BLE packet (15 bytes each),
        terminated by [0x56, 0xFF]. No per-packet CRC.

        Each record contains:
          - hrv_device: HRV value as computed by device firmware
          - heart_rate: HR at time of measurement (bpm)
          - fatigue:    fatigue index
          - systolic_bp / diastolic_bp: blood pressure (mmHg)
        """
        import dataclasses

        async def fetch(pkt):
            while not self._response_queue.empty():
                self._response_queue.get_nowait()
            await self._client.write_gatt_char(TX_UUID, pkt, response=False)
            try:
                return await asyncio.wait_for(
                    self._response_queue.get(),
                    timeout=self.RESPONSE_TIMEOUT
                )
            except asyncio.TimeoutError:
                return None

        raw = await fetch(pkt_get_hrv(0))
        if raw is None or self._is_no_data(raw):
            log.info("No HRV records on device.")
            return []

        records = []
        batch = parse_hrv_multi(raw)
        for rec in batch:
            records.append(dataclasses.asdict(rec))
            log.info(
                f"  🧠 HRV #{rec.record_id} [{rec.timestamp}]  "
                f"HRV={rec.hrv_device}  HR={rec.heart_rate}bpm  "
                f"fatigue={rec.fatigue}  BP={rec.systolic_bp}/{rec.diastolic_bp}mmHg"
            )

        log.info(f"✅ HRV: {len(records)} records retrieved")
        return records

    @staticmethod
    def _is_no_data(raw: bytes) -> bool:
        """Device signals no data as [cmd, 0xFF] (2 bytes) or error byte (bit7 set)."""
        if len(raw) == 2 and raw[1] == 0xFF:
            return True
        if len(raw) >= 1 and (raw[0] & 0x80):
            return True
        return False

    async def _read_all_records(self, first_pkt, cont_pkt_fn, parser, label) -> list:
        """Generic paginated record reader."""
        records = []
        seen_ids = set()

        raw = await self._send(first_pkt)
        if self._is_no_data(raw):
            log.info(f"No {label} data on device.")
            return records

        while True:
            obj = parser(raw)
            if obj is None:
                break
            rid = getattr(obj, "record_id", None)
            if rid in seen_ids:
                break
            if rid is not None:
                seen_ids.add(rid)

            records.append(_dataclass_to_dict(obj))
            log.info(f"  📦 {label} record #{rid}: {_dataclass_to_dict(obj)}")

            # Ask for next
            try:
                raw = await self._send(cont_pkt_fn())
                if self._is_no_data(raw):  # [cmd,0xFF] or error bit
                    break
            except TimeoutError:
                break

        log.info(f"✅ {label}: {len(records)} records retrieved")
        return records

    # ── Real-time measurements ─────────────────────────────────────────────────
    async def realtime_heart_rate(self, duration_seconds: int = 30) -> list:
        """Start real-time HR measurement, collect for N seconds."""
        return await self._realtime_measure(
            start_pkt=pkt_start_heart_rate(),
            stop_pkt=pkt_stop_heart_rate(),
            cmd_byte=CMD_HEALTH_MEASURE,
            measure_type=0x02,
            parser=parse_health_measure,
            value_key="heart_rate",
            duration=duration_seconds,
            label="HR"
        )

    async def realtime_spo2(self, duration_seconds: int = 30,
                             warmup_seconds: int = 5) -> list:
        """
        Real-time SpO2 + HR measurement.

        The optical sensor needs a warmup period before SpO2 stabilizes.
        We start HR mode first for `warmup_seconds`, then switch to SpO2.
        Both HR and SpO2 are returned from the SpO2 phase (the device
        provides HR for free when computing SpO2).
        """
        if warmup_seconds > 0:
            log.info(f"🌡  Warming up sensor ({warmup_seconds}s HR mode)...")
            await self._client.write_gatt_char(
                TX_UUID, pkt_start_heart_rate(), response=False
            )
            await asyncio.sleep(warmup_seconds)
            await self._client.write_gatt_char(
                TX_UUID, pkt_stop_heart_rate(), response=False
            )
            await asyncio.sleep(0.3)  # brief pause before switching mode
            log.info("✅ Sensor warm — starting SpO2 measurement")

        return await self._realtime_measure(
            start_pkt=pkt_start_spo2(),
            stop_pkt=pkt_stop_spo2(),
            cmd_byte=CMD_HEALTH_MEASURE,
            measure_type=0x03,
            parser=parse_health_measure,
            value_key="spo2",
            duration=duration_seconds,
            label="SpO2"
        )

    async def realtime_hrv(self, duration_seconds: int = 90) -> list:
        """
        CardioAnalysis mode (0x28 01) — measures HR continuously then delivers
        a point-in-time result after ~30s with:
          - hrv_device: HRV value as computed by device firmware (same scale as 0x56)
          - heart_rate: HR in bpm (continuous, every packet)
          - fatigue:    fatigue index (appears after ~30s)
          - systolic_bp / diastolic_bp: blood pressure mmHg (appears after ~30s)

        Verified byte layout (2026-03-11):
          raw[2]  = heart_rate (bpm, every packet)
          raw[4]  = unknown index (value ~58)
          raw[5]  = fatigue
          raw[6]  = systolic_bp
          raw[7]  = diastolic_bp
          raw[9]  = hrv_device  ← same field as D1 in 0x56 records

        Minimum recommended duration: 60s (device needs ~30s to converge).
        """
        if duration_seconds < 60:
            log.warning(
                f"CardioAnalysis requested for only {duration_seconds}s — "
                f"recommend at least 60s; device needs ~30s to deliver HRV/BP/fatigue"
            )

        log.info("🌡  Warming up sensor (5s HR mode)...")
        await self._client.write_gatt_char(
            TX_UUID, pkt_start_heart_rate(), response=False
        )
        await asyncio.sleep(5)
        await self._client.write_gatt_char(
            TX_UUID, pkt_stop_heart_rate(), response=False
        )
        await asyncio.sleep(0.3)

        samples = []
        handler_key = (CMD_HEALTH_MEASURE, 0x01)

        def handler(raw: bytes):
            ts      = datetime.now().isoformat(timespec="milliseconds")
            elapsed = round((datetime.now() - start_time).total_seconds(), 1)

            hr          = raw[2]
            hrv_device  = raw[4]   # verified: 0x38=56ms, consistent with 0x56 records
            fatigue     = raw[5]
            systolic    = raw[6]
            diastolic   = raw[7]
            result_ready = systolic > 0 and diastolic > 0

            if result_ready:
                log.info(
                    f"  ✅ [{elapsed:6.1f}s] HR={hr}bpm  "
                    f"HRV={hrv_device}  "
                    f"BP={systolic}/{diastolic}mmHg  "
                    f"fatigue={fatigue}"
                )
            else:
                log.info(
                    f"  🫀 [{elapsed:6.1f}s] HR={hr}bpm  "
                    f"(waiting for HRV/BP/fatigue...)"
                )

            samples.append({
                "timestamp":    ts,
                "elapsed_s":    elapsed,
                "heart_rate":   hr,
                "hrv_device":   hrv_device,
                "fatigue":      fatigue,
                "systolic_bp":  systolic,
                "diastolic_bp": diastolic,
                "result_ready": result_ready,
            })

        # Device stops sending on its own once it has computed the result (~60s).
        # We poll every second and exit early if no new packet arrives within
        # SILENCE_TIMEOUT seconds after the result is ready.
        SILENCE_TIMEOUT = 5   # seconds of silence = device finished
        last_packet_time = [datetime.now()]  # mutable ref for closure
        done_event = asyncio.Event()

        original_handler = handler

        def handler_with_watchdog(raw: bytes):
            last_packet_time[0] = datetime.now()
            original_handler(raw)

        start_time = datetime.now()
        self._notify_handlers[handler_key] = handler_with_watchdog
        await self._client.write_gatt_char(
            TX_UUID, pkt_start_hrv(), response=False
        )
        log.info(
            f"▶ CardioAnalysis started (max {duration_seconds}s) — "
            f"HR streams now, HRV+BP+fatigue appear after ~30s, "
            f"stops automatically once device finishes..."
        )

        # Wait loop — exit immediately on first valid result
        deadline = start_time.timestamp() + duration_seconds
        while datetime.now().timestamp() < deadline:
            await asyncio.sleep(0.1)
            if any(s["result_ready"] for s in samples):
                first = next(s for s in samples if s["result_ready"])
                log.info(
                    f"  ✅ First valid result at t={first['elapsed_s']}s — stopping."
                )
                break

        await self._client.write_gatt_char(
            TX_UUID, build_packet(CMD_HEALTH_MEASURE, bytes([0x01, 0x00, 0x00])),
            response=False
        )
        self._notify_handlers.pop(handler_key, None)
        log.info(f"⏹ CardioAnalysis stopped. {len(samples)} packets received.")

        final = [s for s in samples if s["result_ready"]]
        if final:
            last = final[-1]
            log.info(
                f"  ✅ Final result — "
                f"HRV={last['hrv_device']}  "
                f"HR={last['heart_rate']}bpm  "
                f"BP={last['systolic_bp']}/{last['diastolic_bp']}mmHg  "
                f"fatigue={last['fatigue']}  "
                f"(first result at t={final[0]['elapsed_s']}s)"
            )
        else:
            log.warning(
                "  ⚠ No HRV/BP/fatigue result — keep band still, need ~30s"
            )
        return samples

    async def realtime_steps(self, duration_seconds: int = 10) -> list:
        """Enable real-time step updates for N seconds."""
        return await self._realtime_measure(
            start_pkt=pkt_real_time_steps(True),
            stop_pkt=pkt_real_time_steps(False),
            cmd_byte=CMD_REAL_TIME_STEPS,
            measure_type=None,
            parser=parse_realtime_steps,
            value_key=None,
            duration=duration_seconds,
            label="Steps"
        )

    async def _realtime_measure(self, start_pkt, stop_pkt, cmd_byte,
                                  measure_type, parser, value_key,
                                  duration, label) -> list:
        samples = []
        # Use (cmd_byte, measure_type) as unique key so HR (0x02) and SpO2 (0x03)
        # don't overwrite each other in self._notify_handlers.
        handler_key = (cmd_byte, measure_type)

        def handler(raw: bytes):
            obj = parser(raw)
            if obj is None:
                return
            # Filter: only accept packets matching the expected measure_type
            if measure_type is not None:
                pkt_type = getattr(obj, "measure_type", None)
                if pkt_type != measure_type:
                    return
            # Warn and skip zero values — sensor not in contact or still warming up
            if value_key:
                val = getattr(obj, value_key, None)
                if val == 0:
                    log.warning(
                        f"  ⚠  {label}=0 — sensor may not be in contact yet. "
                        f"Raw: {raw.hex(' ')}"
                    )
                    return
            samples.append(_dataclass_to_dict(obj))
            log.info(f"  📡 {label}: {_dataclass_to_dict(obj)}")

        self._notify_handlers[handler_key] = handler

        await self._client.write_gatt_char(TX_UUID, start_pkt, response=False)
        log.info(f"▶ Real-time {label} started ({duration}s)...")
        await asyncio.sleep(duration)
        await self._client.write_gatt_char(TX_UUID, stop_pkt, response=False)
        self._notify_handlers.pop(handler_key, None)
        log.info(f"⏹ Real-time {label} stopped. {len(samples)} samples.")
        return samples

    # ── PPI ───────────────────────────────────────────────────────────────────
    async def get_all_ppi(self) -> list:
        """
        Read all stored PPI (Peak-to-Peak Interval) records.

        Each BLE packet contains multiple concatenated 123-byte records
        with no per-packet CRC. We use parse_ppi_multi to extract all
        records from each packet, then paginate until the device sends
        [0x64, 0xFF] (no more data).

        Each record contains raw RR intervals in ms (one per heartbeat)
        plus pre-computed RMSSD, mean/min/max RR.
        """
        import dataclasses
        records = []
        seen_ids = set()

        async def fetch(pkt):
            while not self._response_queue.empty():
                self._response_queue.get_nowait()
            await self._client.write_gatt_char(TX_UUID, pkt, response=False)
            try:
                return await asyncio.wait_for(
                    self._response_queue.get(),
                    timeout=self.RESPONSE_TIMEOUT
                )
            except asyncio.TimeoutError:
                return None

        raw = await fetch(pkt_get_ppi(0))
        while raw is not None:
            # End of data signal
            if self._is_no_data(raw):
                break

            batch = parse_ppi_multi(raw)
            if not batch:
                break

            new_records = 0
            for rec in batch:
                if rec.record_id not in seen_ids:
                    seen_ids.add(rec.record_id)
                    records.append(dataclasses.asdict(rec))
                    new_records += 1
                    log.info(
                        f"  📦 PPI record #{rec.record_id} "
                        f"[{rec.timestamp}] "
                        f"{len(rec.intervals_ms)} beats  "
                        f"mean={rec.mean_rr_ms}ms  "
                        f"RMSSD={rec.rmssd_ms}ms  "
                        f"HR≈{round(60000/rec.mean_rr_ms)}bpm"
                    )

            if new_records == 0:
                break  # all records in this packet already seen

            # Each packet has 2 records (IDs N and N+1). The device sends
            # [0x64, 0xFF] after the last packet, NOT between the two records
            # inside a packet — so we only need to request "continue" once
            # per packet, not once per record.
            raw = await fetch(pkt_get_ppi(2))
            # If we got another real packet, its two records are already
            # handled in the next loop iteration. No extra fetch needed.

        log.info(f"✅ PPI: {len(records)} records retrieved")
        return records

    # ── Auto measurement schedule ──────────────────────────────────────────────
    async def set_auto_heart_rate(
        self,
        enable: bool = True,
        start_hour: int = 8,
        end_hour: int = 22,
        interval_minutes: int = 30,
        all_week: bool = True,
    ) -> bool:
        """
        Enable/disable automatic HR recording.
        Default: every 30 min from 08:00 to 22:00, all week.
        """
        days = 0x7F if all_week else 0b0111110  # all week or Mon-Fri
        pkt = pkt_set_auto_measure(
            sensor=1, enable=enable,
            start_hour=start_hour, start_min=0,
            end_hour=end_hour, end_min=59,
            days=days, interval_minutes=interval_minutes,
        )
        raw = await self._send(pkt)
        ok = len(raw) >= 1 and raw[0] == 0x2A and not self._is_no_data(raw)
        log.info(f"⏰ Auto HR {'enabled' if enable else 'disabled'}: {'OK' if ok else 'FAILED'}")
        return ok

    async def run_scheduled_heart_rate_monitor(
        self,
        first_wait_seconds: int = SCHEDULED_HR_FIRST_WAIT_SECONDS,
        poll_seconds: int = SCHEDULED_HR_POLL_SECONDS,
        max_cycles: Optional[int] = None,
    ) -> dict:
        """
        Configure 0x2A for HR every five minutes and collect 0x55 records.

        The first post-configuration read happens after one minute. Later reads
        happen every minute so newly completed measurements are retrieved with
        low latency. A persisted timestamp checkpoint allows every newer record
        to be recovered after a restart or BLE disconnection.
        """
        if first_wait_seconds < 0 or poll_seconds < 0:
            raise ValueError("Monitor delays cannot be negative")

        log.info("Starting scheduled HR acquisition (0x2A + 0x55).")
        if not await self.sync_time(timezone_minutes=-300):
            raise RuntimeError("Could not synchronize wristband time")

        configured = await self.set_auto_heart_rate(
            enable=True,
            start_hour=0,
            end_hour=23,
            interval_minutes=self.SCHEDULED_HR_INTERVAL_MINUTES,
            all_week=True,
        )
        if not configured:
            raise RuntimeError("Could not configure scheduled HR acquisition")

        log.info(
            "Auto HR configured: every 5 min, 00:00-23:59, all week. "
            "The schedule remains active when this monitor stops."
        )
        session_start = datetime.now().replace(microsecond=0) - timedelta(seconds=2)

        collected_count = 0
        cycles = 0

        async def collect_with_reconnect(
            minimum_timestamp: Optional[datetime] = None,
        ) -> list:
            while True:
                try:
                    return await self.collect_new_scheduled_heart_rate_records(
                        minimum_timestamp=minimum_timestamp,
                    )
                except asyncio.CancelledError:
                    raise
                except (BleakError, TimeoutError, RuntimeError) as exc:
                    log.warning(
                        f"Scheduled HR read failed: {exc}. Reconnecting."
                    )
                    if self._client:
                        try:
                            await self._client.disconnect()
                        except Exception:
                            pass
                    self._client = None
                    await self._reconnect_until_connected()

        # Recover records produced while the application was not running before
        # waiting for the first new measurement from this configuration cycle.
        had_checkpoint = self._load_scheduled_hr_state() is not None
        if had_checkpoint:
            recovered = await collect_with_reconnect()
            collected_count += len(recovered)

        if first_wait_seconds:
            log.info(
                f"Waiting {first_wait_seconds}s for the first one-minute "
                "measurement to finish..."
            )
            await asyncio.sleep(first_wait_seconds)

        while max_cycles is None or cycles < max_cycles:
            records = await collect_with_reconnect(
                minimum_timestamp=None if had_checkpoint else session_start
            )
            collected_count += len(records)
            cycles += 1

            if max_cycles is not None and cycles >= max_cycles:
                break
            log.info(
                f"Next scheduled HR query in {poll_seconds}s "
                f"(records saved: {collected_count})."
            )
            await asyncio.sleep(poll_seconds)

        return {
            "cycles": cycles,
            "records_saved": collected_count,
            "records_file": str(self._scheduled_hr_records_file),
            "checkpoint_file": str(self._scheduled_hr_state_file),
        }

    async def set_auto_spo2(
        self,
        enable: bool = True,
        start_hour: int = 8,
        end_hour: int = 22,
        interval_minutes: int = 60,
        all_week: bool = True,
    ) -> bool:
        """Enable/disable automatic SpO2 recording."""
        days = 0x7F if all_week else 0b0111110
        pkt = pkt_set_auto_measure(
            sensor=2, enable=enable,
            start_hour=start_hour, start_min=0,
            end_hour=end_hour, end_min=59,
            days=days, interval_minutes=interval_minutes,
        )
        raw = await self._send(pkt)
        ok = len(raw) >= 1 and raw[0] == 0x2A and not self._is_no_data(raw)
        log.info(f"⏰ Auto SpO2 {'enabled' if enable else 'disabled'}: {'OK' if ok else 'FAILED'}")
        return ok

    async def set_auto_cardio(
        self,
        enable: bool = True,
        start_hour: int = 8,
        end_hour: int = 22,
        interval_minutes: int = 30,
        all_week: bool = True,
    ) -> bool:
        """
        Enable/disable automatic CardioAnalysis recording (sensor=4).
        This triggers the 0x28 01 mode automatically at the given interval
        and stores results in 0x56 records (HRV index + fatigue + BP).
        Recommended: disconnect BLE after enabling so the device measures
        autonomously — some firmware skips auto-measure while BLE is active.
        """
        days = 0x7F if all_week else 0b0111110
        pkt = pkt_set_auto_measure(
            sensor=4, enable=enable,
            start_hour=start_hour, start_min=0,
            end_hour=end_hour, end_min=59,
            days=days, interval_minutes=interval_minutes,
        )
        raw = await self._send(pkt)
        ok = len(raw) >= 1 and raw[0] == 0x2A and not self._is_no_data(raw)
        log.info(
            f"⏰ Auto CardioAnalysis (sensor=4) "
            f"{'enabled' if enable else 'disabled'}: {'OK' if ok else 'FAILED'}"
        )
        if ok and enable:
            log.info(
                f"  ⚠ Disconnect BLE now and wait {interval_minutes} min "
                f"before reconnecting to read 0x56 records."
            )
        return ok

    async def probe_hrv_mode(self, duration_seconds: int = 180) -> dict:
        """
        Experiment: capture 0x28 01 raw packets for N seconds and analyse
        every byte to determine whether the device ever delivers real HRV
        (SDNN/RMSSD in ms, typically 20-100ms) vs just HR (60-200 bpm).

        Field layout per SDK:
          [0] cmd=0x28  [1] type=0x01
          [2] = "HRV"?  [3] = SpO2?  [4] = ?  [5] = ?
          [6] = fatigue? [7] = systolic_bp? [8] = diastolic_bp?
          [9..14] = ? [15] = CRC
        """
        import dataclasses
        from datetime import datetime as dt

        samples = []
        handler_key = (CMD_HEALTH_MEASURE, 0x01)

        def handler(raw: bytes):
            ts = dt.now().isoformat(timespec="milliseconds")
            # Capture every byte individually for analysis
            sample = {
                "ts": ts,
                "elapsed_s": round((dt.now() - start_time).total_seconds(), 1),
            }
            for i in range(min(len(raw), 16)):
                sample[f"b{i:02d}"] = raw[i]
            samples.append(sample)
            log.info(
                f"  🔬 [{sample['elapsed_s']:6.1f}s] "
                f"b02={raw[2]:3d}  b03={raw[3]:3d}  b04={raw[4]:3d}  "
                f"b05={raw[5]:3d}  b06={raw[6]:3d}  b07={raw[7]:3d}  "
                f"b08={raw[8]:3d}  raw={raw.hex(' ')}"
            )

        # Warmup
        log.info("🌡  Warming up sensor (5s HR mode)...")
        await self._client.write_gatt_char(
            TX_UUID, pkt_start_heart_rate(), response=False
        )
        await asyncio.sleep(5)
        await self._client.write_gatt_char(
            TX_UUID, build_packet(CMD_HEALTH_MEASURE, bytes([0x02, 0x00, 0x00])),
            response=False
        )
        await asyncio.sleep(0.5)

        self._notify_handlers[handler_key] = handler
        await self._client.write_gatt_char(
            TX_UUID, pkt_start_hrv(), response=False
        )
        start_time = dt.now()
        log.info(f"▶ 0x28 01 probe started — capturing {duration_seconds}s of raw packets...")
        await asyncio.sleep(duration_seconds)
        await self._client.write_gatt_char(
            TX_UUID, build_packet(CMD_HEALTH_MEASURE, bytes([0x01, 0x00, 0x00])),
            response=False
        )
        self._notify_handlers.pop(handler_key, None)
        log.info(f"⏹ Capture done. {len(samples)} packets received.")

        if not samples:
            return {"error": "No packets received — is the band on your wrist?"}

        # ── Analysis ──────────────────────────────────────────────────────────
        # For each byte position, collect all values and check if they vary
        analysis = {}
        for col in [f"b{i:02d}" for i in range(2, 9)]:
            vals = [s[col] for s in samples if col in s]
            if not vals:
                continue
            unique = sorted(set(vals))
            mn, mx = min(vals), max(vals)
            # Heuristic: HR range = 40-200 bpm, HRV range = 10-150 ms
            # If values cluster in 40-200 AND vary by <30 → likely HR
            # If values cluster in 10-150 AND vary      → could be HRV
            likely_hr  = 40 <= mn and mx <= 200 and (mx - mn) < 40
            likely_hrv = 5  <= mn and mx <= 200 and (mx - mn) >= 5
            always_zero = mn == 0 and mx == 0

            analysis[col] = {
                "min": mn, "max": mx,
                "unique_values": unique[:20],  # cap at 20
                "always_zero": always_zero,
                "likely_HR_bpm": likely_hr and not always_zero,
                "could_be_HRV_ms": likely_hrv and not always_zero,
            }

        # Check if any non-b02 field ever became non-zero after 30s
        # (device might need time to compute HRV)
        late_samples = [s for s in samples if s.get("elapsed_s", 0) > 30]
        fields_active_after_30s = {}
        for col in [f"b{i:02d}" for i in range(3, 9)]:
            late_vals = [s[col] for s in late_samples if col in s]
            nonzero = [v for v in late_vals if v != 0]
            fields_active_after_30s[col] = {
                "any_nonzero_after_30s": len(nonzero) > 0,
                "values": sorted(set(nonzero))[:10]
            }

        conclusion_parts = []
        b02 = analysis.get("b02", {})
        if b02.get("likely_HR_bpm"):
            conclusion_parts.append("b02 looks like HR (bpm), NOT HRV")
        elif b02.get("could_be_HRV_ms"):
            conclusion_parts.append("b02 COULD be HRV (ms) — values in plausible range")

        all_zero_after_30s = all(
            not v["any_nonzero_after_30s"]
            for k, v in fields_active_after_30s.items()
            if k != "b02"
        )
        if all_zero_after_30s:
            conclusion_parts.append(
                "All other fields (b03-b08) remained 0 after 30s — "
                "no fatigue/BP/SpO2 ever delivered"
            )
        else:
            active = [k for k, v in fields_active_after_30s.items()
                      if v["any_nonzero_after_30s"]]
            conclusion_parts.append(
                f"Fields active after 30s: {active} — device IS delivering more than just HR"
            )

        return {
            "total_packets": len(samples),
            "duration_s": duration_seconds,
            "per_byte_analysis": analysis,
            "fields_after_30s": fields_active_after_30s,
            "conclusion": " | ".join(conclusion_parts) if conclusion_parts else "Inconclusive",
        }

    async def test_min_ppi_interval(self, wait_minutes: int = 5) -> dict:
        """
        Experiment: configure auto HR at interval=1 min, wait N minutes,
        read PPI and measure the actual gap between timestamps.
        This tells us the real minimum recording frequency of the device.
        """
        import dataclasses
        from datetime import datetime as dt

        log.info("🧪 EXPERIMENT: setting auto HR interval=1 min...")

        # 1. Sync time so timestamps are meaningful
        await self.sync_time()

        # 2. Configure auto HR at 1-minute interval, all day today
        now = dt.now()
        pkt = pkt_set_auto_measure(
            sensor=1, enable=True,
            start_hour=0, start_min=0,
            end_hour=23, end_min=59,
            days=0x7F,
            interval_minutes=1,
        )
        raw = await self._send(pkt)
        ok = len(raw) >= 1 and raw[0] == 0x2A
        if not ok:
            return {"error": "Failed to configure auto HR at 1-min interval"}
        log.info(f"✅ Auto HR set to 1-min interval. Waiting {wait_minutes} min...")

        # 3. Wait
        for remaining in range(wait_minutes * 60, 0, -15):
            log.info(f"  ⏳ {remaining}s remaining...")
            await asyncio.sleep(15)

        # 4. Read PPI records
        log.info("📖 Reading PPI records...")
        records = await self.get_all_ppi()

        if len(records) < 2:
            return {
                "conclusion": "Not enough records to measure gap",
                "records_found": len(records),
                "tip": f"Try waiting longer — only {len(records)} record(s) found"
            }

        # 5. Analyse gaps between consecutive timestamps
        from datetime import datetime as dt2
        def parse_ts(s):
            return dt2.strptime(s, "%Y-%m-%d %H:%M:%S")

        # Group by unique timestamp (each session = multiple trozos)
        seen_ts = []
        for r in records:
            ts = r["timestamp"]
            if ts not in seen_ts:
                seen_ts.append(ts)

        seen_ts_sorted = sorted(seen_ts)
        gaps = []
        for i in range(1, len(seen_ts_sorted)):
            delta = (parse_ts(seen_ts_sorted[i]) -
                     parse_ts(seen_ts_sorted[i-1])).total_seconds() / 60
            gaps.append(round(delta, 1))

        result = {
            "unique_sessions": len(seen_ts_sorted),
            "timestamps": seen_ts_sorted,
            "gaps_between_sessions_minutes": gaps,
            "min_gap_minutes": min(gaps) if gaps else None,
            "max_gap_minutes": max(gaps) if gaps else None,
            "conclusion": (
                f"Minimum observed interval: {min(gaps)} min"
                if gaps else "Need more data"
            )
        }
        log.info(f"🔬 Result: {result['conclusion']}")
        log.info(f"   Gaps: {gaps}")
        return result

    async def get_auto_schedule(self, sensor: int = 1) -> dict:
        """Read current auto-measure schedule. sensor: 1=HR, 2=SpO2, 4=HRV"""
        label = {1: "HR", 2: "SpO2", 4: "HRV"}.get(sensor, str(sensor))
        raw = await self._send(pkt_get_auto_measure(sensor))
        if self._is_no_data(raw) or len(raw) < 9:
            return {"error": f"No schedule configured for {label}"}
        mode_map = {0: "OFF", 1: "Time-based", 2: "Interval"}
        days_map = ["Sun","Mon","Tue","Wed","Thu","Fri","Sat"]
        days_byte = raw[6] if len(raw) > 6 else 0
        active_days = [days_map[i] for i in range(7) if days_byte & (1 << i)]
        interval = raw[7] | (raw[8] << 8) if len(raw) > 8 else 0

        def from_bcd(b): return ((b >> 4) * 10) + (b & 0x0F)
        result = {
            "sensor": label,
            "mode": mode_map.get(raw[1], "unknown"),
            "start": f"{from_bcd(raw[2]):02d}:{from_bcd(raw[3]):02d}",
            "end":   f"{from_bcd(raw[4]):02d}:{from_bcd(raw[5]):02d}",
            "days":  active_days,
            "interval_minutes": interval,
        }
        log.info(f"📅 Auto {label} schedule: {result}")
        return result

    # ── Full dump ─────────────────────────────────────────────────────────────
    async def dump_all(self) -> dict:
        """
        Sync time, then collect all stored health data.
        Saves results to data/dump_<timestamp>.json
        """
        log.info("═══ Starting full data dump ═══")
        await self.sync_time()

        result = {
            "device_address": self.address,
            "dump_time": _now(),
            "battery": await self.get_battery(),
            "device_time": await self.get_time(),
            "heart_rate_records": await self.get_all_heart_rate(),
            "spo2_records": await self.get_all_blood_oxygen(),
            "sleep_records": await self.get_all_sleep(),
            "step_detail_records": await self.get_all_step_detail(),
            "total_activity_days": await self.get_total_activity(),
            "hrv_records": await self.get_all_hrv(),
            "ppi_records": await self.get_all_ppi(),
        }

        out_file = self.data_dir / f"dump_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        with open(out_file, "w") as f:
            json.dump(result, f, indent=2, default=str)
        log.info(f"💾 Saved to {out_file}")
        return result


# ── Scanner ───────────────────────────────────────────────────────────────────
async def scan_for_wristband(timeout: float = 10.0) -> list[BLEDevice]:
    """Scan for BLE devices and return likely Wristband 2501 candidates."""
    log.info(f"🔍 Scanning for BLE devices ({timeout}s)...")

    try:
        discovered = await BleakScanner.discover(timeout=timeout, return_adv=True)
    except TypeError:
        discovered = await BleakScanner.discover(timeout=timeout, return_adv=False)

    candidates = []

    if isinstance(discovered, dict):
        entries = discovered.values()
    else:
        entries = [(d, None) for d in discovered]

    target_service = SERVICE_UUID.lower()
    name_markers = ("J2501", "J-STYLE", "JSTYLE", "2501")

    for d, adv in entries:
        local_name = d.name or getattr(adv, "local_name", None) or "(unnamed)"
        service_uuids = [u.lower() for u in (getattr(adv, "service_uuids", None) or [])]

        log.info(f"  Found: {local_name:30s}  {d.address}")

        candidate_reason = None
        if target_service in service_uuids:
            candidate_reason = f"advertises {SERVICE_UUID}"
        elif any(marker in local_name.upper() for marker in name_markers):
            candidate_reason = "name matches Wristband 2501 markers"

        if candidate_reason:
            log.info(f"    ↳ candidate: {candidate_reason}")
            candidates.append(d)

    if candidates:
        log.info(f"✨ {len(candidates)} candidate(s) found")
    else:
        log.info("No Wristband 2501 candidates found.")
    return candidates


# ── Helpers ───────────────────────────────────────────────────────────────────
def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _dataclass_to_dict(obj) -> dict:
    import dataclasses
    if dataclasses.is_dataclass(obj):
        return dataclasses.asdict(obj)
    return str(obj)
