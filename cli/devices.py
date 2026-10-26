"""`assist devices` — list, rename, revoke and reset signed-in browsers.

Runs on the header token, so it keeps working when every browser is revoked
and when the registry is quarantined (`reset` is the way out of that).
"""

import argparse
import json
import sys
import time

from cli import http

USAGE = """usage: assist devices [list]
       assist devices rename ID NAME
       assist devices revoke ID
       assist devices reset

Every browser that signs in is a device with its own cookie. Revoking one
signs out that browser only; scripts on the token are not devices.
`reset` empties the list: every browser signs in again (and it is the only
way out of a quarantined registry).
"""


def _ago(ts):
    if not ts:
        return "—"
    sec = max(0, int(time.time() - ts))
    if sec < 90:
        return "now"
    if sec < 3600:
        return f"{sec // 60} min ago"
    if sec < 86400:
        return f"{sec // 3600} h ago"
    if sec < 7 * 86400:
        return f"{sec // 86400} d ago"
    return time.strftime("%b %d", time.localtime(ts))


def _list() -> int:
    data = http.get("/access/devices")
    if data.get("recovery_required"):
        print(data.get("message") or "registry quarantined; run assist devices reset")
        return 0
    rows = data.get("devices") or []
    if not rows:
        print("No browsers signed in. (Scripts on the token are not devices.)")
        return 0
    print(f"{'ID':<36}  {'NAME':<24}  {'SOURCE':<8}  {'LAST SEEN':<10}  {'IP':<15}  STATE")
    for d in rows:
        state = "revoked " + time.strftime("%b %d", time.localtime(d["revoked_at"])) \
            if d.get("revoked_at") else "active"
        print(
            f"{d['id']:<36}  {(d.get('name') or '—')[:24]:<24}  {d.get('source') or '':<8}  "
            f"{_ago(d.get('last_seen')):<10}  {(d.get('ip') or '—'):<15}  {state}"
        )
    active = sum(1 for d in rows if not d.get("revoked_at"))
    print(f"{active} active of {data.get('max_active', 32)} allowed")
    return 0


def _error_code(exc: http.ApiError) -> str:
    try:
        return json.loads(exc.body).get("error") or str(exc.status)
    except (ValueError, AttributeError):
        return str(exc.status)


def command(argv: list[str]) -> int:
    if argv and argv[0] in ("-h", "--help", "help"):
        print(USAGE, end="")
        return 0
    action = argv[0] if argv else "list"
    rest = argv[1:]
    parser = argparse.ArgumentParser(prog=f"assist devices {action}", add_help=True)
    if action == "rename":
        parser.add_argument("id", help="device id from `assist devices`")
        parser.add_argument("name", help="a label, 1 to 40 characters")
    elif action == "revoke":
        parser.add_argument("id", help="device id from `assist devices`")
    elif action not in ("list", "reset"):
        print(f"assist devices: unknown action {action!r}\n\n{USAGE}", file=sys.stderr, end="")
        return 2
    try:
        args = parser.parse_args(rest)
    except SystemExit as exc:
        return int(exc.code or 0)

    try:
        if action == "list":
            return _list()
        if action == "rename":
            http.post("/access/devices/rename", {"id": args.id, "name": args.name})
            print(f"Renamed {args.id}.")
        elif action == "revoke":
            # The CLI holds the token, not a device cookie, so it is never
            # revoking itself; confirm_self is for a browser on its own row.
            http.post("/access/devices/revoke", {"id": args.id, "confirm_self": True})
            print(f"Revoked {args.id}: that browser is signed out.")
        elif action == "reset":
            http.post("/access/devices/reset", {})
            print("Device list reset: every browser signs in again.")
        return 0
    except http.ApiError as exc:
        if exc.kind != "http":
            raise
        print(f"assist devices {action}: {_error_code(exc)}", file=sys.stderr)
        return 1
