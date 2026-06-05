#!/usr/bin/env python3
"""
main.py — CLI for Wristband 2501
Run: python main.py
"""

import asyncio
import argparse
import json
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent / "src"))

from bleak.exc import BleakDeviceNotFoundError
from ble_client import WristbandClient, scan_for_wristband


# ── Address persistence ───────────────────────────────────────────────────────
CONFIG_FILE = Path(".wristband_address")

def load_address() -> str | None:
    if CONFIG_FILE.exists():
        return CONFIG_FILE.read_text().strip()
    return None

def save_address(addr: str):
    CONFIG_FILE.write_text(addr.strip())


async def pick_address_via_scan() -> str | None:
    print("Scanning for wristbands…")
    devs = await scan_for_wristband()
    if not devs:
        return None

    if len(devs) == 1:
        address = devs[0].address
        print(f"Selected: {devs[0].name or '(unnamed)'}  {address}")
    else:
        for i, d in enumerate(devs):
            print(f"  [{i}] {d.address}  {d.name}")
        idx = int(input("Select device index: "))
        address = devs[idx].address

    return address


# ── Menu ──────────────────────────────────────────────────────────────────────
MENU = """
╔══════════════════════════════════════╗
║      Wristband 2501 — Data Tool      ║
╚══════════════════════════════════════╝
  1) Scan & connect
  2) Battery level
  3) Sync time (set device clock)
  4) Real-time heart rate  (30s)
  4b) Real-time HRV        (60s)
  5) Real-time SpO2        (30s)
  6) Real-time steps       (10s)
  7) Read stored heart rate records
  8) Read stored SpO2 records
  9) Read stored sleep records
 10) Read stored step details
 11) Read total activity (30 days)
 12) Read HRV records
 13b) Read PPI (raw RR intervals + RMSSD)
 13) ★ Full dump (all data → JSON)
 ── Auto-measure setup ──────────────────
 14) Enable auto heart rate recording
 15) Enable auto SpO2 recording
 16) Disable auto heart rate
 17) Disable auto SpO2
 18) View current auto-measure schedule
 18c) Enable auto CardioAnalysis recording (sensor=4 → stores to 0x56)
 18d) Disable auto CardioAnalysis
 ── Experiments ─────────────────────────
 19) Test minimum PPI interval (sets 1-min, waits, measures gap)
 20) ★ Probe 0x28 01 — is it real HRV or just HR? (3 min capture)
  q) Quit
"""

