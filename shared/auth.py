"""Shared-secret auth for Assist.

Every endpoint here can start a process in a live tmux pane — /api/commands/run
takes an arbitrary command string by design, including saved commands that a
user or an agent may have written into `.assist-commands.json`. That makes the
trust boundary, not input validation, the thing that has to hold: anyone who
can reach the port can run anything.

A single shared secret gates the whole app. It is generated on first start and
written to `auth_token` in the install's config dir (0600; beside the code on
the legacy layout, gitignored), so a fresh install
needs no configuration step — the operator reads it out of the file or the
startup log once and logs in from the phone.

The browser never stores the raw token. Each signed-in browser is a device in
shared/devices.py and holds a cookie bound to that device: `v2.<id>.<mac>`,
where the mac is an HMAC of the id keyed by the token. Revoking one device
stops its cookie without touching any other; rotating the token still stops
them all. A token presented directly — in the `X-Assist-Token` header, for curl
and the CLI — is compared to the real value. Both comparisons are
constant-time. The header is the only accepted carrier for the raw secret: see
request_authenticated() for why a query string is not.

A second way in exists for onboarding: a temporary open-access window. It is
not an auth bypass — it admits one client from a configured LAN range to `GET
/` and mints it a device, then closes. Four limits apply at once: endpoint,
network, single use, and a monotonic deadline.

Lock order: shared.devices takes its own lock and then, inside it, at most one
of `_window_lock` / `_requests_lock`. Nothing here may reach for the devices
lock while holding either of those (tests/test_devices.py guards it).
"""

import hmac
import ipaddress
import math
import os
import secrets
import sys
import threading
import time
from hashlib import sha256
from pathlib import Path

COOKIE_NAME = "assist_auth"
HEADER_NAME = "X-Assist-Token"

# Ten years: this is a single-user tool on a phone that must not be asked to
# re-authenticate mid-session. Revocation is per device (shared/devices.py);
# deleting `auth_token` and restarting remains the way to stop every cookie at
# once, because the HMAC key changed.
COOKIE_MAX_AGE = 10 * 365 * 24 * 3600

# Resolved at first use, not import: where the token lives depends on the
# install's layout (shared/paths.py), which an entry path checks first. Tests
# set this directly.
_TOKEN_PATH = None
_token_cache = None


def token_path():
    """auth_token: ASSIST_AUTH_TOKEN_PATH, else the install's config dir."""
    global _TOKEN_PATH
    if _TOKEN_PATH is None:
        from shared import paths

        _TOKEN_PATH = paths.token_path().resolve()
    return _TOKEN_PATH

# Open-access window. `_window_deadline` is a time.monotonic() value so a clock
# correction cannot extend it; None means closed. `_last_onboard` holds one
# pending report for the UI and is cleared by the first /poll that carries it.
_window_lock = threading.Lock()
_window_deadline = None
# Set while shared.devices writes the record for the admission this window is
# about to grant; a second navigation finds the window taken. `_window_source`
# is what the device record says it came through ("pair" when `assist pair`
# opened it).
_window_reservation = None
_window_source = "window"
_last_onboard = None
_last_onboard_at = 0.0  # monotonic, for the TTL below
_onboard_seq = 0

# The onboard report is the only warning the operator gets that someone walked
# in, so it is published to EVERY polling client for this long and de-duplicated
# client-side by its id — never handed to whichever poll happened to arrive
# first and then destroyed. A poll that dies in flight must not be able to eat a
# security notice. It still expires, so it cannot surface an hour later out of
# context.
_ONBOARD_TTL_SEC = 300


def get_token():
    """Return the shared secret, generating and persisting it on first call."""
    global _token_cache
    if _token_cache:
        return _token_cache

    path = token_path()
    if path.exists():
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            _token_cache = existing
            return _token_cache

    token = secrets.token_urlsafe(32)
    # Mode at CREATE time: a plain open() would leave the secret world-readable
    # for the length of the write (same reasoning as state.atomic_write_json).
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(token + "\n")
    _token_cache = token
    return _token_cache


def print_startup_token_notice(output=None):
    """Print the token only to an interactive terminal; otherwise print its path."""
    destination = output if output is not None else sys.stdout
    token = get_token()
    if bool(getattr(destination, "isatty", lambda: False)()):
        message = f"[assist] auth token: {token}  (file: {token_path()})"
    else:
        message = f"[assist] auth token file: {token_path()}"
    print(message, file=destination, flush=True)


