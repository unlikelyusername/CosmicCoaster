# lib/wifi.py
#
# WiFi connect + NTP clock set, driven from config.json.
#
# Tries a priority-ordered list of networks: scan first, connect to the
# highest-priority network that is actually visible. If none are visible
# (hidden SSIDs, iOS personal hotspots — which frequently do not appear in a
# scan) fall back to a direct connect against the first entry.
#
# Ported from archive/wifi_manager.py, which already solved this; the display
# animation has been dropped, since that took the old board object.
#
#   import wifi
#   wifi.connect_and_sync(cfg)   # returns True if the clock was set

import time
import network

_SCAN_RETRIES    = 3
_CONNECT_TIMEOUT = 15    # seconds, per network

_NTP_SERVERS = [
    "pool.ntp.org",
    "time.cloudflare.com",
    "time.google.com",
]


def networks_from_config(cfg):
    """
    Extract a priority-ordered [(ssid, password), ...] from config.

    Accepts the "networks" list, and falls back to the legacy single
    ssid/password keys so an un-migrated config still boots.
    """
    out = []

    for entry in cfg.get("networks") or ():
        if isinstance(entry, dict):
            ssid = entry.get("ssid")
            pw   = entry.get("password", "")
        elif isinstance(entry, (list, tuple)) and len(entry) >= 2:
            ssid, pw = entry[0], entry[1]
        else:
            continue
        if ssid:
            out.append((ssid, pw or ""))

    if not out:
        ssid = cfg.get("ssid")
        if ssid:
            out.append((ssid, cfg.get("password", "") or ""))

    return out


def _sta_connect(ssid, password, timeout=_CONNECT_TIMEOUT):
    """Connect to one specific network. True on success."""
    sta = network.WLAN(network.STA_IF)
    sta.active(True)
    if sta.isconnected():
        sta.disconnect()
        time.sleep(0.5)

    sta.connect(ssid, password)
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = sta.status()
        if status == network.STAT_GOT_IP:
            return True
        if status in (network.STAT_CONNECT_FAIL,
                      network.STAT_NO_AP_FOUND,
                      network.STAT_WRONG_PASSWORD):
            return False
        time.sleep(0.3)
    return False


def _scan_and_match(networks, retries=_SCAN_RETRIES):
    """
    Scan for visible APs and return (ssid, password) for the highest-priority
    match. Falls back to the first configured network when nothing matches —
    hidden and hotspot SSIDs often never show up in a scan.
    """
    sta = network.WLAN(network.STA_IF)
    sta.active(True)

    visible = set()
    for attempt in range(retries):
        try:
            for ap in sta.scan():          # blocks ~2-3s
                ssid = ap[0]
                if isinstance(ssid, bytes):
                    ssid = ssid.decode("utf-8", "ignore")
                if ssid:
                    visible.add(ssid)
        except Exception as e:
            print("[wifi] scan error (attempt {}): {}".format(attempt + 1, e))

        for ssid, password in networks:
            if ssid in visible:
                print("[wifi] matched visible network:", ssid)
                return ssid, password

        if attempt < retries - 1:
            time.sleep(1)

    print("[wifi] no configured network visible after {} scans; saw {}".format(
        retries, sorted(visible) or "nothing"))

    if networks:
        print("[wifi] trying direct connect to", networks[0][0])
        return networks[0]

    return None, None


def connect(cfg):
    """Bring WiFi up using the configured network list. True if connected."""
    networks = networks_from_config(cfg)
    if not networks:
        print("[wifi] no networks configured")
        return False

    ssid, password = _scan_and_match(networks)
    if ssid is None:
        return False

    timeout = cfg.get("timeout", _CONNECT_TIMEOUT)
    if _sta_connect(ssid, password, timeout):
        print("[wifi] connected:", ssid)
        return True

    # The scan-matched network failed — try every other configured network
    # before giving up, in priority order.
    for other_ssid, other_pw in networks:
        if other_ssid == ssid:
            continue
        print("[wifi] retrying with", other_ssid)
        if _sta_connect(other_ssid, other_pw, timeout):
            print("[wifi] connected:", other_ssid)
            return True

    print("[wifi] all networks failed")
    return False


def _apply_utc_offset(offset_hours):
    import machine
    rtc = machine.RTC()
    t = rtc.datetime()
    epoch = time.mktime((t[0], t[1], t[2], t[4], t[5], t[6], 0, 0))
    epoch += int(offset_hours * 3600)
    lt = time.localtime(epoch)
    rtc.datetime((lt[0], lt[1], lt[2], lt[6], lt[3], lt[4], lt[5], 0))
    return lt


def sync_ntp(utc_offset=0, host=None):
    """Set the RTC from NTP, then apply the UTC offset. True on success."""
    import ntptime

    servers = list(_NTP_SERVERS)
    if host and host not in servers:
        servers.insert(0, host)

    for server in servers:
        ntptime.host = server
        ntptime.timeout = 5
        for attempt in range(2):
            try:
                ntptime.settime()
                lt = _apply_utc_offset(utc_offset)
                print("[wifi] NTP OK via {} — {:04d}-{:02d}-{:02d} "
                      "{:02d}:{:02d}:{:02d}".format(
                          server, lt[0], lt[1], lt[2], lt[3], lt[4], lt[5]))
                return True
            except Exception as e:
                print("[wifi] NTP {} attempt {}/2: {}".format(
                    server, attempt + 1, e))
                time.sleep(1)

    print("[wifi] NTP failed — clock will drift")
    return False


def deinit():
    """Fully release the radio."""
    try:
        sta = network.WLAN(network.STA_IF)
        if sta.isconnected():
            sta.disconnect()
        sta.active(False)
    except Exception as e:
        print("[wifi] deinit:", e)


def connect_and_sync(cfg):
    """Connect, set the clock, release the radio. True if the clock was set."""
    if not connect(cfg):
        return False
    ok = sync_ntp(cfg.get("utc_offset", 0), cfg.get("ntp_host"))
    deinit()
    return ok