async def interactive(address: str):
    print(f"\n🔗 Connecting to {address}…")
    async with WristbandClient(address) as wb:
        print("✅ Connected!\n")
        while True:
            print(MENU)
            choice = input("Choose> ").strip()

            if choice == "q":
                break
            elif choice == "1":
                print("Already connected.")
            elif choice == "2":
                r = await wb.get_battery()
                _pretty(r)
            elif choice == "3":
                ok = await wb.sync_time(timezone_minutes=-300)  # UTC-5 Colombia
                print("✅ Synced" if ok else "❌ Failed")
            elif choice == "4":
                secs = int(input("Duration (seconds) [30]: ") or 30)
                r = await wb.realtime_heart_rate(secs)
                _pretty(r)
            elif choice == "4b":
                secs = int(input("Duration (seconds) [60]: ") or 60)
                print("\n⚠  Keep band firmly on wrist, stay still.")
                r = await wb.realtime_hrv(secs)
                final = [s for s in r if s.get("result_ready")]
                if final:
                    last = final[-1]
                    print(f"\n── CardioAnalysis result ──")
                    print(f"  HRV      : {last['hrv_device']}")
                    print(f"  HR       : {last['heart_rate']} bpm")
                    print(f"  BP       : {last['systolic_bp']}/{last['diastolic_bp']} mmHg")
                    print(f"  Fatigue  : {last['fatigue']}")
                    print(f"  At t     : {last['elapsed_s']}s")
                else:
                    print("\n⚠  No result — try again keeping the band still for longer.")
            elif choice == "5":
                secs = int(input("Duration (seconds) [30]: ") or 30)
                r = await wb.realtime_spo2(secs)
                _pretty(r)
            elif choice == "6":
                secs = int(input("Duration (seconds) [10]: ") or 10)
                r = await wb.realtime_steps(secs)
                _pretty(r)
            elif choice == "7":
                r = await wb.get_all_heart_rate()
                _pretty(r)
            elif choice == "8":
                r = await wb.get_all_blood_oxygen()
                _pretty(r)
            elif choice == "9":
                r = await wb.get_all_sleep()
                _pretty(r)
            elif choice == "10":
                r = await wb.get_all_step_detail()
                _pretty(r)
            elif choice == "11":
                r = await wb.get_total_activity()
                _pretty(r)
            elif choice == "12":
                r = await wb.get_all_hrv()
                _pretty(r)
            elif choice == "13b":
                r = await wb.get_all_ppi()
                if r:
                    # Print summary per record
                    for rec in r:
                        ivs = rec.get("intervals_ms", [])
                        print(
                            f"  [{rec['timestamp']}] "
                            f"{len(ivs)} beats  "
                            f"mean={rec['mean_rr_ms']}ms  "
                            f"RMSSD={rec['rmssd_ms']}ms  "
                            f"HR≈{round(60000/rec['mean_rr_ms'])}bpm"
                        )
                else:
                    print("No PPI data on device yet.")
                    print("Tip: enable auto HR (option 14) and wait for first measurement.")
            elif choice == "13":
                r = await wb.dump_all()
                print(f"\n✅ Dump complete — {len(json.dumps(r))} bytes written to data/")
            elif choice == "14":
                h = int(input("Start hour [8]: ") or 8)
                e = int(input("End hour   [22]: ") or 22)
                iv = int(input("Interval minutes [30]: ") or 30)
                ok = await wb.set_auto_heart_rate(True, h, e, iv)
                print("✅ Auto HR enabled — wear the band; records appear after first measurement." if ok else "❌ Failed")
            elif choice == "15":
                h = int(input("Start hour [8]: ") or 8)
                e = int(input("End hour   [22]: ") or 22)
                iv = int(input("Interval minutes [60]: ") or 60)
                ok = await wb.set_auto_spo2(True, h, e, iv)
                print("✅ Auto SpO2 enabled." if ok else "❌ Failed")
            elif choice == "16":
                ok = await wb.set_auto_heart_rate(False)
                print("✅ Auto HR disabled." if ok else "❌ Failed")
            elif choice == "17":
                ok = await wb.set_auto_spo2(False)
                print("✅ Auto SpO2 disabled." if ok else "❌ Failed")
            elif choice == "18":
                for sensor in [1, 2, 4]:
                    r = await wb.get_auto_schedule(sensor)
                    _pretty(r)
            elif choice == "18c":
                h  = int(input("Start hour [8]: ") or 8)
                e  = int(input("End hour   [22]: ") or 22)
                iv = int(input("Interval minutes [30]: ") or 30)
                ok = await wb.set_auto_cardio(True, h, e, iv)
                if ok:
                    print(
                        f"\n✅ Auto CardioAnalysis enabled every {iv} min "
                        f"from {h:02d}:00 to {e:02d}:00."
                        f"\n⚠  IMPORTANT: disconnect now and wait {iv} min,"
                        f" then reconnect and use option 12 to read 0x56 records."
                    )
                else:
                    print("❌ Failed")
            elif choice == "18d":
                ok = await wb.set_auto_cardio(False)
                print("✅ Auto CardioAnalysis disabled." if ok else "❌ Failed")
            elif choice == "19":
                mins = int(input("How many minutes to wait? [5]: ") or 5)
                print(f"\n⚠  This will:\n"
                      f"  1. Set auto HR interval to 1 minute\n"
                      f"  2. Wait {mins} minutes (stay connected)\n"
                      f"  3. Read PPI and report actual gaps\n")
                confirm = input("Continue? (y/n): ").strip().lower()
                if confirm == "y":
                    r = await wb.test_min_ppi_interval(mins)
                    _pretty(r)
                else:
                    print("Cancelled.")
            elif choice == "20":
                secs = int(input("Duration seconds [180]: ") or 180)
                print(f"\n⚠  Keep the band firmly on your wrist.\n"
                      f"   Capturing 0x28 01 for {secs}s — logging every packet.\n")
                r = await wb.probe_hrv_mode(secs)
                print("\n── Per-byte analysis ──")
                for byte_pos, info in r.get("per_byte_analysis", {}).items():
                    flag = ""
                    if info["always_zero"]:        flag = "  ← always 0"
                    elif info["likely_HR_bpm"]:    flag = "  ← looks like HR (bpm)"
                    elif info["could_be_HRV_ms"]:  flag = "  ← could be HRV (ms)"
                    print(f"  {byte_pos}: min={info['min']:3d}  max={info['max']:3d}  "
                          f"unique={info['unique_values']}{flag}")
                print("\n── Fields active after 30s ──")
                for bp, info in r.get("fields_after_30s", {}).items():
                    status = info['values'] if info['any_nonzero_after_30s'] else 'always 0'
                    print(f"  {bp}: {status}")
                print(f"\n🔬 CONCLUSION: {r.get('conclusion')}")
            else:
                print("Unknown option.")


async def main():
    parser = argparse.ArgumentParser(description="Wristband 2501 BLE Tool")
    parser.add_argument("--address", "-a", help="Bluetooth MAC address (e.g. AA:BB:CC:DD:EE:FF)")
    parser.add_argument("--scan",    "-s", action="store_true", help="Scan for devices and exit")
    parser.add_argument("--dump",    "-d", action="store_true", help="Non-interactive: full dump")
    args = parser.parse_args()

    if args.scan:
        devs = await scan_for_wristband()
        for d in devs:
            print(f"  {d.address}  {d.name}")
        return

    address = args.address or load_address()
    save_after_success = False

    if not address:
        print("No address configured.")
        address = await pick_address_via_scan()
        if not address:
            print("\n⚠  No device found. Run again with --address XX:XX:XX:XX:XX:XX")
            sys.exit(1)
        save_after_success = True

    tried_rescan = False
    while True:
        try:
            if args.dump:
                async with WristbandClient(address) as wb:
                    await wb.dump_all()
            else:
                await interactive(address)
            if save_after_success:
                save_address(address)
                print(f"💾 Address saved to {CONFIG_FILE}")
            return
        except (BleakDeviceNotFoundError, asyncio.TimeoutError) as exc:
            if isinstance(exc, BleakDeviceNotFoundError):
                print(f"\n⚠  Device {address} was not found.")
            else:
                print(f"\n⚠  Timed out while connecting to {address}.")

            if tried_rescan:
                print("Tip: wake up the band, keep it close, and make sure it is not connected to another device.")
                sys.exit(1)

            print("The saved address may be stale or may belong to another BLE device. Re-scanning…")
            address = await pick_address_via_scan()
            if not address:
                print("\n⚠  No device found during re-scan.")
                print("Look specifically for a device named like 'J2501 ...' or one advertising service FFF0.")
                print("Tip: wake up the band, keep it close, and make sure it is not connected to another device.")
                sys.exit(1)
            save_after_success = True
            tried_rescan = True


def _pretty(data):
    print(json.dumps(data, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
