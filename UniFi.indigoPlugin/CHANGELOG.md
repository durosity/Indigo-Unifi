# Changelog

All notable changes to this project are documented here. This starts at
3.2.0 (the official-Integration-API rewrite); earlier history predates
public release.

## [3.12.14] - Fixed NameNotUniqueError during bulk client discovery

User hit `NameNotUniqueError` running Create All Clients. Root cause:
`_create_or_skip`'s dedup check matches by prop (e.g. `clientMac`), not by
name, so two different clients with no UniFi-assigned name both fall back
to the same computed name (name → hostname → MAC vendor/OUI, e.g. "Apple,
Inc.") - common whenever more than one of the same device type is on the
network. Indigo requires device names to be unique across the entire
install, so the second create call failed outright, which previously
aborted the whole remaining batch since nothing caught it.

Fixed at the one shared creation point every device type in this plugin
already goes through, so every discovery flow benefits, not just clients:

- **Auto-disambiguation**: a name collision now appends " 2", " 3", etc.
  until a free name is found, rather than failing.
- **Per-item resilience**: `_create_or_skip` no longer lets any creation
  failure propagate - it's logged and treated like "already exists" (skip
  and continue), so one bad item can no longer abort an entire bulk-
  discovery run.

## [3.12.13] - Auto-create client devices on connect

Client device creation was previously action-only (Create All Clients /
Create Unknown Clients, run manually or via an Indigo Schedule). Added a
genuinely automatic path: a new Controller option creates a device the
moment UniFi's own WebSocket reports a brand-new client connecting, no
scheduled action needed.

- **"Auto-create on connect"** (off by default) - new checkbox on the
  Controller device. Hooks the existing `sta:connect` WebSocket message
  specifically (not `sta:sync`, which also fires for every already-known
  client on reconnect, and not `sta:disconnect`) - the one message type
  that actually means "this is new."
- **"Skip randomised/private MACs"** (on by default once auto-create is
  enabled) - reuses the exact same MAC-randomisation detection the Purge
  Randomised Clients action already uses (the standard U/L-bit check on
  the first octet), so a phone or other device that rotates its MAC for
  privacy doesn't generate an endless stream of new "client" devices.
- Respects the same deleted-clients list the manual discovery actions
  already use - deleting an auto-created device doesn't bring it
  straight back on its next connect.
- The `sta:connect` payload itself can be partial, so a newly auto-
  created device is immediately followed by a full state refresh (same
  mechanism already used for keeping existing client devices current),
  rather than waiting for the next periodic refresh to fill in IP
  address, connected AP/switch, etc.
- Refactored the actual device-creation step (name/MAC/props, the
  `_create_or_skip` call) into one shared helper used by both the manual
  bulk-discovery actions and this new automatic path, so both stay
  identical rather than risking drift between two copies of the same
  logic.

## [3.12.12] - Package Camera rebuilt for genuine structural parity with its parent

User pointed out Package Camera looked inconsistent with the rest of the
plugin - correctly: it was `type="relay"` with a minimal 2-state list,
while every other camera device is `type="sensor"` with a rich one, and
critically, the parent camera itself controls RTSP via dedicated actions,
not an on/off toggle - the 3.12.9 design didn't even match its own
parent's convention, let alone "same structure."

Rebuilt properly rather than patched:

- **`protectPackageCamera` is now `type="sensor"`** with the exact same
  38-state structure as `protectCamera` (motion/person/vehicle/package/
  audio detection, connection/event-feed health, doorbell, recording/IR/
  video/HDR mode, mic volume, status light, RTSP URLs, firmware/model/
  network info), plus its own `rtspUrlPackage`. Genuinely reasonable to
  mirror all of it: this is the same camera object in Protect's own data
  model, not separate hardware.
- Every mirrored state is kept in sync from the parent both on refresh
  (`_update_packagecam_states` now copies the parent device's current
  states directly, no separate API calls needed for any of it except the
  RTSP check) and **immediately** via events - the WS handlers mirror
  motion/detection/audio/doorbell updates onto a linked Package Camera
  device the moment they update the parent, including the private-WS fast
  motion path, not just the official one.
