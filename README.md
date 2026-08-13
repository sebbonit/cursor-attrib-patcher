# Cursor Attribution Patcher

Strip Cursor's injected git and PR attribution from **local** IDE and CLI installs.

Cursor rewrites `git commit` and `gh pr create` after the model runs, adding:

```text
Co-authored-by: Cursor <cursoragent@cursor.com>
```

```text
Made with [Cursor](https://cursor.com)
```

This patcher finds those injectors on your machine and disables them. It does **not** modify git repositories. Cloud / background agents cannot be patched.

**Python 3.10+.** No extra packages. macOS, Linux, and Windows.

## Install

```bash
git clone https://github.com/sebbonit/cursor-attrib-patcher.git
cd cursor-attrib-patcher
```

## Usage

```bash
python3 patch.py status      # show detected installs and patch state
python3 patch.py --dry-run   # show what would change
python3 patch.py             # patch everything found, then restart CLIs
python3 patch.py restore     # restore originals from backups
python3 patch.py restart     # only restart running cursor-agent processes
python3 patch.py --no-restart
```

On macOS you can also double-click `patch.command`.

After patching, fully quit and reopen the Cursor IDE so it loads the patched JS. CLI processes are restarted automatically.

### Commands

| Command | What it does |
|---|---|
| `status` | Detect Cursor IDE/CLI files and report `LIVE` vs `patched` |
| `patch` (default) | Patch injectors, set CLI attribution flags off, re-sign the macOS app, restart CLIs |
| `restore` | Copy backed-up originals back and re-sign |
| `restart` | Stop running `cursor-agent` processes so they reload |

## What it finds

Typical install locations, plus `cursor` / `cursor-agent` / `agent` on `PATH`:

- **macOS:** `/Applications/Cursor*.app`, `~/Applications`, `~/.local/share/cursor-agent`, IDE-bundled agent CLI
- **Linux:** `/opt/Cursor`, `/usr/share/cursor`, `~/.local/share/cursor-agent`
- **Windows:** `%LOCALAPPDATA%\Programs\cursor`, `%APPDATA%\Cursor`

Extra paths:

```bash
export CURSOR_ATTRIB_PATCHER_PATHS="/path/to/Cursor.app:/other/install"
python3 patch.py
```

## What it changes

In Cursor's own JavaScript it:

- Clears the `git commit --trailer` insert
- Clears the `Made with Cursor` PR footer insert
- Forces the co-author / PR-footer gates off
- Stops sending `commitAttributionMessage: "enabled"`
- Defaults missing attribution config to off

It also sets `~/.cursor/cli-config.json`:

```json
"attribution": {
  "attributeCommitsToAgent": false,
  "attributePRsToAgent": false
}
```

Backups are stored in `~/.cursor-attrib-patcher/backups/`.

## Restart behavior

After a successful patch the tool stops running `cursor-agent` processes so they pick up the patched files. ACP hosts (Synara, etc.) and IDE workers are expected to spawn a fresh process. The Cursor IDE app itself is not quit.

Use `--no-restart` if you do not want that.

## macOS notes

Patching `/Applications/Cursor.app` needs **App Management** permission:

**System Settings → Privacy & Security → App Management** → allow Terminal (or Python).

The app is re-signed ad-hoc after editing. Gatekeeper may warn that Cursor was modified. That is expected.

If a Cursor/agent session cannot write into the app bundle, run the same command in **Terminal.app**.

## After a Cursor update

Updates overwrite the JS. Run `python3 patch.py` again.

## Restore

```bash
python3 patch.py restore
```

## Limitations

- Local IDE and CLI only. Cloud/background agents still attribute on Cursor's servers.
- A new Cursor build can move or rename the injector. `python3 patch.py status` shows whether it still matches.
- This modifies files inside your Cursor install. Use at your own risk.

## License

MIT
