# Roadmap

Backlog from reviewing Home Assistant's official `unifi` integration and its
underlying `aiounifi` library (Kane610, MIT License) for features/endpoints
this plugin doesn't cover yet. Commit this file so it survives between work
sessions - a prior version of this list (DOCS_TODO.md) was lost because it
was never added to a release.

## Package Camera support (3.12.9) - DONE

User's doorbell (UVC G6 Entry) has a secondary "package" lens; asked
whether it could be exposed, with a preference stated up front for a
linked child device rather than extra fields bolted onto the existing
camera (gave a real example from another Indigo plugin - a Z-Wave
multi-input sensor exposed as two separate devices sharing one physical
address - as the shape wanted).

Checked thoroughly before building rather than assuming: the official
camera schema and its `featureFlags` have nothing package-related in
either (checked both directly against the local spec copy). Confirmed via
a live controller that it's real and reachable two ways:

- **Officially**: `channelQuality` (the RTSP streaming endpoint's own
  enum) is `high`/`medium`/`low`/`package` - the exact same
  `/v1/cameras/{id}/rtsps-stream` mechanism already used for the other
  three qualities. Found while checking this that the backend already
  half-supported "package" as a quality (an unused `enablePackage` prop
  was already being read in `actionEnableRtspStreaming`) - the gap was
  only in the UI (no checkbox) and the states (no `rtspUrlPackage`).
