# Drift Assist — details

Material moved out of the README's first screens: how the OpenCode reader
works, the launch-provenance store, the temporary execution park, and the host
CLI proxy.

## Platform notes

Linux is the primary deployment platform. Interactive tmux input and launch
provenance read process identity through Linux `/proc` or macOS `libproc`;
macOS clipboard helpers use `pbcopy`/`pbpaste`. The v16 activation/drain tooling
still requires Linux `/proc`.

The receipted park activation controller is `assist activate-park-v16 [--resume]`.
It is left out of `assist help`.

## OpenCode Output reader

OpenCode panes also offer **Output**: choose the conversation shown in the pane
to read wrapped messages, reasoning and tool details with normal browser scrolling.
**Latest** follows new output; **Terminal** returns to the interactive TUI, and
detected prompts return there automatically. Input always goes to the pane, so
choose again in Output after switching conversations inside OpenCode.

In OpenCode's **Terminal** view, drag in either direction to pan a capture
larger than the phone. Swipe at its top or bottom edge, or use **▲ / ▼**, to
page through the app's transcript. The TUI stays live while you read;
scrolling up holds your position, and **Latest** returns to the newest output.

The reader uses the host's `opencode session list` and `opencode export --pure`
(verified with 1.18.18), refreshing snapshots while visible. It needs no plugin
or listening OpenCode server. Selection lasts for the browser's current pane
generation. Remote/custom data stores use Terminal; a local `attach` must share
the host's OpenCode store. The picker searches 200 recent sessions in the pane's
exact folder; message/detail limits are disclosed in the reader.

The OpenCode reader uses the first executable file found in this order:
`ASSIST_OPENCODE_BIN`, `opencode` on the server's `PATH`,
`~/.local/bin/opencode`, `~/.opencode/bin/opencode`,
`/opt/homebrew/bin/opencode`, then `/usr/local/bin/opencode`.
Set `ASSIST_OPENCODE_BIN` to an absolute path for a custom installation;
`~/` is also accepted. Missing paths, directories and non-executable files
are skipped. These fallbacks work when a service has a minimal `PATH`.


## Launch provenance

On an ordinary startup, Assist automatically creates a missing
`.assist-launch-provenance-v1/` store using the same empty-epoch operation as
the explicit initializer and writes
`.assist-launch-provenance-v1-initialization.json` beside it. Startup never
re-initializes an existing path: an existing but invalid store still fails
closed. The park-handoff startup path does not run this initializer.


## Temporary execution park

Assist refuses exactly two execution intents:

- The configured host CLI proxy (`configured_cli_proxy`, `/api/cli-proxy`).
- Configured container image builds (`configured_image_build`). No route
  reaches it any more: Automate and Container, which the park used to hold,
  were retired (effort 621), and `/api/automate/*` and `/api/container/*`
  answer 404.

Saved commands, `/api/git/run`, project-venv creation, `/api/restart`, run-init,
launch or duplicate with an init command, and the native folder picker remain
available. There is no un-park API or CLI verb; changing the park phase does not
enable a denied intent. A refused CLI-proxy call returns HTTP 409 with this exact
body (the `intent` value identifies the refused operation):

```json
{
  "ok": false,
  "error": "container_launch_parked",
  "reason": "Container launch automation is temporarily parked while host wiring migrates.",
  "intent": "configured_cli_proxy"
}
```

## Host CLI proxy

`/api/cli-proxy` runs one host-side CLI tool for a caller that cannot hold the
token. Its callers were containers launched by Automate, on an isolated network
that could reach the host on port 8089; Automate and its container tooling were
retired, so nothing in Assist calls it today.

The proxy is in the execution park: every request returns the 409 body
documented above, with `"intent":"configured_cli_proxy"`, before a host
subprocess starts. The configuration and inner allowlist gates below remain in
place; there is no runtime un-park verb.

Host-side configuration (`.env`): `ASSIST_CLI_BIN`, `ASSIST_CLI_DIR`,
`ASSIST_CLI_ALLOWED`. Behind the park, the endpoint runs `ASSIST_CLI_BIN <args>`
on the host and returns stdout/stderr/exit code. `ASSIST_CLI_ALLOWED`
(comma-separated) restricts which first-arg subcommands are accepted; **leaving
it empty disables the proxy (403)**.

```bash
# .env
ASSIST_CLI_BIN=/opt/mycli/bin/mycli
ASSIST_CLI_DIR=/srv/mycli
ASSIST_CLI_ALLOWED=status,build,deploy
```

A request may carry base64 files; the host writes them to a temp dir and
replaces each `__PROXY_FILE_<n>__` arg with the resolved path before invoking
the CLI. The
temp dir is removed whether or not the call succeeds.

A `--timeout N` in the forwarded args sets how long the host waits, plus 30s of slack. It must be a non-negative integer — anything else is a 400 rather than a silent fallback — and it is capped at 600s, so a proxied call cannot hold a host subprocess open indefinitely.