- **RTSP control moved to dedicated actions** ("Enable/Disable RTSP
  Streaming"), matching the parent camera's own convention instead of a
  relay on/off - `onOffState` now mirrors the parent's motion-based
  Detected/Clear instead, consistent with every other camera-type device.
- **Completed the parent camera's own package-quality support while in
  there**: `protectCamera` gets the `rtspUrlPackage` state and an
  `enablePackage` checkbox it was missing since 3.12.8 (the backend
  already half-read this prop but never had anywhere to put the result).
  Enabling/disabling package streaming from either the parent or the
  Package Camera device keeps both in sync immediately.

## [3.12.11] - Fixed the Controller device's own stats - most were never actually wired up

User reported `cameraCount`/`apCount`/`switchCount`/`gatewayCount`/
`networkDeviceCount` all showing 0 despite having 10 cameras and 8 network
devices, plus `protectVersion: Unknown` and `storageTotal`/`storageUsed`
both 0. Checked `_update_controller_states` directly rather than guess:
the counts were never written by any code at all (not a wrong field name
- genuinely never implemented), and protectVersion/storage always read
`nvr.get("version"/...)` from the *official* NVR object, which (confirmed
directly against the local spec copy, and again live) has only `id`/
`modelKey`/`name`/`doorbellSettings` - no version, no storage, nothing
else. All fixed now, each from a confirmed-real source:

- **Counts**: computed from devices this plugin already tracks in memory
  - no extra API calls. Network device type codes confirmed directly
  against a live controller: `usw`=switch, `uap`=access point,
  `udm`=gateway/console, `umbb`=5G/mobile-broadband gateway.
  `gatewayCount` also includes `ugw`/`uxg` (older USG and newer UXG
  gateway families) as well-established Ubiquiti naming, though only
  `udm` has actually been confirmed on real hardware so far.
- **protectVersion/storageTotal/storageUsed**: confirmed legacy-bootstrap-
  only, sourced from `bootstrap.nvr.version` and `bootstrap.nvr.
  storageStats.recordingSpace.{total,used}` (converted to GB to match
  what the declared states are actually labelled as - this API returns
  raw bytes).
- **cpuUsage/memoryUsage/uptime**: checked thoroughly for an NVR-specific
  source and there isn't one, in either the official object or the full
  legacy bootstrap one - Protect runs on the same physical console as the
  gateway, not separate hardware, so these now mirror whatever that
  gateway's own networkDevice already reports instead.

Worth knowing, found while checking storage data on a live controller:
Protect recording storage utilization was at 99.74% at the time. Not
something this plugin flags or alerts on (no change made here) - just
passing along since it came up directly while confirming this fix.

## [3.12.10] - Package Camera discovery folded into regular camera discovery

3.12.9 shipped Package Camera discovery as its own standalone Action Group
action. Moved into `createCameraDevices` instead, at the user's request -
more consistent with how every other device type already works here (a
config-dialog button on the Controller, not a separate action), since
this is genuinely "the cameras discovery flow" regardless of which
specific button or action triggers it. The existing "Discover and Create
Cameras" button and the "Discover Camera Devices" Action Group action
both already called this same function, so both now pick up package
cameras automatically with no new UI needed. Runs for already-existing
camera devices too, not just newly-created ones, so re-running discovery
after upgrading picks up package cameras for cameras you created before
this feature existed. The standalone "Discover Package Cameras" action
from 3.12.9 is removed.

## [3.12.9] - Package Camera support (linked device)

Some doorbell cameras (confirmed: UVC G6 Entry) have a second, downward-
facing "package" lens alongside their main view. Checked thoroughly before
building anything: the official camera schema and its featureFlags have
nothing package-related at all, but it's real and reachable two ways,
checked directly against a live controller:

- **Officially**: the RTSP streaming endpoint's own `channelQuality` enum
  is `high`/`medium`/`low`/`package` - this is the exact same
  `/v1/cameras/{id}/rtsps-stream` mechanism protectCamera's existing
  Enable/Disable RTSP Streaming actions already use, just a quality value
  that was missing from the checkboxes. The backend already half-
  supported this (an unused `enablePackage` prop was already being read).
- **Which cameras have one**: `hasPackageCamera` (plus two package-zone
  feature flags) - legacy-bootstrap-only, confirmed against a live
  doorbell's real payload, not in the official schema.

Built as its own linked device type (`protectPackageCamera`), not a 4th
checkbox on the existing camera - deliberately chosen over the simpler
option, since a package lens is conceptually a different camera even
though it lives on the same API object as its parent. Links to an
existing protectCamera device via a parent picker, which filters to
cameras confirmed to have a package lens when legacy data is available
and falls back to listing every camera otherwise. On/Off enables/disables
RTSP streaming for the package lens specifically, reusing the existing
RTSP methods rather than duplicating them. Bulk discovery only
auto-creates where `hasPackageCamera` can actually be confirmed (legacy
credentials configured) - without that, create one manually instead.

Also: renamed `get_alarm_debug_dump()` to the more accurate
`get_bootstrap_raw()` (it was never specific to Alarm Manager, just named
that way since that's the first thing that used it) - kept the old name
as a thin alias so nothing already calling it breaks.

## [3.12.8] - Eight improvements found by comparing notes with another UniFi Indigo plugin

A friend's separate, independently-written UniFi Protect plugin for
Indigo turned out to be an excellent source of ideas worth comparing
against - not by copying its code, but by checking each idea against our
own data/spec and building it our own way where it held up. Everything
below was verified against our own local copy of Ubiquiti's official
spec and/or a live controller before being built, same discipline as
everywhere else in this project.

- **Audio detection** - smoke alarm, CO alarm, siren, baby crying,
  speech, barking, burglar alarm, car horn, and glass break, all via
  whatever microphone the camera already has - no dedicated smoke/CO
  sensor hardware needed. Confirmed official event (`smartAudioDetect`)
  and its full `smartDetectTypes` enum, checked directly against our own
  spec copy rather than taken on faith. New states and Indigo triggers on
  protectCamera, handled with the same dedup/is-active pattern already
  used for motion and person/vehicle/package detection - deliberately
  NOT folded into onOffState/motionDetected, since a camera picking up a
  sound is a different claim than the camera seeing motion.
- **"Set Smart Detection Types" extended to cover audio types too** -
  without this, the audio detection above would never actually fire:
  Protect only reports audio event types a camera's own
  `smartDetectSettings.audioTypes` has been configured to listen for, and
  the action previously only ever read-and-passed-through that field,
  never let the user set it.
- **Event feed health gating** - new `eventFeedLive` state per camera,
  checked every 5-second poll tick (cheap - just reads whether the
  socket's own `.connected` flag, not a network call). The problem this
  solves: Indigo booleans have no "unknown" value, so if the Protect
  event socket drops, every live-event-driven state (motion, person/
  vehicle/package, all the new audio ones) would otherwise just freeze at
  its last value - looking identical to "nothing is happening" to any
  automation watching it. Now the moment the feed actually goes down,
  every one of those states gets explicitly forced to False and
  `eventFeedLive` goes False too, so "the feed is down" and "genuinely
  quiet" are distinguishable.
- **Alarm Manager's `armMode` field confirmed real** - checked directly
  against a live controller: `bootstrap.nvr.armMode` really exists
  (`{"status": "disabled", "armedAt": null, "breachDetectedAt": null,
  "breachEventCount": 0, ...}`). `get_alarm_state()` now uses this as its
  primary, trusted source instead of guessing across several possible
  locations. Honest about its limit: it confirms overall armed/disarmed
  reliably, but has no field identifying *which* arm profile is active,
  so that part of the experimental feature still falls back to the
  older guess-based matching.
- **Capability-checking before writes** - `actionSetStatusLight` now
  checks the confirmed `hasLedStatus` feature flag before sending
  anything, rather than letting the controller reject (or silently
  ignore) a command for hardware that doesn't have the feature.
- **Three new camera actions**, all confirmed-writable official fields:
  Set HDR Mode (auto/on/off, capability-checked against `hasHdr`), Set
  Microphone Volume (0-100, capability-checked against `hasMic`), and Set
  Video Mode - this last one's dropdown is populated live from each
  specific camera's own reported `featureFlags.videoModes` where that
  camera is already cached, rather than a static list regardless of
  hardware; falls back to the full confirmed spec enum marked
  "(unverified)" for a camera not yet cached, same discipline as every
  other "no real data yet" situation in this project. New `videoMode`/
  `hdrType`/`micVolume` states read these back from the real camera
  object.
- **Configurable primary-state override for Protect sensors** - the
  automatic mount-type detection added in 3.11.0 (door/window/garage →
  Open/Closed, leak → Wet/Dry, general-purpose → Motion) stays the
  default ("Auto"), but can now be pinned explicitly to Open/Closed,
  Motion, Leak, or Alarm for a sensor used for something its mount type
  alone doesn't capture.
- **Trigger Alarm Manager Webhook** - a new plugin-level action
  (confirmed official endpoint, `POST .../alarm-manager/webhook/{id}`,
  no request body). Deliberately separate from the experimental Alarm
  Profile arm/disarm elsewhere in this plugin: it doesn't arm or disarm
  anything, it only fires whatever alarm action is already configured in
  Protect for a given webhook trigger ID - genuinely official rather than
  reverse-engineered. Supports Indigo variable substitution for the
  trigger ID.

Checked and found to already be covered, so not duplicated: an explicit
"delete stream URLs" action - `actionDisableRtspStreaming` already does
exactly that.

## [3.12.7] - "Dump Device Payload to Log" now on every device type

Closed the last consistency gap: `protectSensor` and `unifiPort` now have
the same config-dialog diagnostic button every other device type already
had. `dumpSensorPayload` is a direct per-sensor GET, same pattern as the
door/policy/route diagnostics. `dumpPortPayload` is slightly different
under the hood since a port has no standalone GET of its own - it reads
the matching entry off the parent switch's `port_table`, the same lookup
`_update_port_states` already does for regular refreshes, so the dump
reflects exactly what the state-refresh code actually sees.

No functional changes elsewhere - purely closing out the diagnostic
coverage gap noted after 3.12.6.

## [3.12.6] - Alarm Manager arm/disarm (EXPERIMENTAL, UNVERIFIED)

Built at the user's explicit request despite having no Alarm Manager
hardware to verify against - confirmed this directly first by checking
the user's own real bootstrap payload (read-only): `hubs`, `sirens`,
`fobs`, `relays`, `locks` are all empty, meaning Global mode isn't even
active. Everything here is based on two independent community sources
(a forum script, a recent HACS project built specifically for this), not
Ubiquiti's own documented API - re-checked the local Protect OpenAPI spec
directly and confirmed it still has nothing for this.

- **New device type: Alarm Profile** (`alarmProfile`, relay-type - one
  device per arm profile, On arms to it, Off disarms). Confirmed real
  endpoint shape: `POST /proxy/protect/api/arm/enable` and `.../disable`,
  both taking `{"armProfileId": ...}`.
- `get_alarm_profiles()`/`get_alarm_state()` check every plausible
  location in the bootstrap payload (top-level, under `nvr`, under a hub
  entry; several key-name variants each) rather than assuming one, and
  degrade to "found nothing" rather than guess wrong silently.
- Auth failures (401/403) get a specific error message pointing at the
  one confirmed real requirement: the configured Protect account must be
  Super Admin with MFA disabled - no lesser role is reported to work.
  This is a genuine security trade-off, stated plainly in the device's
  own config instructions, not just buried in code comments.
- **"Dump Device Payload to Log"**, the most important piece of this
  release: deliberately dumps more than what the extraction functions
  currently look for - every bootstrap top-level key (type/count), full
  content of anything plausibly alarm-related or tied to the hardware
  categories Alarm Manager's Global mode depends on, plus what the
  extraction functions currently pull from all of that. This is how a
  real user's bug report actually becomes fixable, rather than another
  round of guessing.
- Bulk discovery ("Discover Alarm Profiles") alongside manual single-
  device creation.

Deliberately NOT built: the WS-based instant-update path a community
source mentions (this plugin's existing Protect WS handling doesn't know
what message type to listen for, and guessing one felt like one guess
too many here), siren/output-channel control, and arm-profile CRUD -
all would compound an already entirely-unverified feature. Full
reasoning and what's confirmed vs. guessed in ROADMAP.md.

## [3.12.5] - Port Forwarding and WLAN management

Two more of the six originally-planned classic-REST/v2 features, both
confirmed against a live controller first.

- **New device type: Port Forward** (`portForward`, relay-type - on/off =
  enabled/disabled). Config picks the rule by name from a live dropdown.
  States: forwardName, destinationIP, destinationPort, forwardPort,
  protocol, status.
- **New device type: WLAN** (`wlanDevice`, relay-type - same convention).
  Enable/disable an SSID from its dropdown; a new "Change WLAN Password"
  action handles the passphrase separately. The new password is never
  logged on success or failure, the config field is marked secure, and
  the diagnostic dump redacts the password field even with debug logging
  on.
- Both get bulk-discovery actions ("Discover Port Forwards"/"Discover
  WLANs") and a "Dump Device Payload to Log" button, same pattern as
  every other device type.
- Both reuse the existing legacy cookie/CSRF session - classic REST, same
  auth already in place, no new credentials.

**Also investigated, not built**: DPI restrictions may have the same
"migrated to the newer Object system" situation as Traffic Rules -
`dpigroup` has real entries but `dpiapp` (the actual per-app rules) came
back completely empty, mirroring what was seen with `trafficrules`
before concluding that one was likely vestigial. Didn't build from zero
real examples - flagged in ROADMAP.md for a proper check next time.
Guest vouchers also not built - the list endpoint being empty doesn't
mean anything (vouchers are ephemeral by design), but the *create*
command's shape still isn't confirmed against a real payload.
VLAN/network read-only listing has a confirmed field shape from this
same session but isn't built as its own device type yet - next up.

## [3.12.4] - Traffic Rules investigated properly - and probably won't be built

No code changes - documentation only, recording a real investigation so
it isn't repeated from scratch later. With explicit go-ahead, created a
temporary test object through the Network UI to see what it actually
generates, then deleted it. Findings: the newer "Object Oriented
Networking" UI writes to `firewall-policies`, not `trafficrules` - and
this controller's unified Policy Table (which lists every policy type
that exists on this firmware: ACL Rules, DNS Records, Firewall, NAT
Rules, Policy-Based Routes, Port Forwarding, QoS Rules, Static Routes) has
no "Traffic Rules" category at all. Combined with the endpoint being live
but permanently empty with no UI path found to populate it, this looks
like a legacy/vestigial concept on current UDM-Pro-class, zone-based-
firewall hardware - likely still real on older USG-class "classic"
firewall-mode hardware, which this user doesn't have. What a traffic rule
would mean here is already covered by Firewall Policies and Traffic
Routes (3.12.1). Full reasoning in ROADMAP.md.

## [3.12.3] - Bulk discovery for Firewall Policies and Traffic Routes

New Action Group actions, same pattern as "Discover Network Devices":
"Discover Firewall Policies" (creates a device for every custom/non-
predefined policy) and "Discover Traffic Routes" (every route). Safe to
re-run - already-created devices are matched by policyId/routeId and
skipped, not duplicated (added both as new dedup keys in
`_create_or_skip`, alongside the existing cameraId/doorId/deviceMac etc.).
Manual single-device creation via the dropdown picker (3.12.1) still
works as before for anyone who'd rather add them one at a time.

## [3.12.2] - Fixed a plugin-breaking typo in 3.12.1 (wouldn't start at all)

Both new device types in 3.12.1 had `<n>` instead of the required
`<Name>` in Devices.xml - my own error writing those two blocks, confirmed
directly against the raw file: every other device in Devices.xml already
had the correct tag, these two were the only ones wrong. Indigo refused to
start the plugin at all ("required XML element <Name> is missing"). The
generic XML validation this project has used all session (just checking
the file parses as well-formed XML) doesn't catch a wrong-but-valid tag
name like this - it was structurally fine XML, just not what Indigo's
schema expects. Added a stricter check for this specifically (every
`<Device>` has a real, non-empty `<Name>` child) to the validation used
before packaging, so this exact mistake gets caught here before reaching
the user, not after a failed plugin start.

## [3.12.1] - Firewall Policies and Traffic Routes (v2 API)

First of the six long-planned v2/classic-REST network features (see
ROADMAP.md's "Buildable now" list). Confirmed everything below against
the user's own live controller via a browser session with an
authenticated cookie (not from documentation alone): real endpoint paths,
real field shapes, and a real finding that the write path requires the
full record body, not a partial update (confirmed via a real bug report
from a different open-source project that hit validation failures with
partial PUTs).

- **New device type: Firewall Policy** (`firewallPolicy`, relay-type -
  on/off = enabled/disabled). Config picks the policy by name from a live
  dropdown, filtered to exclude Ubiquiti's dozens of built-in predefined
  policies - only user-created ones are listed, since those are the ones
  worth toggling from automation (e.g. a "Block Internet" policy for
  specific client MACs). States: policyName, action (Allow/Block/Reject),
  hits, lastHit, status.
- **New device type: Traffic Route** (`trafficRoute`, relay-type - same
  on/off convention). Policy-based routing - e.g. routing specific
  domains/IPs/clients via a VPN gateway, with an optional kill switch.
  States: routeDescription, matchingTarget (Domain/Internet/IP), 
  killSwitchEnabled, status.
- Both reuse the existing legacy cookie/CSRF session (`_legacy_request`)
  against the v2 API's different base path and response shape (a plain
  array, not the legacy `{"data": [...]}` envelope) - no new credentials
  or auth mechanism needed.
- Both get a "Dump Device Payload to Log" config-dialog button, same
  pattern as every other device type that has one.

**Traffic Rules (the third v2 feature) deliberately not built yet**: the
endpoint is live and reachable on the user's controller, but currently has
zero configured rules, so there's no real payload to confirm its exact
shape against - same discipline as everywhere else in this project.
Revisit once either the user has a real example or a test rule is
created and confirmed with their permission first (this would modify
live firewall configuration, even temporarily, so it isn't done
unprompted). Port forwarding, WLAN management, VLAN read, DPI
restrictions, and guest vouchers (the three classic-REST items) are still
fully unbuilt - next up per the agreed one-at-a-time order.

## [3.12.0] - Door auto-relock, real Lock support, leave-unlocked mode

A momentary unlock (3.11.9) was staying unlocked indefinitely - there's no
built-in auto-relock from UniFi's side at all on this door; the plugin
has to drive it. Also confirmed (via a working openHAB UniFi Access
binding, cross-checked against the one official example) the full set of
valid `lock_rule` values: `keep_lock`, `keep_unlock`, `custom`, `lock_now`,
`lock_early` - this unlocks everything below.

- **Auto-relock timer**: a momentary unlock now re-locks itself
  (`lock_rule` type `lock_now`) after 10s if the door is never opened
  (covers doors with no position sensor, `doorState: none`, same as this
  user's Front Door), or waits for the door to physically close again if
  it is opened (the hardware re-locks itself once closed - this just
  stops tracking once that's observed, no redundant API call). Runs on
  every 5s poll tick, independent of the stats/sanity intervals.
- **Lock now actually does something**: cancels any active unlock/leave-
  unlocked rule and locks immediately (`lock_rule` type `lock_now`) -
  previously a no-op pending confirming the real API (see 3.11.9).
- **New `unlockMode` config option** (momentary / leave unlocked):
  controls what the device's own Unlock button does. "Leave unlocked"
  uses the confirmed-real `keep_unlock` rule and never starts the
  auto-relock timer, by design - it's supposed to stay unlocked until
  explicitly locked.
- **Three standalone Action Group actions**, available regardless of the
  device's own unlockMode setting: "Unlock Door (Momentary)" (existing
  action, renamed, now also starts the relock timer), "Unlock Door (Leave
  Unlocked)" (new), "Lock Door" (new).

**lastAccessUser/lastAccessTime**: confirmed these have no code writing to
them at all - not a bug in the usual sense, just never implemented.
Community sources disagree on whether actor/timestamp data for this is
available over WebSocket or requires a webhook receiver (a materially
bigger undertaking - an HTTP listener registered with the controller, not
just reading an existing connection) - added a debug-mode raw-event dump
to `_on_access_ws` to determine this directly against the user's own
controller before building either way. See ROADMAP.md.

## [3.11.9] - Fixed the real cause of "Unlock does nothing": wrong endpoint entirely

3.11.8 fixed the Lock/Unlock *action routing* (Indigo sending Lock/Unlock
as distinct values from TurnOn/TurnOff), but unlock still failed in
practice: `unlock_door()` was calling
`/api/v1/developer/doors/{id}/unlocking_rule`, which doesn't exist on the
real API at all - confirmed via the official UniFi Access OpenAPI docs
(unifi-access-api-openapi.pages.dev), which is also why it 404'd
("resource not found") rather than failing on auth or permissions. Fixed
to the real endpoint, `PUT .../doors/{id}/unlock`, with the actual
documented request body (`actor_name: "Indigo"`, so remote unlocks are
attributable in UniFi's own access history, rather than the old made-up
`{"type": "custom", "interval": 5}` body that belonged to neither the old
nor the new path).

Also confirmed (same source) that an explicit Lock command may genuinely
be possible via a separate real endpoint (`lock_rule`), not just "the door
re-locks on its own" as previously assumed - not implemented yet pending
confirming the full set of valid values it accepts. See ROADMAP.md.

## [3.11.8] - Access door Lock/Unlock buttons actually work now

Two real, confirmed bugs, both from the same root cause:

1. **Lock/Unlock buttons did nothing.** Confirmed via Indigo's own
   official action-class documentation: `indigo.kDeviceAction.Lock` and
   `.Unlock` are distinct enum values, separate from `TurnOn`/`TurnOff`/
   `Toggle` (each other's "complementary action", per Indigo's own docs) -
   sent instead of TurnOn/TurnOff for IsLockSubType devices. The shared
   `actionControlDevice` handler never checked for these at all, so
   clicking Lock or Unlock matched nothing, `target` stayed `None`, and
   the function returned immediately - confirmed, not guessed, before
   fixing.
2. **Icon showed "Unlocked" while text correctly showed "Locked".**
   Confirmed from the onOffState boolean actually used (3.9.3 set it to
   `not is_locked` - "on" = unlocked) against the icon Indigo actually
   displayed for that value - Indigo's own IsLockSubType icon convention
   is onState=True → Locked, the opposite of what 3.9.3 used. Flipped to
   `value: is_locked`.

Both fixed together: `is_on()` in `actionControlDevice` now recognises
Lock (→ True) and Unlock (→ False), matching the corrected onOffState
convention, and the accessDoor branch now unlocks on `target=False`
(covers Unlock and a plain TurnOff should Indigo ever send that too)
instead of the old `target=True`.

Also added "Dump Device Payload to Log" to accessDoor's config dialog
(same pattern as networkDevice/clientDevice, 3.11.3-3.11.5) - directly
useful for confirming this fix, and for any door issue going forward.
Also corrected the device's instructions text, which still said "Use the
On/Off buttons."

## [3.11.7] - networkDevice onOffState now means online/offline, not enabled/disabled

Reconsidered from 3.11.0: prompted by the UDM Pro case (reported
disabled=true while fully online and passing traffic, cause never
confirmed), connectivity is the more generally useful thing to see at a
glance for a network device than an admin-enabled flag whose meaning
isn't even reliable for every device class. onOffState now reflects
isConnected ("Online"/"Offline"); the separate `disabled` state is
unchanged and still tracks the raw admin-enabled flag for anyone who
wants it specifically. `actionToggleDevice` already read `disabled`
directly rather than onOffState, so this doesn't affect enable/disable
actions - checked before making the change, not after.

**Worth knowing if you have existing triggers**: anything set up against
this device's generic on/off (Indigo's built-in "turns on"/"turns off"
trigger, not the plugin's own "Device Online"/"Device Offline" custom
trigger, which already existed separately and is unaffected) will now
fire on connectivity changes instead of admin-enable/disable changes.

## [3.11.6] - 5G/LTE gateway support, plus a real onOffState bug fix

**5G gateway (U5G Backup) states added**, built entirely from a real
payload - closes out a roadmap item open since the original `aiounifi`
research. Confirmed `type="umbb"`, not `ucg`/`uxg` as speculated. New
cellular states on networkDevice (populated only when `device_type ==
"umbb"`): cellularMode/State, cellularHasCarrier/CarrierName/
NetworkOperator, cellularRAT, cellularSignalPercent/RSRP/RSRQ/RSSI/SNR,
cellularRoaming, cellularDataRxMB/TxMB (converted from the payload's
string byte counts), cellularDataLimited/Warning, cellularRejectReason.

**Also found and fixed**: three device types (protectCamera, networkDevice,
protectSensor) had an illegal explicit `<State id="onOffState">`
redeclaration in Devices.xml - onOffState is a native state for relay/
sensor base types and Indigo rejects redeclaring it ("illegal state
key... native state keys cannot be overridden"), silently falling back to
its own definition and discarding the custom trigger/control-page labels
that redeclaration specified. Likely long-standing (predates this
session) and functionally harmless in practice - Indigo's fallback kept
onOffState/uiValue writes working regardless - but alarming-looking in the
log and worth being correct regardless. All three redeclarations removed.

## [3.11.5] - Dump Device Payload moved into the device's own config dialog

networkDevice and clientDevice's "Dump Device Payload to Log" (3.11.3/
3.11.4) moved from an Action Group action to a button in the device's own
config dialog, at the user's request - makes more sense there than needing
a separate Action Group for a one-off diagnostic. Same underlying logic,
just invoked differently (`dumpNetworkDevicePayload`/`dumpClientPayload`,
now `(valuesDict, typeId, devId)` signature, same convention as
`createPortDevices`/`testFanControl`). protectCamera's version stays as an
Action Group action, unchanged.

## [3.11.4] - "Dump Device Payload to Log" extended to clientDevice

Same diagnostic action as 3.11.3's networkDevice version, now also on
client devices.

## [3.11.3] - On-demand "Dump Device Payload to Log" action for network devices

User's suggestion, and a better one than 3.11.2's debug-mode dump: a
standing action, same pattern as the existing camera payload diagnostic -
logs a device's full current payload at info level immediately, no debug
mode or plugin restart needed. Useful generally for any networkDevice
whose states look wrong/blank, not just the 5G gateway it was originally
needed for.

Also: talkback (two-way audio) has been dropped from the roadmap - turns
out it's already working via a separate project (HomeKitLink-Siri +
HomeKit's own native camera audio), unrelated to anything built or planned
here. See ROADMAP.md for the full note.

## [3.11.2] - Network device payload diagnostic (5G gateway support pending real data)

The roadmap has flagged the U5G/5G gateway as "needs real hardware" since
the original `aiounifi` research - cellular-specific fields (signal
strength, carrier, data usage) aren't in any reference checked. User now
has one (U5G Backup). Added a debug-mode raw payload dump to the existing
generic network device state update, matching the diagnostic pattern used
for cameras/doors/fan control all session. Dumps every network device
(not just the gateway) since we don't yet know what `type` string Ubiquiti
uses for it to filter on specifically. No functional change - purely
diagnostic, pending what the dump actually shows.

## [3.11.1] - protectCamera gets the same Detected/Clear treatment

Requested as a follow-up to 3.11.0 - camera `onOffState` now shows
"Detected"/"Clear" instead of generic on/off, matching the general-purpose
Protect sensor convention, since cameras are being used here as motion
sensors. Fixed in both places that write it: the official events WS
handler and the private fast-path handler (3.9.0), so it's consistent
regardless of which one is actively driving a given camera's motion state.

One pre-existing edge case worth knowing about, not changed here: a
freshly linked camera has no onOffState write at all until its first real
motion event, so it'll show Indigo's own blank default until then rather
than "Clear" - same as before this fix, just noting it.

## [3.11.0] - Meaningful on/off text across the board, not just Access doors

Follow-up to the Locked/Unlocked fix: audited every other device type
showing generic on/off to find what each one's boolean actually represents
in code, then gave each the same `uiValue` treatment rather than leaving
them all meaningless by default.

- **networkDevice** (switches/APs/gateway): `onOffState` was always tied
  to admin-disabled state, not online/offline (that's `status`, separate
  and unaffected). Now shows "Enabled"/"Disabled".
- **clientDevice**: was `connected AND not blocked` collapsed into one
  on/off. Now distinguishes "Blocked" from "Offline" from "Connected" -
  two different reasons to be "off" that a bare on/off couldn't tell apart.
- **unifiPort**: already had two different control modes (PoE vs. link-
  state) driving the same boolean depending on a per-port setting. Now
  shows "PoE On"/"PoE Off" or "Link Up"/"Link Down" - whichever this
  specific port is actually configured for, not a generic on/off that
  doesn't say which.
- **protectSensor**: was always tied to motion, even for pure door/window/
  garage contact sensors with no motion component - meaning some sensors
  would've sat at "Clear" forever, never reflecting what they actually
  monitor. Confirmed the real `sensorMountType` enum against the official
  schema (`door`/`window`/`garage`/`leak`/`none`) rather than guessing, and
  made both the underlying value and its text match what each sensor
  actually is: door/window/garage → Open/Closed (from `isOpened`), leak →
  Wet/Dry (from `alarmActive`, the only "triggered" signal available for
  these), general-purpose (`none`) → Detected/Clear (from `motionDetected`,
  matching the DSC Alarm plugin's convention).

protectCamera (motion → Detected/Clear) deliberately left as-is for now -
lower priority, can revisit later.

## [3.10.3] - Individual per-fan RPM states

3.10.2 collapsed all fan tachometer readings into one `fanRPM` summary
(fastest fan) without surfacing that as a deliberate trade-off - asked
directly why 2+ real readings weren't all shown. Added `fan1RPM`..`fan4RPM`
alongside the existing `fanRPM` summary, each written only when hwmon
actually reports that fan (an unpopulated header on a given board just
keeps Indigo's own unwritten-state default rather than this asserting a
reading for hardware that isn't there).

## [3.10.2] - fanRPM now reads real hardware data, plus a deployment-detection bug found along the way

**fanRPM**: the 3.10.1 diagnostic confirmed `/sys/class/hwmon/hwmon0/fan1_input`
and `fan2_input` return real, plausible RPM values on this hardware (fan3/
fan4 read 0 - almost certainly unpopulated headers on this board, not a
read failure, since the files exist and return a valid number). `fanRPM`
now reads these via the same SSH access already used for fan control,
reporting the fastest-spinning fan as the single representative figure.
Only attempted when SSH fan control is confirmed deployed; falls back to
the REST field (which has no RPM data at all, confirmed earlier) otherwise.

**Found while investigating this - a real, independent bug**: the
diagnostic also showed `indigo_fan_control.service` crash-looping on the
user's UNAS (restart counter climbing every ~10s, `/usr/local/sbin/
indigo_fan_control.sh: No such file or directory`). `is_deployed()` was
only checking for the marker file and the systemd *unit* file, never the
actual script the service tries to execute - so if the script alone ever
goes missing while the marker and unit file are untouched, every check
(including the periodic one that's supposed to catch exactly this) kept
reporting "deployed" and nothing flagged the problem. `is_deployed()` now
also checks for the script file itself.

This is a detection fix, not an automatic repair - it stops the plugin
from wrongly trusting a non-functional SSH path (mode-setting and the
previously-wrong RPM source), but **does not by itself redeploy the
missing script or stop the crash loop**. If you're seeing this, click
"Install/Update Fan Control on UNAS" on the Drive controller device to
fix it directly - don't wait for a background check to do it for you,
since none currently does.

## [3.10.1] - fanRPM diagnostic (no fix yet, pending real hardware data)

fanRPM always reads 0 because the fan-control REST payload only ever
contained `availableProfiles`/`currentProfile` (confirmed against a real
payload back when the Drive API was first built) - there's no RPM field in
it to read. Rather than guess at a different field name (the mistake this
whole session has been about avoiding), checked whether the SSH access
already deployed for fan control might expose real tachometer data
instead: standard Linux hwmon convention pairs a `pwmN` (write, controls
speed - already read by the existing fan diagnostic) with a `fanN_input`
(read, actual measured RPM) in the same hwmon directory, but this is
driver/board-specific and needs confirming against the real hardware
before building anything.

Added a read-only probe for `/sys/class/hwmon/hwmon*/fan*_input` to the
existing "Run Fan Control Diagnostic" action, alongside the PWM register
read it already does. No behaviour change otherwise - this is purely
diagnostic, same as the camera payload/motion timing diagnostics earlier
this session, pending what the probe actually finds on this UNAS.

## [3.10.0] - UNAS fan is now a proper speedcontrol device, not a dimmer

The fan has three real modes (Quiet/Balance/Cooling) plus Default
(hand control back to firmware) - a fixed set of named states, not a
continuous level. It was modeled as a dimmer with modes mapped onto 0/50/
100% brightness purely because that's what got built first; `speedcontrol`
is Indigo's actual built-in type for "a fixed number of speeds" and fits
this far better - confirmed via the official Device Base Class reference
(`speedIndex`/`speedIndexCount`/`speedIndex.ui`).

- `driveFan` changed from `type="dimmer"` to `type="speedcontrol"`.
  **Existing fan devices need to be deleted and recreated** (via the
  normal "Create Fan Device" button) - same unavoidable cost as the Access
  door fix in 3.9.3, for the same reason: a device's fundamental type
  isn't something that updates in place.
- Default `speedIndexCount` is 4, confirmed to map directly onto our four
  modes with no XML override: 0=Default, 1=Quiet, 2=Balance, 3=Cooling.
- Replaced `actionControlDimmerRelay` (brightness-snapping logic, now
  gone entirely) with `actionControlSpeedControl`, handling SetSpeedIndex,
  SetSpeedLevel (0-100, rounded to the nearest of our 4 indices),
  IncreaseSpeedIndex/DecreaseSpeedIndex, RequestStatus, TurnOn/Toggle (map
  to Default), and TurnOff (logs a clear warning and does nothing - there's
  no real "off" for a NAS cooling fan, and actually stopping it risks the
  hardware overheating).
- Callback name, its exact `(action, dev)` signature, and the
  `kSpeedControlAction.*` enum were confirmed against the real source of a
  production Indigo plugin rather than assumed by analogy with the dimmer
  handler - an initial single-argument draft would have been wrong, the
  same kind of mistake this whole session has been about avoiding.
- Device state display uses the same `uiValue` mechanism confirmed for
  Access doors in 3.9.3 (`speedIndex.ui`), not `UiDisplayStateId` - that
  still does nothing for built-in device types like this one.

## [3.9.3] - Found the actual mechanism for Locked/Unlocked (3.9.2 was wrong)

3.9.2's `UiDisplayStateId` fix did nothing - confirmed the hard way, across
a programmatic refresh, a manual dialog dismiss, and a full delete-and-
recreate of the device, none of which changed the displayed state even
though the installed Devices.xml was verified correct on disk throughout.
That ruled out caching entirely and pointed at something more fundamental.

Checked docs.indigodomo.com/2026.1 directly (prompted by a real working
counter-example - a lock device from another plugin in this same Indigo
install correctly showing "unlocked", not generic on/off). The actual
mechanism, confirmed in the official Device Base Class reference:

- For `type="relay"` devices, the device list's State column always shows
  `onOffState` - `UiDisplayStateId` does not override this, which is why
  3.9.2 had no effect no matter how the device was created or refreshed.
- What DOES control the displayed *text* is the optional `uiValue`
  parameter on `updateStateOnServer()` - e.g.
  `dev.updateStateOnServer("onOffState", value=True, uiValue="Unlocked")`.
  This sets a display-only string for that state without affecting the
  raw value used in triggers/conditions.
- `pluginProps["IsLockSubType"] = True` is a separate, Indigo-recognized
  marker that unlocks the native `indigo.device.lock()`/`unlock()`
  commands for a relay device.

Access doors now set `IsLockSubType` on creation (with a one-time
migration for existing devices, since pluginProps don't retroactively
update any more than states or display-state do), and `_update_door_states`
passes `uiValue="Locked"`/`"Unlocked"` when writing `onOffState`. The
`UiDisplayStateId` addition from 3.9.2 is left in place (harmless, and may
still matter for other UI surfaces like Control Pages) but the real fix is
this one.

## [3.9.2] - Access doors now show Locked/Unlocked as their primary status

`accessDoor` had no `UiDisplayStateId` set, so as a relay-type device it
defaulted to showing the generic on/off indicator as its status - which is
what lock/unlock actually control, but reads as meaningless for something
called a door. Set to show `status` (locked/unlocked) instead, which is
always meaningful - every Access door has a lock, not every one has a
position sensor wired in. `doorState` (open/closed) is untouched and still
populates normally for doors that do have a position sensor; this only
changes which state is shown as the device's primary status in the device
list.

## [3.9.1] - Visibility fix for the fast-path suppression check

First live test of 3.9.0 showed the private channel reporting the same
~3.3s latency as the official one for a single motion event - directly
contradicting the expected speedup. Before drawing any conclusion from
that: the timing diagnostic logs unconditionally on both channels
regardless of whether the official handler's double-fire suppression
actually triggered, so there was no way to tell from the log alone
whether a duplicate trigger fired or the suppression correctly stepped
aside. Added a debug line for the skip case specifically
(`"skipped (fast path active) - not fired again here"`) so this is
directly verifiable next time rather than inferred.

Also worth noting: that same test event landed immediately after all
three Protect WebSockets (devices, events, and private) reconnected
together at the same moment, which is a plausible confound - a message
arriving right as a connection recovers could easily be delayed
regardless of which channel carries it. Need a few motion events from a
stretch where the private connection has been stable (not mid-reconnect)
before concluding anything about whether it's actually faster.

Private connection also reconnects considerably more often than the
other two (~24s apart in that log, versus ~1-2 minutes for the devices
channel) - checked uiprotect's own heartbeat settings, which are
identical (180s) across all three connection types, so this isn't a
missing client-side keepalive on our end. Most likely this specific
legacy endpoint simply has a more aggressive server-side connection
lifetime than the official API's channels - the existing fast 1-second
clean-close reconnect (3.8.4) already minimises the gap this causes.

## [3.9.0] - Fast motion detection via Protect's private update channel

3.8.3 established that motion detection via the official Integration API's
events subscription has a consistent ~3.3s delay. Before accepting that as
an immovable floor, we tested it properly: timed the Protect app's own
live-view motion indicator against the clock. The app reacts near-
instantly - so the delay is specific to the official API's events
subscription, not the camera's own detection pipeline.

The app almost certainly runs on `/proxy/protect/ws/updates` - Protect's
original real-time channel, in continuous use for years before the
official Integration API existed. Confirmed this is still live and
decoded its wire format by reading uilibs/uiprotect (MIT License), the
library behind Home Assistant's Protect integration:

- It's a binary protocol, not JSON text: each message is two consecutive
  frames (an "action" frame, then a "data" frame), each with an 8-byte
  header (`packet_type`, `payload_format`, `deflated`, `unknown`,
  `payload_size`) followed by its payload. `payload_format == 1` means
  JSON (optionally zlib-compressed if `deflated` is set) - confirmed
  against synthetic test data before relying on it against a live system.
- The camera-reference field on this channel's event objects is called
  `camera`, not `device` like the official API - confirmed against
  uiprotect's `Event` model rather than assumed, since getting a field
  name wrong here would silently drop every message the same way the
  original events-WS envelope bug did in 3.8.1.
- Authenticates with the exact same session the legacy bootstrap login
  (added in 3.8.0) already established - no new credentials needed.

Added:
- `UnifiWebSocket` gained a `binary_passthrough` mode so a connection can
  hand raw bytes to its handler instead of assuming every frame is JSON
  text.
- A new private-channel connection, started automatically whenever legacy
  bootstrap auth is already working, used *only* for motion detection.
  Smart-detect (person/vehicle/package) and doorbell remain on the
  official events WS, which continues to work correctly - just more
  slowly - for those.
- The official events handler now checks this connection's live state on
  every message and steps aside for motion specifically while it's up, so
  one real occurrence doesn't fire `motionDetected` twice (once fast, once
  ~3s later). If the private connection ever drops, the official handler
  resumes covering motion itself automatically until it reconnects - this
  is a live check each time, not a cached flag that could go stale.
- Entirely additive and optional: with no legacy credentials configured,
  behaviour is unchanged from 3.8.4.

## [3.8.4] - Found a third, silent WebSocket reconnect path - and an unnecessary delay on it

Full debug logs across an 8-minute live session (several different
cameras, including a different model) confirmed the ~3.2-3.4s "new motion"
and ~1.6-3.3s "noticed it ended" numbers from 3.8.3 are rock solid -
treating that as the genuine, inherent floor now.

Separately, the same logs showed `[...-protect-devices] WebSocket
connected` recurring every 1-2 minutes throughout the whole session, with
neither of the two debug lines that would indicate our own zombie-connection
detection or a read error ever appearing. That ruled out both, and pointed
at a third return path in `_read_frames` that had no logging at all: when
the **server** sends a proper WebSocket close frame (not an error - a
deliberate "I'm done with this connection"), the code just returned
silently. Looks like Ubiquiti's NVR applies some kind of periodic
connection-cycling policy specifically to the Protect "devices" subscription
- plausible given it's a much quieter channel than "events", which never
disconnected once in the same window.

- That clean-close path is now logged (`"Server closed the connection
  (clean close)"`), so this kind of thing is visible directly instead of
  needing a multi-message log-correlation exercise to prove by elimination.
- More importantly: `_loop` was applying the *same exponential backoff*
  meant for genuine connectivity failures to this completely normal case,
  needlessly delaying reconnection by several seconds every time the server
  did this - dozens of times over a session. A clean server-initiated close
  now reconnects after a short fixed 1-second delay instead, while actual
  failures still back off exponentially as before (5s up to 5min) - the two
  cases are no longer conflated.
- Practical effect: brief windows of missed live camera/sensor property
  updates (connection status, settings changes) via this specific
  WebSocket should now be substantially shorter and more frequent-but-
  brief rather than occasional multi-second gaps.

## [3.8.3] - The "lag" wasn't lag - fixed the diagnostic and a real dedup bug

User-supplied debug logs from 3.8.2 showed numbers in consistent pairs -
one reading around 3.2-3.4s, then a second, larger one (8s, 12s, 22s, up to
47s) - with zero WebSocket reconnects anywhere in a 9-minute, dozens-of-
events sample. That ruled out the connection-dropping theory and pointed
at the diagnostic itself.

**Root cause:** a single physical motion/smart-detect occurrence generates
more than one WebSocket message over its lifetime - an initial one when it
starts, zero or more "still active" continuations while it's ongoing (same
event id, `end` still null), and a final one once it ends. `start` never
changes across any of them. The 3.8.2 diagnostic computed `now - start` for
every message indiscriminately, so for the end-of-event message it was
really measuring total event duration, not latency - a 47-second reading
meant a 47-second-long motion event that was actually noticed in ~3 seconds,
not 47 seconds of lag. The true "time to detect brand new motion" was the
~3.2-3.4s cluster all along - consistent across different cameras, most
likely UniFi's own camera-side confirmation debounce before it publishes the
event at all, which isn't something a plugin sitting on the other end of a
WebSocket can speed up.

**Also found and fixed a real bug while digging into this:** the handler
didn't distinguish a genuinely new event from a continuation of one already
in progress, so a long-running or repeatedly-updated motion/smart-detect
zone could re-fire the `motionDetected`/`personDetected`/etc. trigger on
every interim update, not just once at the true start. Event ids are now
tracked per controller so triggers fire exactly once per occurrence.

The debug-mode timing log now reports what actually happened: `"Xs to
detect new event start"` for genuine new detections, `"still-active update
... (not a new detection)"` for continuations, and `"event lasted Xs, Ys to
notice it ended"` for the close-out message - three different, honestly
labelled numbers instead of one misleading one.

## [3.8.2] - Motion event timing diagnostic

User reported a noticeable lag between real-world motion and
`motionDetected` updating, now that 3.8.1 fixed the dead events handler.
Audited the WebSocket read path for anything that could add delay on our
side: the socket read is a plain blocking call processed the instant bytes
arrive (no polling interval, no batching), and the motion handler itself
only does local state writes - no network round-trip before updating
Indigo. Found nothing in our own code that should add seconds of delay.

Added a debug-mode log line comparing Ubiquiti's own reported event start
time (millisecond precision, set when their camera/NVR decided the event
began) against the moment we actually process it - covers every event type
(motion, smart detect, ring), not just motion. This gives a precise number
for whether the lag happens before the event reaches us (camera detection +
NVR publish time - not something a plugin can speed up) or between the
message landing on our socket and us handling it (which would point back at
our own code). No behaviour change otherwise.

## [3.8.1] - Protect WebSocket events were never actually firing

Found while chasing why `lastMotion` never populated despite motion being
confirmed in the Protect app. The real bug was much bigger than one state:
**neither Protect WebSocket handler has ever matched a real message**,
confirmed against developer.ui.com/protect/*/openapi.json.

- **Events WS** (`_on_protect_events_ws` - motion, person/vehicle/package/
  animal detection, doorbell rings): the real message envelope is
  `{"type": "add"|"update", "item": {"type": "motion"|..., "device": "...",
  ...}}` - the actual event lives under `item`, not `event`. The old code
  checked `message.get("event")`, which is never present, silently fell
  back to treating the *whole envelope* as the event, read its outer
  `type` ("add"/"update" - never "motion"), and returned early every time
  because `device` (nested inside `item`) was never found at the top
  level. Result: `lastMotion`, `lastPersonDetection`, `vehicleDetected`,
  `packageDetected`, `doorbellRinging`/`lastDoorbellRing`, and their
  matching triggers have not fired via this path since Protect moved to
  the official Integration API - motion-related automations that appeared
  to work were relying on something else (a trigger on `isConnected`/
  `status`, or coincidence), not this.
- **Devices WS** (`_on_protect_devices_ws` - live camera/sensor state
  changes): same family of bug. The real envelope's `item` is the full (or
  partial) device object, discriminated by `modelKey` ("camera"/"sensor"/
  etc.), with the device's own `id` nested inside it - not a separate
  top-level `id`/`itemType` pair as the old code assumed. This meant
  camera/sensor state changes (connection status, settings changes made in
  the Protect app, etc.) only ever appeared after the next periodic stats
  poll, never instantly as the WebSocket is meant to provide.
- Also added `smartDetectLoiterZone` to the recognised smart-detect event
  types (loitering detection), confirmed as a real, same-shaped event type
  in the official schema - a trivial addition once the envelope fix was in.

Both fixes are a straight unwrap-the-right-key correction; no other logic
changed. If you have triggers on motion/person/vehicle/package/doorbell
that never seemed to fire, they should start working now.

## [3.8.0] - Camera monitoring fields now populate via legacy bootstrap auth

The fields restored in 3.7.3 (firmwareVersion, ipAddress, linkSpeed, uptime,
voltage, recordingMode, irMode) now actually populate. Confirmed against
uilibs/uiprotect (MIT License - the library behind Home Assistant's Protect
integration): this data lives on the older private/local Protect API
(`GET /proxy/protect/api/bootstrap`), behind the exact same console login
(`POST /api/auth/login`) already used for Network's and Drive's legacy
auth - not a Protect-specific credential, so no new setup is needed if
you've already got a local admin account configured for Network.

- `UnifiProtectAPI` now optionally authenticates against this private API
  when the controller's existing local admin username/password are
  present, following the identical pattern already proven for Network and
  Drive (`_legacy_login`, retry-once-on-401, etc.) - this time reading the
  CSRF token straight from the login response's `x-csrf-token` header
  (confirmed as the real mechanism via uiprotect's source), rather than
  decoding it out of the JWT cookie.
- The bootstrap payload returns every camera in one request regardless of
  count, so this rides the existing bulk-refresh cycle with no added
  per-camera request burst - same discipline as the 3.6.4 rate-limit fix.
- Entirely optional and silent if not configured: no local admin account
  on the controller means these specific fields just stay blank, same as
  before, nothing else is affected.
- RTSP URLs are NOT sourced from here. Recent uiprotect releases have
  themselves dropped the old private-API RTSP alias fields ("Drop private
  model members home assistant no longer uses"), indicating HA's own
  integration has moved to the same official `rtsps-stream` endpoint this
  plugin already uses (since 3.7.0) - that remains the current, correct
  mechanism.
- Connection test button and controller startup log now report legacy
  bootstrap auth status for Protect alongside the existing API key check.

## [3.7.3] - Restored camera states removed in 3.7.2

- 3.7.2 removed firmwareVersion, ipAddress, linkSpeed, uptime, voltage,
  recordingMode, and irMode after confirming none of them are populatable
  via the official Protect API. Restored at the user's request: device
  states shouldn't be removed without asking first, confirmed-unpopulatable
  or not.
- These states are not written to by anything right now (removing them
  wasn't the issue - writing a misleading "Unknown"/0 into them forever
  was), so they'll stay blank until a real data source exists. See
  ROADMAP.md for the plan: a legacy/local-credential Protect client reading
  the private bootstrap endpoint, reusing the same local admin account
  already configured for Network/Drive, which does carry this data per
  camera. Not built yet.

## [3.7.2] - Camera states and actions fixed against the real schema

The 3.7.1 diagnostic confirmed the bulk camera list and a fresh single-
camera GET return byte-for-byte identical payloads (17 top-level keys,
matching the published OpenAPI schema exactly) - so 3.6.4's rate-limit fix
cost nothing, there's no second hidden bug, and camera monitoring data is
genuinely limited to what that 17-key object contains. That, plus an
exhaustive re-check of every documented camera endpoint, surfaced several
more places where code was written against the old/legacy Protect API's
field names rather than the official Integration API this plugin actually
uses. All fixed in this release:

**States removed (confirmed unavailable via any camera endpoint - no field
exists anywhere to populate them from, same conclusion as RTSP in 3.7.0):**
firmwareVersion, ipAddress, linkSpeed, uptime, voltage, recordingMode,
irMode. These always showed "Unknown"/0/blank for every camera, regardless
of model or firmware - not a bug to fix, just data the official API doesn't
expose yet. Removed rather than left perpetually misleading.

**Fixed:**
- `model` now reads the camera's `type` field (e.g. "UVC G3 Flex") before
  `modelKey` (a generic device-class string, always "camera" regardless of
  actual hardware - confirmed against a live payload). Previously checked
  `modelKey` first, so the specific model name was never reached.
- **Enable Detection Types** → renamed **Set Smart Detection Types** and
  rebuilt entirely. Previously sent `{"recordingSettings":
  {"enableMotionDetection": ...}}` - not a real field, would never have
  changed anything. The real schema has no on/off "motion detection"
  toggle; smart detection is a specific list of AI object types
  (person/vehicle/package/animal/face/licensePlate) sent as
  `smartDetectSettings.objectTypes`, alongside a required `audioTypes`
  array the action now reads from the camera and carries through unchanged
  so it doesn't silently clear audio-alarm detection settings it wasn't
  asked to touch.
- **Set Status Message** now sends the correct `lcdMessage` shape
  (`{"type": "CUSTOM_MESSAGE", "text": ...}`). The schema is a
  discriminated union with four fixed shapes; a bare `{"text": ...}`
  (what this previously sent) matches none of them.

**Recording Mode and IR Mode actions now fail clearly instead of silently
no-op'ing or erroring cryptically.** Both previously sent fields
(`recordingSettings`, `ispSettings`) that don't exist in the writable
schema at all - confirmed via the complete list of fields the camera PATCH
endpoint accepts (name, osdSettings, ledSettings, lcdMessage, micVolume,
videoMode, hdrType, smartDetectSettings). There's currently no supported
way to control either via the official API; the actions now log a clear
explanation and point to the Protect app instead of attempting a call that
was always going to fail.

**Confirmed working correctly already, no changes needed:** Set Status
Light (ledSettings), Set Overlays (osdSettings), macAddress and statusLight
states, all WebSocket-driven motion/person/vehicle/package/doorbell
detection.

## [3.7.1] - Camera payload diagnostics (no functional fix yet)

- User reports that beyond RTSP, several other camera states
  (uptime, voltage, recordingMode, model, linkSpeed) are also blank, and
  specifically recalls RTSP URLs having worked before - which doesn't fully
  square with 3.7.0's finding that the official schema has no `channels`
  field. The published OpenAPI spec documents Ubiquiti's intent, not
  necessarily every field a specific console/firmware actually sends, so
  this needs checking against a real payload rather than settling it from
  docs alone.
- Also worth ruling out: 3.6.4 switched routine camera refreshes from an
  individual `GET /cameras/{id}` to the bulk `GET /cameras` list (to fix
  the HTTP 429 rate-limit burst) - if the list endpoint returns a less
  complete object than the single-camera endpoint in practice (even though
  the OpenAPI spec says both use the same schema), that fix could have
  traded one bug for a different one.
- Added a debug-mode dump of the raw camera payload on every refresh,
  tagged with whether it came from the bulk list or a fresh single-camera
  GET, so the two can be compared.
- Added a new **Run Camera Payload Diagnostic** camera action that fetches
  both in one go, logs each in full, and reports any difference in which
  top-level fields each response includes - no need to enable debug mode or
  wait for a natural refresh to catch it.
- No states were changed by this release - it's purely diagnostic, pending
  what the dumps actually show.

## [3.7.0] - Fixed RTSP URLs (were never populated)

- `rtspUrlHigh`/`rtspUrlMedium`/`rtspUrlLow` read `camera.channels[].isRtspEnabled`/
  `rtspAlias` - fields that belong to the old, unofficial local Protect API
  (the one community tools like pyunifiprotect reverse-engineered), not the
  official Integration API this plugin actually uses. Confirmed against
  Ubiquiti's published OpenAPI spec (developer.ui.com/protect/*/openapi.json):
  the official `camera` schema has no `channels` field at all, so this loop
  could never populate anything, for anyone, on any camera, ever.
- The official API treats RTSPS streaming as a separate, opt-in resource:
  `GET /v1/cameras/{id}/rtsps-stream` reports which qualities (high/medium/
  low/package) currently have an active stream (all null until something
  creates one), `POST` with a `qualities` array creates them, `DELETE`
  removes them. Added `get_rtsps_streams`/`create_rtsps_streams`/
  `delete_rtsps_streams` to UnifiProtectAPI matching this exactly.
- Added two new camera actions: **Enable RTSP Streaming** (checkboxes for
  high/medium/low/package, default high+medium+low; idempotent - already-
  active qualities are left alone) and **Disable RTSP Streaming** (removes
  whatever's currently active). RTSP URL states are populated as a direct
  result of running these, not during routine polling.
- Deliberately NOT polled automatically: there's no bulk-list endpoint for
  RTSPS stream status, only a per-camera GET, and checking it on every
  camera during a routine refresh would reintroduce exactly the kind of
  request burst that tripped Protect's 10-requests/second rate limit in
  3.6.4. Run the action once per camera when you actually want streaming;
  it'll stay set until you explicitly disable it.

Known related issue not fixed in this release: several other camera states
(firmwareVersion, ipAddress, linkSpeed, uptime, voltage, irMode,
recordingMode) also read fields that don't exist in the official camera
schema and will likely need the same treatment - see ROADMAP.md.

## [3.6.4] - Fixed Protect HTTP 429 burst on startup

- Every protectCamera/protectSensor device was forcing its own individual
  `GET /cameras/{id}` (or `/sensors/{id}`) request the moment its
  communication started, completely redundant with the bulk
  `refresh_cache()` the controller had just run moments earlier covering
  every camera/sensor/light/chime in one shot. With enough cameras starting
  up in the same second (10 in this case, matching Protect's own
  10-requests-per-second rate limit exactly), this produced a repeating
  `Protect API HTTP 429: Too many requests` error for every camera beyond
  the first ~10, every time the plugin started.
- Camera/sensor device startup now reads from the cache the controller just
  warmed (`refresh=False`) instead of re-fetching individually - the same
  bulk-refresh-then-cached-read pattern the periodic stats poll already
  used correctly. Confirmed safe: `controller["protect_api"]` is only ever
  set after `refresh_cache()` completes, so a device can never see the
  controller as "Protect enabled" with a cold/empty cache.

## [3.6.3] - Fixed "state key not defined" on existing devices

- Devices created before v3.6.0 didn't have the new lastEventKey/
  lastEventMessage/lastEventAt states (added to unifiController,
  networkDevice, and clientDevice) - Indigo caches each device's actual
  state list at creation time, so editing Devices.xml doesn't retroactively
  extend devices that already exist. This caused a repeating
  `state key lastEventKey not defined (ignoring update request)` error
  for anyone who installed 3.6.0+ on top of an existing setup rather than a
  fresh install.
- `deviceStartComm` now calls `dev.stateListOrDisplayStateIdChanged()` for
  every device on every plugin start, refreshing its state list to match
  the current Devices.xml. This fixes the immediate issue on upgrade to
  this version with no manual action needed, and prevents the same class of
  bug for any states added in future releases - existing devices just pick
  them up on the next plugin start instead of needing to be deleted and
  recreated.

## [3.6.2] - Fixed "Menu Action" calling convention

- The 5 standalone plugin actions with no deviceFilter - Discover Camera
  Devices, Discover Network Devices, Discover All Clients, Discover Unknown
  Clients, Remove Missing Clients - were written as
  `def menuXxx(self, valuesDict, typeId):`, the calling convention for
  MenuItems.xml (the Plugins-menu dropdown) and for device ConfigUI button
  callbacks. Actions.xml always calls its callbacks with a single
  `indigo.PluginAction` argument regardless of whether a deviceFilter is
  present, so running any of these from an Action Group failed with
  `'PluginAction' object has no attribute 'get'` the moment someone actually
  used one that way instead of through a device's config dialog. Rewrote all
  5 to `def menuXxx(self, action):`, reading the user's controller selection
  from `action.props` instead.

## [3.6.1] - WebSocket reliability fixes

Reviewed aiohttp's `heartbeat=` dead-connection handling and Home
Assistant's UniFi websocket reconnect wrapper for architectural gaps in our
own from-scratch WebSocket client (we don't depend on aiohttp, so we
implement the WS protocol ourselves). Found and fixed two real bugs:

- **Zombie connection detection.** Previously, on a read timeout we sent a
  ping and kept waiting indefinitely - with no limit on how many consecutive
  timeouts we'd tolerate. A half-open TCP connection (the far end stops
  responding but never sends a close/RST - common behind NAT, a sleeping
  host, or a hung remote process) would cause us to ping into the void
  forever without ever reconnecting, silently losing real-time updates
  until the plugin was restarted. Now, after `WS_MAX_MISSED_PINGS` (2)
  consecutive timeout cycles with zero frames received - not even a pong -
  we force the connection closed and let the normal reconnect/backoff loop
  take over.
- **401/403 on the Network WebSocket no longer gives up permanently.**
  Previously any auth rejection during the WS handshake was treated as a
  dead end requiring a plugin restart. For the Network controller
  specifically, this is wrong: the legacy cookie session can expire during
  normal operation, and a 401 on reconnect usually just means "log in
  again", not "these credentials are permanently bad". The WebSocket client
  now accepts an optional `on_auth_failure` callback; the Network WS wires
  this to force a fresh legacy login before its next reconnect attempt,
  which then picks up the refreshed session cookie automatically. Protect
  and Access use static Bearer/API-key auth with no login flow to retry, so
  they're left with the original behaviour (correctly giving up and logging
  clearly, since there's nothing to self-heal).

Both fixes found by reviewing Kane610/aiounifi (MIT) and
home-assistant/core's `unifi` component (Apache 2.0) - see ROADMAP.md for
how that review was scoped and what else came out of it.

## [3.6.0] - Expanded event taxonomy

- The legacy Network WebSocket connection carries an "events" (and "alert")
  message type, keyed by EVT_* strings, that this plugin was never reading -
  only device:sync/sta:sync/device-port:sync were handled. Added parsing for
  it, with event key names and payload field semantics cross-checked against
  Kane610/aiounifi (MIT License).
- 16 new triggers: Network Device Overheating, PoE Overload, PoE Disconnected,
  Rogue AP/DHCP Detected, Radar Detected, Possible Interference, STP Port
  Blocking, Restarted, Firmware Upgraded, Firmware Upgrade Failed, Adopted;
  Gateway WAN Transition (failover); Client Roamed; Controller UniFi OS
  Update Available, IPS/IDS Alert, and Admin Login.
- Every networkDevice/clientDevice/unifiController now has
  lastEventKey/lastEventMessage/lastEventAt states, updated on every
  recognised event even when there's no dedicated trigger for it yet - use
  these in a conditional trigger to react to event keys we haven't
  special-cased.
- Client/device connect, disconnect, block, and unblock events are
  intentionally NOT re-wired to new triggers here, since they're already
  covered by the existing state-diff-driven triggers and would have fired
  twice for the same real-world occurrence.
- Added ROADMAP.md cataloguing further features found while reviewing
  aiounifi/Home Assistant's UniFi integration (firewall policies, traffic
  rules/routes, port forwarding, WLAN management, guest vouchers, DPI
  restriction groups) plus hardware-dependent items (SmartPower/UPS, 5G
  gateway, smoke detectors) that need real payload data before they can be
  built reliably.

## [3.5.1] - Auto-redeploy fan control

- Drive controller now checks once a day whether the SSH-deployed fan
  control service is still present on the UNAS, and silently redeploys it if
  a UniFi OS update has wiped it. Opt-out checkbox provided (default on).
  Only acts if the plugin previously did a manual deploy - first-time setup
  is never automatic.

## [3.5.0] - Randomised client purge

- New per-controller settings and a Purge Randomised Clients action.
  Detects locally-administered/randomised MAC addresses (iPhone Private
  Address, Android random MAC, etc.) per the IEEE 802 U/L bit rule, skips
  anything named, fixed-IP'd, grouped, or already tracked as an Indigo
  device, and removes stale entries from both UniFi (forget-sta) and
  Indigo. Configurable age threshold and a dry-run mode for safe testing.

## [3.4.2] - Fan control UX and correctness fixes

- Deploy/Test/Remove fan control buttons now build their SSH session from
  the values currently in the config dialog, so they work without saving
  first.
- Dimmer state for the fan device now sources its "current mode" from the
  SSH-deployed config file when fan control is deployed, instead of the
  UniFi REST API (which kept reporting a stale profile since the API can't
  be told about the SSH-driven changes). Fixes dimmer resetting to 100%
  every poll cycle.
- Added a Run Fan Control Diagnostic action: one-shot script run,
  service status, last 30 systemd journal lines, and current PWM register
  values, all returned in a single log block.
- Fan control script now logs to the systemd journal in service mode
  (previously silent) and reads back PWM values after every write to
  confirm they stuck.

## [3.4.1] - Startup crash fix

- Fixed NameError: name '__file__' is not defined on plugin startup.
  Indigo's plugin host runs plugin.py via exec(), so __file__ isn't
  set in the module namespace. Asset loading now falls back to the current
  working directory (which Indigo does set correctly) and then to known
  Indigo install paths.

## [3.4.0] - SSH-based fan control

- UniFi's own fan-control write API (PUT /proxy/drive/api/v2/systems/fan-control)
  was confirmed broken on current UNAS firmware - every payload shape
  returns HTTP 500, verified with a standalone probe script trying ~20
  variants (verbs, paths, payload wrappers). This is a firmware limitation,
  not a plugin bug.
- Added SSH-based fan control as a workaround: a bundled script (adapted
  from hoxxep/UNAS-Pro-fan-control) and systemd service, deployed to the
  UNAS on demand, that writes fan PWM directly via sysfs.
- New controller config fields: enable SSH, username/password/port, deploy
  and remove buttons.
- paramiko added to requirements.txt.

## [3.3.8] - Dimmer routing bug

- Found and fixed a routing bug where, because the plugin defines both
  actionControlDevice and actionControlDimmerRelay, Indigo routes ALL
  device actions (including dimmer brightness) through
  actionControlDevice. The fan dimmer's brightness changes were silently
  swallowed as a result. actionControlDevice now delegates to the dimmer
  handler for fan devices.

## [3.3.7] - Fan mode mapping

- Fan dimmer brightness anchors changed to 0% / 50% / 100% for
  Quiet / Balance / Cooling, with snap-to-nearest behaviour for
  intermediate values.
- Fan mode API write now tries multiple payload variants
  (profile/mode fields, with/without an apply flag).

## [3.3.6] - Duplicate-device detection bug

- Fixed _create_or_skip's uniqueness check silently treating every
  existing device of a type as a duplicate when the device type's key prop
  (e.g. diskKey, poolKey) wasn't in its recognised list - caused by
  Python's all([]) == True. Only the first disk/pool of a given type was
  ever being created; subsequent discovery runs reported "0 new" even
  though more existed. Added diskKey/poolKey to the match logic and a
  name-based fallback.

## [3.3.5] - Drive payload field corrections

- Corrected disk parsing to match real UNAS firmware payloads: disks live
  at the top level of the storage response (not nested per-pool), linked to
  their pool via poolId. Field names corrected (state not status,
  size not capacity, slotId for naming). RAID level sourced from
  preferLevel, rebuild progress from raidGroups[0].progress. At-risk
  detection uses the riskReasons array.
- Added fullyOperational/optimal (and other firmware-specific spellings)
  to the healthy-status whitelist.

## [3.3.4] - Access WebSocket crash fix

- Fixed 'str' object has no attribute 'get' in the Access WebSocket
  handler - some messages arrive as plain strings or JSON-as-string rather
  than parsed dicts. Handler now normalises and safely ignores anything it
  can't turn into a usable event.

## [3.3.3] - Drive device consolidation

- Removed the standalone unifiDrive appliance device type. All UNAS
  appliance-level data (storage totals, system info, pool/disk counts) now
  lives directly on the Drive controller device, avoiding duplicate state
  across two devices.

## [3.3.2] - Device creation crash fix

- Fixed indigo.device.create() failing with a C++ signature mismatch
  across every discovery button in the plugin (cameras, sensors, network
  devices, doors, pools, disks). The required protocol keyword argument
  was missing from the call.

## [3.3.1] - Drive Pool and Disk device types

- Corrected Drive API endpoints to the /proxy/drive/api/v2/ paths
  (confirmed against community reverse-engineering, since no official Drive
  API exists).
- Added unifiDrivePool and unifiDriveDisk device types with discovery
  actions, so multi-pool/multi-disk UNAS systems get one device per
  pool/disk rather than a single aggregate.

## [3.3.0] - Drive controller split

- Split the single controller device type into unifiController (Network /
  Protect / Access) and unifiDriveController (Drive/UNAS), since a UNAS is
  a separate appliance with its own IP and auth.
- Added device-name prefix/suffix options, applied consistently across all
  discovery types.

## [3.2.0] - Integration API rewrite

- Migrated Protect and Access to Ubiquiti's official Integration APIs
  (Bearer auth, JSON WebSockets). Motion, smart detection, and doorbell
  events now arrive in ~100ms over WebSocket instead of polling.
- Network uses the Integration API by default with a documented legacy
  fallback for endpoints not yet covered (port tables, radio tables, PoE
  overrides, client block/unblock).
- Rearchitected polling: WebSocket for real-time state, polling only for
  slow-changing stats, with configurable stats/sanity intervals.
- Exponential WebSocket reconnect backoff (5s to 5min).