- **Which cameras have one**: `hasPackageCamera` (confirmed `true` on the
  user's own doorbell) plus `hasPackageZoneSupportForPrimaryLens`/
  `...SecondaryLens` - all three legacy-bootstrap-only, not in the
  official schema.

Given the RTSP piece was ~90% already wired up as a 4th quality on the
existing device, that would have been the lower-effort path - said so
explicitly before building, but respected the user's clearly-stated
preference for a linked device instead of substituting the simpler
option unasked. Built `protectPackageCamera`: links to an existing
protectCamera via a parent picker (filtered to confirmed-package-lens
cameras when legacy data is available, falls back to listing every
camera otherwise), on/off enables/disables RTSP streaming for the
package lens specifically, reusing the existing RTSP methods rather than
duplicating them. Bulk discovery only auto-creates where
`hasPackageCamera` can actually be confirmed. Shipped in 3.12.9 as its
own standalone Action Group action; folded into `createCameraDevices`
in 3.12.10 instead, at the user's request, so it runs automatically from
the existing "Discover and Create Cameras" button/action rather than
needing a separate one - more consistent with how every other device
type's discovery already works here.

**Not built**: a package-lens-specific snapshot (checked - the official
snapshot endpoint's only parameters are `id` and `highQuality`, no
channel/lens selector, so a snapshot can only ever come from the primary
lens via the official API) and the package-zone feature flags themselves
as states (legacy-only, and it's not yet clear whether package-type smart
detection can be attributed to a specific lens or just reports at the
parent-camera level regardless of which lens triggered it - the existing
`packageDetected` state on the parent camera already covers detection
itself; this device is purely about the secondary lens's own stream).

**Rebuilt in 3.12.12** after feedback that the initial design was
inconsistent with the rest of the plugin: `type="relay"` with 2 states
didn't match `protectCamera`'s own `type="sensor"`/38-state structure, and
critically didn't even match the parent's own RTSP convention (dedicated
actions, not a relay toggle) - so "same structure" wasn't just a nice-to-
have, it was correcting a real design mismatch. Now full state parity,
kept in sync via direct state-copy on refresh and real-time WS mirroring
for anything event-driven (both the official and private-WS fast motion
paths). RTSP control moved to dedicated actions matching the parent's own
convention. Also completed `protectCamera`'s own half-built "package"
RTSP quality support while in there (states/checkbox were missing even
though the backend already read the prop, since 3.12.8).

## Comparison against another UniFi Indigo plugin (3.12.8)

A friend's separate, independently-written UniFi Protect plugin for
Indigo (`simons-plugins/indigo-unifi-protect`, official-API-only design)
turned out to be a genuinely useful comparison - read thoroughly, but
nothing copied; each idea checked against our own spec copy and/or a live
controller, then built our own way. Full detail in the 3.12.8 CHANGELOG
entry; summary here for anyone revisiting this later:

- **Audio detection** (smoke/CO/siren/baby cry/speech/bark/burglar/car
  horn/glass break via camera mics) - DONE. Confirmed official event
  (`smartAudioDetect`) directly against our own spec copy.
- **"Set Smart Detection Types" now also controls audio types** - DONE,
  and necessary: without it the audio states above could never actually
  fire, since Protect only reports types a camera's own
  `smartDetectSettings.audioTypes` is configured to listen for.
- **Event feed health gating** (`eventFeedLive`) - DONE. The core
  principle (an Indigo boolean can't express "unknown", so a dead event
  socket must be distinguishable from "nothing is happening") is sound
  and worth keeping as a standard going forward for any future live-
  event-driven state.
- **`armMode` confirmed real** - DONE. Directly improves the experimental
  Alarm Profile feature's "is anything armed at all" reliability; "which
  profile" remains a genuine gap (armMode has no profile-identifying
  field).
- **Capability-checking before writes** - DONE for `actionSetStatusLight`
  (`hasLedStatus`). Worth extending to other existing write actions if
  any of them turn out to need it in practice.
- **HDR mode, mic volume, video mode actions** - DONE, all confirmed
  official PATCH fields. Video mode's picker is populated from each
  camera's own `featureFlags.videoModes` where cached, full spec enum
  marked unverified otherwise.
- **Sensor primary-state override** - DONE. Auto (mount-type detection,
  unchanged default) or pin explicitly to Open/Closed, Motion, Leak,
  Alarm.
- **Trigger Alarm Manager Webhook** - DONE. Genuinely official
  (`POST .../alarm-manager/webhook/{id}`), separate from and
  complementary to the experimental arm/disarm feature - it fires an
  already-configured alarm action rather than arming/disarming anything
  itself.
- **"Delete Stream URLs"** - checked, already covered:
  `actionDisableRtspStreaming` already deletes every active stream.
- **Chime/Viewer (ViewPort) device types** - considered, not built: not
  in the user's own device inventory, lower priority than everything
  above. Revisit if ever relevant.

## Access door fixes (3.11.8-3.11.9)

**Confirmed real bug, fixed**: `unlock_door()` was calling a path that
doesn't exist at all (`/api/v1/developer/doors/{id}/unlocking_rule` -
404'd with "resource not found" the moment it was actually tried against
live hardware). Confirmed the real path and request body against the
official UniFi Access OpenAPI docs
(https://unifi-access-api-openapi.pages.dev): `PUT .../doors/{id}/unlock`,
body `{"actor_id"?, "actor_name"?, "extra"?}`, all optional. Now sends
`{"actor_name": "Indigo"}` so remote unlocks are attributable in UniFi's
own access history.

**DONE (3.12.0)**: `lock_rule`'s full type enum confirmed (cross-checked
official OpenAPI example against a working openHAB UniFi Access binding -
both agree on `keep_lock`): `keep_lock`, `keep_unlock`, `custom`,
`lock_now`, `lock_early`. Now wired up for real Lock support, leave-
unlocked mode, and the auto-relock timer - see the 3.12.0 changelog entry
for the full design.

**Still open - lastAccessUser/lastAccessTime**: these two accessDoor
states have no code writing to them at all. The data probably exists
(openHAB's binding has `last-unlock`/`last-actor` channels, aiounifiaccess
exposes `event.data.actor.name`), but community sources conflict on
whether it's available over WebSocket or requires registering a webhook
receiver (a materially bigger undertaking than anything built so far for
Access - an actual HTTP listener, not just reading the existing
connection). 3.12.0 added a debug-mode raw-event dump to `_on_access_ws`
specifically to check this directly: next step is the user triggering an
unlock with debug logging on and sharing what (if anything) comes through
on the existing WS connection, before deciding whether this needs a
webhook receiver or not.

## Active project (started 3.11.2) - "do everything we can here"

User's real device inventory (screenshot, confirmed): UDM Pro, USW 48 PoE
500W, USW Aggregation, 2x USW Flex Mini, AC Pro, U7 Pro, **U5G Backup** (the
5G/cellular WAN-failover gateway from the "needs hardware" section below -
now actually owned, unblocking that item), 10x Protect cameras (mostly G3
Flex - mic only, no speaker - plus a G6 Entry doorbell with a speaker), a
UA Hub Door Mini (Access), and a UNAS Pro. Deliberately wants broad, best-
effort support beyond just this exact inventory, for other users of the
open-source plugin - "if they don't work they can log bug reports."

