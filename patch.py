#!/usr/bin/env python3
"""Strip Cursor's git/PR attribution injectors from local IDE and CLI installs."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

MARKER = "cursor-attrib-patcher"
BACKUP_ROOT = Path.home() / ".cursor-attrib-patcher" / "backups"
MANIFEST_PATH = Path.home() / ".cursor-attrib-patcher" / "manifest.json"

TRAILER_EMAIL = "cursoragent@cursor.com"
TRAILER_LINE = "Co-authored-by: Cursor <cursoragent@cursor.com>"
PR_FOOTER = "Made with [Cursor](https://cursor.com)"

PatchReplacement = str | Callable[[re.Match[str]], str]


def quoted_spaces(match: re.Match[str]) -> str:
    """Replace a quoted/template literal with same-length spaces. Keeps JS valid and asar offsets intact."""
    s = match.group(0)
    if len(s) < 2:
        return s
    return s[0] + (" " * (len(s) - 2)) + s[-1]


def same_length_spaces(match: re.Match[str]) -> str:
    return " " * len(match.group(0))


def same_length_false_if(match: re.Match[str]) -> str:
    """Turn `if(t?.enable…)` into `if(false …)` without changing file length."""
    s = match.group(0)
    inner = "false"
    pad = len(s) - len("if(") - len(inner) - len(")")
    if pad < 0:
        return s
    return "if(" + inner + (" " * pad) + ")"


def zero_ternary_flag(match: re.Match[str]) -> str:
    """`flag:h?"enabled":void 0` → `flag:0?"enabled":void 0` (same length)."""
    s = match.group(0)
    return re.sub(r":(\w+)\?", lambda m: ":" + "0" + (" " * max(0, len(m.group(1)) - 1)) + "?", s, count=1)


# Unique literals / stable minified shapes. Variable names change between builds.
# Attribution payloads are blanked with same-length spaces (not deleted) so git/PR
# get whitespace instead of Cursor text, and packed asar offsets stay valid.
PATCHES: list[tuple[str, re.Pattern[str], PatchReplacement]] = [
    (
        "git-trailer-const",
        re.compile(r'"Co-authored-by: Cursor <cursoragent@cursor\.com>"'),
        quoted_spaces,
    ),
    (
        "git-trailer-insert",
        re.compile(r'` --trailer "\$\{[A-Za-z_$][\w$]*\}"`'),
        quoted_spaces,
    ),
    (
        "pr-footer-insert",
        re.compile(r'"\\n\\nMade with \[Cursor\]\(https://cursor\.com\)"'),
        quoted_spaces,
    ),
    (
        "pr-footer-text",
        re.compile(r'"Made with \[Cursor\]\(https://cursor\.com\)"'),
        quoted_spaces,
    ),
    (
        "legacy-made-with-trailer",
        re.compile(r' --trailer "Made-with: Cursor"'),
        same_length_spaces,
    ),
    (
        "disable-coauthor-gate",
        re.compile(r"if\(\w+\?\.enableCoAuthoredByTrailer\)"),
        same_length_false_if,
    ),
    (
        "disable-pr-footer-gate",
        re.compile(r"if\(\w+\?\.enablePRGeneratedByFooter\)"),
        same_length_false_if,
    ),
    (
        "commit-attribution-flag",
        re.compile(r"commitAttributionMessage:\w+\?\"enabled\":void 0"),
        zero_ternary_flag,
    ),
    (
        "pr-attribution-flag",
        re.compile(r"prAttributionMessage:\w+\?\"enabled\":void 0"),
        zero_ternary_flag,
    ),
    (
        "default-commit-attr-off",
        re.compile(r"(\.attribution\?\.attributeCommitsToAgent\?\?!)0"),
        r"\g<1>1",
    ),
    (
        "default-pr-attr-off",
        re.compile(r"(\.attribution\?\.attributePRsToAgent\?\?!)0"),
        r"\g<1>1",
    ),
]

# Same-length blanks applied to asar bytes (archive, not encryption).
ASAR_BLANK_NEEDLES: tuple[tuple[str, bytes], ...] = (
    ("git-trailer-const", TRAILER_LINE.encode("utf-8")),
    ("pr-footer-text", PR_FOOTER.encode("utf-8")),
)

ASAR_RELATIVE = (
    "Contents/Resources/app.asar",
    "Contents/Resources/app/node_modules.asar",
    "resources/app.asar",
    "resources/app/node_modules.asar",
    "app.asar",
    "node_modules.asar",
)

SCAN_MARKERS = (
    TRAILER_EMAIL.encode(),
    b"enableCoAuthoredByTrailer",
    b"commitAttributionMessage",
    b"Made-with: Cursor",
    b"Made with [Cursor]",
    b"attributeCommitsToAgent",
)

IDE_RELATIVE_JS = [
    "Contents/Resources/app/extensions/cursor-agent-exec/dist",
    "Contents/Resources/app/extensions/cursor-agent-host/dist",
    "Contents/Resources/app/extensions/cursor-local-agent-runtime/dist",
    "Contents/Resources/app/out/vs/workbench",
    "resources/app/extensions/cursor-agent-exec/dist",
    "resources/app/extensions/cursor-agent-host/dist",
    "resources/app/extensions/cursor-local-agent-runtime/dist",
    "resources/app/out/vs/workbench",
]


def home() -> Path:
    return Path.home()


def candidate_roots() -> list[Path]:
    h = home()
    roots: list[Path] = []

    if sys.platform == "darwin":
        roots += [
            Path("/Applications"),
            h / "Applications",
            h / "Library/Application Support/Cursor",
            h / "Library/Application Support/Cursor Nightly",
        ]
    elif sys.platform == "win32":
        local = Path(os.environ.get("LOCALAPPDATA", h / "AppData/Local"))
        roaming = Path(os.environ.get("APPDATA", h / "AppData/Roaming"))
        program_files = Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"))
        program_files_x86 = Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"))
        roots += [
            local / "Programs" / "cursor",
            local / "Programs" / "Cursor",
            local / "cursor",
            local / "Cursor",
            roaming / "Cursor",
            roaming / "Cursor Nightly",
            program_files / "Cursor",
            program_files_x86 / "Cursor",
        ]
    else:
        roots += [
            Path("/opt/Cursor"),
            Path("/opt/cursor"),
            Path("/usr/share/cursor"),
            Path("/usr/lib/cursor"),
            Path("/usr/local/share/cursor"),
            h / ".local/share/cursor",
            h / ".config/Cursor",
            h / ".config/cursor",
            Path("/var/lib/flatpak/app/anysphere.cursor"),
            h / ".local/share/flatpak/app/anysphere.cursor",
        ]

    roots += [
        h / ".local/share/cursor-agent",
        h / ".local/bin",
    ]

    extra = os.environ.get("CURSOR_ATTRIB_PATCHER_PATHS", "")
    for item in extra.split(os.pathsep):
        if item.strip():
            roots.append(Path(item.strip()).expanduser())

    # Follow PATH binaries (cursor, cursor-agent, agent).
    for name in ("cursor", "cursor-agent", "agent"):
        found = shutil.which(name)
        if not found:
            continue
        path = Path(found).resolve()
        roots.append(path.parent)
        # ~/.local/share/cursor-agent/versions/<ver>/cursor-agent
        if path.parent.name not in {".bin", "bin", ".local"}:
            roots.append(path.parent)
            roots.append(path.parent.parent)

    return dedupe_existing(roots)


def dedupe_existing(paths: list[Path]) -> list[Path]:
    seen: set[str] = set()
    out: list[Path] = []
    for path in paths:
        try:
            resolved = path.expanduser()
            if not resolved.exists():
                continue
            key = str(resolved.resolve()) if resolved.is_dir() or resolved.is_file() else str(resolved)
        except OSError:
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append(resolved)
    return out


def js_in_dir(directory: Path, names: tuple[str, ...] | None = None) -> list[Path]:
    if not directory.is_dir():
        return []
    files: list[Path] = []
    try:
        for entry in directory.iterdir():
            if not entry.is_file() or entry.suffix != ".js":
                continue
            if entry.stat().st_size >= 80_000_000:
                continue
            if names and entry.name not in names:
                continue
            files.append(entry)
    except OSError:
        return []
    return files


def collect_ide_js(install: Path) -> list[Path]:
    files: list[Path] = []
    bases = [install]
    if install.is_dir() and install.name in {"Applications"}:
        try:
            bases = [
                child
                for child in install.iterdir()
                if child.suffix == ".app" and "ursor" in child.name
            ]
        except OSError:
            return []
    for base in bases:
        for rel in IDE_RELATIVE_JS:
            folder = base / rel
            if "workbench" in rel:
                files.extend(
                    js_in_dir(folder, ("workbench.desktop.main.js", "workbench.glass.main.js"))
                )
            else:
                files.extend(js_in_dir(folder))
    return files


def collect_cli_js(root: Path, seen: set[str] | None = None) -> list[Path]:
    files: list[Path] = []
    if seen is None:
        seen = set()
    try:
        key = str(root.resolve())
    except OSError:
        return []
    if key in seen:
        return []
    seen.add(key)

    if root.is_file():
        root = root.parent
        try:
            key = str(root.resolve())
        except OSError:
            return []
        if key in seen:
            return []
        seen.add(key)

    if root.name == "bin" or root.as_posix().endswith("/.local/bin"):
        for name in ("cursor-agent", "agent"):
            binary = root / name
            if binary.exists():
                try:
                    files.extend(collect_cli_js(binary.resolve().parent, seen))
                except OSError:
                    pass
        return files

    version_roots: list[Path] = []
    if (root / "index.js").is_file():
        version_roots.append(root)
    if root.name == "versions":
        version_roots.append(root)
    elif (root / "versions").is_dir():
        version_roots.append(root / "versions")

    for rel in (
        root / "User" / "globalStorage" / "anysphere.cursor-agent-worker" / "agent-cli" / ".local" / "share" / "cursor-agent" / "versions",
        root / "anysphere.cursor-agent-worker" / "agent-cli" / ".local" / "share" / "cursor-agent" / "versions",
    ):
        if rel.is_dir():
            version_roots.append(rel)

    seen: set[str] = set()
    for versions in version_roots:
        key = str(versions)
        if key in seen:
            continue
        seen.add(key)
        try:
            children = [versions] if (versions / "index.js").is_file() else list(versions.iterdir())
        except OSError:
            continue
        for version in children:
            if version.name.startswith(".tmp"):
                continue
            if version.is_dir():
                files.extend(js_in_dir(version))
    return files


def iter_js_files(root: Path) -> list[Path]:
    if root.is_file():
        if root.suffix == ".js":
            return [root]
        if root.name in {"cursor-agent", "agent", "cursor"}:
            try:
                return collect_cli_js(root.resolve().parent)
            except OSError:
                return []
        return []

    files: list[Path] = []
    files.extend(collect_ide_js(root))
    files.extend(collect_cli_js(root))
    return files


def file_looks_relevant(path: Path) -> bool:
    try:
        data = path.read_bytes()
    except OSError:
        return False
    return any(marker in data for marker in SCAN_MARKERS)


def classify(path: Path) -> str:
    text = str(path).replace("\\", "/")
    lower = text.lower()
    if path.suffix.lower() == ".asar":
        return "asar"
    if "cursor-agent-exec" in lower or "cursor-agent-host" in lower or "cursor-local-agent-runtime" in lower:
        return "ide-agent"
    if "workbench" in lower:
        return "ide-workbench"
    if "agent-cli" in lower or "/cursor-agent/versions/" in lower or "/share/cursor-agent/" in lower:
        return "cli"
    if "/Contents/Resources/app/" in text or "/resources/app/" in lower:
        return "ide"
    return "unknown"


def asar_search_roots(root: Path) -> list[Path]:
    if root.is_dir() and root.name in {"Applications"}:
        try:
            return [
                child
                for child in root.iterdir()
                if child.suffix == ".app" and "ursor" in child.name
            ]
        except OSError:
            return []
    return [root]


def find_asars(root: Path, max_depth: int = 8) -> list[Path]:
    found: list[Path] = []
    for base in asar_search_roots(root):
        found.extend(_find_asars_in(base, max_depth=max_depth))
    return found


def _find_asars_in(root: Path, max_depth: int = 8) -> list[Path]:
    found: list[Path] = []
    if root.is_file() and root.suffix.lower() == ".asar":
        return [root]
    if not root.is_dir():
        return []
    for rel in ASAR_RELATIVE:
        candidate = root / rel
        if candidate.is_file():
            found.append(candidate)
    skip_dirs = {
        "node_modules",
        ".git",
        "Cache",
        "CachedData",
        "GPUCache",
        "Code Cache",
        "CachedExtensions",
        "logs",
    }
    try:
        for dirpath, dirnames, filenames in os.walk(root):
            rel = Path(dirpath)
            try:
                depth = len(rel.relative_to(root).parts)
            except ValueError:
                depth = 0
            if depth >= max_depth:
                dirnames.clear()
                continue
            dirnames[:] = [name for name in dirnames if name not in skip_dirs]
            for name in filenames:
                if name.endswith(".asar"):
                    found.append(Path(dirpath) / name)
    except OSError:
        pass
    return found


def blank_bytes(data: bytes) -> tuple[bytes, list[str]]:
    applied: list[str] = []
    for name, needle in ASAR_BLANK_NEEDLES:
        count = data.count(needle)
        if not count:
            continue
        data = data.replace(needle, b" " * len(needle))
        applied.append(f"{name} x{count}")
    return data, applied


def asar_looks_relevant(data: bytes) -> bool:
    return any(marker in data for marker in SCAN_MARKERS) or any(
        needle in data for _name, needle in ASAR_BLANK_NEEDLES
    )


def load_manifest() -> dict:
    if not MANIFEST_PATH.exists():
        return {"files": {}}
    try:
        return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"files": {}}


def save_manifest(manifest: dict) -> None:
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def backup_path_for(path: Path) -> Path:
    digest = hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()[:16]
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", path.name)
    return BACKUP_ROOT / f"{digest}_{safe}"


def ensure_writable(path: Path) -> None:
    try:
        mode = path.stat().st_mode
        path.chmod(mode | stat.S_IWUSR)
    except OSError:
        pass


def apply_patches(text: str) -> tuple[str, list[str]]:
    applied: list[str] = []
    for name, pattern, replacement in PATCHES:
        new_text, count = pattern.subn(replacement, text)
        if count:
            applied.append(f"{name} x{count}")
            text = new_text
    return text, applied


def js_needs_patch(text: str) -> tuple[bool, list[str]]:
    updated, applied = apply_patches(text)
    return updated != text, applied


def is_already_patched(text: str) -> bool:
    needs, _applied = js_needs_patch(text)
    if needs:
        return False
    return (
        "commitAttributionMessage:void 0" in text
        or "commitAttributionMessage:0?" in text
        or "false&&" in text
        or 'if(false' in text
        or f'"{TRAILER_LINE}"' not in text
    )


def enclosing_app(path: Path) -> Path | None:
    for parent in path.parents:
        if parent.suffix == ".app":
            return parent
    return None


def resign_macos_apps(apps: set[Path], dry_run: bool) -> None:
    if sys.platform != "darwin" or not apps:
        return
    for app in sorted(apps):
        print(f"  re-sign {app}")
        if dry_run:
            continue
        subprocess.run(["xattr", "-cr", str(app)], check=False, capture_output=True)
        result = subprocess.run(
            ["codesign", "--force", "--sign", "-", str(app)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            result = subprocess.run(
                ["codesign", "--force", "--deep", "--sign", "-", str(app)],
                capture_output=True,
                text=True,
            )
        if result.returncode != 0:
            print(f"    warning: codesign failed ({result.stderr.strip() or result.stdout.strip()})")
            print("    Cursor may still run; restart it after the patch.")


CLI_COMMAND_HINTS = (
    "cursor-agent",
    "/cursor-agent/versions/",
    "anysphere.cursor-agent-worker",
)
CLI_SKIP_HINTS = (
    "/.grok/bin/agent",
    "Cursor.app/Contents/MacOS/Cursor",
    "Cursor Helper",
    "cursor-attrib-patcher",
)


def _ps_rows() -> list[tuple[int, int, str]]:
    rows: list[tuple[int, int, str]] = []
    if sys.platform == "win32":
        result = subprocess.run(
            ["wmic", "process", "get", "ProcessId,ParentProcessId,CommandLine", "/FORMAT:CSV"],
            capture_output=True,
            text=True,
        )
        for line in result.stdout.splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 4 or not parts[-2].isdigit():
                continue
            try:
                pid = int(parts[-2])
                ppid = int(parts[-3]) if parts[-3].isdigit() else 0
            except ValueError:
                continue
            rows.append((pid, ppid, parts[-1]))
        return rows

    result = subprocess.run(
        ["ps", "-axo", "pid=,ppid=,command="],
        capture_output=True,
        text=True,
    )
    for line in result.stdout.splitlines():
        match = re.match(r"\s*(\d+)\s+(\d+)\s+(.*)$", line)
        if not match:
            continue
        rows.append((int(match.group(1)), int(match.group(2)), match.group(3).strip()))
    return rows


def _process_cwd(pid: int) -> str | None:
    if sys.platform == "darwin":
        result = subprocess.run(
            ["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
            capture_output=True,
            text=True,
        )
        for line in result.stdout.splitlines():
            if line.startswith("n"):
                return line[1:]
        return None
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        return None


def _is_cursor_cli(command: str) -> bool:
    lower = command.lower()
    if "cursor-attrib-patcher" in lower or "/.grok/bin/agent" in command:
        return False
    if "Cursor Helper" in command:
        return False
    if command.endswith("/Cursor") or command.endswith("\\Cursor.exe"):
        return False
    return any(hint in command or hint in lower for hint in CLI_COMMAND_HINTS)


def list_cursor_cli_processes() -> list[dict]:
    found: list[dict] = []
    for pid, ppid, command in _ps_rows():
        if pid in {os.getpid(), os.getppid()}:
            continue
        if not _is_cursor_cli(command):
            continue
        kind = "standalone"
        if re.search(r"(?:^|\s)acp(?:\s|$)", command) or "Synara" in command:
            kind = "acp"
        elif "anysphere.cursor-agent-worker" in command:
            kind = "worker"
        found.append(
            {
                "pid": pid,
                "ppid": ppid,
                "command": command,
                "cwd": _process_cwd(pid),
                "kind": kind,
            }
        )
    return found


def _parent_will_respawn(proc: dict, command_by_pid: dict[int, str]) -> bool:
    if proc["kind"] in {"acp", "worker"}:
        return True
    parent_cmd = command_by_pid.get(proc["ppid"], "")
    return any(
        name in parent_cmd
        for name in ("Synara", "Cursor.app", "cursor-agent", "Code Helper")
    )


def restart_cursor_clis(dry_run: bool = False) -> None:
    procs = list_cursor_cli_processes()
    if not procs:
        print("  no running cursor-agent CLI processes")
        return

    command_by_pid = {pid: cmd for pid, _ppid, cmd in _ps_rows()}
    relaunch: list[dict] = []
    pids: list[int] = []
    for proc in procs:
        print(f"  stop {proc['kind']:10} pid {proc['pid']}  {proc['command'][:140]}")
        pids.append(proc["pid"])
        if not _parent_will_respawn(proc, command_by_pid):
            try:
                argv = shlex.split(proc["command"], posix=sys.platform != "win32")
            except ValueError:
                argv = [proc["command"]]
            if argv:
                relaunch.append({"argv": argv, "cwd": proc.get("cwd")})

    if dry_run:
        print(f"  would restart {len(pids)} CLI process(es)")
        return

    payload = {
        "pids": pids,
        "relaunch": relaunch,
        "delay_sec": 3.0,
    }
    payload_path = Path.home() / ".cursor-attrib-patcher" / "restart-job.json"
    payload_path.parent.mkdir(parents=True, exist_ok=True)
    payload_path.write_text(json.dumps(payload), encoding="utf-8")

    # Detach so this still works when the patcher is a child of cursor-agent.
    subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "--internal-restart", str(payload_path)],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    )
    print(f"  scheduled restart of {len(pids)} CLI process(es)")
    print("  ACP/Synara and IDE workers are stopped so their parent can spawn a fresh process.")


def internal_restart(payload_path: Path) -> int:
    try:
        payload = json.loads(payload_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 1
    time.sleep(float(payload.get("delay_sec") or 3.0))
    pids = [int(pid) for pid in payload.get("pids") or []]
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    deadline = time.time() + 4
    alive = set(pids)
    while alive and time.time() < deadline:
        still = set()
        for pid in alive:
            try:
                os.kill(pid, 0)
                still.add(pid)
            except OSError:
                pass
        alive = still
        if alive:
            time.sleep(0.2)
    for pid in alive:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    for job in payload.get("relaunch") or []:
        argv = job.get("argv") or []
        if not argv:
            continue
        cwd = job.get("cwd") or None
        try:
            subprocess.Popen(
                argv,
                cwd=cwd if cwd and Path(cwd).is_dir() else None,
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
            )
        except OSError:
            continue
    try:
        payload_path.unlink()
    except OSError:
        pass
    return 0


def discover() -> list[Path]:
    found: list[Path] = []
    seen: set[str] = set()
    for root in candidate_roots():
        for js in iter_js_files(root):
            try:
                key = str(js.resolve())
            except OSError:
                continue
            if key in seen:
                continue
            if not file_looks_relevant(js):
                continue
            seen.add(key)
            found.append(js)
    found.sort()
    return found


def discover_asars() -> list[Path]:
    found: list[Path] = []
    seen: set[str] = set()
    for root in candidate_roots():
        for asar in find_asars(root):
            try:
                key = str(asar.resolve())
            except OSError:
                continue
            if key in seen:
                continue
            seen.add(key)
            found.append(asar)
    found.sort()
    return found


def patch_cli_config(dry_run: bool) -> bool:
    config = home() / ".cursor" / "cli-config.json"
    if not config.is_file():
        return False
    try:
        data = json.loads(config.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False
    attribution = data.setdefault("attribution", {})
    changed = False
    for key in ("attributeCommitsToAgent", "attributePRsToAgent"):
        if attribution.get(key) is not False:
            attribution[key] = False
            changed = True
    if not changed:
        return False
    print(f"  {'would update' if dry_run else 'updated'} {config}")
    if not dry_run:
        config.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return True


def _backup_file(path: Path, manifest: dict, surface: str) -> None:
    if str(path.resolve()) in manifest.get("files", {}):
        return
    backup = backup_path_for(path)
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, backup)
    manifest.setdefault("files", {})[str(path.resolve())] = {
        "backup": str(backup),
        "surface": surface,
    }


def cmd_detect(*, verbose: bool = False) -> int:
    """Report whether any local Cursor install still injects attribution."""
    js_files = discover()
    asars = discover_asars()

    dirty = 0
    patched = 0
    clean_asars = 0
    stub_asars = 0

    print("== JavaScript ==")
    if not js_files:
        print("  no attribution injectors found in unpacked JS")
    for path in js_files:
        text = path.read_text(encoding="utf-8", errors="ignore")
        needs, applied = js_needs_patch(text)
        if needs:
            state = "NEED"
            dirty += 1
        else:
            state = "ok"
            patched += 1
        if needs or verbose:
            extra = f"  ({', '.join(applied)})" if needs and applied else ""
            print(f"  [{state:4}] {classify(path):13} {path}{extra}")
    if not verbose and patched:
        print(f"  {patched} file(s) already patched")

    print("\n== Packed archives (.asar) ==")
    print("  Electron asar is an archive, not encryption. Same-length blanks keep offsets valid.")
    if not asars:
        print("  no .asar files found (this Cursor build likely ships unpacked JS)")
    for path in asars:
        try:
            data = path.read_bytes()
        except OSError as exc:
            print(f"  [err ] asar          {path}  ({exc})")
            continue
        size = len(data)
        if size < 1024:
            stub_asars += 1
            print(f"  [stub] asar          {path}  ({size} bytes, pointer/stub)")
            continue
        _updated, applied = blank_bytes(data)
        relevant = asar_looks_relevant(data)
        if applied:
            dirty += 1
            print(f"  [NEED] asar          {path}  ({size} bytes; {', '.join(applied)})")
        elif relevant:
            patched += 1
            print(f"  [ok  ] asar          {path}  ({size} bytes, markers present, already blanked)")
        else:
            clean_asars += 1
            print(f"  [clean] asar         {path}  ({size} bytes, no attribution markers)")

    print("\n== Summary ==")
    print(f"  {dirty} still need patching, {patched} already patched, {clean_asars} clean asar(s), {stub_asars} asar stub(s).")
    if dirty:
        print("  Needs patching: YES")
        print("  Run: python3 patch.py")
        return 2
    if not js_files and not asars:
        print("  Needs patching: UNKNOWN (no Cursor injectors or archives found)")
        print("  Set CURSOR_ATTRIB_PATCHER_PATHS to extra install dirs if needed.")
        return 1
    print("  Needs patching: NO")
    return 0


def cmd_status() -> int:
    return cmd_detect(verbose=True)


def cmd_patch(dry_run: bool, restart: bool = True) -> int:
    files = discover()
    asars = discover_asars()
    if not files and not asars:
        print("No Cursor attribution injectors found.")
        print("Set CURSOR_ATTRIB_PATCHER_PATHS to extra install dirs if needed.")
        return 1

    manifest = load_manifest()
    apps: set[Path] = set()
    changed_files = 0
    unchanged = 0
    blocked: list[Path] = []

    for path in files:
        original = path.read_text(encoding="utf-8", errors="ignore")
        updated, applied = apply_patches(original)
        rel = classify(path)
        if not applied or updated == original:
            unchanged += 1
            continue

        print(f"[{'dry' if dry_run else 'patch'}] {rel:13} {path}")
        for item in applied:
            print(f"         {item}")
        changed_files += 1
        if dry_run:
            app = enclosing_app(path)
            if app:
                apps.add(app)
            continue

        _backup_file(path, manifest, rel)
        try:
            ensure_writable(path)
            path.write_text(updated, encoding="utf-8")
        except PermissionError:
            blocked.append(path)
            changed_files -= 1
            print("         blocked by macOS (App Management). Run this from Terminal.app.")
            continue
        app = enclosing_app(path)
        if app:
            apps.add(app)

    for path in asars:
        try:
            original = path.read_bytes()
        except OSError:
            continue
        if len(original) < 1024:
            unchanged += 1
            continue
        updated, applied = blank_bytes(original)
        if not applied or updated == original:
            unchanged += 1
            continue
        print(f"[{'dry' if dry_run else 'patch'}] asar          {path}")
        for item in applied:
            print(f"         {item}")
        changed_files += 1
        if dry_run:
            app = enclosing_app(path)
            if app:
                apps.add(app)
            continue
        _backup_file(path, manifest, "asar")
        try:
            ensure_writable(path)
            path.write_bytes(updated)
        except PermissionError:
            blocked.append(path)
            changed_files -= 1
            print("         blocked by macOS (App Management). Run this from Terminal.app.")
            continue
        app = enclosing_app(path)
        if app:
            apps.add(app)

    cli_config_changed = patch_cli_config(dry_run)
    if not dry_run:
        save_manifest(manifest)
        resign_macos_apps(apps, dry_run=False)
        if restart:
            print("Restarting Cursor CLI processes...")
            restart_cursor_clis(dry_run=False)

    print(
        f"\n{'Would patch' if dry_run else 'Patched'} {changed_files} file(s), "
        f"{unchanged} unchanged."
        + (" CLI config attribution flags set to false." if cli_config_changed else "")
    )
    if changed_files and not dry_run and not restart:
        print("Restart Cursor and cursor-agent for the patch to take effect.")
    if blocked:
        script = Path(__file__).resolve()
        print("\nmacOS blocked in-place writes to Cursor.app from this process.")
        print("CLI copies in your home folder were still patched.")
        print("To patch the IDE, run this in Terminal.app:")
        print(f"  python3 {script}")
        print("If Terminal is also blocked: System Settings → Privacy & Security → App Management")
        print("and allow Terminal (or python), then re-run.")
        return 2
    return 0 if changed_files or unchanged else 1


def cmd_restore(restart: bool = True) -> int:
    manifest = load_manifest()
    files = manifest.get("files") or {}
    if not files:
        print("No backup manifest found. Nothing to restore.")
        return 1
    apps: set[Path] = set()
    restored = 0
    for original, meta in files.items():
        src = Path(meta["backup"])
        dest = Path(original)
        if not src.is_file():
            print(f"[miss ] backup missing     {original}")
            continue
        print(f"[rest ] {dest}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        ensure_writable(dest)
        shutil.copy2(src, dest)
        app = enclosing_app(dest)
        if app:
            apps.add(app)
        restored += 1
    resign_macos_apps(apps, dry_run=False)
    if restart:
        print("Restarting Cursor CLI processes...")
        restart_cursor_clis(dry_run=False)
    print(f"\nRestored {restored} file(s).")
    return 0 if restored else 1


def main() -> int:
    if len(sys.argv) >= 3 and sys.argv[1] == "--internal-restart":
        return internal_restart(Path(sys.argv[2]))

    parser = argparse.ArgumentParser(
        description="Detect Cursor IDE/CLI installs and disable git/PR attribution injection."
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="patch",
        choices=("patch", "detect", "status", "restore", "scan", "restart"),
        help="patch (default), detect, status, restore, restart CLIs, or scan",
    )
    parser.add_argument("--dry-run", action="store_true", help="show what would change")
    parser.add_argument(
        "--no-restart",
        action="store_true",
        help="do not stop/restart running cursor-agent processes",
    )
    args = parser.parse_args()

    if args.command == "detect":
        return cmd_detect()
    if args.command in {"status", "scan"}:
        return cmd_status()
    if args.command == "restart":
        print("Restarting Cursor CLI processes...")
        restart_cursor_clis(dry_run=args.dry_run)
        return 0
    if args.command == "restore":
        return cmd_restore(restart=not args.no_restart)
    return cmd_patch(dry_run=args.dry_run, restart=not args.no_restart)


if __name__ == "__main__":
    sys.exit(main())
