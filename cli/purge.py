"""Dry-run-first idle-session purge through the server's shared preview."""

import json
import os
import shlex
import sys
from urllib.parse import urlencode

from cli import http

IDLE_CHOICES = {"1h": 3600, "4h": 14400, "12h": 43200, "24h": 86400, "3d": 259200}


def command(idle="24h", yes=False, keep=(), json_output=False):
    response = http.get("/terminal/purge?" + urlencode({"idle": IDLE_CHOICES[idle]}))
    rows = response.get("sessions", [])
    unknown = sorted(set(keep) - {row["session"] for row in rows})
    if unknown:
        error = "--keep name not in purge preview: " + ", ".join(unknown)
        if json_output:
            print(json.dumps({"ok": False, "error": error, "killed": [], "skipped": []}))
        else:
            print("assist: " + error, file=sys.stderr)
        return 2
    for row in rows:
        if row["session"] in keep:
            row["checked"] = False
            row["reasons"] = [*row["reasons"], "kept by --keep"]
    chosen = [row["session"] for row in rows if row["checked"]]
    result = {**response, "dry_run": not yes, "killed": [], "skipped": []}
    if yes:
        result.update(http.post("/terminal/purge", {"preview": response["preview"], "sessions": chosen}))
    if json_output:
        print(json.dumps(result, indent=4))
    else:
        for row in rows:
            reason = ", ".join(row["reasons"])
            print(f"[{'x' if row['checked'] else ' '}] {row['session']}  "
                  f"{row['idle_seconds'] // 3600}h idle" + (f"  ({reason})" if reason else ""))
        if not yes:
            print(f"Dry run: {len(chosen)} selected; nothing killed.")
        else:
            for name in result["killed"]:
                print(f"killed {name}")
            for row in result["skipped"]:
                print(f"skipped {row['session']}: {row['reason']}")
        if os.environ.get("ASSIST_NO_HINTS") != "1":
            keeps = "".join(" --keep " + shlex.quote(name) for name in keep)
            next_command = f"assist purge --idle {idle}{keeps}" + ("" if yes or not chosen else " --yes")
            print(f"next: {next_command}  ·  assist ls", file=sys.stderr)
    return 0
