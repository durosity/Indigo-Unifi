# UniFi Plugin for Indigo Domotics

A comprehensive Indigo plugin for the UniFi ecosystem: **Network**, **Protect**,
**Access**, and **Drive (UNAS)**, with real-time updates via WebSocket and
HomeKit-friendly device types throughout.

Released under the MIT License by Durosity.

---

## What's new in 3.2.0

This release migrates to Ubiquiti's **official Integration APIs** wherever
possible, and re-architects the plugin around real-time WebSocket events
instead of aggressive polling.

### Highlights

- **Protect** moved to the official Integration API with Bearer-token auth and
  JSON WebSockets. Motion, smart detect, and doorbell events arrive instantly
  (~100ms) over the device-update and event subscription streams.
- **Access** integration rewritten against the official Developer API on its
  dedicated port (default 12445). Door state, lock/unlock, and access events
  now actually work.
- **Network** uses the Integration API by default, with a documented
  username/password fallback for endpoints not yet exposed by the official
  Integration API (PoE port mode, client block/unblock, rich device stats).
  Each method in the Network API class is labelled `[INTEGRATION]` or
  `[LEGACY]` so it's easy to see when a method can be promoted to the
  official path as Ubiquiti expands the API surface.
- **Drive (UNAS)** continues to use legacy auth — there is no official Drive
  API yet. As soon as one ships, this plugin will adopt it.
- **Polling architecture rewritten.** Real-time events come via WebSocket, so
  polling is only used for slow-changing stats (CPU, memory, temperature,
  fan RPM). Stats and sanity-check intervals are user-configurable in the
  Controller config UI, with recommended/min/max values shown in the
  dropdowns.
- **WebSocket reliability** — exponential reconnect backoff (5s → 5min) and
  permanent-failure detection so we stop hammering services that aren't
  available.
- All `<n>` tags converted to `<Name>` per project conventions.

### Future roadmap

- As Ubiquiti exposes more endpoints in the Network Integration API, those
  methods will be promoted from `[LEGACY]` to `[INTEGRATION]`.
- v3.3.0 will add: Protect Lights, Chimes, WLAN device types.
- When Ubiquiti publishes an official Drive API, that integration will be
  rebuilt around it.

---

## Authentication

The plugin uses three optional API keys plus an optional username/password
pair, depending on which features you enable:

| Feature             | Auth                                                       |
|---------------------|------------------------------------------------------------|
| Network (read/full) | **Network Integration API key** + **local admin username/password** |
| Protect             | **Protect Integration API key** (Bearer)                   |
| Access              | **Access Developer API key** (Bearer, port 12445)          |
| Drive (UNAS)        | **Local admin username/password** (no official API yet)    |

### Generating API keys

- **Network and Protect**: in the UniFi OS web UI, go to
  **Settings → Control Plane → Integrations** and create a key for each.
- **Access**: open the UniFi Access app → settings → Developer API. Note the
  port (12445 by default).

### Local admin account

The legacy username/password path **must** be a local-only admin:

1. Settings → Admins → Create New Admin
2. Set username and password
3. **Tick "Restrict to local access only"**
4. Do **not** use a UI.com cloud account
5. Do **not** enable MFA on this account

This is the most common cause of the HTTP 403 errors seen in earlier versions
of this plugin.

---

## Installation

1. Download `UniFi.indigoPlugin` from the latest release
2. Double-click to install in Indigo
3. Enable the plugin

## Configuration

### Step 1: Create the Controller device

1. **New Device → UniFi → UniFi Controller**
2. Fill in:
   - **Controller IP Address** (e.g. `10.0.1.1` for a UDM Pro)
   - **HTTPS Port** (443 for UDM Pro, 8443 for self-hosted)
   - **Site ID** (usually `default`)
3. Tick the features you want to enable (Network, Protect, Access, Drive)
4. Paste in the API keys for the enabled features
5. For Network or Drive, enter the local admin username/password
6. Configure update intervals (defaults are sensible — 5 min stats, 15 min
   sanity)
7. Click **Test All Configured APIs** to verify each path works
8. Save

### Step 2: Discover devices

In the same Controller config dialog, scroll to the **Create Devices** section
and click the buttons for whichever you want to import:

- **Cameras** — Protect cameras (one device per camera)
- **Sensors** — Protect sensors (door/window, motion, environmental)
- **Network Devices** — switches, APs, gateways
- **Access Doors** — UniFi Access doors
- **UNAS Device** — single device exposing storage stats
- **UNAS Fan** — dimmer-style device for fan mode (low/balanced/high)

### Step 3: Discover ports (optional)

To create devices for individual switch ports:

1. Go to **Plugins → UniFi → Discover Ports** (if menu present), or open the
   Controller and use the port discovery action.
2. Pick the parent switch, click discover.

Port devices respect a **Control Mode** prop:
- `poe` (default): on/off toggles PoE auto/off
- `link`: on/off enables/disables the port
- `powercycle`: on triggers power-cycle, off does nothing

---

## Real-time vs polled updates

| Update                                      | Method     |
|---------------------------------------------|------------|
| Camera: motion start/end                    | WebSocket  |
| Camera: person/vehicle/package detect       | WebSocket  |
| Camera: doorbell ring                       | WebSocket  |
| Camera: connection status                   | WebSocket  |
| Sensor: motion / contact / alarm            | WebSocket  |
| Network device: connect/disconnect          | WebSocket  |
| Switch port: link state, PoE state          | WebSocket  |
| Client: connect / disconnect / roam         | WebSocket  |
| Door: lock / unlock                         | WebSocket  |
| Camera/device: CPU, memory, temperature     | Polled (stats interval)     |
| UNAS: storage, RAID, disk health            | Polled (stats interval)     |
| Anything missed by WebSocket                | Polled (sanity interval)    |

Stats and sanity intervals are configurable per-controller from the dropdowns
in the Controller config. The `★` markers indicate recommended values.

---

## Architecture notes

### Why both Integration API and legacy?

The Network Integration API is currently narrower than the legacy
cookie-auth API. It doesn't yet expose `port_table`, `radio_table`, PoE port
overrides, client block/unblock, or per-AP client counts. Until it does,
Network device state monitoring needs the legacy path. We use the Integration
API for actions where it works, and legacy as a documented bridge.

In the source code, every Network API method is labelled `[INTEGRATION]` or
`[LEGACY]`, so promoting a method from one to the other is a one-place
change.

### Why no Drive API?

Ubiquiti has not yet published an official UniFi Drive (UNAS) API. The
plugin uses unofficial reverse-engineered endpoints over the same legacy
session as Network. When an official API ships, this will be rebuilt.

### Why is the Access port different?

UniFi Access exposes its developer API on a dedicated port (default 12445)
rather than going through the main UniFi OS proxy. The plugin connects
directly to that port with Bearer auth.

---

## Triggers (Events)

- **Camera**: motion, person, vehicle, package, doorbell ring, connect,
  disconnect, became dark, became light
- **Sensor**: motion, contact opened, contact closed, alarm triggered
- **Network device**: came online, went offline
- **Port**: link up, link down, PoE enabled, PoE disabled
- **Client**: connected, disconnected, blocked, unblocked
- **Door**: locked, unlocked

---

## Actions

- **Camera**: set recording mode, set IR mode, status light, overlays,
  status message, detections, reboot, take snapshot
- **Network device**: restart, enable/disable/toggle, set/toggle LED,
  locate (flash LEDs)
- **Port**: enable, disable, power-cycle, set PoE mode
- **Client**: block, unblock, reconnect
- **Access**: unlock door

Discovery actions:

- Discover all clients (with last-seen filter: 24h, 7d, 30d, 90d, any)
- Create unknown clients (guests etc.)
- Remove missing clients (cleanup)

---

## Logging

Enable **Show debug info** in the plugin config to see verbose API and
WebSocket logging. Debug logs include API request paths, WebSocket reconnect
attempts, state-change-trigger firing, and per-device update timing. Default
logging is concise — connection status, errors, and major events only.

---

## License

MIT License. See LICENSE.

## Acknowledgements

- Thanks to the openHAB and Home Assistant projects for documenting the
  Protect Integration API event stream shape.
- Thanks to the UniFi developer community for reverse-engineering Drive
  endpoints in the absence of an official API.