COOKIE_VERSION = "v2"


def device_cookie_mac(device_id):
    """The per-device mac: an HMAC of the id, keyed by the token."""
    msg = ("assist-auth-v2:" + device_id).encode()
    return hmac.new(get_token().encode(), msg, sha256).hexdigest()


def device_cookie(device_id):
    """The value a signed-in browser holds — a derivative, never the secret."""
    return f"{COOKIE_VERSION}.{device_id}.{device_cookie_mac(device_id)}"


def parse_device_cookie(value):
    """(device_id, mac) for a well-formed v2 cookie whose mac verifies, else None.

    The v1 value (one HMAC shared by every browser) is not accepted: it could
    not tell two browsers apart, so it could never be revoked one at a time.
    """
    parts = (value or "").split(".")
    if len(parts) != 3 or parts[0] != COOKIE_VERSION or not parts[1]:
        return None
    device_id, mac = parts[1], parts[2]
    if not hmac.compare_digest(mac, device_cookie_mac(device_id)):
        return None
    return device_id, mac


def header_authenticated(request):
    """True if the raw token arrived in X-Assist-Token."""
    presented = request.headers.get(HEADER_NAME)
    return bool(presented) and hmac.compare_digest(presented, get_token())


def request_device(request):
    """The device record behind this request's cookie, or None."""
    from shared import devices

    cookie = request.cookies.get(COOKIE_NAME)
    if not cookie or parse_device_cookie(cookie) is None:
        return None
    return devices.authenticate(cookie, ip=client_ip(request))


def request_authenticated(request):
    """True if this request carries a valid device cookie or the token itself.

    Header only for the raw token — never a `?token=` query parameter. A URL is
    the one place a credential gets copied somewhere Assist does not control:
    nginx writes the full request line to its access log, the browser keeps it
    in history and in any bookmark or shared link, and it goes out in `Referer`
    on the next outbound request. A header does none of that.

    It was also actively misleading for the one job people reached for it:
    `?token=` authenticates the HTML document and nothing it then loads, so a
    page fetched that way renders unstyled while every stylesheet 401s. Use
    `X-Assist-Token` on the request (or on the browser context, for Playwright).
    """
    if request_device(request) is not None:
        return True
    return header_authenticated(request)


def token_matches(candidate):
    """Constant-time check of a token typed into the login form."""
    return bool(candidate) and hmac.compare_digest(candidate.strip(), get_token())


def set_auth_cookie(response, device_id):
    """Give this response the cookie for `device_id`. One definition, three issuers."""
    response.set_cookie(
        COOKIE_NAME,
        device_cookie(device_id),
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        samesite="Lax",
    )
    return response


# ---------------------------------------------------------------------------
# Temporary open-access window
# ---------------------------------------------------------------------------


def client_ip(request):
    """The real peer address.

    X-Real-IP is believed only when the TCP peer is loopback, i.e. a reverse
    proxy on this host wrote it. nginx sets it with $remote_addr — an
    unconditional overwrite, so there it is the actual peer. From any other
    peer (Flask bound to 0.0.0.0, a port-forward) the header is whatever the
    client typed, and trusting it let any reachable host pose as an in-scope
    LAN address for approval requests and the open window.

    X-Forwarded-For is never read: nginx's $proxy_add_x_forwarded_for APPENDS
    to what the client sent, so its leftmost entry is attacker-controlled. With
    no header from loopback, remote_addr (127.0.0.1) is correct and out of
    scope — a host-local process can read `auth_token` anyway.
    """
    peer = (request.remote_addr or "").strip()
    if _is_loopback(peer):
        forwarded = (request.headers.get("X-Real-IP") or "").strip()
        if forwarded:
            return forwarded
    return peer


def _is_loopback(address):
    try:
        return ipaddress.ip_address(address).is_loopback
    except ValueError:
        return False


def _networks_raw():
    from shared import state

    configured = (state.get_settings().get("access") or {}).get("open_networks")
    if configured is None:
        return state.DEFAULT_SETTINGS["access"]["open_networks"]
    return str(configured)


