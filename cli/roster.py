"""Model-family command roster for the Assist CLI.

Supports both the legacy flat TOML format (`[roles.<role>] key = "cmd"`) and
the structured v2 schema (`[roles.<role>.<key>]`, `[quota_groups.<id>]`,
`[[bans]]`, `[judge_rules]`), with atomic mutators, timestamped `.bak` backups,
diagnostics (`doctor`), and flat-to-structured migration (`migrate`).
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import date, datetime
import fnmatch
import json
import os
from pathlib import Path
import re
import shlex
import sys
import tempfile

try:
    import tomllib
except ImportError:  # Python 3.10 fallback
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ImportError:
        tomllib = None


_KEY = re.compile(r"^[A-Za-z0-9_-]+$")
_VALID_ENTRY_STATUS = ("default", "option", "emergency", "trial")
_VALID_APPROVAL = ("none", "ask_daniel", "ask_karen")
_VALID_QUOTA_STATUS = ("ok", "low", "exhausted")
_VALID_BAN_MODE = ("hard", "ask_daniel")
_SUBCOMMANDS = frozenset({"set", "note", "quota", "ban", "rm", "show", "doctor", "migrate"})
_EFFORT_SUFFIX_RE = re.compile(r"-(?:minimal|low|medium|high|xhigh|ultra|max)$", re.IGNORECASE)
_DATE_SUFFIX_RE = re.compile(r"-(?:\d{4}-\d{2}(?:-\d{2})?|\d{8})$")
_VARIANT_SUFFIX_RE = re.compile(r"-(?:preview|mini|latest|exp|beta|v\d+(?:\.\d+)*)$", re.IGNORECASE)
_BRACKET_SUFFIX_RE = re.compile(r"\[[^\]]*\]$")
_CODEX_TIER_WORDS = frozenset({"luna", "sol", "astra", "terra"})
_CLAUDE_TIER_WORDS = frozenset({"opus", "sonnet", "haiku", "fable"})
_GEMINI_TIER_WORDS = frozenset({"gemini", "argon", "carbon", "neon", "hydrogen", "flash"})


@dataclass(frozen=True)
class RosterEntry:
    command: str
    family: str
    transport: str
    quota_group: str
    status: str = "default"
    approval: str = "none"
    notes: str = ""
    updated: str = ""
    source: str = ""
    allows_judging_families: tuple[str, ...] = ()
    paired_with: tuple[str, ...] = ()
    permissions: str = ""


@dataclass(frozen=True)
class QuotaGroup:
    status: str = "ok"
    check_cmd: str = ""
    notes: str = ""
    updated: str = ""
    source: str = ""


@dataclass(frozen=True)
class BanRule:
    pattern: str
    mode: str
    reason: str
    updated: str = ""
    source: str = ""


@dataclass(frozen=True)
class ExtraJudge:
    author_family: str
    judge: str
    reason: str = ""


@dataclass(frozen=True)
class JudgeRules:
    require_different_family: bool = True
    default_per_phase: str = ""
    milestone_role: str = "signoff"
    extra_judges: tuple[ExtraJudge, ...] = ()


@dataclass(frozen=True)
class Roster:
    updated: str
    note: str
    roles: dict[str, dict[str, str]]
    entries: dict[str, dict[str, RosterEntry]] = field(default_factory=dict)
    quota_groups: dict[str, QuotaGroup] = field(default_factory=dict)
    bans: tuple[BanRule, ...] = ()
    judge_rules: JudgeRules = field(default_factory=JudgeRules)
    schema_version: int = 1

    def __post_init__(self) -> None:
        if not self.entries and self.roles:
            synth: dict[str, dict[str, RosterEntry]] = {}
            for role_name, keys in self.roles.items():
                synth[role_name] = {
                    key_name: _synthesize_entry(key_name, cmd, self.updated)
                    for key_name, cmd in keys.items()
                }
            object.__setattr__(self, "entries", synth)


def _error(message: str) -> None:
    print(f"assist: {message}", file=sys.stderr)
    raise SystemExit(2)


def _canonical_family(name: str) -> str:
    low = (name or "").strip().lower()
    if low == "codex":
        return "openai"
    return low


def _split_raw_tokens(command: str) -> tuple[list[tuple[str, str]], list[str]]:
    """Split a launch command into (leading_env_assignments, command_tokens)."""
    try:
        tokens = shlex.split(command or "")
    except ValueError:
        tokens = (command or "").split()
    env_vars: list[tuple[str, str]] = []
    while tokens:
        head = tokens[0]
        if "=" in head and not head.startswith(("/", "./", "-")):
            name, _sep, val = head.partition("=")
            if name.replace("_", "").isalnum():
                env_vars.append((name, val))
                tokens.pop(0)
                continue
        if head in ("command", "exec"):
            tokens.pop(0)
            continue
        if os.path.basename(head) == "env":
            tokens.pop(0)
            while tokens:
                opt = tokens[0]
                if opt == "--":
                    tokens.pop(0)
                    break
                if opt in ("-u", "--unset", "-C", "--chdir", "-S", "--split-string"):
                    tokens.pop(0)
                    if tokens:
                        tokens.pop(0)
                    continue
                if opt in ("-i", "-0", "--ignore-environment", "--null") or opt.startswith(
                    ("--unset=", "--chdir=", "--split-string=")
                ):
                    tokens.pop(0)
                    continue
                if "=" in opt and not opt.startswith(("/", "./", "-")):
                    name, _sep, val = opt.partition("=")
                    if name.replace("_", "").isalnum():
                        env_vars.append((name, val))
                        tokens.pop(0)
                        continue
                break
            continue
        break
    return env_vars, tokens


def _split_tokens(command: str) -> list[str]:
    return _split_raw_tokens(command)[1]


def _infer_transport(command: str) -> str:
    tokens = _split_tokens(command)
    if not tokens:
        return ""
    exe = os.path.basename(tokens[0]).lower()
    if exe == "claude":
        return "claude"
    if exe == "codex":
        return "codex"
    if exe in ("agy", "antigravity"):
        return "agy"
    if exe in ("cursor-agent", "cursor"):
        return "cursor-agent"
    if exe == "npx" and len(tokens) > 1:
        for tok in tokens[1:]:
            if tok.startswith("-"):
                continue
            low = tok.lower()
            if low.startswith("@anthropic-ai/claude-code"):
                return "claude"
            if low.startswith("@openai/codex"):
                return "codex"
            break
    return exe


def _extract_model_args(command: str) -> list[str]:
    """Return every model value carried by env vars, -m/--model, glued -mX, or -c/--config model=X."""
    env_vars, tokens = _split_raw_tokens(command)
    found: list[str] = []
    for name, val in env_vars:
        if name.upper().endswith("_MODEL") and val.strip():
            found.append(val.strip().strip("\"'"))
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        key, sep, val = tok.partition("=")
        if key in ("--model", "-m"):
            if sep:
                if val.strip():
                    found.append(val.strip().strip("\"'"))
            elif i + 1 < len(tokens):
                found.append(tokens[i + 1].strip().strip("\"'"))
                i += 1
        elif tok.startswith("-m") and not tok.startswith("--") and len(tok) > 2:
            found.append(tok[2:].strip().strip("\"'"))
        elif key in ("-c", "--config"):
            cfg = val if sep else (tokens[i + 1] if i + 1 < len(tokens) else "")
            if not sep and i + 1 < len(tokens):
                i += 1
            ckey, csep, cval = cfg.partition("=")
            if csep and ckey.strip().lower() == "model" and cval.strip():
                found.append(cval.strip().strip("\"'"))
        i += 1
    return [m for m in found if m]


def _extract_model_arg(command: str) -> str | None:
    """Return the effective (last) model argument in `command`, or None."""
    models = _extract_model_args(command)
    return models[-1] if models else None


def _infer_model_lineage_family(command: str) -> str | None:
    """Infer independence lineage family from the effective model name when unambiguous."""
    model = (_extract_model_arg(command) or "").lower()
    if not model:
        return None
    parts = set(re.split(r"[^a-z0-9]+", model))
    if parts & _CLAUDE_TIER_WORDS or "claude" in parts:
        return "claude"
    if parts & _CODEX_TIER_WORDS or "gpt" in parts:
        return "openai"
    if parts & _GEMINI_TIER_WORDS:
        return "gemini"
    return None


def _infer_family(key: str, transport: str, command: str) -> str:
    lineage = _infer_model_lineage_family(command)
    if transport == "agy":
        return lineage or "gemini"
    if transport == "codex":
        return lineage or "openai"
    if transport == "claude":
        return lineage or "claude"
    if transport == "cursor-agent":
        return lineage or _canonical_family(key) or "cursor"
    return lineage or _canonical_family(key)


def _infer_quota_group(transport: str, command: str) -> str:
    model = (_extract_model_arg(command) or "").lower()
    if transport == "claude":
        return "claude-max"
    if transport == "codex":
        return "chatgpt-pro"
    if transport == "agy":
        if "argon" in model:
            return "agy-argon"
        if "carbon" in model:
            return "agy-carbon"
        if any(word in model for word in ("claude", "opus", "sonnet", "haiku", "fable", "gpt")):
            return "agy-claude-gpt"
        return "agy-gemini"
    if transport == "cursor-agent":
        return "cursor"
    return transport or "default"


def _default_check_cmd(quota_group: str) -> str:
    low = (quota_group or "").lower()
    if low.startswith("agy"):
        return "/quota"
    if "claude" in low:
        return "/usage"
    return "/status"


def _permission_flag_for_command(command: str, transport: str) -> str | None:
    tokens = _split_tokens(command)
    if transport == "codex":
        for tok in tokens:
            if tok in ("--yolo", "--dangerously-bypass-approvals-and-sandbox"):
                return tok
        return None
    if transport == "claude":
        for idx, tok in enumerate(tokens):
            if tok == "--dangerously-skip-permissions":
                return tok
            if tok == "--permission-mode" and idx + 1 < len(tokens) and tokens[idx + 1] == "bypassPermissions":
                return "--permission-mode bypassPermissions"
            if tok == "--permission-mode=bypassPermissions":
                return tok
        return None
    if transport == "agy":
        for tok in tokens:
            if tok == "--dangerously-skip-permissions":
                return tok
        return None
    if transport == "cursor-agent":
        for tok in tokens:
            if tok in ("--force", "--yolo"):
                return tok
        return None
    return None


def _permission_label(entry: RosterEntry) -> str:
    flag = _permission_flag_for_command(entry.command, entry.transport)
    if flag:
        label = f"FULL ({flag})"
    elif entry.permissions == "restricted":
        label = "RESTRICTED"
    else:
        label = "PROMPT (!)"
    if entry.approval != "none":
        label += f" [{entry.approval}]"
    return label


def _synthesize_entry(key: str, command: str, updated: str, notes: str = "", source: str = "") -> RosterEntry:
    transport = _infer_transport(command)
    family = _infer_family(key, transport, command)
    quota_group = _infer_quota_group(transport, command)
    return RosterEntry(
        command=command,
        family=family,
        transport=transport,
        quota_group=quota_group,
        status="default",
        approval="none",
        notes=notes,
        updated=updated if updated != "-" else "",
        source=source,
    )


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


def _strip_inline_comment(raw: str) -> str:
    """Strip a trailing `# comment` outside single/double quotes."""
    quote: str | None = None
    escaped = False
    for idx, ch in enumerate(raw):
        if escaped:
            escaped = False
            continue
        if quote == "'":
            if ch == "'":
                quote = None
            continue
        if quote == '"':
            if ch == "\\":
                escaped = True
            elif ch == '"':
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
        elif ch == "#":
            return raw[:idx].strip()
    return raw.strip()


def _load_fallback(lines: list[str], path: Path) -> Roster:
    """Parse flat v1 rosters on Python 3.10 where tomllib is unavailable."""
    metadata: dict[str, str] = {}
    roles: dict[str, dict[str, str]] = {}
    current_role: str | None = None
    for line_number, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("[["):
            _error(
                f"structured v2 roster in {path}:{line_number} requires Python 3.11+ "
                "(tomllib) or install tomli"
            )
        if stripped.startswith("["):
            header = _strip_inline_comment(stripped)
            if not header.endswith("]"):
                _error(f"invalid roster section in {path}:{line_number}")
            section = header[1:-1].strip()
            parts = section.split(".")
            if (
                section in ("judge_rules", "quota_groups")
                or parts[0] in ("quota_groups", "judge_rules")
                or (len(parts) >= 3 and parts[0] == "roles")
            ):
                _error(
                    f"structured v2 roster in {path}:{line_number} requires Python 3.11+ "
                    "(tomllib) or install tomli"
                )
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
        if current_role is None and key == "schema_version":
            _error(
                f"structured v2 roster in {path}:{line_number} requires Python 3.11+ "
                "(tomllib) or install tomli"
            )
        value = _value(_strip_inline_comment(raw_value), path, line_number)
        target = metadata if current_role is None else roles[current_role]
        if key in target:
            _error(f"duplicate roster key {key!r} in {path}:{line_number}")
        target[key] = value

    return Roster(
        updated=metadata.get("updated", "-"),
        note=metadata.get("note", "-"),
        roles=roles,
        schema_version=1,
    )


def _as_str_tuple(raw: object, field_name: str, path: Path) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
        _error(f"invalid roster TOML in {path}: {field_name} must be a list of strings")
    return tuple(item.strip() for item in raw if item.strip())


def _parse_entry_table(
    role_name: str,
    key_name: str,
    raw: dict,
    default_updated: str,
    path: Path,
) -> RosterEntry:
    cmd = raw.get("command")
    if not isinstance(cmd, str) or not cmd.strip():
        _error(f"invalid roster entry {role_name}.{key_name} in {path}: missing string 'command'")
    transport = raw.get("transport")
    if transport is None:
        transport = _infer_transport(cmd)
    elif not isinstance(transport, str):
        _error(f"invalid roster entry {role_name}.{key_name} in {path}: transport must be a string")
    family = raw.get("family")
    if family is None:
        family = _infer_family(key_name, transport, cmd)
    elif not isinstance(family, str) or not family.strip():
        _error(f"invalid roster entry {role_name}.{key_name} in {path}: family must be a non-empty string")
    quota_group = raw.get("quota_group")
    if quota_group is None:
        quota_group = _infer_quota_group(transport, cmd)
    elif not isinstance(quota_group, str):
        _error(f"invalid roster entry {role_name}.{key_name} in {path}: quota_group must be a string")
    status = raw.get("status", "default")
    if not isinstance(status, str) or status not in _VALID_ENTRY_STATUS:
        _error(
            f"invalid status {status!r} for {role_name}.{key_name} in {path}; "
            f"valid statuses: {', '.join(_VALID_ENTRY_STATUS)}"
        )
    approval = raw.get("approval", "none")
    if not isinstance(approval, str) or approval not in _VALID_APPROVAL:
        _error(
            f"invalid approval {approval!r} for {role_name}.{key_name} in {path}; "
            f"valid approvals: {', '.join(_VALID_APPROVAL)}"
        )
    notes = raw.get("notes", "")
    if not isinstance(notes, str):
        _error(f"invalid notes for {role_name}.{key_name} in {path}: must be a string")
    updated = raw.get("updated", default_updated if default_updated != "-" else "")
    if not isinstance(updated, str):
        updated = str(updated)
    source = raw.get("source", "")
    if not isinstance(source, str):
        _error(f"invalid source for {role_name}.{key_name} in {path}: must be a string")
    permissions = raw.get("permissions", "")
    if not isinstance(permissions, str):
        _error(f"invalid permissions for {role_name}.{key_name} in {path}: must be a string")
    allows = _as_str_tuple(raw.get("allows_judging_families"), "allows_judging_families", path)
    paired = _as_str_tuple(raw.get("paired_with"), "paired_with", path)
    return RosterEntry(
        command=cmd,
        family=family.strip(),
        transport=transport.strip(),
        quota_group=quota_group.strip(),
        status=status,
        approval=approval,
        notes=notes,
        updated=updated,
        source=source,
        allows_judging_families=allows,
        paired_with=paired,
        permissions=permissions.strip(),
    )


def _parse_toml_text(text: str, path: Path) -> Roster:
    """Parse and validate TOML text into a Roster (used by both load() and write_atomic())."""
    if tomllib is None:
        return _load_fallback(text.splitlines(), path)
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        _error(f"invalid roster TOML in {path}: {exc}")

    updated = str(data.get("updated", "-"))
    note = str(data.get("note", "-"))
    raw_roles = data.get("roles") or {}
    if not isinstance(raw_roles, dict):
        _error(f"invalid roster TOML in {path}: [roles] must be a table")

    roles: dict[str, dict[str, str]] = {}
    entries: dict[str, dict[str, RosterEntry]] = {}
    saw_structured = bool(
        data.get("schema_version") == 2
        or "quota_groups" in data
        or "bans" in data
        or "judge_rules" in data
    )
    for role_name, role_table in raw_roles.items():
        if not isinstance(role_table, dict):
            _error(f"invalid roster TOML in {path}: roles.{role_name} must be a table")
        roles[role_name] = {}
        entries[role_name] = {}
        for key_name, val in role_table.items():
            if isinstance(val, str):
                roles[role_name][key_name] = val
                entries[role_name][key_name] = _synthesize_entry(key_name, val, updated)
            elif isinstance(val, dict):
                saw_structured = True
                entry = _parse_entry_table(role_name, key_name, val, updated, path)
                roles[role_name][key_name] = entry.command
                entries[role_name][key_name] = entry
            else:
                _error(
                    f"invalid roster TOML in {path}: roles.{role_name}.{key_name} "
                    "must be a command string or entry table"
                )

    raw_quotas = data.get("quota_groups") or {}
    if not isinstance(raw_quotas, dict):
        _error(f"invalid roster TOML in {path}: [quota_groups] must be a table")
    quota_groups: dict[str, QuotaGroup] = {}
    for qid, qval in raw_quotas.items():
        if not isinstance(qval, dict):
            _error(f"invalid quota_groups.{qid} in {path}: must be a table")
        qstatus = qval.get("status", "ok")
        if not isinstance(qstatus, str) or qstatus not in _VALID_QUOTA_STATUS:
            _error(
                f"invalid quota status {qstatus!r} for quota_groups.{qid} in {path}; "
                f"valid statuses: {', '.join(_VALID_QUOTA_STATUS)}"
            )
        quota_groups[qid] = QuotaGroup(
            status=qstatus,
            check_cmd=str(qval.get("check_cmd", "")),
            notes=str(qval.get("notes", "")),
            updated=str(qval.get("updated", "")),
            source=str(qval.get("source", "")),
        )

    raw_bans = data.get("bans") or []
    if not isinstance(raw_bans, list):
        _error(f"invalid roster TOML in {path}: [[bans]] must be an array of tables")
    bans: list[BanRule] = []
    for idx, bval in enumerate(raw_bans):
        if not isinstance(bval, dict):
            _error(f"invalid bans[{idx}] in {path}: must be a table")
        pattern = bval.get("pattern")
        mode = bval.get("mode")
        reason = bval.get("reason", "")
        if not isinstance(pattern, str) or not pattern.strip():
            _error(f"invalid bans[{idx}] in {path}: pattern must be a non-empty string")
        if not isinstance(mode, str) or mode not in _VALID_BAN_MODE:
            _error(
                f"invalid ban mode {mode!r} for pattern {pattern!r} in {path}; "
                f"valid modes: {', '.join(_VALID_BAN_MODE)}"
            )
        if not isinstance(reason, str):
            _error(f"invalid ban reason for pattern {pattern!r} in {path}: must be a string")
        bans.append(
            BanRule(
                pattern=pattern.strip(),
                mode=mode,
                reason=reason,
                updated=str(bval.get("updated", "")),
                source=str(bval.get("source", "")),
            )
        )

    raw_jr = data.get("judge_rules") or {}
    if not isinstance(raw_jr, dict):
        _error(f"invalid roster TOML in {path}: [judge_rules] must be a table")
    req_diff = raw_jr.get("require_different_family", True)
    if not isinstance(req_diff, bool):
        _error(f"invalid judge_rules.require_different_family in {path}: must be a boolean")
    extra_judges: list[ExtraJudge] = []
    raw_extras = raw_jr.get("extra_judges") or []
    if not isinstance(raw_extras, list):
        _error(f"invalid judge_rules.extra_judges in {path}: must be a list of tables")
    for idx, ej in enumerate(raw_extras):
        if not isinstance(ej, dict):
            _error(f"invalid judge_rules.extra_judges[{idx}] in {path}: must be a table")
        af = ej.get("author_family")
        jg = ej.get("judge")
        if not isinstance(af, str) or not af.strip() or not isinstance(jg, str) or not jg.strip():
            _error(
                f"invalid judge_rules.extra_judges[{idx}] in {path}: "
                "author_family and judge must be non-empty strings"
            )
        extra_judges.append(
            ExtraJudge(
                author_family=af.strip(),
                judge=jg.strip(),
                reason=str(ej.get("reason", "")),
            )
        )
    judge_rules = JudgeRules(
        require_different_family=req_diff,
        default_per_phase=str(raw_jr.get("default_per_phase", "")),
        milestone_role=str(raw_jr.get("milestone_role", "signoff")),
        extra_judges=tuple(extra_judges),
    )

    schema_version = 2 if saw_structured else 1
    return Roster(
        updated=updated,
        note=note,
        roles=roles,
        entries=entries,
        quota_groups=quota_groups,
        bans=tuple(bans),
        judge_rules=judge_rules,
        schema_version=schema_version,
    )


def load(path: Path) -> Roster:
    """Load a flat (v1) or structured (v2) roster TOML file."""
    if not path.is_file():
        _error(f"roster not found: {path}; copy roster.example.toml there")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        _error(f"unable to read roster {path}: {exc}")
    return _parse_toml_text(text, path)


def _normalize_model_candidates(raw_model: str, transport: str) -> set[str]:
    """Expand a model specifier into canonical forms for bypass-resistant ban matching."""
    low = raw_model.strip().strip("\"'").lower()
    if not low:
        return set()
    cands: set[str] = {low}
    stripped = _BRACKET_SUFFIX_RE.sub("", low)
    cands.add(stripped)

    # Iteratively peel effort, date, and variant suffixes so e.g.
    # gpt-5.6-luna-2026-11-01, gpt-5.6-luna-preview, gpt-6.1-luna-mini,
    # gpt-5.6-luna-ultra all yield gpt-5.6-luna / gpt-6.1-luna.
    cur = stripped
    for _ in range(4):
        nxt = _EFFORT_SUFFIX_RE.sub("", cur)
        nxt = _DATE_SUFFIX_RE.sub("", nxt)
        nxt = _VARIANT_SUFFIX_RE.sub("", nxt)
        if nxt == cur or not nxt:
            break
        cur = nxt
        cands.add(cur)

    # Hyphen-separated components & leading two-part prefixes (e.g. claude-opus-4-6 -> claude-opus, opus)
    for form in list(cands):
        parts = [p for p in form.split("-") if p]
        for part in parts:
            cands.add(part)
        if len(parts) >= 2:
            cands.add(f"{parts[0]}-{parts[1]}")

    # Bare tier aliases expanded to canonical prefixed forms so gpt-*-luna catches
    # `codex -m luna` and claude-opus* catches `claude --model opus`.
    if cur in _CODEX_TIER_WORDS and transport in ("codex", "agy", ""):
        cands.add(f"gpt-0-{cur}")
    if cur in _CLAUDE_TIER_WORDS and transport in ("claude", "agy", ""):
        cands.add(f"claude-{cur}")
    return cands


def match_bans(roster: Roster, command: str) -> list[BanRule]:
    """Return all BanRules matching `command` across flags, env vars, aliases, and suffixes."""
    if not roster.bans or not command:
        return []
    transport = _infer_transport(command)
    env_vars, tokens = _split_raw_tokens(command)
    candidates: set[str] = {command.lower()}
    for name, val in env_vars:
        candidates.add(f"{name}={val}".lower())
        if val:
            candidates.add(val.lower())
    for tok in tokens:
        candidates.add(tok.lower())
    for raw_model in _extract_model_args(command):
        candidates.update(_normalize_model_candidates(raw_model, transport))

    matched: list[BanRule] = []
    for ban in roster.bans:
        pat = ban.pattern.lower()
        if any(fnmatch.fnmatchcase(cand, pat) for cand in candidates):
            matched.append(ban)
    return matched


def enforce_launch_policy(
    roster: Roster,
    role: str,
    key: str,
    entry: RosterEntry,
    approved_by_daniel: bool = False,
    approved_by_karen: bool = False,
) -> None:
    """Enforce [[bans]] and entry approval before a launch creates any pane."""
    matched = match_bans(roster, entry.command)
    hard = [b for b in matched if b.mode == "hard"]
    if hard:
        first = hard[0]
        detail = f": {first.reason}" if first.reason else ""
        _error(f"launch refused by hard ban {first.pattern!r}{detail}")
    ask = [b for b in matched if b.mode == "ask_daniel"]
    if ask and not approved_by_daniel:
        first = ask[0]
        detail = f" ({first.reason})" if first.reason else ""
        _error(
            f"model matches ask_daniel ban {first.pattern!r}{detail}; "
            "re-run with --approved-by-daniel after explicit approval"
        )
    if entry.approval == "ask_daniel" and not approved_by_daniel:
        _error(
            f"roster entry {role}.{key} requires approval (ask_daniel); "
            "re-run with --approved-by-daniel after explicit approval"
        )
    if entry.approval == "ask_karen" and not approved_by_karen:
        _error(
            f"roster entry {role}.{key} requires approval (ask_karen); "
            "re-run with --approved-by-karen after explicit approval"
        )


def resolve_entry(
    roster: Roster,
    role: str,
    family: str | None = None,
    against: str | None = None,
    key: str | None = None,
) -> tuple[str, RosterEntry]:
    """Resolve (entry_key, RosterEntry) for a role and selector."""
    role_entries = roster.entries.get(role)
    if role_entries is None:
        valid = ", ".join(sorted(roster.roles)) or "(none)"
        _error(f"unknown role {role!r}; valid roles: {valid}")

    if key is not None:
        if key not in role_entries:
            valid = ", ".join(sorted(role_entries)) or "(none)"
            _error(f"unknown key {key!r} for role {role!r}; valid keys: {valid}")
        return key, role_entries[key]

    if family is not None:
        if family in role_entries:
            return family, role_entries[family]
        canon = _canonical_family(family)
        matches = [
            (k, e)
            for k, e in role_entries.items()
            if _canonical_family(e.family) == canon or _canonical_family(k) == canon
        ]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            defaults = [(k, e) for k, e in matches if e.status == "default"]
            if len(defaults) == 1:
                return defaults[0]
            names = ", ".join(sorted(k for k, _ in matches))
            _error(f"multiple entries match family {family!r} for role {role!r}: {names}; choose one with --key")
        valid = ", ".join(sorted(role_entries)) or "(none)"
        _error(f"unknown family {family!r} for role {role!r}; valid families: {valid}")

    if against is not None:
        canon_against = _canonical_family(against)
        present_match = against in role_entries or any(
            _canonical_family(e.family) == canon_against for e in role_entries.values()
        )
        if not present_match:
            valid = ", ".join(sorted(role_entries)) or "(none)"
            _error(f"unknown family {against!r} for role {role!r}; valid families: {valid}")
        competitors = sorted(
            k
            for k, e in role_entries.items()
            if k != against
            and _canonical_family(e.family) != canon_against
            and not any(b.mode == "hard" for b in match_bans(roster, e.command))
        )
        if not competitors:
            valid = ", ".join(sorted(role_entries)) or "(none)"
            _error(
                f"no competing family for {against!r} in role {role!r}; "
                f"valid families: {valid}"
            )
        if len(competitors) > 1:
            _error(
                f"multiple competing families for {against!r} in role {role!r}: "
                f"{', '.join(competitors)}; choose one with --family"
            )
        chosen = competitors[0]
        return chosen, role_entries[chosen]

    if len(role_entries) == 1:
        only_key = next(iter(role_entries))
        return only_key, role_entries[only_key]
    _error(
        "ask the user which model family to use (--family), or use --against "
        "<author family> for a judge"
    )
    raise AssertionError("unreachable")


def list_competitors(roster: Roster, role: str, against: str) -> list[tuple[str, RosterEntry]]:
    """List all non-hard-banned entries in `role` whose family competes with `against`."""
    role_entries = roster.entries.get(role)
    if role_entries is None:
        valid = ", ".join(sorted(roster.roles)) or "(none)"
        _error(f"unknown role {role!r}; valid roles: {valid}")
    canon_against = _canonical_family(against)
    return [
        (k, e)
        for k, e in sorted(role_entries.items())
        if k != against
        and _canonical_family(e.family) != canon_against
        and not any(b.mode == "hard" for b in match_bans(roster, e.command))
    ]


def resolve_command(
    roster: Roster,
    role: str,
    family: str | None = None,
    against: str | None = None,
    key: str | None = None,
) -> str:
    """Resolve the launch command string for a role."""
    _key, entry = resolve_entry(roster, role, family=family, against=against, key=key)
    return entry.command


def _lookup_ref(roster: Roster, ref: str) -> tuple[str, str, RosterEntry] | None:
    if "." not in ref:
        return None
    role_name, key_name = ref.split(".", 1)
    entry = (roster.entries.get(role_name) or {}).get(key_name)
    if entry is None:
        return None
    return role_name, key_name, entry


def judge_rule_for(roster: Roster, author_family: str) -> dict:
    """Compute which families judge `author_family` and any configured extra judges."""
    canon_author = _canonical_family(author_family)
    jr = roster.judge_rules
    judge_entries = roster.entries.get("judge") or {}

    eligible_judges: list[dict] = []
    eligible_families: set[str] = set()
    for key_name, entry in sorted(judge_entries.items()):
        if any(b.mode == "hard" for b in match_bans(roster, entry.command)):
            continue
        entry_fam = _canonical_family(entry.family)
        if jr.require_different_family and entry_fam == canon_author:
            continue
        eligible_families.add(entry_fam)
        eligible_judges.append(
            {
                "role": "judge",
                "key": key_name,
                "ref": f"judge.{key_name}",
                "family": entry.family,
                "command": entry.command,
                "status": entry.status,
            }
        )

    extra_by_ref: dict[str, dict] = {}
    for extra in jr.extra_judges:
        if extra.author_family != "*" and _canonical_family(extra.author_family) != canon_author:
            continue
        found = _lookup_ref(roster, extra.judge)
        if found is None:
            extra_by_ref[extra.judge] = {
                "ref": extra.judge,
                "reason": extra.reason,
                "missing": True,
            }
        else:
            r_name, k_name, ent = found
            if any(b.mode == "hard" for b in match_bans(roster, ent.command)):
                continue
            extra_by_ref[extra.judge] = {
                "role": r_name,
                "key": k_name,
                "ref": extra.judge,
                "family": ent.family,
                "command": ent.command,
                "paired_with": list(ent.paired_with),
                "reason": extra.reason or ent.notes,
            }

    for r_name, role_entries in roster.entries.items():
        for k_name, ent in role_entries.items():
            if any(b.mode == "hard" for b in match_bans(roster, ent.command)):
                continue
            allowed = {_canonical_family(f) if f != "*" else "*" for f in ent.allows_judging_families}
            if canon_author in allowed or "*" in allowed:
                ref = f"{r_name}.{k_name}"
                if ref not in extra_by_ref:
                    extra_by_ref[ref] = {
                        "role": r_name,
                        "key": k_name,
                        "ref": ref,
                        "family": ent.family,
                        "command": ent.command,
                        "paired_with": list(ent.paired_with),
                        "reason": ent.notes,
                    }

    return {
        "author_family": canon_author,
        "require_different_family": jr.require_different_family,
        "default_per_phase": jr.default_per_phase,
        "milestone_role": jr.milestone_role,
        "eligible_judge_families": sorted(eligible_families),
        "eligible_judges": eligible_judges,
        "extra_judges": list(extra_by_ref.values()),
    }


def _entry_to_dict(role_name: str, key_name: str, entry: RosterEntry) -> dict:
    return {
        "role": role_name,
        "key": key_name,
        "command": entry.command,
        "family": entry.family,
        "transport": entry.transport,
        "quota_group": entry.quota_group,
        "status": entry.status,
        "approval": entry.approval,
        "permissions": _permission_label(entry),
        "permissions_override": entry.permissions,
        "allows_judging_families": list(entry.allows_judging_families),
        "paired_with": list(entry.paired_with),
        "notes": entry.notes,
        "updated": entry.updated,
        "source": entry.source,
    }


def _quota_to_dict(qid: str, qg: QuotaGroup) -> dict:
    return {
        "id": qid,
        "status": qg.status,
        "check_cmd": qg.check_cmd,
        "notes": qg.notes,
        "updated": qg.updated,
        "source": qg.source,
    }


def _ban_to_dict(ban: BanRule) -> dict:
    return {
        "pattern": ban.pattern,
        "mode": ban.mode,
        "reason": ban.reason,
        "updated": ban.updated,
        "source": ban.source,
    }


def _toml_str(value: str) -> str:
    # TOML forbids literal DEL (0x7f) inside basic strings; escape it as \u007f.
    return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")


def _toml_str_list(values: tuple[str, ...]) -> str:
    return "[" + ", ".join(_toml_str(v) for v in values) + "]"


def _toml_key(key: str) -> str:
    if _KEY.fullmatch(key):
        return key
    return _toml_str(key)


def render_toml(roster: Roster) -> str:
    """Serialize a Roster into deterministic structured v2 TOML."""
    lines: list[str] = [
        "schema_version = 2",
        f"updated = {_toml_str(roster.updated if roster.updated != '-' else date.today().isoformat())}",
        f"note = {_toml_str(roster.note if roster.note != '-' else '')}",
        "",
        "[judge_rules]",
        f"require_different_family = {'true' if roster.judge_rules.require_different_family else 'false'}",
    ]
    if roster.judge_rules.default_per_phase:
        lines.append(f"default_per_phase = {_toml_str(roster.judge_rules.default_per_phase)}")
    if roster.judge_rules.milestone_role:
        lines.append(f"milestone_role = {_toml_str(roster.judge_rules.milestone_role)}")
    for extra in roster.judge_rules.extra_judges:
        lines.extend(
            [
                "",
                "[[judge_rules.extra_judges]]",
                f"author_family = {_toml_str(extra.author_family)}",
                f"judge = {_toml_str(extra.judge)}",
                f"reason = {_toml_str(extra.reason)}",
            ]
        )

    for qid, qg in roster.quota_groups.items():
        lines.extend(
            [
                "",
                f"[quota_groups.{_toml_key(qid)}]",
                f"status = {_toml_str(qg.status)}",
                f"check_cmd = {_toml_str(qg.check_cmd)}",
                f"notes = {_toml_str(qg.notes)}",
                f"updated = {_toml_str(qg.updated)}",
                f"source = {_toml_str(qg.source)}",
            ]
        )

    for ban in roster.bans:
        lines.extend(
            [
                "",
                "[[bans]]",
                f"pattern = {_toml_str(ban.pattern)}",
                f"mode = {_toml_str(ban.mode)}",
                f"reason = {_toml_str(ban.reason)}",
                f"updated = {_toml_str(ban.updated)}",
                f"source = {_toml_str(ban.source)}",
            ]
        )

    for role_name, role_entries in roster.entries.items():
        for key_name, entry in role_entries.items():
            lines.extend(
                [
                    "",
                    f"[roles.{_toml_key(role_name)}.{_toml_key(key_name)}]",
                    f"command = {_toml_str(entry.command)}",
                    f"family = {_toml_str(entry.family)}",
                    f"transport = {_toml_str(entry.transport)}",
                    f"quota_group = {_toml_str(entry.quota_group)}",
                    f"status = {_toml_str(entry.status)}",
                    f"approval = {_toml_str(entry.approval)}",
                ]
            )
            if entry.allows_judging_families:
                lines.append(f"allows_judging_families = {_toml_str_list(entry.allows_judging_families)}")
            if entry.paired_with:
                lines.append(f"paired_with = {_toml_str_list(entry.paired_with)}")
            if entry.permissions:
                lines.append(f"permissions = {_toml_str(entry.permissions)}")
            lines.extend(
                [
                    f"notes = {_toml_str(entry.notes)}",
                    f"updated = {_toml_str(entry.updated)}",
                    f"source = {_toml_str(entry.source)}",
                ]
            )

    return "\n".join(lines) + "\n"


def _next_backup_path(path: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    candidate = path.parent / f"{path.name}.{stamp}.bak"
    counter = 1
    while candidate.exists():
        candidate = path.parent / f"{path.name}.{stamp}.{counter}.bak"
        counter += 1
    return candidate


def write_atomic(path: Path, roster: Roster) -> Path | None:
    """Atomically write structured TOML to `path`, creating a timestamped `.bak` if it exists."""
    if tomllib is None:
        _error("writing structured roster requires Python 3.11+ (tomllib) or install tomli")
    rendered = render_toml(roster)
    # Full schema round-trip validation before creating any backup or touching the file.
    _parse_toml_text(rendered, path)

    path.parent.mkdir(parents=True, exist_ok=True)
    backup_path: Path | None = None
    mode = 0o644
    if path.exists():
        stat_info = path.stat()
        mode = stat_info.st_mode & 0o777
        backup_path = _next_backup_path(path)
        raw_orig = path.read_bytes()
        bak_fd = os.open(backup_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        try:
            os.write(bak_fd, raw_orig)
            os.fsync(bak_fd)
        finally:
            os.close(bak_fd)

    tmp_path: Path | None = None
    try:
        fd, tmp_name = tempfile.mkstemp(
            dir=str(path.parent),
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        tmp_path = Path(tmp_name)
        try:
            os.fchmod(fd, mode)
            os.write(fd, rendered.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp_path, path)
        tmp_path = None
    finally:
        if tmp_path is not None and tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
    return backup_path


def _extract_flat_inline_comments(text: str) -> dict[tuple[str, str], str]:
    """Extract trailing `# comment` text from flat `[roles.<role>]` key = '...' lines."""
    comments: dict[tuple[str, str], str] = {}
    current_role: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("[") and not stripped.startswith("[["):
            header = stripped.split("#", 1)[0].strip()
            if header.startswith("[") and header.endswith("]"):
                parts = header[1:-1].split(".")
                if len(parts) == 2 and parts[0] == "roles" and _KEY.fullmatch(parts[1]):
                    current_role = parts[1]
                else:
                    current_role = None
            continue
        if current_role is None or "=" not in line:
            continue
        key_part, rhs = line.split("=", 1)
        key = key_part.strip()
        if not _KEY.fullmatch(key):
            continue
        # Scan rhs respecting quotes to find a trailing comment outside quotes.
        quote: str | None = None
        escaped = False
        comment = ""
        for idx, ch in enumerate(rhs):
            if escaped:
                escaped = False
                continue
            if quote == "'":
                if ch == "'":
                    quote = None
                continue
            if quote == '"':
                if ch == "\\":
                    escaped = True
                elif ch == '"':
                    quote = None
                continue
            if ch in ("'", '"'):
                quote = ch
            elif ch == "#":
                comment = rhs[idx + 1 :].strip()
                break
        if comment:
            comments[(current_role, key)] = comment
    return comments


def migrate_roster(src_path: Path) -> Roster:
    """Convert a flat (or mixed) roster at `src_path` into a complete structured v2 Roster."""
    loaded = load(src_path)
    raw_text = src_path.read_text(encoding="utf-8")
    inline_comments = _extract_flat_inline_comments(raw_text)
    today = date.today().isoformat()
    base_updated = loaded.updated if loaded.updated not in ("", "-") else today

    new_entries: dict[str, dict[str, RosterEntry]] = {}
    new_roles: dict[str, dict[str, str]] = {}
    referenced_quotas: set[str] = set()

    has_judge_claude = "claude" in (loaded.entries.get("judge") or {})
    extra_judges: list[ExtraJudge] = list(loaded.judge_rules.extra_judges)

    for role_name, role_entries in loaded.entries.items():
        new_entries[role_name] = {}
        new_roles[role_name] = {}
        for key_name, ent in role_entries.items():
            notes = ent.notes or inline_comments.get((role_name, key_name), "")
            allows = ent.allows_judging_families
            paired = ent.paired_with
            if role_name == "signoff" and key_name == "gemini" and "carbon" in ent.command.lower():
                if not allows:
                    allows = ("gemini",)
                if not paired and has_judge_claude:
                    paired = ("judge.claude",)
                if not any(ej.judge == "signoff.gemini" for ej in extra_judges):
                    extra_judges.append(
                        ExtraJudge(
                            author_family="gemini",
                            judge="signoff.gemini",
                            reason="Carbon runs in parallel with Opus on Gemini-built major milestones.",
                        )
                    )
            migrated_entry = RosterEntry(
                command=ent.command,
                family=ent.family,
                transport=ent.transport,
                quota_group=ent.quota_group,
                status=ent.status,
                approval=ent.approval,
                notes=notes,
                updated=ent.updated or base_updated,
                source=ent.source or "migrated",
                allows_judging_families=allows,
                paired_with=paired,
                permissions=ent.permissions,
            )
            new_entries[role_name][key_name] = migrated_entry
            new_roles[role_name][key_name] = migrated_entry.command
            if migrated_entry.quota_group:
                referenced_quotas.add(migrated_entry.quota_group)

    new_quotas: dict[str, QuotaGroup] = dict(loaded.quota_groups)
    for qid in sorted(referenced_quotas):
        if qid not in new_quotas:
            new_quotas[qid] = QuotaGroup(
                status="ok",
                check_cmd=_default_check_cmd(qid),
                notes="",
                updated=base_updated,
                source="migrated",
            )

    bans = list(loaded.bans)
    if not bans:
        note_low = (loaded.note or "").lower()
        if "never luna" in note_low:
            bans.append(
                BanRule(
                    pattern="*luna*",
                    mode="hard",
                    reason="Never use Luna (migrated from roster note)",
                    updated=base_updated,
                    source="migrated",
                )
            )
        if "never haiku" in note_low:
            bans.append(
                BanRule(
                    pattern="*haiku*",
                    mode="hard",
                    reason="Never use Haiku (migrated from roster note)",
                    updated=base_updated,
                    source="migrated",
                )
            )
        if "no fable" in note_low:
            bans.append(
                BanRule(
                    pattern="*fable*",
                    mode="ask_daniel",
                    reason="No Fable panes without explicit approval (migrated from roster note)",
                    updated=base_updated,
                    source="migrated",
                )
            )

    default_per_phase = loaded.judge_rules.default_per_phase or (
        "judge.claude" if has_judge_claude else ""
    )
    milestone_role = loaded.judge_rules.milestone_role or (
        "signoff" if "signoff" in new_entries else ""
    )
    judge_rules = JudgeRules(
        require_different_family=loaded.judge_rules.require_different_family,
        default_per_phase=default_per_phase,
        milestone_role=milestone_role,
        extra_judges=tuple(extra_judges),
    )

    return Roster(
        updated=base_updated,
        note=loaded.note,
        roles=new_roles,
        entries=new_entries,
        quota_groups=new_quotas,
        bans=tuple(bans),
        judge_rules=judge_rules,
        schema_version=2,
    )


def run_doctor(roster: Roster) -> dict:
    """Validate roster health without modifying anything."""
    errors: list[str] = []
    warnings: list[str] = []

    if roster.schema_version < 2:
        warnings.append(
            "roster uses legacy flat schema (v1); convert with: "
            "assist roster migrate --dry-run"
        )

    entry_count = 0
    for role_name, role_entries in roster.entries.items():
        for key_name, ent in role_entries.items():
            entry_count += 1
            ref = f"{role_name}.{key_name}"
            if not ent.command.strip():
                errors.append(f"{ref}: empty command")
            else:
                try:
                    shlex.split(ent.command)
                except ValueError as exc:
                    errors.append(f"{ref}: invalid shell quoting in command ({exc})")
            if not ent.family:
                errors.append(f"{ref}: missing family")
            else:
                inferred_lineage = _infer_model_lineage_family(ent.command)
                if inferred_lineage and _canonical_family(ent.family) != inferred_lineage:
                    errors.append(
                        f"{ref}: declared family {ent.family!r} contradicts model lineage "
                        f"{inferred_lineage!r}"
                    )
            if not ent.transport:
                warnings.append(f"{ref}: could not infer transport")
            flag = _permission_flag_for_command(ent.command, ent.transport)
            if flag is None and ent.permissions != "restricted":
                errors.append(
                    f"{ref}: missing full-permissions flag for transport {ent.transport or '(unknown)'} "
                    '(add the flag or set permissions = "restricted")'
                )
            for ban in match_bans(roster, ent.command):
                if ban.mode == "hard":
                    errors.append(
                        f"{ref}: command matches hard ban {ban.pattern!r} ({ban.reason})"
                    )
                else:
                    warnings.append(
                        f"{ref}: command matches ask_daniel ban {ban.pattern!r} ({ban.reason})"
                    )
            if roster.schema_version >= 2:
                if not ent.quota_group.strip():
                    errors.append(f"{ref}: missing quota_group")
                elif ent.quota_group not in roster.quota_groups:
                    errors.append(f"{ref}: references undefined quota_group {ent.quota_group!r}")
            for pref in ent.paired_with:
                if _lookup_ref(roster, pref) is None:
                    errors.append(f"{ref}: paired_with references unknown entry {pref!r}")

    for qid, qg in roster.quota_groups.items():
        if qg.status in ("low", "exhausted"):
            hint = f" (check: {qg.check_cmd})" if qg.check_cmd else ""
            warnings.append(f"quota_group {qid}: status is {qg.status}{hint}")
        if not qg.check_cmd:
            warnings.append(f"quota_group {qid}: missing check_cmd")

    jr = roster.judge_rules
    if jr.default_per_phase and _lookup_ref(roster, jr.default_per_phase) is None:
        errors.append(f"judge_rules.default_per_phase references unknown entry {jr.default_per_phase!r}")
    for ej in jr.extra_judges:
        if _lookup_ref(roster, ej.judge) is None:
            errors.append(f"judge_rules.extra_judges references unknown entry {ej.judge!r}")
    if jr.require_different_family and "judge" in roster.entries:
        judge_fams = {
            _canonical_family(e.family)
            for e in roster.entries["judge"].values()
            if not any(b.mode == "hard" for b in match_bans(roster, e.command))
        }
        if len(judge_fams) < 2:
            warnings.append(
                f"judge role has fewer than 2 distinct families ({', '.join(sorted(judge_fams)) or 'none'})"
            )

    return {
        "ok": len(errors) == 0,
        "schema_version": roster.schema_version,
        "entries": entry_count,
        "quota_groups": len(roster.quota_groups),
        "bans": len(roster.bans),
        "errors": errors,
        "warnings": warnings,
    }


def _parse_role_key(spec: str) -> tuple[str, str]:
    if "." not in spec:
        _error(f"expected <role>.<key>, got {spec!r}")
    role_name, key_name = spec.split(".", 1)
    if not _KEY.fullmatch(role_name) or not _KEY.fullmatch(key_name):
        _error(f"invalid <role>.<key> {spec!r} (use letters, digits, '-' and '_')")
    return role_name, key_name


def _with_updated_entries(
    roster: Roster,
    new_entries: dict[str, dict[str, RosterEntry]],
    *,
    quota_groups: dict[str, QuotaGroup] | None = None,
    bans: tuple[BanRule, ...] | None = None,
    judge_rules: JudgeRules | None = None,
    updated: str | None = None,
) -> Roster:
    new_roles = {
        r: {k: e.command for k, e in ents.items()}
        for r, ents in new_entries.items()
    }
    return Roster(
        updated=updated or date.today().isoformat(),
        note=roster.note,
        roles=new_roles,
        entries=new_entries,
        quota_groups=roster.quota_groups if quota_groups is None else quota_groups,
        bans=roster.bans if bans is None else bans,
        judge_rules=roster.judge_rules if judge_rules is None else judge_rules,
        schema_version=2,
    )


def _print_table(roster: Roster, role_filter: str | None = None) -> None:
    print(
        "perms legend: codex --yolo → \"permissions: YOLO mode\"  ·  "
        "claude --dangerously-skip-permissions → \"bypass permissions on\"  ·  "
        "agy --dangerously-skip-permissions → argv"
    )
    print(f"updated: {roster.updated}")
    if roster.note and roster.note != "-":
        print(f"note: {roster.note}")

    rows: list[tuple[str, str, str, str, str, str, str, str]] = []
    for role_name, role_entries in roster.entries.items():
        if role_filter is not None and role_name != role_filter:
            continue
        for key_name, ent in role_entries.items():
            rows.append(
                (
                    role_name,
                    key_name,
                    ent.family,
                    ent.status,
                    _permission_label(ent),
                    ent.quota_group or "-",
                    ent.command,
                    ent.notes,
                )
            )

    if rows:
        headers = ("ROLE", "KEY", "FAMILY", "STATUS", "PERMS", "QUOTA", "COMMAND")
        widths = [
            max(len(headers[i]), *(len(row[i]) for row in rows))
            for i in range(6)
        ]
        print(
            "  ".join(headers[i].ljust(widths[i]) for i in range(6))
            + "  "
            + headers[6]
        )
        for row in rows:
            print("  ".join(row[i].ljust(widths[i]) for i in range(6)) + "  " + row[6])
            if row[7]:
                print(f"    note: {row[7]}")

    if roster.quota_groups:
        print("\nquota_groups:")
        for qid, qg in roster.quota_groups.items():
            check = f"  check={qg.check_cmd}" if qg.check_cmd else ""
            note = f"  — {qg.notes}" if qg.notes else ""
            print(f"  {qid}: {qg.status}{check}{note}")

    if roster.bans:
        print("\nbans:")
        for ban in roster.bans:
            reason = f"  — {ban.reason}" if ban.reason else ""
            print(f"  {ban.pattern} [{ban.mode}]{reason}")

    jr = roster.judge_rules
    if roster.schema_version >= 2:
        parts = [f"require_different_family={str(jr.require_different_family).lower()}"]
        if jr.default_per_phase:
            parts.append(f"default_per_phase={jr.default_per_phase}")
        if jr.milestone_role:
            parts.append(f"milestone_role={jr.milestone_role}")
        print("\njudge_rules: " + "  ".join(parts))
        for ej in jr.extra_judges:
            reason = f" — {ej.reason}" if ej.reason else ""
            print(f"  extra ({ej.author_family}): {ej.judge}{reason}")


def _roster_to_json(roster: Roster, role_filter: str | None = None) -> dict:
    roles_out: dict[str, dict[str, dict]] = {}
    for r_name, role_entries in roster.entries.items():
        if role_filter is not None and r_name != role_filter:
            continue
        roles_out[r_name] = {
            k_name: _entry_to_dict(r_name, k_name, ent)
            for k_name, ent in role_entries.items()
        }
    return {
        "schema_version": roster.schema_version,
        "updated": roster.updated,
        "note": roster.note,
        "roles": roles_out,
        "quota_groups": {qid: _quota_to_dict(qid, qg) for qid, qg in roster.quota_groups.items()},
        "bans": [_ban_to_dict(b) for b in roster.bans],
        "judge_rules": {
            "require_different_family": roster.judge_rules.require_different_family,
            "default_per_phase": roster.judge_rules.default_per_phase,
            "milestone_role": roster.judge_rules.milestone_role,
            "extra_judges": [
                {"author_family": ej.author_family, "judge": ej.judge, "reason": ej.reason}
                for ej in roster.judge_rules.extra_judges
            ],
        },
    }


def _load_for_mutation(path: Path) -> Roster:
    """Load `path` for a mutator, migrating flat v1 files so inline comments/bans/quotas are preserved."""
    loaded = load(path)
    if loaded.schema_version < 2:
        return migrate_roster(path)
    return loaded


def _cmd_set(args: list[str], default_path: Path) -> int:
    parser = argparse.ArgumentParser(
        prog="assist roster set",
        description="Add or update a structured roster entry (<role>.<key>).",
    )
    parser.add_argument("target", help="<role>.<key> entry to add or update")
    parser.add_argument("--cmd", dest="command", help="full launch command (required for new entries)")
    parser.add_argument("--family", help="independence family (claude|openai|gemini|...)")
    parser.add_argument("--transport", help="CLI transport (claude|codex|agy|cursor-agent)")
    parser.add_argument("--quota", dest="quota_group", help="quota_group id")
    parser.add_argument("--status", choices=_VALID_ENTRY_STATUS, help="entry status")
    parser.add_argument("--approval", choices=_VALID_APPROVAL, help="approval mode")
    parser.add_argument("--note", dest="notes", help="visible operational note")
    parser.add_argument("--source", help="provenance source (e.g. studio:decision:123)")
    parser.add_argument("--permissions", help='optional override (e.g. "restricted")')
    parser.add_argument("--allows-judging", action="append", default=None, help="author family this entry may judge")
    parser.add_argument("--paired-with", action="append", default=None, help="paired judge <role>.<key>")
    parser.add_argument("--file", dest="roster_file", type=Path, default=None, help="roster file override")
    ns = parser.parse_args(args)

    path = ns.roster_file or default_path
    role_name, key_name = _parse_role_key(ns.target)
    if ns.family is not None and not ns.family.strip():
        _error("--family must be a non-empty string")
    if ns.command is not None:
        if not ns.command.strip():
            _error("--cmd must be a non-empty string")
        try:
            shlex.split(ns.command)
        except ValueError as exc:
            _error(f"invalid shell quoting in --cmd: {exc}")

    loaded = _load_for_mutation(path)
    existing = (loaded.entries.get(role_name) or {}).get(key_name)
    cmd = ns.command if ns.command is not None else (existing.command if existing else None)
    if not cmd or not cmd.strip():
        _error("--cmd is required when creating a new roster entry")

    hard_bans = [b for b in match_bans(loaded, cmd) if b.mode == "hard"]
    if hard_bans:
        first = hard_bans[0]
        _error(f"refused by hard ban {first.pattern!r}: {first.reason}")

    transport = (
        ns.transport.strip()
        if ns.transport is not None
        else (existing.transport if existing and ns.command is None else _infer_transport(cmd))
    )
    family = (
        ns.family.strip()
        if ns.family is not None
        else (existing.family if existing and ns.command is None else _infer_family(key_name, transport, cmd))
    )
    quota_group = (
        ns.quota_group.strip()
        if ns.quota_group is not None
        else (existing.quota_group if existing else _infer_quota_group(transport, cmd))
    )
    today = date.today().isoformat()
    entry = RosterEntry(
        command=cmd,
        family=family,
        transport=transport,
        quota_group=quota_group,
        status=ns.status or (existing.status if existing else "default"),
        approval=ns.approval or (existing.approval if existing else "none"),
        notes=ns.notes if ns.notes is not None else (existing.notes if existing else ""),
        updated=today,
        source=ns.source if ns.source is not None else (existing.source if existing else ""),
        allows_judging_families=(
            tuple(ns.allows_judging)
            if ns.allows_judging is not None
            else (existing.allows_judging_families if existing else ())
        ),
        paired_with=(
            tuple(ns.paired_with)
            if ns.paired_with is not None
            else (existing.paired_with if existing else ())
        ),
        permissions=(
            ns.permissions
            if ns.permissions is not None
            else (existing.permissions if existing else "")
        ),
    )

    new_entries = {r: dict(ents) for r, ents in loaded.entries.items()}
    new_entries.setdefault(role_name, {})[key_name] = entry
    updated_roster = _with_updated_entries(loaded, new_entries, updated=today)
    bak = write_atomic(path, updated_roster)
    print(f"updated {role_name}.{key_name} in {path}" + (f" (backup: {bak})" if bak else ""))
    return 0


def _cmd_note(args: list[str], default_path: Path) -> int:
    parser = argparse.ArgumentParser(
        prog="assist roster note",
        description="Update the visible note on an existing roster entry (<role>.<key>).",
    )
    parser.add_argument("target", help="<role>.<key>")
    parser.add_argument("text", help="note text")
    parser.add_argument("--source", help="provenance source")
    parser.add_argument("--file", dest="roster_file", type=Path, default=None, help="roster file override")
    ns = parser.parse_args(args)

    path = ns.roster_file or default_path
    role_name, key_name = _parse_role_key(ns.target)
    loaded = _load_for_mutation(path)
    existing = (loaded.entries.get(role_name) or {}).get(key_name)
    if existing is None:
        _error(f"unknown roster entry {role_name}.{key_name}")

    today = date.today().isoformat()
    entry = RosterEntry(
        command=existing.command,
        family=existing.family,
        transport=existing.transport,
        quota_group=existing.quota_group,
        status=existing.status,
        approval=existing.approval,
        notes=ns.text,
        updated=today,
        source=ns.source if ns.source is not None else existing.source,
        allows_judging_families=existing.allows_judging_families,
        paired_with=existing.paired_with,
        permissions=existing.permissions,
    )
    new_entries = {r: dict(ents) for r, ents in loaded.entries.items()}
    new_entries[role_name][key_name] = entry
    bak = write_atomic(path, _with_updated_entries(loaded, new_entries, updated=today))
    print(f"updated note for {role_name}.{key_name}" + (f" (backup: {bak})" if bak else ""))
    return 0


def _cmd_quota(args: list[str], default_path: Path) -> int:
    parser = argparse.ArgumentParser(
        prog="assist roster quota",
        description="Create or update a [quota_groups.<id>] record.",
    )
    parser.add_argument("group", help="quota_group id")
    parser.add_argument("--status", choices=_VALID_QUOTA_STATUS, help="ok | low | exhausted")
    parser.add_argument("--check-cmd", dest="check_cmd", help="command to check quota (e.g. /quota, /status)")
    parser.add_argument("--note", dest="notes", help="status note")
    parser.add_argument("--source", help="provenance source")
    parser.add_argument("--file", dest="roster_file", type=Path, default=None, help="roster file override")
    ns = parser.parse_args(args)

    if not _KEY.fullmatch(ns.group):
        _error(f"invalid quota group id {ns.group!r}")
    if ns.status is None and ns.check_cmd is None and ns.notes is None and ns.source is None:
        _error("specify at least one of --status, --check-cmd, --note, or --source")

    path = ns.roster_file or default_path
    loaded = _load_for_mutation(path)
    existing = loaded.quota_groups.get(ns.group)
    today = date.today().isoformat()
    updated_qg = QuotaGroup(
        status=ns.status or (existing.status if existing else "ok"),
        check_cmd=(
            ns.check_cmd
            if ns.check_cmd is not None
            else (existing.check_cmd if existing else _default_check_cmd(ns.group))
        ),
        notes=ns.notes if ns.notes is not None else (existing.notes if existing else ""),
        updated=today,
        source=ns.source if ns.source is not None else (existing.source if existing else ""),
    )
    new_quotas = dict(loaded.quota_groups)
    new_quotas[ns.group] = updated_qg
    bak = write_atomic(
        path,
        _with_updated_entries(loaded, loaded.entries, quota_groups=new_quotas, updated=today),
    )
    print(f"updated quota_group {ns.group} ({updated_qg.status})" + (f" (backup: {bak})" if bak else ""))
    return 0


def _cmd_ban(args: list[str], default_path: Path) -> int:
    parser = argparse.ArgumentParser(
        prog="assist roster ban",
        description="Add, update, or remove a [[bans]] rule.",
    )
    parser.add_argument("pattern", help="glob pattern to ban (e.g. *luna*, *haiku*)")
    parser.add_argument("--mode", choices=_VALID_BAN_MODE, help="hard | ask_daniel")
    parser.add_argument("--reason", help="reason for the ban")
    parser.add_argument("--source", help="provenance source")
    parser.add_argument("--rm", dest="remove", action="store_true", help="remove matching ban pattern")
    parser.add_argument("--file", dest="roster_file", type=Path, default=None, help="roster file override")
    ns = parser.parse_args(args)

    pattern = (ns.pattern or "").strip()
    if not pattern:
        _error("ban pattern must be a non-empty string")

    path = ns.roster_file or default_path
    loaded = _load_for_mutation(path)
    today = date.today().isoformat()
    bans = list(loaded.bans)

    if ns.remove:
        remaining = [b for b in bans if b.pattern != pattern]
        if len(remaining) == len(bans):
            _error(f"no ban rule found for pattern {pattern!r}")
        bak = write_atomic(
            path,
            _with_updated_entries(loaded, loaded.entries, bans=tuple(remaining), updated=today),
        )
        print(f"removed ban {pattern!r}" + (f" (backup: {bak})" if bak else ""))
        return 0

    reason = (ns.reason or "").strip()
    if not ns.mode or not reason:
        _error("--mode and non-empty --reason are required (or pass --rm to remove a ban)")

    new_rule = BanRule(
        pattern=pattern,
        mode=ns.mode,
        reason=reason,
        updated=today,
        source=ns.source or "",
    )
    replaced = False
    for idx, existing in enumerate(bans):
        if existing.pattern == new_rule.pattern:
            bans[idx] = new_rule
            replaced = True
            break
    if not replaced:
        bans.append(new_rule)

    bak = write_atomic(
        path,
        _with_updated_entries(loaded, loaded.entries, bans=tuple(bans), updated=today),
    )
    print(f"set ban {new_rule.pattern!r} ({new_rule.mode})" + (f" (backup: {bak})" if bak else ""))
    return 0


def _cmd_rm(args: list[str], default_path: Path) -> int:
    parser = argparse.ArgumentParser(
        prog="assist roster rm",
        description="Remove an entry (<role>.<key>) from the roster.",
    )
    parser.add_argument("target", help="<role>.<key>")
    parser.add_argument("--file", dest="roster_file", type=Path, default=None, help="roster file override")
    ns = parser.parse_args(args)

    path = ns.roster_file or default_path
    role_name, key_name = _parse_role_key(ns.target)
    loaded = _load_for_mutation(path)
    if key_name not in (loaded.entries.get(role_name) or {}):
        _error(f"unknown roster entry {role_name}.{key_name}")

    new_entries = {r: dict(ents) for r, ents in loaded.entries.items()}
    del new_entries[role_name][key_name]
    if not new_entries[role_name]:
        del new_entries[role_name]
    bak = write_atomic(path, _with_updated_entries(loaded, new_entries))
    print(f"removed {role_name}.{key_name}" + (f" (backup: {bak})" if bak else ""))
    return 0


def _cmd_show(args: list[str], default_path: Path) -> int:
    parser = argparse.ArgumentParser(
        prog="assist roster show",
        description="Inspect one roster entry (<role>.<key>) with quota, bans, and judge rules.",
    )
    parser.add_argument("target", help="<role>.<key>")
    parser.add_argument("--json", dest="json_output", action="store_true", help="emit JSON")
    parser.add_argument("--file", dest="roster_file", type=Path, default=None, help="roster file override")
    ns = parser.parse_args(args)

    path = ns.roster_file or default_path
    role_name, key_name = _parse_role_key(ns.target)
    loaded = load(path)
    entry = (loaded.entries.get(role_name) or {}).get(key_name)
    if entry is None:
        _error(f"unknown roster entry {role_name}.{key_name}")

    qg = loaded.quota_groups.get(entry.quota_group)
    payload = {
        "role": role_name,
        "key": key_name,
        "entry": _entry_to_dict(role_name, key_name, entry),
        "quota": _quota_to_dict(entry.quota_group, qg) if qg else None,
        "bans_matched": [_ban_to_dict(b) for b in match_bans(loaded, entry.command)],
        "judge_rule": judge_rule_for(loaded, entry.family),
    }
    if ns.json_output:
        print(json.dumps(payload, indent=2))
        return 0
    for field_name, value in payload["entry"].items():
        if value not in ("", [], None):
            print(f"{field_name}: {value}")
    if qg:
        print(f"quota_status: {qg.status} (check: {qg.check_cmd or '-'})")
    jr = payload["judge_rule"]
    print("eligible_judge_families: " + (", ".join(jr["eligible_judge_families"]) or "(none)"))
    for ej in jr["extra_judges"]:
        print(f"extra_judge: {ej['ref']} — {ej.get('reason', '')}")
    return 0


def _cmd_doctor(args: list[str], default_path: Path) -> int:
    parser = argparse.ArgumentParser(
        prog="assist roster doctor",
        description="Validate roster schema, permission flags, bans, quota groups, and judge rules.",
    )
    parser.add_argument("--json", dest="json_output", action="store_true", help="emit JSON report")
    parser.add_argument("--file", dest="roster_file", type=Path, default=None, help="roster file override")
    ns = parser.parse_args(args)

    path = ns.roster_file or default_path
    loaded = load(path)
    report = run_doctor(loaded)
    if ns.json_output:
        print(json.dumps(report, indent=2))
        return 0 if report["ok"] else 1

    print(
        f"roster doctor ({path}): schema v{report['schema_version']}, "
        f"{report['entries']} entries, {report['quota_groups']} quota groups, {report['bans']} bans"
    )
    for err in report["errors"]:
        print(f"  ERROR: {err}")
    for warn in report["warnings"]:
        print(f"  WARN:  {warn}")
    if report["ok"]:
        print("OK")
        return 0
    return 1


def _cmd_migrate(args: list[str], default_path: Path) -> int:
    parser = argparse.ArgumentParser(
        prog="assist roster migrate",
        description="Convert a flat roster.toml into the structured v2 schema.",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="print the converted TOML to stdout without writing")
    mode.add_argument("--write", dest="write_path", type=Path, metavar="PATH", help="atomically write converted TOML to PATH")
    parser.add_argument("--from", dest="src_file", type=Path, default=None, help="source flat roster file")
    parser.add_argument("--file", dest="alt_src_file", type=Path, default=None, help="alias for --from")
    ns = parser.parse_args(args)

    src = ns.src_file or ns.alt_src_file or default_path
    migrated = migrate_roster(src)
    rendered = render_toml(migrated)
    if ns.dry_run:
        sys.stdout.write(rendered)
        return 0
    bak = write_atomic(ns.write_path, migrated)
    print(f"migrated {src} -> {ns.write_path}" + (f" (backup: {bak})" if bak else ""))
    return 0


def cli_main(args: list[str], default_path: Path) -> int:
    """Entry point for `assist roster ...` (read queries + mutator subcommands)."""
    if args and args[0] in _SUBCOMMANDS:
        sub, rest = args[0], args[1:]
        dispatch = {
            "set": _cmd_set,
            "note": _cmd_note,
            "quota": _cmd_quota,
            "ban": _cmd_ban,
            "rm": _cmd_rm,
            "show": _cmd_show,
            "doctor": _cmd_doctor,
            "migrate": _cmd_migrate,
        }
        return dispatch[sub](rest, default_path)

    parser = argparse.ArgumentParser(
        prog="assist roster",
        description=(
            "List configured role commands, query a role/family with judge rules, "
            "or manage the roster via set/note/quota/ban/rm/show/doctor/migrate."
        ),
    )
    parser.add_argument("positional_role", nargs="?", metavar="ROLE", help="role whose command to print")
    parser.add_argument("--role", dest="opt_role", metavar="R", help="filter or query role R")
    parser.add_argument("--family", metavar="F", help="family to select (requires ROLE/--role)")
    parser.add_argument("--key", metavar="K", help="specific entry key within a role")
    parser.add_argument("--against", metavar="F", help="list/select competing judge family against author family F")
    parser.add_argument("--json", dest="json_output", action="store_true", help="emit structured JSON")
    parser.add_argument("--file", dest="roster_file", type=Path, default=None, help="roster file override")
    ns = parser.parse_args(args)

    if ns.positional_role is not None and ns.opt_role is not None:
        _error("specify ROLE positionally or via --role, not both")
    role = ns.opt_role if ns.opt_role is not None else ns.positional_role
    if (ns.family is not None or ns.key is not None or ns.against is not None) and role is None:
        _error("--family, --key, and --against require ROLE or --role")

    path = ns.roster_file or default_path
    loaded = load(path)

    # Query for one specific role + selector (or JSON query)
    if ns.json_output:
        if role is not None and ns.against is not None and ns.family is None and ns.key is None:
            comps = list_competitors(loaded, role, ns.against)
            payload = {
                "role": role,
                "against": _canonical_family(ns.against),
                "competitors": [_entry_to_dict(role, k, ent) for k, ent in comps],
                "judge_rule": judge_rule_for(loaded, ns.against),
            }
            print(json.dumps(payload, indent=2))
            return 0
        if role is not None and (ns.family is not None or ns.key is not None or ns.against is not None):
            k_name, entry = resolve_entry(
                loaded, role, family=ns.family, against=ns.against, key=ns.key
            )
            qg = loaded.quota_groups.get(entry.quota_group)
            author_fam = ns.against if ns.against is not None else entry.family
            payload = {
                "role": role,
                "family": entry.family,
                "key": k_name,
                "entry": _entry_to_dict(role, k_name, entry),
                "quota": _quota_to_dict(entry.quota_group, qg) if qg else None,
                "bans_matched": [_ban_to_dict(b) for b in match_bans(loaded, entry.command)],
                "judge_rule": judge_rule_for(loaded, author_fam),
            }
            print(json.dumps(payload, indent=2))
            return 0
        if role is not None and role not in loaded.entries:
            valid = ", ".join(sorted(loaded.roles)) or "(none)"
            _error(f"unknown role {role!r}; valid roles: {valid}")
        print(json.dumps(_roster_to_json(loaded, role_filter=role), indent=2))
        return 0

    # Backward-compatible positional `assist roster <role> [--family F|--against F]` -> command only
    if ns.positional_role is not None:
        print(
            resolve_command(
                loaded, ns.positional_role, family=ns.family, against=ns.against, key=ns.key
            )
        )
        return 0

    if ns.opt_role is not None:
        if ns.opt_role not in loaded.entries:
            valid = ", ".join(sorted(loaded.roles)) or "(none)"
            _error(f"unknown role {ns.opt_role!r}; valid roles: {valid}")
        if ns.against is not None and ns.family is None and ns.key is None:
            comps = list_competitors(loaded, ns.opt_role, ns.against)
            print(f"competitors in {ns.opt_role} against {_canonical_family(ns.against)}:")
            for k_name, ent in comps:
                print(f"  {ns.opt_role}.{k_name} ({ent.family}): {ent.command}")
            jr = judge_rule_for(loaded, ns.against)
            for ej in jr["extra_judges"]:
                print(f"  extra_judge: {ej['ref']} — {ej.get('reason', '')}")
            return 0
        if ns.family is not None or ns.key is not None or ns.against is not None:
            k_name, entry = resolve_entry(
                loaded, ns.opt_role, family=ns.family, against=ns.against, key=ns.key
            )
            print(f"{ns.opt_role}.{k_name}: {entry.command}")
            print(f"family={entry.family}  transport={entry.transport}  perms={_permission_label(entry)}  quota={entry.quota_group or '-'}")
            if entry.notes:
                print(f"note: {entry.notes}")
            jr = judge_rule_for(loaded, ns.against or entry.family)
            print("judges: " + (", ".join(jr["eligible_judge_families"]) or "(none)"))
            for ej in jr["extra_judges"]:
                print(f"extra_judge: {ej['ref']} — {ej.get('reason', '')}")
            return 0
        _print_table(loaded, role_filter=ns.opt_role)
        return 0

    _print_table(loaded)
    return 0


def command(
    path: Path,
    role: str | None = None,
    family: str | None = None,
) -> int:
    """Backward-compatible wrapper kept for existing callers and tests."""
    args: list[str] = []
    if role is not None:
        args.append(role)
    if family is not None:
        args.extend(["--family", family])
    return cli_main(args, path)
