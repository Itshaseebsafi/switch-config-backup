"""Tests for backup.py. No real devices or Netmiko needed.
Run with:  python -m unittest discover tests
"""

import os
import sys
import tempfile
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import backup  # noqa: E402

RAW_CONFIG = """Building configuration...

Current configuration : 1520 bytes
!
! Last configuration change at 14:02:11 EST Mon Nov 23 2026 by admin
!
hostname sw-access-01
!
interface GigabitEthernet1/0/1
 switchport access vlan 10
!
ntp clock-period 36028797
end
"""


class TestCleanConfig(unittest.TestCase):
    def test_drops_volatile_lines(self):
        cleaned = backup.clean_config(RAW_CONFIG)
        self.assertNotIn("Last configuration change", cleaned)
        self.assertNotIn("Current configuration", cleaned)
        self.assertNotIn("ntp clock-period", cleaned)
        self.assertIn("hostname sw-access-01", cleaned)

    def test_strips_trailing_spaces(self):
        self.assertIn(" switchport access vlan 10\n", backup.clean_config(RAW_CONFIG))

    def test_same_config_different_timestamp_is_identical(self):
        later = RAW_CONFIG.replace("14:02:11", "09:15:40").replace("1520", "1534")
        self.assertEqual(backup.clean_config(RAW_CONFIG), backup.clean_config(later))


class TestSaveBackup(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_first_backup_is_new(self):
        status, diff = backup.save_backup("sw1", "hostname sw1\n", self.dir, datetime(2026, 11, 1))
        self.assertEqual(status, "new")
        self.assertTrue((Path(self.dir) / "sw1" / "latest.cfg").exists())

    def test_unchanged_config_saves_nothing_new(self):
        backup.save_backup("sw1", "hostname sw1\n", self.dir, datetime(2026, 11, 1))
        status, _ = backup.save_backup("sw1", "hostname sw1\n", self.dir, datetime(2026, 11, 2))
        self.assertEqual(status, "unchanged")
        self.assertEqual(len(list((Path(self.dir) / "sw1").glob("sw1_*.cfg"))), 1)

    def test_change_writes_a_diff(self):
        backup.save_backup("sw1", "hostname sw1\nvlan 10\n", self.dir, datetime(2026, 11, 1))
        status, diff = backup.save_backup("sw1", "hostname sw1\nvlan 10\nvlan 20\n", self.dir,
                                          datetime(2026, 11, 2))
        self.assertEqual(status, "changed")
        self.assertIn("+vlan 20", diff)
        self.assertEqual(backup.count_changes(diff), (1, 0))
        self.assertTrue((Path(self.dir) / "sw1" / "sw1_20261102-000000.diff").exists())

    def test_prune_keeps_newest(self):
        for day in range(1, 6):
            backup.save_backup("sw1", f"version {day}\n", self.dir, datetime(2026, 11, day))
        backup.prune("sw1", self.dir, keep=2)
        left = sorted(p.name for p in (Path(self.dir) / "sw1").glob("sw1_*.cfg"))
        self.assertEqual(left, ["sw1_20261104-000000.cfg", "sw1_20261105-000000.cfg"])
        self.assertFalse((Path(self.dir) / "sw1" / "sw1_20261103-000000.diff").exists())


class TestLoadDevices(unittest.TestCase):
    def test_skips_bad_rows(self):
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False) as f:
            f.write("name,host,device_type,port\n"
                    "sw1,10.0.0.2,cisco_ios,\n"
                    "fw1,10.0.0.1,fortinet,2222\n"
                    ",10.0.0.3,cisco_ios,\n"          # no name
                    "ap1,10.0.0.4,toaster_os,\n")     # unknown type
        try:
            with mock.patch("builtins.print"):
                devices = backup.load_devices(f.name)
        finally:
            os.unlink(f.name)
        self.assertEqual([d["name"] for d in devices], ["sw1", "fw1"])
        self.assertEqual(devices[1]["port"], 2222)


class TestBackupDevice(unittest.TestCase):
    def test_uses_netmiko_and_saves(self):
        conn = mock.MagicMock()
        conn.send_command.return_value = RAW_CONFIG
        fake_netmiko = types.SimpleNamespace(ConnectHandler=mock.MagicMock(return_value=conn))
        device = {"name": "sw1", "host": "10.0.0.2", "device_type": "cisco_ios", "port": 22}

        with tempfile.TemporaryDirectory() as d, mock.patch.dict(sys.modules, {"netmiko": fake_netmiko}):
            result = backup.backup_device(device, ("admin", "pw", ""), d, keep=0)

        self.assertEqual(result["status"], "new")
        conn.send_command.assert_called_once_with("show running-config", read_timeout=60)
        conn.disconnect.assert_called_once()

    def test_connection_error_is_reported_not_raised(self):
        fake_netmiko = types.SimpleNamespace(ConnectHandler=mock.MagicMock(side_effect=TimeoutError("no route")))
        device = {"name": "sw9", "host": "10.0.0.9", "device_type": "cisco_ios", "port": 22}
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(sys.modules, {"netmiko": fake_netmiko}):
            result = backup.backup_device(device, ("admin", "pw", ""), d, keep=0)
        self.assertEqual(result["status"], "failed")
        self.assertIn("no route", result["detail"])


if __name__ == "__main__":
    unittest.main()
