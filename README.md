# Indigo-Unifi
Unifi Integration for Indigo Domotics

THIS PROJECT IS IN A VERY EARLY ALPHA PHASE AND WILL PROBABLY BREAK YOUR UNIFI SETUP SO DO NOT USE.  SERIOUSLY.  

# UniFi Plugin for Indigo Domotics — v3.5.1 (Initial Public Release)

## ⚠️ ALPHA SOFTWARE — READ THIS BEFORE INSTALLING ⚠️

**This is an alpha release.** It has been tested against exactly one UniFi
deployment (mine). It has **not** been tested across the wide range of UniFi
OS versions, hardware combinations, and site configurations that exist in the
wild.

By installing this plugin, you accept that:

- **It could misbehave against your UniFi setup in ways I haven't seen.**
  Device discovery, state parsing, and especially the *write* actions
  (reboot, shutdown, port control, client blocking, fan control) all talk to
  live UniFi APIs that Ubiquiti can and does change without notice.
- **The Drive (UNAS) integration uses unofficial, reverse-engineered
  endpoints.** There is no official public API for UNAS storage/fan control
  yet. These endpoints have changed shape before and will likely change
  again.
- **Fan control on the UNAS uses SSH to deploy a script directly onto the
  appliance**, bypassing UniFi's own (currently broken) write API. This
  writes to `/usr/local/sbin`, `/etc/systemd/system`, and raw sysfs PWM
  registers on your NAS. It is designed to be safe and reversible (an
  uninstall action is provided), but it **is** modifying files outside
  UniFi's supported configuration surface.
- **No warranty, express or implied.** See the MIT license — "AS IS", no
  liability for damages. This is a hobby project, not a commercial product.

### Before you install

- **Take a UniFi controller backup** (Settings → System → Backups) before
  connecting this plugin to a production network.
- **Use a dedicated local-only admin account** for the plugin (instructions
  in the README) — never your primary cloud/UI.com account.
- **Start read-only.** Get Network/Protect/Access/Drive discovery and
  monitoring working first before trying write actions like reboot, port
  control, or fan control.
- **If you enable SSH fan control on the UNAS**, understand that the plugin
  will install a systemd service and modify sysfs PWM values directly. Test
  on a non-critical NAS first if you can, or at minimum, know how to SSH in
  and manually run `systemctl stop indigo_fan_control.service` if something
  looks wrong.
- **Test in a low-stakes window** — not while you're relying on cameras for
  active security monitoring, not while critical backups are running on the
  UNAS.

If any of that gives you pause, wait for a more mature release. Issues and
PRs are welcome, but please don't run this unattended on a system you can't
afford to have misbehave.

---

## What this plugin does

A single Indigo Domotics plugin covering the whole UniFi ecosystem:

- **Network** — switches, access points, gateways, ports, and clients
- **Protect** — cameras and sensors, with near-real-time WebSocket events
- **Access** — doors, lock/unlock control
- **Drive (UNAS)** — storage pools, physical disks, and fan control

Real-time events (motion, doorbell rings, port link state, client
connect/disconnect, door lock state) arrive over WebSocket rather than
polling, so triggers fire within roughly 100ms of the underlying UniFi event.
Slow-changing stats (storage capacity, disk temperatures, pool health) are
polled on a configurable interval.

## Highlights in this release

**Network**
- Official Integration API used wherever Ubiquiti exposes it; documented
  legacy fallback for endpoints they haven't opened up yet (port tables,
  radio tables, PoE overrides, client block/unblock)
- Switch port devices with PoE/link/power-cycle control modes
- Client discovery with last-seen filtering, and a **new randomised-MAC
  purge action** — finds clients using privacy MAC randomisation (iPhone
  Private Address, Android random MAC, etc.), skips anything you've named,
  fixed an IP for, or already track in Indigo, and removes stale ones from
  both Indigo and UniFi itself

**Protect**
- Cameras and sensors on the official Integration API with Bearer auth
- WebSocket-driven motion, smart detection (person/vehicle/package),
  doorbell rings, and connection state

**Access**
- Doors with lock/unlock, WebSocket-driven state

**Drive (UNAS)**
- Storage pool and physical disk devices with capacity, RAID health, SMART
  temperature, and at-risk detection
- **Fan control that actually works.** UniFi's own fan-control write API
  returns HTTP 500 on current firmware (confirmed via extensive testing, not
  a plugin bug) — this release works around it by deploying a small script
  to the UNAS over SSH, which writes fan speed directly via sysfs. Three
  named modes (Quiet/Balance/Cooling) map to a dimmer device at 0/50/100%.
- **Auto-redeploy after firmware updates.** UniFi OS updates can wipe the
  deployed fan control files; the plugin checks daily and silently
  redeploys if needed (opt-out available).
- A built-in diagnostic action (SSH into the UNAS, run the script once, dump
  the last 30 systemd journal lines, and report current PWM register
  values) for when something isn't behaving as expected.

## Known limitations

- **UniFi Drive UI may show a stale fan mode.** Because SSH fan control
  bypasses UniFi's broken write API, the mode shown in UniFi's own web UI
  can lag behind reality. The Indigo dimmer and device states are accurate;
  the UniFi app's fan mode label is cosmetic-only in this scenario.
- **No AlarmHub Kit / alarm arm-disarm support.** Ubiquiti has not shipped a
  public API for this as of writing (community feature requests open since
  2025 with no ETA). Individual Protect sensors may still show up via
  existing sensor discovery, but zone/arm state is not exposed anywhere the
  plugin can reach.
- **Legacy auth is required for several Network and all Drive features**
  because Ubiquiti's official Integration API doesn't cover them yet. This
  means a local-only admin account and password living in Indigo's plugin
  prefs (encrypted at rest by Indigo, but worth knowing).
- Tested against one site, one controller topology, one hardware set. Your
  mileage will vary.

## Requirements

- Indigo Domotics 2024.1 or later (developed against 2025.2)
- macOS running the Indigo server
- UniFi OS console (UDM Pro or similar) with Network, Protect, and/or Access
  enabled as applicable
- For Drive fan control: a UniFi Drive/UNAS appliance with SSH enabled
  (Settings → Control Plane → Console) and `paramiko` installed automatically
  via the bundled `requirements.txt`
- A local-only admin account (see README for setup — this is the most common
  source of auth errors)

## Installation

See the full [README](README.md) for setup steps, API key generation, and
the local admin account walkthrough. Short version:

1. Download `UniFi.indigoPlugin` from this release's assets
2. Double-click to install into Indigo
3. Create a Controller device, fill in your API keys / local admin creds
4. Use the discovery buttons to create devices for whatever you've enabled

## Acknowledgements

- Fan control script adapted from
  [hoxxep/UNAS-Pro-fan-control](https://github.com/hoxxep/UNAS-Pro-fan-control)
  (MIT)
- UNAS endpoint and payload research cross-checked against
  [memphi2/ha-unifi-drive](https://github.com/memphi2/ha-unifi-drive) (MIT)
- Thanks to the openHAB and Home Assistant communities for documenting the
  Protect Integration API event stream shape

## License

MIT — see [LICENSE](LICENSE).
