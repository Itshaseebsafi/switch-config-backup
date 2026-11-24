# switch-config-backup

Logs into switches, routers and firewalls over SSH, saves their running configs, and shows what changed since the last backup. Built on [Netmiko](https://github.com/ktbyers/netmiko).

## Why

When something breaks on a network, one of the first questions is "did anyone change anything?" If you don't have yesterday's config, you're guessing. I wanted something I could run every night that keeps a history of every device's config and tells me exactly which lines changed, so a bad change can be found and rolled back quickly.

## What it does

- Reads your devices from a CSV (name, IP, device type)
- Connects to a few devices at once over SSH and pulls the running config
- Strips lines that change on their own, like "Last configuration change at..." timestamps, byte counts and NTP clock drift, so a backup only counts as changed when someone actually changed the config
- Saves a timestamped copy only when something changed, plus a `.diff` showing the exact lines
- One unreachable device doesn't stop the others. It's marked failed and the script keeps going
- Exits with an error code if anything failed, so a scheduled task can alert you
- `--keep N` deletes old backups past the newest N per device

Works with Cisco IOS/IOS-XE/NX-OS/ASA, Fortinet, Ubiquiti EdgeSwitch, Juniper and Arista. Adding another is one line in `SHOW_COMMANDS`.

## Setup

```bash
git clone https://github.com/Itshaseebsafi/switch-config-backup.git
cd switch-config-backup
pip install -r requirements.txt
cp devices.example.csv devices.csv   # then put your own devices in it
```

`devices.csv` looks like this:

```
name,host,device_type,port
sw-core-01,10.10.0.2,cisco_ios,
sw-access-01,10.10.0.3,cisco_ios,
fw-edge-01,10.10.0.1,fortinet,
```

`port` is optional (defaults to 22). `device_type` is the Netmiko name for the platform.

## Running it

```bash
python backup.py devices.csv
```

It asks for the SSH username and password. Credentials are never stored in a file. To run it on a schedule, set them as environment variables instead:

```bash
export NETBACKUP_USER=netadmin
export NETBACKUP_PASS='...'
export NETBACKUP_SECRET='...'   # enable password, only if your devices need it
python backup.py devices.csv --keep 30
```

On Windows, set them with `setx` or in Task Scheduler. Ideally use a read-only account that can only run show commands.

## Example

The second run of the day, after someone changed a port on `sw-access-01`:

```
DEVICE             HOST             STATUS     DETAIL
----------------------------------------------------------------------
sw-core-01         10.10.0.2        unchanged
sw-access-01       10.10.0.3        changed    +3 / -2 lines
sw-access-02       10.10.0.4        unchanged
sw-access-03       10.10.0.9        failed     TimeoutError: TCP connection to device failed (10.10.0.9:22)

3 backed up, 1 changed, 1 failed.
```

And the diff it saved:

```diff
 interface GigabitEthernet1/0/2
- switchport access vlan 10
+ switchport access vlan 30
+ description GUEST-KIOSK
 !
 line vty 0 4
- transport input ssh
+ transport input ssh telnet
```

So someone moved a port to the guest VLAN and turned Telnet back on. The Telnet part is the kind of thing you want to catch the same day.

Backups end up like this:

```
backups/
  sw-access-01/
    latest.cfg
    sw-access-01_20261124-173309.cfg
    sw-access-01_20261124-173310.cfg
    sw-access-01_20261124-173310.diff
```

The example output above is from test devices with made-up IPs. Full copies are in `sample_output/`.

## Trying it without your own gear

- Cisco DevNet has free sandbox devices you can SSH into. Check their site for the current address and login.
- Or run virtual switches in GNS3, EVE-NG or Cisco Modeling Labs.

## Tests

The tests fake the SSH connection, so they don't need Netmiko or any devices:

```bash
python -m unittest discover tests
```

## Notes

- `backups/` and `devices.csv` are in .gitignore. Configs can contain password hashes, SNMP community strings and internal IPs, so they should never be pushed anywhere public.
- Only back up devices you're authorized to log into.

## Things I want to add

- Email or Teams alert when a config changes
- Push the backups into a private git repo for full history
- Flag risky changes automatically (Telnet enabled, ACLs removed, SNMP "public")
