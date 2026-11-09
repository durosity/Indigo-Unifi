# UniFi for Indigo

An [Indigo](https://www.indigodomo.com) plugin bridging **UniFi Network**, **UniFi Protect**, **UniFi Access**, and **UniFi Drive (UNAS)** into your home automation system — devices, clients, cameras, sensors, door locks, firewall rules, and more, all as native Indigo devices, actions, and triggers.

Released under the MIT License by Durosity.

**Before using this plugin, back up your UniFi configuration.** This plugin writes to your live UniFi configuration: it can enable/disable firewall policies and WLANs, change port forwards, lock/unlock doors, reboot devices, change camera and sensor settings, and (experimentally) arm/disarm Protect's Alarm Manager.  I've tested it quite extensively at my end, but it's plausible different configurations and setups may not work the same so please be careful.

Each UniFi OS console has its own backup/restore option (Settings → System → Backups on a Network console; the UNAS has its own separate backup under its own Settings). Do this now, not after something goes wrong. Real care is taken to confirm behavior against real data before shipping anything (see `ROADMAP.md` for the detailed, ongoing record of what's confirmed vs. still unverified), but this is community software with no warranty — you are responsible for your own backups, and for reviewing what an action does before wiring it into an automation that runs unattended.

A few specific things worth knowing going in:

- **The Alarm Manager feature (arm/disarm) is explicitly experimental.** It's built from community reverse-engineering of an undocumented endpoint, not Ubiquiti's official API, and has never been tested against real Alarm Manager hardware. It also requires a dedicated **Super Admin account with MFA disabled** — a real security trade-off, not a minor detail. If you use this feature, treat it as unverified until proven otherwise on your own setup, and please report back (see Diagnostics below) if anything looks wrong.
- Recording Mode and IR Mode **cannot** be controlled from this plugin — Ubiquiti's official Protect Integration API doesn't expose them (confirmed against the published spec). Use the UniFi Protect app directly for those.
- Legacy/local admin credentials (see Setup) are required for several features and are stored by Indigo, not sent anywhere else by this plugin — but as with any credential, use a dedicated local-only account, not your main cloud login.

If something doesn't look right, every major device type has a **"Dump Device Payload to Log"** button in its own configuration dialog (or an equivalent action, for a couple of device types where that fits better) — use it, then open an issue with the result rather than guessing.

## What this plugin covers

| UniFi product | What you get |
|---|---|
| **Network** | Switches/APs/gateways, clients, individual switch ports (PoE control), firewall policies, traffic routes (policy-based routing), port forwarding, WLAN management, 5G/LTE gateway (U5G-class) cellular status |
| **Protect** | Cameras (motion, person/vehicle/package, doorbell, audio detection, snapshots, and configuration), sensors (door/window/motion/leak), Alarm Manager (experimental) |
| **Access** | Door locks — momentary unlock, leave-unlocked, auto-relock, lock |
| **Drive (UNAS)** | Storage pools and disks, fan control, wake-on-LAN, reboot/shutdown |

Real-time events (motion, smart detection, doorbell rings, client connect/disconnect, port link state, door unlock, and a long list of Network health events) arrive instantly over WebSocket connections, not polling. Slower-changing stats (CPU, memory, temperature, throughput, fan RPM) are refreshed on a configurable interval instead.

## Requirements

- A recent Indigo release (developed and tested against Indigo 2026.1)
- A UniFi OS console (UDM, UDM Pro, UDR, UNVR, or similar) for Network/Protect/Access
- A UniFi Drive (UNAS) appliance, separately, if you want Drive features
- API keys/credentials as described in Setup below

## Installation

1. Download the latest release and double-click to install, or install manually via Indigo's plugin manager.
2. Create a **"UniFi Controller (Network)"** device for your main console, and/or a **"UniFi Controller (Drive)"** device for your UNAS, if you have one.
3. Configure each controller as described below, then use the **Create Devices** buttons in its own configuration dialog (or the equivalent **Discover…** actions) to populate individual devices.

## Setup

### Network / Protect / Access controller

Create a **UniFi Controller (Network)** device and fill in:

- **Connection**: the console's IP address, HTTPS port, and Site ID (`default` unless you run multiple sites).
- **Features to enable**: tick Network, Protect, and/or Access depending on what this console actually runs.
- **API Keys**: Network and Protect share a single key, generated from the console's own **Settings → Control Plane → Integrations** page. Access uses a completely separate token, generated from inside the **UniFi Access application itself** (look for an API Token / Integrations menu there) — Access also runs on its own port, 12445 by default.
- **Legacy username/password**: a number of features aren't available through the official Integration API yet and use a secondary, local-session login instead — PoE port mode toggling, client block/unblock, and richer device stats on the Network side. Use a **local-only admin account** (Settings → Admins → Create New, tick "Restrict to local access only"), not a UI.com cloud account, and not one with MFA enabled. This is the most common cause of HTTP 403 errors if misconfigured.
- Click **Test Connection** to confirm everything's working before creating any devices.

### Drive (UNAS) controller

Create a separate **UniFi Controller (Drive)** device — the UNAS is its own appliance with its own login, independent of the Network console above:

- **Connection + Authentication**: IP address and port, then either an API key (Settings → Control Plane → Integrations, on the UNAS itself) or local admin username/password if your UNAS firmware doesn't yet support API keys.
- **SSH (fan control only)**: the UNAS's own REST API for fan control is currently read-only on UniFi firmware — writes return an HTTP error. To actually change fan modes, this plugin deploys a small helper service to the UNAS over SSH. Enable SSH on the UNAS (Settings → Control Plane → Console) and provide credentials here, then use the **Deploy fan control** button. Leave this section blank if you only want monitoring, not control.

### Creating devices

Both controller types have **Create Devices** buttons directly in their own configuration dialog (cameras, sensors, network devices, access doors, storage pools, disks, the fan), and most device types also have a matching **Discover…** action for bulk creation via an Action Group — useful for re-running discovery later without reopening the controller's dialog. Both approaches skip devices that already exist rather than creating duplicates.

## Features in detail

### Network devices, clients, and ports

Switches, access points, and gateways come in as devices reporting online/offline status, CPU/memory/temperature, uptime, and more. Ports can be individually enabled/disabled, power-cycled, or have their PoE mode changed. Clients can be blocked, unblocked, or disconnected, and a built-in purge tool helps manage the buildup of randomized-MAC devices (iPhones, Android phones, and Windows machines rotating MACs for privacy) that accumulate over time — with dry-run support before anything is actually removed.

### Firewall Policies, Traffic Routes, Port Forwarding, WLANs

Pick any of your own custom firewall policies, policy-based traffic routes, port forward rules, or WLANs from a live dropdown (built-in/predefined policies are filtered out — there are usually dozens, and toggling them isn't useful from automation) and control them as simple on/off devices. WLAN devices also support a dedicated password-change action.

### 5G/LTE gateway

If you have a U5G-class cellular gateway, it reports signal strength, carrier, radio technology, data usage, and roaming status alongside the usual network-device stats.

### Protect cameras

Motion, person/vehicle/package detection, and doorbell rings arrive as live-updating states and triggers. Audio detection (via the camera's own microphone — no separate sensor needed) covers smoke alarms, CO alarms, sirens, glass breaking, burglar alarms, car horns, barking, speech, and baby crying; each type needs enabling via the **Set Smart Detection Types** action, same as object detection. An `eventFeedLive` state tracks whether the live event connection is actually up, so a dropped connection and a genuinely quiet house are never confused with each other.

Also supported: snapshots, reboot, status LED, on-screen overlays (name/date/logo), a custom LCD status message (doorbell hardware), HDR mode, microphone volume, video mode (only options this specific camera actually supports are offered), and RTSP stream URLs for external viewers.

### Protect sensors

Door/window/garage contact, motion, leak, and general-purpose sensors, with temperature/humidity/light readings where supported. Each sensor's primary on/off state is picked sensibly by its mount type automatically, or can be pinned explicitly (Open/Closed, Motion, Leak, Alarm) if the automatic choice isn't what you want for a particular sensor.

### Alarm Manager — experimental

Arm/disarm by profile (Home/Away/Night-style), built from an undocumented endpoint with no real hardware available to verify it against — see the warning at the top of this README before relying on it. Separately, a genuinely **official** "Trigger Alarm Manager Webhook" action fires any alarm you've already configured with a webhook trigger inside Protect itself — this doesn't arm or disarm anything on its own, it just fires whatever you've set up.

### Access doors

Lock and unlock, with a configurable auto-relock timer for momentary unlocks (it re-locks on its own after a short window if the door's never opened, or waits for the door to close again if it is) and a separate "leave unlocked" mode for when you want the door to stay open until explicitly locked again.

### Drive (UNAS)

Storage pools and individual disks report capacity, usage, health, and temperature. Fan control (once deployed via SSH, see Setup) lets you pick Quiet/Balance/Cooling/Default modes and reports real RPM per fan. Reboot, shutdown, and wake-on-LAN are also available.

### Diagnostics

Almost every device type has a **"Dump Device Payload to Log"** button (in its config dialog) or action that logs the complete raw data UniFi is actually reporting — no debug mode required. This is the fastest way to get useful information into a bug report if a state looks wrong, blank, or missing. Debug logging (plugin config) adds further detail — API request paths, WebSocket reconnect attempts, and per-event timing — on top of that.

## Why both official API and legacy auth?

Ubiquiti's official Integration APIs are growing but still don't cover everything — notably PoE port mode, client block/unblock, and richer Network device stats, and (for Drive/UNAS) there's no official API at all yet. Where the official API covers a feature, this plugin uses it; where it doesn't, a secondary local-session login bridges the gap, clearly labelled in the source as `[INTEGRATION]` or `[LEGACY]` so it's obvious which path is in use and easy to migrate a given feature across as Ubiquiti's own API coverage expands.

## Known limitations

- Recording Mode and IR Mode cannot be changed — not exposed by the official Protect API.
- Traffic Rules and DPI app/category restrictions aren't built — on current UDM-class firmware, both appear to have been absorbed into the newer zone-based firewall and policy-routing systems this plugin already supports, rather than remaining distinct, live features worth building against.
- Two-way audio ("talkback") isn't built into this plugin — if you need it, HomeKit's own native camera audio (via a HomeKit bridge plugin) already covers this well.
- Guest voucher creation isn't built yet.
- Chimes and Protect Viewers (ViewPorts) aren't supported as device types yet.

See `ROADMAP.md` in this repository for the full, ongoing detail behind every item above, plus everything that's been confirmed, built, investigated, and deliberately left out along the way.

## License

MIT. See `LICENSE`.

## Acknowledgements

Built with reference to several other MIT-licensed open-source projects and community research along the way — full attribution for each is recorded in `ROADMAP.md` at the point it was used, including the openHAB and Home Assistant projects' own documentation of the Protect event stream shape, and the wider UniFi developer community's reverse-engineering of UNAS/Drive endpoints in the continued absence of an official API.

## Support

Open an issue on this repository. Including the output of the relevant device's "Dump Device Payload to Log" is the single most useful thing you can attach.