def open_networks():
    """Configured CIDRs, unparseable entries dropped. May be empty."""
    nets = []
    for chunk in _networks_raw().split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            nets.append(ipaddress.ip_network(chunk, strict=False))
        except ValueError:
            continue
    return nets


def ip_in_scope(ip):
    """True if `ip` falls in a configured network. Empty config -> always False."""
    nets = open_networks()
    if not nets:
        return False
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in net for net in nets)


def is_onboarding_navigation(request):
    """True only for a real top-level page load of the app shell.

    Two ways in that `request.endpoint` alone does not close:

    Flask maps HEAD and OPTIONS onto the same endpoint as GET, and OPTIONS is
    additionally skipped by the Origin guard — either would spend the window
    and collect the cookie without ever rendering the app.

    Sec-Fetch-Dest is stamped by the browser and a page cannot forge it. An
    `<img src="http://assist.example.lan/">` on any LAN page sends `image`, a fetch
    sends `empty`, and sw.js precaching `/` sends `empty`; only an address-bar
    navigation sends `document`. Without this check a hostile or merely
    unlucky background request burns the operator's window and walks away with
    a permanent cookie for its own browser. Absent means a non-browser client
    (curl, the verification matrix), which was never the exposure.
    """
    if request.method != "GET":
        return False
    dest = request.headers.get("Sec-Fetch-Dest")
    return dest is None or dest == "document"


def open_window(seconds, source="window"):
    """Start (or restart) the window. Returns the new state."""
    global _window_deadline, _window_source
    with _window_lock:
        _window_deadline = time.monotonic() + seconds
        _window_source = "pair" if source == "pair" else "window"
    print(
        f"[assist] access window open for {int(seconds)}s "
        f"(networks: {_networks_raw()})",
        flush=True,
    )
    return window_state()


def close_window():
    """Shut the window early. Idempotent. Returns the new state."""
    global _window_deadline
    with _window_lock:
        was_open = _window_deadline is not None
        _window_deadline = None
    if was_open:
        print("[assist] access window closed", flush=True)
    return window_state()


def window_admissible(ip):
    """True if the window is open, unreserved, and `ip` is in scope.

    A lookup only: shared.devices calls it before its capacity check, then
    reserve_window() — both under the devices lock, so no other admission can
    slip between them.
    """
    if not ip_in_scope(ip):
        return False
    with _window_lock:
        return (
            _window_deadline is not None
            and time.monotonic() < _window_deadline
            and _window_reservation is None
        )


def reserve_window(ip):
    """Hold the window for one admission. Returns (token, source) or None.

    The check-and-reserve is a single critical section, so two devices racing
    `GET /` cannot both be admitted. The scope test sits outside the lock
    because it reads settings, not window state.
    """
    global _window_reservation
    if not ip_in_scope(ip):
        return None
    with _window_lock:
        if _window_deadline is None or time.monotonic() >= _window_deadline:
            return None
        if _window_reservation is not None:
            return None
        _window_reservation = object()
        return _window_reservation, _window_source


def release_window_reservation(token):
    """The device record could not be written: the window is as it was."""
    global _window_reservation
    with _window_lock:
        if _window_reservation is token:
            _window_reservation = None


def consume_reserved_window(token, ip, user_agent=""):
    """Spend the window on the admission that reserved it. True if it was ours."""
    global _window_deadline, _window_reservation
    global _last_onboard, _last_onboard_at, _onboard_seq
    with _window_lock:
        if _window_reservation is not token:
            return False
        _window_reservation = None
        _window_deadline = None
        _onboard_seq += 1
        _last_onboard = {
            "id": _onboard_seq,
            "ip": ip,
            "ua": user_agent,
            "at": time.time(),
        }
        _last_onboard_at = time.monotonic()
    print(f"[assist] access window consumed by {ip} ({user_agent})", flush=True)
    return True


