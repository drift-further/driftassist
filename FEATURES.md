# Drift Assist features

Every feature in Drift Assist. Each entry says what it does and where to find it.
Install steps, configuration and the security posture are in [README.md](README.md)
and [SECURITY.md](SECURITY.md).

## Agents working together

### Agents messaging agents
Agents running in Assist can work as a team. Through the `assist` CLI, one agent can
list the other sessions, read another agent's pane, and send it a message or a task.
`assist send` adds a line asking the receiving agent to report back to the sender's pane
when it is done or stuck, so the answer arrives as a message instead of the sender
checking. `assist wait` waits for a pane to settle and says whether it is idle, asking a
question or still working.
Where: `assist ls`, `assist view`, `assist send`, `assist wait`.

### The `assist` CLI
Every session control from a shell: list, view, send, wait, launch and kill sessions,
and set Auto-Yes. `assist launch --role` starts a session from your roster of agent
commands.
Where: `assist help`.

## Watch your sessions

### Live terminal panes
Each tmux pane streams to the browser as it changes, colours included. If the
WebSocket drops, the view falls back to polling until it reconnects.
Where: the main view.

### Split view
Two panes side by side. The selected side is outlined and takes everything you type.
Each side's tab has a menu for choosing its pane. On a phone, one pane shows at a time
and the A/B tabs switch between them.
Where: Actions → Split view.

### Session tabs
One tab per pane, with a running dot and idle time. Long-press a tab to pin, reorder,
rename, duplicate, fit or kill it. Idle tabs tuck into a zZ sheet, and the tab list
shows every tab at once.
Where: the tab strip under the top bar.

### Fit and text size
Fit resizes the tmux window to your screen. Smaller and Larger change the text size.
Where: Actions → Fit, Smaller, Larger.

### Search and line selection
Search the transcript of the current pane. Long-press a line to select it, drag to
extend, and copy the lines as plain text.
Where: Actions → Search; long-press in the terminal.

### Full-screen apps
Panes running a full-screen app have no browser scrollback, so ▲, ▼ and End page
through the app's transcript instead.
Where: the pane header, shown only for those panes.

### OpenCode output reader
Read an OpenCode conversation beside its terminal. It only reads; it never types into
the pane.
Where: Output, in the header of an OpenCode pane.

## Answer prompts

### One-tap answers
Permission prompts and numbered menus become buttons above the composer.
Where: above the composer, when a prompt is on screen.

### Auto-Yes
Answers permission prompts for a session after a countdown you can STOP. It is off
until you turn it on, and the delay is 0.1 to 30 seconds. A codex pane is never
answered while a model-downgrade menu is showing.
Where: Actions → Auto-Yes, or `assist autoyes`.

### All-sessions switch
Arms Auto-Yes for every agent pane — claude, codex, opencode, cursor and gemini —
including sessions started later. Plain shells stay manual, and any session can opt out.
Where: Settings → Auto-Yes → All Sessions, or `assist autoyes --global`.

### Prompt popups
A popup tells you when a pane you are not looking at asks a question, with a button to
open it. For a sudo password prompt, Send types the vault's `$sudo` secret. Popups can
hide themselves after a set time.
Where: they appear on their own; timing is in Settings → UI & Behavior.

## Type into a pane

### Composer
A multi-line box that types into the selected pane. Unsent text is saved per pane on
the server, so the phone and the desktop show the same draft. ↵ switches Enter between
sending and starting a new line.
Where: the box under the terminal.

### Prompt segments
Give a favorite a handle, then type `[handle]` to insert its text when you send.
History keeps the short form.
Where: Hist → Favs, then type the handle in the composer.

### Secret vault
Secrets are kept in this browser only. `[$handle]` in a message sends one: the value is
typed into the pane and never saved to history or drafts. 🔑 sends a stored secret in
one tap.
Where: Settings → Secret vault; 🔑 beside the composer.

### History and favorites
Every prompt and command you send is kept, each in its own list. Star one to keep it
as a favorite and send it again with a tap.
Where: Hist → Prompts, Favs, Cmds.

### File upload
Attach files or photos from the phone. They are saved on the host and sent as `@path`
references, so the agent can open them.
Where: File.

### Keys
Esc, Tab, arrows, Ctrl-C and the other keys a phone keyboard lacks. Any key builds a
Ctrl/Alt/Shift combination and sends it in one tap.
Where: Keys.

### Autocomplete
Type `@` for a file path or `/` for a skill, and pick from the list.
Where: the composer.

### Skills panel
Lists the current project's skills so you can insert one.
Where: Actions → /Skills.

## Projects and your machine

### Project launcher
Open a new session in any project folder, with an optional command to run first.
Recent projects are listed, and Browse opens a folder picker.
Where: Actions → Projects.

### Git panel
Git status and push for the pane's project. Commit & push shows the branch, upstream
and number of changed files, and asks you to confirm before it runs.
Where: Actions → Git.

### Project venv
A new pane can enter the project's Python virtual environment before anything runs.
It is off by default, and it only uses a venv that git does not track.
Where: Settings → Server Controls → Auto-Activate venv.

### Idle-session cleanup
Lists sessions with no new output for a set time. With `--yes` it kills them, checking
each one again first; `--keep` spares the ones you name.
Where: `assist purge --idle 24h`.

### Software updates
Checks the configured upstream and installs an update. Restarting is a separate step.
Where: Actions → Software updates, or `assist update`.

### Settings
Server, terminal, Auto-Yes, popup and Studio settings, edited in the browser.
Where: Actions → Settings.

## Studio

### Connect to Studio
Connect to a Studio to answer its questions from your phone, see an attention badge,
and see the project and effort for the active session. The token stays on the server.
Where: ◇ Studio.

## Access

### Sign-in
One shared secret, generated on first start. The browser signs in once and keeps a
cookie; scripts send the secret in a header.
Where: `/login`; `assist token` prints where the secret is kept.

### Phone pairing
`assist pair` prints the phone's URL and a QR code, and opens a short window where the
phone can join without typing the secret.
Where: `assist pair`.

### LAN access
`assist expose` adds a listener on your LAN address. Assist never listens on all
addresses or a public one.
Where: `assist expose`.

### Background service
Runs Assist as a systemd user service (Linux) or a launchd agent (macOS). Your tmux
sessions keep running when Assist restarts or stops.
Where: `assist service install`.
