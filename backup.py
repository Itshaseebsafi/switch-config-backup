#!/usr/bin/env python3
"""
backup.py - back up network device configs over SSH and show what changed.

Reads a list of devices from a CSV, logs into each one with Netmiko, pulls the
running config, and saves it with a timestamp. If the config is different from
the last backup, it saves a diff so you can see exactly what changed.

Example:
  python backup.py devices.csv
  python backup.py devices.csv --keep 30
"""

import argparse
import csv
import difflib
import getpass
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

# What to run on each kind of device to get its config.
# The keys are Netmiko device types.
SHOW_COMMANDS = {
    "cisco_ios": "show running-config",
    "cisco_xe": "show running-config",
    "cisco_nxos": "show running-config",
    "cisco_asa": "show running-config",
    "fortinet": "show",
    "ubiquiti_edgeswitch": "show running-config",
    "juniper_junos": "show configuration | display set",
    "arista_eos": "show running-config",
}

# Lines that change every time you look at them even when nobody touched the
# config (timestamps, byte counts, clock drift). We drop these before comparing,
# otherwise every backup would look like a change.
VOLATILE_PATTERNS = [
    r"^Building configuration",
    r"^Current configuration\s*:",
    r"^! Last configuration change at",
    r"^! NVRAM config last updated at",
    r"^! No configuration change since last restart",
    r"^ntp clock-period",
    r"^#config-version=",           # Fortinet header
    r"^#conf_file_ver=",            # Fortinet header
    r"^## Last commit:",            # Juniper
]
_volatile = re.compile("|".join(VOLATILE_PATTERNS))


def clean_config(text):
    """Remove volatile lines and trailing whitespace so diffs only show real changes."""
    lines = [line.rstrip() for line in text.splitlines()]
    lines = [line for line in lines if not _volatile.match(line)]
    return "\n".join(lines).strip() + "\n"


def load_devices(path):
    """Read devices from a CSV with columns: name, host, device_type (port is optional)."""
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    devices = []
    for i, row in enumerate(rows, start=2):  # row 1 is the header
        name, host, dtype = (row.get(k, "").strip() for k in ("name", "host", "device_type"))
        if not (name and host and dtype):
            print(f"Skipping line {i} of {path}: needs name, host and device_type")
            continue
        if dtype not in SHOW_COMMANDS:
            print(f"Skipping {name}: don't know how to back up device_type '{dtype}'")
            continue
        devices.append({"name": name, "host": host, "device_type": dtype,
                        "port": int(row.get("port") or 22)})
    return devices


def get_credentials():
    """Username/password come from environment variables or a prompt, never from a file."""
    username = os.environ.get("NETBACKUP_USER") or input("SSH username: ")
    password = os.environ.get("NETBACKUP_PASS") or getpass.getpass("SSH password: ")
    secret = os.environ.get("NETBACKUP_SECRET", "")  # enable password, if your devices use one
    return username, password, secret


def fetch_config(device, username, password, secret, timeout=20):
    """Log into the device and return its running config as text."""
    from netmiko import ConnectHandler  # imported here so the rest works (and tests run) without it

    conn = ConnectHandler(
        device_type=device["device_type"], host=device["host"], port=device["port"],
        username=username, password=password, secret=secret, timeout=timeout,
    )
    try:
        if secret:
            conn.enable()
        return conn.send_command(SHOW_COMMANDS[device["device_type"]], read_timeout=60)
    finally:
        conn.disconnect()


def save_backup(name, config, backup_dir, now=None):
    """
    Save the config and compare it to the last one.

    backups/<name>/<name>_<timestamp>.cfg   every backup
    backups/<name>/latest.cfg               copy of the newest one
    backups/<name>/<name>_<timestamp>.diff  only when something changed

    Returns "new", "changed" or "unchanged", plus the diff text.
    """
    now = now or datetime.now()
    folder = Path(backup_dir) / name
    folder.mkdir(parents=True, exist_ok=True)
    stamp = now.strftime("%Y%m%d-%H%M%S")
    latest = folder / "latest.cfg"

    previous = latest.read_text(encoding="utf-8") if latest.exists() else None
    if previous == config:
        return "unchanged", ""  # nothing new, don't fill the folder with duplicates

    (folder / f"{name}_{stamp}.cfg").write_text(config, encoding="utf-8")
    latest.write_text(config, encoding="utf-8")

    if previous is None:
        return "new", ""

    diff = "".join(difflib.unified_diff(
        previous.splitlines(keepends=True), config.splitlines(keepends=True),
        fromfile=f"{name} (previous)", tofile=f"{name} ({stamp})",
    ))
    (folder / f"{name}_{stamp}.diff").write_text(diff, encoding="utf-8")
    return "changed", diff


def prune(name, backup_dir, keep):
    """Keep only the newest `keep` backups (and their diffs) for a device."""
    folder = Path(backup_dir) / name
    configs = sorted(folder.glob(f"{name}_*.cfg"))  # timestamps sort correctly as text
    for old in configs[:-keep] if keep else []:
        old.unlink()
        old.with_suffix(".diff").unlink(missing_ok=True)


def count_changes(diff):
    """How many lines were added and removed, ignoring the diff headers."""
    added = sum(1 for l in diff.splitlines() if l.startswith("+") and not l.startswith("+++"))
    removed = sum(1 for l in diff.splitlines() if l.startswith("-") and not l.startswith("---"))
    return added, removed


def backup_device(device, creds, backup_dir, keep):
    """Do one device start to finish. Returns a result row; never raises."""
    try:
        raw = fetch_config(device, *creds)
        status, diff = save_backup(device["name"], clean_config(raw), backup_dir)
        if keep:
            prune(device["name"], backup_dir, keep)
        added, removed = count_changes(diff)
        detail = f"+{added} / -{removed} lines" if status == "changed" else ""
        return {"name": device["name"], "host": device["host"], "status": status, "detail": detail}
    except Exception as e:  # one bad device shouldn't stop the rest
        return {"name": device["name"], "host": device["host"], "status": "failed",
                "detail": f"{type(e).__name__}: {e}"}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Back up network device configs and track changes.")
    parser.add_argument("devices", help="CSV with name, host, device_type (and optional port)")
    parser.add_argument("--backup-dir", default="backups", help="where to save backups (default: backups)")
    parser.add_argument("--keep", type=int, default=0, help="keep only the newest N backups per device")
    parser.add_argument("--workers", type=int, default=5, help="devices to back up at the same time")
    args = parser.parse_args(argv)

    devices = load_devices(args.devices)
    if not devices:
        sys.exit("No valid devices to back up.")

    creds = get_credentials()
    print(f"\nBacking up {len(devices)} device(s)...\n")

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(lambda d: backup_device(d, creds, args.backup_dir, args.keep), devices))

    print(f"{'DEVICE':<18} {'HOST':<16} {'STATUS':<10} DETAIL")
    print("-" * 70)
    for r in results:
        print(f"{r['name']:<18} {r['host']:<16} {r['status']:<10} {r['detail']}")

    changed = [r for r in results if r["status"] == "changed"]
    failed = [r for r in results if r["status"] == "failed"]
    print(f"\n{len(results) - len(failed)} backed up, {len(changed)} changed, {len(failed)} failed.")
    if changed:
        print(f"Diffs are in {args.backup_dir}/<device>/ next to each backup.")

    sys.exit(1 if failed else 0)  # non-zero so a scheduled task can alert on failures


if __name__ == "__main__":
    main()