def window_state():
    """Snapshot for /poll and the access endpoints.

    The onboard report is published to every caller until it expires, carrying
    a stable `id` so each browser toasts it exactly once. Reads never mutate
    it: at-least-once delivery plus client-side de-duplication, because the
    alternative — hand it to the first poll and clear it — loses the notice
    entirely if that poll's connection dies, or shows it on whichever tab
    happened to ask first rather than the operator's.
    """
    global _last_onboard
    with _window_lock:
        deadline = _window_deadline
        onboard = _last_onboard
        if onboard is not None and time.monotonic() - _last_onboard_at > _ONBOARD_TTL_SEC:
            _last_onboard = onboard = None
    is_open = deadline is not None and time.monotonic() < deadline
    # pending_requests() is called outside the `with _window_lock` block above
    # on purpose: it takes _requests_lock, and the two must never be held at
    # once or different callers could acquire them in opposite orders.
    return {
        "open": is_open,
        "remaining_sec": math.ceil(deadline - time.monotonic()) if is_open else 0,
        "networks": _networks_raw(),
        "last_onboard": onboard,
        "pending_requests": pending_requests(),
    }


# ---------------------------------------------------------------------------
# Device approval requests
# ---------------------------------------------------------------------------
#
# The window above is the operator pushing: they open a slot and whichever
# in-scope device gets there first takes it. This is the pull direction — a
# device asks by name and a logged-in session answers. The admission is
# targeted rather than broadcast: it is bound to a `claim` secret that only the
# requesting browser ever sees, so an approval meant for one device cannot be
# intercepted by another racing to load `/`.

_requests_lock = threading.Lock()
_requests = {}              # id -> record
_request_seq = 0
_request_last_create = {}   # ip -> monotonic of its last create, for the cooldown

# A decided-and-denied record lingers this long so the requester's next poll
# reports "denied" rather than the misleading "expired". A UI timing detail,
# not an operator knob — hence a constant rather than a setting.
_REQUEST_DENIED_GRACE = 20

# A redeemed approval stays re-pollable this long, returning the SAME device's
# cookie: the response that carried it can be lost, and the claim is a secret
# only the requesting browser holds. Measured from redemption, not from the
# request's deadline, so an approval just before expiry keeps the full window.
CLAIM_REPLAY_SEC = 60


def _request_cfg():
    """(ttl_seconds, max_pending, cooldown_seconds) from settings.

    Read at use time, the way _networks_raw() reads open_networks, so editing
    settings.json takes effect without a restart. Each value is clamped: a
    hand-edited zero or a string must not disable the limit it configures.
    """
    from shared import state

    cfg = state.get_settings().get("access") or {}
    defaults = state.DEFAULT_SETTINGS["access"]

    def _num(key, low, high):
        try:
            value = float(cfg.get(key, defaults[key]))
        except (TypeError, ValueError):
            value = float(defaults[key])
        if not math.isfinite(value):
            value = float(defaults[key])
        return max(low, min(value, high))

    return (
        _num("request_ttl_minutes", 1, 60) * 60,
        int(_num("request_max_pending", 1, 10)),
        _num("request_cooldown_sec", 0, 3600),
    )


def _prune_requests(now):
    """Drop expired and finished records. Caller holds _requests_lock.

    A reserved record (its device is being written) is never pruned, and a
    redeemed one lives until its replay window closes, whatever the request
    deadline says. Pruning drops the claim association, never the device.
    """
    for rid, rec in list(_requests.items()):
        if rec.get("reserved"):
            continue
        if rec.get("redeemed_by"):
            if now >= rec["redeemed_at"] + CLAIM_REPLAY_SEC:
                del _requests[rid]
            continue
        if now >= rec["deadline"]:
            del _requests[rid]
        elif (
            rec["status"] == "denied"
            and now - rec["decided_at"] > _REQUEST_DENIED_GRACE
        ):
            del _requests[rid]


def _fresh_code(taken):
    """A 4-digit code not already on screen beside another pending request.

    The operator's whole job is matching digits, so two simultaneous requests
    showing the same code would defeat the feature. Bounded retry, then accept
    a duplicate rather than spin.
    """
    for _ in range(20):
        code = f"{secrets.randbelow(10000):04d}"
        if code not in taken:
            return code
    return f"{secrets.randbelow(10000):04d}"