Agreed build order (sequenced, not attempted all at once - verify-first
discipline matters more as scope grows, not less):

1. **U5G/5G gateway real fields** - DONE (3.11.6). Real payload confirmed
   `type="umbb"` (not `ucg`/`uxg` as speculated), model `UMBBE634`.
   Cellular states added under `elif device_type == "umbb"` in
   `_apply_netdev_state`: cellularMode/State (failover/ready), carrier
   (has_carrier/spn/networkoperator - note spn "Sky" vs networkoperator
   "O2 - UK" differ because Sky Mobile is an MVNO on O2's network, not a
   bug), signal (signal_percent/rsrp/rsrq/rssi/snr), radio tech (rat -
   was "LTE" even though the hardware is 5G-capable, i.e. not currently
   on a 5G connection), roaming, data usage (rxbytes/txbytes - note these
   arrive as strings in the payload, converted to MB), data_limited/
   data_warning, and network_reject_text (worth the user's own attention:
   the captured payload showed "Registration blocked by network" /
   http_captive_portal errors - may be normal for a dormant failover link
   with primary WAN up, may be worth checking - not something to guess at
   from here). `has_speaker: false` also confirmed directly in this same
   payload, consistent with the talkback note below.
   Also found and fixed while reading this device's live log: three
   device types (protectCamera, networkDevice, protectSensor) had an
   illegal explicit `<State id="onOffState">` redeclaration in Devices.xml
   - onOffState is a native state for relay/sensor base types and can't be
   redeclared; Indigo rejected it at load time ("illegal state key...
   native state keys cannot be overridden") and silently fell back to its
   own definition, discarding the custom trigger/control-page labels.
   Likely long-standing (predates this session), functionally harmless
   since Indigo's fallback kept onOffState/uiValue writes working, but
   alarming-looking in the log and worth being correct regardless - fixed
   in 3.11.6 by removing all three redeclarations.
2. ~~Talkback (two-way audio)~~ - **dropped.** Initially scoped as
   confirmed-official (`/v1/cameras/{id}/talkback-session`, doorbell-only
   since the G3 Flex cameras are mic-only with no speaker), with a real
   complication found: the endpoint returns an RTP URL + codec info
   (`{"url": "rtp://...", "codec": "opus", "samplingRate": 24000,
   "bitsPerSample": 16}`), not a simple "upload a sound file" call - would
   have needed shelling out to ffmpeg for Opus/RTP encoding, since Indigo's
   Python has no native audio codec support. Turned out to be moot: the
   user's separate HomeKitLink-Siri plugin project already has two-way
   audio working for the doorbell via HomeKit's own native camera audio
   protocol (confirmed in that project's own notes) - a different
   mechanism entirely, unrelated to Ubiquiti's talkback-session endpoint,
   but delivering the same real-world result already. Building this here
   too would duplicate working functionality for no real benefit. Leaving
   this note so a future session doesn't re-propose it from scratch.
3. **Network v2 API** (firewall policies, traffic rules, traffic routes)
   and **classic REST** (port forwarding, WLAN management, VLAN read, DPI
   restrictions, guest vouchers) - six distinct features; tackle one at a
   time with its own device type/states/actions and real-payload
   verification, same as everything else, not as one combined change.
   - **Firewall Policies** - DONE (3.12.1). Confirmed live against the
     user's own controller via an authenticated browser session (not
     documentation alone): real endpoint
     (`/proxy/network/v2/api/site/{site}/firewall-policies`), real field
     shapes (78 real policies read, including a client-MAC-targeted
     "Block Internet" example), and a real finding that writes need the
     FULL record body (confirmed via a real bug report from a different
     open-source project hitting validation failures on partial PUTs) -
     `set_firewall_policy_enabled()` always fetches fresh and PUTs the
     complete modified record back, never a partial body.
   - **Traffic Routes** - DONE (3.12.1). Same session, same endpoint
     family (`/proxy/network/v2/api/site/{site}/trafficroutes`), 2 real
     routes read (domain-based and client-based policy routing, both with
     `kill_switch_enabled`).
   - **Traffic Rules** - still NOT built, and probably shouldn't be, based
     on real investigation rather than left as a guess. With the user's
     explicit go-ahead, created a real test object through the Network UI
     (a simple "block internet for one device" rule) specifically to see
     what it generated, then deleted it afterward. Two findings:
     1. The newer "Object Oriented Networking" UI (Secure/Route/QoS/
        Schedule) doesn't write to `trafficrules` at all - it auto-
        generates entries in `firewall-policies` instead (tagged
        `origin_type: "object_firewall_rule"`, `predefined: true` even
        though user-created - worth knowing, since it means `predefined`
        doesn't cleanly mean "Ubiquiti's own built-in policy" as assumed
        when `getFirewallPolicyList`'s exclusion filter was written -
        both meanings happen to want the same filter, so no code change
        needed, but the reasoning was wrong until this was checked).
     2. This controller's unified "Policy Table" view (Settings → Policy
        Engine) lists every policy type that exists on this firmware
        generation: ACL Rules, DNS Records, Firewall, NAT Rules, Policy-
        Based Routes, Port Forwarding, QoS Rules, Static Routes. There is
        no "Traffic Rules" category at all. Combined with the
        `trafficrules` endpoint being live but permanently empty with no
        UI path found to populate it, this looks like a legacy/vestigial
        endpoint on current UDM-Pro-class, zone-based-firewall firmware
        (10.6.109) - probably still meaningful on older USG-class
        hardware running "classic" (non-zone-based) firewall mode, which
        this user doesn't have.
     Conclusion: what this user (and probably most current UDM/UDR-class
     users) would think of as "a traffic rule" is already covered by
     Firewall Policies (built, 3.12.1) and Traffic Routes (built, 3.12.1).
     Not building a separate, likely-dead feature on the strength of
     older documentation that may predate the zone-based firewall system.
     Revisit only if a real non-empty `trafficrules` payload ever turns up
     (e.g. from a bug report on older/different hardware), not before.
   - **Port Forwarding** - DONE (3.12.5). Confirmed live against the
     user's own controller: 6 real rules, clean field shape (`_id`,
     `name`, `enabled`, `pfwd_interface`, `src`, `dst_port`, `fwd`
     (destination IP), `fwd_port`, `proto`, `log`). Classic REST
     (`/proxy/network/api/s/{site}/rest/portforward`), same full-record-
     update discipline as the v2 features (`set_port_forward_enabled`
     fetches fresh, flips `enabled`, PUTs the whole record back).
   - **WLAN management** - DONE (3.12.5). Confirmed live: 7 real SSIDs,
     clean field shape (`_id`, `name`, `enabled`, `security`, `is_guest`,
     `x_passphrase` for the password - the `x_` prefix is UniFi's own
     convention for a write-only/sensitive field). Enable/disable via the
     device's on/off; password changes via a dedicated "Change WLAN
     Password" action (`actionChangeWLANPassword`) - the new password is
     never logged on either the success or error path, and the config-
     dialog field is marked `secure="true"`. The diagnostic dump
     (`dumpWLANPayload`) redacts `x_passphrase`/`x_iapp_key` even with
     debug logging on - a log file isn't where a WiFi password belongs.
   - **VLAN/network read** - not yet built as its own device type, but the
     same live session confirmed 15 real networks with a clean, simple
     shape (`_id`, `name`, `purpose`, `enabled`, `vlan_enabled`, `vlan`) -
     this one's read-only and lower-risk than the others, worth doing
     next.
   - **DPI restrictions** - NOT built, and possibly the same situation as
     Traffic Rules, worth checking before building rather than assuming:
     `dpigroup` has 2 real entries, but `dpiapp` (the actual per-app
     block/allow rules) came back completely empty (0) on this user's
     network, mirroring exactly what was seen with `trafficrules`. Given
     the newer "Object Oriented Networking" UI's "Secure" panel has its
     own "App" blocking checkbox (seen during the Traffic Rules
     investigation, 3.12.4), DPI restrictions may have been similarly
     absorbed into the Object/zone-based-firewall system on current
     firmware. Don't build from zero real examples - check with a live
     test the same way Traffic Rules was checked, if/when revisited.
   - **Guest vouchers** - NOT built. The list endpoint
     (`/stat/voucher`) came back empty, but that's expected either way -
     vouchers are ephemeral and only show up while unused, so an empty
     list doesn't tell us anything about whether the feature still works
     on this firmware (unlike DPI apps, where a *persistent* config being
     empty is the actual signal). The real unconfirmed piece is the
     *create* command's request shape (`/cmd/hotspot`) - never verified
     against a live payload. Would need a real test voucher created (and
     revoked) to confirm, same approach as the door lock_rule and Traffic
     Rules investigations - ask first, since it's still a live-config
     write.
   - Every built device type (`firewallPolicy`/`trafficRoute`/
     `portForward`/`wlanDevice`) is relay-type, on/off = enabled/disabled,
     with a dynamic dropdown menu (`getFirewallPolicyList`/
     `getTrafficRouteList`/`getPortForwardList`/`getWLANList`) to pick by
     name rather than needing a manually-typed `_id`. All reuse the
     existing legacy cookie/CSRF session (`_legacy_request`) - v2 uses the
     same auth as the classic REST surface, just a different base path and
     a plain-array response (not the legacy `{"data": [...]}` envelope) -
     confirmed directly, not assumed.
   - Note for future similar work: a live, authenticated browser session
     (Claude in Chrome) is by far the fastest way to confirm real
     endpoint/field shapes when available - used here instead of guessing
     from community documentation alone, and worth reaching for again for
     Traffic Rules and the remaining classic-REST items.
