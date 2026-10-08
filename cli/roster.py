"""Model-family command roster for the Assist CLI."""

import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

try:
    import tomllib
except ImportError:  # Python 3.10 fallback
    tomllib = None


_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


@dataclass(frozen=True)
class Roster:
    updated: str
    note: str
    roles: dict[str, dict[str, str]]


def _error(message: str) -> None:
    print(f"assist: {message}", file=sys.stderr)
    raise SystemExit(2)


def _value(raw: str, path: Path, line_number: int) -> str:
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] == "'":
        return raw[1:-1]
    if len(raw) >= 2 and raw[0] == raw[-1] == '"':
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            value = None
        if isinstance(value, str):
            return value
    _error(f"invalid roster value in {path}:{line_number} (use a quoted string)")
    raise AssertionError("unreachable")


def _load_fallback(lines: list[str], path: Path) -> Roster:
    """Parse the roster on Python 3.10, where tomllib is unavailable."""
    metadata: dict[str, str] = {}
    roles: dict[str, dict[str, str]] = {}
    current_role: str | None = None
    for line_number, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            parts = stripped[1:-1].split(".")
            if len(parts) != 2 or parts[0] != "roles" or not _KEY.fullmatch(parts[1]):
                _error(f"invalid roster section in {path}:{line_number}")
            current_role = parts[1]
            roles.setdefault(current_role, {})
            continue
        if "=" not in stripped:
            _error(f"invalid roster entry in {path}:{line_number}")
        key, raw_value = (part.strip() for part in stripped.split("=", 1))
        if not _KEY.fullmatch(key):
            _error(f"invalid roster key in {path}:{line_number}")
        value = _value(raw_value, path, line_number)
        target = metadata if current_role is None else roles[current_role]
        if key in target:
            _error(f"duplicate roster key {key!r} in {path}:{line_number}")
        target[key] = value

    return Roster(
        updated=metadata.get("updated", "-"),
        note=metadata.get("note", "-"),
        roles=roles,
    )


def load(path: Path) -> Roster:
    """Load the string-only roster TOML surface."""
    if not path.is_file():
        _error(f"roster not found: {path}; copy roster.example.toml there")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        _error(f"unable to read roster {path}: {exc}")

    if tomllib is None:
        return _load_fallback(text.splitlines(), path)
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        _error(f"invalid roster TOML in {path}: {exc}")
    roles = data.get("roles") or {}
    if not isinstance(roles, dict) or any(
        not isinstance(families, dict)
        or any(not isinstance(command, str) for command in families.values())
        for families in roles.values()
    ):
        _error(f"invalid roster TOML in {path}: roles and commands must be strings")
    return Roster(
        updated=str(data.get("updated", "-")),
        note=str(data.get("note", "-")),
        roles=roles,
    )


def resolve_command(
    roster: Roster,
    role: str,
    family: str | None = None,
    against: str | None = None,
) -> str:
    commands = roster.roles.get(role)
    if commands is None:
        valid = ", ".join(sorted(roster.roles)) or "(none)"
        _error(f"unknown role {role!r}; valid roles: {valid}")
    if family is not None:
        if family not in commands:
            valid = ", ".join(sorted(commands)) or "(none)"
            _error(f"unknown family {family!r} for role {role!r}; valid families: {valid}")
        return commands[family]
    if against is not None:
        if against not in commands:
            valid = ", ".join(sorted(commands)) or "(none)"
            _error(
                f"unknown family {against!r} for role {role!r}; "
                f"valid families: {valid}"
            )
        competitors = sorted(name for name in commands if name != against)
        if not competitors:
            valid = ", ".join(sorted(commands)) or "(none)"
            _error(
                f"no competing family for {against!r} in role {role!r}; "
                f"valid families: {valid}"
            )
        if len(competitors) > 1:
            _error(
                f"multiple competing families for {against!r} in role {role!r}: "
                f"{', '.join(competitors)}; choose one with --family"
            )
        return commands[competitors[0]]
    if len(commands) == 1:
        return next(iter(commands.values()))
    _error(
        "ask the user which model family to use (--family), or use --against "
        "<author family> for a judge"
    )
    raise AssertionError("unreachable")


def command(
    path: Path,
    role: str | None = None,
    family: str | None = None,
) -> int:
    roster = load(path)
    if role is not None:
        print(resolve_command(roster, role, family=family))
        return 0

    print(f"updated: {roster.updated}")
    print(f"note: {roster.note}")
    rows = [
        (role_name, family_name, launch_command)
        for role_name, families in roster.roles.items()
        for family_name, launch_command in families.items()
    ]
    if not rows:
        return 0
    role_width = max(len("ROLE"), *(len(row[0]) for row in rows))
    family_width = max(len("FAMILY"), *(len(row[1]) for row in rows))
    print(f"{'ROLE'.ljust(role_width)}  {'FAMILY'.ljust(family_width)}  COMMAND")
    for role_name, family_name, launch_command in rows:
        print(
            f"{role_name.ljust(role_width)}  "
            f"{family_name.ljust(family_width)}  {launch_command}"
        )
    return 0