def create_request(ip, ua):
    """Register a device asking to be let in.

    The `claim` in the success payload is the only copy that leaves here: it
    binds the eventual cookie to the browser that asked, and is never published
    to /poll or any other operator-facing response.
    """
    global _request_seq
    if not ip_in_scope(ip):
        return {"ok": False, "error": "out_of_scope"}

    ttl, max_pending, cooldown = _request_cfg()
    now = time.monotonic()
    with _requests_lock:
        _prune_requests(now)
        last = _request_last_create.get(ip)
        if last is not None and now - last < cooldown:
            return {"ok": False, "error": "cooldown"}
        pending = [r for r in _requests.values() if r["status"] == "pending"]
        if len(pending) >= max_pending:
            return {"ok": False, "error": "too_many"}

        _request_seq += 1
        rid = _request_seq
        code = _fresh_code({r["code"] for r in pending})
        claim = secrets.token_urlsafe(32)
        _requests[rid] = {
            "id": rid,
            "code": code,
            "claim": claim,
            "ip": ip,
            "ua": ua,
            "status": "pending",
            "deadline": now + ttl,
            "decided_at": 0.0,
        }
        _request_last_create[ip] = now
        # The cooldown map is keyed by IP and would otherwise grow for the life
        # of the process. Sweep entries whose cooldown has already lapsed.
        if len(_request_last_create) > 256:
            for old_ip, at in list(_request_last_create.items()):
                if now - at >= cooldown:
                    del _request_last_create[old_ip]

    print(f"[assist] device approval requested by {ip} ({ua}) code {code}", flush=True)
    return {
        "ok": True,
        "request": {"id": rid, "code": code, "expires_in": int(ttl)},
        "claim": claim,
    }


def decide_request(rid, approve):
    """Record the operator's verdict. True if a pending record moved.

    Approve and deny racing from two logged-in sessions is resolved here: the
    first call under the lock wins and the second sees a non-pending record.
    """
    now = time.monotonic()
    with _requests_lock:
        _prune_requests(now)
        rec = _requests.get(rid)
        if rec is None or rec["status"] != "pending":
            return False
        rec["status"] = "approved" if approve else "denied"
        rec["decided_at"] = now
        ip = rec["ip"]
    verdict = "approved" if approve else "denied"
    print(f"[assist] device approval {verdict} for {ip}", flush=True)
    return True


def lookup_claim(claim, ip):
    """What a waiting device's claim stands for, without changing anything.

    None is also the answer to an unknown, replayed-out or IP-mismatched
    claim: a caller that cannot prove it made the request learns nothing about
    whether one exists. Otherwise a snapshot: id, status, remaining_sec, and
    for a redeemed claim `redeemed_by` (the device id).
    """
    if not claim:
        return None
    now = time.monotonic()
    with _requests_lock:
        _prune_requests(now)
        for rec in _requests.values():
            if rec["ip"] == ip and hmac.compare_digest(rec["claim"], claim):
                return {
                    "id": rec["id"],
                    "status": rec["status"],
                    "remaining_sec": max(0, math.ceil(rec["deadline"] - now)),
                    "redeemed_by": rec.get("redeemed_by"),
                    "reserved": bool(rec.get("reserved")),
                }
    return None


def reserve_claim(rid):
    """Hold an approved, unredeemed claim for one admission. True if held."""
    with _requests_lock:
        rec = _requests.get(rid)
        if (
            rec is None
            or rec["status"] != "approved"
            or rec.get("redeemed_by")
            or rec.get("reserved")
        ):
            return False
        rec["reserved"] = True
        return True


def release_claim(rid):
    """The device record could not be written: the approval stands, unspent."""
    with _requests_lock:
        rec = _requests.get(rid)
        if rec is not None:
            rec["reserved"] = False


def redeem_claim(rid, device_id):
    """Spend a reserved claim on `device_id`; it stays re-pollable for a minute."""
    with _requests_lock:
        rec = _requests.get(rid)
        if rec is None or not rec.get("reserved"):
            return False
        rec["reserved"] = False
        rec["redeemed_by"] = device_id
        rec["redeemed_at"] = time.monotonic()
        return True


def pending_requests():
    """Operator-facing view. Never carries `claim`.

    Filtering to `pending` is what clears the approval sheet the moment a
    verdict lands, without needing a second signal.
    """
    now = time.monotonic()
    out = []
    with _requests_lock:
        _prune_requests(now)
        for rec in sorted(_requests.values(), key=lambda r: r["id"]):
            if rec["status"] != "pending":
                continue
            out.append(
                {
                    "id": rec["id"],
                    "code": rec["code"],
                    "ip": rec["ip"],
                    "ua": rec["ua"],
                    "remaining_sec": max(0, math.ceil(rec["deadline"] - now)),
                }
            )
    return out