4. **VPN client connect/disconnect** - WS message keys already flow
   through the existing connection (see "WebSocket message keys not yet
   handled" below), just never modelled as device states/triggers.

Generic-robustness principle for other users' hardware (SmartPower/UPS,
other gateway models, etc.): `createNetworkDevices`/`_apply_netdev_state`
already creates a generic networkDevice for literally any adopted device
regardless of type, with a shared baseline of fields (status, model,
firmware, IP/MAC, uptime, CPU/memory, temperature) - that's the fallback
for hardware this project has never seen a payload for. Build dedicated
states for recognised device types as real payloads become available
(this user's or from bug reports), don't speculatively model fields for
hardware nobody's confirmed yet - the Drive/UNAS lesson applies here too.

**"Dump Device Payload to Log" button (3.11.2-3.12.7) - DONE for every
device type that can meaningfully have one**: on-demand diagnostic, logs
a device's full raw payload at info level immediately, no debug mode or
restart needed. protectCamera's version (`actionDumpCameraPayload`)
stayed an Action Group action since that's where the user wanted it;
every other type got a button in the device's own config dialog instead
(`(valuesDict, typeId, devId)` signature, same convention as
`createPortDevices`/`testFanControl`): networkDevice, clientDevice,
accessDoor, firewallPolicy, trafficRoute, portForward, wlanDevice,
alarmProfile, and - closing the consistency gap flagged earlier -
protectSensor (`dumpSensorPayload`, a direct per-sensor GET) and
unifiPort (`dumpPortPayload`, read off the parent switch's `port_table`
since a port has no standalone GET of its own - same lookup
`_update_port_states` already does for regular refreshes).

Sources:
- https://github.com/Kane610/aiounifi (MIT) - the actual UniFi API client,
  endpoint paths, and event taxonomy. Safe to adapt code from directly with
  attribution.
- https://github.com/home-assistant/core/tree/dev/homeassistant/components/unifi
  (Apache 2.0) - HA's entity/platform glue on top of aiounifi. Not directly
  portable (different framework), but useful as a feature/sensor idea list.

## Done - legacy/local-credential Protect client (3.8.0)

firmwareVersion/ipAddress/linkSpeed/uptime/voltage/recordingMode/irMode now
populate from the private bootstrap API (`GET /proxy/protect/api/bootstrap`),
behind the same console login already configured for Network's/Drive's
legacy auth - no new credentials needed. See the 3.8.0 changelog entry for
the full implementation details.

One correction worth recording: while building this, checked uilibs/uiprotect
(the actual library behind HA's integration, MIT licensed) directly rather
than going by what HA's UI merely displays - "it shows up in HA" doesn't
indicate which of HA's two API clients (public or private) supplied a given
field, which matters for knowing what to actually build. Also found that
recent uiprotect releases have dropped the old `channels[].rtspAlias` /
`isRtspEnabled` fields entirely ("Drop private model members home assistant
no longer uses"), implying HA's own integration has itself moved to the
official `rtsps-stream` endpoint for RTSP - the same one this plugin adopted
in 3.7.0. So the bootstrap client built here deliberately does NOT also wire
up old-style RTSP URLs; the official-API action-driven approach remains
correct and current.

## Shipped

### v3.6.1 - WebSocket reliability fixes (architecture review)

Went looking specifically for core architectural improvements rather than
features - "is there a better way to get live data via sockets rather than
polling" - by reading aiohttp's `heartbeat=` dead-connection handling and
Home Assistant's `hub/websocket.py` reconnect wrapper. Found two real bugs
in our own hand-rolled WebSocket client (we don't use aiohttp, so we
implement the WS protocol ourselves - see `class UnifiWebSocket`):

1. **No dead-connection detection beyond raw socket errors.** A half-open
   TCP connection (far end stops responding, never sends close/RST) would
   cause us to ping into the void forever, every 60s, with no ceiling -
   silently losing real-time updates until a manual plugin restart. Fixed
   with a consecutive-missed-pings counter (`WS_MAX_MISSED_PINGS = 2`) that
   forces a reconnect when nothing at all - not even a pong - comes back
   for two consecutive timeout cycles. This mirrors what aiohttp's
   `heartbeat=15` parameter does automatically when you use that library.

2. **401/403 during WS handshake was always treated as permanent.** For the
   Network controller's legacy cookie auth, a session can expire mid-run;
   HA's wrapper explicitly re-logs-in before restarting the websocket on
   any disconnect, treating auth failure as self-healing rather than fatal.
   We now support an optional `on_auth_failure` callback on `UnifiWebSocket`,
   wired for the Network WS only (the one with an actual login flow to
   retry) to force a fresh `_legacy_login()` before backing off and
   reconnecting. Protect/Access use static keys with no login flow, so they
   correctly keep the original "give up and log clearly" behaviour.

**Considered, deliberately not adopted:**

- **HA's reconnect timer is a flat 15 seconds, not exponential backoff.**
  Our existing 5s → 5min exponential backoff is more considerate of a
  struggling controller and we kept it - simpler isn't always better here.
- **Typed exceptions per HTTP status** (`BadGateway`, `ServiceUnavailable`,
  a specific `AuthenticationRateLimitError` for 429 responses with code
  `AUTHENTICATION_FAILED_LIMIT_REACHED`) - aiounifi's `interfaces/
  connectivity.py` has much finer-grained error typing than our blanket
  exception handling. Worth doing eventually so retry/backoff decisions can
  vary by failure type, but no concrete bug motivated it yet - noted here
  rather than built speculatively.
- **WS compression** (`compress=12` in aiohttp's `ws_connect`) - minor
  bandwidth optimisation, not worth the added protocol complexity in a
  hand-rolled client for the data volumes involved here.
- **SSO/MFA-aware login (TOTP, cloud SSO cookie flow)** - aiounifi's
  `Connectivity.login()` handles this in real depth. We deliberately tell
  users to use a local-only, non-MFA admin account instead, which sidesteps
  the entire problem rather than solving it. Not revisiting unless local-only
  accounts stop being viable for some reason.

### v3.6.0 - Expanded event taxonomy

Added EVT_* event parsing from the legacy WebSocket "events"/"alert" message
stream (previously only device:sync/sta:sync/device-port:sync were handled -
the events/alert message type was arriving over the same connection but
never read). New triggers: device overheat, PoE overload/disconnect, rogue
AP/DHCP detection, radar detected, possible interference, STP port blocking,
restarted, firmware upgraded/upgrade failed, adopted, gateway WAN transition
(failover), client roamed, controller update available, IPS/IDS alert, admin
login. Every networkDevice/clientDevice/unifiController also gets
lastEventKey/lastEventMessage/lastEventAt states updated on EVERY event,
including ones without a dedicated trigger, so you can build conditional
triggers around event keys this plugin hasn't special-cased yet.

Event key names and payload field semantics (key/msg/time/ap/gw/sw/user/
client/guest) cross-checked against aiounifi's `models/event.py` and
`models/message.py`.

## Buildable now (same auth, no new hardware needed)

### A previously-unknown API path

Legacy cookie auth also reaches a **v2 API** at
`/proxy/network/v2/api/site/{site}/...` (distinct from both the classic
`/proxy/network/api/s/{site}/...` REST API and the official
`/proxy/network/integration/v1/sites/{site}/...` Integration API). This is
what powers newer UniFi OS features:

| Feature | Path (relative to v2 base) | Notes |
|---|---|---|
| Firewall Policies (Zone-Based Firewall) | `/firewall-policies` | GET list, PUT `/firewall-policies/{id}` to update |
| Traffic Rules | `/trafficrules` | GET list, PUT `/trafficrules/{id}` to enable/disable |
| Traffic Routes | `/trafficroutes` | GET list, PUT `/trafficroutes/{id}` to save |

Response decoding differs from the classic API: v2 responses are a bare
JSON array/object (no `meta`/`data` envelope) and errors use `errorCode` +
`message` instead of `meta.rc == "error"`.

### Classic REST endpoints (same auth, not yet used by this plugin)

| Feature | Path | Notes |
|---|---|---|
| Port Forwarding | `/rest/portforward` | GET list, PUT `/rest/portforward/{id}` to enable/disable |
| WLAN management | `/rest/wlanconf` | GET list, PUT `/rest/wlanconf/{id}` to enable/disable or change password |
| Network/VLAN config | `/rest/networkconf` | GET list, PUT `/rest/networkconf/{id}` - read-only listing is lower risk than editing |
| DPI restriction apps | `/rest/dpiapp` | GET list, PUT `/rest/dpiapp/{id}` to enable/disable |
| DPI restriction groups | (see aiounifi `models/dpi_restriction_group.py`) | App-category blocking groups |
| Guest vouchers | `/stat/voucher` (list), `/cmd/hotspot` (create/delete) | Hotspot guest access codes |

### WebSocket message keys not yet handled

Beyond the EVT_* events added in 3.6.0, the same WS connection also carries
these `meta.message` values we don't act on yet (see aiounifi
`models/message.py` for the full list):

- `firewallgroup:add/sync`, `firewallrule:add/sync` - legacy firewall config
  changes (different from the v2 firewall-policies above)
- `portforward:add/sync/delete` - port forward config changes
- `wlanconf:add/sync/delete` - WLAN config changes
- `dpiapp:add/delete/sync`, `dpigroup:add/delete/sync` - DPI config changes
- `vpn:connect/disconnect`, `vpn-connection:sync`, `vpn-users:sync` - VPN
  client connect/disconnect - likely a good trigger candidate once we decide
  how to model VPN clients as devices
- `speed-test:update` - WAN speedtest results
- `ping-test:update` - network ping test results

Natural pairing: build each v2/REST feature's device type alongside handling
its corresponding WS message key for live updates, same pattern as
network_devices/clients today.

## Needs your hardware first (don't guess at field names again)

We learned this the hard way with Drive - every time we guessed at UniFi
payload field names instead of looking at a real response, we were wrong.
Don't repeat that for these:

### UniFi SmartPower / UPS Pro (device type `usp` in the Network API)

Ubiquiti doesn't distinguish "UPS" as a separate device type from SmartPower
PDUs in the API - both report as `usp`. Relevant HA sensor keys worth
replicating once we can see a real payload: `outlet_power`,
`smartpower_ac_power_budget`, `smartpower_ac_power_consumption`,
`ups_battery_level`, `ups_battery_runtime`, `ups_bypass_voltage`,
`ups_input_voltage`, `ups_output_current/power/power_factor/voltage`.
Also relevant: `EVT_XG_OutletPowerCycle` event (already recognised, not yet
trigger-mapped - no outlet device type exists to attach it to).

**When you get the Pro model**: enable debug logging, let it discover as a
generic networkDevice first, then dump its raw `/v1/sites/{site}/devices`
payload (same approach as the Drive debug dumps) so we can see actual field
names before building dedicated states/actions.

### 5G/LTE gateway hardware

No distinct device type in aiounifi's `DeviceType` enum - likely surfaces as
`ucg` (Cloud Gateway) or `uxg` (NextGen Gateway) via the existing generic
network device discovery, meaning it might already create a networkDevice
with *some* useful states today. Cellular-specific fields (signal strength,
carrier, data usage) aren't in any reference we've checked - needs a live
payload dump once you have the hardware.

### Smoke/CO detectors

Not covered by `aiounifi` at all (Network-only library). These are UniFi
Protect devices, likely surfacing through the Protect Integration API's
sensor model - possibly alongside or instead of the existing door/window/
motion sensor types this plugin already supports. Needs the actual hardware
and a payload dump through the existing Protect sensor discovery to confirm
whether it's recognised at all and what fields it reports.

### AlarmHub Kit (arm/disarm, zones) - BUILT (3.12.6), EXPERIMENTAL and UNVERIFIED

Revisited the old "no public API" conclusion rather than trusting a stale
note - still no *official* API (confirmed again directly against the
locally-held Protect OpenAPI spec: no arm/alarm-profile schema or path
exists in it), but there IS a real, specific undocumented one, confirmed
via two independent sources - a community forum script and a recent
(HA 2026.9+) HACS project (`violuke/ha_unifi_protect_alarm_bridge`) built
specifically for this:

- `POST /proxy/protect/api/arm/enable` and `.../arm/disable`, body
  `{"armProfileId": "<id>"}` for both (confirmed real, not guessed - the
  disable call taking a profile ID too is unintuitive but followed
  exactly as the reference shows it, not second-guessed).
- **Requires the configured Protect account to be Super Admin with MFA
  disabled** - the HACS project's author explicitly tested Protect Full
  Management, Network Site Admin, and user-management roles and reports
  all three were refused. This is a real security trade-off for whoever
  enables this feature, not a minor detail - documented in the device's
  own instructions text, not just in here.
- Arming state changes apparently propagate over Protect's own update
  websocket "within about a second" per the HACS project - not wired up
  here (this plugin's existing Protect WS handling doesn't know what
  message type to listen for, and guessing that felt like one guess too
  many on top of everything else already unconfirmed) - polling via the
  existing bootstrap refresh cycle is what's actually built.

**What's genuinely unconfirmed**: where arm profile data and current arm
state actually live in the bootstrap payload. Checked Mike's own real
bootstrap response directly (read-only, no arm/disarm attempted) and
confirmed he has zero Alarm Manager hardware adopted (`hubs`, `sirens`,
`fobs`, `relays`, `locks` all empty) - meaning there was nothing to
verify the actual data shape against, not even once. `get_alarm_profiles()`
and `get_alarm_state()` check every plausible location (top-level,
under `nvr`, under a `hub` entry; several plausible key name variants
each) rather than assuming one, and are written to degrade to "found
nothing" rather than crash or silently misreport.

**Built per the user's explicit instruction to make this as complete as
possible while prioritizing diagnosability**: `alarmProfile` device type
(one device per arm profile, same pattern as WLANs/firewall policies -
On arms to that profile, Off disarms), bulk discovery, and a "Dump Device
Payload to Log" button that deliberately dumps *more* than what the
current code looks for - every bootstrap top-level key (type/count), plus
the full content of anything plausibly alarm-related by name or by being
one of the Alarm-Manager-dependent hardware categories, plus what
`get_alarm_profiles()`/`get_alarm_state()` currently extract from it. This
is the mechanism for fixing the inevitable wrong guesses once a real user
with real hardware reports back - genuinely the point of this feature
existing at all before any such report comes in.

Not building the WS-based instant-update path, the siren/output-channel
control also mentioned in community sources, or the arm-profile
create/update/delete operations some MCP tooling references - all of that
compounds the amount of unverified surface for a feature that's already
entirely unverified at its core. Revisit once a real payload exists.

## Notes on licensing when pulling from these sources

- `aiounifi` is MIT - safe to adapt code from with an attribution comment,
  same approach as the Drive fan control script (hoxxep) and endpoint
  research (memphi2).
- HA's `unifi` component is Apache 2.0 and built on `homeassistant.core`'s
  async entity/coordinator framework - not realistically portable to
  Indigo's plugin model. Treat it as a feature/sensor idea list only, not a
  code source. If we ever do copy a non-trivial chunk of it verbatim, Apache
  2.0 requires retaining its copyright notice and stating what changed -
  more involved than MIT, so default to "write our own" unless there's a
  specific algorithm worth lifting.
