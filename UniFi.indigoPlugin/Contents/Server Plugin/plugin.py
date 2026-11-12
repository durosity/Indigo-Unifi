#! /usr/bin/env python
# -*- coding: utf-8 -*-
# UniFi Plugin for Indigo Domotics
# Author: Durosity
# Version: 3.12.14
#
# Architecture:
#   - Network:  Official Integration API (X-API-KEY) where possible,
#               legacy cookie auth as a documented bridge for endpoints
#               not yet exposed by the Integration API.
#   - Protect:  Official Integration API (Bearer) with JSON WebSockets.
#   - Access:   Official Developer API on port 12445 (Bearer) with WS.
#   - Drive:    Legacy cookie auth (no official API exists yet).

import indigo

import base64
import json
import os
import re
import socket
import ssl
import struct
import zlib
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from http.cookiejar import CookieJar


# ============================================================================
# Constants
# ============================================================================

PLUGIN_VERSION       = "3.12.14"

# Default ports
DEFAULT_HTTPS_PORT   = 443
DEFAULT_ACCESS_PORT  = 12445  # UniFi Access developer API runs on its own port

# Polling intervals (seconds)
DEFAULT_STATS_INTERVAL_SEC   = 300   # 5 min - configurable in controller UI
DEFAULT_SANITY_INTERVAL_SEC  = 900   # 15 min - configurable
AGGRESSIVE_INTERVAL_SEC      = 30    # only used when aggressive mode toggled
DOOR_RELOCK_TIMEOUT_SEC      = 10    # momentary-unlock auto-relock window

# WebSocket reconnect backoff
WS_RECONNECT_INITIAL_SEC     = 5
WS_RECONNECT_MAX_SEC         = 300
WS_RECONNECT_BACKOFF_FACTOR  = 2

# Dead-connection detection: a TCP connection can go "half-open" (the far
# end stops responding but never sends a close/RST - common behind NAT,
# sleeping hosts, or a hung remote process) without our socket ever raising
# an error. We only find out by noticing nothing has come back - not even a
# pong - for several consecutive read-timeout cycles in a row. After this
# many consecutive cycles with zero frames received, we treat the
# connection as dead and force a fresh reconnect rather than pinging into
# the void forever. Mirrors aiohttp's `heartbeat=` dead-connection handling,
# which inspired this fix.
WS_MAX_MISSED_PINGS          = 2

# Network timeouts
DEFAULT_HTTP_TIMEOUT_SEC     = 30
BOOTSTRAP_TIMEOUT_SEC        = 60

# ----------------------------------------------------------------------------
# UniFi Drive (UNAS) endpoints
#
# Cross-checked against the memphi2/ha-unifi-drive HA integration (MIT-
# licensed). That project actively maintains support for current UNAS
# firmware. The plugin's earlier guesses at /proxy/drive/api/storage were
# wrong on current firmware; the v2 paths below are what UNAS responds to.
# ----------------------------------------------------------------------------
DRIVE_LOGIN_PATH               = "/api/auth/login"
DRIVE_STORAGE_PATH             = "/proxy/drive/api/v2/storage"
DRIVE_FAN_CONTROL_PATH         = "/proxy/drive/api/v2/systems/fan-control"
DRIVE_SYSTEM_PATH              = "/api/system"
DRIVE_REBOOT_PATH              = "/api/system/reboot"
DRIVE_POWEROFF_PATH            = "/api/system/poweroff"
DRIVE_FIRMWARE_UPDATE_PATH     = "/api/firmware/update"
DRIVE_APP_UPDATE_PATH          = "/api/applications/drive/update"

# Fan modes: canonical name (used in UI) -> tuple of API payload values to try
DRIVE_FAN_MODE_OPTIONS = ("Quiet", "Balance", "Cooling")
DRIVE_FAN_MODE_API_VALUES = {
    "Quiet":   ("quiet",),
    "Balance": ("default", "default_profile", "balance", "balanced"),
    "Cooling": ("cooling", "cool"),
}


# ============================================================================
# Shared HTTP helpers
# ============================================================================

def _build_ssl_context(verify_ssl):
    """Build an SSL context that either verifies certs or doesn't."""
    if verify_ssl:
        return ssl.create_default_context()
    return ssl._create_unverified_context()


def _build_opener(ssl_context, cookie_jar=None):
    """Build a urllib opener with optional cookie support."""
    handlers = [urllib.request.HTTPSHandler(context=ssl_context)]
    if cookie_jar is not None:
        handlers.append(urllib.request.HTTPCookieProcessor(cookie_jar))
    return urllib.request.build_opener(*handlers)


def _decode_error_body(http_error):
    """Read the body of an HTTPError and return a clean string for logs."""
    try:
        body = http_error.read()
        if not body:
            return ""
        text = body.decode("utf-8", errors="replace")
        # If it's HTML, don't dump the whole page
        stripped = text.lstrip()
        if stripped.startswith("<"):
            return f"(HTML response, status {http_error.code})"
        return text[:500]
    except Exception:
        return ""


class UnifiAuthError(Exception):
    """Raised when authentication fails."""


class UnifiNotFoundError(Exception):
    """Raised when a resource (camera, door, etc) is not found."""


class UnifiAPIError(Exception):
    """Generic API failure."""


# ============================================================================
# Drive payload parsing helpers
# ----------------------------------------------------------------------------
# UNAS storage payloads have a lot of field-name variation across firmware
# versions (memphi2's integration calls out 10+ variants per field). Rather
# than copy that whole helper module, we use a focused "first matching key"
# approach with the most common variant names.
# ============================================================================

_DRIVE_CAPACITY_KEYS = (
    "capacity", "total", "totalBytes", "totalSize", "totalCapacity",
    "capacityBytes", "size", "usableCapacity", "usableSize", "volumeSize",
)
_DRIVE_USAGE_KEYS = (
    "usage", "used", "usedBytes", "usedSize", "usedCapacity",
    "usedSpace", "usedSpaceBytes", "usedStorage",
)
_DRIVE_AVAILABLE_KEYS = (
    "available", "availableBytes", "availableSize", "availableCapacity",
    "free", "freeBytes", "freeSize", "freeSpace", "freeSpaceBytes",
    "remaining", "remainingBytes",
)
_DRIVE_AGG_CAPACITY_KEYS = (
    "totalStorage", "totalStorageBytes", "storageTotalBytes", "storageTotal",
)
_DRIVE_AGG_USAGE_KEYS = (
    "usedStorage", "usedStorageBytes", "storageUsedBytes", "storageUsed",
)
_DRIVE_AGG_AVAILABLE_KEYS = (
    "availableStorage", "availableStorageBytes", "storageAvailableBytes",
    "storageAvailable", "freeStorage",
)
_DRIVE_RAID_KEYS = (
    "raidType", "raid_type", "raidLevel", "raid_level",
    "poolType", "pool_type", "protection", "protectionLevel",
    "layout", "type",
)
_DRIVE_POOL_LIST_KEYS = (
    "pools", "storagePools", "storage_pools", "volumes", "volumeList",
)
_DRIVE_DISK_LIST_KEYS = (
    "disks", "drives", "members", "physicalDrives", "physicalDisks",
)

# Status values that mean "everything is fine" — used to decide whether to
# raise problem flags. Case-insensitive; new variants belong here, not in the
# call sites.
_DRIVE_HEALTHY_STATUSES = frozenset({
    "ok", "online", "healthy", "normal", "good",
    "fullyoperational", "fully_operational",
    "operational", "optimal",
    "active", "ready", "running",
})

# Status values that mean "this needs attention".
_DRIVE_PROBLEM_STATUSES = frozenset({
    "degraded", "warning", "unhealthy", "failed", "failing",
    "error", "critical", "missing", "offline",
})

# Status values that mean "this disk is showing early warning signs".
_DRIVE_AT_RISK_STATUSES = frozenset({
    "at_risk", "atrisk", "warning", "degraded", "predictivefail",
    "predictive_fail",
})


def _drive_is_healthy(status_text):
    """Return True if status_text is a known good status, False otherwise.
    None / empty / unknown statuses are treated as NOT healthy (conservative
    default — better to flag a phantom problem than miss a real one)."""
    if not status_text:
        return False
    return str(status_text).strip().lower().replace("-", "_") in _DRIVE_HEALTHY_STATUSES


def _drive_is_at_risk(status_text):
    if not status_text:
        return False
    return str(status_text).strip().lower().replace("-", "_") in _DRIVE_AT_RISK_STATUSES


# ============================================================================
# Client purge — MAC randomisation detection
# ----------------------------------------------------------------------------
# Per IEEE 802 (and RFC 9724), a locally-administered unicast MAC address has
# the second-least-significant bit of the first octet set to 1. iPhone "Private
# Address", Android "Use random MAC", and Windows "Random hardware addresses"
# all generate addresses that follow this rule.
#
# The simple test: the second hex digit of the first octet is 2, 6, A, or E
# (i.e. first-octet & 0x02 != 0).
# ============================================================================

def _mac_is_randomised(mac):
    """Return True if `mac` looks like a randomised/locally-administered MAC.

    Accepts any common MAC formatting (with or without separators, any case).
    Returns False for malformed input — safer to leave unknown MACs alone than
    to risk purging something legitimate.
    """
    if not mac:
        return False
    # Strip everything except hex digits
    hex_only = "".join(c for c in str(mac) if c in "0123456789abcdefABCDEF")
    if len(hex_only) < 2:
        return False
    try:
        first_octet = int(hex_only[:2], 16)
    except ValueError:
        return False
    # Bit 1 of the first octet is the U/L bit — set means locally administered
    return bool(first_octet & 0x02)


def _has_user_named_client(client):
    """Return True if the user has explicitly named or noted this client in
    UniFi (custom name, hostname override, or note)."""
    if not isinstance(client, dict):
        return False
    for k in ("name", "hostname_override", "display_name"):
        v = client.get(k)
        if v and str(v).strip():
            return True
    for k in ("noted", "note"):
        v = client.get(k)
        if v and str(v).strip():
            return True
    return False


def _has_user_metadata_other_than_name(client):
    """Return True if the user has set non-name metadata: fixed IP, user
    group, or device-type override. These all suggest 'I care about this
    client' beyond just naming it."""
    if not isinstance(client, dict):
        return False
    # Fixed IP assignment
    if client.get("use_fixedip") and client.get("fixed_ip"):
        return True
    # User group assignment
    ug = client.get("usergroup_id")
    if ug and str(ug).strip():
        return True
    # Device identification override
    for k in ("dev_id_override", "dev_cat", "dev_family"):
        v = client.get(k)
        if v not in (None, "", 0):
            return True
    return False


def _client_has_user_metadata(client):
    """Return True if a UniFi client record has ANY user-set metadata that
    suggests the user cares about it. Combines the name and metadata checks."""
    return _has_user_named_client(client) or _has_user_metadata_other_than_name(client)


def _client_age_seconds(client, now_ts=None):
    """Return the number of seconds since the client was last seen, or None
    if last_seen is missing/invalid."""
    if not isinstance(client, dict):
        return None
    last_seen = client.get("last_seen")
    if last_seen is None:
        return None
    try:
        ls = float(last_seen)
    except (TypeError, ValueError):
        return None
    if ls <= 0:
        return None
    if now_ts is None:
        now_ts = time.time()
    return max(0.0, now_ts - ls)


def _drive_first_number(payload, keys):
    """Return the first numeric value found in `payload` under any of `keys`."""
    if not isinstance(payload, dict):
        return None
    for k in keys:
        v = payload.get(k)
        if v is None:
            continue
        try:
            n = float(v)
            return n
        except (TypeError, ValueError):
            continue
    return None


def _drive_first_text(payload, keys):
    """Return the first non-empty string value in `payload` under any of `keys`."""
    if not isinstance(payload, dict):
        return None
    for k in keys:
        v = payload.get(k)
        if v is None:
            continue
        s = str(v).strip()
        if s:
            return s
    return None


def _drive_bytes_to_gb(value):
    """Convert a byte count to GB (1024^3). Returns None on bad input."""
    if value is None:
        return None
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return 0.0
    return round(n / (1024 ** 3), 2)


def _drive_extract_pools(storage_payload):
    """Find the pool list inside a storage payload. UNAS payloads put it under
    one of several keys, sometimes nested in a 'storage' or 'data' container."""
    if not isinstance(storage_payload, dict):
        return []

    # Try at top level
    for key in _DRIVE_POOL_LIST_KEYS:
        v = storage_payload.get(key)
        if isinstance(v, list):
            return [p for p in v if isinstance(p, dict)]

    # Try one level of nesting
    for container_key in ("storage", "storageInfo", "data", "result"):
        sub = storage_payload.get(container_key)
        if isinstance(sub, dict):
            for key in _DRIVE_POOL_LIST_KEYS:
                v = sub.get(key)
                if isinstance(v, list):
                    return [p for p in v if isinstance(p, dict)]

    return []


def _drive_extract_disks(storage):
    """Find the disk list. UniFi UNAS firmware puts disks at the TOP level of
    the storage payload (not nested per-pool); each disk's `poolId` field links
    it to its parent pool. Older firmware variants nested disks per-pool — we
    still handle those as a fallback."""
    if not isinstance(storage, dict):
        return []
    # Top-level disks array (current UNAS firmware)
    for key in _DRIVE_DISK_LIST_KEYS:
        v = storage.get(key)
        if isinstance(v, list):
            return [d for d in v if isinstance(d, dict)]
    # Try one level of nesting
    for container_key in ("storage", "storageInfo", "data", "result"):
        sub = storage.get(container_key)
        if isinstance(sub, dict):
            for key in _DRIVE_DISK_LIST_KEYS:
                v = sub.get(key)
                if isinstance(v, list):
                    return [d for d in v if isinstance(d, dict)]
    return []


def _drive_extract_disks_for_pool(storage, pool):
    """Return all disks belonging to a given pool. Disks reference their
    parent pool by `poolId`. Falls back to per-pool nesting on legacy firmware."""
    if not isinstance(pool, dict):
        return []
    # Modern: top-level disks list filtered by poolId
    pool_id = pool.get("id") or pool.get("guid") or pool.get("poolGuid") or pool.get("uuid")
    top_level = _drive_extract_disks(storage if isinstance(storage, dict) else {})
    if top_level and pool_id:
        filtered = [d for d in top_level if d.get("poolId") == pool_id or
                                              d.get("pool_id") == pool_id or
                                              d.get("poolGuid") == pool_id]
        if filtered:
            return filtered
        # If no disks matched by poolId but there's only one pool, all disks
        # implicitly belong to it.
        return top_level
    # Legacy: per-pool nesting
    for key in _DRIVE_DISK_LIST_KEYS:
        v = pool.get(key)
        if isinstance(v, list):
            return [d for d in v if isinstance(d, dict)]
    return []


# Backwards-compatible alias for old call sites that don't have storage context.
def _drive_extract_disks_from_pool(pool):
    """Deprecated: prefer _drive_extract_disks_for_pool(storage, pool)."""
    return _drive_extract_disks_for_pool({}, pool)


def _drive_pool_key(pool, index):
    """Return a stable identifier for a pool: guid > id > name > 'pool_N'."""
    if not isinstance(pool, dict):
        return f"pool_{index}"
    for k in ("poolGuid", "guid", "id", "uuid"):
        v = pool.get(k)
        if v:
            return str(v)
    name = _drive_first_text(pool, ("name", "displayName", "label"))
    if name:
        return f"name:{name}"
    return f"pool_{index}"


def _drive_pool_name(pool, index):
    """Return a display name for a pool."""
    name = _drive_first_text(pool, ("displayName", "name", "label"))
    return name or f"Pool {index + 1}"


def _drive_disk_key(disk, index):
    """Return a stable identifier for a disk: serial > guid > id > slot > 'disk_N'."""
    if not isinstance(disk, dict):
        return f"disk_{index}"
    for k in ("serialNumber", "serial", "guid", "id", "uuid"):
        v = disk.get(k)
        if v:
            return str(v)
    slot = disk.get("slotId") or disk.get("slot") or disk.get("bay") or disk.get("position")
    if slot is not None:
        return f"slot:{slot}"
    return f"disk_{index}"


def _drive_disk_name(disk, index, pool_index=0):
    """Return a display name for a disk. Prefer slot number since that's what
    you see on the physical UNAS chassis."""
    if isinstance(disk, dict):
        slot = (disk.get("slotId") or disk.get("slot") or
                disk.get("bay") or disk.get("position"))
        if slot is not None:
            return f"Slot {slot}"
        name = _drive_first_text(disk, ("displayName", "name", "label"))
        if name:
            return name
    return f"Disk {pool_index + 1}.{index + 1}"


# ============================================================================
# UniFi event taxonomy (legacy WebSocket "events"/"alert" message stream)
# ----------------------------------------------------------------------------
# The classic UniFi controller pushes WebSocket messages with
# meta.message == "events" (or "alert"), where each item in `data` is an
# event object keyed by an EVT_* string (e.g. "EVT_SW_PoeOverload"). The
# shape of these objects — the key names, and which payload fields hold the
# device MAC (ap/gw/sw) vs. the client MAC (user/client/guest) — are
# cross-checked against Kane610/aiounifi (MIT License):
#   https://github.com/Kane610/aiounifi
#   aiounifi/models/event.py and aiounifi/models/message.py
# The routing table below and all WebSocket handling in this plugin are our
# own implementation, written independently for Indigo's device/trigger model.
# ============================================================================

# EVT_* -> Events.xml trigger id, for device-scoped events (routed by
# matching the event's ap/gw/sw MAC to a networkDevice). Keys not listed here
# still update the device's lastEventKey/lastEventMessage/lastEventAt states,
# just without firing a dedicated trigger.
NETWORK_DEVICE_EVENT_TRIGGERS = {
    "EVT_SW_Overheat":             "deviceOverheat",
    "EVT_SW_PoeOverload":          "devicePoeOverload",
    "EVT_SW_PoeDisconnect":        "devicePoeDisconnect",
    "EVT_AP_DetectRogueAP":        "deviceRogueDetected",
    "EVT_SW_DetectRogueDHCP":      "deviceRogueDetected",
    "EVT_AP_RadarDetected":        "deviceRadarDetected",
    "EVT_AP_PossibleInterference": "devicePossibleInterference",
    "EVT_SW_StpPortBlocking":      "deviceStpBlocking",
    "EVT_AP_Restarted":            "deviceRestarted",
    "EVT_AP_RestartedUnknown":     "deviceRestarted",
    "EVT_SW_Restarted":            "deviceRestarted",
    "EVT_SW_RestartedUnknown":     "deviceRestarted",
    "EVT_GW_Restarted":            "deviceRestarted",
    "EVT_GW_RestartedUnknown":     "deviceRestarted",
    "EVT_AP_Upgraded":             "deviceUpgraded",
    "EVT_SW_Upgraded":             "deviceUpgraded",
    "EVT_GW_Upgraded":             "deviceUpgraded",
    "EVT_DM_Upgraded":             "deviceUpgraded",
    "EVT_AP_UpgradeFailed":        "deviceUpgradeFailed",
    "EVT_AP_Adopted":              "deviceAdopted",
    "EVT_AP_AutoReadopted":        "deviceAdopted",
    "EVT_SW_Adopted":              "deviceAdopted",
    "EVT_SW_AutoReadopted":        "deviceAdopted",
    "EVT_GW_Adopted":              "deviceAdopted",
    "EVT_GW_AutoReadopted":        "deviceAdopted",
    "EVT_GW_WANTransition":        "gatewayWanTransition",
}

# EVT_* -> Events.xml trigger id, for client-scoped events (routed by
# matching the event's user/client/guest MAC to a clientDevice).
CLIENT_EVENT_TRIGGERS = {
    "EVT_WU_Roam":      "clientRoamed",
    "EVT_WU_RoamRadio": "clientRoamed",
    "EVT_WG_Roam":      "clientRoamed",
    "EVT_WG_RoamRadio": "clientRoamed",
}

# EVT_* -> Events.xml trigger id, for events with no device/client MAC at
# all — these fire against the Network controller device itself.
CONTROLLER_EVENT_TRIGGERS = {
    "EVT_AD_Update_Available": "controllerUpdateAvailable",
    "EVT_IPS_IpsAlert":        "controllerIpsAlert",
    "EVT_AD_Login":            "controllerAdminLogin",
}

# Every EVT_* key this plugin recognises, whether or not it has a dedicated
# trigger. Used only to decide whether an unmapped key is "known but not
# trigger-worthy" vs. "genuinely new to us" for debug logging.
_KNOWN_EVENT_KEYS = frozenset({
    # Access point lifecycle
    "EVT_AP_Adopted", "EVT_AP_AutoReadopted", "EVT_AP_ChannelChanged",
    "EVT_AP_Configured", "EVT_AP_Connected", "EVT_AP_Deleted",
    "EVT_AP_DetectRogueAP", "EVT_AP_DiscoveredPending",
    "EVT_AP_HostKeyMismatch", "EVT_AP_Isolated", "EVT_AP_Lost_Contact",
    "EVT_AP_PossibleInterference", "EVT_AP_RadarDetected",
    "EVT_AP_Restarted", "EVT_AP_RestartedUnknown",
    "EVT_AP_UpgradeScheduled", "EVT_AP_UpgradeFailed", "EVT_AP_Upgraded",
    # Building-to-building bridge
    "EVT_BB_ChannelChanged",
    # Dream Machine
    "EVT_DM_Connected", "EVT_DM_Lost_Contact", "EVT_DM_Upgraded",
    # Gateway lifecycle
    "EVT_GW_Adopted", "EVT_GW_AutoReadopted", "EVT_GW_Configured",
    "EVT_GW_Connected", "EVT_GW_Deleted", "EVT_GW_Lost_Contact",
    "EVT_GW_Restarted", "EVT_GW_RestartedUnknown", "EVT_GW_Upgraded",
    "EVT_GW_WANTransition",
    # Switch lifecycle
    "EVT_SW_Adopted", "EVT_SW_AutoReadopted", "EVT_SW_Configured",
    "EVT_SW_Connected", "EVT_SW_Deleted", "EVT_SW_DetectRogueDHCP",
    "EVT_SW_DiscoveredPending", "EVT_SW_Lost_Contact", "EVT_SW_Overheat",
    "EVT_SW_PoeOverload", "EVT_SW_PoeDisconnect", "EVT_SW_Restarted",
    "EVT_SW_RestartedUnknown", "EVT_SW_StpPortBlocking",
    "EVT_SW_UpgradeScheduled", "EVT_SW_Upgraded",
    # Vouchers / hotspot (no device type yet - logged only, see ROADMAP.md)
    "EVT_AD_VoucherCreated", "EVT_AD_VoucherDeleted",
    "EVT_HS_AuthedByNoAuth", "EVT_HS_AuthedByPassword", "EVT_HS_VoucherUsed",
    # Clients (connect/disconnect/block/unblock are already covered by
    # state-diff triggers off sta:sync - listed here only so they don't log
    # as "unrecognised")
    "EVT_LU_Connected", "EVT_LU_Disconnected",
    "EVT_LC_Blocked", "EVT_LC_Unblocked",
    "EVT_WU_Connected", "EVT_WU_Disconnected",
    "EVT_WC_Blocked", "EVT_WC_Unblocked",
    "EVT_WU_Roam", "EVT_WU_RoamRadio",
    "EVT_LG_Connected", "EVT_LG_Disconnected",
    "EVT_WG_AuthorizationEnded", "EVT_WG_Connected", "EVT_WG_Disconnected",
    "EVT_WG_Roam", "EVT_WG_RoamRadio",
    # XG (legacy PDU-class device - no outlet device type yet)
    "EVT_XG_AutoReadopted", "EVT_XG_Connected", "EVT_XG_Lost_Contact",
    "EVT_XG_OutletPowerCycle",
    # Security / admin
    "EVT_IPS_IpsAlert", "EVT_AD_GuestUnauthorized", "EVT_AD_Login",
    "EVT_AD_ScheduleUpgradeFailedNotFound", "EVT_AD_Update_Available",
    # Smart Power RPS (no SmartPower/UPS device type yet)
    "EVT_USP_RpsPowerDeniedByPsuOverload",
})


# ============================================================================
# UniFi Network API
# ----------------------------------------------------------------------------
# This class wraps BOTH the official Integration API and the legacy cookie
# API. The Integration API is preferred where it's available; legacy is the
# documented bridge for endpoints Ubiquiti hasn't yet exposed officially.
#
# Each method documents which path it uses so it's easy to spot when a method
# can be promoted from legacy to integration as the official API expands.
# ============================================================================

class UnifiNetworkAPI:
    """UniFi Network API - Integration API where possible, legacy bridge elsewhere."""

    def __init__(self, host, port, api_key, username, password, site="default", verify_ssl=False, logger=None, debug=False):
        self.host         = host
        self.port         = int(port)
        self.api_key      = api_key
        self.username     = username
        self.password     = password
        self.site         = site
        self.verify_ssl   = verify_ssl
        self.logger       = logger
        self.debug        = debug

        self.base_url     = f"https://{host}:{self.port}"
        self.ssl_context  = _build_ssl_context(verify_ssl)

        # Separate openers for integration (no cookies needed) and legacy (cookies needed)
        self.integration_opener = _build_opener(self.ssl_context)
        self.legacy_cookies     = CookieJar()
        self.legacy_opener      = _build_opener(self.ssl_context, self.legacy_cookies)
        self.legacy_csrf        = None
        self.legacy_logged_in   = False

    # ------------------------------------------------------------------
    # Internal request helpers
    # ------------------------------------------------------------------

    def _integration_request(self, method, path, data=None, timeout=DEFAULT_HTTP_TIMEOUT_SEC):
        """Make a request to the official Integration API using the X-API-KEY."""
        if not self.api_key:
            raise UnifiAuthError("Network Integration API key is not configured")

        url = f"{self.base_url}{path}"
        body = json.dumps(data).encode("utf-8") if data is not None else None

        req = urllib.request.Request(url, data=body, method=method)
        req.add_header("X-API-KEY", self.api_key)
        req.add_header("Accept", "application/json")
        if body is not None:
            req.add_header("Content-Type", "application/json")

        try:
            response = self.integration_opener.open(req, timeout=timeout)
            payload = response.read().decode("utf-8")
            return json.loads(payload) if payload else {}
        except urllib.error.HTTPError as e:
            err_body = _decode_error_body(e)
            if e.code in (401, 403):
                raise UnifiAuthError(f"Integration API auth failed (HTTP {e.code}): {err_body}")
            raise UnifiAPIError(f"Integration API HTTP {e.code}: {err_body}")
        except urllib.error.URLError as e:
            raise UnifiAPIError(f"Integration API network error: {e}")
        except json.JSONDecodeError as e:
            raise UnifiAPIError(f"Integration API returned invalid JSON: {e}")

    def _legacy_login(self):
        """Authenticate against the legacy cookie API. Idempotent."""
        if not self.username or not self.password:
            raise UnifiAuthError(
                "Legacy username/password not configured "
                "(needed for endpoints not yet in the Integration API)"
            )

        url = f"{self.base_url}/api/auth/login"
        body = json.dumps({
            "username":   self.username,
            "password":   self.password,
            "rememberMe": True,
        }).encode("utf-8")

        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json")

        try:
            response = self.legacy_opener.open(req, timeout=DEFAULT_HTTP_TIMEOUT_SEC)
            response.read()  # drain
        except urllib.error.HTTPError as e:
            err_body = _decode_error_body(e)
            if e.code in (401, 403):
                raise UnifiAuthError(
                    f"Legacy login failed (HTTP {e.code}). "
                    f"Use a LOCAL-ONLY admin (no MFA, not a UI.com cloud account). "
                    f"Details: {err_body}"
                )
            raise UnifiAuthError(f"Legacy login HTTP {e.code}: {err_body}")
        except urllib.error.URLError as e:
            raise UnifiAuthError(f"Legacy login network error: {e}")

        # Extract CSRF token from JWT cookie
        self.legacy_csrf = None
        for cookie in self.legacy_cookies:
            if cookie.name == "TOKEN":
                self.legacy_csrf = self._extract_csrf_from_jwt(cookie.value) or cookie.value
                break

        self.legacy_logged_in = True

    def _extract_csrf_from_jwt(self, jwt_value):
        """Pull csrfToken out of the JWT payload if present."""
        try:
            parts = jwt_value.split(".")
            if len(parts) < 2:
                return None
            payload = parts[1]
            # Pad for base64 decode
            padding = (-len(payload)) % 4
            if padding:
                payload += "=" * padding
            decoded = base64.urlsafe_b64decode(payload)
            data = json.loads(decoded)
            return data.get("csrfToken")
        except Exception:
            return None

    def _legacy_request(self, method, path, data=None, timeout=DEFAULT_HTTP_TIMEOUT_SEC, _retry=True):
        """Make a request to the legacy cookie API. Logs in lazily."""
        if not self.legacy_logged_in:
            self._legacy_login()

        url = f"{self.base_url}{path}"
        body = json.dumps(data).encode("utf-8") if data is not None else None

        req = urllib.request.Request(url, data=body, method=method)
        req.add_header("Accept", "application/json")
        if body is not None:
            req.add_header("Content-Type", "application/json")
        if self.legacy_csrf and method in ("POST", "PUT", "PATCH", "DELETE"):
            req.add_header("X-CSRF-Token", self.legacy_csrf)

        try:
            response = self.legacy_opener.open(req, timeout=timeout)
            payload = response.read().decode("utf-8")
            if not payload:
                return {}
            # Legacy API sometimes returns HTML on errors; guard against it
            if payload.lstrip().startswith("<"):
                raise UnifiAPIError(f"Legacy API returned HTML (endpoint may not exist — service not installed?)")
            return json.loads(payload)
        except urllib.error.HTTPError as e:
            # 401 -> session expired. Re-login once and retry.
            if e.code == 401 and _retry:
                self.legacy_logged_in = False
                self.legacy_csrf      = None
                return self._legacy_request(method, path, data, timeout, _retry=False)
            err_body = _decode_error_body(e)
            if e.code in (401, 403):
                raise UnifiAuthError(f"Legacy API auth failed (HTTP {e.code}): {err_body}")
            raise UnifiAPIError(f"Legacy API HTTP {e.code}: {err_body}")
        except urllib.error.URLError as e:
            raise UnifiAPIError(f"Legacy API network error: {e}")
        except json.JSONDecodeError as e:
            raise UnifiAPIError(f"Legacy API returned invalid JSON: {e}")

    # ------------------------------------------------------------------
    # Public API - reads (currently legacy; promote to integration as
    # endpoints become available)
    # ------------------------------------------------------------------

    def get_site_info(self):
        """[LEGACY] Get site info."""
        return self._legacy_request("GET", f"/proxy/network/api/s/{self.site}/self")

    def get_devices(self):
        """[LEGACY] Get all network devices (rich state including port_table, radio_table)."""
        # NOTE: Integration API has /sites/{id}/devices but with a much narrower
        # response shape. We use legacy here to preserve rich state monitoring.
        result = self._legacy_request("GET", f"/proxy/network/api/s/{self.site}/stat/device")
        return result.get("data", []) if isinstance(result, dict) else []

    def get_device(self, mac_or_id):
        """[LEGACY] Find a device by MAC or _id."""
        target = (mac_or_id or "").lower()
        for d in self.get_devices():
            if d.get("mac", "").lower() == target or d.get("_id", "") == mac_or_id:
                return d
        return None

    def get_clients(self):
        """[LEGACY] Get all currently active clients."""
        result = self._legacy_request("GET", f"/proxy/network/api/s/{self.site}/stat/sta")
        return result.get("data", []) if isinstance(result, dict) else []

    def get_all_clients(self):
        """[LEGACY] Get all known clients (active + inactive)."""
        result = self._legacy_request("GET", f"/proxy/network/api/s/{self.site}/rest/user")
        return result.get("data", []) if isinstance(result, dict) else []

    def get_client(self, mac):
        """[LEGACY] Find a client by MAC across active and known."""
        target = (mac or "").lower()
        for c in self.get_clients():
            if c.get("mac", "").lower() == target:
                return c
        for c in self.get_all_clients():
            if c.get("mac", "").lower() == target:
                return c
        return None

    def get_wlans(self):
        """[LEGACY] Get all SSIDs/WLANs."""
        result = self._legacy_request("GET", f"/proxy/network/api/s/{self.site}/rest/wlanconf")
        return result.get("data", []) if isinstance(result, dict) else []

    def set_wlan_enabled(self, wlan_id, enabled):
        """[LEGACY] Enable/disable a WLAN.

        Fetches the full record fresh and PUTs it back with just
        `enabled` flipped - same full-record-update discipline used for
        the v2 API (firewall policies/traffic routes), applied here too
        since sending the full object back is a longstanding UniFi
        controller API convention, not something specific to v2.
        """
        wlans = self.get_wlans()
        wlan = next((w for w in wlans if w.get("_id") == wlan_id), None)
        if not wlan:
            raise UnifiAPIError(f"WLAN {wlan_id} not found")
        wlan["enabled"] = enabled
        return self._legacy_request("PUT", f"/proxy/network/api/s/{self.site}/rest/wlanconf/{wlan_id}",
                                    data=wlan)

    def set_wlan_password(self, wlan_id, new_password):
        """[LEGACY] Change a WLAN's passphrase. `x_passphrase` confirmed
        as the real write field against a live controller (3.12.5) - the
        `x_` prefix is UniFi's own convention for a write-only/sensitive
        field. Same full-record-update pattern as set_wlan_enabled."""
        wlans = self.get_wlans()
        wlan = next((w for w in wlans if w.get("_id") == wlan_id), None)
        if not wlan:
            raise UnifiAPIError(f"WLAN {wlan_id} not found")
        wlan["x_passphrase"] = new_password
        return self._legacy_request("PUT", f"/proxy/network/api/s/{self.site}/rest/wlanconf/{wlan_id}",
                                    data=wlan)

    def get_networks(self):
        """[LEGACY] Get all networks/VLANs."""
        result = self._legacy_request("GET", f"/proxy/network/api/s/{self.site}/rest/networkconf")
        return result.get("data", []) if isinstance(result, dict) else []

    def get_port_forwards(self):
        """[LEGACY] Get all port forward rules. Confirmed real field
        shape against a live controller (3.12.5): _id, name, enabled,
        pfwd_interface, src, dst_port, fwd (destination IP), fwd_port,
        proto, log."""
        result = self._legacy_request("GET", f"/proxy/network/api/s/{self.site}/rest/portforward")
        return result.get("data", []) if isinstance(result, dict) else []

    def set_port_forward_enabled(self, forward_id, enabled):
        """[LEGACY] Enable/disable a port forward rule. Same full-record-
        update pattern as set_wlan_enabled."""
        forwards = self.get_port_forwards()
        fwd = next((f for f in forwards if f.get("_id") == forward_id), None)
        if not fwd:
            raise UnifiAPIError(f"Port forward {forward_id} not found")
        fwd["enabled"] = enabled
        return self._legacy_request("PUT", f"/proxy/network/api/s/{self.site}/rest/portforward/{forward_id}",
                                    data=fwd)

    # ------------------------------------------------------------------
    # v2 API - this is UniFi's own internal API (what the web app itself
    # calls), not the documented Integration API - same cookie/CSRF auth
    # as the legacy surface above (_legacy_request), different base path
    # and response shape: a plain array/object, NOT the legacy {"data":
    # [...]} envelope. Confirmed directly against a live controller
    # (3.12.1), not from documentation alone - see ROADMAP.md.
    # ------------------------------------------------------------------

    def get_firewall_policies(self):
        """[V2] Get all firewall policies (zone-based firewall, the
        primary firewall system on UDM/UDR consoles) - includes Ubiquiti's
        own predefined policies alongside any user-created ones."""
        result = self._legacy_request("GET", f"/proxy/network/v2/api/site/{self.site}/firewall-policies")
        return result if isinstance(result, list) else []

    def set_firewall_policy_enabled(self, policy_id, enabled):
        """[V2] Enable/disable a firewall policy.

        This endpoint requires the FULL policy record on write, not a
        partial body - confirmed via a real-world bug report (a different
        open-source project hit HTTP 400/validation failures sending
        partial updates). So this fetches the current list fresh, finds
        the matching record, flips just `enabled`, and PUTs the complete
        record back - never a hand-built partial body.
        """
        policies = self.get_firewall_policies()
        policy = next((p for p in policies if p.get("_id") == policy_id), None)
        if not policy:
            raise UnifiAPIError(f"Firewall policy {policy_id} not found")
        policy["enabled"] = enabled
        return self._legacy_request(
            "PUT", f"/proxy/network/v2/api/site/{self.site}/firewall-policies/{policy_id}",
            data=policy)

    def get_traffic_routes(self):
        """[V2] Get all traffic routes (policy-based routing - e.g.
        routing specific domains/IPs/clients via a VPN or alternate
        gateway, with an optional kill switch)."""
        result = self._legacy_request("GET", f"/proxy/network/v2/api/site/{self.site}/trafficroutes")
        return result if isinstance(result, list) else []

    def set_traffic_route_enabled(self, route_id, enabled):
        """[V2] Enable/disable a traffic route - same full-record-update
        requirement as set_firewall_policy_enabled above."""
        routes = self.get_traffic_routes()
        route = next((r for r in routes if r.get("_id") == route_id), None)
        if not route:
            raise UnifiAPIError(f"Traffic route {route_id} not found")
        route["enabled"] = enabled
        return self._legacy_request(
            "PUT", f"/proxy/network/v2/api/site/{self.site}/trafficroutes/{route_id}",
            data=route)

    # ------------------------------------------------------------------
    # Public API - actions
    # ------------------------------------------------------------------

    def restart_device(self, mac):
        """[LEGACY] Restart a network device by MAC."""
        # Integration API: POST /sites/{siteId}/devices/{deviceId}/actions {"action":"RESTART"}
        # Switching to that requires mapping MAC -> deviceId from integration list.
        # Keep legacy for now; promote when we add an integration device cache.
        return self._legacy_request("POST", f"/proxy/network/api/s/{self.site}/cmd/devmgr",
                                    data={"cmd": "restart", "mac": mac})

    def locate_device(self, mac, enabled=True):
        """[LEGACY] Flash device LEDs."""
        return self._legacy_request("POST", f"/proxy/network/api/s/{self.site}/cmd/devmgr",
                                    data={"cmd": "set-locate", "mac": mac, "enabled": enabled})

    def set_device_led(self, device_id, enabled):
        """[LEGACY] Enable/disable device LED (uses _id, not mac)."""
        return self._legacy_request("PUT", f"/proxy/network/api/s/{self.site}/rest/device/{device_id}",
                                    data={"led_override": "on" if enabled else "off"})

    def disable_device(self, device_id, disabled=True):
        """[LEGACY] Disable/enable a device."""
        return self._legacy_request("PUT", f"/proxy/network/api/s/{self.site}/rest/device/{device_id}",
                                    data={"disabled": bool(disabled)})

    def cycle_port_poe(self, mac, port_idx):
        """[LEGACY] Power-cycle a PoE port."""
        # Integration API has POST /sites/{siteId}/devices/{deviceId}/interfaces/ports/{port}/actions
        # Will be promoted once we cache integration device IDs.
        return self._legacy_request("POST", f"/proxy/network/api/s/{self.site}/cmd/devmgr",
                                    data={"cmd": "power-cycle", "mac": mac, "port_idx": port_idx})

    def set_port_override(self, device_id, port_idx, overrides):
        """[LEGACY] Update port_overrides on a switch. Preserves other ports' overrides."""
        device = self.get_device(device_id)
        if not device:
            raise UnifiNotFoundError(f"Device {device_id} not found")

        existing = device.get("port_overrides", []) or []
        updated  = []
        found    = False
        for o in existing:
            if o.get("port_idx") == port_idx:
                merged = dict(o)
                merged.update(overrides)
                updated.append(merged)
                found = True
            else:
                updated.append(o)
        if not found:
            updated.append({"port_idx": port_idx, **overrides})

        return self._legacy_request("PUT", f"/proxy/network/api/s/{self.site}/rest/device/{device_id}",
                                    data={"port_overrides": updated})

    def set_port_poe_mode(self, device_id, port_idx, poe_mode):
        """[LEGACY] Set PoE mode on a port: off/auto/pasv24/passthrough."""
        return self.set_port_override(device_id, port_idx, {"poe_mode": poe_mode})

    def block_client(self, mac):
        """[LEGACY] Block a client by MAC."""
        return self._legacy_request("POST", f"/proxy/network/api/s/{self.site}/cmd/stamgr",
                                    data={"cmd": "block-sta", "mac": (mac or "").lower()})

    def unblock_client(self, mac):
        """[LEGACY] Unblock a client by MAC."""
        return self._legacy_request("POST", f"/proxy/network/api/s/{self.site}/cmd/stamgr",
                                    data={"cmd": "unblock-sta", "mac": (mac or "").lower()})

    def reconnect_client(self, mac):
        """[LEGACY] Force a client to reconnect."""
        return self._legacy_request("POST", f"/proxy/network/api/s/{self.site}/cmd/stamgr",
                                    data={"cmd": "kick-sta", "mac": (mac or "").lower()})

    def forget_client(self, mac):
        """[LEGACY] Remove a client from the controller entirely. This is the
        same as right-clicking a client in the UniFi UI and choosing 'Forget'.
        After this the client disappears from the client list. Calls the
        stamgr forget-sta command, which accepts either a single MAC or a
        list of MACs (we send a list for compatibility)."""
        macs = [(mac or "").lower()]
        return self._legacy_request("POST", f"/proxy/network/api/s/{self.site}/cmd/stamgr",
                                    data={"cmd": "forget-sta", "macs": macs})

    # ------------------------------------------------------------------
    # Connectivity tests
    # ------------------------------------------------------------------

    def test_integration(self):
        """[INTEGRATION] Test the API key by hitting /info."""
        result = self._integration_request("GET", "/proxy/network/integration/v1/info")
        return result

    def test_legacy(self):
        """[LEGACY] Test username/password by logging in."""
        if not self.legacy_logged_in:
            self._legacy_login()
        return self.get_site_info()


# ============================================================================
# Protect private update WebSocket - binary frame decoder
# ----------------------------------------------------------------------------
# /proxy/protect/ws/updates is the original real-time channel Protect used
# for years before the official Integration API existed - confirmed still
# live and active by cross-referencing uilibs/uiprotect (MIT License,
# github.com/uilibs/uiprotect), the library behind Home Assistant's Protect
# integration. We use it for exactly one thing: motion detection latency.
# Comparing the Protect app's own near-instant reaction against our
# official-API event timing (a consistent, verified ~3.3s - see the 3.8.3
# changelog entry) narrowed the delay specifically to the official events
# subscription, not the camera's own detection. This channel is what the
# app itself almost certainly uses, so it should share that same speed.
#
# Each message on this connection is TWO consecutive binary frames - an
# "action" frame then a "data" frame - back to back in one WS binary
# message. Each frame is an 8-byte header followed by its payload:
#   struct format "!bbbbi" (network/big-endian):
#     packet_type    (int8)  - not used here
#     payload_format (int8)  - 1 = JSON, others = a binary "NodeBuffer"
#                               format we don't decode (not needed for
#                               motion events, which arrive as JSON)
#     deflated       (int8)  - 1 = payload is zlib-compressed
#     unknown        (int8)
#     payload_size   (int32) - length of the payload that follows
# The action frame carries {"action": "add"|"update"|"remove",
# "modelKey": "event"|"camera"|..., "id": "...", "newUpdateId": "..."};
# the data frame carries the actual fields. For a motion event, the data
# frame's camera-reference field is called "camera" - NOT "device" like the
# official API's event schema (confirmed against uiprotect's Event model,
# whose `_get_unifi_remaps` maps the wire key "camera" to its own
# `camera_id` attribute). Mixing the two field names up would silently
# drop every message, the same way the original events-WS envelope bug did
# in 3.8.1 - so this is deliberately NOT assumed to match the official
# schema anywhere.
# ============================================================================

def _decode_protect_private_frame(raw_bytes):
    """Decode one private-WS binary message into (action_dict, data_obj).
    Returns (None, None) if either frame can't be parsed. data_obj is the
    parsed JSON dict for JSON-format frames, or raw bytes for the binary
    "NodeBuffer" format (which we don't attempt to decode - callers should
    check the action's modelKey/type before assuming data_obj is a dict)."""
    def _read_one(buf, pos):
        try:
            packet_type, payload_format, deflated, unknown, payload_size = \
                struct.unpack_from("!bbbbi", buf, pos)
        except struct.error:
            return None, pos
        start = pos + 8
        end   = start + payload_size
        if end > len(buf):
            return None, pos
        chunk = buf[start:end]
        if deflated:
            try:
                chunk = zlib.decompress(chunk)
            except Exception:
                return None, end
        if payload_format == 1:  # JSON
            try:
                return json.loads(chunk.decode("utf-8")), end
            except Exception:
                return None, end
        return chunk, end  # raw bytes - not JSON, caller won't use this

    action, next_pos = _read_one(raw_bytes, 0)
    if action is None:
        return None, None
    data, _ = _read_one(raw_bytes, next_pos)
    return action, data


# ============================================================================
# UniFi Protect Integration API
# ----------------------------------------------------------------------------
# Uses the official Integration API at /proxy/protect/integration/v1/...
# with Bearer-token auth. JSON WebSocket subscriptions for real-time events
# (devices and events streams).
# ============================================================================

class UnifiProtectAPI:
    """UniFi Protect via the official Integration API (Bearer token + JSON WebSocket).

    Optionally also authenticates against the older private/local API
    (same `/api/auth/login` as Network's and Drive's legacy auth - this is
    a UniFi OS console-level login, not a Protect-specific one) to read a
    handful of monitoring fields the official API doesn't expose at all:
    firmwareVersion, host (IP), upSince (uptime), voltage, recordingSettings,
    ispSettings (IR mode), and wiredConnectionState (link speed). Confirmed
    against uilibs/uiprotect (MIT License) - github.com/uilibs/uiprotect,
    src/uiprotect/api.py (login flow) and src/uiprotect/data/devices.py +
    data/base.py (field names). This is genuinely a different, older API
    surface - not a trick or a shortcut, just the one place this particular
    data still lives. RTSP streaming is NOT sourced from here: the official
    rtsps-stream endpoint (see 3.7.0) is the current, correct mechanism -
    recent uiprotect releases have themselves dropped the old private-API
    RTSP alias fields, suggesting Home Assistant made the same move.
    Legacy auth here is entirely optional - everything works without it,
    just without those specific fields.
    """

    BASE_PATH        = "/proxy/protect/integration/v1"
    WS_DEVICES       = "/proxy/protect/integration/v1/subscribe/devices"
    WS_EVENTS        = "/proxy/protect/integration/v1/subscribe/events"
    WS_PRIVATE_UPDATES = "/proxy/protect/ws/updates"  # legacy real-time channel - see _decode_protect_private_frame
    LEGACY_BASE_PATH = "/proxy/protect/api"

    def __init__(self, host, port, api_key, username="", password="",
                 verify_ssl=False, logger=None, debug=False):
        self.host        = host
        self.port        = int(port)
        self.api_key     = api_key
        self.username    = username
        self.password    = password
        self.verify_ssl  = verify_ssl
        self.logger      = logger
        self.debug       = debug

        self.base_url    = f"https://{host}:{self.port}"
        self.ssl_context = _build_ssl_context(verify_ssl)
        self.opener      = _build_opener(self.ssl_context)

        # Cached lookups, refreshed on demand
        self._cameras_by_id = {}
        self._sensors_by_id = {}
        self._lights_by_id  = {}
        self._chimes_by_id  = {}
        self._nvr_info      = {}
        self._cache_time    = 0

        # Legacy/private-API session state (optional - only used if
        # username/password are provided)
        self.legacy_cookies    = CookieJar()
        self.legacy_opener     = _build_opener(self.ssl_context, self.legacy_cookies)
        self.legacy_csrf       = None
        self.legacy_logged_in  = False
        self._bootstrap_cameras_by_id = {}
        self._bootstrap_cache_time    = 0
        self._bootstrap_raw           = {}

    # ------------------------------------------------------------------
    # Internal request
    # ------------------------------------------------------------------

    def _request(self, method, path, data=None, timeout=DEFAULT_HTTP_TIMEOUT_SEC):
        if not self.api_key:
            raise UnifiAuthError("Protect API key is not configured")

        url  = f"{self.base_url}{path}"
        body = json.dumps(data).encode("utf-8") if data is not None else None

        req = urllib.request.Request(url, data=body, method=method)
        req.add_header("X-API-KEY", self.api_key)
        req.add_header("Accept", "application/json")
        if body is not None:
            req.add_header("Content-Type", "application/json")

        try:
            response = self.opener.open(req, timeout=timeout)
            payload  = response.read().decode("utf-8")
            return json.loads(payload) if payload else {}
        except urllib.error.HTTPError as e:
            err_body = _decode_error_body(e)
            if e.code in (401, 403):
                raise UnifiAuthError(f"Protect API auth failed (HTTP {e.code}): {err_body}")
            if e.code == 404:
                raise UnifiNotFoundError(f"Protect resource not found: {path}")
            raise UnifiAPIError(f"Protect API HTTP {e.code}: {err_body}")
        except urllib.error.URLError as e:
            raise UnifiAPIError(f"Protect API network error: {e}")
        except json.JSONDecodeError as e:
            raise UnifiAPIError(f"Protect API returned invalid JSON: {e}")

    # ------------------------------------------------------------------
    # Cache management
    # ------------------------------------------------------------------

    def refresh_cache(self):
        """Refresh the internal device list caches."""
        cameras = self._request("GET", f"{self.BASE_PATH}/cameras")
        sensors = self._request("GET", f"{self.BASE_PATH}/sensors")
        try:
            lights = self._request("GET", f"{self.BASE_PATH}/lights")
        except UnifiNotFoundError:
            lights = []
        try:
            chimes = self._request("GET", f"{self.BASE_PATH}/chimes")
        except UnifiNotFoundError:
            chimes = []
        try:
            nvrs = self._request("GET", f"{self.BASE_PATH}/nvrs")
            # Some Protect versions expose only one NVR; some return an array
            self._nvr_info = nvrs[0] if isinstance(nvrs, list) and nvrs else nvrs
        except UnifiNotFoundError:
            self._nvr_info = {}

        # Each list response is a list of objects; build by-id maps
        self._cameras_by_id = {c.get("id"): c for c in (cameras or []) if c.get("id")}
        self._sensors_by_id = {s.get("id"): s for s in (sensors or []) if s.get("id")}
        self._lights_by_id  = {l.get("id"): l for l in (lights  or []) if l.get("id")}
        self._chimes_by_id  = {ch.get("id"): ch for ch in (chimes or []) if ch.get("id")}
        self._cache_time    = time.time()

        # Optional: refresh the legacy bootstrap cache too, if credentials
        # are configured. One request regardless of device count (the
        # bootstrap returns everything in one shot), so this rides the same
        # bulk-refresh cadence with no added request-burst risk. A failure
        # here (wrong/missing legacy credentials, MFA enabled, etc.) must
        # never break the official-API refresh above.
        if self.username and self.password:
            try:
                self._refresh_bootstrap_cache()
            except Exception as e:
                if self.debug and self.logger:
                    self.logger.debug(f"Protect legacy bootstrap refresh failed: {e}")

    def _ensure_cache(self, max_age_sec=60):
        """Refresh cache if older than max_age_sec."""
        if (time.time() - self._cache_time) > max_age_sec:
            self.refresh_cache()

    # ------------------------------------------------------------------
    # Legacy/private API (optional - for fields the official API doesn't
    # expose). Same /api/auth/login as Network's and Drive's legacy auth -
    # this is a console-level UniFi OS login, not Protect-specific, so it
    # uses the exact same credentials already configured for those.
    # ------------------------------------------------------------------

    def _legacy_login(self):
        """Authenticate against the private API. Idempotent."""
        if not self.username or not self.password:
            raise UnifiAuthError("Protect legacy username/password not configured")

        url  = f"{self.base_url}/api/auth/login"
        body = json.dumps({
            "username":   self.username,
            "password":   self.password,
            "rememberMe": True,
        }).encode("utf-8")

        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json")

        try:
            response = self.legacy_opener.open(req, timeout=DEFAULT_HTTP_TIMEOUT_SEC)
            # uiprotect reads the CSRF token straight from this response
            # header, which is simpler/more reliable than decoding it out
            # of the JWT cookie payload (the approach Network/Drive use) -
            # try that first, fall back to the JWT-decode approach in case
            # a given firmware version doesn't set the header.
            self.legacy_csrf = response.headers.get("x-csrf-token")
            response.read()
        except urllib.error.HTTPError as e:
            err_body = _decode_error_body(e)
            if e.code in (401, 403):
                raise UnifiAuthError(
                    f"Protect legacy login failed (HTTP {e.code}). "
                    f"Use a LOCAL-ONLY admin (no MFA, not a UI.com cloud account). "
                    f"Details: {err_body}"
                )
            raise UnifiAuthError(f"Protect legacy login HTTP {e.code}: {err_body}")
        except urllib.error.URLError as e:
            raise UnifiAuthError(f"Protect legacy login network error: {e}")

        if not self.legacy_csrf:
            for cookie in self.legacy_cookies:
                if cookie.name in ("TOKEN", "UOS_TOKEN"):
                    try:
                        parts = cookie.value.split(".")
                        if len(parts) >= 2:
                            payload = parts[1]
                            padding = (-len(payload)) % 4
                            if padding:
                                payload += "=" * padding
                            decoded = base64.urlsafe_b64decode(payload)
                            self.legacy_csrf = json.loads(decoded).get("csrfToken")
                    except Exception:
                        pass
                    break

        self.legacy_logged_in = True

    def _legacy_request(self, method, path, data=None, timeout=DEFAULT_HTTP_TIMEOUT_SEC, _retry=True):
        """Request against the private API. Logs in lazily, retries once on 401."""
        if not self.legacy_logged_in:
            self._legacy_login()

        url  = f"{self.base_url}{path}"
        body = json.dumps(data).encode("utf-8") if data is not None else None
        req  = urllib.request.Request(url, data=body, method=method)
        req.add_header("Accept", "application/json")
        if body is not None:
            req.add_header("Content-Type", "application/json")
        if self.legacy_csrf and method in ("POST", "PUT", "PATCH", "DELETE"):
            req.add_header("X-CSRF-Token", self.legacy_csrf)

        try:
            response = self.legacy_opener.open(req, timeout=timeout)
            payload  = response.read().decode("utf-8")
            return json.loads(payload) if payload else {}
        except urllib.error.HTTPError as e:
            if e.code == 401 and _retry:
                self.legacy_logged_in = False
                self.legacy_csrf      = None
                return self._legacy_request(method, path, data, timeout, _retry=False)
            err_body = _decode_error_body(e)
            if e.code in (401, 403):
                raise UnifiAuthError(f"Protect legacy auth failed (HTTP {e.code}): {err_body}")
            raise UnifiAPIError(f"Protect legacy API HTTP {e.code}: {err_body}")
        except urllib.error.URLError as e:
            raise UnifiAPIError(f"Protect legacy API network error: {e}")
        except json.JSONDecodeError as e:
            raise UnifiAPIError(f"Protect legacy API returned invalid JSON: {e}")

    def _refresh_bootstrap_cache(self):
        """Fetch the full bootstrap payload (one request, every device at
        once - mirrors the bulk-refresh pattern already used for the
        official API) and index its cameras by ID for fast lookup."""
        data = self._legacy_request("GET", f"{self.LEGACY_BASE_PATH}/bootstrap")
        cameras = (data or {}).get("cameras", [])
        self._bootstrap_cameras_by_id = {
            c.get("id"): c for c in cameras if isinstance(c, dict) and c.get("id")
        }
        # Kept in full for the Alarm Manager diagnostic dump - its real
        # structure is unconfirmed (no Alarm Manager hardware available to
        # check against, see ROADMAP.md), so the raw payload is what lets
        # a user with real hardware send back what's actually there.
        self._bootstrap_raw = data or {}
        self._bootstrap_cache_time = time.time()

    def get_bootstrap_camera(self, camera_id):
        """Return this camera's entry from the cached bootstrap payload, or
        None if legacy auth isn't configured/working or this camera isn't
        in it. Never fetches on its own - populated by refresh_cache()."""
        return self._bootstrap_cameras_by_id.get(camera_id)

    # ------------------------------------------------------------------
    # Alarm Manager (arm/disarm) - UNVERIFIED, built without access to any
    # Alarm Manager hardware. Based entirely on one community-confirmed
    # endpoint shape (a forum script, cross-referenced against a recent
    # HACS integration's documented requirements) - not Ubiquiti's own
    # documented API, and not tested against a real response from this
    # plugin. See ROADMAP.md for the full reasoning and what's confirmed
    # vs. guessed. Every method here is written to fail informatively
    # rather than silently, since that's the best we can do without a real
    # payload - get_alarm_debug_dump() exists specifically so a user who
    # actually has this hardware can send back what's really there.
    #
    # Known requirement (confirmed via the community reference, not
    # guessed): arming/disarming needs the configured legacy/local Protect
    # account to hold the Super Admin role specifically, with MFA disabled
    # on that account - no lesser role has been reported to work. A 401/
    # 403 from either arm method most likely means this, not a wrong
    # endpoint, and the error messages below say so.
    # ------------------------------------------------------------------

    def get_bootstrap_raw(self):
        """Return the full raw legacy bootstrap payload. Covers anything
        confirmed real but legacy-only (not in the official schema) -
        Alarm Manager's armMode, a camera's hasPackageCamera flag, etc.
        Never fetches on its own - populated by refresh_cache(), same as
        get_bootstrap_camera()."""
        return self._bootstrap_raw

    def get_alarm_debug_dump(self):
        """Alias of get_bootstrap_raw() - kept under its original name
        too since the Alarm Manager diagnostic (dumpAlarmProfilePayload)
        already calls it by this name."""
        return self.get_bootstrap_raw()

    def get_alarm_profiles(self):
        """Attempt to find configured arm profiles (e.g. Home/Away/Night)
        from the cached bootstrap payload. UNVERIFIED: no real payload
        exists to confirm where these actually live, so this checks every
        plausible location (top-level, under `nvr`, under a `hub` entry)
        rather than assuming one. Returns [] if nothing plausible is
        found - callers should treat that as "couldn't find it", not
        "there are genuinely none", and point the user at
        get_alarm_debug_dump()."""
        data = self._bootstrap_raw or {}
        candidate_keys = ("armProfiles", "arm_profiles", "alarmProfiles", "alarm_profiles")
        for key in candidate_keys:
            if isinstance(data.get(key), list) and data[key]:
                return data[key]
        nvr = data.get("nvr") or {}
        for key in candidate_keys:
            if isinstance(nvr.get(key), list) and nvr[key]:
                return nvr[key]
        for hub in (data.get("hubs") or []):
            if isinstance(hub, dict):
                for key in candidate_keys:
                    if isinstance(hub.get(key), list) and hub[key]:
                        return hub[key]
        return []

    def get_alarm_state(self):
        """Current arm state (armed/disarmed, breach info).

        `bootstrap.nvr.armMode` is CONFIRMED real (3.12.8) - verified
        directly against a live controller (not guessed, not taken on
        faith from a third party): {"status": "disabled", "armedAt":
        null, "willBeArmedAt": null, "breachDetectedAt": null,
        "breachEventCount": 0, "breachTriggerEventId": null,
        "breachEventId": null}. This is checked first and returned
        directly when present.

        Important limit, not glossed over: armMode gives overall arm
        status (and breach/siren info), but no field identifying WHICH
        arm profile is active - so "is Protect armed at all" is now
        reliable, "is it armed to THIS specific profile" still isn't.
        _update_alarmprofile_states uses armMode.status as a confident
        baseline (status == "disabled" means every profile device should
        show Disarmed, full stop) and falls back to the old
        every-plausible-location guesses only for the "which profile"
        question armMode can't answer.
        """
        data = self._bootstrap_raw or {}
        nvr = data.get("nvr") or {}
        if isinstance(nvr.get("armMode"), dict):
            return nvr["armMode"]
        # Fallback for any future renaming - unconfirmed, kept only as a
        # safety net, not the expected path.
        for key in ("armState", "arm_state", "alarmState", "alarm_state"):
            if key in data:
                return data[key]
            if key in nvr:
                return nvr[key]
        for hub in (data.get("hubs") or []):
            if isinstance(hub, dict):
                for key in ("armState", "arm_state", "alarmState", "alarm_state", "state"):
                    if key in hub:
                        return hub[key]
        return None

    def set_alarm_arm(self, profile_id):
        """Arm using the given profile ID. Endpoint and body shape
        confirmed via a community forum script
        (POST .../arm/enable, body {"armProfileId": ...}) - not Ubiquiti's
        own documented API."""
        try:
            return self._legacy_request("POST", f"{self.LEGACY_BASE_PATH}/arm/enable",
                                        data={"armProfileId": profile_id})
        except UnifiAuthError:
            raise UnifiAuthError(
                "Arming failed on auth (401/403). This almost always means the "
                "configured Protect account isn't a Super Admin, or has MFA "
                "enabled - both are required for Alarm Manager control, per "
                "the only confirmed community reference for this (undocumented) "
                "endpoint. Not necessarily a wrong profile ID.")

    def set_alarm_disarm(self, profile_id):
        """Disarm. Same endpoint family as set_alarm_arm() - the
        confirmed reference script passes armProfileId here too, even
        though disarming doesn't obviously need to know which profile was
        active; followed exactly as confirmed rather than second-guessed."""
        try:
            return self._legacy_request("POST", f"{self.LEGACY_BASE_PATH}/arm/disable",
                                        data={"armProfileId": profile_id})
        except UnifiAuthError:
            raise UnifiAuthError(
                "Disarming failed on auth (401/403). Same likely cause as "
                "arming - see set_alarm_arm()'s error for detail.")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_nvr(self):
        """Get NVR info."""
        self._ensure_cache()
        return self._nvr_info

    def trigger_alarm_webhook(self, trigger_id):
        """Trigger any Alarm Manager alarm configured with a matching
        Webhook trigger ID. Confirmed official endpoint (OpenAPI spec,
        "Alarm manager integration" tag) - POST, no request body, `id` is
        a path parameter only, 204 on success. This is NOT the same thing
        as the experimental arm/disarm elsewhere in this file: it fires
        whatever alarm action the user has already configured in Protect
        for this trigger ID (Alarm Manager > alarm > Webhook trigger), it
        doesn't arm/disarm anything itself, and it's genuinely official
        rather than reverse-engineered."""
        return self._request("POST", f"{self.BASE_PATH}/alarm-manager/webhook/{trigger_id}")

    def get_cameras(self):
        """Get all cameras."""
        self._ensure_cache()
        return list(self._cameras_by_id.values())

    def get_camera(self, camera_id):
        """Get a single camera by ID; refresh from server."""
        # Always refresh single-camera reads to get up-to-date state for triggers
        try:
            data = self._request("GET", f"{self.BASE_PATH}/cameras/{camera_id}")
            if data and data.get("id"):
                self._cameras_by_id[data["id"]] = data
            return data
        except UnifiNotFoundError:
            self._cameras_by_id.pop(camera_id, None)
            return None

    def get_sensors(self):
        self._ensure_cache()
        return list(self._sensors_by_id.values())

    def get_sensor(self, sensor_id):
        try:
            data = self._request("GET", f"{self.BASE_PATH}/sensors/{sensor_id}")
            if data and data.get("id"):
                self._sensors_by_id[data["id"]] = data
            return data
        except UnifiNotFoundError:
            self._sensors_by_id.pop(sensor_id, None)
            return None

    def get_lights(self):
        self._ensure_cache()
        return list(self._lights_by_id.values())

    def get_chimes(self):
        self._ensure_cache()
        return list(self._chimes_by_id.values())

    # ----- Actions -----

    def update_camera(self, camera_id, settings):
        """PATCH camera settings - send only fields you want to change."""
        return self._request("PATCH", f"{self.BASE_PATH}/cameras/{camera_id}", data=settings)

    def reboot_camera(self, camera_id):
        """Reboot a camera via the integration action endpoint."""
        return self._request("POST", f"{self.BASE_PATH}/cameras/{camera_id}/reboot")

    def get_camera_snapshot_url(self, camera_id):
        """Build a snapshot URL using Bearer auth (must be fetched with the API key header)."""
        return f"{self.base_url}{self.BASE_PATH}/cameras/{camera_id}/snapshot"

    # ------------------------------------------------------------------
    # RTSPS streams
    # ----------------------------------------------------------------------
    # Confirmed against the official Protect OpenAPI spec
    # (developer.ui.com/protect/<version>/openapi.json): RTSPS stream URLs
    # are NOT a field on the camera object itself (the official schema's
    # `camera` type has no `channels`/`isRtspEnabled`/`rtspAlias` - those
    # belong to the old, unofficial local/legacy Protect API that community
    # tools like pyunifiprotect reverse-engineered). On the official
    # Integration API, RTSPS streaming is a separate opt-in resource: it
    # does not exist until explicitly created with a POST, and GET simply
    # reports which qualities (high/medium/low/package) currently have an
    # active stream - all null until something creates one.
    # ------------------------------------------------------------------

    def get_rtsps_streams(self, camera_id):
        """Read-only: report which RTSPS stream qualities currently exist
        for this camera. Returns a dict with up to 4 keys (high/medium/low/
        package), each a URL string or None. No side effects - safe to call
        on every poll. Raises nothing for a camera with no streams at all;
        that's a normal (all-null) response, not an error."""
        try:
            result = self._request("GET", f"{self.BASE_PATH}/cameras/{camera_id}/rtsps-stream")
            return result if isinstance(result, dict) else {}
        except UnifiNotFoundError:
            return {}

    def create_rtsps_streams(self, camera_id, qualities):
        """Create (enable) RTSPS streams for this camera at the given
        quality levels. `qualities` is one of, or a list of any of:
        'high', 'medium', 'low', 'package'. Returns a dict of
        quality -> URL for the streams just created."""
        if isinstance(qualities, str):
            qualities = [qualities]
        return self._request(
            "POST", f"{self.BASE_PATH}/cameras/{camera_id}/rtsps-stream",
            data={"qualities": list(qualities)},
        )

    def delete_rtsps_streams(self, camera_id, qualities):
        """Remove (disable) RTSPS streams for this camera at the given
        quality levels. `qualities` is one of, or a list of any of:
        'high', 'medium', 'low', 'package'. The API takes these as
        repeated query parameters, not a body."""
        if isinstance(qualities, str):
            qualities = [qualities]
        query = urllib.parse.urlencode([("qualities", q) for q in qualities])
        return self._request(
            "DELETE", f"{self.BASE_PATH}/cameras/{camera_id}/rtsps-stream?{query}",
        )

    # ----- Connectivity test -----

    def test_connection(self):
        """Test the Protect API by fetching the meta info / NVR list."""
        try:
            return self._request("GET", f"{self.BASE_PATH}/meta/info")
        except (UnifiNotFoundError, UnifiAPIError):
            # /meta/info isn't on every version; fall back to nvrs list
            return self._request("GET", f"{self.BASE_PATH}/nvrs")


# ============================================================================
# UniFi Access Developer API
# ----------------------------------------------------------------------------
# Runs on its own port (12445), uses Bearer-token auth, and
# /api/v1/developer/... paths. This is a SEPARATE API from the Integrations
# page used by Network and Protect — the token must be generated inside the
# UniFi Access application itself (not from the unified Integrations menu).
# Per Ubiquiti's official Access API reference at
# https://assets.identity.ui.com/unifi-access/api_reference.pdf
# ============================================================================

class UnifiAccessAPI:
    """UniFi Access via its Developer API (port 12445, Bearer token, separate from Network/Protect API)."""

    def __init__(self, host, port, api_key, verify_ssl=False, logger=None, debug=False):
        self.host        = host
        self.port        = int(port)
        self.api_key     = api_key
        self.verify_ssl  = verify_ssl
        self.logger      = logger
        self.debug       = debug

        self.base_url    = f"https://{host}:{self.port}"
        self.ssl_context = _build_ssl_context(verify_ssl)
        self.opener      = _build_opener(self.ssl_context)

    def _request(self, method, path, data=None, timeout=DEFAULT_HTTP_TIMEOUT_SEC):
        if not self.api_key:
            raise UnifiAuthError("Access API key is not configured")

        url  = f"{self.base_url}{path}"
        body = json.dumps(data).encode("utf-8") if data is not None else None

        req = urllib.request.Request(url, data=body, method=method)
        req.add_header("Authorization", f"Bearer {self.api_key}")
        req.add_header("Accept", "application/json")
        if body is not None:
            req.add_header("Content-Type", "application/json")

        try:
            response = self.opener.open(req, timeout=timeout)
            payload  = response.read().decode("utf-8")
            return json.loads(payload) if payload else {}
        except urllib.error.HTTPError as e:
            err_body = _decode_error_body(e)
            if e.code in (401, 403):
                raise UnifiAuthError(f"Access API auth failed (HTTP {e.code}): {err_body}")
            if e.code == 404:
                raise UnifiNotFoundError(f"Access resource not found: {path}")
            raise UnifiAPIError(f"Access API HTTP {e.code}: {err_body}")
        except urllib.error.URLError as e:
            raise UnifiAPIError(f"Access API network error: {e}")
        except json.JSONDecodeError as e:
            raise UnifiAPIError(f"Access API returned invalid JSON: {e}")

    def _data_payload(self, response):
        """Access API wraps results in {'data': ...}. Unwrap safely."""
        if isinstance(response, dict) and "data" in response:
            return response["data"]
        return response

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_doors(self):
        """List all doors."""
        result = self._request("GET", "/api/v1/developer/doors")
        return self._data_payload(result) or []

    def get_door(self, door_id):
        """Get a specific door by ID."""
        result = self._request("GET", f"/api/v1/developer/doors/{door_id}")
        return self._data_payload(result)

    def unlock_door(self, door_id):
        """Momentarily unlock a door (the door re-locks on its own once
        closed, or per whatever pulse duration the door hardware itself is
        configured with - this call doesn't specify a duration).

        Path and request body confirmed against the real UniFi Access
        OpenAPI docs (unifi-access-api-openapi.pages.dev) - the previous
        path (`/unlocking_rule`) doesn't exist at all, which is exactly
        why it 404'd ("resource not found") rather than failing on auth
        or permissions. The real endpoint is `/unlock`, and its body takes
        optional actor_id/actor_name/extra fields for UniFi's own access
        log attribution - actor_name is set so remote unlocks from Indigo
        are distinguishable in UniFi's own history from a normal
        NFC/PIN/fingerprint unlock. actor_id is left out rather than
        fabricated: it's meant to reference a real UniFi Access user ID,
        which this plugin doesn't have one of.
        """
        return self._request("PUT", f"/api/v1/developer/doors/{door_id}/unlock",
                             data={"actor_name": "Indigo"})

    def set_door_lock_rule(self, door_id, rule_type, interval=None):
        """Set a door's temporary locking rule - confirmed real endpoint
        (official OpenAPI docs show it exists, available since UniFi
        Access v1.24.6), full set of valid rule_type values confirmed via
        a working openHAB UniFi Access binding (independent source, cross-
        checked against the one official example - both agree on
        "keep_lock"):
          - keep_lock:   lock indefinitely, overriding any schedule
          - keep_unlock: unlock indefinitely ("leave unlocked")
          - custom:      unlock for `interval` seconds
          - lock_now:    cancel any active unlock/leave-unlocked rule and
                         lock immediately - this is the real "Lock" action
          - lock_early:  lock now if currently on an unlock SCHEDULE
                         specifically (narrower than lock_now)
        """
        data = {"type": rule_type}
        if interval is not None:
            data["interval"] = interval
        return self._request("PUT", f"/api/v1/developer/doors/{door_id}/lock_rule",
                             data=data)

    def get_devices(self):
        """List all access devices (hubs, readers)."""
        result = self._request("GET", "/api/v1/developer/devices")
        return self._data_payload(result) or []

    def get_access_policies(self):
        """List access policies."""
        result = self._request("GET", "/api/v1/developer/access_policies")
        return self._data_payload(result) or []

    def test_connection(self):
        """Test the Access API by listing doors."""
        return self._request("GET", "/api/v1/developer/doors")


# ============================================================================
# UniFi Drive (UNAS) - LEGACY only
# ----------------------------------------------------------------------------
# No official API exists yet. Uses the legacy cookie session created by the
# Network legacy auth (shares the cookie jar via the same UnifiNetworkAPI).
# When Ubiquiti publishes a Drive integration API, this class can be split.
# ============================================================================

class UnifiDriveAPI:
    """Standalone UniFi Drive (UNAS) API client.

    Endpoints and payload formats based on the memphi2/ha-unifi-drive
    integration (MIT-licensed), which is actively maintained against current
    UNAS firmware. Earlier reverse-engineered /proxy/drive/api/storage paths
    returned 404 on current firmware; the v2 paths used here are correct.

    Authentication supports either:
      * X-API-Key header (preferred — generated from UNAS Integrations page)
      * Legacy cookie auth via /api/auth/login (username + password fallback)
    """

    def __init__(self, host, port, api_key="", username="", password="",
                 verify_ssl=False, logger=None, debug=False):
        self.host         = host
        self.port         = int(port)
        self.api_key      = api_key
        self.username     = username
        self.password     = password
        self.verify_ssl   = verify_ssl
        self.logger       = logger
        self.debug        = debug

        self.base_url     = f"https://{host}:{self.port}"
        self.ssl_context  = _build_ssl_context(verify_ssl)

        # Separate openers: API-key path (no cookies) and legacy (with cookies)
        self.api_opener       = _build_opener(self.ssl_context)
        self.legacy_cookies   = CookieJar()
        self.legacy_opener    = _build_opener(self.ssl_context, self.legacy_cookies)
        self.legacy_csrf      = None
        self.legacy_logged_in = False

        # Firmware-aware caches
        self._fan_mode_write_payload_hint = None  # which payload shape last worked

    @property
    def auth_mode(self):
        """Return 'api_key' or 'legacy' depending on which auth path is active."""
        return "api_key" if self.api_key else "legacy"

    # ------------------------------------------------------------------
    # Auth - API key path
    # ------------------------------------------------------------------

    def _api_request(self, method, path, data=None, timeout=DEFAULT_HTTP_TIMEOUT_SEC,
                     expect_json=True):
        """Request using X-API-Key auth (preferred when configured)."""
        if not self.api_key:
            raise UnifiAuthError("Drive API key is not configured")

        url  = f"{self.base_url}{path}"
        body = json.dumps(data).encode("utf-8") if data is not None else None
        req  = urllib.request.Request(url, data=body, method=method)
        req.add_header("X-API-Key", self.api_key)
        req.add_header("Accept", "application/json")
        if body is not None:
            req.add_header("Content-Type", "application/json")

        try:
            response = self.api_opener.open(req, timeout=timeout)
            payload  = response.read().decode("utf-8", errors="replace")
            if not expect_json:
                return response.status, payload
            return json.loads(payload) if payload else {}
        except urllib.error.HTTPError as e:
            err_body = _decode_error_body(e)
            if e.code in (401, 403):
                raise UnifiAuthError(f"Drive API auth failed (HTTP {e.code}): {err_body}")
            raise UnifiAPIError(f"Drive API HTTP {e.code}: {err_body}")
        except urllib.error.URLError as e:
            raise UnifiAPIError(f"Drive API network error: {e}")
        except json.JSONDecodeError as e:
            raise UnifiAPIError(f"Drive API returned invalid JSON: {e}")

    # ------------------------------------------------------------------
    # Auth - Legacy cookie path
    # ------------------------------------------------------------------

    def _legacy_login(self):
        """Authenticate against /api/auth/login. Idempotent."""
        if not self.username or not self.password:
            raise UnifiAuthError("Drive legacy username/password not configured")

        url  = f"{self.base_url}{DRIVE_LOGIN_PATH}"
        body = json.dumps({
            "username":   self.username,
            "password":   self.password,
            "rememberMe": True,
        }).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json")

        try:
            response = self.legacy_opener.open(req, timeout=DEFAULT_HTTP_TIMEOUT_SEC)
            response.read()
        except urllib.error.HTTPError as e:
            err_body = _decode_error_body(e)
            if e.code in (401, 403):
                raise UnifiAuthError(
                    f"Drive legacy login failed (HTTP {e.code}). "
                    f"Use a LOCAL-ONLY admin (no MFA). Details: {err_body}"
                )
            raise UnifiAuthError(f"Drive legacy login HTTP {e.code}: {err_body}")
        except urllib.error.URLError as e:
            raise UnifiAuthError(f"Drive legacy login network error: {e}")

        # Extract CSRF token from the JWT TOKEN cookie payload
        self.legacy_csrf = None
        for cookie in self.legacy_cookies:
            if cookie.name == "TOKEN":
                try:
                    parts = cookie.value.split(".")
                    if len(parts) >= 2:
                        payload = parts[1]
                        padding = (-len(payload)) % 4
                        if padding:
                            payload += "=" * padding
                        decoded = base64.urlsafe_b64decode(payload)
                        self.legacy_csrf = json.loads(decoded).get("csrfToken")
                except Exception:
                    pass
                break

        self.legacy_logged_in = True

    def _legacy_request(self, method, path, data=None, timeout=DEFAULT_HTTP_TIMEOUT_SEC,
                        expect_json=True, _retry=True):
        """Request using legacy cookie auth. Logs in lazily, retries once on 401."""
        if not self.legacy_logged_in:
            self._legacy_login()

        url  = f"{self.base_url}{path}"
        body = json.dumps(data).encode("utf-8") if data is not None else None
        req  = urllib.request.Request(url, data=body, method=method)
        req.add_header("Accept", "application/json")
        if body is not None:
            req.add_header("Content-Type", "application/json")
        if self.legacy_csrf and method in ("POST", "PUT", "PATCH", "DELETE"):
            req.add_header("X-CSRF-Token", self.legacy_csrf)

        try:
            response = self.legacy_opener.open(req, timeout=timeout)
            payload  = response.read().decode("utf-8", errors="replace")
            if not expect_json:
                return response.status, payload
            if not payload:
                return {}
            if payload.lstrip().startswith("<"):
                raise UnifiAPIError("Drive legacy API returned HTML (endpoint may not exist on this firmware)")
            return json.loads(payload)
        except urllib.error.HTTPError as e:
            if e.code == 401 and _retry:
                self.legacy_logged_in = False
                self.legacy_csrf      = None
                return self._legacy_request(method, path, data, timeout, expect_json, _retry=False)
            err_body = _decode_error_body(e)
            if e.code in (401, 403):
                raise UnifiAuthError(f"Drive legacy auth failed (HTTP {e.code}): {err_body}")
            raise UnifiAPIError(f"Drive legacy API HTTP {e.code}: {err_body}")
        except urllib.error.URLError as e:
            raise UnifiAPIError(f"Drive legacy API network error: {e}")
        except json.JSONDecodeError as e:
            raise UnifiAPIError(f"Drive legacy API returned invalid JSON: {e}")

    # ------------------------------------------------------------------
    # Internal dispatch — pick the right auth path
    # ------------------------------------------------------------------

    def _request(self, method, path, data=None, timeout=DEFAULT_HTTP_TIMEOUT_SEC,
                 expect_json=True):
        if self.api_key:
            return self._api_request(method, path, data=data, timeout=timeout,
                                     expect_json=expect_json)
        return self._legacy_request(method, path, data=data, timeout=timeout,
                                    expect_json=expect_json)

    def _request_raw(self, method, path, data=None, timeout=DEFAULT_HTTP_TIMEOUT_SEC):
        """Like _request but returns (status_code, body_text) without JSON parsing.
        Used for endpoints that may return non-JSON or where we need the status."""
        try:
            return self._request(method, path, data=data, timeout=timeout, expect_json=False)
        except UnifiAPIError as e:
            # Return synthetic error status so callers can fall through gracefully
            return 0, str(e)

    # ------------------------------------------------------------------
    # Public API — Storage & system
    # ------------------------------------------------------------------

    def get_storage_info(self):
        """Get the full storage payload from the UNAS Drive endpoint."""
        result = self._request("GET", DRIVE_STORAGE_PATH)
        if isinstance(result, dict):
            return result.get("data", result)
        return result if isinstance(result, dict) else {}

    def get_system_info(self):
        """Get UniFi OS system info (hostname, version, uptime, etc.)."""
        result = self._request("GET", DRIVE_SYSTEM_PATH)
        if isinstance(result, dict):
            return result.get("data", result)
        return result if isinstance(result, dict) else {}

    # ------------------------------------------------------------------
    # Public API — Fan control
    # ------------------------------------------------------------------

    def get_fan_info(self):
        """Read fan control state. Returns {} if endpoint unavailable."""
        try:
            result = self._request("GET", DRIVE_FAN_CONTROL_PATH)
            if isinstance(result, dict):
                return result.get("data", result)
        except UnifiAPIError as e:
            if self.debug and self.logger:
                self.logger.debug(f"Fan control endpoint unavailable: {e}")
            return {}
        return {}

    def set_fan_mode(self, canonical_mode):
        """Set fan mode. canonical_mode is one of DRIVE_FAN_MODE_OPTIONS.
        Tries multiple payload shapes since firmware varies."""
        if canonical_mode not in DRIVE_FAN_MODE_API_VALUES:
            raise UnifiAPIError(f"Unknown fan mode: {canonical_mode}")

        # Try the canonical values for this mode in order. memphi2 documents
        # several spellings (e.g. "default", "default_profile", "balance",
        # "balanced") that different firmware versions accept.
        api_values = DRIVE_FAN_MODE_API_VALUES[canonical_mode]

        # Payload shapes: bare {"profile": value} OR {"profile": value, "apply": True}
        last_error = None
        for api_value in api_values:
            payload_variants = [
                {"profile": api_value},
                {"profile": api_value, "apply": True},
                {"mode":    api_value},
                {"mode":    api_value, "apply": True},
            ]
            if self._fan_mode_write_payload_hint == "profile_apply":
                # Promote the apply-variant to the front
                payload_variants = [
                    {"profile": api_value, "apply": True},
                    {"profile": api_value},
                    {"mode":    api_value, "apply": True},
                    {"mode":    api_value},
                ]
            for payload in payload_variants:
                if self.debug and self.logger:
                    self.logger.debug(f"set_fan_mode trying PUT {DRIVE_FAN_CONTROL_PATH} "
                                      f"payload={payload}")
                try:
                    result = self._request("PUT", DRIVE_FAN_CONTROL_PATH, data=payload)
                    if self.debug and self.logger:
                        self.logger.debug(f"set_fan_mode response: {result!r}")
                    # Cache which shape worked
                    self._fan_mode_write_payload_hint = (
                        "profile_apply" if "apply" in payload else "profile_only"
                    )
                    return canonical_mode
                except UnifiAPIError as e:
                    last_error = e
                    if self.debug and self.logger:
                        self.logger.debug(f"set_fan_mode variant failed: {e}")
                    # 404/405 means endpoint not supported at all; abort.
                    msg = str(e)
                    if "HTTP 404" in msg or "HTTP 405" in msg:
                        raise

        raise UnifiAPIError(f"Fan mode set failed for all payload variants: {last_error}")

    # ------------------------------------------------------------------
    # Public API — Power actions
    # ------------------------------------------------------------------

    def reboot(self):
        """Reboot the UNAS appliance."""
        status, body = self._request_raw("POST", DRIVE_REBOOT_PATH)
        if status and (status < 200 or status >= 300):
            raise UnifiAPIError(f"Reboot failed: HTTP {status}: {body}")
        return True

    def shutdown(self):
        """Power off the UNAS appliance."""
        status, body = self._request_raw("POST", DRIVE_POWEROFF_PATH)
        if status and (status < 200 or status >= 300):
            raise UnifiAPIError(f"Shutdown failed: HTTP {status}: {body}")
        return True

    # ------------------------------------------------------------------
    # Public API — Updates
    # ------------------------------------------------------------------

    def get_firmware_update_info(self):
        """Get firmware update status (if available)."""
        try:
            result = self._request("GET", DRIVE_FIRMWARE_UPDATE_PATH)
            if isinstance(result, dict):
                return result.get("data", result)
        except UnifiAPIError:
            return {}
        return {}

    def get_app_update_info(self):
        """Get Drive application update status (if available)."""
        try:
            result = self._request("GET", DRIVE_APP_UPDATE_PATH)
            if isinstance(result, dict):
                return result.get("data", result)
        except UnifiAPIError:
            return {}
        return {}

    # ------------------------------------------------------------------
    # Connectivity test
    # ------------------------------------------------------------------

    def test_connection(self):
        """Test by fetching storage info."""
        return self.get_storage_info()


# ============================================================================
# UniFi Drive SSH client
# ----------------------------------------------------------------------------
# The UniFi REST API for fan control is read-only on current firmware (writes
# return HTTP 500). To actually change fan speeds the plugin SSHs into the
# UNAS and manages a small fan control service deployed there.
#
# Uses paramiko (added to requirements.txt). All operations are best-effort:
# any failure raises UnifiSSHError so the caller can surface it to the user.
# ============================================================================

class UnifiSSHError(Exception):
    """SSH operation failed."""


# Path constants on the UNAS — what we deploy and where.
_SSH_SCRIPT_PATH    = "/usr/local/sbin/indigo_fan_control.sh"
_SSH_SERVICE_PATH   = "/etc/systemd/system/indigo_fan_control.service"
_SSH_CONF_PATH      = "/etc/indigo_fan_control.conf"
_SSH_SERVICE_NAME   = "indigo_fan_control.service"

# Marker file used to detect whether the service has been deployed at all.
_SSH_MARKER_PATH    = "/etc/indigo_fan_control.deployed"


class UnifiDriveSSH:
    """SSH client wrapping paramiko for UNAS fan control management."""

    def __init__(self, host, port, username, password, logger=None, debug=False):
        self.host     = host
        self.port     = int(port) if port else 22
        self.username = username
        self.password = password
        self.logger   = logger
        self.debug    = debug
        self._client  = None
        self._paramiko_import_error = None

        try:
            import paramiko  # noqa: F401
            self._paramiko = paramiko
        except ImportError as e:
            self._paramiko = None
            self._paramiko_import_error = str(e)

    def _ensure_paramiko(self):
        if self._paramiko is None:
            raise UnifiSSHError(
                f"paramiko library not available: {self._paramiko_import_error}. "
                f"The plugin's requirements.txt declares it; ensure Indigo has "
                f"installed plugin dependencies."
            )

    def _connect(self):
        self._ensure_paramiko()
        if self._client is not None:
            # Test it's still alive
            try:
                transport = self._client.get_transport()
                if transport is not None and transport.is_active():
                    return self._client
            except Exception:
                pass
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None

        client = self._paramiko.SSHClient()
        client.set_missing_host_key_policy(self._paramiko.AutoAddPolicy())
        try:
            client.connect(
                hostname=self.host,
                port=self.port,
                username=self.username,
                password=self.password,
                timeout=15,
                allow_agent=False,
                look_for_keys=False,
            )
        except Exception as e:
            raise UnifiSSHError(f"SSH connect to {self.host}:{self.port} failed: {e}")
        self._client = client
        return client

    def close(self):
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None

    def run(self, command, timeout=30, check=True):
        """Run a shell command on the UNAS. Returns (exit_code, stdout, stderr)."""
        client = self._connect()
        try:
            stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
            exit_code = stdout.channel.recv_exit_status()
            out = stdout.read().decode("utf-8", errors="replace")
            err = stderr.read().decode("utf-8", errors="replace")
            if self.debug and self.logger:
                self.logger.debug(
                    f"SSH `{command}` -> exit={exit_code} "
                    f"stdout={out[:200]!r} stderr={err[:200]!r}"
                )
            if check and exit_code != 0:
                raise UnifiSSHError(
                    f"Command failed (exit {exit_code}): {command}\n"
                    f"stdout: {out.strip()}\nstderr: {err.strip()}"
                )
            return exit_code, out, err
        except UnifiSSHError:
            raise
        except Exception as e:
            raise UnifiSSHError(f"SSH command error: {e}")

    def write_file(self, remote_path, content, mode=0o755):
        """Upload `content` (string) to remote_path on the UNAS using SFTP.
        Sets the given file mode on the remote file."""
        client = self._connect()
        sftp = None
        try:
            sftp = client.open_sftp()
            with sftp.file(remote_path, "wb") as f:
                f.write(content.encode("utf-8") if isinstance(content, str) else content)
            try:
                sftp.chmod(remote_path, mode)
            except Exception:
                pass
            if self.debug and self.logger:
                self.logger.debug(f"SSH wrote {remote_path} ({len(content)} bytes, mode {oct(mode)})")
        except Exception as e:
            raise UnifiSSHError(f"SFTP write to {remote_path} failed: {e}")
        finally:
            if sftp is not None:
                try: sftp.close()
                except Exception: pass

    def remove_file(self, remote_path):
        """Best-effort delete of a remote file. Doesn't raise if missing."""
        try:
            self.run(f"rm -f {remote_path}", check=False)
        except UnifiSSHError:
            pass

    # ------------------------------------------------------------------
    # High-level operations
    # ------------------------------------------------------------------

    def deploy_fan_control(self, script_content, service_content):
        """Install the fan control script and systemd service. Idempotent —
        running again upgrades to the latest script content. Sets initial
        mode to 'default' so behaviour matches pre-deploy until the user
        explicitly chooses a mode."""
        # Upload script and service
        self.write_file(_SSH_SCRIPT_PATH,  script_content,  mode=0o755)
        self.write_file(_SSH_SERVICE_PATH, service_content, mode=0o644)

        # Ensure a conf file exists with a safe default
        conf_check_exit, _, _ = self.run(f"test -f {_SSH_CONF_PATH}", check=False)
        if conf_check_exit != 0:
            self.write_file(_SSH_CONF_PATH, "MODE=default\n", mode=0o644)

        # Drop the deployment marker
        self.write_file(_SSH_MARKER_PATH, "deployed by Indigo UniFi plugin\n", mode=0o644)

        # Reload systemd, enable + restart service
        self.run("systemctl daemon-reload")
        self.run(f"systemctl enable {_SSH_SERVICE_NAME}", check=False)
        self.run(f"systemctl restart {_SSH_SERVICE_NAME}")

    def remove_fan_control(self):
        """Uninstall: stop+disable service, remove all deployed files. After
        this the UNAS firmware's native fan controller takes over again."""
        self.run(f"systemctl stop {_SSH_SERVICE_NAME}", check=False)
        self.run(f"systemctl disable {_SSH_SERVICE_NAME}", check=False)
        self.remove_file(_SSH_SERVICE_PATH)
        self.remove_file(_SSH_SCRIPT_PATH)
        self.remove_file(_SSH_CONF_PATH)
        self.remove_file(_SSH_MARKER_PATH)
        self.run("systemctl daemon-reload", check=False)

    def set_mode(self, mode):
        """Set the active fan control mode. `mode` is one of:
        quiet, balance, cooling, default."""
        mode = (mode or "").strip().lower()
        if mode not in ("quiet", "balance", "cooling", "default"):
            raise UnifiSSHError(f"Unknown fan mode: {mode}")
        self.write_file(_SSH_CONF_PATH, f"MODE={mode}\n", mode=0o644)
        # Restart the service so the change takes effect immediately rather
        # than waiting up to 60s for the next loop iteration.
        self.run(f"systemctl restart {_SSH_SERVICE_NAME}", check=False)

    def is_deployed(self):
        """True if the fan control marker, service unit, AND script file are
        all present.

        Previously only checked the marker + service unit, not the script
        file itself - meaning if the script ever went missing (observed in
        practice: systemd crash-looping every ~10s, "Unable to locate
        executable ... No such file or directory", restart counter climbing
        indefinitely) while the marker and unit file were untouched, this
        would still report "deployed" and the daily auto-redeploy check
        would never fire to fix it. The marker file is really only
        meaningful as a record of "we deployed this," not proof that what
        we deployed is still there."""
        try:
            exit_code, _, _ = self.run(
                f"test -f {_SSH_MARKER_PATH} && test -f {_SSH_SERVICE_PATH} "
                f"&& test -f {_SSH_SCRIPT_PATH}",
                check=False,
            )
            return exit_code == 0
        except UnifiSSHError:
            return False

    def get_active_mode(self):
        """Read the current MODE from the remote conf file. Returns 'default'
        if not deployed or unreadable."""
        try:
            exit_code, out, _ = self.run(
                f"cat {_SSH_CONF_PATH} 2>/dev/null || echo MODE=default",
                check=False,
            )
            for line in (out or "").splitlines():
                line = line.strip()
                if line.startswith("MODE="):
                    val = line.split("=", 1)[1].strip().strip('"').strip("'").lower()
                    return val or "default"
            return "default"
        except UnifiSSHError:
            return "default"

    def get_fan_rpms(self):
        """Read actual measured fan RPM via the hwmon tachometer inputs.

        Confirmed present on this hardware (not assumed): fan1_input/
        fan2_input return real, plausible RPM values (e.g. 6716/6553),
        fan3_input/fan4_input read 0 - likely unpopulated headers on this
        board, not a read failure, since the files exist and return a
        valid (zero) number rather than erroring. The REST fan-control
        endpoint has no RPM field at all (confirmed against a real
        payload), so this SSH-based reading is the only source for this.

        Returns a dict of {hwmon_path: rpm_int} for every fan*_input file
        found - empty dict if none exist or SSH fails, so callers should
        treat this as best-effort, same as the rest of SSH fan control.
        """
        try:
            _, out, _ = self.run(
                "for f in /sys/class/hwmon/hwmon*/fan*_input; do "
                "  [ -e \"$f\" ] && echo \"$f $(cat \"$f\")\"; done",
                check=False, timeout=10,
            )
        except UnifiSSHError:
            return {}

        rpms = {}
        for line in (out or "").splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.rsplit(" ", 1)
            if len(parts) != 2:
                continue
            path, value = parts
            try:
                rpms[path] = int(value)
            except ValueError:
                continue
        return rpms

    def get_service_status(self):
        """Returns 'active', 'inactive', 'failed', or 'unknown'."""
        try:
            _, out, _ = self.run(
                f"systemctl is-active {_SSH_SERVICE_NAME} 2>/dev/null || echo unknown",
                check=False,
            )
            return (out or "").strip() or "unknown"
        except UnifiSSHError:
            return "unknown"

    def run_diagnostic(self):
        """Run the fan control script once (not as service) to see what
        happens. Returns a multi-line string summarising:
          - one-shot script output (shows PWM writes and current temps)
          - service active state
          - last 30 journal lines for the service
          - current PWM values reported by sysfs
        """
        out_lines = []

        # 1. One-shot script run (logs all PWM writes and read-backs)
        try:
            exit_code, stdout, stderr = self.run(
                f"{_SSH_SCRIPT_PATH} 2>&1", check=False, timeout=30,
            )
            out_lines.append("=== One-shot script output ===")
            out_lines.append(stdout.strip() or "(no output)")
            if stderr.strip():
                out_lines.append(f"stderr: {stderr.strip()}")
            out_lines.append(f"exit code: {exit_code}")
        except UnifiSSHError as e:
            out_lines.append(f"One-shot script run failed: {e}")

        # 2. Service state
        try:
            status = self.get_service_status()
            out_lines.append(f"\n=== Service status: {status} ===")
        except Exception:
            pass

        # 3. Recent journal
        try:
            _, out, _ = self.run(
                f"journalctl -u {_SSH_SERVICE_NAME} -n 30 --no-pager 2>&1 || echo '(journalctl unavailable)'",
                check=False, timeout=15,
            )
            out_lines.append("\n=== Last 30 journal lines ===")
            out_lines.append((out or "(empty)").strip())
        except UnifiSSHError as e:
            out_lines.append(f"Journal fetch failed: {e}")

        # 4. Current PWM values
        try:
            _, out, _ = self.run(
                "for f in /sys/class/hwmon/hwmon0/pwm[1-4]; do "
                "  [ -e \"$f\" ] && echo \"$f = $(cat \"$f\")\"; done",
                check=False, timeout=10,
            )
            out_lines.append("\n=== Current PWM register values ===")
            out_lines.append((out or "(none found)").strip())
        except UnifiSSHError as e:
            out_lines.append(f"PWM read failed: {e}")

        # 4b. Fan tachometer (RPM) readback, if present. Standard Linux
        # hwmon convention pairs each pwmN (write, controls speed) with a
        # fanN_input (read, actual measured RPM from the tachometer) in the
        # same directory - but this is hardware/driver-specific, so check
        # directly rather than assume it exists just because pwm[1-4] does.
        # Listing every numeric hwmon*/fan*_input rather than hardcoding
        # hwmon0 in case the tachometer inputs live under a different
        # hwmon index than the PWM controls on this board.
        try:
            _, out, _ = self.run(
                "for f in /sys/class/hwmon/hwmon*/fan*_input; do "
                "  [ -e \"$f\" ] && echo \"$f = $(cat \"$f\")\"; done",
                check=False, timeout=10,
            )
            out_lines.append("\n=== Fan tachometer (RPM) readback ===")
            out_lines.append((out or "(none found - this board may not expose fan RPM via hwmon)").strip())
        except UnifiSSHError as e:
            out_lines.append(f"Fan tachometer read failed: {e}")

        # 5. Current mode in conf
        try:
            _, out, _ = self.run(
                f"cat {_SSH_CONF_PATH} 2>/dev/null || echo '(no conf file)'",
                check=False, timeout=5,
            )
            out_lines.append(f"\n=== Conf file ===")
            out_lines.append((out or "").strip())
        except UnifiSSHError:
            pass

        return "\n".join(out_lines)


# ============================================================================
# WebSocket client
# ----------------------------------------------------------------------------
# Lightweight raw WebSocket client (no external deps, plain urllib + ssl
# socket). Supports cookie auth (Network legacy WS) and Bearer auth (Protect
# integration / Access developer WS). Exponential backoff on reconnect.
# ============================================================================

class UnifiWebSocket:
    """Minimal WebSocket client with auto-reconnect and exponential backoff."""

    def __init__(self, host, port, ws_path, ssl_context, on_message, logger,
                 cookies=None, bearer_token=None, api_key=None, debug=False, label="ws",
                 on_auth_failure=None, binary_passthrough=False):
        self.host         = host
        self.port         = int(port)
        self.ws_path      = ws_path
        self.ssl_context  = ssl_context
        self.on_message   = on_message
        self.logger       = logger
        self.cookies      = cookies          # CookieJar or None
        self.bearer_token = bearer_token     # str or None - sets Authorization: Bearer
        self.api_key      = api_key          # str or None - sets X-API-KEY header
        self.debug        = debug
        self.label        = label
        # When True, binary (opcode 0x2) frames are handed to on_message as
        # raw bytes instead of being UTF-8/JSON-decoded first. For
        # connections that speak a custom binary protocol rather than plain
        # JSON text - specifically Protect's private update channel, which
        # uses UniFi's own two-frame binary packet format (see
        # _decode_protect_private_frame). Text (opcode 0x1) frames are
        # unaffected either way.
        self.binary_passthrough = binary_passthrough
        # Optional zero-arg callback invoked when the handshake is rejected
        # as unauthorized (401/403). For legacy cookie auth, this is the hook
        # to force a fresh login before the next reconnect attempt picks up
        # the refreshed cookies (self.cookies is a live CookieJar reference,
        # so it reflects the refresh automatically). Leave unset for auth
        # schemes with no "log in again" concept (Bearer token / API key) -
        # those fall back to treating 401/403 as a permanent failure, same
        # as before this option existed.
        self.on_auth_failure = on_auth_failure

        self.thread       = None
        self.stop_flag    = False
        self.connected    = False
        self._clean_close = False  # set True by _read_frames when the
                                   # server (not an error) ended the
                                   # connection; tells _loop to skip the
                                   # failure-oriented backoff delay
        self._backoff     = WS_RECONNECT_INITIAL_SEC

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self):
        self.stop_flag = False
        self.thread    = threading.Thread(target=self._loop, daemon=True,
                                          name=f"unifi-ws-{self.label}")
        self.thread.start()

    def stop(self):
        self.stop_flag = True
        if self.thread:
            self.thread.join(timeout=5)

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def _loop(self):
        while not self.stop_flag:
            sock = None
            self._clean_close = False
            try:
                sock = self._connect_and_handshake()
                self.connected = True
                self._backoff  = WS_RECONNECT_INITIAL_SEC  # reset on success

                if self.debug:
                    self.logger.debug(f"[{self.label}] WebSocket connected")

                self._read_frames(sock)
            except _PermanentWSFailure as e:
                # Service not available / 502 / 503 / 404 - stop trying
                if self.debug:
                    self.logger.debug(f"[{self.label}] WebSocket permanently unavailable: {e}")
                self.stop_flag = True
                break
            except _WSAuthFailure as e:
                # 401/403 on handshake. If we have a way to self-heal (e.g.
                # force a fresh legacy login), try it, then treat this like
                # any other transient failure - back off and retry. With no
                # self-heal hook available, there's nothing we can do about a
                # rejected credential ourselves, so preserve the old
                # behaviour and give up for this run.
                if self.on_auth_failure is not None:
                    if self.debug:
                        self.logger.debug(f"[{self.label}] WebSocket unauthorized ({e}), "
                                          f"attempting to refresh auth before retrying")
                    try:
                        self.on_auth_failure()
                    except Exception as refresh_err:
                        if self.debug:
                            self.logger.debug(f"[{self.label}] Auth refresh failed: {refresh_err}")
                else:
                    if self.debug:
                        self.logger.debug(f"[{self.label}] WebSocket unauthorized ({e}), "
                                          f"no refresh available - giving up")
                    self.stop_flag = True
                    break
            except Exception as e:
                if not self.stop_flag:
                    if self.debug:
                        self.logger.debug(f"[{self.label}] WebSocket error: {e}")
            finally:
                self.connected = False
                if sock is not None:
                    try:
                        sock.close()
                    except Exception:
                        pass

            if self.stop_flag:
                break

            if self._clean_close:
                # The server ended this one deliberately - not a failure,
                # so don't make it wait through the same backoff meant for
                # actual connectivity problems. A short fixed delay (rather
                # than reconnecting instantly) just guards against a tight
                # loop in the unlikely case the server closes every new
                # connection immediately. Deliberately does NOT touch
                # self._backoff - a genuine failure right after a clean
                # close should still back off from a clean slate, not from
                # wherever backoff happened to be.
                time.sleep(1)
            else:
                # Exponential backoff
                time.sleep(self._backoff)
                self._backoff = min(self._backoff * WS_RECONNECT_BACKOFF_FACTOR,
                                    WS_RECONNECT_MAX_SEC)

    # ------------------------------------------------------------------
    # Connection / handshake
    # ------------------------------------------------------------------

    def _connect_and_handshake(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(10)
        wrapped = self.ssl_context.wrap_socket(sock, server_hostname=self.host)
        wrapped.connect((self.host, self.port))

        ws_key = base64.b64encode(os.urandom(16)).decode("ascii")

        request_lines = [
            f"GET {self.ws_path} HTTP/1.1",
            f"Host: {self.host}:{self.port}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {ws_key}",
            "Sec-WebSocket-Version: 13",
            f"Origin: https://{self.host}:{self.port}",
        ]
        if self.bearer_token:
            request_lines.append(f"Authorization: Bearer {self.bearer_token}")
        if self.api_key:
            request_lines.append(f"X-API-KEY: {self.api_key}")
        if self.cookies is not None:
            cookie_header = "; ".join(f"{c.name}={c.value}" for c in self.cookies)
            if cookie_header:
                request_lines.append(f"Cookie: {cookie_header}")

        request = ("\r\n".join(request_lines) + "\r\n\r\n").encode("ascii")
        wrapped.send(request)

        # Read upgrade response
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = wrapped.recv(1024)
            if not chunk:
                raise UnifiAPIError("WebSocket: connection closed during handshake")
            buf += chunk
            if len(buf) > 16384:
                raise UnifiAPIError("WebSocket: handshake response too large")

        status_line = buf.split(b"\r\n", 1)[0].decode("ascii", errors="replace")
        if " 101 " not in status_line and not status_line.endswith(" 101"):
            # 404/502/503: the endpoint doesn't exist or the service is down.
            # Retrying won't change either outcome - genuinely permanent.
            if "502" in status_line or "503" in status_line or "404" in status_line:
                raise _PermanentWSFailure(status_line)
            # 401/403: rejected credentials. For session-based (cookie) auth
            # this commonly just means the session expired - potentially
            # self-healing, not necessarily permanent. See _WSAuthFailure.
            if "401" in status_line or "403" in status_line:
                raise _WSAuthFailure(f"auth rejected ({status_line})")
            raise UnifiAPIError(f"WebSocket handshake failed: {status_line}")

        # Switch to longer timeout for steady-state reads
        wrapped.settimeout(60)
        return wrapped

    # ------------------------------------------------------------------
    # Frame reading
    # ------------------------------------------------------------------

    def _read_frames(self, sock):
        # Counts consecutive read-timeout cycles with NO frame received at
        # all (not even a pong). A healthy connection resets this to 0 the
        # moment any frame arrives. A connection that's gone silently dead
        # (half-open TCP: the far end stopped responding but never sent a
        # close/RST) will just keep timing out forever otherwise - we'd send
        # pings into the void indefinitely and never notice. After
        # WS_MAX_MISSED_PINGS consecutive silent cycles, give up on this
        # socket and let the outer loop reconnect fresh.
        missed_pings = 0

        while not self.stop_flag:
            try:
                payload, opcode = self._read_frame(sock)
            except socket.timeout:
                missed_pings += 1
                if missed_pings > WS_MAX_MISSED_PINGS:
                    if self.debug:
                        self.logger.debug(
                            f"[{self.label}] No response at all for "
                            f"{missed_pings} consecutive cycles - connection "
                            f"appears dead, forcing reconnect"
                        )
                    return
                self._send_ping(sock)
                continue
            except Exception as e:
                if self.debug:
                    self.logger.debug(f"[{self.label}] frame read error: {e}")
                return

            # Any successful frame read - data, ping, or pong - proves the
            # connection is still alive.
            missed_pings = 0

            if payload is None:
                # The server sent a proper WebSocket close frame - it ended
                # this connection deliberately, not a failure on either
                # side. Observed in practice on Protect's "devices"
                # subscription specifically, recurring every ~1-2 minutes
                # with no preceding error of any kind - looks like a
                # deliberate server-side policy for that endpoint (a max
                # connection lifetime, resource cycling, or similar), not
                # anything wrong with us. Previously this returned with no
                # logging at all, indistinguishable from a real failure
                # without exactly this kind of log correlation exercise.
                # self._clean_close tells _loop not to apply the backoff
                # meant for genuine connectivity problems - there's no
                # reason to delay reconnecting just because the server
                # asked us to.
                if self.debug:
                    self.logger.debug(f"[{self.label}] Server closed the connection (clean close)")
                self._clean_close = True
                return

            if opcode == 0x9:   # ping -> pong
                self._send_pong(sock, payload)
                continue
            if opcode == 0xA:   # pong -> ignore
                continue
            if opcode not in (0x1, 0x2):  # only text/binary carry data
                continue

            if self.binary_passthrough and opcode == 0x2:
                # Raw bytes straight to the caller - this connection speaks
                # a custom binary protocol, not JSON text.
                msg = payload
            else:
                try:
                    text = payload.decode("utf-8")
                    msg  = json.loads(text)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    # Not JSON - skip
                    continue

            try:
                self.on_message(msg)
            except Exception as e:
                if self.debug:
                    self.logger.debug(f"[{self.label}] callback error: {e}")

    def _read_frame(self, sock):
        header = self._recv_exact(sock, 2)
        if header is None:
            return None, 0x8

        b1, b2 = header[0], header[1]
        opcode = b1 & 0x0F
        masked = (b2 & 0x80) != 0
        plen   = b2 & 0x7F

        if plen == 126:
            ext = self._recv_exact(sock, 2)
            plen = struct.unpack(">H", ext)[0]
        elif plen == 127:
            ext = self._recv_exact(sock, 8)
            plen = struct.unpack(">Q", ext)[0]

        mask_key = self._recv_exact(sock, 4) if masked else None

        payload = b""
        if plen > 0:
            payload = self._recv_exact(sock, plen) or b""
            if masked and mask_key:
                payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))

        if opcode == 0x8:
            return None, 0x8
        return payload, opcode

    def _recv_exact(self, sock, n):
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                return None
            buf += chunk
        return buf

    def _send_ping(self, sock):
        try:
            sock.send(struct.pack("BB", 0x89, 0))
        except Exception:
            pass

    def _send_pong(self, sock, payload):
        try:
            length = len(payload) if payload else 0
            if length < 126:
                header = struct.pack("BB", 0x8A, length)
            elif length <= 0xFFFF:
                header = struct.pack("BBH", 0x8A, 126, length)
            else:
                header = struct.pack("BBQ", 0x8A, 127, length)
            sock.send(header + (payload or b""))
        except Exception:
            pass


class _PermanentWSFailure(Exception):
    """Raised internally when WebSocket should stop trying to reconnect
    (handshake rejected with 404/502/503 - the endpoint genuinely isn't
    there or the service is down; retrying won't fix either)."""


class _WSAuthFailure(Exception):
    """Raised internally when a WebSocket handshake is rejected as
    unauthorized (401/403). Unlike _PermanentWSFailure, this is NOT always a
    dead end: for legacy cookie-session auth, a 401 usually just means the
    session expired and a fresh login will fix it. The WS loop calls
    on_auth_failure (if the caller supplied one) to attempt that, then
    keeps retrying with normal backoff. Callers that can't self-heal (Bearer
    token / API key auth, where there's no login flow to refresh) simply
    don't pass on_auth_failure, and this falls back to the old permanent
    behaviour."""


# ============================================================================
# Plugin class
# ============================================================================

class Plugin(indigo.PluginBase):

    def __init__(self, pluginId, pluginDisplayName, pluginVersion, pluginPrefs):
        super(Plugin, self).__init__(pluginId, pluginDisplayName, pluginVersion, pluginPrefs)
        self.debug             = pluginPrefs.get("showDebugInfo", False)

        # Per-controller state. See controllers dict structure below.
        self.controllers       = {}

        # Track deleted client MACs to prevent auto-recreation
        self.deleted_clients   = set(pluginPrefs.get("deletedClients", []))

        self.polling_thread    = None
        self.stop_polling      = False

        # Load embedded fan control assets that the plugin deploys to UNAS
        # over SSH. These ship inside the plugin bundle, next to plugin.py.
        plugin_dir = self._find_plugin_dir()
        self.fan_script_content  = self._load_asset(plugin_dir, "indigo_fan_control.sh")
        self.fan_service_content = self._load_asset(plugin_dir, "indigo_fan_control.service")

    def _find_plugin_dir(self):
        """Locate the directory that contains plugin.py and the bundled assets.

        Indigo's plugin host loads plugin.py via exec() so `__file__` is NOT
        defined in the module's globals. We fall back to multiple strategies:
          1. `__file__` (if a future Indigo sets it)
          2. CWD (Indigo sets working directory to the Server Plugin folder)
          3. Standard Indigo plugin install location
        """
        # Strategy 1: __file__ if defined
        try:
            return os.path.dirname(os.path.abspath(__file__))  # noqa: F821
        except NameError:
            pass

        # Strategy 2: current working directory
        cwd = os.getcwd()
        if os.path.exists(os.path.join(cwd, "indigo_fan_control.sh")):
            return cwd

        # Strategy 3: standard Indigo install path for this plugin
        try:
            plugin_id = self.pluginId  # e.g. "com.durosity.indigoplugin.unifi"
        except Exception:
            plugin_id = "com.durosity.indigoplugin.unifi"
        # Indigo plugins live at:
        #   /Library/Application Support/Perceptive Automation/Indigo <ver>/Plugins/<Name>.indigoPlugin/Contents/Server Plugin/
        candidates = [
            f"/Library/Application Support/Perceptive Automation/Indigo 2025.2/Plugins/UniFi.indigoPlugin/Contents/Server Plugin",
            f"/Library/Application Support/Perceptive Automation/Indigo 2024.2/Plugins/UniFi.indigoPlugin/Contents/Server Plugin",
            f"/Library/Application Support/Perceptive Automation/Indigo 2024.1/Plugins/UniFi.indigoPlugin/Contents/Server Plugin",
            f"/Library/Application Support/Perceptive Automation/Indigo 2023.2/Plugins/UniFi.indigoPlugin/Contents/Server Plugin",
            f"/Library/Application Support/Perceptive Automation/Indigo 2022.2/Plugins/UniFi.indigoPlugin/Contents/Server Plugin",
        ]
        for c in candidates:
            if os.path.exists(os.path.join(c, "indigo_fan_control.sh")):
                return c

        # Last resort — return CWD even though we couldn't verify
        return cwd

    def _load_asset(self, plugin_dir, filename):
        """Load a bundled asset file. Returns empty string on failure."""
        try:
            with open(os.path.join(plugin_dir, filename), "r", encoding="utf-8") as f:
                return f.read()
        except Exception as e:
            # Don't crash plugin startup over an asset issue — log and continue.
            try:
                self.logger.error(f"Could not load asset {filename}: {e}")
            except Exception:
                pass
            return ""

    # ------------------------------------------------------------------
    # Plugin lifecycle
    # ------------------------------------------------------------------

    def startup(self):
        self.logger.info(f"Starting UniFi Plugin v{PLUGIN_VERSION}")
        self.stop_polling   = False
        self.polling_thread = threading.Thread(target=self._polling_loop, daemon=True,
                                               name="unifi-poll")
        self.polling_thread.start()

    def shutdown(self):
        self.logger.info("Shutting down UniFi Plugin")
        self.stop_polling = True
        if self.polling_thread:
            self.polling_thread.join(timeout=5)

        for controller_id in list(self.controllers.keys()):
            self._stop_controller(controller_id)
        self.controllers.clear()

    def closedPrefsConfigUi(self, valuesDict, userCancelled):
        if userCancelled:
            return True
        self.debug = valuesDict.get("showDebugInfo", False)
        self.logger.info(f"Debug mode: {'enabled' if self.debug else 'disabled'}")
        return True

    # ------------------------------------------------------------------
    # Trigger plumbing
    # ------------------------------------------------------------------

    def triggerStartProcessing(self, trigger):
        if self.debug:
            self.logger.debug(f"Trigger started: {trigger.name} ({trigger.pluginTypeId})")

    def triggerStopProcessing(self, trigger):
        if self.debug:
            self.logger.debug(f"Trigger stopped: {trigger.name}")

    def _fire_trigger(self, event_id, device):
        """Fire any user triggers matching this event/device combination."""
        for trigger in indigo.triggers.iter("self"):
            if trigger.pluginTypeId != event_id:
                continue
            # Support both 'device' and 'deviceId' field names for compatibility
            device_filter = trigger.pluginProps.get("device", "") or \
                            trigger.pluginProps.get("deviceId", "")
            # Empty = "Any device"
            if device_filter == "" or device_filter == str(device.id):
                if self.debug:
                    self.logger.debug(f"Firing trigger '{trigger.name}' for {device.name}")
                indigo.trigger.execute(trigger)

    # ------------------------------------------------------------------
    # Device lifecycle
    # ------------------------------------------------------------------

    def deviceStartComm(self, dev):
        # Indigo caches each device's actual state list at creation time -
        # editing Devices.xml to add new states later does NOT retroactively
        # apply to devices that already exist. Without this call, writing to
        # a state that's new in a later plugin version fails with "state key
        # X not defined (ignoring update request)" on any device created
        # before that version shipped, forever, until the device is deleted
        # and recreated. Calling this here refreshes the existing device's
        # state list to match the current Devices.xml on every plugin start
        # (and whenever the device is re-enabled), so new states added in an
        # update just start working - no manual device recreation needed.
        # Cheap and safe to call unconditionally; it's a no-op if nothing
        # changed.
        try:
            dev.stateListOrDisplayStateIdChanged()
        except Exception as e:
            if self.debug:
                self.logger.debug(f"{dev.name}: stateListOrDisplayStateIdChanged() failed: {e}")

        type_id = dev.deviceTypeId
        if type_id == "unifiController":
            self._start_controller(dev)
        elif type_id == "unifiDriveController":
            self._start_drive_controller(dev)
        elif type_id == "protectCamera":
            self._setup_camera_link(dev)
        elif type_id == "protectSensor":
            self._setup_sensor_link(dev)
        elif type_id == "networkDevice":
            self._setup_netdev_link(dev)
        elif type_id == "unifiPort":
            self._setup_port_link(dev)
        elif type_id == "clientDevice":
            self._setup_client_link(dev)
        elif type_id == "accessDoor":
            self._setup_door_link(dev)
        elif type_id == "firewallPolicy":
            self._setup_firewallpolicy_link(dev)
        elif type_id == "trafficRoute":
            self._setup_trafficroute_link(dev)
        elif type_id == "portForward":
            self._setup_portforward_link(dev)
        elif type_id == "wlanDevice":
            self._setup_wlan_link(dev)
        elif type_id == "alarmProfile":
            self._setup_alarmprofile_link(dev)
        elif type_id == "protectPackageCamera":
            self._setup_packagecam_link(dev)
        elif type_id == "driveFan":
            self._setup_fan_link(dev)
        elif type_id == "unifiDrivePool":
            self._setup_pool_link(dev)
        elif type_id == "unifiDriveDisk":
            self._setup_disk_link(dev)

    def deviceStopComm(self, dev):
        if dev.deviceTypeId in ("unifiController", "unifiDriveController"):
            self._stop_controller(dev.id)

    # ------------------------------------------------------------------
    # Controller start / stop
    # ------------------------------------------------------------------

    def _start_controller(self, dev):
        """Initialize a controller from its config."""
        try:
            address     = dev.pluginProps.get("address", "").strip()
            port        = int(dev.pluginProps.get("port", DEFAULT_HTTPS_PORT) or DEFAULT_HTTPS_PORT)
            access_port = int(dev.pluginProps.get("accessPort", DEFAULT_ACCESS_PORT) or DEFAULT_ACCESS_PORT)
            verify_ssl  = bool(dev.pluginProps.get("verifySSL", False))
            site        = dev.pluginProps.get("site", "default") or "default"

            # Auth credentials
            network_api_key = dev.pluginProps.get("networkApiKey", "").strip()
            # Network and Protect share one API key via the unified Integrations
            # page (Settings → Control Plane → Integrations) with X-API-KEY auth.
            protect_api_key = network_api_key
            # Access has a SEPARATE token, generated inside the UniFi Access app
            # (not the unified Integrations page). It uses Bearer auth on port 12445.
            access_api_key  = dev.pluginProps.get("accessApiKey", "").strip()
            legacy_user     = dev.pluginProps.get("legacyUsername", "").strip()
            legacy_pass     = dev.pluginProps.get("legacyPassword", "")

            # Feature toggles
            enable_network = bool(dev.pluginProps.get("enableNetwork", True))
            enable_protect = bool(dev.pluginProps.get("enableProtect", True))
            enable_access  = bool(dev.pluginProps.get("enableAccess",  False))

            # Polling settings
            aggressive    = bool(dev.pluginProps.get("aggressivePolling", False))
            try:
                stats_interval = int(dev.pluginProps.get("statsIntervalSec", DEFAULT_STATS_INTERVAL_SEC))
            except (TypeError, ValueError):
                stats_interval = DEFAULT_STATS_INTERVAL_SEC
            try:
                sanity_interval = int(dev.pluginProps.get("sanityIntervalSec", DEFAULT_SANITY_INTERVAL_SEC))
            except (TypeError, ValueError):
                sanity_interval = DEFAULT_SANITY_INTERVAL_SEC

            if not address:
                self.logger.error(f"{dev.name}: Controller address is empty")
                dev.updateStateOnServer("status", "Error: Missing address")
                dev.updateStateOnServer("connected", False)
                return

            self.logger.info(f"{dev.name}: Connecting to {address}:{port}")

            controller = {
                "device":               dev,
                "address":              address,
                "port":                 port,
                "access_port":          access_port,
                "site":                 site,
                "cameras":              {},
                "sensors":              {},
                "lights":               {},
                "chimes":               {},
                "network_devices":      {},
                "ports":                {},
                "clients":              {},
                "doors":                {},
                "door_relock_pending":  {},  # {door_id: {"unlocked_at": ts, "opened": bool}}
                "firewall_policies":   {},
                "traffic_routes":      {},
                "port_forwards":       {},
                "wlans":               {},
                "alarm_profiles":      {},
                "package_cameras":     {},
                "fan_device":           None,
                "stats_interval_sec":   max(60, stats_interval),
                "sanity_interval_sec":  max(0,  sanity_interval),  # 0 = off
                "aggressive":           aggressive,
                "last_stats_at":        0,
                "last_sanity_at":       0,
                "websockets":           [],
            }

            # ---- Network API ----
            if enable_network:
                net_api = UnifiNetworkAPI(
                    address, port, network_api_key,
                    legacy_user, legacy_pass, site=site,
                    verify_ssl=verify_ssl, logger=self.logger, debug=self.debug,
                )
                controller["network_api"] = net_api

                # Try integration first, fall back to legacy if no API key
                tried = []
                if network_api_key:
                    try:
                        net_api.test_integration()
                        self.logger.info(f"{dev.name}: Network Integration API OK")
                    except UnifiAuthError as e:
                        self.logger.warning(f"{dev.name}: Network Integration API key invalid: {e}")
                        tried.append("integration")
                    except UnifiAPIError as e:
                        self.logger.warning(f"{dev.name}: Network Integration API unavailable: {e}")
                        tried.append("integration")

                if legacy_user and legacy_pass:
                    try:
                        net_api.test_legacy()
                        self.logger.info(f"{dev.name}: Network legacy auth OK")
                    except UnifiAuthError as e:
                        self.logger.error(f"{dev.name}: Network legacy auth failed: {e}")
                    except UnifiAPIError as e:
                        self.logger.warning(f"{dev.name}: Network legacy auth unavailable: {e}")

                # Network WebSocket (legacy events stream) - real-time client/device updates
                if legacy_user and legacy_pass:
                    try:
                        ws = UnifiWebSocket(
                            host=address, port=port,
                            ws_path=f"/proxy/network/wss/s/{site}/events",
                            ssl_context=net_api.ssl_context,
                            on_message=lambda msg, cid=dev.id: self._on_network_ws(cid, msg),
                            logger=self.logger,
                            cookies=net_api.legacy_cookies,
                            bearer_token=None,
                            debug=self.debug,
                            label=f"{dev.name}-net",
                            # Legacy cookie sessions expire; a 401 on
                            # reconnect usually just means we need a fresh
                            # login, not that the credentials are wrong. Pass
                            # the bound method directly - net_api.legacy_cookies
                            # is the same CookieJar object the WS reads from,
                            # so a successful re-login is picked up
                            # automatically on the next handshake attempt.
                            on_auth_failure=net_api._legacy_login,
                        )
                        ws.start()
                        controller["websockets"].append(ws)
                    except Exception as e:
                        self.logger.warning(f"{dev.name}: Network WebSocket failed: {e}")

            # ---- Protect API ----
            if enable_protect and protect_api_key:
                try:
                    protect_api = UnifiProtectAPI(
                        address, port, protect_api_key,
                        username=legacy_user, password=legacy_pass,
                        verify_ssl=verify_ssl, logger=self.logger, debug=self.debug,
                    )
                    protect_api.test_connection()
                    protect_api.refresh_cache()
                    controller["protect_api"] = protect_api
                    legacy_note = ""
                    if legacy_user and legacy_pass:
                        legacy_note = (
                            " + legacy bootstrap OK" if protect_api.legacy_logged_in
                            else " (legacy bootstrap unavailable - monitoring-only fields will stay blank)"
                        )
                    self.logger.info(f"{dev.name}: Protect Integration API OK "
                                     f"({len(protect_api.get_cameras())} cameras, "
                                     f"{len(protect_api.get_sensors())} sensors)"
                                     f"{legacy_note}")

                    # Subscribe to device updates and events streams
                    controller["protect_events_ws"] = None
                    for ws_path, handler, label in [
                        (UnifiProtectAPI.WS_DEVICES, self._on_protect_devices_ws, "protect-devices"),
                        (UnifiProtectAPI.WS_EVENTS,  self._on_protect_events_ws,  "protect-events"),
                    ]:
                        ws = UnifiWebSocket(
                            host=address, port=port,
                            ws_path=ws_path,
                            ssl_context=protect_api.ssl_context,
                            on_message=lambda msg, cid=dev.id, h=handler: h(cid, msg),
                            logger=self.logger,
                            cookies=None,
                            api_key=protect_api_key,
                            debug=self.debug,
                            label=f"{dev.name}-{label}",
                        )
                        ws.start()
                        controller["websockets"].append(ws)
                        # Tracked explicitly (same pattern as
                        # protect_private_ws) so eventFeedLive can check
                        # this one specifically, not just "some socket for
                        # this controller is up" - the devices socket
                        # being fine says nothing about whether motion/
                        # audio events are actually arriving.
                        if label == "protect-events":
                            controller["protect_events_ws"] = ws

                    # Optional fast-motion path: the private update channel,
                    # reusing the SAME session the bootstrap login already
                    # established (no separate auth needed). Only attempted
                    # if that login actually succeeded - entirely additive,
                    # motion still works via the official events WS above
                    # if this isn't available or doesn't connect for any
                    # reason (see the live .connected check in
                    # _on_protect_events_ws).
                    controller["protect_private_ws"] = None
                    if protect_api.legacy_logged_in:
                        try:
                            private_ws = UnifiWebSocket(
                                host=address, port=port,
                                ws_path=UnifiProtectAPI.WS_PRIVATE_UPDATES,
                                ssl_context=protect_api.ssl_context,
                                on_message=lambda msg, cid=dev.id: self._on_protect_private_ws(cid, msg),
                                logger=self.logger,
                                cookies=protect_api.legacy_cookies,
                                binary_passthrough=True,
                                debug=self.debug,
                                label=f"{dev.name}-protect-private",
                                on_auth_failure=protect_api._legacy_login,
                            )
                            private_ws.start()
                            controller["websockets"].append(private_ws)
                            controller["protect_private_ws"] = private_ws
                            self.logger.info(f"{dev.name}: Protect fast motion path connected "
                                             f"(private update channel)")
                        except Exception as e:
                            self.logger.warning(f"{dev.name}: Protect fast motion path unavailable "
                                                f"(falling back to official events for motion): {e}")
                except UnifiAuthError as e:
                    self.logger.error(f"{dev.name}: Protect API auth failed: {e}")
                except UnifiAPIError as e:
                    self.logger.warning(f"{dev.name}: Protect API unavailable: {e}")
            elif enable_protect:
                self.logger.warning(f"{dev.name}: Protect enabled but no API key configured")

            # ---- Access API ----
            # Access has a separate API server on its own port (12445), using a
            # separate API token generated from inside the UniFi Access app.
            if enable_access and access_api_key:
                try:
                    access_api = UnifiAccessAPI(
                        address, access_port, access_api_key,
                        verify_ssl=verify_ssl, logger=self.logger, debug=self.debug,
                    )
                    access_api.test_connection()
                    controller["access_api"] = access_api
                    self.logger.info(f"{dev.name}: Access Developer API OK")

                    # Subscribe to door notifications.
                    ws = UnifiWebSocket(
                        host=address, port=access_port,
                        ws_path="/api/v1/developer/devices/notifications",
                        ssl_context=access_api.ssl_context,
                        on_message=lambda msg, cid=dev.id: self._on_access_ws(cid, msg),
                        logger=self.logger,
                        cookies=None,
                        bearer_token=access_api_key,
                        debug=self.debug,
                        label=f"{dev.name}-access",
                    )
                    ws.start()
                    controller["websockets"].append(ws)
                except UnifiAuthError as e:
                    self.logger.error(f"{dev.name}: Access API auth failed: {e}")
                except UnifiAPIError as e:
                    self.logger.warning(f"{dev.name}: Access API unavailable: {e}")
            elif enable_access:
                self.logger.warning(f"{dev.name}: Access enabled but no API key configured")

            # NOTE: Drive (UNAS) is now handled by a separate device type,
            # unifiDriveController, since UNAS is a separate appliance with
            # its own IP and credentials. See _start_drive_controller().

            self.controllers[dev.id] = controller
            dev.updateStateOnServer("status", "Connected")
            dev.updateStateOnServer("connected", True)

            # Trigger initial state population
            controller["last_stats_at"]  = 0
            controller["last_sanity_at"] = 0

            # Set up any devices that were waiting for this controller
            self._setup_waiting_devices(dev.id)
            self.logger.info(f"{dev.name}: Connected")

        except Exception as e:
            self.logger.error(f"{dev.name}: Connection error: {e}")
            if self.debug:
                self.logger.error(traceback.format_exc())
            dev.updateStateOnServer("status", f"Error: {e}")
            dev.updateStateOnServer("connected", False)

    def _stop_controller(self, controller_id):
        controller = self.controllers.get(controller_id)
        if not controller:
            return
        for ws in controller.get("websockets", []):
            try:
                ws.stop()
            except Exception:
                pass
        ssh = controller.get("drive_ssh")
        if ssh is not None:
            try:
                ssh.close()
            except Exception:
                pass
        dev = controller["device"]
        try:
            dev.updateStateOnServer("status", "Disconnected")
            dev.updateStateOnServer("connected", False)
        except Exception:
            pass
        self.controllers.pop(controller_id, None)
        self.logger.info(f"{dev.name}: Disconnected")

    # ------------------------------------------------------------------
    # Drive controller start (separate device type, separate IP)
    # ------------------------------------------------------------------

    def _start_drive_controller(self, dev):
        """Initialize a standalone UNAS controller."""
        try:
            address     = dev.pluginProps.get("address", "").strip()
            try:
                port = int(dev.pluginProps.get("port", DEFAULT_HTTPS_PORT) or DEFAULT_HTTPS_PORT)
            except (TypeError, ValueError):
                port = DEFAULT_HTTPS_PORT
            verify_ssl  = bool(dev.pluginProps.get("verifySSL", False))

            api_key     = dev.pluginProps.get("driveApiKey", "").strip()
            legacy_user = dev.pluginProps.get("legacyUsername", "").strip()
            legacy_pass = dev.pluginProps.get("legacyPassword", "")

            try:
                stats_interval = int(dev.pluginProps.get("statsIntervalSec", DEFAULT_STATS_INTERVAL_SEC))
            except (TypeError, ValueError):
                stats_interval = DEFAULT_STATS_INTERVAL_SEC

            if not address:
                self.logger.error(f"{dev.name}: UNAS address is empty")
                dev.updateStateOnServer("status", "Error: Missing address")
                dev.updateStateOnServer("connected", False)
                return

            if not api_key and not (legacy_user and legacy_pass):
                self.logger.error(f"{dev.name}: Either API key or username/password is required")
                dev.updateStateOnServer("status", "Error: Missing credentials")
                dev.updateStateOnServer("connected", False)
                return

            self.logger.info(f"{dev.name}: Connecting to UNAS at {address}:{port}")

            controller = {
                "device":               dev,
                "address":              address,
                "port":                 port,
                "fan_device":           None,
                "stats_interval_sec":   max(60, stats_interval),
                "sanity_interval_sec":  0,
                "aggressive":           False,
                "last_stats_at":        0,
                "last_sanity_at":       0,
                "websockets":           [],
                # Empty maps so polling/setup code can iterate without checks
                "cameras":              {},
                "sensors":              {},
                "lights":               {},
                "chimes":               {},
                "network_devices":      {},
                "ports":                {},
                "clients":              {},
                "doors":                {},
                "door_relock_pending":  {},  # {door_id: {"unlocked_at": ts, "opened": bool}}
                "firewall_policies":   {},
                "traffic_routes":      {},
                "port_forwards":       {},
                "wlans":               {},
                "alarm_profiles":      {},
                "package_cameras":     {},
                "is_drive_controller":  True,
            }

            try:
                drive_api = UnifiDriveAPI(
                    address, port, api_key=api_key,
                    username=legacy_user, password=legacy_pass,
                    verify_ssl=verify_ssl,
                    logger=self.logger, debug=self.debug,
                )
                drive_api.test_connection()
                controller["drive_api"] = drive_api
                auth_label = "API key" if api_key else "legacy auth"
                self.logger.info(f"{dev.name}: UNAS OK ({auth_label})")
            except UnifiAuthError as e:
                self.logger.error(f"{dev.name}: UNAS auth failed: {e}")
                dev.updateStateOnServer("status", "Error: Auth failed")
                dev.updateStateOnServer("connected", False)
                return
            except UnifiAPIError as e:
                self.logger.error(f"{dev.name}: UNAS unavailable: {e}")
                dev.updateStateOnServer("status", "Error: Unavailable")
                dev.updateStateOnServer("connected", False)
                return

            self.controllers[dev.id] = controller
            dev.updateStateOnServer("status", "Connected")
            dev.updateStateOnServer("connected", True)

            # Set up SSH client for fan control (if enabled in config)
            ssh_enabled  = bool(dev.pluginProps.get("sshEnabled", False))
            ssh_user     = (dev.pluginProps.get("sshUsername", "") or "").strip()
            ssh_pass     = dev.pluginProps.get("sshPassword", "") or ""
            try:
                ssh_port = int(dev.pluginProps.get("sshPort", "22") or "22")
            except (TypeError, ValueError):
                ssh_port = 22
            if ssh_enabled and ssh_user and ssh_pass:
                ssh = UnifiDriveSSH(
                    address, ssh_port, ssh_user, ssh_pass,
                    logger=self.logger, debug=self.debug,
                )
                controller["drive_ssh"] = ssh
                # Don't try to connect yet — we'll lazy-connect when needed.
                if self.debug:
                    self.logger.debug(f"{dev.name}: SSH helper configured for {ssh_user}@{address}:{ssh_port}")
            else:
                controller["drive_ssh"] = None
                if ssh_enabled:
                    self.logger.warning(
                        f"{dev.name}: SSH fan control enabled but username/password missing — "
                        f"fan write actions will return an error."
                    )

            # Trigger initial polling
            controller["last_stats_at"]  = 0
            controller["last_sanity_at"] = 0

            # Set up any Drive child devices waiting on this controller
            self._setup_waiting_devices(dev.id)

            # Do an immediate state refresh so the controller (and any
            # already-existing child devices) get populated right away rather
            # than waiting for the next poll tick.
            try:
                self._update_drive_states(dev.id)
            except Exception as e:
                if self.debug:
                    self.logger.debug(f"Initial drive state refresh failed: {e}")

            self.logger.info(f"{dev.name}: Connected")

        except Exception as e:
            self.logger.error(f"{dev.name}: UNAS connection error: {e}")
            if self.debug:
                self.logger.error(traceback.format_exc())
            try:
                dev.updateStateOnServer("status", f"Error: {e}")
                dev.updateStateOnServer("connected", False)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Setup waiting devices (any device whose controller wasn't ready)
    # ------------------------------------------------------------------

    def _setup_waiting_devices(self, controller_id):
        for dev in indigo.devices.iter("self"):
            try:
                cid = int(dev.pluginProps.get("controllerId", 0))
            except (ValueError, TypeError):
                continue
            if cid != controller_id:
                continue
            type_id = dev.deviceTypeId
            if type_id == "protectCamera":
                self._setup_camera_link(dev)
            elif type_id == "protectSensor":
                self._setup_sensor_link(dev)
            elif type_id == "networkDevice":
                self._setup_netdev_link(dev)
            elif type_id == "unifiPort":
                self._setup_port_link(dev)
            elif type_id == "clientDevice":
                self._setup_client_link(dev)
            elif type_id == "accessDoor":
                self._setup_door_link(dev)
            elif type_id == "firewallPolicy":
                self._setup_firewallpolicy_link(dev)
            elif type_id == "trafficRoute":
                self._setup_trafficroute_link(dev)
            elif type_id == "portForward":
                self._setup_portforward_link(dev)
            elif type_id == "wlanDevice":
                self._setup_wlan_link(dev)
            elif type_id == "alarmProfile":
                self._setup_alarmprofile_link(dev)
            elif type_id == "protectPackageCamera":
                self._setup_packagecam_link(dev)
            elif type_id == "driveFan":
                self._setup_fan_link(dev)
            elif type_id == "unifiDrivePool":
                self._setup_pool_link(dev)
            elif type_id == "unifiDriveDisk":
                self._setup_disk_link(dev)

    def _get_controller(self, dev):
        """Return (controller_id, controller_dict) or (None, None)."""
        try:
            cid = int(dev.pluginProps.get("controllerId", 0))
        except (ValueError, TypeError):
            return None, None
        return cid, self.controllers.get(cid)

    # ------------------------------------------------------------------
    # Per-device link setup
    # ------------------------------------------------------------------

    def _setup_camera_link(self, dev):
        cid, controller = self._get_controller(dev)
        camera_id = dev.pluginProps.get("cameraId", "")
        if not controller:
            dev.updateStateOnServer("status", "Waiting for controller")
            return
        if "protect_api" not in controller:
            dev.updateStateOnServer("status", "Error: Protect not enabled")
            return
        if not camera_id:
            dev.updateStateOnServer("status", "Error: Missing camera ID")
            return
        controller["cameras"][camera_id] = dev.id
        # refresh=False: the controller already did a bulk refresh_cache()
        # covering every camera/sensor/light/chime in one shot before this
        # device's communication even started (protect_api is only ever set
        # on the controller AFTER that refresh completes - see
        # _start_controller). Forcing an individual GET here too is pure
        # waste, and with enough cameras starting up in the same second it
        # blows straight through Protect's 10-requests/second rate limit,
        # logging an HTTP 429 "state update error" for every camera beyond
        # the first ~10. The periodic stats poll already uses this same
        # bulk-refresh-then-cached-read pattern (see the Protect block in
        # the main polling loop) - this just applies it at startup too.
        self._update_camera_states(cid, camera_id, refresh=False)

    def _setup_sensor_link(self, dev):
        cid, controller = self._get_controller(dev)
        sensor_id = dev.pluginProps.get("sensorId", "")
        if not controller:
            dev.updateStateOnServer("status", "Waiting for controller")
            return
        if "protect_api" not in controller:
            dev.updateStateOnServer("status", "Error: Protect not enabled")
            return
        if not sensor_id:
            dev.updateStateOnServer("status", "Error: Missing sensor ID")
            return
        controller["sensors"][sensor_id] = dev.id
        # See the matching comment in _setup_camera_link - same reasoning.
        self._update_sensor_states(cid, sensor_id, refresh=False)

    def _setup_netdev_link(self, dev):
        cid, controller = self._get_controller(dev)
        mac = dev.pluginProps.get("deviceMac", "").lower()
        if not controller:
            dev.updateStateOnServer("status", "Waiting for controller")
            return
        if "network_api" not in controller:
            dev.updateStateOnServer("status", "Error: Network not enabled")
            return
        if not mac:
            dev.updateStateOnServer("status", "Error: Missing MAC")
            return
        controller["network_devices"][mac] = dev.id
        self._update_netdev_states(cid, mac)

    def _setup_port_link(self, dev):
        cid, controller = self._get_controller(dev)
        mac = dev.pluginProps.get("deviceMac", "").lower()
        try:
            port_num = int(dev.pluginProps.get("portNumber", 0))
        except (TypeError, ValueError):
            port_num = 0
        if not controller:
            dev.updateStateOnServer("portName", "Waiting for controller")
            return
        if "network_api" not in controller:
            dev.updateStateOnServer("portName", "Error: Network not enabled")
            return
        if not mac or port_num <= 0:
            dev.updateStateOnServer("portName", "Error: Missing config")
            return
        controller["ports"][f"{mac}:{port_num}"] = dev.id
        self._update_port_states(cid, mac, port_num)

    def _setup_client_link(self, dev):
        cid, controller = self._get_controller(dev)
        mac = dev.pluginProps.get("clientMac", "").lower()
        if not controller:
            dev.updateStateOnServer("status", "Waiting for controller")
            return
        if "network_api" not in controller:
            dev.updateStateOnServer("status", "Error: Network not enabled")
            return
        if not mac:
            dev.updateStateOnServer("status", "Error: Missing MAC")
            return
        controller["clients"][mac] = dev.id
        self._update_client_states(cid, mac)

    def _setup_door_link(self, dev):
        cid, controller = self._get_controller(dev)
        door_id = dev.pluginProps.get("doorId", "")
        if not controller:
            dev.updateStateOnServer("status", "Waiting for controller")
            return
        if "access_api" not in controller:
            dev.updateStateOnServer("status", "Error: Access not enabled")
            return
        if not door_id:
            dev.updateStateOnServer("status", "Error: Missing door ID")
            return

        # Migration: devices created before this was added won't have it in
        # pluginProps. One-time fix-up, same idea as stateListOrDisplayStateIdChanged()
        # for states - existing devices don't retroactively pick up new
        # plugin-side props just because Devices.xml/creation code changed.
        if not dev.pluginProps.get("IsLockSubType"):
            try:
                new_props = dev.pluginProps
                new_props["IsLockSubType"] = True
                dev.replacePluginPropsOnServer(new_props)
            except Exception as e:
                if self.debug:
                    self.logger.debug(f"{dev.name}: failed to set IsLockSubType: {e}")

        controller["doors"][door_id] = dev.id
        self._update_door_states(cid, door_id)

    def _setup_firewallpolicy_link(self, dev):
        cid, controller = self._get_controller(dev)
        policy_id = dev.pluginProps.get("policyId", "")
        if not controller:
            dev.updateStateOnServer("status", "Waiting for controller")
            return
        if "network_api" not in controller:
            dev.updateStateOnServer("status", "Error: Network not enabled")
            return
        if not policy_id:
            dev.updateStateOnServer("status", "Error: No policy selected")
            return
        controller["firewall_policies"][policy_id] = dev.id
        self._update_firewallpolicy_states(cid, policy_id)

    def _update_firewallpolicy_states(self, controller_id, policy_id):
        controller = self.controllers.get(controller_id)
        if not controller:
            return
        dev_id = controller.get("firewall_policies", {}).get(policy_id)
        if not dev_id or dev_id not in indigo.devices:
            return
        dev = indigo.devices[dev_id]
        api = controller.get("network_api")
        if not api:
            return
        try:
            policies = api.get_firewall_policies()
        except Exception as e:
            dev.updateStateOnServer("status", f"Error: {e}")
            return
        policy = next((p for p in policies if p.get("_id") == policy_id), None)
        if not policy:
            dev.updateStateOnServer("status", "Error: Policy not found (deleted?)")
            return

        is_enabled = bool(policy.get("enabled", False))
        last_hit_ms = policy.get("last_hit")
        last_hit_str = ""
        if last_hit_ms:
            try:
                last_hit_str = datetime.fromtimestamp(int(last_hit_ms) / 1000).strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                pass

        states = [
            {"key": "policyName", "value": policy.get("name", "Unknown")},
            {"key": "action",     "value": policy.get("action", "")},
            {"key": "hits",       "value": int(policy.get("hits", 0))},
            {"key": "lastHit",    "value": last_hit_str},
            {"key": "status",     "value": "enabled" if is_enabled else "disabled"},
            # onOffState is native (type="relay") - uiValue carries the
            # meaningful text, same mechanism used throughout this plugin
            # (Access doors, network devices, etc).
            {"key": "onOffState", "value": is_enabled,
             "uiValue": "Enabled" if is_enabled else "Disabled"},
        ]
        try:
            dev.updateStatesOnServer(states)
        except Exception as e:
            if self.debug:
                self.logger.debug(f"updateStatesOnServer({dev.name}) failed: {e}")

    def _setup_trafficroute_link(self, dev):
        cid, controller = self._get_controller(dev)
        route_id = dev.pluginProps.get("routeId", "")
        if not controller:
            dev.updateStateOnServer("status", "Waiting for controller")
            return
        if "network_api" not in controller:
            dev.updateStateOnServer("status", "Error: Network not enabled")
            return
        if not route_id:
            dev.updateStateOnServer("status", "Error: No route selected")
            return
        controller["traffic_routes"][route_id] = dev.id
        self._update_trafficroute_states(cid, route_id)

    def _update_trafficroute_states(self, controller_id, route_id):
        controller = self.controllers.get(controller_id)
        if not controller:
            return
        dev_id = controller.get("traffic_routes", {}).get(route_id)
        if not dev_id or dev_id not in indigo.devices:
            return
        dev = indigo.devices[dev_id]
        api = controller.get("network_api")
        if not api:
            return
        try:
            routes = api.get_traffic_routes()
        except Exception as e:
            dev.updateStateOnServer("status", f"Error: {e}")
            return
        route = next((r for r in routes if r.get("_id") == route_id), None)
        if not route:
            dev.updateStateOnServer("status", "Error: Route not found (deleted?)")
            return

        is_enabled = bool(route.get("enabled", False))
        states = [
            {"key": "routeDescription",  "value": route.get("description", "Unknown")},
            {"key": "matchingTarget",    "value": route.get("matching_target", "")},
            {"key": "killSwitchEnabled", "value": bool(route.get("kill_switch_enabled", False))},
            {"key": "status",            "value": "enabled" if is_enabled else "disabled"},
            {"key": "onOffState", "value": is_enabled,
             "uiValue": "Enabled" if is_enabled else "Disabled"},
        ]
        try:
            dev.updateStatesOnServer(states)
        except Exception as e:
            if self.debug:
                self.logger.debug(f"updateStatesOnServer({dev.name}) failed: {e}")

    def _setup_portforward_link(self, dev):
        cid, controller = self._get_controller(dev)
        forward_id = dev.pluginProps.get("forwardId", "")
        if not controller:
            dev.updateStateOnServer("status", "Waiting for controller")
            return
        if "network_api" not in controller:
            dev.updateStateOnServer("status", "Error: Network not enabled")
            return
        if not forward_id:
            dev.updateStateOnServer("status", "Error: No rule selected")
            return
        controller["port_forwards"][forward_id] = dev.id
        self._update_portforward_states(cid, forward_id)

    def _update_portforward_states(self, controller_id, forward_id):
        controller = self.controllers.get(controller_id)
        if not controller:
            return
        dev_id = controller.get("port_forwards", {}).get(forward_id)
        if not dev_id or dev_id not in indigo.devices:
            return
        dev = indigo.devices[dev_id]
        api = controller.get("network_api")
        if not api:
            return
        try:
            forwards = api.get_port_forwards()
        except Exception as e:
            dev.updateStateOnServer("status", f"Error: {e}")
            return
        fwd = next((f for f in forwards if f.get("_id") == forward_id), None)
        if not fwd:
            dev.updateStateOnServer("status", "Error: Rule not found (deleted?)")
            return

        is_enabled = bool(fwd.get("enabled", False))
        states = [
            {"key": "forwardName",      "value": fwd.get("name", "Unknown")},
            {"key": "destinationIP",    "value": fwd.get("fwd", "")},
            {"key": "destinationPort",  "value": str(fwd.get("dst_port", ""))},
            {"key": "forwardPort",      "value": str(fwd.get("fwd_port", ""))},
            {"key": "protocol",         "value": fwd.get("proto", "")},
            {"key": "status",           "value": "enabled" if is_enabled else "disabled"},
            {"key": "onOffState", "value": is_enabled,
             "uiValue": "Enabled" if is_enabled else "Disabled"},
        ]
        try:
            dev.updateStatesOnServer(states)
        except Exception as e:
            if self.debug:
                self.logger.debug(f"updateStatesOnServer({dev.name}) failed: {e}")

    def _setup_wlan_link(self, dev):
        cid, controller = self._get_controller(dev)
        wlan_id = dev.pluginProps.get("wlanId", "")
        if not controller:
            dev.updateStateOnServer("status", "Waiting for controller")
            return
        if "network_api" not in controller:
            dev.updateStateOnServer("status", "Error: Network not enabled")
            return
        if not wlan_id:
            dev.updateStateOnServer("status", "Error: No WLAN selected")
            return
        controller["wlans"][wlan_id] = dev.id
        self._update_wlan_states(cid, wlan_id)

    def _update_wlan_states(self, controller_id, wlan_id):
        controller = self.controllers.get(controller_id)
        if not controller:
            return
        dev_id = controller.get("wlans", {}).get(wlan_id)
        if not dev_id or dev_id not in indigo.devices:
            return
        dev = indigo.devices[dev_id]
        api = controller.get("network_api")
        if not api:
            return
        try:
            wlans = api.get_wlans()
        except Exception as e:
            dev.updateStateOnServer("status", f"Error: {e}")
            return
        wlan = next((w for w in wlans if w.get("_id") == wlan_id), None)
        if not wlan:
            dev.updateStateOnServer("status", "Error: WLAN not found (deleted?)")
            return

        is_enabled = bool(wlan.get("enabled", False))
        states = [
            {"key": "wlanName", "value": wlan.get("name", "Unknown")},
            {"key": "security", "value": wlan.get("security", "")},
            {"key": "isGuest",  "value": bool(wlan.get("is_guest", False))},
            {"key": "status",   "value": "enabled" if is_enabled else "disabled"},
            {"key": "onOffState", "value": is_enabled,
             "uiValue": "Enabled" if is_enabled else "Disabled"},
        ]
        try:
            dev.updateStatesOnServer(states)
        except Exception as e:
            if self.debug:
                self.logger.debug(f"updateStatesOnServer({dev.name}) failed: {e}")

    def _setup_alarmprofile_link(self, dev):
        cid, controller = self._get_controller(dev)
        profile_id = dev.pluginProps.get("armProfileId", "")
        if not controller:
            dev.updateStateOnServer("status", "Waiting for controller")
            return
        if "protect_api" not in controller:
            dev.updateStateOnServer("status", "Error: Protect not enabled")
            return
        if not profile_id:
            dev.updateStateOnServer("status", "Error: No profile selected")
            return
        controller["alarm_profiles"][profile_id] = dev.id
        self._update_alarmprofile_states(cid, profile_id)

    def _update_alarmprofile_states(self, controller_id, profile_id):
        """EXPERIMENTAL - see get_alarm_profiles()/get_alarm_state()
        docstrings. Matches on either `id` or `_id` since the real field
        name isn't confirmed."""
        controller = self.controllers.get(controller_id)
        if not controller:
            return
        dev_id = controller.get("alarm_profiles", {}).get(profile_id)
        if not dev_id or dev_id not in indigo.devices:
            return
        dev = indigo.devices[dev_id]
        api = controller.get("protect_api")
        if not api:
            return

        profiles = api.get_alarm_profiles()
        profile = next((p for p in profiles
                        if isinstance(p, dict) and (p.get("id") == profile_id or p.get("_id") == profile_id)),
                       None)
        profile_name = (profile or {}).get("name", profile_id)

        current_state = api.get_alarm_state()
        is_armed_to_this = False
        if isinstance(current_state, dict) and "status" in current_state:
            # Confirmed armMode shape (3.12.8) - "status" is a real field,
            # verified directly against a live controller. It says whether
            # Protect is armed at all, not which profile - "disabled" is
            # therefore a confident, complete answer (every profile device
            # is Disarmed, no guessing needed); anything else means SOME
            # profile is active, so fall through to the guess-based match
            # below to work out which one, since armMode doesn't say.
            if current_state.get("status") == "disabled":
                is_armed_to_this = False
            else:
                active_id = current_state.get("armProfileId") or current_state.get("profileId")
                is_armed_to_this = (active_id == profile_id) if active_id else False
        elif isinstance(current_state, dict):
            # Not the confirmed armMode shape - one of the older unconfirmed
            # guesses from get_alarm_state()'s fallback path.
            active_id = current_state.get("armProfileId") or current_state.get("profileId") or current_state.get("id")
            is_armed_to_this = (active_id == profile_id) and bool(
                current_state.get("armed", current_state.get("isArmed", True)))
        elif isinstance(current_state, str):
            # Some plausible shapes just report the active profile id/name as a bare string
            is_armed_to_this = (current_state == profile_id or current_state == profile_name)

        states = [
            {"key": "profileName", "value": profile_name},
            {"key": "status", "value": "armed" if is_armed_to_this else "disarmed"},
            {"key": "onOffState", "value": is_armed_to_this,
             "uiValue": "Armed" if is_armed_to_this else "Disarmed"},
        ]
        try:
            dev.updateStatesOnServer(states)
        except Exception as e:
            if self.debug:
                self.logger.debug(f"updateStatesOnServer({dev.name}) failed: {e}")

    def _mirror_states(self, dev_id, states):
        """Write states to a device by Indigo device id, if it still
        exists. Used to keep a camera and its linked Package Camera
        device's genuinely-shared states (they're the same underlying
        camera object) in sync with each other immediately, rather than
        waiting for each device's own next refresh."""
        if not dev_id or dev_id not in indigo.devices:
            return
        try:
            indigo.devices[dev_id].updateStatesOnServer(states)
        except Exception as e:
            if self.debug:
                self.logger.debug(f"_mirror_states({dev_id}) failed: {e}")

    def _mirror_to_package_camera(self, controller_id, cam_id, states):
        """Mirror states to the Package Camera device linked to the
        protectCamera with this Protect camera id, if one exists."""
        controller = self.controllers.get(controller_id)
        if not controller:
            return
        parent_dev_id = controller.get("cameras", {}).get(cam_id)
        if not parent_dev_id:
            return
        pkg_dev_id = controller.get("package_cameras", {}).get(str(parent_dev_id))
        self._mirror_states(pkg_dev_id, states)

    def _setup_packagecam_link(self, dev):
        cid, controller = self._get_controller(dev)
        parent_dev_id = dev.pluginProps.get("parentCameraId", "")
        if not controller:
            dev.updateStateOnServer("status", "Waiting for controller")
            return
        if "protect_api" not in controller:
            dev.updateStateOnServer("status", "Error: Protect not enabled")
            return
        if not parent_dev_id:
            dev.updateStateOnServer("status", "Error: No parent camera selected")
            return
        controller["package_cameras"][parent_dev_id] = dev.id
        self._update_packagecam_states(cid, parent_dev_id)

    def _update_packagecam_states(self, controller_id, parent_dev_id):
        """Package Camera devices mirror the full state structure of a
        regular protectCamera (3.12.12, at the user's explicit request) -
        genuinely reasonable since it IS the same camera object in
        Protect's own data model, not a separate piece of hardware. Every
        state except rtspUrlPackage is simply copied from the parent
        device's own current states (no separate API call - the parent's
        own refresh/WS handling is already the source of truth for all of
        it); rtspUrlPackage gets its own fresh check since it can change
        independently via this device's own dedicated actions."""
        controller = self.controllers.get(controller_id)
        if not controller:
            return
        dev_id = controller.get("package_cameras", {}).get(parent_dev_id)
        if not dev_id or dev_id not in indigo.devices:
            return
        dev = indigo.devices[dev_id]
        api = controller.get("protect_api")
        parent_id_int = int(parent_dev_id) if str(parent_dev_id).isdigit() else None
        if not api or parent_id_int not in indigo.devices:
            dev.updateStateOnServer("status", "Error: Parent camera device not found")
            return
        parent_dev = indigo.devices[parent_id_int]
        cam_id = parent_dev.pluginProps.get("cameraId", "")
        if not cam_id:
            dev.updateStateOnServer("status", "Error: Parent camera has no cameraId")
            return

        states = []
        mirrored_keys = [
            "status", "isConnected", "eventFeedLive",
            "motionDetected", "lastMotion", "personDetected", "lastPersonDetection",
            "vehicleDetected", "packageDetected",
            "audioDetected", "lastAudio", "smokeAlarmDetected", "coAlarmDetected",
            "sirenDetected", "babyCryDetected", "speechDetected", "barkDetected",
            "burglarAlarmDetected", "carHornDetected", "glassBreakDetected",
            "doorbellRinging", "lastDoorbellRing",
            "isDark", "voltage", "recordingMode", "videoMode", "hdrType",
            "micVolume", "irMode", "statusLight", "ipAddress", "macAddress",
            "linkSpeed", "uptime", "rtspUrlHigh", "rtspUrlMedium", "rtspUrlLow",
            "snapshotUrl", "firmwareVersion", "model",
        ]
        for key in mirrored_keys:
            if key in parent_dev.states:
                states.append({"key": key, "value": parent_dev.states[key]})
        # onOffState mirrors the parent's own motion-based Detected/Clear
        # (3.11.1 convention) - same camera, same meaning.
        if "motionDetected" in parent_dev.states:
            motion = bool(parent_dev.states["motionDetected"])
            states.append({"key": "onOffState", "value": motion,
                           "uiValue": "Detected" if motion else "Clear"})

        try:
            existing = api.get_rtsps_streams(cam_id) or {}
            states.append({"key": "rtspUrlPackage", "value": existing.get("package") or ""})
        except Exception as e:
            if self.debug:
                self.logger.debug(f"get_rtsps_streams for package camera failed: {e}")

        try:
            dev.updateStatesOnServer(states)
        except Exception as e:
            if self.debug:
                self.logger.debug(f"updateStatesOnServer({dev.name}) failed: {e}")

    def _setup_fan_link(self, dev):
        cid, controller = self._get_controller(dev)
        if not controller:
            dev.updateStateOnServer("status", "Waiting for controller")
            return
        if "drive_api" not in controller:
            dev.updateStateOnServer("status", "Error: Drive not enabled")
            return
        controller["fan_device"] = dev.id
        self._update_fan_states(cid)

    def _setup_pool_link(self, dev):
        cid, controller = self._get_controller(dev)
        if not controller:
            dev.updateStateOnServer("status", "Waiting for controller")
            return
        if "drive_api" not in controller:
            dev.updateStateOnServer("status", "Error: Drive not enabled")
            return
        controller.setdefault("pool_devices", {})
        key = dev.pluginProps.get("poolKey", "")
        if not key:
            dev.updateStateOnServer("status", "Error: Missing pool key")
            return
        controller["pool_devices"][key] = dev.id
        self._update_pool_states(cid, key)

    def _setup_disk_link(self, dev):
        cid, controller = self._get_controller(dev)
        if not controller:
            dev.updateStateOnServer("status", "Waiting for controller")
            return
        if "drive_api" not in controller:
            dev.updateStateOnServer("status", "Error: Drive not enabled")
            return
        controller.setdefault("disk_devices", {})
        key = dev.pluginProps.get("diskKey", "")
        if not key:
            dev.updateStateOnServer("status", "Error: Missing disk key")
            return
        controller["disk_devices"][key] = dev.id
        self._update_disk_states(cid, key)

    # ------------------------------------------------------------------
    # Polling loop
    # ------------------------------------------------------------------

    def _polling_loop(self):
        """Tiered polling: stats interval (CPU/mem/temp), sanity interval (full re-sync)."""
        while not self.stop_polling:
            try:
                now = time.time()
                for cid in list(self.controllers.keys()):
                    try:
                        self._poll_controller(cid, now)
                    except Exception as e:
                        self.logger.error(f"Polling error on controller {cid}: {e}")
                        if self.debug:
                            self.logger.error(traceback.format_exc())
            except Exception as e:
                self.logger.error(f"Polling loop error: {e}")
            time.sleep(5)  # outer tick is cheap; the per-controller intervals gate the work

    def _check_door_relocks(self, controller_id, now):
        """Check doors with a pending momentary-unlock auto-relock timer
        and re-lock them once appropriate.

        Behavior as specified by the user:
          - doorState == "none" (no position sensor on this door): flat
            DOOR_RELOCK_TIMEOUT_SEC after unlock, unconditionally - there's
            no way to know whether anyone actually opened it.
          - doorState available and never seen "open" within the window:
            same timeout applies (nobody opened it).
          - doorState seen "open" during the window: stop timing out and
            wait for "closed" instead - the door hardware re-locks itself
            once physically closed (normal strike/maglock behavior), so
            this just stops tracking once that's observed rather than
            issuing a redundant lock_now - the next regular state read
            will already show it locked.

        Only applies to momentary unlocks (see actionControlDevice) -
        "leave unlocked" mode never populates door_relock_pending at all,
        by design.
        """
        controller = self.controllers.get(controller_id)
        if not controller:
            return
        pending = controller.get("door_relock_pending")
        if not pending:
            return
        api = controller.get("access_api")
        if not api:
            return

        for door_id, info in list(pending.items()):
            dev_id = controller.get("doors", {}).get(door_id)
            if not dev_id or dev_id not in indigo.devices:
                del pending[door_id]
                continue
            dev = indigo.devices[dev_id]
            door_state = dev.states.get("doorState", "none")

            if door_state == "open":
                info["opened"] = True
                continue

            if info.get("opened") and door_state == "closed":
                del pending[door_id]
                continue

            if now - info.get("unlocked_at", now) >= DOOR_RELOCK_TIMEOUT_SEC:
                try:
                    api.set_door_lock_rule(door_id, "lock_now")
                    self.logger.info(
                        f"{dev.name}: Auto-relocked after "
                        f"{DOOR_RELOCK_TIMEOUT_SEC}s (no door-open detected)")
                    self._update_door_states(controller_id, door_id)
                except Exception as e:
                    self.logger.error(f"{dev.name}: Auto-relock failed: {e}")
                del pending[door_id]

    def _check_event_feed_health(self, controller_id):
        """Keep every camera's eventFeedLive state honest, and force every
        live-event-driven boolean (motion, person/vehicle/package, all the
        audio types) to False the moment the events WS actually drops -
        rather than leaving them frozen at whatever they last said.

        The reasoning this exists for: Indigo booleans have no "unknown"
        value. If this plugin just stopped updating motionDetected while
        the socket was down, a dead connection and a genuinely quiet house
        would look identical to any automation watching that state - the
        two situations need to be distinguishable, and eventFeedLive is
        what distinguishes them. Checked every 5s poll tick (cheap - just
        reads a connection object's own .connected flag, no network call)
        rather than hooked to a close callback, since UnifiWebSocket
        doesn't expose one."""
        controller = self.controllers.get(controller_id)
        if not controller:
            return
        cameras = controller.get("cameras") or {}
        if not cameras:
            return
        events_ws = controller.get("protect_events_ws")
        is_live = bool(events_ws is not None and events_ws.connected)

        live_driven_keys = [
            "motionDetected", "personDetected", "vehicleDetected", "packageDetected",
            "audioDetected", "smokeAlarmDetected", "coAlarmDetected", "sirenDetected",
            "babyCryDetected", "speechDetected", "barkDetected", "burglarAlarmDetected",
            "carHornDetected", "glassBreakDetected",
        ]
        for dev_id in cameras.values():
            if dev_id not in indigo.devices:
                continue
            dev = indigo.devices[dev_id]
            was_live = bool(dev.states.get("eventFeedLive", False))
            if is_live == was_live:
                continue  # no change - don't rewrite states every 5s for nothing
            dev.updateStateOnServer("eventFeedLive", is_live)
            if not is_live:
                for key in live_driven_keys:
                    if key in dev.states and dev.states.get(key):
                        dev.updateStateOnServer(key, False)
                # onOffState mirrors motionDetected's Detected/Clear
                # convention (3.11.1) - clear it the same way on a drop.
                dev.updateStateOnServer("onOffState", False, uiValue="Clear")

    def _poll_controller(self, controller_id, now):
        controller = self.controllers.get(controller_id)
        if not controller:
            return

        # Door auto-relock timers run every outer tick (5s) regardless of
        # the stats/sanity gating below - the relock window itself
        # (DOOR_RELOCK_TIMEOUT_SEC) is much shorter than either interval.
        if controller.get("door_relock_pending"):
            self._check_door_relocks(controller_id, now)

        # Same "every tick, not gated" treatment as the door relock check
        # above - an event feed outage needs to be reflected immediately,
        # not after the next stats/sanity interval.
        if controller.get("protect_events_ws") is not None:
            self._check_event_feed_health(controller_id)

        # Aggressive mode short-circuits both intervals to AGGRESSIVE_INTERVAL_SEC
        if controller.get("aggressive"):
            stats_iv  = AGGRESSIVE_INTERVAL_SEC
            sanity_iv = AGGRESSIVE_INTERVAL_SEC
        else:
            stats_iv  = controller["stats_interval_sec"]
            sanity_iv = controller["sanity_interval_sec"]

        do_stats  = (now - controller["last_stats_at"])  >= stats_iv  if stats_iv  > 0 else False
        do_sanity = (now - controller["last_sanity_at"]) >= sanity_iv if sanity_iv > 0 else False

        if do_sanity:
            controller["last_sanity_at"] = now
            self._sanity_resync(controller_id)

        if do_stats:
            controller["last_stats_at"] = now
            self._refresh_stats(controller_id)

    def _refresh_stats(self, controller_id):
        """Refresh only the slow-changing stats data (CPU, memory, throughput, drive)."""
        controller = self.controllers[controller_id]

        # Network device stats - we re-fetch the full device list since legacy
        # bundles all stats together
        if "network_api" in controller:
            try:
                # Single call refreshes data; per-device updates pull from cache
                # but we want fresh data so we just call _update_netdev_states which
                # internally calls get_device which does a fresh fetch
                # Optimization: fetch list once, distribute
                devices = controller["network_api"].get_devices()
                by_mac  = {d.get("mac", "").lower(): d for d in devices}
                for mac, dev_id in list(controller["network_devices"].items()):
                    if mac in by_mac:
                        self._apply_netdev_state(controller_id, mac, by_mac[mac])
                # Ports rely on the same fetch
                for port_key, dev_id in list(controller["ports"].items()):
                    try:
                        mac, port_num = port_key.split(":")
                        if mac in by_mac:
                            self._apply_port_state(controller_id, mac, int(port_num), by_mac[mac])
                    except Exception:
                        pass
            except Exception as e:
                if self.debug:
                    self.logger.debug(f"Network stats refresh error: {e}")

        # Drive stats - always poll when API is connected, since the controller
        # device itself surfaces useful at-a-glance fields even if no child
        # pool/disk devices have been created yet.
        if "drive_api" in controller:
            try:
                self._update_drive_states(controller_id)
            except Exception as e:
                if self.debug:
                    self.logger.debug(f"Drive stats refresh error: {e}")
            if controller.get("fan_device"):
                try:
                    self._update_fan_states(controller_id)
                except Exception as e:
                    if self.debug:
                        self.logger.debug(f"Fan stats refresh error: {e}")
            # Daily check: if fan control was deployed but its marker file is
            # now missing (UniFi OS update wiped /etc), silently redeploy.
            # Triggered only when the user has opted in via the controller
            # config tickbox (default on).
            try:
                self._check_fan_control_deployment(controller_id)
            except Exception as e:
                if self.debug:
                    self.logger.debug(f"Fan control deployment check error: {e}")

        # Protect: cameras have a lot of stats (recording/bitrate/voltage). WebSocket
        # handles motion/smart events; this just keeps the slow stats fresh.
        if "protect_api" in controller:
            try:
                controller["protect_api"].refresh_cache()
                for camera_id in list(controller["cameras"].keys()):
                    self._update_camera_states(controller_id, camera_id, refresh=False)
                for sensor_id in list(controller["sensors"].keys()):
                    self._update_sensor_states(controller_id, sensor_id, refresh=False)
            except Exception as e:
                if self.debug:
                    self.logger.debug(f"Protect stats refresh error: {e}")

    def _sanity_resync(self, controller_id):
        """Fully re-sync state in case WebSocket dropped messages.
        Currently performs the same work as stats refresh; kept as a separate
        method so we can later add deeper checks (auto-rediscover new devices, etc)."""
        if self.debug:
            self.logger.debug(f"Sanity re-sync for controller {controller_id}")
        self._refresh_stats(controller_id)

        # Update controller-level states
        try:
            self._update_controller_states(controller_id)
        except Exception as e:
            if self.debug:
                self.logger.debug(f"Controller state refresh error: {e}")

    def _update_controller_states(self, controller_id):
        """Confirmed real sources for every field here (3.12.11) - none
        of this was wired up before (cameraCount/apCount/switchCount/
        gatewayCount/networkDeviceCount were never written at all;
        protectVersion/storageTotal/storageUsed always read fields that
        don't exist on the official NVR object, confirmed directly
        against the local spec copy and a live controller):

        - Counts: computed from devices this plugin already tracks in
          memory, no extra API calls needed. Network device type codes
          confirmed directly against a live controller: usw=switch,
          uap=access point, udm=gateway/console, umbb=5G/mobile-broadband
          gateway. ugw/uxg (older USG and newer UXG gateway families)
          are included in gatewayCount too as well-established Ubiquiti
          naming, though only udm has actually been seen on real
          hardware so far.
        - protectVersion/storageTotal/storageUsed: confirmed legacy-
          bootstrap-only - the official /v1/nvrs object has only id/
          modelKey/name/doorbellSettings, nothing else. Sourced from
          bootstrap.nvr.version and bootstrap.nvr.storageStats.
          recordingSpace.{total,used}.
        - cpuUsage/memoryUsage/uptime: no NVR-specific field exists
          anywhere (checked both the official object and the full
          legacy bootstrap one) - Protect runs on the same physical
          console as the gateway, not separate hardware, so these
          mirror whatever that gateway's own networkDevice already
          reports instead of asserting data that doesn't exist for the
          NVR specifically.
        """
        controller = self.controllers.get(controller_id)
        if not controller:
            return
        dev = controller["device"]

        # Only write states that are actually declared on the unifiController
        # device type. Add new ones to Devices.xml before writing them here.
        declared_states = set(dev.states.keys())
        states = []

        gateway_dev = None
        if "network_api" in controller:
            network_devices = controller.get("network_devices", {})
            if "networkDeviceCount" in declared_states:
                states.append({"key": "networkDeviceCount", "value": len(network_devices)})

            type_counts = {"usw": 0, "uap": 0, "udm": 0, "ugw": 0, "uxg": 0, "umbb": 0}
            for dev_id in network_devices.values():
                if dev_id not in indigo.devices:
                    continue
                d = indigo.devices[dev_id]
                dtype = d.states.get("deviceType", "")
                if dtype in type_counts:
                    type_counts[dtype] += 1
                if dtype in ("udm", "ugw", "uxg") and gateway_dev is None:
                    gateway_dev = d  # first gateway found - used below for cpu/mem/uptime
            if "switchCount" in declared_states:
                states.append({"key": "switchCount", "value": type_counts["usw"]})
            if "apCount" in declared_states:
                states.append({"key": "apCount", "value": type_counts["uap"]})
            if "gatewayCount" in declared_states:
                states.append({"key": "gatewayCount",
                               "value": type_counts["udm"] + type_counts["ugw"] + type_counts["uxg"]})

        if "protect_api" in controller:
            if "cameraCount" in declared_states:
                states.append({"key": "cameraCount", "value": len(controller.get("cameras", {}))})
            try:
                nvr = controller["protect_api"].get_nvr() or {}
                if nvr and "nvrName" in declared_states:
                    states.append({"key": "nvrName", "value": nvr.get("name", "Unknown")})
            except Exception:
                pass
            try:
                bootstrap = controller["protect_api"].get_bootstrap_raw() or {}
                braw_nvr = bootstrap.get("nvr") or {}
                if braw_nvr:
                    if "protectVersion" in declared_states:
                        states.append({"key": "protectVersion", "value": braw_nvr.get("version", "Unknown")})
                    # Declared states are labelled "(GB)" - converting
                    # from the raw bytes the API actually returns, not
                    # storing bytes directly.
                    rec_space = (braw_nvr.get("storageStats") or {}).get("recordingSpace") or {}
                    if "storageTotal" in declared_states and "total" in rec_space:
                        states.append({"key": "storageTotal", "value": round(rec_space["total"] / (1024 ** 3), 1)})
                    if "storageUsed" in declared_states and "used" in rec_space:
                        states.append({"key": "storageUsed", "value": round(rec_space["used"] / (1024 ** 3), 1)})
            except Exception:
                pass

        if gateway_dev is not None:
            for key in ("cpuUsage", "memoryUsage", "uptime"):
                if key in declared_states and key in gateway_dev.states:
                    states.append({"key": key, "value": gateway_dev.states[key]})

        if states:
            try:
                dev.updateStatesOnServer(states)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # WebSocket handlers
    # ------------------------------------------------------------------

    def _on_network_ws(self, controller_id, message):
        """Handle Network legacy WebSocket message."""
        try:
            meta = message.get("meta", {})
            msg_type = meta.get("message", "")

            if msg_type in ("device:update", "device:sync"):
                for d in message.get("data", []):
                    mac = d.get("mac", "").lower()
                    if mac and mac in self.controllers[controller_id]["network_devices"]:
                        self._apply_netdev_state(controller_id, mac, d)

            elif msg_type in ("sta:connect", "sta:disconnect", "sta:sync"):
                for c in message.get("data", []):
                    mac = c.get("mac", "").lower()
                    if mac and mac in self.controllers[controller_id]["clients"]:
                        # Trigger fresh fetch since WS data may be partial
                        self._update_client_states(controller_id, mac)
                    elif mac and msg_type == "sta:connect":
                        # Genuinely new client, no device yet - auto-create
                        # only on the actual connect message (not sync,
                        # which fires for every already-known client on
                        # reconnect/resubscribe too, and not disconnect,
                        # which would create a device for something
                        # that's already left).
                        self._maybe_auto_create_client(controller_id, c, mac)

            elif msg_type in ("device-port:update", "device-port:sync"):
                for d in message.get("data", []):
                    mac = d.get("mac", "").lower()
                    if mac and mac in self.controllers[controller_id]["network_devices"]:
                        self._apply_netdev_state(controller_id, mac, d)
                        # Also update any port devices on this switch
                        for port_key in list(self.controllers[controller_id]["ports"].keys()):
                            try:
                                pmac, pidx = port_key.split(":")
                                if pmac == mac:
                                    self._apply_port_state(controller_id, pmac, int(pidx), d)
                            except Exception:
                                pass

            elif msg_type in ("events", "alert"):
                for event in message.get("data", []):
                    try:
                        self._parse_network_event(controller_id, event)
                    except Exception as e:
                        if self.debug:
                            self.logger.debug(f"Event parse error: {e}")

        except Exception as e:
            if self.debug:
                self.logger.debug(f"Network WS handler error: {e}")

    def _parse_network_event(self, controller_id, event):
        """Route a single EVT_* event to the matching networkDevice,
        clientDevice, or the controller itself. Always updates the target's
        lastEventKey/lastEventMessage/lastEventAt states; additionally fires
        a dedicated Events.xml trigger when one is mapped for this key.
        """
        controller = self.controllers.get(controller_id)
        if not controller or not isinstance(event, dict):
            return

        key = event.get("key", "") or ""
        msg = event.get("msg", "") or ""
        ts  = event.get("time")

        when = ""
        if ts:
            try:
                when = datetime.fromtimestamp(int(ts) / 1000.0).strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                when = ""

        if self.debug and key and key not in _KNOWN_EVENT_KEYS:
            self.logger.debug(f"Unrecognised UniFi event key: {key} ({msg})")

        event_states = [
            {"key": "lastEventKey",     "value": key},
            {"key": "lastEventMessage", "value": msg},
            {"key": "lastEventAt",      "value": when},
        ]

        # Device-scoped: ap/gw/sw hold the device MAC depending on hardware class
        device_mac = (event.get("ap") or event.get("gw") or event.get("sw") or "").lower()
        if device_mac and device_mac in controller.get("network_devices", {}):
            dev_id = controller["network_devices"][device_mac]
            if dev_id in indigo.devices:
                dev = indigo.devices[dev_id]
                try:
                    dev.updateStatesOnServer(event_states)
                except Exception:
                    pass
                trigger_id = NETWORK_DEVICE_EVENT_TRIGGERS.get(key)
                if trigger_id:
                    self._fire_trigger(trigger_id, dev)
            return

        # Client-scoped: user/client/guest hold the client MAC depending on
        # whether it's a known user, a classified client, or a guest.
        client_mac = (event.get("user") or event.get("client") or event.get("guest") or "").lower()
        if client_mac and client_mac in controller.get("clients", {}):
            dev_id = controller["clients"][client_mac]
            if dev_id in indigo.devices:
                dev = indigo.devices[dev_id]
                try:
                    dev.updateStatesOnServer(event_states)
                except Exception:
                    pass
                trigger_id = CLIENT_EVENT_TRIGGERS.get(key)
                if trigger_id:
                    self._fire_trigger(trigger_id, dev)
            return

        # Neither matched (or the event is inherently controller-wide, e.g.
        # IPS alert, admin login, update available) - route to the
        # controller device itself.
        cdev = controller.get("device")
        if cdev is not None:
            try:
                states = list(event_states)
                if key == "EVT_AD_Update_Available":
                    states.append({"key": "updateAvailable", "value": True})
                cdev.updateStatesOnServer(states)
            except Exception:
                pass
            trigger_id = CONTROLLER_EVENT_TRIGGERS.get(key)
            if trigger_id:
                self._fire_trigger(trigger_id, cdev)

    def _on_protect_devices_ws(self, controller_id, message):
        """Handle Protect Integration API device update WebSocket message.

        Real envelope, confirmed against developer.ui.com/protect/*/openapi.json
        (`deviceAdd`/`deviceUpdate`/`deviceRemove`, discriminated by the
        outer `type`): { "type": "add"|"update"|"remove", "item": {
        "id": "...", "modelKey": "camera"|"sensor"|"light"|"chime"|..., ... } }.
        `item` is the full (or partial, for updates) device object, not a
        bare type-label string - and the device's own id/type live inside
        it, not as separate top-level `id`/`itemType` fields. The previous
        code's assumptions meant this handler never matched anything, for
        any device, ever - live device state changes only ever showed up
        via the next periodic stats poll, not instantly as intended.
        """
        try:
            controller = self.controllers.get(controller_id)
            if not controller:
                return
            api = controller.get("protect_api")
            if not api:
                return

            item = message.get("item")
            if not isinstance(item, dict):
                return
            item_id   = item.get("id")
            model_key = item.get("modelKey")
            if not item_id or not model_key:
                return

            if model_key == "camera" and item_id in controller["cameras"]:
                self._update_camera_states(controller_id, item_id)
            elif model_key == "sensor" and item_id in controller["sensors"]:
                self._update_sensor_states(controller_id, item_id)
            # Lights / chimes can be added when device types exist

        except Exception as e:
            if self.debug:
                self.logger.debug(f"Protect devices WS handler error: {e}")

    def _on_protect_events_ws(self, controller_id, message):
        """Handle Protect Integration API event stream (motion, smart detect, doorbell).

        Real envelope, confirmed against developer.ui.com/protect/*/openapi.json
        (`eventAdd`/`eventUpdate`, discriminated by the outer `type`):
            { "type": "add" | "update", "item": {
                  "type": "motion" | "smartDetectZone" | "ring" | ...,
                  "id": "...", "device": "<camera_id>",
                  "start": <ms>, "end": <ms or null>,
                  "smartDetectTypes": [...] } }
        The actual event lives under "item", not "event" (fixed in 3.8.1 -
        the previous code checked message.get("event"), which is never
        present, so nothing in this handler ever fired).

        A single physical occurrence generates multiple messages over its
        lifetime - a start, zero or more "still active" continuations (same
        id, end still null), and an end. `start` never changes across them,
        so `now - start` is only a meaningful "detection latency" number on
        the very FIRST message; on later ones it just measures how long the
        event has been running (fixed in 3.8.2's follow-up - that version's
        diagnostic reported event duration as if it were lag). Event ids are
        tracked per controller so triggers fire once at the true start, not
        again on every interim update for a long-running zone.
        """
        try:
            controller = self.controllers.get(controller_id)
            if not controller:
                return

            event = message.get("item") or message.get("event") or message
            evt_type = event.get("type", "")
            cam_id   = event.get("device") or event.get("camera") or event.get("deviceId")
            if not cam_id or cam_id not in controller["cameras"]:
                return

            dev = indigo.devices[controller["cameras"][cam_id]]
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            # Protect sends more than one message per physical motion/smart-
            # detect occurrence: an initial one when it starts (end: null),
            # zero or more "still active" updates while it continues (also
            # end: null, SAME event id), and a final one once it ends (end
            # set). `start` stays fixed at the ORIGINAL start time across
            # all of them - so comparing `now` against `start` on anything
            # but the very first message doesn't measure latency at all, it
            # measures how long the event has been running. That conflation
            # is exactly what made the 3.8.2 diagnostic report things like
            # "47s of lag" for what was actually a 47-second-long motion
            # event correctly noticed in ~3s - not a processing delay.
            #
            # Track which event ids we've already fired a "start" trigger
            # for (per controller) so a long or repeatedly-updated zone
            # fires motionDetected/personDetected/etc. once at the true
            # start, not again on every interim "still active" update.
            event_id      = event.get("id")
            end           = event.get("end")
            start_ms      = event.get("start")
            active_events = controller.setdefault("active_protect_events", set())
            is_new_event  = bool(event_id) and event_id not in active_events
            is_repeat      = bool(event_id) and not is_new_event and end is None

            if self.debug and isinstance(start_ms, (int, float)) and start_ms > 0:
                if end is None and is_new_event:
                    lag = time.time() - (start_ms / 1000.0)
                    self.logger.debug(
                        f"[Protect event timing] {evt_type} on {dev.name}: "
                        f"{lag:.2f}s to detect new event start"
                    )
                elif end is None and is_repeat:
                    self.logger.debug(
                        f"[Protect event timing] {evt_type} on {dev.name}: "
                        f"still-active update for an already-tracked event (not a new detection)"
                    )
                elif end is not None and isinstance(end, (int, float)):
                    duration   = (end - start_ms) / 1000.0 if isinstance(start_ms, (int, float)) else None
                    react_time = time.time() - (end / 1000.0)
                    dur_str = f"event lasted {duration:.2f}s, " if duration is not None else ""
                    self.logger.debug(
                        f"[Protect event timing] {evt_type} on {dev.name}: "
                        f"{dur_str}{react_time:.2f}s to notice it ended"
                    )

            if evt_type == "motion":
                _private_ws = controller.get("protect_private_ws")
                if _private_ws is not None and _private_ws.connected:
                    # The private-WS fast path (see _on_protect_private_ws)
                    # is live and already handling motionDetected/
                    # lastMotion/the trigger for this controller, ~3
                    # seconds faster than this official channel reports
                    # the same occurrence. Handling it again here would
                    # fire the trigger twice for one real event - once
                    # fast, once slow - so this path is skipped entirely
                    # for motion while the fast path is connected. Checked
                    # fresh on every message (not a cached flag), so if the
                    # private connection ever drops, this resumes handling
                    # motion itself automatically until it's back.
                    if self.debug:
                        self.logger.debug(
                            f"[Protect event timing] motion on {dev.name}: "
                            f"skipped (fast path active) - not fired again here"
                        )
                elif end is None:
                    if is_new_event:
                        if event_id:
                            active_events.add(event_id)
                        dev.updateStateOnServer("motionDetected", True)
                        # uiValue matches the same Detected/Clear
                        # convention used for general-purpose Protect
                        # sensors (3.11.0) - the camera is being used as a
                        # motion sensor, same idea.
                        dev.updateStateOnServer("onOffState", True, uiValue="Detected")
                        dev.updateStateOnServer("lastMotion",     now_str)
                        self._fire_trigger("motionDetected", dev)
                    # else: interim "still active" update - state is
                    # already correct, nothing to do
                else:  # motion end
                    if event_id:
                        active_events.discard(event_id)
                    dev.updateStateOnServer("motionDetected", False)
                    dev.updateStateOnServer("onOffState", False, uiValue="Clear")

            elif evt_type in ("smartDetectZone", "smartDetectLine", "smartDetectLoiterZone"):
                detect_types = event.get("smartDetectTypes", []) or []
                is_active    = end is None
                if is_active and not is_new_event:
                    pass  # interim "still active" update - already handled
                else:
                    if event_id:
                        if is_active:
                            active_events.add(event_id)
                        else:
                            active_events.discard(event_id)
                    if "person" in detect_types:
                        dev.updateStateOnServer("personDetected", is_active)
                        if is_active:
                            dev.updateStateOnServer("lastPersonDetection", now_str)
                            self._fire_trigger("personDetected", dev)
                    if "vehicle" in detect_types:
                        dev.updateStateOnServer("vehicleDetected", is_active)
                        if is_active:
                            self._fire_trigger("vehicleDetected", dev)
                    if "package" in detect_types:
                        dev.updateStateOnServer("packageDetected", is_active)
                        if is_active:
                            self._fire_trigger("packageDetected", dev)

            elif evt_type == "smartAudioDetect":
                # Confirmed official event (3.12.8) - same microphone every
                # camera already has, no dedicated smoke/CO sensor hardware
                # needed. Deliberately NOT folded into onOffState/motion -
                # audio is a different signal (a camera picking up a sound
                # elsewhere in the room isn't the same claim as the camera
                # itself seeing motion), so it gets its own aggregate state
                # instead of overloading the existing one.
                detect_types = event.get("smartDetectTypes", []) or []
                is_active    = end is None
                if is_active and not is_new_event:
                    pass  # interim "still active" update - already handled
                else:
                    if event_id:
                        if is_active:
                            active_events.add(event_id)
                        else:
                            active_events.discard(event_id)
                    dev.updateStateOnServer("audioDetected", is_active)
                    if is_active:
                        dev.updateStateOnServer("lastAudio", now_str)
                    # (alrm_type, state key, trigger id) - alrm_type names
                    # confirmed against the official smartDetectTypes enum
                    # for this event, not guessed.
                    audio_type_map = [
                        ("alrmSmoke",      "smokeAlarmDetected",   "smokeAlarmDetected"),
                        ("alrmCmonx",      "coAlarmDetected",      "coAlarmDetected"),
                        ("alrmSiren",      "sirenDetected",        "sirenDetected"),
                        ("alrmBabyCry",    "babyCryDetected",      "babyCryDetected"),
                        ("alrmSpeak",      "speechDetected",       "speechDetected"),
                        ("alrmBark",       "barkDetected",         "barkDetected"),
                        ("alrmBurglar",    "burglarAlarmDetected", "burglarAlarmDetected"),
                        ("alrmCarHorn",    "carHornDetected",      "carHornDetected"),
                        ("alrmGlassBreak", "glassBreakDetected",   "glassBreakDetected"),
                    ]
                    for alrm_type, state_key, trigger_id in audio_type_map:
                        if alrm_type in detect_types:
                            dev.updateStateOnServer(state_key, is_active)
                            if is_active:
                                self._fire_trigger(trigger_id, dev)

            elif evt_type == "ring":
                dev.updateStateOnServer("doorbellRinging",  True)
                dev.updateStateOnServer("lastDoorbellRing", now_str)
                self._fire_trigger("doorbellRing", dev)
                # Auto-clear after a few seconds
                threading.Timer(5.0, lambda d=dev: self._clear_doorbell(d)).start()

            self._mirror_event_states_to_package_camera(controller_id, cam_id, dev)

        except Exception as e:
            if self.debug:
                self.logger.debug(f"Protect events WS handler error: {e}")

    def _mirror_event_states_to_package_camera(self, controller_id, cam_id, dev):
        """Mirror every event-driven state this handler can touch onto a
        linked Package Camera device, reading dev's just-updated states
        rather than tracking what changed per branch - simpler and covers
        every event type (including ones added later) uniformly. Cheap:
        a no-op if no Package Camera is linked to this camera."""
        keys = ["motionDetected", "lastMotion", "onOffState",
                "personDetected", "lastPersonDetection", "vehicleDetected", "packageDetected",
                "audioDetected", "lastAudio", "smokeAlarmDetected", "coAlarmDetected",
                "sirenDetected", "babyCryDetected", "speechDetected", "barkDetected",
                "burglarAlarmDetected", "carHornDetected", "glassBreakDetected",
                "doorbellRinging", "lastDoorbellRing"]
        states = [{"key": k, "value": dev.states[k]} for k in keys if k in dev.states]
        if states:
            self._mirror_to_package_camera(controller_id, cam_id, states)

    def _clear_doorbell(self, dev):
        try:
            dev.updateStateOnServer("doorbellRinging", False)
        except Exception:
            pass

    def _on_protect_private_ws(self, controller_id, raw_bytes):
        """Handle Protect's private update WebSocket
        (/proxy/protect/ws/updates) - used ONLY for fast motion detection.

        This is the original real-time channel Protect used for years
        before the official Integration API existed, and is almost
        certainly what the Protect app's own live view still runs on -
        confirmed by timing it directly: the app reacts to motion near-
        instantly, while the official events subscription (_on_protect_
        events_ws) consistently takes ~3.3s for the same occurrence (see
        the 3.8.3 changelog entry). That gap is specific to the official
        API's events subscription, not the camera's own detection, so this
        channel should share the app's speed.

        Binary two-frame packet format decoded by _decode_protect_private_
        frame (see that function's docstring for the wire format). Only
        motion events are handled here; smart-detect (person/vehicle/
        package) and doorbell stay on the official events WS, which
        continues to work correctly - and more slowly - for those. See
        _on_protect_events_ws for why its own motion handling steps aside
        while this connection is active.
        """
        try:
            controller = self.controllers.get(controller_id)
            if not controller:
                return

            action, data = _decode_protect_private_frame(raw_bytes)
            if not isinstance(action, dict) or not isinstance(data, dict):
                return
            if action.get("modelKey") != "event":
                return
            if data.get("type") != "motion":
                return

            # Private API's event object names the camera-reference field
            # "camera", not "device" like the official API - confirmed
            # against uiprotect's Event model (Event._get_unifi_remaps
            # maps wire key "camera" -> its own camera_id attribute).
            cam_id = data.get("camera")
            if not cam_id or cam_id not in controller["cameras"]:
                return

            dev = indigo.devices[controller["cameras"][cam_id]]
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            event_id = data.get("id")
            end      = data.get("end")
            start_ms = data.get("start")

            # Same new-vs-continuation dedup as the official handler, kept
            # in a separate set since this channel's event ids are not
            # guaranteed to match the official API's ids for what's
            # conceptually the same occurrence.
            active_events = controller.setdefault("active_private_motion_events", set())
            is_new_event  = bool(event_id) and event_id not in active_events

            if self.debug and end is None and is_new_event and isinstance(start_ms, (int, float)) and start_ms > 0:
                lag = time.time() - (start_ms / 1000.0)
                self.logger.debug(
                    f"[Protect private-WS timing] motion on {dev.name}: "
                    f"{lag:.2f}s to detect new event start"
                )

            if end is None:
                if is_new_event:
                    if event_id:
                        active_events.add(event_id)
                    dev.updateStateOnServer("motionDetected", True)
                    # Same Detected/Clear convention as the official-WS
                    # handler and the general-purpose Protect sensors
                    # (3.11.0) - consistent regardless of which path is
                    # currently driving this camera's motion state.
                    dev.updateStateOnServer("onOffState", True, uiValue="Detected")
                    dev.updateStateOnServer("lastMotion",     now_str)
                    self._fire_trigger("motionDetected", dev)
                # else: interim "still active" update - nothing to do
            else:
                if event_id:
                    active_events.discard(event_id)
                dev.updateStateOnServer("motionDetected", False)
                dev.updateStateOnServer("onOffState", False, uiValue="Clear")

            # This fast path replaces the official handler's motion
            # handling entirely while connected (see the official
            # handler's own comment on this), so its own mirror call
            # never runs for motion in that case - mirrored here instead.
            self._mirror_to_package_camera(controller_id, cam_id, [
                {"key": "motionDetected", "value": dev.states.get("motionDetected", False)},
                {"key": "onOffState", "value": dev.states.get("motionDetected", False),
                 "uiValue": "Detected" if dev.states.get("motionDetected", False) else "Clear"},
                {"key": "lastMotion", "value": dev.states.get("lastMotion", "")},
            ])

        except Exception as e:
            if self.debug:
                self.logger.debug(f"Protect private WS handler error: {e}")

    def _on_access_ws(self, controller_id, message):
        """Handle Access door notifications WebSocket message.

        Access can deliver several message shapes:
          * dict — actual event payload
          * str  — a heartbeat/keepalive ("Hello", ping/pong text), OR a JSON
                   document delivered as a string that needs parsing
          * bytes — same as str but binary
          * None / empty — keepalive ticks
        Anything that isn't a dict event payload is silently ignored.
        """
        try:
            controller = self.controllers.get(controller_id)
            if not controller:
                return

            # Normalise to a dict if we can; otherwise drop silently.
            if isinstance(message, (bytes, bytearray)):
                try:
                    message = message.decode("utf-8", errors="replace")
                except Exception:
                    return
            if isinstance(message, str):
                stripped = message.strip()
                if not stripped:
                    return
                # Some Access servers send keepalive as plain text ("Hello",
                # "ping", etc.) — only attempt JSON parse if it looks like JSON.
                if stripped[0] not in "{[":
                    return
                try:
                    message = json.loads(stripped)
                except (json.JSONDecodeError, ValueError):
                    return
            if not isinstance(message, dict):
                return

            event_type = message.get("event") or message.get("type")
            data       = message.get("data") or message
            door_id    = (data.get("door_id") if isinstance(data, dict) else None) \
                         or message.get("doorId")

            # Debug: full raw event, every time. Needed specifically to
            # determine whether actor/timestamp data (for lastAccessUser/
            # lastAccessTime) is actually present on THIS WS connection -
            # community sources disagree on whether that data is WS or
            # webhook-only, so this checks directly against the real
            # controller rather than guessing further.
            if self.debug:
                try:
                    self.logger.debug(
                        f"[Access WS event] type={event_type} door_id={door_id}\n"
                        f"{json.dumps(message, default=str, indent=2)}")
                except Exception:
                    self.logger.debug(f"[Access WS event] type={event_type} (not JSON-serialisable)")

            if door_id and door_id in controller.get("doors", {}):
                self._update_door_states(controller_id, door_id)
        except Exception as e:
            if self.debug:
                self.logger.debug(f"Access WS handler error: {e}")

    # ------------------------------------------------------------------
    # State updates - Cameras (Protect Integration API)
    # ------------------------------------------------------------------

    def _update_camera_states(self, controller_id, camera_id, refresh=True):
        try:
            controller = self.controllers.get(controller_id)
            if not controller:
                return
            api = controller.get("protect_api")
            if not api:
                return
            camera = api.get_camera(camera_id) if refresh else \
                     api._cameras_by_id.get(camera_id)
            if not camera:
                return

            # Debug: dump the raw camera payload so field-name mismatches
            # between what we read and what this console actually returns
            # can be diagnosed directly, rather than relying solely on the
            # published OpenAPI spec (which documents Ubiquiti's intent, not
            # necessarily every field a given console/firmware combination
            # actually sends - extra undocumented fields do happen).
            if self.debug:
                try:
                    source = "fresh single-camera GET" if refresh else "bulk list / cached"
                    cam_dump = json.dumps(camera, default=str, indent=2)
                    if len(cam_dump) > 4000:
                        cam_dump = cam_dump[:4000] + "\n…(truncated)"
                    self.logger.debug(
                        f"[Protect raw camera payload: {camera.get('name', camera_id)} "
                        f"| source={source} | {len(camera)} top-level keys]\n{cam_dump}"
                    )
                except Exception:
                    self.logger.debug(f"[Protect raw camera payload - not JSON-serialisable: {type(camera)}]")

            dev_id = controller["cameras"].get(camera_id)
            if not dev_id or dev_id not in indigo.devices:
                return
            dev = indigo.devices[dev_id]

            # Track previous state for triggers
            prev_connected = dev.states.get("isConnected", True)
            prev_dark      = dev.states.get("isDark", False)

            # Connection / model
            is_connected = camera.get("isConnected", camera.get("state") == "CONNECTED")
            is_dark      = camera.get("isDark", False)
            # `type` carries the actual hardware model (e.g. "UVC G3 Flex");
            # `modelKey` is just the device-class ("camera"/"sensor"/"light"/
            # etc, same for every camera regardless of hardware) - confirmed
            # against a real payload, so check the specific one first.
            model        = camera.get("type") or camera.get("modelKey") or \
                           camera.get("marketName") or "Unknown"

            states = [
                {"key": "status",      "value": "online" if is_connected else "offline"},
                {"key": "isConnected", "value": bool(is_connected)},
                {"key": "model",       "value": str(model)},
                {"key": "isDark",      "value": bool(is_dark)},
                {"key": "macAddress",  "value": camera.get("mac", "Unknown")},
            ]

            # LED / status light - genuinely present on the real camera object
            led = camera.get("ledSettings") or {}
            states.append({"key": "statusLight", "value": bool(led.get("isEnabled", False))})

            # videoMode/hdrType/micVolume (3.12.8) - confirmed top-level,
            # official fields (the same ones the Set Video Mode/HDR Mode/
            # Microphone Volume actions write to), only written when
            # actually present rather than asserting a default.
            if camera.get("videoMode") is not None:
                states.append({"key": "videoMode", "value": camera["videoMode"]})
            if camera.get("hdrType") is not None:
                states.append({"key": "hdrType", "value": camera["hdrType"]})
            if camera.get("micVolume") is not None:
                states.append({"key": "micVolume", "value": camera["micVolume"]})

            # firmwareVersion, ipAddress, linkSpeed, uptime, voltage,
            # recordingMode and irMode don't exist anywhere in the official
            # Protect Integration API's camera object (confirmed by diffing
            # a live bulk-list payload against a live single-camera GET -
            # identical, 17 keys each - and by an exhaustive check of every
            # documented camera endpoint). They DO exist on the older
            # private/local bootstrap API, behind the same console login
            # already configured for Network's legacy auth - see the
            # UnifiProtectAPI docstring for how that's wired up. Populated
            # from there when available; left unwritten (not a misleading
            # "Unknown"/0) if legacy credentials aren't configured or this
            # camera isn't in the bootstrap cache for some reason.
            boot = api.get_bootstrap_camera(camera_id)
            if boot:
                fw = boot.get("firmwareVersion")
                if fw:
                    states.append({"key": "firmwareVersion", "value": fw})

                host = boot.get("host")
                if host:
                    states.append({"key": "ipAddress", "value": str(host)})

                voltage = boot.get("voltage")
                if voltage is not None:
                    states.append({"key": "voltage", "value": voltage})

                rec = boot.get("recordingSettings") or {}
                if rec.get("mode"):
                    states.append({"key": "recordingMode", "value": rec["mode"]})

                isp = boot.get("ispSettings") or {}
                if isp.get("irLedMode"):
                    states.append({"key": "irMode", "value": isp["irLedMode"]})

                wired = boot.get("wiredConnectionState") or {}
                phy_rate = wired.get("phyRate")
                if phy_rate:
                    states.append({"key": "linkSpeed", "value": phy_rate})

                # upSince is a timestamp (ms since epoch per the official
                # UniFi convention used elsewhere in this plugin), not a
                # duration - compute elapsed time from it for display.
                up_since = boot.get("upSince")
                if isinstance(up_since, (int, float)) and up_since > 0:
                    try:
                        elapsed = max(0, time.time() - (up_since / 1000.0))
                        days  = int(elapsed) // 86400
                        hours = (int(elapsed) % 86400) // 3600
                        states.append({"key": "uptime", "value": f"{days}d {hours}h"})
                    except Exception:
                        pass

            # RTSP URLs are intentionally NOT refreshed here. The official
            # Protect API has no `channels`/`isRtspEnabled` fields on the
            # camera object at all (confirmed against the published
            # OpenAPI spec) - RTSPS streams are a separate, opt-in resource
            # with no bulk-list endpoint, only a per-camera GET. Checking it
            # on every camera during a routine state refresh would mean one
            # extra HTTP request per camera per cycle, which is exactly the
            # kind of burst that tripped Protect's 10-requests/second rate
            # limit before (see the 3.6.4 changelog entry). Instead, RTSP
            # URL states are set explicitly by the "Enable RTSP Streaming"
            # and "Disable RTSP Streaming" actions, which only ever touch
            # one camera at a time on deliberate user action.

            # Snapshot URL (Bearer-auth path through the integration API)
            states.append({
                "key":   "snapshotUrl",
                "value": api.get_camera_snapshot_url(camera_id),
            })

            # NOTE: motion / smart detect / doorbell are driven by the WS event
            # stream (real-time). We don't poll those into state on stats refresh.

            dev.updateStatesOnServer(states)

            # Connection / dark triggers
            if bool(is_connected) != bool(prev_connected):
                self._fire_trigger("cameraConnected" if is_connected else "cameraDisconnected", dev)
            if bool(is_dark) != bool(prev_dark):
                self._fire_trigger("cameraDark" if is_dark else "cameraLight", dev)

        except Exception as e:
            self.logger.error(f"Camera {camera_id} state update error: {e}")
            if self.debug:
                self.logger.error(traceback.format_exc())

    # ------------------------------------------------------------------
    # State updates - Sensors (Protect Integration API)
    # ------------------------------------------------------------------

    def _update_sensor_states(self, controller_id, sensor_id, refresh=True):
        try:
            controller = self.controllers.get(controller_id)
            if not controller:
                return
            api = controller.get("protect_api")
            if not api:
                return
            sensor = api.get_sensor(sensor_id) if refresh else \
                     api._sensors_by_id.get(sensor_id)
            if not sensor:
                return
            dev_id = controller["sensors"].get(sensor_id)
            if not dev_id or dev_id not in indigo.devices:
                return
            dev = indigo.devices[dev_id]

            # Track prev for triggers
            prev_motion  = dev.states.get("motionDetected", False)
            prev_contact = dev.states.get("isOpened",       False)
            prev_alarm   = dev.states.get("alarmActive",    False)

            is_connected = sensor.get("isConnected", sensor.get("state") == "CONNECTED")
            mount_type   = sensor.get("mountType", "")

            states = [
                {"key": "status",      "value": "online" if is_connected else "offline"},
                {"key": "isConnected", "value": bool(is_connected)},
                {"key": "model",       "value": sensor.get("modelKey") or sensor.get("type", "Unknown")},
                {"key": "macAddress",  "value": sensor.get("mac", "Unknown")},
                {"key": "mountType",   "value": mount_type},
            ]

            # Battery
            battery = sensor.get("batteryStatus") or {}
            if isinstance(battery, dict):
                lvl = battery.get("percentage")
                if lvl is not None:
                    states.append({"key": "batteryLevel", "value": int(lvl)})
                states.append({"key": "batteryLow", "value": bool(battery.get("isLow", False))})
                # Keep human-readable batteryStatus too
                if battery.get("isLow"):
                    states.append({"key": "batteryStatus", "value": "Low"})
                elif battery.get("isCharging"):
                    states.append({"key": "batteryStatus", "value": "Charging"})
                else:
                    states.append({"key": "batteryStatus", "value": "OK"})

            # Motion sensor
            stats = sensor.get("stats") or {}
            motion_stats = stats.get("motion", {}) if isinstance(stats, dict) else {}
            motion_active = bool(motion_stats.get("isTriggered", False))
            states.append({"key": "motionDetected", "value": motion_active})
            if motion_active:
                states.append({"key": "lastMotion",
                               "value": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})

            # Contact (open/close)
            is_opened = bool(sensor.get("isOpened", False))
            states.append({"key": "isOpened", "value": is_opened})

            # Alarm (used below for leak sensors - this is the only
            # "triggered" signal available for them)
            alarm_stats  = stats.get("alarm", {}) if isinstance(stats, dict) else {}
            alarm_active = bool(alarm_stats.get("isTriggered", False))
            states.append({"key": "alarmActive", "value": alarm_active})

            # onOffState reflects whatever this specific sensor is actually
            # for, confirmed against the official sensorMountType enum
            # (door/window/garage/leak/none) rather than always defaulting
            # to motion regardless of what the sensor is mounted as - a
            # pure door/window/garage contact sensor would otherwise always
            # show "Clear" forever, since it has no motion component to
            # ever trigger. "none" is the general-purpose/motion case.
            #
            # "primaryState" device setting (3.12.8) can override this
            # auto-detection - e.g. a general-purpose ("none" mount)
            # sensor the user wants read as Alarm rather than Motion, or
            # a multi-purpose sensor where the mount type alone doesn't
            # capture what the user actually cares about. Defaults to
            # "auto" (the mount-type logic above, unchanged from 3.11.0).
            primary_override = dev.pluginProps.get("primaryState", "auto")
            if primary_override == "contact":
                onoff_value, onoff_ui = is_opened, ("Open" if is_opened else "Closed")
            elif primary_override == "leak":
                onoff_value, onoff_ui = alarm_active, ("Wet" if alarm_active else "Dry")
            elif primary_override == "motion":
                onoff_value, onoff_ui = motion_active, ("Detected" if motion_active else "Clear")
            elif primary_override == "alarm":
                onoff_value, onoff_ui = alarm_active, ("Alarm" if alarm_active else "Clear")
            elif mount_type in ("door", "window", "garage"):
                onoff_value, onoff_ui = is_opened, ("Open" if is_opened else "Closed")
            elif mount_type == "leak":
                onoff_value, onoff_ui = alarm_active, ("Wet" if alarm_active else "Dry")
            else:
                onoff_value, onoff_ui = motion_active, ("Detected" if motion_active else "Clear")
            states.append({"key": "onOffState", "value": onoff_value, "uiValue": onoff_ui})

            # Temperature / humidity / light from stats
            for key, src_key in (("temperature", "temperature"),
                                 ("humidity",    "humidity"),
                                 ("lightLevel",  "light")):
                src = stats.get(src_key, {}) if isinstance(stats, dict) else {}
                val = src.get("value") if isinstance(src, dict) else None
                if val is not None:
                    states.append({"key": key, "value": round(float(val), 1)})

            # Sensor name + type
            states.append({"key": "sensorName", "value": sensor.get("name", "")})
            sensor_type = sensor.get("modelKey") or sensor.get("type") or "sensor"
            states.append({"key": "sensorType", "value": sensor_type})
            states.append({"key": "firmwareVersion", "value": sensor.get("firmwareVersion", "Unknown")})

            dev.updateStatesOnServer(states)

            # Triggers
            if motion_active and not prev_motion:
                self._fire_trigger("sensorMotionDetected", dev)
            if is_opened != prev_contact:
                self._fire_trigger("sensorOpened" if is_opened else "sensorClosed", dev)
            if alarm_active and not prev_alarm:
                self._fire_trigger("sensorAlarmTriggered", dev)

        except Exception as e:
            self.logger.error(f"Sensor {sensor_id} state update error: {e}")
            if self.debug:
                self.logger.error(traceback.format_exc())

    # ------------------------------------------------------------------
    # State updates - Network devices (legacy)
    # ------------------------------------------------------------------

    def _update_netdev_states(self, controller_id, mac):
        controller = self.controllers.get(controller_id)
        if not controller or "network_api" not in controller:
            return
        try:
            d = controller["network_api"].get_device(mac)
            if d:
                self._apply_netdev_state(controller_id, mac, d)
        except Exception as e:
            self.logger.error(f"Net device {mac} state update error: {e}")

    def _apply_netdev_state(self, controller_id, mac, device):
        controller = self.controllers.get(controller_id)
        if not controller:
            return
        dev_id = controller["network_devices"].get(mac)
        if not dev_id or dev_id not in indigo.devices:
            return
        dev = indigo.devices[dev_id]

        # Debug: dump the raw device payload. Specifically needed right now
        # for the U5G/5G gateway - the roadmap has flagged since the
        # aiounifi research that cellular-specific fields (signal strength,
        # carrier, data usage) aren't in any reference checked and need a
        # live payload once real hardware exists to test against. Dumps
        # every networkDevice, not just the gateway, since we don't yet
        # know what `type` string Ubiquiti uses for it to filter on - same
        # "enable debug, capture, disable again" pattern as every other
        # payload diagnosis this session (cameras, doors, etc).
        if self.debug:
            try:
                dev_dump = json.dumps(device, default=str, indent=2)
                if len(dev_dump) > 4000:
                    dev_dump = dev_dump[:4000] + "\n…(truncated)"
                self.logger.debug(
                    f"[Network raw device payload: {device.get('name', mac)} "
                    f"| type={device.get('type', 'unknown')} | model={device.get('model', '?')}]\n{dev_dump}"
                )
            except Exception:
                self.logger.debug(f"[Network raw device payload - not JSON-serialisable: {type(device)}]")

        # Track previous connection state for triggers
        prev_connected = dev.states.get("isConnected", True)

        is_connected = device.get("state", 0) == 1
        is_disabled  = bool(device.get("disabled", False))

        states = [
            {"key": "status",          "value": "online" if is_connected else "offline"},
            {"key": "isConnected",     "value": is_connected},
            {"key": "adopted",         "value": is_connected},
            {"key": "disabled",        "value": is_disabled},
            {"key": "model",           "value": device.get("model", "Unknown")},
            {"key": "firmwareVersion", "value": device.get("version", "Unknown")},
            {"key": "ipAddress",       "value": device.get("ip", "Unknown")},
            {"key": "macAddress",      "value": device.get("mac", "Unknown")},
            {"key": "deviceType",      "value": device.get("type", "unknown")},
            {"key": "deviceName",      "value": device.get("name", "Unknown")},
            # onOffState reflects connectivity (online/offline), not
            # admin-disabled state - reconsidered from the 3.11.0 version
            # (which tied it to `disabled`) since connectivity is the more
            # generally useful at-a-glance signal for a network device, and
            # `disabled`'s meaning turned out to be unclear/inconsistent
            # for at least gateway-class devices (see the UDM Pro case:
            # reports disabled=true while fully online and passing
            # traffic - never confirmed why, and not worth tying the
            # primary indicator to a field whose meaning isn't reliable).
            # The separate `disabled` state above still tracks the raw
            # admin-enabled flag for anyone who wants it specifically.
            # Note for anyone with existing triggers on this device's
            # on/off: this changes what they actually fire on.
            {"key": "onOffState",      "value": is_connected,
             "uiValue": "Online" if is_connected else "Offline"},
        ]

        # Uptime
        uptime_sec = device.get("uptime")
        if isinstance(uptime_sec, (int, float)):
            d_ = int(uptime_sec) // 86400
            h_ = (int(uptime_sec) % 86400) // 3600
            m_ = (int(uptime_sec) % 3600) // 60
            states.append({"key": "uptime",        "value": f"{d_}d {h_}h {m_}m"})
            states.append({"key": "uptimeSeconds", "value": int(uptime_sec)})

        # System stats
        sys_stats = device.get("system-stats") or {}
        for key, src_key in (("cpuUsage", "cpu"), ("memoryUsage", "mem")):
            v = sys_stats.get(src_key)
            if v is not None:
                try:
                    states.append({"key": key, "value": round(float(str(v).strip()), 1)})
                except (ValueError, TypeError):
                    pass
        temps = sys_stats.get("temps") or []
        if isinstance(temps, list) and temps:
            try:
                states.append({"key": "temperature",
                               "value": round(float(str(temps[0]).strip()), 1)})
            except (ValueError, TypeError):
                pass

        # LED
        led_override = device.get("led_override", "default")
        states.append({"key": "ledEnabled",  "value": led_override == "on"})
        states.append({"key": "ledOverride", "value": led_override})
        states.append({"key": "updateAvailable", "value": bool(device.get("upgradable", False))})

        device_type = device.get("type", "")
        if device_type == "usw":
            port_table = device.get("port_table") or []
            states.append({"key": "portCount", "value": len(port_table)})
            poe_total = 0.0
            for p in port_table:
                try:
                    poe_total += float(p.get("poe_power") or 0)
                except (ValueError, TypeError):
                    pass
            states.append({"key": "poeConsumption", "value": round(poe_total, 2)})
        elif device_type == "uap":
            states.append({"key": "connectedClients", "value": int(device.get("num_sta", 0))})
            states.append({"key": "guestClients",     "value": int(device.get("guest-num_sta", 0))})
            states.append({"key": "userClients",      "value": int(device.get("user-num_sta", 0))})
            for radio in (device.get("radio_table") or []):
                if not isinstance(radio, dict):
                    continue
                name = (radio.get("name") or "").lower()
                channel = radio.get("channel", 0)
                key = None
                if "ng" in name or "2g" in name or name == "wifi0":
                    key = "channel2G"
                elif "na" in name or "5g" in name or name == "wifi1":
                    key = "channel5G"
                if key:
                    try:
                        states.append({"key": key, "value": int(channel)})
                    except (ValueError, TypeError):
                        states.append({"key": key, "value": 0})
            sat = device.get("satisfaction")
            if sat is not None:
                try:
                    states.append({"key": "satisfaction", "value": int(sat)})
                except (ValueError, TypeError):
                    pass
        elif device_type == "umbb":
            # 5G/LTE gateway (e.g. U5G Backup) - all fields below confirmed
            # against a real payload (3.11.6), not guessed, closing out the
            # "needs real hardware" roadmap item. mbb.sim is a list (one
            # entry per physical SIM slot) - this plugin only models the
            # active slot (sim[0]), same simplification as treating a
            # multi-WAN gateway as its primary link elsewhere in this file.
            mbb = device.get("mbb") or {}
            states.append({"key": "cellularMode",  "value": mbb.get("mode", "")})
            states.append({"key": "cellularState", "value": mbb.get("state", "")})

            sim_list = mbb.get("sim") or []
            sim = sim_list[0] if sim_list and isinstance(sim_list[0], dict) else {}
            states.append({"key": "cellularHasCarrier",  "value": bool(sim.get("has_carrier", False))})
            states.append({"key": "cellularCarrierName", "value": sim.get("spn", "")})
            states.append({"key": "cellularDataLimited", "value": bool(sim.get("data_limited", False))})
            states.append({"key": "cellularDataWarning", "value": bool(sim.get("data_warning", False))})
            states.append({"key": "cellularRejectReason", "value": sim.get("network_reject_text", "") or ""})
            for key, src_key in (("cellularDataRxMB", "rxbytes"), ("cellularDataTxMB", "txbytes")):
                try:
                    states.append({"key": key, "value": round(int(sim.get(src_key, 0)) / (1024 * 1024), 1)})
                except (ValueError, TypeError):
                    pass

            radio = mbb.get("radio") or {}
            states.append({"key": "cellularNetworkOperator", "value": radio.get("networkoperator", "")})
            states.append({"key": "cellularRAT",     "value": radio.get("rat_mode_active") or radio.get("rat", "")})
            states.append({"key": "cellularRoaming", "value": bool(radio.get("roaming", False))})
            for key, src_key in (("cellularSignalPercent", "signal_percent"),
                                 ("cellularRSRP", "rsrp"), ("cellularRSRQ", "rsrq"),
                                 ("cellularRSSI", "rssi"), ("cellularSNR", "snr")):
                v = radio.get(src_key)
                if v is not None:
                    try:
                        states.append({"key": key, "value": round(float(v), 1)})
                    except (ValueError, TypeError):
                        pass

        try:
            dev.updateStatesOnServer(states)
        except Exception as e:
            if self.debug:
                self.logger.debug(f"updateStatesOnServer({dev.name}) failed: {e}")

        # Fire connection-state triggers
        if bool(is_connected) != bool(prev_connected):
            self._fire_trigger("deviceOnline" if is_connected else "deviceOffline", dev)

    # ------------------------------------------------------------------
    # State updates - Ports (legacy)
    # ------------------------------------------------------------------

    def _update_port_states(self, controller_id, mac, port_num):
        controller = self.controllers.get(controller_id)
        if not controller or "network_api" not in controller:
            return
        try:
            d = controller["network_api"].get_device(mac)
            if d:
                self._apply_port_state(controller_id, mac, port_num, d)
        except Exception as e:
            self.logger.error(f"Port {mac}:{port_num} state update error: {e}")

    def _apply_port_state(self, controller_id, mac, port_num, device):
        controller = self.controllers.get(controller_id)
        if not controller:
            return
        dev_id = controller["ports"].get(f"{mac}:{port_num}")
        if not dev_id or dev_id not in indigo.devices:
            return
        dev = indigo.devices[dev_id]

        # Find port by index
        port = None
        for p in (device.get("port_table") or []):
            if p.get("port_idx") == port_num:
                port = p
                break
        if not port:
            return

        # Previous values for triggers
        prev_link_up     = bool(dev.states.get("up", False))
        prev_poe_enabled = bool(dev.states.get("poeEnabled", False))

        is_up        = bool(port.get("up", False))
        poe_enabled  = bool(port.get("poe_enable", False))
        poe_power    = port.get("poe_power", 0)
        speed        = port.get("speed", 0)
        port_name    = port.get("name") or f"Port {port_num}"
        poe_mode     = port.get("poe_mode", "off")

        # On/off state depends on the device's control mode
        control_mode = dev.pluginProps.get("controlMode", "poe")
        if control_mode == "poe":
            on_off = poe_enabled
        else:
            on_off = is_up  # link-state mode

        states = [
            {"key": "portName",     "value": port_name},
            {"key": "up",           "value": is_up},
            {"key": "poeEnabled",   "value": poe_enabled},
            {"key": "poeEnable",    "value": poe_enabled},
            {"key": "poeMode",      "value": poe_mode},
            {"key": "poePower",     "value": float(poe_power) if poe_power else 0.0},
            {"key": "speed",        "value": speed},
            # uiValue matches whichever concept this port's control mode
            # actually uses for on/off, rather than a generic on/off that
            # doesn't say if it means "PoE is powered" or "link is up".
            {"key": "onOffState",   "value": on_off,
             "uiValue": (("PoE On" if poe_enabled else "PoE Off") if control_mode == "poe"
                         else ("Link Up" if is_up else "Link Down"))},
        ]
        try:
            dev.updateStatesOnServer(states)
        except Exception as e:
            if self.debug:
                self.logger.debug(f"updateStatesOnServer({dev.name}) failed: {e}")

        # Fire triggers
        if is_up != prev_link_up:
            self._fire_trigger("portLinkUp" if is_up else "portLinkDown", dev)
        if poe_enabled != prev_poe_enabled:
            self._fire_trigger("portPoeOn" if poe_enabled else "portPoeOff", dev)

    # ------------------------------------------------------------------
    # State updates - Clients (legacy)
    # ------------------------------------------------------------------

    def _update_client_states(self, controller_id, mac):
        controller = self.controllers.get(controller_id)
        if not controller or "network_api" not in controller:
            return
        try:
            client = controller["network_api"].get_client(mac)
            dev_id = controller["clients"].get(mac)
            if not dev_id or dev_id not in indigo.devices:
                return
            dev = indigo.devices[dev_id]

            # Previous values for triggers
            prev_connected = bool(dev.states.get("isConnected", False))
            prev_blocked   = bool(dev.states.get("isBlocked",   False))

            if not client:
                # Client not currently visible - mark offline but don't error
                dev.updateStateOnServer("status", "offline")
                dev.updateStateOnServer("isConnected", False)
                dev.updateStateOnServer("onOffState", False)
                if prev_connected:
                    self._fire_trigger("clientDisconnected", dev)
                return

            is_connected = bool(client.get("is_online", True))  # /stat/sta returns active clients only
            is_blocked   = bool(client.get("blocked", False))

            states = [
                {"key": "status",       "value": "online" if is_connected else "offline"},
                {"key": "isConnected",  "value": is_connected},
                {"key": "isBlocked",    "value": is_blocked},
                {"key": "ipAddress",    "value": client.get("ip", "")},
                {"key": "hostname",     "value": client.get("hostname") or client.get("name", "")},
                {"key": "macAddress",   "value": client.get("mac", "")},
                {"key": "manufacturer", "value": client.get("oui", "")},
                {"key": "rxBytes",      "value": int(client.get("rx_bytes", 0))},
                {"key": "txBytes",      "value": int(client.get("tx_bytes", 0))},
                # onOffState combines two different reasons a client could
                # be "off" (blocked vs. simply not on the network) into one
                # boolean - the uiValue at least names which one it is
                # instead of a generic on/off that can't tell them apart.
                {"key": "onOffState",   "value": is_connected and not is_blocked,
                 "uiValue": ("Blocked" if is_blocked
                             else "Connected" if is_connected
                             else "Offline")},
            ]

            ssid = client.get("essid")
            if ssid:
                states.append({"key": "ssid", "value": ssid})
            ap_mac = client.get("ap_mac")
            if ap_mac:
                states.append({"key": "apMac", "value": ap_mac})

            try:
                dev.updateStatesOnServer(states)
            except Exception as e:
                if self.debug:
                    self.logger.debug(f"updateStatesOnServer({dev.name}) failed: {e}")

            # Fire triggers
            if is_connected != prev_connected:
                self._fire_trigger("clientConnected" if is_connected else "clientDisconnected", dev)
            if is_blocked != prev_blocked:
                self._fire_trigger("clientBlocked" if is_blocked else "clientUnblocked", dev)

        except Exception as e:
            self.logger.error(f"Client {mac} state update error: {e}")

    # ------------------------------------------------------------------
    # State updates - Doors (Access)
    # ------------------------------------------------------------------

    def _update_door_states(self, controller_id, door_id):
        controller = self.controllers.get(controller_id)
        if not controller or "access_api" not in controller:
            return
        try:
            door = controller["access_api"].get_door(door_id)
            dev_id = controller["doors"].get(door_id)
            if not dev_id or dev_id not in indigo.devices:
                return
            dev = indigo.devices[dev_id]

            # Previous state
            prev_locked = bool(dev.states.get("isLocked", True))

            if not door:
                dev.updateStateOnServer("status", "Not found")
                return

            is_locked = not bool(door.get("door_lock_relay_status") == "unlock")
            states = [
                {"key": "status",     "value": "locked" if is_locked else "unlocked"},
                {"key": "isLocked",   "value": is_locked},
                {"key": "doorName",   "value": door.get("name", "Unknown")},
                # uiValue is what actually controls the text shown in the
                # device list's State column for a relay device - confirmed
                # against docs.indigodomo.com's updateStateOnServer
                # reference. UiDisplayStateId (tried in 3.9.2) does nothing
                # here: relay-type devices always show onOffState in that
                # column regardless of what Devices.xml says, so the fix is
                # to make onOffState's own displayed text meaningful
                # instead of trying to redirect which state is shown.
                #
                # Boolean flipped in 3.11.8: this was "on = unlocked" (value
                # = not is_locked), which is backwards from what Indigo's
                # own icon selection expects for IsLockSubType devices -
                # confirmed by the icon actually shown ("Unlocked") not
                # matching the correct text ("Locked") while onState was
                # False. Indigo's convention is onState=True -> Locked.
                {"key": "onOffState", "value": is_locked,
                 "uiValue": "Locked" if is_locked else "Unlocked"},
            ]
            position = door.get("door_position_status")
            if position:
                states.append({"key": "doorState", "value": position})

            try:
                dev.updateStatesOnServer(states)
            except Exception as e:
                if self.debug:
                    self.logger.debug(f"updateStatesOnServer({dev.name}) failed: {e}")

            # Fire triggers
            if is_locked != prev_locked:
                self._fire_trigger("doorLocked" if is_locked else "doorUnlocked", dev)
        except Exception as e:
            self.logger.error(f"Door {door_id} state update error: {e}")

    # ------------------------------------------------------------------
    # State updates - Drive / Fan (legacy)
    # ------------------------------------------------------------------

    def _update_drive_states(self, controller_id):
        """Refresh the Drive controller device states, then cascade pool/disk updates.

        All UNAS data lives on the controller device itself; per-pool and
        per-disk detail is surfaced through child unifiDrivePool/unifiDriveDisk
        devices.
        """
        controller = self.controllers.get(controller_id)
        if not controller or "drive_api" not in controller:
            return
        api = controller["drive_api"]

        # Always fetch storage + system; firmware/app updates are best-effort
        try:
            storage  = api.get_storage_info() or {}
        except Exception as e:
            if self.debug:
                self.logger.debug(f"Drive storage fetch error: {e}")
            storage = {}

        try:
            system = api.get_system_info() or {}
        except Exception as e:
            if self.debug:
                self.logger.debug(f"Drive system fetch error: {e}")
            system = {}

        # Debug: dump the raw payloads so field-name mismatches are easy to
        # diagnose. Only fires when debug logging is enabled on the plugin.
        # JSON is truncated to ~4 KB per payload to avoid log spam.
        if self.debug:
            try:
                storage_dump = json.dumps(storage, default=str, indent=2)
                if len(storage_dump) > 4000:
                    storage_dump = storage_dump[:4000] + "\n…(truncated)"
                self.logger.debug(f"[Drive raw /storage payload]\n{storage_dump}")
            except Exception:
                self.logger.debug(f"[Drive raw /storage payload — not JSON-serialisable: {type(storage)}]")
            try:
                system_dump = json.dumps(system, default=str, indent=2)
                if len(system_dump) > 4000:
                    system_dump = system_dump[:4000] + "\n…(truncated)"
                self.logger.debug(f"[Drive raw /system payload]\n{system_dump}")
            except Exception:
                self.logger.debug(f"[Drive raw /system payload — not JSON-serialisable: {type(system)}]")

        # Cache the parsed payloads so pool/disk updates don't need to refetch
        controller["last_storage"]  = storage
        controller["last_system"]   = system
        controller["last_storage_at"] = time.time()

        # Pool list (extracted once, used for both aggregate counts and per-pool updates)
        pools = _drive_extract_pools(storage)
        controller["last_pools"] = pools

        # ---- Mirror at-a-glance info to the Drive Controller device.
        # We only write states the controller device type actually declares.
        try:
            controller_dev = controller.get("device")
            if controller_dev is not None and controller_dev.deviceTypeId == "unifiDriveController":
                declared = set(controller_dev.states.keys())
                ctrl_states = []

                # Simple text fields from /api/system
                if "hostname" in declared:
                    v = _drive_first_text(system, ("name", "hostname", "deviceName"))
                    if v: ctrl_states.append({"key": "hostname", "value": v})

                if "macAddress" in declared:
                    v = _drive_first_text(system, ("mac", "macAddress", "mac_address"))
                    if v: ctrl_states.append({"key": "macAddress", "value": v})

                # Model lives under hardware.shortname (e.g. "UNASPRO")
                if "model" in declared:
                    hw = system.get("hardware") if isinstance(system, dict) else None
                    model = None
                    if isinstance(hw, dict):
                        model = _drive_first_text(hw, ("shortname", "model", "type", "name"))
                    if not model:
                        model = _drive_first_text(system, ("model",))
                    if model:
                        ctrl_states.append({"key": "model", "value": model})

                if "deviceState" in declared:
                    v = _drive_first_text(system, ("deviceState", "device_state", "status"))
                    if v: ctrl_states.append({"key": "deviceState", "value": v})

                if "cloudConnected" in declared:
                    v = system.get("cloudConnected") if isinstance(system, dict) else None
                    if v is not None:
                        ctrl_states.append({"key": "cloudConnected", "value": bool(v)})

                if "hasInternet" in declared:
                    v = system.get("hasInternet") if isinstance(system, dict) else None
                    if v is not None:
                        ctrl_states.append({"key": "hasInternet", "value": bool(v)})

                # Storage aggregates — try top-level keys first, fall back to
                # summing across pools.
                total_b = _drive_first_number(storage, _DRIVE_AGG_CAPACITY_KEYS)
                used_b  = _drive_first_number(storage, _DRIVE_AGG_USAGE_KEYS)
                avail_b = _drive_first_number(storage, _DRIVE_AGG_AVAILABLE_KEYS)
                if total_b is None and pools:
                    total_b = sum((_drive_first_number(p, _DRIVE_CAPACITY_KEYS) or 0) for p in pools) or None
                if used_b is None and pools:
                    used_b = sum((_drive_first_number(p, _DRIVE_USAGE_KEYS) or 0) for p in pools) or None
                if avail_b is None and total_b is not None and used_b is not None:
                    avail_b = max(0, total_b - used_b)
                total_gb = _drive_bytes_to_gb(total_b)
                used_gb  = _drive_bytes_to_gb(used_b)
                avail_gb = _drive_bytes_to_gb(avail_b)
                if "totalCapacity" in declared and total_gb is not None:
                    ctrl_states.append({"key": "totalCapacity", "value": total_gb})
                if "usedSpace"     in declared and used_gb  is not None:
                    ctrl_states.append({"key": "usedSpace",     "value": used_gb})
                if "freeSpace"     in declared and avail_gb is not None:
                    ctrl_states.append({"key": "freeSpace",     "value": avail_gb})
                if "usagePercent"  in declared and total_gb and total_gb > 0 and used_gb is not None:
                    ctrl_states.append({"key": "usagePercent",
                                        "value": round((used_gb / total_gb) * 100, 1)})

                # Derive overallStatus from the pool statuses since the storage
                # endpoint doesn't expose a top-level overall status. If all
                # pools healthy -> "ok"; any not -> "problem".
                if "overallStatus" in declared:
                    explicit = _drive_first_text(storage, ("overallStatus", "overall_status", "status"))
                    if explicit:
                        ctrl_states.append({"key": "overallStatus", "value": explicit})
                    elif pools:
                        any_problem = any(
                            not _drive_is_healthy(_drive_first_text(p, ("status", "state")))
                            for p in pools
                        )
                        ctrl_states.append({"key": "overallStatus",
                                            "value": "problem" if any_problem else "ok"})
                    else:
                        ctrl_states.append({"key": "overallStatus", "value": "unknown"})

                if "poolCount" in declared:
                    ctrl_states.append({"key": "poolCount", "value": len(pools)})

                # Disks (top-level array). Build once, reuse for counts.
                all_disks = _drive_extract_disks(storage)
                if "diskCount" in declared:
                    ctrl_states.append({"key": "diskCount", "value": len(all_disks)})
                if "atRiskDiskCount" in declared:
                    at_risk = sum(
                        1 for d in all_disks
                        if d.get("atRisk") or d.get("at_risk") or
                           (isinstance(d.get("riskReasons"), list) and d["riskReasons"]) or
                           _drive_is_at_risk(_drive_first_text(d, ("state", "status", "health")))
                    )
                    ctrl_states.append({"key": "atRiskDiskCount", "value": at_risk})
                if "averageDiskTemperature" in declared:
                    temps = [
                        _drive_first_number(d, ("temperature", "temp", "temperatureC", "temperature_c"))
                        for d in all_disks
                    ]
                    temps = [t for t in temps if t is not None]
                    if temps:
                        ctrl_states.append({"key": "averageDiskTemperature",
                                            "value": round(sum(temps) / len(temps), 1)})

                # SSH fan control deployment status (best effort — don't fail
                # the poll if SSH is offline or slow). Only checks every 10
                # poll cycles to keep network traffic down.
                ssh = controller.get("drive_ssh")
                if ssh is not None and ("fanControlDeployed" in declared or
                                        "fanControlActiveMode" in declared):
                    last_check = controller.get("last_ssh_check_at", 0)
                    if time.time() - last_check > 300:  # at most every 5 min
                        try:
                            deployed = ssh.is_deployed()
                            ctrl_states.append({"key": "fanControlDeployed", "value": bool(deployed)})
                            if deployed:
                                active_mode = ssh.get_active_mode()
                                ctrl_states.append({"key": "fanControlActiveMode", "value": active_mode})
                            else:
                                ctrl_states.append({"key": "fanControlActiveMode", "value": ""})
                            controller["last_ssh_check_at"] = time.time()
                        except UnifiSSHError as e:
                            if self.debug:
                                self.logger.debug(f"SSH deployment check failed: {e}")

                if ctrl_states:
                    try:
                        controller_dev.updateStatesOnServer(ctrl_states)
                    except Exception as e:
                        if self.debug:
                            self.logger.debug(f"Controller updateStatesOnServer failed: {e}")
        except Exception as e:
            if self.debug:
                self.logger.debug(f"Controller mirror error: {e}")

        # ---- Cascade: pool devices and disk devices ----
        controller["pools_by_key"] = {}
        controller["disks_by_key"] = {}

        # Build a pool_id -> (pool_key, pool_index) lookup so we can attribute
        # each disk back to its parent pool when iterating top-level disks.
        pool_id_to_key = {}
        for idx, pool in enumerate(pools):
            pkey = _drive_pool_key(pool, idx)
            controller["pools_by_key"][pkey] = {"data": pool, "index": idx}
            for id_field in ("id", "guid", "poolGuid", "uuid"):
                pid = pool.get(id_field)
                if pid:
                    pool_id_to_key[pid] = (pkey, idx)
                    break

        all_disks = _drive_extract_disks(storage)
        for didx, disk in enumerate(all_disks):
            dkey = _drive_disk_key(disk, didx)
            pool_id  = disk.get("poolId") or disk.get("pool_id") or disk.get("poolGuid")
            pkey, pidx = pool_id_to_key.get(pool_id, ("", 0))
            controller["disks_by_key"][dkey] = {
                "data": disk, "index": didx, "pool_index": pidx, "pool_key": pkey,
            }

        # Update all pool devices we know about
        for pkey, pool_dev_id in (controller.get("pool_devices") or {}).items():
            self._update_pool_states(controller_id, pkey)
        for dkey, disk_dev_id in (controller.get("disk_devices") or {}).items():
            self._update_disk_states(controller_id, dkey)

    def _update_pool_states(self, controller_id, pool_key):
        """Refresh a single unifiDrivePool device's states from cached pool data."""
        controller = self.controllers.get(controller_id)
        if not controller:
            return
        pool_entry = (controller.get("pools_by_key") or {}).get(pool_key)
        dev_id = (controller.get("pool_devices") or {}).get(pool_key)
        if not dev_id or dev_id not in indigo.devices:
            return
        dev = indigo.devices[dev_id]
        if not pool_entry:
            try:
                dev.updateStatesOnServer([
                    {"key": "status",     "value": "Not found"},
                    {"key": "poolStatus", "value": "missing"},
                ])
            except Exception:
                pass
            return

        pool  = pool_entry["data"]
        index = pool_entry["index"]
        states = []

        name = _drive_pool_name(pool, index)
        states.append({"key": "poolName", "value": name})

        status = _drive_first_text(pool, ("status", "state", "health")) or "unknown"
        states.append({"key": "poolStatus", "value": status})
        states.append({"key": "status",     "value": "Connected"})

        cap_b   = _drive_first_number(pool, _DRIVE_CAPACITY_KEYS)
        used_b  = _drive_first_number(pool, _DRIVE_USAGE_KEYS)
        avail_b = _drive_first_number(pool, _DRIVE_AVAILABLE_KEYS)
        # If "available" not in payload but we have cap and used, derive it.
        if avail_b is None and cap_b is not None and used_b is not None:
            avail_b = max(0, cap_b - used_b)

        cap_gb   = _drive_bytes_to_gb(cap_b)
        used_gb  = _drive_bytes_to_gb(used_b)
        avail_gb = _drive_bytes_to_gb(avail_b)

        if cap_gb   is not None: states.append({"key": "capacity",  "value": cap_gb})
        if used_gb  is not None: states.append({"key": "used",      "value": used_gb})
        if avail_gb is not None: states.append({"key": "available", "value": avail_gb})
        if cap_gb and cap_gb > 0 and used_gb is not None:
            states.append({"key": "usagePercent",
                           "value": round((used_gb / cap_gb) * 100, 1)})

        # RAID level: prefer 'preferLevel' (raid5/raid6/etc) over 'type' (lvm)
        raid = (pool.get("preferLevel") or
                _drive_first_text(pool, _DRIVE_RAID_KEYS) or
                "unknown")
        states.append({"key": "raidLevel", "value": raid})

        # Disks for THIS pool — filter the top-level disk array by poolId.
        # The cached storage payload is held on the controller.
        cached_storage = controller.get("last_storage", {})
        disks = _drive_extract_disks_for_pool(cached_storage, pool)
        states.append({"key": "driveCount", "value": len(disks)})

        at_risk = sum(
            1 for d in disks
            if d.get("atRisk") or d.get("at_risk") or
               (isinstance(d.get("riskReasons"), list) and d["riskReasons"]) or
               _drive_is_at_risk(_drive_first_text(d, ("status", "state", "health")))
        )
        states.append({"key": "atRiskDriveCount", "value": at_risk})

        temps = [
            _drive_first_number(d, ("temperature", "temp", "temperatureC")) for d in disks
        ]
        temps = [t for t in temps if t is not None]
        if temps:
            states.append({"key": "averageDriveTemperature",
                           "value": round(sum(temps) / len(temps), 1)})

        # Rebuild/sync progress: at pool level OR in raidGroups[0].progress
        rebuild = _drive_first_number(pool, ("rebuildProgress", "rebuild_progress", "rebuildPercent"))
        if rebuild is None:
            raid_groups = pool.get("raidGroups")
            if isinstance(raid_groups, list) and raid_groups:
                rebuild = _drive_first_number(raid_groups[0], ("progress", "rebuildProgress"))
        if rebuild is not None:
            states.append({"key": "rebuildProgress", "value": round(rebuild, 1)})

        sync = _drive_first_number(pool, ("syncProgress", "sync_progress", "syncPercent",
                                          "resyncProgress", "scrubProgress"))
        if sync is not None:
            states.append({"key": "syncProgress", "value": round(sync, 1)})

        states.append({"key": "poolProblem",
                       "value": not _drive_is_healthy(status)})
        states.append({"key": "maintenanceActive",
                       "value": bool(pool.get("maintenance") or pool.get("inMaintenance"))})

        try:
            dev.updateStatesOnServer(states)
        except Exception as e:
            if self.debug:
                self.logger.debug(f"Pool update for {dev.name} failed: {e}")

    def _update_disk_states(self, controller_id, disk_key):
        """Refresh a single unifiDriveDisk device's states from cached disk data."""
        controller = self.controllers.get(controller_id)
        if not controller:
            return
        disk_entry = (controller.get("disks_by_key") or {}).get(disk_key)
        dev_id = (controller.get("disk_devices") or {}).get(disk_key)
        if not dev_id or dev_id not in indigo.devices:
            return
        dev = indigo.devices[dev_id]
        if not disk_entry:
            try:
                dev.updateStatesOnServer([
                    {"key": "status",     "value": "Not found"},
                    {"key": "diskStatus", "value": "missing"},
                ])
            except Exception:
                pass
            return

        disk      = disk_entry["data"]
        index     = disk_entry["index"]
        pool_idx  = disk_entry.get("pool_index", 0)
        pool_key  = disk_entry.get("pool_key", "")

        # Get the parent pool name for display
        pool_name = "Unknown"
        if pool_key:
            pe = (controller.get("pools_by_key") or {}).get(pool_key)
            if pe:
                pool_name = _drive_pool_name(pe["data"], pe["index"])

        states = []
        states.append({"key": "status",     "value": "Connected"})
        states.append({"key": "diskName",   "value": _drive_disk_name(disk, index, pool_idx)})
        states.append({"key": "poolName",   "value": pool_name})

        # 'state' is the canonical field on current UNAS firmware (e.g. "optimal")
        # — older firmware may use 'status' or 'health'. _drive_first_text picks
        # the first that exists.
        d_status = _drive_first_text(disk, ("state", "status", "health")) or "unknown"
        states.append({"key": "diskStatus", "value": d_status})

        # At-risk detection: prefer the explicit riskReasons array if present
        # (current firmware), fall back to status-string matching for older.
        risk_reasons = disk.get("riskReasons")
        is_at_risk = bool(
            disk.get("atRisk") or disk.get("at_risk") or
            (isinstance(risk_reasons, list) and risk_reasons) or
            _drive_is_at_risk(d_status)
        )
        states.append({"key": "atRisk", "value": is_at_risk})

        temp = _drive_first_number(disk, ("temperature", "temp", "temperatureC", "temperature_c"))
        if temp is not None:
            states.append({"key": "temperature", "value": round(temp, 1)})

        poh = _drive_first_number(disk, ("powerOnHours", "power_on_hours", "powerOnTime"))
        if poh is not None:
            states.append({"key": "powerOnHours", "value": int(poh)})

        model = _drive_first_text(disk, ("model", "modelName", "device"))
        if model: states.append({"key": "model", "value": model})

        serial = _drive_first_text(disk, ("serialNumber", "serial", "serial_number"))
        if serial: states.append({"key": "serial", "value": serial})

        cap_b = _drive_first_number(disk, _DRIVE_CAPACITY_KEYS)
        cap_gb = _drive_bytes_to_gb(cap_b)
        if cap_gb is not None:
            states.append({"key": "capacityGB", "value": cap_gb})

        try:
            dev.updateStatesOnServer(states)
        except Exception as e:
            if self.debug:
                self.logger.debug(f"Disk update for {dev.name} failed: {e}")

    def _check_fan_control_deployment(self, controller_id):
        """Once a day, verify the SSH-deployed fan control is still installed
        on the UNAS. If it's gone (most likely because a UniFi OS update wiped
        the filesystem), redeploy silently.

        Skipped entirely when:
          - SSH isn't enabled / no SSH helper
          - The user has unticked 'Auto-redeploy after firmware updates'
          - We've never deployed it (first-time install — wait for the user
            to click the manual button)
          - We checked less than 24 hours ago
        """
        controller = self.controllers.get(controller_id)
        if not controller:
            return
        ssh = controller.get("drive_ssh")
        if ssh is None:
            return

        cdev = controller.get("device")
        if cdev is None:
            return

        # Respect user's opt-out
        if not bool(cdev.pluginProps.get("autoRedeployFanControl", True)):
            return

        # Only check once per 24h
        last = controller.get("last_fan_redeploy_check_at", 0)
        now  = time.time()
        if now - last < 86400:
            return
        controller["last_fan_redeploy_check_at"] = now

        # Only consider redeploying if we previously deployed (fanControlDeployed
        # state is true). First-time setup must be done manually.
        try:
            previously_deployed = bool(cdev.states.get("fanControlDeployed", False))
        except Exception:
            previously_deployed = False
        if not previously_deployed:
            return

        # Check if still installed
        try:
            if ssh.is_deployed():
                if self.debug:
                    self.logger.debug(f"{cdev.name}: Fan control deployment intact.")
                return
        except UnifiSSHError as e:
            if self.debug:
                self.logger.debug(f"{cdev.name}: Fan control deployment check failed: {e}")
            return

        # Marker missing — redeploy silently.
        if not self.fan_script_content or not self.fan_service_content:
            self.logger.error(
                f"{cdev.name}: Fan control needs redeploy but bundled assets "
                f"are missing — cannot proceed automatically."
            )
            return

        self.logger.warning(
            f"{cdev.name}: Fan control was deployed but is no longer present "
            f"on the UNAS (likely after a UniFi OS update). Redeploying…"
        )
        try:
            ssh.deploy_fan_control(self.fan_script_content, self.fan_service_content)
            status      = ssh.get_service_status()
            active_mode = ssh.get_active_mode()
            self.logger.info(
                f"{cdev.name}: Fan control redeployed automatically. "
                f"Service: {status}, mode: {active_mode}"
            )
            try:
                cdev.updateStatesOnServer([
                    {"key": "fanControlDeployed",   "value": True},
                    {"key": "fanControlActiveMode", "value": active_mode},
                ])
            except Exception:
                pass
        except UnifiSSHError as e:
            self.logger.error(f"{cdev.name}: Auto-redeploy failed: {e}")
        except Exception as e:
            self.logger.error(f"{cdev.name}: Unexpected auto-redeploy error: {e}")
            if self.debug:
                self.logger.error(traceback.format_exc())

    def _update_fan_states(self, controller_id):
        """Refresh the driveFan device's states.

        When SSH fan control is deployed on the UNAS, that is the source of
        truth for the active mode (because the UniFi REST API can't be told
        about our SSH-deployed script and continues reporting whatever native
        profile the user last picked in the UI). When SSH is not deployed,
        we fall back to the REST API value."""
        controller = self.controllers.get(controller_id)
        if not controller or "drive_api" not in controller:
            return
        dev_id = controller.get("fan_device")
        if not dev_id or dev_id not in indigo.devices:
            return
        dev = indigo.devices[dev_id]
        try:
            fan = controller["drive_api"].get_fan_info() or {}

            # Debug: surface the raw fan payload so the field names / shapes
            # for fan-control endpoints can be diagnosed.
            if self.debug:
                try:
                    fan_dump = json.dumps(fan, default=str, indent=2)
                    if len(fan_dump) > 2000:
                        fan_dump = fan_dump[:2000] + "\n…(truncated)"
                    self.logger.debug(f"[Drive raw /fan-control payload]\n{fan_dump}")
                except Exception:
                    self.logger.debug(f"[Drive raw /fan-control payload — type {type(fan).__name__}]")

            # RPM: the REST fan-control payload has no RPM field at all
            # (confirmed against a real payload - this lookup is a no-op
            # safety net in case a future firmware version ever adds one,
            # not a real source today). The actual source is SSH - see
            # below, once `ssh` is resolved - reading the hwmon tachometer
            # inputs directly (confirmed present and returning real values
            # on this hardware).
            rpm = _drive_first_number(fan, ("rpm", "currentRpm", "fanRpm")) or 0
            per_fan_rpm = {}  # {1: rpm, 2: rpm, ...} - populated below if SSH is deployed

            # Source of truth for display mode: SSH conf > API profile
            display_mode = None
            source       = "api"
            ssh = controller.get("drive_ssh")
            if ssh is not None:
                try:
                    ssh_deployed = ssh.is_deployed()  # one check, reused below
                except Exception:
                    ssh_deployed = False

                if ssh_deployed:
                    try:
                        rpms = ssh.get_fan_rpms()
                        if rpms:
                            # fanRPM keeps the single fastest-fan figure for
                            # anyone already using it as an at-a-glance
                            # value, but each fan's own reading is exposed
                            # individually too (fan1RPM.. fan4RPM) rather
                            # than only the summary - asked for directly
                            # rather than assumed.
                            rpm = max(rpms.values())
                            for path, value in rpms.items():
                                m = re.search(r"fan(\d+)_input", path)
                                if m:
                                    per_fan_rpm[int(m.group(1))] = value
                    except Exception as e:
                        if self.debug:
                            self.logger.debug(f"Fan RPM read failed: {e}")

                    try:
                        # We trust the deployed config — even if SSH is briefly
                        # unreachable, the controller-level state cache reflects
                        # what we last set, which matches what's running on the
                        # UNAS (the systemd script reads the same conf file).
                        ssh_mode = ssh.get_active_mode()
                        # Map ssh mode -> canonical display name
                        mode_map = {
                            "quiet":   "Quiet",
                            "balance": "Balance",
                            "cooling": "Cooling",
                            "default": "Default",
                        }
                        display_mode = mode_map.get(ssh_mode.lower(), "Default")
                        source = "ssh"
                        if self.debug:
                            self.logger.debug(f"Fan mode source: SSH conf = {ssh_mode}")
                    except Exception as e:
                        if self.debug:
                            self.logger.debug(f"SSH fan mode read failed, falling back to API: {e}")

            if display_mode is None:
                # API fallback. Map 'profile' to one of our canonical names.
                if not fan:
                    dev.updateStateOnServer("status", "Not available")
                    return
                raw_profile = (_drive_first_text(fan, ("profile", "currentProfile", "mode",
                                                        "fanMode", "fan_mode")) or "").lower()
                display_mode = "Balance"
                for canonical, api_values in DRIVE_FAN_MODE_API_VALUES.items():
                    if raw_profile in api_values:
                        display_mode = canonical
                        break

            # speedIndex is speedcontrol's native state (0=Default,
            # 1=Quiet, 2=Balance, 3=Cooling - matches Indigo's default
            # speedIndexCount of 4 exactly, no XML override needed). The
            # uiValue is what actually shows in the device list's State
            # column for a speedcontrol device - same mechanism confirmed
            # for accessDoor's onOffState (3.9.3) and documented
            # identically for speedIndex.ui in the official Device Base
            # Class reference. Replaces the old 0/50/100% brightness-anchor
            # scheme (3.9.x and earlier), which was a semantic mismatch:
            # this is a fixed set of named modes, not a continuous level.
            mode_to_index = {"Default": 0, "Quiet": 1, "Balance": 2, "Cooling": 3}
            states = [
                {"key": "fanMode",         "value": display_mode},
                {"key": "fanRPM",          "value": int(rpm)},
                {"key": "status",
                 "value": f"{display_mode} ({int(rpm)} RPM)" if rpm else display_mode},
                {"key": "speedIndex",
                 "value": mode_to_index.get(display_mode, 2),
                 "uiValue": display_mode},
            ]
            # Individual per-fan readings (fan1RPM..fan4RPM), not just the
            # fanRPM summary - only the ones hwmon actually reported get
            # overwritten, so a fan header this board doesn't have simply
            # keeps whatever default Indigo gives an unwritten Number state
            # (0) rather than us asserting a specific value for hardware we
            # have no reading for.
            for fan_num, fan_value in per_fan_rpm.items():
                if 1 <= fan_num <= 4:
                    states.append({"key": f"fan{fan_num}RPM", "value": int(fan_value)})

            dev.updateStatesOnServer(states)
        except Exception as e:
            if self.debug:
                self.logger.debug(f"Fan state update error: {e}")

    # ------------------------------------------------------------------
    # Test Connection (callback from controller config UI)
    # ------------------------------------------------------------------

    def testConnection(self, valuesDict, typeId="", devId=0):
        """Test each enabled API path independently and report results."""
        try:
            address     = valuesDict.get("address", "").strip()
            port        = int(valuesDict.get("port", DEFAULT_HTTPS_PORT) or DEFAULT_HTTPS_PORT)
            access_port = int(valuesDict.get("accessPort", DEFAULT_ACCESS_PORT) or DEFAULT_ACCESS_PORT)
            verify_ssl  = bool(valuesDict.get("verifySSL", False))
            site        = valuesDict.get("site", "default") or "default"

            net_key     = valuesDict.get("networkApiKey", "").strip()
            # Protect uses the same key as Network (unified Integrations page)
            protect_key = net_key
            # Access uses a separate token generated inside the Access app itself
            access_key  = valuesDict.get("accessApiKey", "").strip()
            user        = valuesDict.get("legacyUsername", "").strip()
            pwd         = valuesDict.get("legacyPassword", "")

            if not address:
                self.logger.error("Test failed: Controller address is empty")
                return valuesDict

            self.logger.info(f"=== Testing connection to {address}:{port} ===")

            # Network Integration
            if net_key:
                try:
                    net = UnifiNetworkAPI(address, port, net_key, "", "",
                                          site=site, verify_ssl=verify_ssl,
                                          logger=self.logger, debug=self.debug)
                    net.test_integration()
                    self.logger.info("✓ Network Integration API: OK")
                except UnifiAuthError as e:
                    self.logger.error(f"✗ Network Integration API: auth failed - {e}")
                except Exception as e:
                    self.logger.warning(f"✗ Network Integration API: {e}")
            else:
                self.logger.info("- Network Integration API: skipped (no key)")

            # Network Legacy
            if user and pwd:
                try:
                    net = UnifiNetworkAPI(address, port, "", user, pwd,
                                          site=site, verify_ssl=verify_ssl,
                                          logger=self.logger, debug=self.debug)
                    net.test_legacy()
                    self.logger.info("✓ Network Legacy auth: OK")
                except UnifiAuthError as e:
                    self.logger.error(f"✗ Network Legacy auth: {e}")
                except Exception as e:
                    self.logger.warning(f"✗ Network Legacy auth: {e}")
            else:
                self.logger.info("- Network Legacy auth: skipped (no username/password)")

            # Protect (uses the shared API key)
            if protect_key:
                try:
                    p = UnifiProtectAPI(address, port, protect_key,
                                        username=user, password=pwd,
                                        verify_ssl=verify_ssl,
                                        logger=self.logger, debug=self.debug)
                    p.test_connection()
                    p.refresh_cache()
                    self.logger.info(f"✓ Protect API: OK ({len(p.get_cameras())} cameras, "
                                     f"{len(p.get_sensors())} sensors)")
                    if user and pwd:
                        if p.legacy_logged_in:
                            self.logger.info(
                                "✓ Protect legacy bootstrap: OK "
                                "(firmware/uptime/voltage/recording/IR mode will populate)"
                            )
                        else:
                            self.logger.warning(
                                "✗ Protect legacy bootstrap: did not authenticate - "
                                "those monitoring fields will stay blank"
                            )
                except UnifiAuthError as e:
                    self.logger.error(f"✗ Protect API: auth failed - {e}")
                except Exception as e:
                    self.logger.warning(f"✗ Protect API: {e}")
            else:
                self.logger.info("- Protect API: skipped (no key)")

            # Access (uses a separate token generated inside the Access app,
            # not the unified Integrations page; Bearer auth on port 12445)
            if access_key:
                try:
                    a = UnifiAccessAPI(address, access_port, access_key,
                                       verify_ssl=verify_ssl,
                                       logger=self.logger, debug=self.debug)
                    doors = a.get_doors()
                    self.logger.info(f"✓ Access API: OK ({len(doors)} doors on port {access_port})")
                except UnifiAuthError as e:
                    self.logger.error(f"✗ Access API: auth failed - {e}")
                except Exception as e:
                    self.logger.warning(f"✗ Access API: {e}")
            else:
                self.logger.info("- Access API: skipped (no token)")

            self.logger.info("=== Test complete ===")
            return valuesDict
        except Exception as e:
            self.logger.error(f"Test error: {e}")
            if self.debug:
                self.logger.error(traceback.format_exc())
            return valuesDict

    def testDriveConnection(self, valuesDict, typeId="", devId=0):
        """Test the UNAS connection from the Drive controller config UI."""
        try:
            address    = valuesDict.get("address", "").strip()
            try:
                port = int(valuesDict.get("port", DEFAULT_HTTPS_PORT) or DEFAULT_HTTPS_PORT)
            except (TypeError, ValueError):
                port = DEFAULT_HTTPS_PORT
            verify_ssl = bool(valuesDict.get("verifySSL", False))
            api_key    = valuesDict.get("driveApiKey", "").strip()
            user       = valuesDict.get("legacyUsername", "").strip()
            pwd        = valuesDict.get("legacyPassword", "")

            if not address:
                self.logger.error("UNAS test failed: address is empty")
                return valuesDict

            self.logger.info(f"=== Testing UNAS connection to {address}:{port} ===")

            if api_key:
                try:
                    d = UnifiDriveAPI(address, port, api_key=api_key,
                                      verify_ssl=verify_ssl,
                                      logger=self.logger, debug=self.debug)
                    d.test_connection()
                    self.logger.info("✓ UNAS API key auth: OK")
                except UnifiAuthError as e:
                    self.logger.error(f"✗ UNAS API key auth failed: {e}")
                except Exception as e:
                    self.logger.warning(f"✗ UNAS API key auth: {e}")
            else:
                self.logger.info("- UNAS API key: skipped (no key configured)")

            if user and pwd:
                try:
                    d = UnifiDriveAPI(address, port, api_key="",
                                      username=user, password=pwd,
                                      verify_ssl=verify_ssl,
                                      logger=self.logger, debug=self.debug)
                    d.test_connection()
                    self.logger.info("✓ UNAS legacy auth: OK")
                except UnifiAuthError as e:
                    self.logger.error(f"✗ UNAS legacy auth failed: {e}")
                except Exception as e:
                    self.logger.warning(f"✗ UNAS legacy auth: {e}")
            else:
                self.logger.info("- UNAS legacy auth: skipped (no username/password)")

            self.logger.info("=== UNAS test complete ===")
            return valuesDict
        except Exception as e:
            self.logger.error(f"UNAS test error: {e}")
            if self.debug:
                self.logger.error(traceback.format_exc())
            return valuesDict

    # ------------------------------------------------------------------
    # Helpers for trigger / action UI lists
    # ------------------------------------------------------------------

    def getControllerList(self, filter="", valuesDict=None, typeId="", targetId=0):
        """Return list of controller devices for action/event UI menus."""
        out = []
        for d in indigo.devices.iter("self.unifiController"):
            out.append((str(d.id), d.name))
        return out

    def getDriveControllerList(self, filter="", valuesDict=None, typeId="", targetId=0):
        """Return list of UNAS Drive controller devices."""
        out = []
        for d in indigo.devices.iter("self.unifiDriveController"):
            out.append((str(d.id), d.name))
        return out

    def getCameraList(self, filter="", valuesDict=None, typeId="", targetId=0):
        out = [("", "Any Camera")]
        for d in indigo.devices.iter("self.protectCamera"):
            out.append((str(d.id), d.name))
        return out

    def getPackageCameraParentList(self, filter="", valuesDict=None, typeId="", targetId=0):
        """Dynamic menu: existing protectCamera devices to link a Package
        Camera device to. `hasPackageCamera` is confirmed real but
        legacy-bootstrap-only data (not in the official schema) - filters
        to cameras confirmed to actually have one where that data is
        available, and falls back to listing every camera (so the user
        can still pick by their own knowledge of the hardware) when it
        isn't, rather than returning nothing."""
        out = []
        try:
            controller_id = int((valuesDict or {}).get("controllerId", 0) or 0)
            controller = self.controllers.get(controller_id)
            bootstrap = {}
            if controller and "protect_api" in controller:
                bootstrap = controller["protect_api"].get_bootstrap_raw() or {}
            bootstrap_cams = {c.get("id"): c for c in (bootstrap.get("cameras") or []) if isinstance(c, dict)}

            for d in indigo.devices.iter("self.protectCamera"):
                if controller_id and d.pluginProps.get("controllerId") != str(controller_id):
                    continue
                cam_id = d.pluginProps.get("cameraId", "")
                bcam = bootstrap_cams.get(cam_id)
                if bcam is not None and not bcam.get("hasPackageCamera", False):
                    continue  # confirmed NOT to have one - skip
                out.append((str(d.id), d.name))
        except Exception as e:
            if self.debug:
                self.logger.debug(f"getPackageCameraParentList error: {e}")
        if not out:
            out = [("", "(no cameras found - create the parent camera device first, "
                        "then pick the controller above)")]
        return out

    def getSensorList(self, filter="", valuesDict=None, typeId="", targetId=0):
        out = [("", "Any Sensor")]
        for d in indigo.devices.iter("self.protectSensor"):
            out.append((str(d.id), d.name))
        return out

    def getFirewallPolicyList(self, filter="", valuesDict=None, typeId="", targetId=0):
        """Dynamic menu: non-predefined (user-created) firewall policies
        on the selected controller. Ubiquiti's own built-in policies are
        deliberately excluded - toggling those from automation is rarely
        useful, and there are usually dozens of them."""
        out = []
        try:
            controller_id = int((valuesDict or {}).get("controllerId", 0) or 0)
            controller = self.controllers.get(controller_id)
            if controller and "network_api" in controller:
                for p in controller["network_api"].get_firewall_policies():
                    if not p.get("predefined", False):
                        out.append((p.get("_id", ""), p.get("name", "Unnamed")))
        except Exception as e:
            if self.debug:
                self.logger.debug(f"getFirewallPolicyList error: {e}")
        if not out:
            out = [("", "(none found - pick the controller above first)")]
        return out

    def getTrafficRouteList(self, filter="", valuesDict=None, typeId="", targetId=0):
        """Dynamic menu: traffic routes (policy-based routing) on the
        selected controller."""
        out = []
        try:
            controller_id = int((valuesDict or {}).get("controllerId", 0) or 0)
            controller = self.controllers.get(controller_id)
            if controller and "network_api" in controller:
                for r in controller["network_api"].get_traffic_routes():
                    label = r.get("description") or "(unnamed route)"
                    out.append((r.get("_id", ""), label))
        except Exception as e:
            if self.debug:
                self.logger.debug(f"getTrafficRouteList error: {e}")
        if not out:
            out = [("", "(none found - pick the controller above first)")]
        return out

    def getPortForwardList(self, filter="", valuesDict=None, typeId="", targetId=0):
        """Dynamic menu: port forward rules on the selected controller."""
        out = []
        try:
            controller_id = int((valuesDict or {}).get("controllerId", 0) or 0)
            controller = self.controllers.get(controller_id)
            if controller and "network_api" in controller:
                for f in controller["network_api"].get_port_forwards():
                    label = f.get("name") or "(unnamed forward)"
                    out.append((f.get("_id", ""), label))
        except Exception as e:
            if self.debug:
                self.logger.debug(f"getPortForwardList error: {e}")
        if not out:
            out = [("", "(none found - pick the controller above first)")]
        return out

    def getWLANList(self, filter="", valuesDict=None, typeId="", targetId=0):
        """Dynamic menu: WLANs/SSIDs on the selected controller."""
        out = []
        try:
            controller_id = int((valuesDict or {}).get("controllerId", 0) or 0)
            controller = self.controllers.get(controller_id)
            if controller and "network_api" in controller:
                for w in controller["network_api"].get_wlans():
                    label = w.get("name") or "(unnamed WLAN)"
                    out.append((w.get("_id", ""), label))
        except Exception as e:
            if self.debug:
                self.logger.debug(f"getWLANList error: {e}")
        if not out:
            out = [("", "(none found - pick the controller above first)")]
        return out

    def getVideoModeList(self, filter="", valuesDict=None, typeId="", targetId=0):
        """Dynamic menu for Set Video Mode: this camera's own supported
        modes (featureFlags.videoModes) where the camera is already
        cached, so the picker only ever offers what this specific camera
        actually reports supporting. Falls back to the full confirmed
        spec enum, each option marked (unverified), if the camera isn't
        cached yet - picking one there still goes through the normal
        write path and gets checked against the real camera by the
        controller, same as any other PATCH this plugin sends."""
        labels = {
            "default": "Default", "highFps": "High FPS", "sport": "Sport",
            "slowShutter": "Slow Shutter", "lprReflex": "LPR (Reflex)",
            "lprNoneReflex": "LPR (Non-Reflex)",
        }
        try:
            dev = indigo.devices[targetId] if targetId and targetId in indigo.devices else None
            if dev:
                cid, controller = self._get_controller_for_dev(dev)
                if controller and "protect_api" in controller:
                    cam_id = dev.pluginProps.get("cameraId", "")
                    camera = controller["protect_api"].get_camera(cam_id) if cam_id else None
                    modes = (camera.get("featureFlags") or {}).get("videoModes") if camera else None
                    if modes:
                        return [(m, labels.get(m, m)) for m in modes]
        except Exception as e:
            if self.debug:
                self.logger.debug(f"getVideoModeList error: {e}")
        return [(k, f"{v} (unverified)") for k, v in labels.items()]

    def getArmProfileList(self, filter="", valuesDict=None, typeId="", targetId=0):
        """Dynamic menu: Alarm Manager arm profiles on the selected
        controller. EXPERIMENTAL - get_alarm_profiles() is itself a
        best-effort guess at where this data lives (see its docstring),
        so an empty result here is the expected, common case without
        real Alarm Manager hardware to confirm against - the fallback
        message says so rather than implying something is broken."""
        out = []
        try:
            controller_id = int((valuesDict or {}).get("controllerId", 0) or 0)
            controller = self.controllers.get(controller_id)
            if controller and "protect_api" in controller:
                for p in controller["protect_api"].get_alarm_profiles():
                    if isinstance(p, dict):
                        label = p.get("name") or p.get("id") or "(unnamed profile)"
                        value = p.get("id") or p.get("_id") or ""
                        out.append((value, label))
        except Exception as e:
            if self.debug:
                self.logger.debug(f"getArmProfileList error: {e}")
        if not out:
            out = [("", "(none found - this is expected if Alarm Manager isn't set up, "
                        "or if it's not where this plugin currently looks; see the "
                        "instructions below)")]
        return out

    def getNetworkDeviceList(self, filter="", valuesDict=None, typeId="", targetId=0):
        out = [("", "Any Device")]
        for d in indigo.devices.iter("self.networkDevice"):
            out.append((str(d.id), d.name))
        return out

    def getPortDeviceList(self, filter="", valuesDict=None, typeId="", targetId=0):
        out = [("", "Any Port")]
        for d in indigo.devices.iter("self.unifiPort"):
            out.append((str(d.id), d.name))
        return out

    def getClientList(self, filter="", valuesDict=None, typeId="", targetId=0):
        out = [("", "Any Client")]
        for d in indigo.devices.iter("self.clientDevice"):
            out.append((str(d.id), d.name))
        return out

    def getDoorList(self, filter="", valuesDict=None, typeId="", targetId=0):
        out = [("", "Any Door")]
        for d in indigo.devices.iter("self.accessDoor"):
            out.append((str(d.id), d.name))
        return out

    def getPortList(self, filter="", valuesDict=None, typeId="", targetId=0):
        """Port number list, populated based on the selected parent switch."""
        ports = []
        parent_id = valuesDict.get("parentDevice") if valuesDict else None
        if not parent_id:
            return [("0", "Select a switch first")]
        try:
            parent = indigo.devices[int(parent_id)]
            count = int(parent.states.get("portCount", 0) or 0)
            if count <= 0:
                return [("0", "No ports available")]
            for n in range(1, count + 1):
                ports.append((str(n), f"Port {n}"))
        except Exception:
            return [("0", "Error loading ports")]
        return ports

    # ------------------------------------------------------------------
    # Discovery: create camera, sensor, network, port, client, door,
    # drive, fan devices
    # ------------------------------------------------------------------

    def _affix_name(self, base_name, valuesDict):
        """Wrap a discovered base name with the optional prefix and suffix
        from the controller config UI. Both default to empty."""
        prefix = (valuesDict.get("deviceNamePrefix", "") or "") if valuesDict else ""
        suffix = (valuesDict.get("deviceNameSuffix", "") or "") if valuesDict else ""
        return f"{prefix}{base_name}{suffix}"

    def _create_or_skip(self, name, type_id, props, folder=None):
        """Create a device if not already present (matched by props), return new device."""
        # Test for an existing device with the same key prop
        key_pairs = []
        if "cameraId" in props:
            key_pairs = [("cameraId", props["cameraId"])]
        elif "sensorId" in props:
            key_pairs = [("sensorId", props["sensorId"])]
        elif "doorId" in props:
            key_pairs = [("doorId", props["doorId"])]
        elif "deviceMac" in props and "portNumber" in props:
            key_pairs = [("deviceMac", props["deviceMac"]),
                         ("portNumber", str(props["portNumber"]))]
        elif "deviceMac" in props:
            key_pairs = [("deviceMac", props["deviceMac"])]
        elif "clientMac" in props:
            key_pairs = [("clientMac", props["clientMac"])]
        elif "diskKey" in props:
            key_pairs = [("diskKey", props["diskKey"])]
        elif "poolKey" in props:
            key_pairs = [("poolKey", props["poolKey"])]
        elif "policyId" in props:
            key_pairs = [("policyId", props["policyId"])]
        elif "routeId" in props:
            key_pairs = [("routeId", props["routeId"])]
        elif "forwardId" in props:
            key_pairs = [("forwardId", props["forwardId"])]
        elif "wlanId" in props:
            key_pairs = [("wlanId", props["wlanId"])]
        elif "armProfileId" in props:
            key_pairs = [("armProfileId", props["armProfileId"])]
        elif "parentCameraId" in props:
            key_pairs = [("parentCameraId", props["parentCameraId"])]

        # If we have no key to match on, fall back to matching by name within
        # the same type — at worst this causes a duplicate-name collision the
        # user can rename, rather than silently swallowing all subsequent
        # devices of the same type (the bug this guards against).
        if not key_pairs:
            for d in indigo.devices.iter("self"):
                if d.deviceTypeId == type_id and d.name == name:
                    return None

        for d in indigo.devices.iter("self"):
            if d.deviceTypeId != type_id:
                continue
            if not key_pairs:
                continue
            match = all(str(d.pluginProps.get(k, "")) == str(v) for k, v in key_pairs)
            if match:
                return None  # already exists

        # Indigo device names must be unique across the ENTIRE install, not
        # just within this device type - two clients with no UniFi-assigned
        # name both fall back to the same manufacturer string (e.g. "Apple,
        # Inc.") often enough that this needs handling, not just hoping it
        # doesn't happen. Auto-disambiguate rather than fail: append " 2",
        # " 3", etc. until a free name is found.
        existing_names = {d.name for d in indigo.devices}
        final_name = name
        suffix = 2
        while final_name in existing_names:
            final_name = f"{name} {suffix}"
            suffix += 1

        kwargs = dict(
            protocol    = indigo.kProtocol.Plugin,
            address     = "",
            deviceTypeId= type_id,
            name        = final_name,
            props       = props,
        )
        if folder:
            try:
                folder_id = indigo.devices.folder.create(folder).id if folder not in indigo.devices.folders else indigo.devices.folders[folder].id
                kwargs["folder"] = folder_id
            except Exception:
                pass

        # Never let a single bad create (a name collision this check
        # somehow missed, a race with another process, anything else)
        # propagate and abort the rest of whatever bulk-discovery loop
        # called this - log it and return None, same as "already exists",
        # so the loop carries on to the next item instead of stopping dead.
        try:
            return indigo.device.create(**kwargs)
        except Exception as e:
            self.logger.error(f"Could not create device '{final_name}' ({type_id}): {e}")
            return None

    def createCameraDevices(self, valuesDict, typeId, devId):
        """Discover cameras and create devices. Also runs Package Camera
        discovery (createPackageCameraDevices) as a natural continuation
        once every regular camera device exists - folded in here rather
        than left as a separate action, at the user's request, since this
        is "the cameras discovery flow" regardless of which specific
        button/action triggered it. Runs for both new and already-
        existing camera devices, so re-running this after upgrading to
        3.12.9 picks up package cameras for cameras created before this
        feature existed, not just newly-discovered ones."""
        try:
            controller_id = int(devId or valuesDict.get("controllerId", 0))
            controller    = self.controllers.get(controller_id)
            if not controller or "protect_api" not in controller:
                self.logger.error("Cannot discover cameras: Protect not connected on controller")
                return valuesDict
            cameras = controller["protect_api"].get_cameras()
            created = 0
            for cam in cameras:
                name = cam.get("name") or cam.get("marketName") or "Camera"
                props = {
                    "controllerId": str(controller_id),
                    "cameraId":     cam.get("id", ""),
                    "cameraName":   name,
                }
                full_name = self._affix_name(name, valuesDict)
                new_dev = self._create_or_skip(full_name, "protectCamera", props)
                if new_dev:
                    created += 1
                    self.logger.info(f"Created camera device: {new_dev.name}")
            self.logger.info(f"Camera discovery: {created} new device(s) created, {len(cameras)} total")
        except Exception as e:
            self.logger.error(f"Error discovering cameras: {e}")
            if self.debug:
                self.logger.error(traceback.format_exc())
        self.createPackageCameraDevices(valuesDict, "protectPackageCamera", controller_id)
        return valuesDict

    def createSensorDevices(self, valuesDict, typeId, devId):
        try:
            controller_id = int(devId or valuesDict.get("controllerId", 0))
            controller    = self.controllers.get(controller_id)
            if not controller or "protect_api" not in controller:
                self.logger.error("Cannot discover sensors: Protect not connected")
                return valuesDict
            sensors = controller["protect_api"].get_sensors()
            created = 0
            for s in sensors:
                name = s.get("name") or "Sensor"
                props = {
                    "controllerId": str(controller_id),
                    "sensorId":     s.get("id", ""),
                    "sensorName":   name,
                }
                full_name = self._affix_name(name, valuesDict)
                new_dev = self._create_or_skip(full_name, "protectSensor", props)
                if new_dev:
                    created += 1
                    self.logger.info(f"Created sensor device: {new_dev.name}")
            self.logger.info(f"Sensor discovery: {created} new device(s) created, {len(sensors)} total")
        except Exception as e:
            self.logger.error(f"Error discovering sensors: {e}")
        return valuesDict

    def createNetworkDevices(self, valuesDict, typeId, devId):
        try:
            controller_id = int(devId or valuesDict.get("controllerId", 0))
            controller    = self.controllers.get(controller_id)
            if not controller or "network_api" not in controller:
                self.logger.error("Cannot discover network devices: Network not connected")
                return valuesDict
            devices = controller["network_api"].get_devices()
            created = 0
            for d in devices:
                name = d.get("name") or d.get("model") or d.get("mac")
                props = {
                    "controllerId": str(controller_id),
                    "deviceMac":    d.get("mac", "").lower(),
                    "deviceName":   name,
                }
                full_name = self._affix_name(name, valuesDict)
                new_dev = self._create_or_skip(full_name, "networkDevice", props)
                if new_dev:
                    created += 1
                    self.logger.info(f"Created network device: {new_dev.name}")
            self.logger.info(f"Network discovery: {created} new device(s) created, {len(devices)} total")
        except Exception as e:
            self.logger.error(f"Error discovering network devices: {e}")
        return valuesDict

    def createFirewallPolicyDevices(self, valuesDict, typeId, devId):
        """Create a device for every non-predefined firewall policy -
        same exclusion as getFirewallPolicyList (Ubiquiti's own built-in
        policies aren't included, there are usually dozens of them and
        toggling them from automation is rarely useful)."""
        try:
            controller_id = int(devId or valuesDict.get("controllerId", 0))
            controller    = self.controllers.get(controller_id)
            if not controller or "network_api" not in controller:
                self.logger.error("Cannot discover firewall policies: Network not connected")
                return valuesDict
            policies = [p for p in controller["network_api"].get_firewall_policies()
                        if not p.get("predefined", False)]
            created = 0
            for p in policies:
                name = p.get("name") or p.get("_id")
                props = {
                    "controllerId": str(controller_id),
                    "policyId":     p.get("_id", ""),
                }
                full_name = self._affix_name(name, valuesDict)
                new_dev = self._create_or_skip(full_name, "firewallPolicy", props)
                if new_dev:
                    created += 1
                    self.logger.info(f"Created firewall policy device: {new_dev.name}")
            self.logger.info(f"Firewall policy discovery: {created} new device(s) created, "
                             f"{len(policies)} custom polic{'y' if len(policies) == 1 else 'ies'} found")
        except Exception as e:
            self.logger.error(f"Error discovering firewall policies: {e}")
        return valuesDict

    def createTrafficRouteDevices(self, valuesDict, typeId, devId):
        """Create a device for every traffic route."""
        try:
            controller_id = int(devId or valuesDict.get("controllerId", 0))
            controller    = self.controllers.get(controller_id)
            if not controller or "network_api" not in controller:
                self.logger.error("Cannot discover traffic routes: Network not connected")
                return valuesDict
            routes = controller["network_api"].get_traffic_routes()
            created = 0
            for r in routes:
                name = r.get("description") or r.get("_id")
                props = {
                    "controllerId": str(controller_id),
                    "routeId":      r.get("_id", ""),
                }
                full_name = self._affix_name(name, valuesDict)
                new_dev = self._create_or_skip(full_name, "trafficRoute", props)
                if new_dev:
                    created += 1
                    self.logger.info(f"Created traffic route device: {new_dev.name}")
            self.logger.info(f"Traffic route discovery: {created} new device(s) created, {len(routes)} total")
        except Exception as e:
            self.logger.error(f"Error discovering traffic routes: {e}")
        return valuesDict

    def createPortForwardDevices(self, valuesDict, typeId, devId):
        """Create a device for every port forward rule."""
        try:
            controller_id = int(devId or valuesDict.get("controllerId", 0))
            controller    = self.controllers.get(controller_id)
            if not controller or "network_api" not in controller:
                self.logger.error("Cannot discover port forwards: Network not connected")
                return valuesDict
            forwards = controller["network_api"].get_port_forwards()
            created = 0
            for f in forwards:
                name = f.get("name") or f.get("_id")
                props = {
                    "controllerId": str(controller_id),
                    "forwardId":    f.get("_id", ""),
                }
                full_name = self._affix_name(name, valuesDict)
                new_dev = self._create_or_skip(full_name, "portForward", props)
                if new_dev:
                    created += 1
                    self.logger.info(f"Created port forward device: {new_dev.name}")
            self.logger.info(f"Port forward discovery: {created} new device(s) created, {len(forwards)} total")
        except Exception as e:
            self.logger.error(f"Error discovering port forwards: {e}")
        return valuesDict

    def createWLANDevices(self, valuesDict, typeId, devId):
        """Create a device for every WLAN/SSID."""
        try:
            controller_id = int(devId or valuesDict.get("controllerId", 0))
            controller    = self.controllers.get(controller_id)
            if not controller or "network_api" not in controller:
                self.logger.error("Cannot discover WLANs: Network not connected")
                return valuesDict
            wlans = controller["network_api"].get_wlans()
            created = 0
            for w in wlans:
                name = w.get("name") or w.get("_id")
                props = {
                    "controllerId": str(controller_id),
                    "wlanId":       w.get("_id", ""),
                }
                full_name = self._affix_name(name, valuesDict)
                new_dev = self._create_or_skip(full_name, "wlanDevice", props)
                if new_dev:
                    created += 1
                    self.logger.info(f"Created WLAN device: {new_dev.name}")
            self.logger.info(f"WLAN discovery: {created} new device(s) created, {len(wlans)} total")
        except Exception as e:
            self.logger.error(f"Error discovering WLANs: {e}")
        return valuesDict

    def createAlarmProfileDevices(self, valuesDict, typeId, devId):
        """Create a device for every Alarm Manager arm profile found.
        EXPERIMENTAL - see get_alarm_profiles()'s docstring. Finding zero
        is the expected, common case without real Alarm Manager hardware
        to confirm the real data location against."""
        try:
            controller_id = int(devId or valuesDict.get("controllerId", 0))
            controller    = self.controllers.get(controller_id)
            if not controller or "protect_api" not in controller:
                self.logger.error("Cannot discover alarm profiles: Protect not connected")
                return valuesDict
            profiles = controller["protect_api"].get_alarm_profiles()
            created = 0
            for p in profiles:
                if not isinstance(p, dict):
                    continue
                profile_id = p.get("id") or p.get("_id") or ""
                name = p.get("name") or profile_id
                props = {
                    "controllerId": str(controller_id),
                    "armProfileId": profile_id,
                }
                full_name = self._affix_name(name, valuesDict)
                new_dev = self._create_or_skip(full_name, "alarmProfile", props)
                if new_dev:
                    created += 1
                    self.logger.info(f"Created alarm profile device: {new_dev.name}")
            if profiles:
                self.logger.info(f"Alarm profile discovery: {created} new device(s) created, {len(profiles)} total")
            else:
                self.logger.info(
                    "Alarm profile discovery: found none. This is expected if Alarm "
                    "Manager isn't configured, or if the profile data isn't where this "
                    "plugin currently looks for it - create one Alarm Profile device "
                    "manually and use its \"Dump Device Payload to Log\" button to help "
                    "confirm the real structure.")
        except Exception as e:
            self.logger.error(f"Error discovering alarm profiles: {e}")
        return valuesDict

    def createPackageCameraDevices(self, valuesDict, typeId, devId):
        """Create a Package Camera device for every existing protectCamera
        device confirmed (via legacy bootstrap data) to actually have a
        package lens. Only auto-creates where that confirmation is
        possible - hasPackageCamera is legacy-only data (see Devices.xml's
        note on this device type), so without legacy credentials
        configured this can't tell which cameras qualify and creates
        nothing, same as the alarm profile discovery above when its own
        data isn't available either."""
        try:
            controller_id = int(devId or valuesDict.get("controllerId", 0))
            controller    = self.controllers.get(controller_id)
            if not controller or "protect_api" not in controller:
                self.logger.error("Cannot discover package cameras: Protect not connected")
                return valuesDict
            bootstrap = controller["protect_api"].get_bootstrap_raw() or {}
            bootstrap_cams = {c.get("id"): c for c in (bootstrap.get("cameras") or []) if isinstance(c, dict)}
            if not bootstrap_cams:
                self.logger.info(
                    "Package camera discovery: no legacy bootstrap data available, so there's "
                    "no way to confirm which cameras actually have a package lens - nothing "
                    "created automatically. Create a Package Camera device manually and pick "
                    "the parent camera yourself instead.")
                return valuesDict

            created = 0
            checked = 0
            for d in indigo.devices.iter("self.protectCamera"):
                if d.pluginProps.get("controllerId") != str(controller_id):
                    continue
                cam_id = d.pluginProps.get("cameraId", "")
                bcam = bootstrap_cams.get(cam_id)
                if not bcam or not bcam.get("hasPackageCamera", False):
                    continue
                checked += 1
                props = {
                    "controllerId":   str(controller_id),
                    "parentCameraId": str(d.id),
                }
                full_name = self._affix_name(f"{d.name} Package", valuesDict)
                new_dev = self._create_or_skip(full_name, "protectPackageCamera", props)
                if new_dev:
                    created += 1
                    self.logger.info(f"Created package camera device: {new_dev.name}")
            self.logger.info(f"Package camera discovery: {created} new device(s) created, "
                             f"{checked} camera(s) confirmed to have a package lens")
        except Exception as e:
            self.logger.error(f"Error discovering package cameras: {e}")
        return valuesDict

    def createPortDevices(self, valuesDict, typeId, devId):
        """Create devices for ports of a switch."""
        try:
            controller_id = int(valuesDict.get("controllerId", 0))
            parent_id     = int(valuesDict.get("parentDevice", 0))
            controller    = self.controllers.get(controller_id)
            if not controller or "network_api" not in controller:
                self.logger.error("Cannot discover ports: Network not connected")
                return valuesDict
            if parent_id not in indigo.devices:
                self.logger.error("Parent switch device not found")
                return valuesDict
            parent = indigo.devices[parent_id]
            mac = parent.pluginProps.get("deviceMac", "").lower()
            d   = controller["network_api"].get_device(mac)
            if not d:
                self.logger.error("Could not find parent switch on controller")
                return valuesDict
            ports = d.get("port_table") or []
            created = 0
            for p in ports:
                pidx     = p.get("port_idx")
                pname    = p.get("name") or f"Port {pidx}"
                props = {
                    "controllerId":   str(controller_id),
                    "parentDevice":   str(parent_id),
                    "deviceMac":      mac,
                    "portNumber":     str(pidx),
                    "controlMode":    "poe",
                }
                new_dev = self._create_or_skip(
                    self._affix_name(f"{parent.name} - {pname}", valuesDict),
                    "unifiPort", props)
                if new_dev:
                    created += 1
            self.logger.info(f"Port discovery: {created} new device(s) created on {parent.name}")
        except Exception as e:
            self.logger.error(f"Error discovering ports: {e}")
        return valuesDict

    def createAccessDoors(self, valuesDict, typeId, devId):
        try:
            controller_id = int(devId or valuesDict.get("controllerId", 0))
            controller    = self.controllers.get(controller_id)
            if not controller or "access_api" not in controller:
                self.logger.error("Cannot discover doors: Access not connected")
                return valuesDict
            doors = controller["access_api"].get_doors()
            created = 0
            for door in doors:
                name = door.get("name", "Door")
                props = {
                    "controllerId":  str(controller_id),
                    "doorId":        door.get("id", ""),
                    "doorName":      name,
                    # Indigo's recognized relay-device Lock subtype marker
                    # (pluginProps["IsLockSubType"]) - confirmed against
                    # docs.indigodomo.com/2026.1/scripting/reference/
                    # devices/base-class/: this is what unlocks the native
                    # indigo.device.lock()/unlock() commands for a relay
                    # device. NOT what controls the "Locked"/"Unlocked"
                    # text in the device list though - that's the uiValue
                    # passed to updateStateOnServer("onOffState", ...) in
                    # _update_door_states.
                    "IsLockSubType": True,
                }
                new_dev = self._create_or_skip(self._affix_name(name, valuesDict), "accessDoor", props)
                if new_dev:
                    created += 1
                    self.logger.info(f"Created door device: {new_dev.name}")
            self.logger.info(f"Door discovery: {created} new device(s) created, {len(doors)} total")
        except Exception as e:
            self.logger.error(f"Error discovering doors: {e}")
        return valuesDict

    def createFanDevice(self, valuesDict, typeId, devId):
        try:
            controller_id = int(devId or valuesDict.get("controllerId", 0))
            controller    = self.controllers.get(controller_id)
            if not controller or "drive_api" not in controller:
                self.logger.error("Cannot create Fan device: Drive not connected")
                return valuesDict
            props = {"controllerId": str(controller_id)}
            new_dev = self._create_or_skip(
                self._affix_name("UNAS Fan", valuesDict),
                "driveFan", props)
            if new_dev:
                self.logger.info(f"Created Fan device: {new_dev.name}")
            else:
                self.logger.info("Fan device already exists")
        except Exception as e:
            self.logger.error(f"Error creating Fan device: {e}")
        return valuesDict

    def _ssh_from_valuesDict(self, valuesDict, controller, dev_for_log=None):
        """Build a one-shot SSH client from the values currently in the config
        UI (which may not yet have been saved). Falls back to the running
        controller's SSH client when the dialog is being run after save.

        Returns (ssh, address, error_message_or_None). On error, ssh is None.
        """
        # Use the configured address from valuesDict (matches the rest of the
        # dialog state). If empty, fall back to the running controller.
        address = (valuesDict.get("address", "") or "").strip()
        if not address and controller:
            address = controller.get("address", "")
        if not address:
            return None, "", "No address configured for UNAS"

        if not bool(valuesDict.get("sshEnabled", False)):
            return None, address, "SSH fan control is not enabled (tick the checkbox)"

        ssh_user = (valuesDict.get("sshUsername", "") or "").strip()
        ssh_pass = valuesDict.get("sshPassword", "") or ""
        try:
            ssh_port = int(valuesDict.get("sshPort", "22") or "22")
        except (TypeError, ValueError):
            ssh_port = 22

        if not ssh_user or not ssh_pass:
            return None, address, "SSH username/password not provided"

        ssh = UnifiDriveSSH(
            address, ssh_port, ssh_user, ssh_pass,
            logger=self.logger, debug=self.debug,
        )
        return ssh, address, None

    def testFanControl(self, valuesDict, typeId, devId):
        """Run a diagnostic against the deployed fan control service. Useful
        when fans aren't actually changing despite SSH writes succeeding."""
        ssh = None
        try:
            controller_id = int(devId or valuesDict.get("controllerId", 0))
            controller    = self.controllers.get(controller_id)

            ssh, address, err = self._ssh_from_valuesDict(valuesDict, controller)
            if err:
                self.logger.error(f"Cannot run fan diagnostic: {err}")
                return valuesDict

            dev = controller.get("device") if controller else None
            name = dev.name if dev else f"UNAS at {address}"

            if not ssh.is_deployed():
                self.logger.error(
                    f"{name}: Fan control service is not deployed. "
                    f"Click 'Install/Update Fan Control on UNAS' first."
                )
                return valuesDict

            self.logger.info(f"{name}: Running fan control diagnostic…")
            output = ssh.run_diagnostic()
            self.logger.info(f"{name}: Fan control diagnostic results follow:\n{output}")
        except UnifiSSHError as e:
            self.logger.error(f"Fan control diagnostic failed: {e}")
        except Exception as e:
            self.logger.error(f"Unexpected error running fan diagnostic: {e}")
            if self.debug:
                self.logger.error(traceback.format_exc())
        finally:
            if ssh is not None:
                try: ssh.close()
                except Exception: pass
        return valuesDict

    def deployFanControl(self, valuesDict, typeId, devId):
        """Install the fan control script + systemd service on the UNAS over
        SSH. Builds an SSH client from the values currently in the config
        dialog so the user doesn't need to save first."""
        ssh = None
        try:
            controller_id = int(devId or valuesDict.get("controllerId", 0))
            controller    = self.controllers.get(controller_id)

            ssh, address, err = self._ssh_from_valuesDict(valuesDict, controller)
            if err:
                self.logger.error(f"Cannot deploy fan control: {err}")
                return valuesDict
            if not self.fan_script_content or not self.fan_service_content:
                self.logger.error(
                    "Cannot deploy fan control: bundled script/service assets are missing from the plugin."
                )
                return valuesDict

            dev = controller.get("device") if controller else None
            name = dev.name if dev else f"UNAS at {address}"
            self.logger.info(f"{name}: Deploying fan control to UNAS via SSH…")
            ssh.deploy_fan_control(self.fan_script_content, self.fan_service_content)

            # Verify the service ended up running and report what we deployed
            service_status = ssh.get_service_status()
            active_mode    = ssh.get_active_mode()
            self.logger.info(
                f"{name}: Fan control deployed. "
                f"Service: {service_status}, current mode: {active_mode}"
            )
            if service_status != "active":
                self.logger.warning(
                    f"{name}: Fan control service is '{service_status}' — "
                    f"check the UNAS systemd logs with: "
                    f"journalctl -u indigo_fan_control.service -n 50"
                )

            if dev is not None:
                try:
                    dev.updateStatesOnServer([
                        {"key": "fanControlDeployed",   "value": True},
                        {"key": "fanControlActiveMode", "value": active_mode},
                    ])
                except Exception:
                    pass
        except UnifiSSHError as e:
            self.logger.error(f"Fan control deploy failed: {e}")
        except Exception as e:
            self.logger.error(f"Unexpected error deploying fan control: {e}")
            if self.debug:
                self.logger.error(traceback.format_exc())
        finally:
            if ssh is not None:
                try: ssh.close()
                except Exception: pass
        return valuesDict

    def removeFanControl(self, valuesDict, typeId, devId):
        """Stop and remove the fan control service from the UNAS. Builds an
        SSH client from the values currently in the config dialog."""
        ssh = None
        try:
            controller_id = int(devId or valuesDict.get("controllerId", 0))
            controller    = self.controllers.get(controller_id)

            ssh, address, err = self._ssh_from_valuesDict(valuesDict, controller)
            if err:
                self.logger.error(f"Cannot remove fan control: {err}")
                return valuesDict

            dev = controller.get("device") if controller else None
            name = dev.name if dev else f"UNAS at {address}"
            self.logger.info(f"{name}: Removing fan control from UNAS via SSH…")
            ssh.remove_fan_control()
            self.logger.info(f"{name}: Fan control removed. UNAS firmware now manages fans.")
            if dev is not None:
                try:
                    dev.updateStatesOnServer([
                        {"key": "fanControlDeployed",   "value": False},
                        {"key": "fanControlActiveMode", "value": ""},
                    ])
                except Exception:
                    pass
        except UnifiSSHError as e:
            self.logger.error(f"Fan control removal failed: {e}")
        except Exception as e:
            self.logger.error(f"Unexpected error removing fan control: {e}")
            if self.debug:
                self.logger.error(traceback.format_exc())
        finally:
            if ssh is not None:
                try: ssh.close()
                except Exception: pass
        return valuesDict

    def createDrivePools(self, valuesDict, typeId, devId):
        """Discover storage pools on the UNAS and create devices for each."""
        try:
            controller_id = int(devId or valuesDict.get("controllerId", 0))
            controller    = self.controllers.get(controller_id)
            if not controller or "drive_api" not in controller:
                self.logger.error("Cannot discover pools: Drive not connected")
                return valuesDict

            # Refresh the cached storage payload so we have current pool data
            try:
                storage = controller["drive_api"].get_storage_info() or {}
                controller["last_storage"] = storage
            except Exception as e:
                self.logger.error(f"Pool discovery: storage fetch failed: {e}")
                return valuesDict

            pools = _drive_extract_pools(storage)
            if not pools:
                self.logger.warning("Pool discovery: no pools found in storage payload")
                return valuesDict

            created = 0
            for idx, pool in enumerate(pools):
                pkey = _drive_pool_key(pool, idx)
                pname = _drive_pool_name(pool, idx)
                props = {
                    "controllerId": str(controller_id),
                    "poolKey":      pkey,
                }
                full_name = self._affix_name(f"UNAS Pool {pname}", valuesDict)
                new_dev = self._create_or_skip(full_name, "unifiDrivePool", props)
                if new_dev:
                    created += 1
                    self.logger.info(f"Created pool device: {new_dev.name}")
            self.logger.info(f"Pool discovery: {created} new device(s) created, {len(pools)} total")
        except Exception as e:
            self.logger.error(f"Error discovering pools: {e}")
            if self.debug:
                self.logger.error(traceback.format_exc())
        return valuesDict

    def createDriveDisks(self, valuesDict, typeId, devId):
        """Discover all physical disks and create devices for each. Disks live
        at the top of the storage payload; their `poolId` field links each to
        its parent pool."""
        try:
            controller_id = int(devId or valuesDict.get("controllerId", 0))
            controller    = self.controllers.get(controller_id)
            if not controller or "drive_api" not in controller:
                self.logger.error("Cannot discover disks: Drive not connected")
                return valuesDict

            try:
                storage = controller["drive_api"].get_storage_info() or {}
                controller["last_storage"] = storage
            except Exception as e:
                self.logger.error(f"Disk discovery: storage fetch failed: {e}")
                return valuesDict

            disks = _drive_extract_disks(storage)
            if not disks:
                self.logger.warning("Disk discovery: no disks found in storage payload")
                return valuesDict

            # Build a pool_id -> (pool, index) lookup so we can put each disk's
            # parent pool name in the display name.
            pools = _drive_extract_pools(storage)
            pool_lookup = {}
            for pidx, pool in enumerate(pools):
                for id_field in ("id", "guid", "poolGuid", "uuid"):
                    pid = pool.get(id_field)
                    if pid:
                        pool_lookup[pid] = (pool, pidx)
                        break

            created = 0
            for didx, disk in enumerate(disks):
                dkey = _drive_disk_key(disk, didx)
                pool_id  = disk.get("poolId") or disk.get("pool_id") or disk.get("poolGuid")
                pool, pidx = pool_lookup.get(pool_id, (None, 0))
                pname = _drive_pool_name(pool, pidx) if pool else "Pool"
                dname = _drive_disk_name(disk, didx, pidx)
                props = {
                    "controllerId": str(controller_id),
                    "diskKey":      dkey,
                }
                full_name = self._affix_name(f"UNAS Disk {pname} {dname}", valuesDict)
                new_dev = self._create_or_skip(full_name, "unifiDriveDisk", props)
                if new_dev:
                    created += 1
                    self.logger.info(f"Created disk device: {new_dev.name}")
            self.logger.info(f"Disk discovery: {created} new device(s) created, {len(disks)} total")
        except Exception as e:
            self.logger.error(f"Error discovering disks: {e}")
            if self.debug:
                self.logger.error(traceback.format_exc())
        return valuesDict

    # ------------------------------------------------------------------
    # Helpers for action handlers
    # ------------------------------------------------------------------

    def _get_controller_for_dev(self, dev):
        try:
            cid = int(dev.pluginProps.get("controllerId", 0))
        except (TypeError, ValueError):
            return None, None
        return cid, self.controllers.get(cid)

    def _get_camera_api(self, dev):
        cid, controller = self._get_controller_for_dev(dev)
        if not controller or "protect_api" not in controller:
            self.logger.error(f"{dev.name}: Protect API not available")
            return None, None, None
        camera_id = dev.pluginProps.get("cameraId", "")
        return cid, camera_id, controller["protect_api"]

    # ------------------------------------------------------------------
    # Camera actions
    # ------------------------------------------------------------------

    def actionSetRecordingMode(self, action):
        """Recording mode control is NOT available via the official Protect
        Integration API. Confirmed against the published OpenAPI spec: the
        camera PATCH endpoint's writable fields are name, osdSettings,
        ledSettings, lcdMessage, micVolume, videoMode, hdrType, and
        smartDetectSettings - no recordingSettings/mode field exists.
        This previously sent `{"recordingSettings": {"mode": ...}}`, which
        isn't a real field and would have been rejected by the API every
        time. Failing clearly here rather than attempting a doomed call."""
        try:
            dev = indigo.devices[action.deviceId]
            self.logger.error(
                f"{dev.name}: Recording mode cannot be changed via the official Protect "
                f"API - Ubiquiti hasn't exposed this control yet. Use the UniFi Protect "
                f"app to change it directly on the camera."
            )
        except Exception as e:
            self.logger.error(f"setRecordingMode error: {e}")

    def actionSetIRMode(self, action):
        """IR/night-vision mode control is NOT available via the official
        Protect Integration API - same situation as recording mode above,
        confirmed against the same writable-field list. This previously
        sent `{"ispSettings": {"irLedMode": ...}}`, which isn't a real
        field. Failing clearly here rather than attempting a doomed call."""
        try:
            dev = indigo.devices[action.deviceId]
            self.logger.error(
                f"{dev.name}: IR mode cannot be changed via the official Protect API - "
                f"Ubiquiti hasn't exposed this control yet. Use the UniFi Protect app "
                f"to change it directly on the camera."
            )
        except Exception as e:
            self.logger.error(f"setIRMode error: {e}")

    def actionSetStatusLight(self, action):
        try:
            dev     = indigo.devices[action.deviceId]
            enabled = bool(action.props.get("enabled", True))
            cid, cam_id, api = self._get_camera_api(dev)
            if not api:
                return
            # Capability check before sending anything - confirmed flag
            # name (hasLedStatus) from the official featureFlags schema,
            # not guessed. A clear Event Log error beats a PATCH the
            # controller would silently ignore or reject anyway.
            camera = api.get_camera(cam_id)
            if camera and not (camera.get("featureFlags") or {}).get("hasLedStatus", True):
                self.logger.error(f"{dev.name}: This camera has no status LED to control")
                return
            self.logger.info(f"{dev.name}: Setting status light to {enabled}")
            api.update_camera(cam_id, {"ledSettings": {"isEnabled": enabled}})
            self._update_camera_states(cid, cam_id)
        except Exception as e:
            self.logger.error(f"setStatusLight error: {e}")

    def actionSetOverlays(self, action):
        try:
            dev = indigo.devices[action.deviceId]
            cid, cam_id, api = self._get_camera_api(dev)
            if not api:
                return
            settings = {"osdSettings": {
                "isNameEnabled": bool(action.props.get("showName", True)),
                "isDateEnabled": bool(action.props.get("showDate", True)),
                "isLogoEnabled": bool(action.props.get("showLogo", False)),
            }}
            self.logger.info(f"{dev.name}: Setting overlays")
            api.update_camera(cam_id, settings)
            self._update_camera_states(cid, cam_id)
        except Exception as e:
            self.logger.error(f"setOverlays error: {e}")

    def actionSetStatusMessage(self, action):
        """Set a custom message on the camera/doorbell's LCD screen (only
        applicable to hardware with an LCD - e.g. G4 Doorbell Pro).
        lcdMessage is a discriminated union keyed by `type`; a free-text
        message is type "CUSTOM_MESSAGE" with a `text` field - a bare
        {"text": ...} (what this previously sent) doesn't match any of the
        four valid shapes and would have been rejected."""
        try:
            dev = indigo.devices[action.deviceId]
            cid, cam_id, api = self._get_camera_api(dev)
            if not api:
                return
            message = action.props.get("message", "")
            self.logger.info(f"{dev.name}: Setting status message to: {message}")
            api.update_camera(cam_id, {
                "lcdMessage": {"type": "CUSTOM_MESSAGE", "text": message},
            })
        except Exception as e:
            self.logger.error(f"setStatusMessage error: {e}")

    def actionEnableDetections(self, action):
        """Set the camera's AI object-detection AND audio-detection types.
        smartDetectSettings requires BOTH objectTypes and audioTypes
        together in one PATCH (per the official schema) - this action now
        sets both explicitly (3.12.8 extended it to cover audio; it
        previously only touched object types and silently carried
        whatever audioTypes was already set, which meant the audio
        detection states/triggers added in 3.12.8 could never actually
        fire unless the camera happened to have audio types enabled by
        some other means). alrm* names confirmed against the official
        audioTypes enum, not guessed. Previously (pre-3.11) sent
        `{"recordingSettings": {"enableMotionDetection": ...}}`, which
        isn't a real field and wouldn't have changed anything on the
        camera."""
        try:
            dev = indigo.devices[action.deviceId]
            cid, cam_id, api = self._get_camera_api(dev)
            if not api:
                return

            object_types = []
            if action.props.get("detectPerson", True):        object_types.append("person")
            if action.props.get("detectVehicle", True):       object_types.append("vehicle")
            if action.props.get("detectPackage", True):       object_types.append("package")
            if action.props.get("detectAnimal", False):       object_types.append("animal")
            if action.props.get("detectFace", False):         object_types.append("face")
            if action.props.get("detectLicensePlate", False): object_types.append("licensePlate")

            audio_types = []
            if action.props.get("detectSmokeAlarm",   True):  audio_types.append("alrmSmoke")
            if action.props.get("detectCoAlarm",      True):  audio_types.append("alrmCmonx")
            if action.props.get("detectBurglarAlarm", True):  audio_types.append("alrmBurglar")
            if action.props.get("detectGlassBreak",   True):  audio_types.append("alrmGlassBreak")
            if action.props.get("detectSiren",        True):  audio_types.append("alrmSiren")
            if action.props.get("detectBabyCry",      False): audio_types.append("alrmBabyCry")
            if action.props.get("detectSpeech",       False): audio_types.append("alrmSpeak")
            if action.props.get("detectBark",         False): audio_types.append("alrmBark")
            if action.props.get("detectCarHorn",      False): audio_types.append("alrmCarHorn")

            api.update_camera(cam_id, {
                "smartDetectSettings": {
                    "objectTypes": object_types,
                    "audioTypes":  audio_types,
                },
            })
            self.logger.info(f"{dev.name}: Smart detection set - objects: {object_types or 'none'}, "
                             f"audio: {audio_types or 'none'}")
            self._update_camera_states(cid, cam_id)
        except Exception as e:
            self.logger.error(f"enableDetections error: {e}")
            if self.debug:
                self.logger.error(traceback.format_exc())

    def actionSetHDRMode(self, action):
        """hdrType: confirmed writable field, confirmed enum (auto/on/
        off) from the official schema."""
        try:
            dev  = indigo.devices[action.deviceId]
            mode = action.props.get("hdrMode", "auto")
            cid, cam_id, api = self._get_camera_api(dev)
            if not api:
                return
            camera = api.get_camera(cam_id)
            if camera and not (camera.get("featureFlags") or {}).get("hasHdr", True):
                self.logger.error(f"{dev.name}: This camera doesn't support HDR")
                return
            self.logger.info(f"{dev.name}: Setting HDR mode to {mode}")
            api.update_camera(cam_id, {"hdrType": mode})
            self._update_camera_states(cid, cam_id)
        except Exception as e:
            self.logger.error(f"setHDRMode error: {e}")
            if self.debug:
                self.logger.error(traceback.format_exc())

    def actionSetMicVolume(self, action):
        """micVolume: confirmed writable field (0-100) from the official
        schema."""
        try:
            dev = indigo.devices[action.deviceId]
            try:
                volume = max(0, min(100, int(action.props.get("volume", 100))))
            except (TypeError, ValueError):
                volume = 100
            cid, cam_id, api = self._get_camera_api(dev)
            if not api:
                return
            camera = api.get_camera(cam_id)
            if camera and not (camera.get("featureFlags") or {}).get("hasMic", True):
                self.logger.error(f"{dev.name}: This camera has no microphone")
                return
            self.logger.info(f"{dev.name}: Setting microphone volume to {volume}")
            api.update_camera(cam_id, {"micVolume": volume})
            self._update_camera_states(cid, cam_id)
        except Exception as e:
            self.logger.error(f"setMicVolume error: {e}")
            if self.debug:
                self.logger.error(traceback.format_exc())

    def actionSetVideoMode(self, action):
        """videoMode: confirmed writable field. Its menu (getVideoModeList)
        is populated from THIS camera's own featureFlags.videoModes where
        possible, so the picker only ever offers modes this specific
        camera actually reports supporting, rather than the full spec
        enum (default/highFps/sport/slowShutter/lprReflex/
        lprNoneReflex) regardless of hardware."""
        try:
            dev  = indigo.devices[action.deviceId]
            mode = action.props.get("videoMode", "default")
            cid, cam_id, api = self._get_camera_api(dev)
            if not api:
                return
            self.logger.info(f"{dev.name}: Setting video mode to {mode}")
            api.update_camera(cam_id, {"videoMode": mode})
            self._update_camera_states(cid, cam_id)
        except Exception as e:
            self.logger.error(f"setVideoMode error: {e}")
            if self.debug:
                self.logger.error(traceback.format_exc())

    def actionRebootCamera(self, action):
        try:
            dev = indigo.devices[action.deviceId]
            cid, cam_id, api = self._get_camera_api(dev)
            if not api:
                return
            self.logger.info(f"{dev.name}: Rebooting camera")
            api.reboot_camera(cam_id)
        except Exception as e:
            self.logger.error(f"rebootCamera error: {e}")

    def actionTakeSnapshot(self, action):
        """Fetch a snapshot via the integration API and optionally save to disk."""
        try:
            dev = indigo.devices[action.deviceId]
            cid, cam_id, api = self._get_camera_api(dev)
            if not api:
                return
            save_path = action.props.get("savePath", "").strip()
            if not save_path:
                self.logger.warning(f"{dev.name}: No save path specified for snapshot")
                return

            url = api.get_camera_snapshot_url(cam_id)
            req = urllib.request.Request(url, method="GET")
            req.add_header("Authorization", f"Bearer {api.api_key}")
            with api.opener.open(req, timeout=30) as response:
                data = response.read()
            with open(save_path, "wb") as f:
                f.write(data)
            self.logger.info(f"{dev.name}: Snapshot saved to {save_path}")
        except Exception as e:
            self.logger.error(f"takeSnapshot error: {e}")

    def actionEnableRtspStreaming(self, action):
        """Create RTSPS streams for the checked quality levels. Idempotent:
        qualities already active are left alone (and their existing URL is
        reused) rather than recreated, so this is safe to run repeatedly -
        e.g. to pick up a quality you didn't enable the first time."""
        try:
            dev = indigo.devices[action.deviceId]
            cid, cam_id, api = self._get_camera_api(dev)
            if not api:
                return

            wanted = []
            if action.props.get("enableHigh", True):     wanted.append("high")
            if action.props.get("enableMedium", True):   wanted.append("medium")
            if action.props.get("enableLow", True):      wanted.append("low")
            if action.props.get("enablePackage", False): wanted.append("package")
            if not wanted:
                self.logger.warning(f"{dev.name}: No quality levels selected - nothing to enable")
                return

            existing = api.get_rtsps_streams(cam_id) or {}
            to_create = [q for q in wanted if not existing.get(q)]
            if to_create:
                created = api.create_rtsps_streams(cam_id, to_create)
                existing.update(created or {})
            else:
                self.logger.info(f"{dev.name}: Requested RTSP quality level(s) already active")

            states = []
            for quality, state_key in (("high", "rtspUrlHigh"),
                                       ("medium", "rtspUrlMedium"),
                                       ("low", "rtspUrlLow"),
                                       ("package", "rtspUrlPackage")):
                if quality in wanted:
                    states.append({"key": state_key, "value": existing.get(quality) or ""})
            if states:
                dev.updateStatesOnServer(states)
            self.logger.info(f"{dev.name}: RTSP streaming active for: {', '.join(wanted)}")

            # Mirror to a linked Package Camera device, if one exists -
            # same underlying camera object, so it should show the same
            # value rather than only updating on its own next refresh.
            if "package" in wanted:
                self._mirror_to_package_camera(cid, cam_id, [
                    {"key": "rtspUrlPackage", "value": existing.get("package") or ""}])
        except Exception as e:
            self.logger.error(f"enableRtspStreaming error: {e}")
            if self.debug:
                self.logger.error(traceback.format_exc())

    def actionDisableRtspStreaming(self, action):
        """Remove all currently active RTSPS streams for this camera."""
        try:
            dev = indigo.devices[action.deviceId]
            cid, cam_id, api = self._get_camera_api(dev)
            if not api:
                return

            existing = api.get_rtsps_streams(cam_id) or {}
            active = [q for q in ("high", "medium", "low", "package") if existing.get(q)]
            if active:
                api.delete_rtsps_streams(cam_id, active)
                self.logger.info(f"{dev.name}: RTSP streaming disabled for: {', '.join(active)}")
            else:
                self.logger.info(f"{dev.name}: No active RTSP streams to disable")

            dev.updateStatesOnServer([
                {"key": "rtspUrlHigh",    "value": ""},
                {"key": "rtspUrlMedium",  "value": ""},
                {"key": "rtspUrlLow",     "value": ""},
                {"key": "rtspUrlPackage", "value": ""},
            ])
            self._mirror_to_package_camera(cid, cam_id, [{"key": "rtspUrlPackage", "value": ""}])
        except Exception as e:
            self.logger.error(f"disableRtspStreaming error: {e}")
            if self.debug:
                self.logger.error(traceback.format_exc())

    def actionEnablePackageCameraRtsp(self, action):
        """Enable RTSP streaming for this Package Camera's lens
        specifically - same mechanism as the parent camera's own Enable
        RTSP Streaming action, just always the "package" quality, since
        that's this device's only purpose. Mirrors the result onto the
        parent camera's own rtspUrlPackage state too, since it's the same
        underlying camera object."""
        try:
            dev = indigo.devices[action.deviceId]
            cid, controller = self._get_controller(dev)
            if not controller or "protect_api" not in controller:
                self.logger.error(f"{dev.name}: Protect not available")
                return
            parent_dev_id = dev.pluginProps.get("parentCameraId", "")
            parent_id_int = int(parent_dev_id) if str(parent_dev_id).isdigit() else None
            if parent_id_int not in indigo.devices:
                self.logger.error(f"{dev.name}: Parent camera device not found")
                return
            cam_id = indigo.devices[parent_id_int].pluginProps.get("cameraId", "")
            if not cam_id:
                self.logger.error(f"{dev.name}: Parent camera has no cameraId")
                return
            api = controller["protect_api"]
            existing = api.get_rtsps_streams(cam_id) or {}
            if not existing.get("package"):
                created = api.create_rtsps_streams(cam_id, ["package"])
                existing.update(created or {})
            url = existing.get("package") or ""
            dev.updateStateOnServer("rtspUrlPackage", url)
            self._mirror_states(parent_id_int, [{"key": "rtspUrlPackage", "value": url}])
            self.logger.info(f"{dev.name}: RTSP streaming enabled")
        except Exception as e:
            self.logger.error(f"enablePackageCameraRtsp error: {e}")
            if self.debug:
                self.logger.error(traceback.format_exc())

    def actionDisablePackageCameraRtsp(self, action):
        """Disable RTSP streaming for this Package Camera's lens."""
        try:
            dev = indigo.devices[action.deviceId]
            cid, controller = self._get_controller(dev)
            if not controller or "protect_api" not in controller:
                self.logger.error(f"{dev.name}: Protect not available")
                return
            parent_dev_id = dev.pluginProps.get("parentCameraId", "")
            parent_id_int = int(parent_dev_id) if str(parent_dev_id).isdigit() else None
            if parent_id_int not in indigo.devices:
                self.logger.error(f"{dev.name}: Parent camera device not found")
                return
            cam_id = indigo.devices[parent_id_int].pluginProps.get("cameraId", "")
            if not cam_id:
                self.logger.error(f"{dev.name}: Parent camera has no cameraId")
                return
            api = controller["protect_api"]
            api.delete_rtsps_streams(cam_id, ["package"])
            dev.updateStateOnServer("rtspUrlPackage", "")
            self._mirror_states(parent_id_int, [{"key": "rtspUrlPackage", "value": ""}])
            self.logger.info(f"{dev.name}: RTSP streaming disabled")
        except Exception as e:
            self.logger.error(f"disablePackageCameraRtsp error: {e}")
            if self.debug:
                self.logger.error(traceback.format_exc())

    def actionDumpCameraPayload(self, action):
        """Diagnostic: fetch this camera via both the bulk camera list and a
        fresh single-camera GET, log both in full, and report any
        difference in which top-level fields each one includes. Logs at
        info level (not gated behind debug) since this is an explicit,
        one-off diagnostic request, not routine chatter."""
        try:
            dev = indigo.devices[action.deviceId]
            cid, cam_id, api = self._get_camera_api(dev)
            if not api:
                return

            self.logger.info(f"{dev.name}: Running camera payload diagnostic…")

            bulk = {}
            single = {}

            try:
                api.refresh_cache()
                bulk = api._cameras_by_id.get(cam_id) or {}
                bulk_dump = json.dumps(bulk, default=str, indent=2)
                self.logger.info(f"[{dev.name}] Bulk list payload ({len(bulk)} top-level keys):\n{bulk_dump}")
            except Exception as e:
                self.logger.error(f"{dev.name}: Bulk list fetch failed: {e}")

            try:
                single = api.get_camera(cam_id) or {}
                single_dump = json.dumps(single, default=str, indent=2)
                self.logger.info(f"[{dev.name}] Single-camera GET payload ({len(single)} top-level keys):\n{single_dump}")
            except Exception as e:
                self.logger.error(f"{dev.name}: Single-camera fetch failed: {e}")

            try:
                bulk_keys   = set(bulk.keys())
                single_keys = set(single.keys())
                only_bulk   = sorted(bulk_keys - single_keys)
                only_single = sorted(single_keys - bulk_keys)
                if only_bulk or only_single:
                    self.logger.info(
                        f"[{dev.name}] Key differences between the two responses - "
                        f"only in bulk list: {only_bulk or 'none'} | "
                        f"only in single-camera GET: {only_single or 'none'}"
                    )
                else:
                    self.logger.info(f"[{dev.name}] Both responses have the same top-level keys.")
            except Exception:
                pass
        except Exception as e:
            self.logger.error(f"dumpCameraPayload error: {e}")
            if self.debug:
                self.logger.error(traceback.format_exc())

    # ------------------------------------------------------------------
    # Network device actions
    # ------------------------------------------------------------------

    def dumpNetworkDevicePayload(self, valuesDict, typeId, devId):
        """Diagnostic button in the device's own config dialog: fetch this
        device's current raw payload and log it in full. Logs at info
        level (not gated behind debug) since this is an explicit, one-off
        diagnostic request - same pattern as the camera payload dump.
        Exists specifically so a device type this plugin only partially
        recognises (a switch/AP/gateway model we haven't built dedicated
        states for) can be diagnosed on demand. Moved here from an Action
        Group action (3.11.3) at the user's request - makes more sense
        attached to the device you're actually looking at."""
        try:
            name = indigo.devices[devId].name if devId in indigo.devices else "this device"
            controller_id = int(valuesDict.get("controllerId", 0))
            controller    = self.controllers.get(controller_id)
            if not controller or "network_api" not in controller:
                self.logger.error(f"{name}: Cannot dump payload — Network not connected")
                return valuesDict
            mac = valuesDict.get("deviceMac", "")
            self.logger.info(f"{name}: Fetching current device payload…")
            d = controller["network_api"].get_device(mac)
            if not d:
                self.logger.error(f"{name}: Device not found on controller (mac={mac})")
                return valuesDict
            dump = json.dumps(d, default=str, indent=2)
            self.logger.info(
                f"[{name}] Raw device payload ({len(d)} top-level keys, "
                f"type={d.get('type', 'unknown')}, model={d.get('model', '?')}):\n{dump}"
            )
        except Exception as e:
            self.logger.error(f"dumpNetworkDevicePayload error: {e}")
            if self.debug:
                self.logger.error(traceback.format_exc())
        return valuesDict

    def actionRestartDevice(self, action):
        try:
            dev = indigo.devices[action.deviceId]
            cid, controller = self._get_controller_for_dev(dev)
            if not controller or "network_api" not in controller:
                return
            mac = dev.pluginProps.get("deviceMac", "")
            self.logger.info(f"{dev.name}: Restarting")
            controller["network_api"].restart_device(mac)
        except Exception as e:
            self.logger.error(f"restartDevice error: {e}")

    def actionSetDeviceLED(self, action):
        try:
            dev = indigo.devices[action.deviceId]
            cid, controller = self._get_controller_for_dev(dev)
            if not controller or "network_api" not in controller:
                return
            mac = dev.pluginProps.get("deviceMac", "")
            d = controller["network_api"].get_device(mac)
            if not d:
                return
            enabled = bool(action.props.get("enabled", True))
            controller["network_api"].set_device_led(d["_id"], enabled)
            self._update_netdev_states(cid, mac)
        except Exception as e:
            self.logger.error(f"setDeviceLED error: {e}")

    def actionLocateDevice(self, action):
        try:
            dev = indigo.devices[action.deviceId]
            cid, controller = self._get_controller_for_dev(dev)
            if not controller or "network_api" not in controller:
                return
            mac = dev.pluginProps.get("deviceMac", "")
            enabled = bool(action.props.get("enabled", True))
            controller["network_api"].locate_device(mac, enabled)
            self.logger.info(f"{dev.name}: Locate {'on' if enabled else 'off'}")
        except Exception as e:
            self.logger.error(f"locateDevice error: {e}")

    # ------------------------------------------------------------------
    # Client actions
    # ------------------------------------------------------------------

    def dumpClientPayload(self, valuesDict, typeId, devId):
        """Diagnostic button in the device's own config dialog: fetch this
        client's current raw payload and log it in full. Same pattern as
        dumpNetworkDevicePayload - moved here from an Action Group action
        (3.11.4) at the user's request."""
        try:
            name = indigo.devices[devId].name if devId in indigo.devices else "this client"
            controller_id = int(valuesDict.get("controllerId", 0))
            controller    = self.controllers.get(controller_id)
            if not controller or "network_api" not in controller:
                self.logger.error(f"{name}: Cannot dump payload — Network not connected")
                return valuesDict
            mac = valuesDict.get("clientMac", "")
            self.logger.info(f"{name}: Fetching current client payload…")
            c = controller["network_api"].get_client(mac)
            if not c:
                self.logger.error(f"{name}: Client not found on controller (mac={mac})")
                return valuesDict
            dump = json.dumps(c, default=str, indent=2)
            self.logger.info(
                f"[{name}] Raw client payload ({len(c)} top-level keys):\n{dump}"
            )
        except Exception as e:
            self.logger.error(f"dumpClientPayload error: {e}")
            if self.debug:
                self.logger.error(traceback.format_exc())
        return valuesDict

    def dumpDoorPayload(self, valuesDict, typeId, devId):
        """Diagnostic button in the device's own config dialog: fetch this
        door's current raw payload and log it in full. Same pattern as
        dumpNetworkDevicePayload/dumpClientPayload."""
        try:
            name = indigo.devices[devId].name if devId in indigo.devices else "this door"
            controller_id = int(valuesDict.get("controllerId", 0))
            controller    = self.controllers.get(controller_id)
            if not controller or "access_api" not in controller:
                self.logger.error(f"{name}: Cannot dump payload — Access not connected")
                return valuesDict
            door_id = valuesDict.get("doorId", "")
            self.logger.info(f"{name}: Fetching current door payload…")
            d = controller["access_api"].get_door(door_id)
            if not d:
                self.logger.error(f"{name}: Door not found on controller (doorId={door_id})")
                return valuesDict
            dump = json.dumps(d, default=str, indent=2)
            self.logger.info(
                f"[{name}] Raw door payload ({len(d)} top-level keys):\n{dump}"
            )
        except Exception as e:
            self.logger.error(f"dumpDoorPayload error: {e}")
            if self.debug:
                self.logger.error(traceback.format_exc())
        return valuesDict

    def dumpSensorPayload(self, valuesDict, typeId, devId):
        """Diagnostic button in the device's own config dialog: fetch this
        sensor's current raw payload and log it in full. Same pattern as
        dumpDoorPayload/dumpNetworkDevicePayload."""
        try:
            name = indigo.devices[devId].name if devId in indigo.devices else "this sensor"
            controller_id = int(valuesDict.get("controllerId", 0))
            controller    = self.controllers.get(controller_id)
            if not controller or "protect_api" not in controller:
                self.logger.error(f"{name}: Cannot dump payload — Protect not connected")
                return valuesDict
            sensor_id = valuesDict.get("sensorId", "")
            self.logger.info(f"{name}: Fetching current sensor payload…")
            s = controller["protect_api"].get_sensor(sensor_id)
            if not s:
                self.logger.error(f"{name}: Sensor not found on controller (sensorId={sensor_id})")
                return valuesDict
            dump = json.dumps(s, default=str, indent=2)
            self.logger.info(
                f"[{name}] Raw sensor payload ({len(s)} top-level keys):\n{dump}"
            )
        except Exception as e:
            self.logger.error(f"dumpSensorPayload error: {e}")
            if self.debug:
                self.logger.error(traceback.format_exc())
        return valuesDict

    def dumpPortPayload(self, valuesDict, typeId, devId):
        """Diagnostic button in the device's own config dialog: fetch this
        port's full raw entry from its parent switch's port_table and log
        it in full. A port has no standalone GET - it's read off the
        parent networkDevice's own payload, same as _update_port_states
        does for regular state refreshes."""
        try:
            name = indigo.devices[devId].name if devId in indigo.devices else "this port"
            controller_id = int(valuesDict.get("controllerId", 0))
            controller    = self.controllers.get(controller_id)
            if not controller or "network_api" not in controller:
                self.logger.error(f"{name}: Cannot dump payload — Network not connected")
                return valuesDict
            mac = valuesDict.get("deviceMac", "").lower()
            try:
                port_num = int(valuesDict.get("portNumber", 0))
            except (TypeError, ValueError):
                port_num = 0
            if not mac or port_num <= 0:
                self.logger.error(f"{name}: Missing parent switch/port number in config")
                return valuesDict
            self.logger.info(f"{name}: Fetching current port payload…")
            d = controller["network_api"].get_device(mac)
            if not d:
                self.logger.error(f"{name}: Parent switch not found on controller (mac={mac})")
                return valuesDict
            port = next((p for p in (d.get("port_table") or []) if p.get("port_idx") == port_num), None)
            if not port:
                self.logger.error(f"{name}: Port {port_num} not found in parent switch's port table")
                return valuesDict
            dump = json.dumps(port, default=str, indent=2)
            self.logger.info(
                f"[{name}] Raw port payload ({len(port)} top-level keys):\n{dump}"
            )
        except Exception as e:
            self.logger.error(f"dumpPortPayload error: {e}")
            if self.debug:
                self.logger.error(traceback.format_exc())
        return valuesDict

    def dumpFirewallPolicyPayload(self, valuesDict, typeId, devId):
        """Diagnostic button: fetch this policy's current raw payload and
        log it in full. Same pattern as dumpDoorPayload - the v2 API has
        no single-GET-by-id endpoint confirmed, so this fetches the full
        list and finds the matching record, same as the read/write methods
        themselves do."""
        try:
            name = indigo.devices[devId].name if devId in indigo.devices else "this policy"
            controller_id = int(valuesDict.get("controllerId", 0))
            controller    = self.controllers.get(controller_id)
            if not controller or "network_api" not in controller:
                self.logger.error(f"{name}: Cannot dump payload — Network not connected")
                return valuesDict
            policy_id = valuesDict.get("policyId", "")
            self.logger.info(f"{name}: Fetching current policy payload…")
            policies = controller["network_api"].get_firewall_policies()
            policy = next((p for p in policies if p.get("_id") == policy_id), None)
            if not policy:
                self.logger.error(f"{name}: Policy not found on controller (policyId={policy_id})")
                return valuesDict
            dump = json.dumps(policy, default=str, indent=2)
            self.logger.info(
                f"[{name}] Raw firewall policy payload ({len(policy)} top-level keys):\n{dump}"
            )
        except Exception as e:
            self.logger.error(f"dumpFirewallPolicyPayload error: {e}")
            if self.debug:
                self.logger.error(traceback.format_exc())
        return valuesDict

    def dumpTrafficRoutePayload(self, valuesDict, typeId, devId):
        """Diagnostic button: fetch this route's current raw payload and
        log it in full. Same pattern as dumpFirewallPolicyPayload."""
        try:
            name = indigo.devices[devId].name if devId in indigo.devices else "this route"
            controller_id = int(valuesDict.get("controllerId", 0))
            controller    = self.controllers.get(controller_id)
            if not controller or "network_api" not in controller:
                self.logger.error(f"{name}: Cannot dump payload — Network not connected")
                return valuesDict
            route_id = valuesDict.get("routeId", "")
            self.logger.info(f"{name}: Fetching current route payload…")
            routes = controller["network_api"].get_traffic_routes()
            route = next((r for r in routes if r.get("_id") == route_id), None)
            if not route:
                self.logger.error(f"{name}: Route not found on controller (routeId={route_id})")
                return valuesDict
            dump = json.dumps(route, default=str, indent=2)
            self.logger.info(
                f"[{name}] Raw traffic route payload ({len(route)} top-level keys):\n{dump}"
            )
        except Exception as e:
            self.logger.error(f"dumpTrafficRoutePayload error: {e}")
            if self.debug:
                self.logger.error(traceback.format_exc())
        return valuesDict

    def dumpPortForwardPayload(self, valuesDict, typeId, devId):
        """Diagnostic button: fetch this port forward rule's current raw
        payload and log it in full."""
        try:
            name = indigo.devices[devId].name if devId in indigo.devices else "this rule"
            controller_id = int(valuesDict.get("controllerId", 0))
            controller    = self.controllers.get(controller_id)
            if not controller or "network_api" not in controller:
                self.logger.error(f"{name}: Cannot dump payload — Network not connected")
                return valuesDict
            forward_id = valuesDict.get("forwardId", "")
            self.logger.info(f"{name}: Fetching current rule payload…")
            forwards = controller["network_api"].get_port_forwards()
            fwd = next((f for f in forwards if f.get("_id") == forward_id), None)
            if not fwd:
                self.logger.error(f"{name}: Rule not found on controller (forwardId={forward_id})")
                return valuesDict
            dump = json.dumps(fwd, default=str, indent=2)
            self.logger.info(
                f"[{name}] Raw port forward payload ({len(fwd)} top-level keys):\n{dump}"
            )
        except Exception as e:
            self.logger.error(f"dumpPortForwardPayload error: {e}")
            if self.debug:
                self.logger.error(traceback.format_exc())
        return valuesDict

    def dumpWLANPayload(self, valuesDict, typeId, devId):
        """Diagnostic button: fetch this WLAN's current raw payload and
        log it in full. The passphrase field is always redacted, even
        with debug logging on - this dump goes to a log file, which
        isn't where a WiFi password belongs."""
        try:
            name = indigo.devices[devId].name if devId in indigo.devices else "this WLAN"
            controller_id = int(valuesDict.get("controllerId", 0))
            controller    = self.controllers.get(controller_id)
            if not controller or "network_api" not in controller:
                self.logger.error(f"{name}: Cannot dump payload — Network not connected")
                return valuesDict
            wlan_id = valuesDict.get("wlanId", "")
            self.logger.info(f"{name}: Fetching current WLAN payload…")
            wlans = controller["network_api"].get_wlans()
            wlan = next((w for w in wlans if w.get("_id") == wlan_id), None)
            if not wlan:
                self.logger.error(f"{name}: WLAN not found on controller (wlanId={wlan_id})")
                return valuesDict
            redacted = dict(wlan)
            for key in ("x_passphrase", "x_iapp_key"):
                if key in redacted:
                    redacted[key] = "(redacted)"
            dump = json.dumps(redacted, default=str, indent=2)
            self.logger.info(
                f"[{name}] Raw WLAN payload ({len(wlan)} top-level keys, password redacted):\n{dump}"
            )
        except Exception as e:
            self.logger.error(f"dumpWLANPayload error: {e}")
            if self.debug:
                self.logger.error(traceback.format_exc())
        return valuesDict

    def dumpAlarmProfilePayload(self, valuesDict, typeId, devId):
        """Diagnostic button for the EXPERIMENTAL Alarm Manager feature -
        the single most important piece of it, since nothing here has
        been confirmed against real hardware. Deliberately dumps more than
        just what get_alarm_profiles()/get_alarm_state() currently look
        for: every top-level bootstrap key (so the overall structure is
        visible), plus the FULL content of every key that's plausibly
        alarm-related (by name, or because it's one of the hardware
        categories Alarm Manager's Global mode depends on - hubs, sirens,
        fobs, relays, locks, readers - or `nvr`, since profile data might
        be nested there). Cameras/sensors/users etc. are deliberately left
        as counts only - large, and not relevant to this specific gap."""
        try:
            name = indigo.devices[devId].name if devId in indigo.devices else "this alarm profile"
            controller_id = int(valuesDict.get("controllerId", 0))
            controller    = self.controllers.get(controller_id)
            if not controller or "protect_api" not in controller:
                self.logger.error(f"{name}: Cannot dump payload — Protect not connected")
                return valuesDict
            api = controller["protect_api"]
            self.logger.info(f"{name}: Fetching current Alarm Manager diagnostic data…")

            data = api.get_alarm_debug_dump() or {}
            top_level_summary = {
                k: (f"array[{len(v)}]" if isinstance(v, list) else type(v).__name__)
                for k, v in data.items()
            }
            alarm_related_keys = [k for k in data if re.search(r"arm|alarm", k, re.IGNORECASE)]
            always_include_keys = ["hubs", "sirens", "fobs", "relays", "locks", "readers", "nvr"]
            detail_keys = sorted(set(alarm_related_keys) | set(k for k in always_include_keys if k in data))
            detail = {k: data[k] for k in detail_keys}

            profiles = api.get_alarm_profiles()
            state    = api.get_alarm_state()

            self.logger.info(
                f"[{name}] Alarm Manager diagnostic dump\n"
                f"=== Bootstrap top-level keys (type/count only) ===\n"
                f"{json.dumps(top_level_summary, indent=2, default=str)}\n"
                f"=== Full content of plausibly-relevant keys ({', '.join(detail_keys) or 'none found'}) ===\n"
                f"{json.dumps(detail, indent=2, default=str)}\n"
                f"=== What get_alarm_profiles() currently extracts from the above ===\n"
                f"{json.dumps(profiles, indent=2, default=str)}\n"
                f"=== What get_alarm_state() currently extracts from the above ===\n"
                f"{json.dumps(state, indent=2, default=str)}"
            )
        except Exception as e:
            self.logger.error(f"dumpAlarmProfilePayload error: {e}")
            if self.debug:
                self.logger.error(traceback.format_exc())
        return valuesDict

    def dumpPackageCameraPayload(self, valuesDict, typeId, devId):
        """Diagnostic button: fetch this lens's current RTSP stream status
        (official, confirmed) plus the parent camera's package-related
        flags from the legacy bootstrap where that's configured (confirmed
        real but legacy-only - see Devices.xml's note on this device
        type)."""
        try:
            name = indigo.devices[devId].name if devId in indigo.devices else "this package camera"
            controller_id = int(valuesDict.get("controllerId", 0))
            controller    = self.controllers.get(controller_id)
            if not controller or "protect_api" not in controller:
                self.logger.error(f"{name}: Cannot dump payload — Protect not connected")
                return valuesDict
            api = controller["protect_api"]
            parent_dev_id = valuesDict.get("parentCameraId", "")
            parent_id_int = int(parent_dev_id) if str(parent_dev_id).isdigit() else None
            if parent_id_int not in indigo.devices:
                self.logger.error(f"{name}: Parent camera device not found")
                return valuesDict
            cam_id = indigo.devices[parent_id_int].pluginProps.get("cameraId", "")
            if not cam_id:
                self.logger.error(f"{name}: Parent camera has no cameraId")
                return valuesDict

            self.logger.info(f"{name}: Fetching current package camera payload…")
            rtsp_streams = api.get_rtsps_streams(cam_id) or {}

            bootstrap = api.get_bootstrap_raw() or {}
            bcam = next((c for c in (bootstrap.get("cameras") or [])
                         if isinstance(c, dict) and c.get("id") == cam_id), None)
            package_flags = {}
            if bcam:
                package_flags = {k: v for k, v in bcam.items() if "package" in k.lower()}
                ff = bcam.get("featureFlags") or {}
                package_flags.update({f"featureFlags.{k}": v for k, v in ff.items() if "package" in k.lower()})

            self.logger.info(
                f"[{name}] Package camera diagnostic dump\n"
                f"=== RTSP streams (official API, cameraId={cam_id}) ===\n"
                f"{json.dumps(rtsp_streams, indent=2, default=str)}\n"
                f"=== Package-related flags from legacy bootstrap "
                f"({'found' if bcam else 'no legacy data available/camera not found there'}) ===\n"
                f"{json.dumps(package_flags, indent=2, default=str)}"
            )
        except Exception as e:
            self.logger.error(f"dumpPackageCameraPayload error: {e}")
            if self.debug:
                self.logger.error(traceback.format_exc())
        return valuesDict

    def actionBlockClient(self, action):
        try:
            dev = indigo.devices[action.deviceId]
            cid, controller = self._get_controller_for_dev(dev)
            if not controller or "network_api" not in controller:
                return
            mac = dev.pluginProps.get("clientMac", "")
            controller["network_api"].block_client(mac)
            self.logger.info(f"{dev.name}: Blocked")
            self._update_client_states(cid, mac.lower())
        except Exception as e:
            self.logger.error(f"blockClient error: {e}")

    def actionUnblockClient(self, action):
        try:
            dev = indigo.devices[action.deviceId]
            cid, controller = self._get_controller_for_dev(dev)
            if not controller or "network_api" not in controller:
                return
            mac = dev.pluginProps.get("clientMac", "")
            controller["network_api"].unblock_client(mac)
            self.logger.info(f"{dev.name}: Unblocked")
            self._update_client_states(cid, mac.lower())
        except Exception as e:
            self.logger.error(f"unblockClient error: {e}")

    def actionReconnectClient(self, action):
        try:
            dev = indigo.devices[action.deviceId]
            cid, controller = self._get_controller_for_dev(dev)
            if not controller or "network_api" not in controller:
                return
            mac = dev.pluginProps.get("clientMac", "")
            controller["network_api"].reconnect_client(mac)
            self.logger.info(f"{dev.name}: Reconnect requested")
        except Exception as e:
            self.logger.error(f"reconnectClient error: {e}")

    # ------------------------------------------------------------------
    # Door action
    # ------------------------------------------------------------------

    def actionUnlockDoor(self, action):
        """Momentary unlock - available as its own Action Group action
        regardless of the device's own unlockMode setting (see
        actionControlDevice's accessDoor branch, same underlying calls)."""
        try:
            dev = indigo.devices[action.deviceId]
            cid, controller = self._get_controller_for_dev(dev)
            if not controller or "access_api" not in controller:
                return
            door_id = dev.pluginProps.get("doorId", "")
            controller["access_api"].unlock_door(door_id)
            controller.setdefault("door_relock_pending", {})[door_id] = {
                "unlocked_at": time.time(), "opened": False,
            }
            self.logger.info(f"{dev.name}: Unlocked (momentary)")
            self._update_door_states(cid, door_id)
        except Exception as e:
            self.logger.error(f"unlockDoor error: {e}")

    def actionUnlockDoorLeaveUnlocked(self, action):
        """Leave-unlocked - available as its own Action Group action
        regardless of the device's own unlockMode setting."""
        try:
            dev = indigo.devices[action.deviceId]
            cid, controller = self._get_controller_for_dev(dev)
            if not controller or "access_api" not in controller:
                return
            door_id = dev.pluginProps.get("doorId", "")
            controller["access_api"].set_door_lock_rule(door_id, "keep_unlock")
            controller.get("door_relock_pending", {}).pop(door_id, None)
            self.logger.info(f"{dev.name}: Unlocked (leave unlocked)")
            self._update_door_states(cid, door_id)
        except Exception as e:
            self.logger.error(f"unlockDoorLeaveUnlocked error: {e}")

    def actionLockDoor(self, action):
        """Explicit lock - available as its own Action Group action,
        same as the device's own Lock button."""
        try:
            dev = indigo.devices[action.deviceId]
            cid, controller = self._get_controller_for_dev(dev)
            if not controller or "access_api" not in controller:
                return
            door_id = dev.pluginProps.get("doorId", "")
            controller["access_api"].set_door_lock_rule(door_id, "lock_now")
            controller.get("door_relock_pending", {}).pop(door_id, None)
            self.logger.info(f"{dev.name}: Locked")
            self._update_door_states(cid, door_id)
        except Exception as e:
            self.logger.error(f"lockDoor error: {e}")

    def actionChangeWLANPassword(self, action):
        """Change a WLAN's passphrase. The new password itself is never
        logged, in either the success or error path - only that a change
        was attempted and whether it succeeded."""
        try:
            dev = indigo.devices[action.deviceId]
            cid, controller = self._get_controller_for_dev(dev)
            if not controller or "network_api" not in controller:
                self.logger.error(f"{dev.name}: Cannot change password — Network not connected")
                return
            wlan_id = dev.pluginProps.get("wlanId", "")
            new_password = action.props.get("newPassword", "")
            if not new_password:
                self.logger.error(f"{dev.name}: No new password provided")
                return
            controller["network_api"].set_wlan_password(wlan_id, new_password)
            self.logger.info(f"{dev.name}: Password changed")
            self._update_wlan_states(cid, wlan_id)
        except Exception as e:
            self.logger.error(f"changeWLANPassword error: {e}")
            if self.debug:
                self.logger.error(traceback.format_exc())

    # ------------------------------------------------------------------
    # Drive (UNAS) actions — these target the unifiDriveController device
    # directly, since all UNAS state now lives on the controller itself.
    # ------------------------------------------------------------------

    def _get_drive_controller(self, action):
        """Resolve action.deviceId -> (controller_id, controller_dict, device).
        Returns (None, None, None) if not a connected Drive controller."""
        try:
            dev = indigo.devices[action.deviceId]
        except Exception:
            return None, None, None
        controller = self.controllers.get(dev.id)
        if not controller or "drive_api" not in controller:
            return None, None, dev
        return dev.id, controller, dev

    def actionRebootUNAS(self, action):
        cid, controller, dev = self._get_drive_controller(action)
        if not controller:
            name = dev.name if dev else f"device {action.deviceId}"
            self.logger.error(f"{name}: Cannot reboot — UNAS not connected")
            return
        try:
            controller["drive_api"].reboot()
            self.logger.info(f"{dev.name}: Reboot command sent")
            try:
                dev.updateStateOnServer("status", "Rebooting…")
            except Exception:
                pass
        except UnifiAuthError as e:
            self.logger.error(f"rebootUNAS auth error: {e}")
        except Exception as e:
            self.logger.error(f"rebootUNAS error: {e}")

    def actionShutdownUNAS(self, action):
        cid, controller, dev = self._get_drive_controller(action)
        if not controller:
            name = dev.name if dev else f"device {action.deviceId}"
            self.logger.error(f"{name}: Cannot shutdown — UNAS not connected")
            return
        try:
            controller["drive_api"].shutdown()
            self.logger.info(f"{dev.name}: Shutdown command sent")
            try:
                dev.updateStateOnServer("status", "Shutting down…")
            except Exception:
                pass
        except UnifiAuthError as e:
            self.logger.error(f"shutdownUNAS auth error: {e}")
        except Exception as e:
            self.logger.error(f"shutdownUNAS error: {e}")

    def _apply_fan_mode(self, controller_id, controller, canonical_mode, dev_for_log=None):
        """Apply a fan mode. Prefers SSH (if deployed and active) over REST
        (which is read-only on current UNAS firmware).

        Returns ("ssh"|"api", success_bool, error_message_or_None).
        """
        log_name = dev_for_log.name if dev_for_log is not None else "fan"
        canonical_mode_lower = canonical_mode.lower()

        # Prefer SSH if the fan control service is deployed
        ssh = controller.get("drive_ssh") if controller else None
        if ssh is not None:
            try:
                if ssh.is_deployed():
                    # Map our canonical names (Quiet/Balance/Cooling) to the
                    # script's lowercase mode tokens. "Default" is also a valid
                    # value that hands control back to firmware.
                    ssh_mode = {
                        "quiet":   "quiet",
                        "balance": "balance",
                        "cooling": "cooling",
                        "default": "default",
                    }.get(canonical_mode_lower, canonical_mode_lower)
                    ssh.set_mode(ssh_mode)
                    self.logger.info(f"{log_name}: Fan mode set via SSH to {ssh_mode}")
                    # Update deployment-state cache on the controller device
                    cdev = controller.get("device")
                    if cdev is not None:
                        try:
                            cdev.updateStateOnServer("fanControlActiveMode", ssh_mode)
                        except Exception:
                            pass
                    return ("ssh", True, None)
                else:
                    if self.debug:
                        self.logger.debug(
                            f"{log_name}: SSH configured but fan control not deployed — "
                            f"falling through to REST API (which is read-only on current firmware)"
                        )
            except UnifiSSHError as e:
                self.logger.error(f"{log_name}: SSH fan mode set failed: {e}")
                # Don't fall through to API — SSH is the intended path. Surface error.
                return ("ssh", False, str(e))

        # Fallback: REST API (will fail on current firmware but try anyway)
        api = controller.get("drive_api") if controller else None
        if api is None:
            return ("api", False, "Drive API not available")
        try:
            api.set_fan_mode(canonical_mode)
            self.logger.info(f"{log_name}: Fan mode set via REST to {canonical_mode}")
            return ("api", True, None)
        except Exception as e:
            return ("api", False, str(e))

    def actionSetFanMode(self, action):
        """Set fan mode. This action targets a driveFan device, not the controller —
        the fan is still a separate child device."""
        try:
            dev = indigo.devices[action.deviceId]
            cid, controller = self._get_controller_for_dev(dev)
            if not controller:
                self.logger.error(f"{dev.name}: Cannot set fan mode — UNAS not connected")
                return
            mode = action.props.get("fanMode", "Balance")
            if mode not in DRIVE_FAN_MODE_OPTIONS:
                self.logger.error(f"{dev.name}: Unknown fan mode: {mode}")
                return
            method, ok, err = self._apply_fan_mode(cid, controller, mode, dev_for_log=dev)
            if not ok:
                self.logger.error(f"{dev.name}: Fan mode set FAILED via {method}: {err}")
                return
            self._update_fan_states(cid)
        except Exception as e:
            self.logger.error(f"setFanMode error: {e}")

    def actionWakeUNAS(self, action):
        """Send a Wake-on-LAN magic packet to the UNAS using its MAC."""
        cid, controller, dev = self._get_drive_controller(action)
        if not controller:
            name = dev.name if dev else f"device {action.deviceId}"
            self.logger.error(f"{name}: Cannot wake — no controller")
            return
        try:
            # Use the cached MAC from controller states if available
            mac = dev.states.get("macAddress") or ""
            if not mac:
                self.logger.error(f"{dev.name}: Cannot wake — no MAC address known. "
                                  f"The system info doesn't expose a MAC; you may need to set it manually.")
                return

            # Normalise: strip separators, lowercase
            mac_hex = "".join(c for c in mac if c.isalnum()).lower()
            if len(mac_hex) != 12:
                self.logger.error(f"{dev.name}: MAC '{mac}' is not a valid 6-byte address")
                return

            try:
                mac_bytes = bytes.fromhex(mac_hex)
            except ValueError:
                self.logger.error(f"{dev.name}: MAC '{mac}' could not be parsed as hex")
                return

            magic = b"\xff" * 6 + mac_bytes * 16

            # Send on UDP port 9 to both the broadcast address and the UNAS's IP
            # (covers most network setups).
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                for target in ("255.255.255.255", "<broadcast>"):
                    try:
                        sock.sendto(magic, (target, 9))
                    except Exception:
                        pass
                # Also try the UNAS IP directly (in case router blocks broadcast)
                address = controller.get("address")
                if address:
                    try:
                        sock.sendto(magic, (address, 9))
                    except Exception:
                        pass
            finally:
                sock.close()

            self.logger.info(f"{dev.name}: Wake-on-LAN magic packet sent to {mac}")
        except Exception as e:
            self.logger.error(f"wakeUNAS error: {e}")

    # ------------------------------------------------------------------
    # Relay / Dimmer control (called by Indigo when user toggles on/off)
    # ------------------------------------------------------------------

    def actionControlDevice(self, action, dev):
        """Handle relay (on/off) actions for ports, clients, network devices, doors.
        driveFan is type=speedcontrol (not dimmer/relay) as of 3.10.0, so
        Indigo routes its actions to actionControlSpeedControl directly -
        no delegation needed here any more."""
        try:
            cmd = action.deviceAction
            type_id = dev.deviceTypeId

            # Helper to determine on/off. Lock/Unlock are confirmed as
            # their own distinct indigo.kDeviceAction values (Indigo's
            # official action-class docs list them separately from
            # TurnOn/TurnOff/Toggle, each other's "complementary action") -
            # sent instead of TurnOn/TurnOff for IsLockSubType devices
            # (accessDoor). Previously unhandled entirely, which is exactly
            # why the Lock/Unlock buttons did nothing: every check below
            # missed, target stayed None, and the function returned without
            # doing anything.
            def is_on():
                if cmd == indigo.kDeviceAction.TurnOn:
                    return True
                if cmd == indigo.kDeviceAction.TurnOff:
                    return False
                if cmd == indigo.kDeviceAction.Toggle:
                    return not dev.onState
                if cmd == indigo.kDeviceAction.Lock:
                    return True
                if cmd == indigo.kDeviceAction.Unlock:
                    return False
                return None

            target = is_on()
            if target is None:
                return

            cid, controller = self._get_controller_for_dev(dev)
            if not controller:
                return

            if type_id == "unifiPort":
                api = controller.get("network_api")
                if not api:
                    return
                control_mode = dev.pluginProps.get("controlMode", "poe")
                mac = dev.pluginProps.get("deviceMac", "")
                try:
                    pidx = int(dev.pluginProps.get("portNumber", 0))
                except (TypeError, ValueError):
                    return
                d = api.get_device(mac)
                if not d:
                    return
                if control_mode == "poe":
                    api.set_port_poe_mode(d["_id"], pidx, "auto" if target else "off")
                    self.logger.info(f"{dev.name}: PoE {'on' if target else 'off'}")
                elif control_mode == "powercycle":
                    if target:
                        api.cycle_port_poe(mac, pidx)
                        self.logger.info(f"{dev.name}: Power-cycled")
                    # off has no meaning for powercycle; ignore
                self._update_port_states(cid, mac, pidx)

            elif type_id == "clientDevice":
                api = controller.get("network_api")
                if not api:
                    return
                mac = dev.pluginProps.get("clientMac", "")
                if target:
                    api.unblock_client(mac)
                else:
                    api.block_client(mac)
                self.logger.info(f"{dev.name}: {'Unblocked' if target else 'Blocked'}")
                self._update_client_states(cid, mac.lower())

            elif type_id == "networkDevice":
                api = controller.get("network_api")
                if not api:
                    return
                mac = dev.pluginProps.get("deviceMac", "")
                d = api.get_device(mac)
                if not d:
                    return
                api.disable_device(d["_id"], disabled=not target)
                self.logger.info(f"{dev.name}: {'Enabled' if target else 'Disabled'}")
                self._update_netdev_states(cid, mac)

            elif type_id == "accessDoor":
                api = controller.get("access_api")
                if not api:
                    return
                door_id = dev.pluginProps.get("doorId", "")
                # target matches onOffState's convention (True=Locked,
                # False=Unlocked, see 3.11.8).
                if target:
                    # Lock: confirmed real endpoint (lock_rule, type=
                    # lock_now) - cancels any active unlock/leave-unlocked
                    # rule and locks immediately. Also cancel our own
                    # pending auto-relock timer for this door, if any -
                    # we've just locked it explicitly, so there's nothing
                    # left for the timer to do.
                    api.set_door_lock_rule(door_id, "lock_now")
                    controller.get("door_relock_pending", {}).pop(door_id, None)
                    self.logger.info(f"{dev.name}: Locked")
                else:
                    # Unlock: which API call depends on unlockMode -
                    # "leaveUnlocked" is a real, separate rule (keep_unlock)
                    # from a plain momentary unlock, confirmed via a
                    # working openHAB UniFi Access binding (independent
                    # source). Only momentary mode starts the auto-relock
                    # timer (_check_door_relocks) - leave-unlocked is
                    # supposed to stay unlocked indefinitely, by design.
                    mode = dev.pluginProps.get("unlockMode", "momentary")
                    if mode == "leaveUnlocked":
                        api.set_door_lock_rule(door_id, "keep_unlock")
                        controller.get("door_relock_pending", {}).pop(door_id, None)
                        self.logger.info(f"{dev.name}: Unlocked (leave unlocked)")
                    else:
                        api.unlock_door(door_id)
                        controller.setdefault("door_relock_pending", {})[door_id] = {
                            "unlocked_at": time.time(), "opened": False,
                        }
                        self.logger.info(f"{dev.name}: Unlocked (momentary)")
                self._update_door_states(cid, door_id)

            elif type_id == "firewallPolicy":
                api = controller.get("network_api")
                if not api:
                    return
                policy_id = dev.pluginProps.get("policyId", "")
                api.set_firewall_policy_enabled(policy_id, bool(target))
                self.logger.info(f"{dev.name}: {'Enabled' if target else 'Disabled'}")
                self._update_firewallpolicy_states(cid, policy_id)

            elif type_id == "trafficRoute":
                api = controller.get("network_api")
                if not api:
                    return
                route_id = dev.pluginProps.get("routeId", "")
                api.set_traffic_route_enabled(route_id, bool(target))
                self.logger.info(f"{dev.name}: {'Enabled' if target else 'Disabled'}")
                self._update_trafficroute_states(cid, route_id)

            elif type_id == "portForward":
                api = controller.get("network_api")
                if not api:
                    return
                forward_id = dev.pluginProps.get("forwardId", "")
                api.set_port_forward_enabled(forward_id, bool(target))
                self.logger.info(f"{dev.name}: {'Enabled' if target else 'Disabled'}")
                self._update_portforward_states(cid, forward_id)

            elif type_id == "wlanDevice":
                api = controller.get("network_api")
                if not api:
                    return
                wlan_id = dev.pluginProps.get("wlanId", "")
                api.set_wlan_enabled(wlan_id, bool(target))
                self.logger.info(f"{dev.name}: {'Enabled' if target else 'Disabled'}")
                self._update_wlan_states(cid, wlan_id)

            elif type_id == "alarmProfile":
                # EXPERIMENTAL - see get_alarm_profiles()/set_alarm_arm()
                # docstrings. Errors here (especially auth failures) are
                # logged with extra detail by the API methods themselves,
                # since that's the most likely failure mode reported by
                # the one community reference this is built from.
                api = controller.get("protect_api")
                if not api:
                    return
                profile_id = dev.pluginProps.get("armProfileId", "")
                if target:
                    api.set_alarm_arm(profile_id)
                    self.logger.info(f"{dev.name}: Armed")
                else:
                    api.set_alarm_disarm(profile_id)
                    self.logger.info(f"{dev.name}: Disarmed")
                self._update_alarmprofile_states(cid, profile_id)

        except Exception as e:
            self.logger.error(f"actionControlDevice error: {e}")
            if self.debug:
                self.logger.error(traceback.format_exc())

    def actionControlSpeedControl(self, action, dev):
        """Handle speed control actions for the UNAS fan (driveFan, type=
        speedcontrol as of 3.10.0 - previously modeled as a dimmer with fan
        modes mapped onto 0/50/100% brightness, which was a semantic
        mismatch: this is a fixed set of named modes, not a continuous
        level. speedcontrol is Indigo's actual built-in type for exactly
        this ("a fixed number of speeds" - see speedIndex/speedIndexCount
        in the official Device Base Class reference).

        Callback name, its (action, dev) signature, and the
        kSpeedControlAction.* enum confirmed against the real source of a
        production Indigo plugin (FlyingDiver/Indigo-Masquerade, MIT-style
        open source) rather than assumed by analogy with
        actionControlDimmerRelay's (action, dev) convention - an initial
        single-arg draft would have been wrong, exactly the kind of detail
        that bit us guessing at Devices.xml behaviour earlier this session.

        Default speedIndexCount is 4 (confirmed: "The Indigo UI only
        allows for 4 speeds - Off, Low, Medium, High"), which maps
        directly onto our four real modes with no XML override needed:
        0=Default (hand control back to firmware), 1=Quiet, 2=Balance,
        3=Cooling.
        """
        try:
            if dev.deviceTypeId != "driveFan":
                return

            cid, controller = self._get_controller_for_dev(dev)
            if not controller or "drive_api" not in controller:
                self.logger.error(f"{dev.name}: Cannot set fan — Drive controller not connected")
                return

            cmd = action.speedControlAction
            index_to_mode = {0: "Default", 1: "Quiet", 2: "Balance", 3: "Cooling"}
            mode_to_index = {v: k for k, v in index_to_mode.items()}

            if cmd == indigo.kSpeedControlAction.SetSpeedIndex:
                mode = index_to_mode.get(int(action.actionValue), "Balance")
            elif cmd == indigo.kSpeedControlAction.SetSpeedLevel:
                # 0-100 -> nearest of our 4 indices (0/33/66/100) - the same
                # convention other 4-speed Indigo devices use, for
                # HomeKit/Alexa-style percentage control.
                idx = round(int(action.actionValue) / 100 * 3)
                mode = index_to_mode.get(idx, "Balance")
            elif cmd in (indigo.kSpeedControlAction.TurnOn, indigo.kSpeedControlAction.Toggle):
                mode = "Default"
            elif cmd == indigo.kSpeedControlAction.TurnOff:
                # There's no real "off" for a NAS cooling fan - turning it
                # off entirely risks the hardware overheating, so this is
                # deliberately a no-op with a clear log message rather than
                # silently doing nothing or picking an arbitrary mode.
                self.logger.warning(f"{dev.name}: UNAS fan control has no 'Off' mode "
                                    f"(the fan can't be safely stopped) - ignoring")
                return
            elif cmd == indigo.kSpeedControlAction.IncreaseSpeedIndex:
                cur = mode_to_index.get(dev.states.get("fanMode"), dev.speedIndex or 0)
                mode = index_to_mode.get(min(cur + 1, 3), "Balance")
            elif cmd == indigo.kSpeedControlAction.DecreaseSpeedIndex:
                cur = mode_to_index.get(dev.states.get("fanMode"), dev.speedIndex or 0)
                mode = index_to_mode.get(max(cur - 1, 0), "Balance")
            elif cmd == indigo.kSpeedControlAction.RequestStatus:
                self._update_fan_states(cid)
                return
            else:
                if self.debug:
                    self.logger.debug(f"{dev.name}: Unhandled speed control action: {cmd}")
                return

            method, ok, err = self._apply_fan_mode(cid, controller, mode, dev_for_log=dev)
            if not ok:
                self.logger.error(f"{dev.name}: Fan mode set FAILED via {method}: {err}")
                return

            # Refresh the actual reported state - confirms the write took.
            self._update_fan_states(cid)
        except Exception as e:
            self.logger.error(f"actionControlSpeedControl error: {e}")
            if self.debug:
                self.logger.error(traceback.format_exc())

    # ------------------------------------------------------------------
    # Additional action handlers (compatibility with existing Actions.xml)
    # ------------------------------------------------------------------

    def actionRestartController(self, action):
        """Reboot the UniFi controller (UDM Pro etc) via the legacy power command."""
        try:
            dev = indigo.devices[action.deviceId]
            cid, controller = self._get_controller_for_dev(dev)
            if not controller or "network_api" not in controller:
                self.logger.error(f"{dev.name}: Network API not available")
                return
            # Legacy: POST /api/system/reboot — only available with legacy auth
            api = controller["network_api"]
            self.logger.info(f"{dev.name}: Rebooting controller (this will take several minutes)")
            try:
                api._legacy_request("POST", "/api/system/reboot")
            except Exception as e:
                self.logger.warning(f"Reboot command may have triggered disconnect: {e}")
        except Exception as e:
            self.logger.error(f"restartController error: {e}")

    def actionDisableDevice(self, action):
        try:
            dev = indigo.devices[action.deviceId]
            cid, controller = self._get_controller_for_dev(dev)
            if not controller or "network_api" not in controller:
                return
            api = controller["network_api"]
            d   = api.get_device(dev.pluginProps.get("deviceMac", ""))
            if not d:
                return
            api.disable_device(d["_id"], disabled=True)
            self.logger.info(f"{dev.name}: Disabled")
            self._update_netdev_states(cid, dev.pluginProps.get("deviceMac", "").lower())
        except Exception as e:
            self.logger.error(f"disableDevice error: {e}")

    def actionEnableDevice(self, action):
        try:
            dev = indigo.devices[action.deviceId]
            cid, controller = self._get_controller_for_dev(dev)
            if not controller or "network_api" not in controller:
                return
            api = controller["network_api"]
            d   = api.get_device(dev.pluginProps.get("deviceMac", ""))
            if not d:
                return
            api.disable_device(d["_id"], disabled=False)
            self.logger.info(f"{dev.name}: Enabled")
            self._update_netdev_states(cid, dev.pluginProps.get("deviceMac", "").lower())
        except Exception as e:
            self.logger.error(f"enableDevice error: {e}")

    def actionToggleDevice(self, action):
        try:
            dev = indigo.devices[action.deviceId]
            currently_disabled = bool(dev.states.get("disabled", False))
            if currently_disabled:
                self.actionEnableDevice(action)
            else:
                self.actionDisableDevice(action)
        except Exception as e:
            self.logger.error(f"toggleDevice error: {e}")

    def actionToggleDeviceLED(self, action):
        try:
            dev = indigo.devices[action.deviceId]
            cid, controller = self._get_controller_for_dev(dev)
            if not controller or "network_api" not in controller:
                return
            api = controller["network_api"]
            d   = api.get_device(dev.pluginProps.get("deviceMac", ""))
            if not d:
                return
            currently_on = bool(dev.states.get("ledEnabled", False))
            api.set_device_led(d["_id"], not currently_on)
            self.logger.info(f"{dev.name}: LED {'off' if currently_on else 'on'}")
            self._update_netdev_states(cid, dev.pluginProps.get("deviceMac", "").lower())
        except Exception as e:
            self.logger.error(f"toggleDeviceLED error: {e}")

    # ----- Port actions -----

    def actionEnablePort(self, action):
        try:
            dev = indigo.devices[action.deviceId]
            cid, controller = self._get_controller_for_dev(dev)
            if not controller or "network_api" not in controller:
                return
            api = controller["network_api"]
            mac = dev.pluginProps.get("deviceMac", "")
            try:
                pidx = int(dev.pluginProps.get("portNumber", 0))
            except (TypeError, ValueError):
                return
            d = api.get_device(mac)
            if not d:
                return
            control_mode = dev.pluginProps.get("controlMode", "poe")
            if control_mode == "poe":
                api.set_port_poe_mode(d["_id"], pidx, "auto")
            else:
                api.set_port_override(d["_id"], pidx, {"port_idx": pidx, "forward": "all"})
            self.logger.info(f"{dev.name}: Enabled")
            self._update_port_states(cid, mac.lower(), pidx)
        except Exception as e:
            self.logger.error(f"enablePort error: {e}")

    def actionDisablePort(self, action):
        try:
            dev = indigo.devices[action.deviceId]
            cid, controller = self._get_controller_for_dev(dev)
            if not controller or "network_api" not in controller:
                return
            api = controller["network_api"]
            mac = dev.pluginProps.get("deviceMac", "")
            try:
                pidx = int(dev.pluginProps.get("portNumber", 0))
            except (TypeError, ValueError):
                return
            d = api.get_device(mac)
            if not d:
                return
            control_mode = dev.pluginProps.get("controlMode", "poe")
            if control_mode == "poe":
                api.set_port_poe_mode(d["_id"], pidx, "off")
            else:
                api.set_port_override(d["_id"], pidx, {"port_idx": pidx, "forward": "disabled"})
            self.logger.info(f"{dev.name}: Disabled")
            self._update_port_states(cid, mac.lower(), pidx)
        except Exception as e:
            self.logger.error(f"disablePort error: {e}")

    def actionCyclePort(self, action):
        try:
            dev = indigo.devices[action.deviceId]
            cid, controller = self._get_controller_for_dev(dev)
            if not controller or "network_api" not in controller:
                return
            api = controller["network_api"]
            mac = dev.pluginProps.get("deviceMac", "")
            try:
                pidx = int(dev.pluginProps.get("portNumber", 0))
            except (TypeError, ValueError):
                return
            api.cycle_port_poe(mac, pidx)
            self.logger.info(f"{dev.name}: Power-cycled")
            self._update_port_states(cid, mac.lower(), pidx)
        except Exception as e:
            self.logger.error(f"cyclePort error: {e}")

    def actionSetPoeMode(self, action):
        try:
            dev  = indigo.devices[action.deviceId]
            mode = action.props.get("poeMode", "auto")
            cid, controller = self._get_controller_for_dev(dev)
            if not controller or "network_api" not in controller:
                return
            api = controller["network_api"]
            mac = dev.pluginProps.get("deviceMac", "")
            try:
                pidx = int(dev.pluginProps.get("portNumber", 0))
            except (TypeError, ValueError):
                return
            d = api.get_device(mac)
            if not d:
                return
            api.set_port_poe_mode(d["_id"], pidx, mode)
            self.logger.info(f"{dev.name}: PoE mode set to {mode}")
            self._update_port_states(cid, mac.lower(), pidx)
        except Exception as e:
            self.logger.error(f"setPoeMode error: {e}")

    # ----- Client discovery (called from controller-scoped action menu) -----

    def actionDiscoverAllClients(self, action):
        try:
            dev = indigo.devices[action.deviceId]
            try:
                min_days = int(action.props.get("minLastSeen", 30))
            except (TypeError, ValueError):
                min_days = 30
            self._discover_clients(dev.id, include_known=True, min_days=min_days)
        except Exception as e:
            self.logger.error(f"discoverAllClients error: {e}")

    def actionDiscoverUnknownClients(self, action):
        try:
            dev = indigo.devices[action.deviceId]
            self._discover_clients(dev.id, include_known=False, min_days=0)
        except Exception as e:
            self.logger.error(f"discoverUnknownClients error: {e}")

    def actionRemoveMissingClients(self, action):
        try:
            dev = indigo.devices[action.deviceId]
            self._remove_missing_clients(dev.id)
        except Exception as e:
            self.logger.error(f"removeMissingClients error: {e}")

    def actionPurgeRandomisedClients(self, action):
        """Purge randomised-MAC clients that haven't been seen recently.

        Behaviour is governed by the controller's pluginProps:
          - purgeMinAgeHours      (default 24)
          - purgeSkipNamed        (default true)
          - purgeSkipMetadata     (default true)
          - purgeSkipIndigoDevice (default true)
          - purgeDryRun           (default false)
        """
        try:
            dev = indigo.devices[action.deviceId]
            self._purge_randomised_clients(dev.id)
        except Exception as e:
            self.logger.error(f"purgeRandomisedClients error: {e}")
            if self.debug:
                self.logger.error(traceback.format_exc())

    def _purge_randomised_clients(self, controller_id):
        controller = self.controllers.get(controller_id)
        if not controller or "network_api" not in controller:
            self.logger.error("Cannot purge: Network controller not connected")
            return
        api  = controller["network_api"]
        cdev = controller.get("device")
        cname = cdev.name if cdev else f"controller {controller_id}"

        props = cdev.pluginProps if cdev else {}
        try:
            min_age_hours = float(props.get("purgeMinAgeHours", 24) or 24)
        except (TypeError, ValueError):
            min_age_hours = 24.0
        skip_named    = bool(props.get("purgeSkipNamed", True))
        skip_metadata = bool(props.get("purgeSkipMetadata", True))
        skip_indigo   = bool(props.get("purgeSkipIndigoDevice", True))
        dry_run       = bool(props.get("purgeDryRun", False))
        min_age_sec   = min_age_hours * 3600.0

        # Build map of MAC -> Indigo device id for existing client devices,
        # so we know which to skip / which to delete after UniFi forget.
        indigo_macs = {}
        for d in indigo.devices.iter("self"):
            if d.deviceTypeId == "clientDevice":
                mac = (d.pluginProps.get("clientMac", "") or "").lower()
                if mac:
                    indigo_macs[mac] = d.id

        try:
            clients = api.get_all_clients() or []
        except Exception as e:
            self.logger.error(f"{cname}: Purge: failed to fetch client list: {e}")
            return

        # Categorise
        candidates    = []
        skipped_recent  = 0
        skipped_named   = 0
        skipped_meta    = 0
        skipped_indigo  = 0
        not_randomised  = 0
        now_ts = time.time()

        for c in clients:
            mac = (c.get("mac") or "").lower()
            if not mac:
                continue
            if not _mac_is_randomised(mac):
                not_randomised += 1
                continue
            age = _client_age_seconds(c, now_ts=now_ts)
            if age is None or age < min_age_sec:
                skipped_recent += 1
                continue
            if skip_named and _has_user_named_client(c):
                skipped_named += 1
                continue
            if skip_metadata and _has_user_metadata_other_than_name(c):
                skipped_meta += 1
                continue
            if skip_indigo and mac in indigo_macs:
                skipped_indigo += 1
                continue
            candidates.append((mac, c, age))

        action_word = "Would purge" if dry_run else "Purging"
        self.logger.info(
            f"{cname}: {action_word} {len(candidates)} randomised client(s) "
            f"(threshold {min_age_hours:.1f}h). "
            f"Skipped: {skipped_recent} too recent, {skipped_named} named, "
            f"{skipped_meta} metadata, {skipped_indigo} Indigo-tracked. "
            f"Non-randomised: {not_randomised}."
        )

        if not candidates:
            return

        purged_unifi  = 0
        purged_indigo = 0
        for mac, c, age in candidates:
            age_h = age / 3600.0
            display_name = c.get("name") or c.get("hostname") or c.get("oui") or mac
            line = f"  • {mac}  age={age_h:.1f}h  ({display_name})"
            self.logger.info(line)

            if dry_run:
                continue

            # 1. Ask UniFi to forget the client
            try:
                api.forget_client(mac)
                purged_unifi += 1
            except Exception as e:
                self.logger.error(f"    UniFi forget failed for {mac}: {e}")
                # If UniFi forget failed, still attempt Indigo removal — the
                # device is at least stale even if UniFi rejected the forget.

            # 2. Remove the Indigo device if one exists
            indigo_id = indigo_macs.get(mac)
            if indigo_id is not None:
                try:
                    indigo.device.delete(indigo_id)
                    purged_indigo += 1
                except Exception as e:
                    self.logger.error(f"    Indigo device delete failed for {mac}: {e}")

            # 3. Track in the deleted-clients set so future discovery doesn't
            # recreate it
            self.deleted_clients.add(mac)

        if not dry_run:
            try:
                self.pluginPrefs["deletedClients"] = list(self.deleted_clients)
            except Exception:
                pass
            self.logger.info(
                f"{cname}: Purge complete. {purged_unifi} forgotten in UniFi, "
                f"{purged_indigo} Indigo device(s) removed."
            )

    # ------------------------------------------------------------------
    # "Menu Action" variants - these are plugin Actions with no deviceFilter
    # (see Actions.xml, the entries under "MENU ACTIONS"), so they're usable
    # standalone in Action Groups/schedules rather than tied to a specific
    # device. Indigo calls EVERY Actions.xml callback with exactly one
    # argument - the indigo.PluginAction object - regardless of whether a
    # deviceFilter is present. The ConfigUI field values the user picked
    # (like which controller to target) live on action.props, a dict-like
    # object, NOT a separate (valuesDict, typeId) pair - that two-argument
    # convention is for MenuItems.xml (the Plugins-menu dropdown) and for
    # device ConfigUI callbacks (buttons, dynamic lists), which are genuinely
    # different call sites. Mixing the two up is exactly what caused these
    # to fail with "'PluginAction' object has no attribute 'get'" the first
    # time anyone ran one from an Action Group instead of a device dialog.
    # ------------------------------------------------------------------

    def actionTriggerAlarmWebhook(self, action):
        """Fire a Protect Alarm Manager webhook trigger. Official,
        documented endpoint - see trigger_alarm_webhook()'s docstring for
        how this differs from the experimental arm/disarm feature."""
        try:
            controller_id = int(action.props.get("controllerId", 0))
            controller    = self.controllers.get(controller_id)
            if not controller or "protect_api" not in controller:
                self.logger.error("Cannot trigger alarm webhook: Protect not connected")
                return
            # Indigo substitutes %%v:...%%/%%d:...%% in this textfield
            # before the plugin ever sees it - a deleted variable/device
            # reference resolves to an empty string rather than raising
            # here, so that's exactly what this catches: a dangling
            # reference never reaches the request.
            trigger_id = (action.props.get("triggerId", "") or "").strip()
            if not trigger_id:
                self.logger.error("Cannot trigger alarm webhook: no trigger ID "
                                  "(empty, or a variable/device reference that no longer resolves)")
                return
            controller["protect_api"].trigger_alarm_webhook(trigger_id)
            self.logger.info(f"Alarm Manager webhook triggered: {trigger_id}")
        except Exception as e:
            self.logger.error(f"triggerAlarmWebhook error: {e}")
            if self.debug:
                self.logger.error(traceback.format_exc())

    def menuDiscoverCameras(self, action):
        try:
            cid = int(action.props.get("controllerId", 0))
            self.createCameraDevices(action.props, "protectCamera", cid)
        except Exception as e:
            self.logger.error(f"menuDiscoverCameras error: {e}")
            if self.debug:
                self.logger.error(traceback.format_exc())

    def menuDiscoverNetworkDevices(self, action):
        try:
            cid = int(action.props.get("controllerId", 0))
            self.createNetworkDevices(action.props, "networkDevice", cid)
        except Exception as e:
            self.logger.error(f"menuDiscoverNetworkDevices error: {e}")
            if self.debug:
                self.logger.error(traceback.format_exc())

    def menuDiscoverFirewallPolicies(self, action):
        try:
            cid = int(action.props.get("controllerId", 0))
            self.createFirewallPolicyDevices(action.props, "firewallPolicy", cid)
        except Exception as e:
            self.logger.error(f"menuDiscoverFirewallPolicies error: {e}")
            if self.debug:
                self.logger.error(traceback.format_exc())

    def menuDiscoverTrafficRoutes(self, action):
        try:
            cid = int(action.props.get("controllerId", 0))
            self.createTrafficRouteDevices(action.props, "trafficRoute", cid)
        except Exception as e:
            self.logger.error(f"menuDiscoverTrafficRoutes error: {e}")
            if self.debug:
                self.logger.error(traceback.format_exc())

    def menuDiscoverPortForwards(self, action):
        try:
            cid = int(action.props.get("controllerId", 0))
            self.createPortForwardDevices(action.props, "portForward", cid)
        except Exception as e:
            self.logger.error(f"menuDiscoverPortForwards error: {e}")
            if self.debug:
                self.logger.error(traceback.format_exc())

    def menuDiscoverWLANs(self, action):
        try:
            cid = int(action.props.get("controllerId", 0))
            self.createWLANDevices(action.props, "wlanDevice", cid)
        except Exception as e:
            self.logger.error(f"menuDiscoverWLANs error: {e}")
            if self.debug:
                self.logger.error(traceback.format_exc())

    def menuDiscoverAlarmProfiles(self, action):
        try:
            cid = int(action.props.get("controllerId", 0))
            self.createAlarmProfileDevices(action.props, "alarmProfile", cid)
        except Exception as e:
            self.logger.error(f"menuDiscoverAlarmProfiles error: {e}")
            if self.debug:
                self.logger.error(traceback.format_exc())

    def menuDiscoverAllClients(self, action):
        try:
            cid = int(action.props.get("controllerId", 0))
            try:
                min_days = int(action.props.get("minLastSeen", 30))
            except (TypeError, ValueError):
                min_days = 30
            self._discover_clients(cid, include_known=True, min_days=min_days)
        except Exception as e:
            self.logger.error(f"menuDiscoverAllClients error: {e}")
            if self.debug:
                self.logger.error(traceback.format_exc())

    def menuDiscoverUnknownClients(self, action):
        try:
            cid = int(action.props.get("controllerId", 0))
            self._discover_clients(cid, include_known=False, min_days=0)
        except Exception as e:
            self.logger.error(f"menuDiscoverUnknownClients error: {e}")
            if self.debug:
                self.logger.error(traceback.format_exc())

    def menuRemoveMissingClients(self, action):
        try:
            cid = int(action.props.get("controllerId", 0))
            self._remove_missing_clients(cid)
        except Exception as e:
            self.logger.error(f"menuRemoveMissingClients error: {e}")
            if self.debug:
                self.logger.error(traceback.format_exc())

    # ----- Client discovery internals -----

    def _discover_clients(self, controller_id, include_known=True, min_days=30):
        """Create client devices, optionally filtered by last-seen window."""
        controller = self.controllers.get(controller_id)
        if not controller or "network_api" not in controller:
            self.logger.error("Cannot discover clients: Network not connected")
            return
        api = controller["network_api"]

        cutoff_ts = 0
        if min_days > 0:
            cutoff_ts = int(time.time()) - (min_days * 86400)

        try:
            active = api.get_clients()
        except Exception as e:
            self.logger.warning(f"Could not list active clients: {e}")
            active = []

        if include_known:
            try:
                known = api.get_all_clients()
            except Exception as e:
                self.logger.warning(f"Could not list known clients: {e}")
                known = []
        else:
            known = []

        # Merge by MAC, preferring active records (have more data)
        merged = {}
        for c in known:
            mac = (c.get("mac") or "").lower()
            if mac:
                merged[mac] = c
        for c in active:
            mac = (c.get("mac") or "").lower()
            if mac:
                merged[mac] = c

        # Existing devices, by MAC
        existing_macs = set()
        for d in indigo.devices.iter("self.clientDevice"):
            m = d.pluginProps.get("clientMac", "").lower()
            if m:
                existing_macs.add(m)

        created = 0
        skipped = 0
        for mac, c in merged.items():
            if mac in existing_macs:
                continue
            if mac in self.deleted_clients:
                skipped += 1
                continue
            last_seen = int(c.get("last_seen", 0) or 0)
            if cutoff_ts and last_seen and last_seen < cutoff_ts:
                continue
            if not include_known and last_seen and (time.time() - last_seen) < 0:
                # only unknown filter is just a presence filter; nothing more
                pass
            new_dev = self._create_client_device(controller_id, c)
            if new_dev:
                created += 1
        self.logger.info(f"Client discovery: {created} created, {skipped} skipped (previously deleted)")

    def _maybe_auto_create_client(self, controller_id, client_data, mac):
        """Called on every sta:connect for a MAC with no existing device.
        Only actually creates one if the controller's own "Auto-create on
        connect" checkbox is enabled - otherwise a no-op, preserving the
        plugin's previous behaviour for anyone who hasn't opted in."""
        controller = self.controllers.get(controller_id)
        if not controller:
            return
        ctrl_dev = controller.get("device")
        if not ctrl_dev or not ctrl_dev.pluginProps.get("autoCreateClients", False):
            return
        if mac in self.deleted_clients:
            return
        if ctrl_dev.pluginProps.get("autoCreateSkipRandomized", True) and _mac_is_randomised(mac):
            return
        new_dev = self._create_client_device(controller_id, client_data)
        if new_dev:
            self.logger.info(f"Auto-created client device: {new_dev.name}")
            # sta:connect's own payload can be partial - refresh from a
            # full fetch immediately so the new device shows complete
            # details (IP, connected AP/switch, etc.) right away rather
            # than waiting for the next periodic refresh.
            self._update_client_states(controller_id, mac)

    def _create_client_device(self, controller_id, client_data):
        """Create a clientDevice from a client data dict (same shape
        whether it came from a REST listing or a sta:connect WS message).
        Shared by bulk discovery (_discover_clients) and auto-create-on-
        connect (_on_network_ws), so both populate exactly the same
        props/states and go through the same existing-device dedup
        (_create_or_skip). Returns the new device, or None if one already
        existed."""
        mac  = (client_data.get("mac") or "").lower()
        name = client_data.get("name") or client_data.get("hostname") or client_data.get("oui") or mac
        props = {
            "controllerId": str(controller_id),
            "clientMac":    mac,
            "clientName":   name,
        }
        new_dev = self._create_or_skip(f"{name} (UniFi Client)", "clientDevice", props)
        if new_dev:
            controller = self.controllers.get(controller_id)
            if controller is not None and mac:
                controller.setdefault("clients", {})[mac] = new_dev.id
        return new_dev

    def _remove_missing_clients(self, controller_id):
        """Remove client devices whose MAC is not in the controller's known set."""
        controller = self.controllers.get(controller_id)
        if not controller or "network_api" not in controller:
            self.logger.error("Cannot remove missing clients: Network not connected")
            return
        api = controller["network_api"]
        try:
            known_macs = {(c.get("mac") or "").lower()
                          for c in api.get_all_clients() + api.get_clients()}
        except Exception as e:
            self.logger.error(f"Could not list known clients: {e}")
            return

        removed = 0
        for d in list(indigo.devices.iter("self.clientDevice")):
            try:
                cid_dev = int(d.pluginProps.get("controllerId", 0))
            except (TypeError, ValueError):
                continue
            if cid_dev != controller_id:
                continue
            mac = d.pluginProps.get("clientMac", "").lower()
            if mac and mac not in known_macs:
                self.logger.info(f"Removing missing client device: {d.name}")
                self.deleted_clients.add(mac)
                indigo.device.delete(d.id)
                removed += 1
        # Persist deleted list so we don't auto-recreate
        try:
            self.pluginPrefs["deletedClients"] = list(self.deleted_clients)
        except Exception:
            pass
        self.logger.info(f"Client cleanup: {removed} removed")

    # ------------------------------------------------------------------
    # Track client deletion to avoid auto-recreation
    # ------------------------------------------------------------------

    def deviceDeleted(self, dev):
        try:
            if dev.deviceTypeId == "clientDevice":
                mac = dev.pluginProps.get("clientMac", "").lower()
                if mac:
                    self.deleted_clients.add(mac)
                    try:
                        self.pluginPrefs["deletedClients"] = list(self.deleted_clients)
                    except Exception:
                        pass
        except Exception:
            pass
        super().deviceDeleted(dev)
