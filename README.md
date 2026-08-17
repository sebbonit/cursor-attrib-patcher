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
python3 patch.py detect      # check whether Cursor still needs patching
python3 patch.py status      # alias of detect
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
| `detect` | Scan unpacked JS **and** `.asar` archives; print files that still need a patch and `Needs patching: YES/NO` |
| `status` / `scan` | Same scan, listing every matched file |
| `patch` (default) | Patch injectors, set CLI attribution flags off, re-sign the macOS app, restart CLIs |
| `restore` | Copy backed-up originals back and re-sign |
| `restart` | Stop running `cursor-agent` processes so they reload |

Exit codes for `detect`: `0` already patched, `2` needs patching, `1` nothing found.

## What it finds

Typical install locations, plus `cursor` / `cursor-agent` / `agent` on `PATH`:

- **macOS:** `/Applications/Cursor*.app`, `~/Applications`, `~/.local/share/cursor-agent`, IDE-bundled agent CLI
- **Linux:** `/opt/Cursor`, `/usr/share/cursor`, `~/.local/share/cursor-agent`
- **Windows:** `%LOCALAPPDATA%\Programs\cursor`, `%APPDATA%\Cursor`

Extra paths:

```bash
export CURSOR_ATTRIB_PATCHER_PATHS="/path/to/Cursor.app:/other/install"
python3 patch.py detect
```

## Packed files (asar)

Electron `.asar` files are **archives, not encryption**. Current Cursor macOS builds ship most JS unpacked; `node_modules.asar` is often a tiny stub pointing at the unpacked `node_modules` folder.

`detect` still scans every `.asar` under Cursor installs. If a live injector is inside an archive, `patch` blanks it in place with **same-length spaces** so the asar file-offset header stays valid. V8 snapshots and Chromium `.pak` files on this build do not contain the git/PR attribution strings.

## What it changes

Attribution **text is not deleted**. The payload strings are replaced with the same number of spaces, so:

- `git commit` would get a blank `--trailer` value instead of `Co-authored-by: Cursor <…>`
- PR bodies would get whitespace instead of `Made with [Cursor](https://cursor.com)`

The injector gates and `commitAttributionMessage` / `prAttributionMessage` flags are forced off as well (also same-length where possible). Short `includes("Co-authored-by: Cursor")` checks are left alone so already-attributed commands are still skipped.

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

## The "installation appears to be corrupt" warning

Cursor inherits VS Code's `IntegrityService`: at startup it SHA-256-hashes the files listed under `checksums` in `product.json` (`workbench.desktop.main.js` among them) and shows **"Your Cursor installation appears to be corrupt"** when a digest no longer matches. The check is advisory only — the IDE keeps working.

Since the patcher intentionally rewrites those files, it also refreshes the affected entries in `product.json` so the check stays green. `detect` reports stale digests (`[NEED] checksums …`) if the files were patched but `product.json` was not updated, which happens for installs patched by older versions of this tool.

## After a Cursor update

Updates overwrite the JS. Run `python3 patch.py detect`, then `python3 patch.py` if it says `Needs patching: YES`.

## Restore

```bash
python3 patch.py restore
```

## Limitations

- Local IDE and CLI only. Cloud/background agents still attribute on Cursor's servers.
- A new Cursor build can move or rename the injector. `python3 patch.py detect` shows whether it still matches.
- This modifies files inside your Cursor install. Use at your own risk.

## License

MIT
