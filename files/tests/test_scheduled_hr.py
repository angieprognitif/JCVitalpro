import json
import asyncio
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from ble_client import WristbandClient
from protocol import (
    parse_single_heart_rate,
    parse_single_heart_rate_multi,
    pkt_get_single_heart_rate,
    pkt_set_auto_measure,
)


def scheduled_hr_record(record_id: int, timestamp: str, heart_rate: int) -> dict:
    return {
        "record_id": record_id,
        "timestamp": timestamp,
        "heart_rate": heart_rate,
    }


class FakeWristbandClient(WristbandClient):
    def __init__(self, data_dir: str):
        super().__init__("AA:BB:CC:DD:EE:FF", data_dir=data_dir)
        self.history = []
        self.sync_timezones = []
        self.schedule_calls = []

    async def sync_time(self, timezone_minutes=-300):
        self.sync_timezones.append(timezone_minutes)
        return True

    async def set_auto_heart_rate(
        self,
        enable=True,
        start_hour=8,
        end_hour=22,
        interval_minutes=30,
        all_week=True,
    ):
        self.schedule_calls.append({
            "enable": enable,
            "start_hour": start_hour,
            "end_hour": end_hour,
            "interval_minutes": interval_minutes,
            "all_week": all_week,
        })
        return True

    async def get_scheduled_heart_rate_history(self):
        return self.history


class FakeBleakClient:
    def __init__(self, owner, packets):
        self.owner = owner
        self.packets = packets
        self.is_connected = True
        self.writes = []

    async def write_gatt_char(self, uuid, packet, response=False):
        self.writes.append((uuid, packet, response))

        async def emit():
            for raw in self.packets:
                self.owner._on_notify(None, bytearray(raw))
                await asyncio.sleep(0)

        asyncio.create_task(emit())


class ScheduledHeartRateProtocolTests(unittest.TestCase):
    def test_scheduled_hr_poll_interval_is_one_minute(self):
        self.assertEqual(WristbandClient.SCHEDULED_HR_POLL_SECONDS, 60)

    def test_auto_measure_packet_matches_sdk(self):
        packet = pkt_set_auto_measure(
            sensor=1,
            enable=True,
            start_hour=0,
            start_min=0,
            end_hour=23,
            end_min=59,
            days=0x7F,
            interval_minutes=5,
        )
        self.assertEqual(
            packet,
            bytes.fromhex(
                "2A 02 00 00 23 59 7F 05 00 01 00 00 00 00 00 2D"
            ),
        )

    def test_single_hr_packet_modes(self):
        self.assertEqual(
            pkt_get_single_heart_rate(),
            bytes.fromhex(
                "55 00 00 00 00 00 00 00 00 00 00 00 00 00 00 55"
            ),
        )
        self.assertEqual(
            pkt_get_single_heart_rate(mode=0x01, record_id=0x1234),
            bytes.fromhex(
                "55 01 34 12 00 00 00 00 00 00 00 00 00 00 00 9C"
            ),
        )

    def test_parse_single_hr_record(self):
        record = parse_single_heart_rate(
            bytes.fromhex("55 34 12 26 06 05 14 07 09 4A")
        )
        self.assertIsNotNone(record)
        self.assertEqual(record.record_id, 0x1234)
        self.assertEqual(record.timestamp, "2026-06-05 14:07:09")
        self.assertEqual(record.heart_rate, 74)

    def test_parse_rejects_invalid_bcd_date(self):
        record = parse_single_heart_rate(
            bytes.fromhex("55 01 00 26 13 05 14 07 09 4A")
        )
        self.assertIsNone(record)

    def test_parse_concatenated_records(self):
        raw = bytes.fromhex(
            "55 01 00 26 06 05 14 00 00 46"
            "55 02 00 26 06 05 14 05 00 48"
        )
        records = parse_single_heart_rate_multi(raw)
        self.assertEqual([record.record_id for record in records], [1, 2])
        self.assertEqual([record.heart_rate for record in records], [70, 72])


class ScheduledHeartRatePersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.client = FakeWristbandClient(self.temp_dir.name)

    async def asyncTearDown(self):
        self.temp_dir.cleanup()

    def read_records(self):
        path = Path(self.temp_dir.name) / "scheduled_hr_records.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    async def test_first_record_creates_data_and_checkpoint(self):
        self.client.history = [
            scheduled_hr_record(0, "2026-06-05 10:01:00", 71)
        ]

        collected = (
            await self.client.collect_new_scheduled_heart_rate_records()
        )

        self.assertEqual([record["record_id"] for record in collected], [0])
        self.assertEqual(len(self.read_records()), 1)
        state = self.client._load_scheduled_hr_state()
        self.assertEqual(state["last_timestamp"], "2026-06-05 10:01:00")
        self.assertEqual(state["source_record_id"], 0)

    async def test_duplicate_is_not_appended(self):
        record = scheduled_hr_record(0, "2026-06-05 10:01:00", 71)
        self.client.history = [record]

        await self.client.collect_new_scheduled_heart_rate_records()
        collected = (
            await self.client.collect_new_scheduled_heart_rate_records()
        )

        self.assertEqual(collected, [])
        self.assertEqual(len(self.read_records()), 1)

    async def test_pre_session_record_is_not_used_as_baseline(self):
        self.client.history = [
            scheduled_hr_record(0, "2026-06-05 09:55:00", 68)
        ]

        collected = await self.client.collect_new_scheduled_heart_rate_records(
            minimum_timestamp=datetime(2026, 6, 5, 10, 0, 0)
        )

        self.assertEqual(collected, [])
        self.assertEqual(self.read_records(), [])
        self.assertIsNone(self.client._load_scheduled_hr_state())

    async def test_shifted_ids_do_not_create_duplicates(self):
        baseline = scheduled_hr_record(0, "2026-06-05 10:00:23", 64)
        self.client._persist_scheduled_hr_record(baseline)
        self.client.history = [
            scheduled_hr_record(0, "2026-06-05 10:05:01", 80),
            scheduled_hr_record(1, "2026-06-05 10:00:23", 64),
        ]

        collected = (
            await self.client.collect_new_scheduled_heart_rate_records()
        )

        self.assertEqual(
            [record["timestamp"] for record in collected],
            ["2026-06-05 10:05:01"],
        )
        self.assertEqual(
            [record["timestamp"] for record in self.read_records()],
            ["2026-06-05 10:00:23", "2026-06-05 10:05:01"],
        )
        self.assertEqual(
            self.client._load_scheduled_hr_state()["last_timestamp"],
            "2026-06-05 10:05:01",
        )

    async def test_disconnection_records_are_recovered_by_timestamp(self):
        baseline = scheduled_hr_record(0, "2026-06-05 10:05:01", 80)
        self.client._persist_scheduled_hr_record(baseline)
        self.client.history = [
            scheduled_hr_record(0, "2026-06-05 10:20:01", 75),
            scheduled_hr_record(1, "2026-06-05 10:15:02", 73),
            scheduled_hr_record(2, "2026-06-05 10:10:01", 77),
            scheduled_hr_record(3, "2026-06-05 10:05:01", 80),
        ]

        collected = (
            await self.client.collect_new_scheduled_heart_rate_records()
        )

        self.assertEqual(
            [record["timestamp"] for record in collected],
            [
                "2026-06-05 10:10:01",
                "2026-06-05 10:15:02",
                "2026-06-05 10:20:01",
            ],
        )
        self.assertEqual(
            self.client._load_scheduled_hr_state()["last_timestamp"],
            "2026-06-05 10:20:01",
        )

    async def test_existing_jsonl_record_is_idempotent_after_restart(self):
        record = scheduled_hr_record(0, "2026-06-05 12:01:00", 69)
        self.assertTrue(self.client._persist_scheduled_hr_record(record))

        restarted = FakeWristbandClient(self.temp_dir.name)
        shifted_record = scheduled_hr_record(
            4, "2026-06-05 12:01:00", 69
        )
        self.assertFalse(restarted._persist_scheduled_hr_record(shifted_record))

        self.assertEqual(len(self.read_records()), 1)
        self.assertEqual(
            restarted._load_scheduled_hr_state()["last_timestamp"],
            "2026-06-05 12:01:00",
        )

    async def test_old_checkpoint_with_last_record_id_is_accepted(self):
        state_path = Path(self.temp_dir.name) / "scheduled_hr_state.json"
        state_path.write_text(json.dumps({
            "device_address": self.client.address,
            "last_record_id": 0,
            "last_timestamp": "2026-06-05 10:05:01",
            "updated_at": "2026-06-05T10:07:10",
        }))
        self.client.history = [
            scheduled_hr_record(0, "2026-06-05 10:10:23", 78),
            scheduled_hr_record(1, "2026-06-05 10:05:01", 80),
        ]

        collected = (
            await self.client.collect_new_scheduled_heart_rate_records()
        )

        self.assertEqual(
            [record["timestamp"] for record in collected],
            ["2026-06-05 10:10:23"],
        )
        migrated = self.client._load_scheduled_hr_state()
        self.assertNotIn("last_record_id", migrated)
        self.assertEqual(migrated["source_record_id"], 0)

    async def test_monitor_configures_exact_five_minute_schedule(self):
        result = await self.client.run_scheduled_heart_rate_monitor(
            first_wait_seconds=0,
            poll_seconds=0,
            max_cycles=1,
        )

        self.assertEqual(self.client.sync_timezones, [-300])
        self.assertEqual(
            self.client.schedule_calls,
            [{
                "enable": True,
                "start_hour": 0,
                "end_hour": 23,
                "interval_minutes": 5,
                "all_week": True,
            }],
        )
        self.assertEqual(result["cycles"], 1)

    async def test_history_reader_drains_complete_notification_burst(self):
        client = WristbandClient(
            "AA:BB:CC:DD:EE:FF",
            data_dir=self.temp_dir.name,
        )
        client.SCHEDULED_HR_BURST_SILENCE_SECONDS = 0.01
        packet_1 = bytes.fromhex(
            "55 00 00 26 06 05 10 05 01 50"
            "55 01 00 26 06 05 10 00 23 40"
        )
        packet_2 = bytes.fromhex(
            "55 02 00 26 06 04 07 00 01 51"
        )
        client._client = FakeBleakClient(client, [packet_1, packet_2])

        history = await client.get_scheduled_heart_rate_history()

        self.assertEqual(len(history), 3)
        self.assertEqual(
            [record["timestamp"] for record in history],
            [
                "2026-06-05 10:05:01",
                "2026-06-05 10:00:23",
                "2026-06-04 07:00:01",
            ],
        )


if __name__ == "__main__":
    unittest.main()
