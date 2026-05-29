#!/usr/bin/env python3
# WSL Mount Manager
# Copyright (c) 2026 Dmitry Savosh <d.savosh@gmail.com>
# Licensed under the MIT License. See the LICENSE file for details.
"""
WSL Mount Manager — mount Linux disk partitions into WSL (single-file Textual TUI).

Mounts ANY filesystem that lsblk recognizes (ext4/ext3/btrfs/xfs/vfat/…),
remembers named profiles, shows a live status of what is mounted, and opens
mounts in Windows Explorer or a WSL terminal.

Run:
    python mounter.py

Dependency:  textual   (pip install textual)
Privileges:  Administrator is required (wsl --mount). If started without it,
             the app requests UAC and relaunches itself elevated.

Fast keyboard mounting:
    ↑↓        — move through the "disks → partitions" tree
    Enter     — on a disk: expand and show its partitions;
                on a partition: mount it under the name from the "Mount name" field.
    Tab       — switch focus between the tree / "Mounted" / "Profiles"

Other keys (see Footer): m mount · u unmount · e explorer · t terminal ·
p save profile · r refresh · q quit · Ctrl+T theme picker · Ctrl+O menu ·
Ctrl+P command palette.

State is stored in  ~/.wsl-mount-manager/  (state.json / profiles.json / active.json).
"""
from __future__ import annotations

import ctypes
import json
import os
import re
import subprocess
import sys
import textwrap

from rich.text import Text

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.command import DiscoveryHit, Hit, Provider
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.theme import Theme
from textual.widgets import (DataTable, Footer, Header, HelpPanel, Input, Label,
                             OptionList, Static, Tree)


# App metadata (shown in the About window and usable from outside).
__version__ = "0.2.1"
__author__ = "Dmitry Savosh"
__email__ = "d.savosh@gmail.com"
from textual.widgets.option_list import Option


# ============================================================
# ENGINE: console, privileges, elevation
# ============================================================

def enable_vt() -> None:
    """Enable ANSI escape sequences in the Windows console (Win10+)."""
    if os.name == "nt":
        try:
            k = ctypes.windll.kernel32
            handle = k.GetStdHandle(-11)
            mode = ctypes.c_ulong()
            k.GetConsoleMode(handle, ctypes.byref(mode))
            k.SetConsoleMode(handle, mode.value | 0x0004)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
        except Exception:
            pass


def is_admin() -> bool:
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False


def elevate_self(extra_args: list[str] | None = None) -> None:
    """
    Relaunch the current process elevated via UAC (ShellExecuteW "runas").
    Does not return on success (calls sys.exit).
    """
    rest = list(sys.argv[1:])
    if extra_args:
        rest += extra_args
    if "--elevated" not in rest:
        rest.append("--elevated")

    # Reconstruct how we were launched: module (`-m pkg.app`) or a script file.
    main_mod = sys.modules.get("__main__")
    spec = getattr(main_mod, "__spec__", None)
    if spec is not None and spec.name:
        launch = ["-m", spec.name]
    else:
        launch = [os.path.abspath(sys.argv[0])]

    params = " ".join(f'"{a}"' for a in [*launch, *rest])
    rc = ctypes.windll.shell32.ShellExecuteW(
        None, "runas", sys.executable, params, os.getcwd(), 1  # SW_SHOWNORMAL
    )
    if rc <= 32:
        raise PermissionError(f"UAC request was declined or failed (ShellExecute code={rc}).")
    sys.exit(0)


def ensure_admin() -> None:
    """Request UAC and relaunch if not elevated. Otherwise do nothing."""
    if is_admin():
        return
    if "--elevated" in sys.argv:
        raise PermissionError("Relaunch did not grant administrator privileges.")
    elevate_self()  # does not return on success


# ============================================================
# ENGINE: running external commands (wsl.exe / powershell)
# ============================================================

def smart_decode(b: bytes) -> str:
    """wsl.exe emits UTF-16 LE, PowerShell emits UTF-8. Guess which one."""
    if not b:
        return ""
    if len(b) >= 2 and b[1] == 0:  # looks like UTF-16 LE
        try:
            return b.decode("utf-16-le").replace("\r\n", "\n").rstrip("\x00")
        except UnicodeDecodeError:
            pass
    try:
        return b.decode("utf-8").replace("\r\n", "\n")
    except UnicodeDecodeError:
        return b.decode("utf-8", errors="replace")


# Hide flashing console windows of child processes.
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW


def run(cmd, check: bool = False):
    """Run a process and return (returncode, stdout, stderr), already decoded."""
    r = subprocess.run(cmd, capture_output=True, creationflags=_NO_WINDOW)
    out = smart_decode(r.stdout)
    err = smart_decode(r.stderr)
    if check and r.returncode != 0:
        raise RuntimeError(f"{cmd}: exit {r.returncode}\n{err or out}")
    return r.returncode, out, err


def wsl_sh(shell_cmd: str):
    """Run a shell command inside the default WSL distro (without profile/rc)."""
    return run(["wsl", "bash", "--noprofile", "--norc", "-c", shell_cmd])


def wsl_root_sh(shell_cmd: str):
    """Run a shell command as root inside the default WSL distro (for mount/umount)."""
    return run(["wsl", "-u", "root", "bash", "--noprofile", "--norc", "-c", shell_cmd])


def extract_json(s: str) -> str:
    """Strip leading terminal escape sequences up to the first { or [."""
    for i, ch in enumerate(s):
        if ch in "{[":
            return s[i:]
    return s


def check_wsl_alive() -> tuple[bool, str]:
    """Check that WSL responds. Returns (ok, diagnostics)."""
    code, out, err = wsl_sh("echo __WSL_OK__")
    if code != 0 or "__WSL_OK__" not in out:
        msg = (
            "WSL is not responding. Check:\n"
            "  wsl --status\n"
            "  wsl --list --verbose\n"
            "  if needed:  wsl --update  or start the distro manually once\n"
            f"\nDiagnostics: exit={code} stdout={out!r} stderr={err!r}"
        )
        return False, msg
    return True, ""


def get_default_distro() -> str:
    code, out, _ = run(["wsl", "--list", "--quiet"])
    if code != 0:
        return "Ubuntu"
    for line in out.splitlines():
        name = line.strip()
        if name:
            return name
    return "Ubuntu"


# ============================================================
# ENGINE: enumerating disks and partitions
# ============================================================

def list_drives() -> list[dict]:
    """Physical Windows disks (Win32_DiskDrive) — candidates for wsl --mount."""
    ps = (
        "Get-CimInstance Win32_DiskDrive | "
        "Sort-Object DeviceID | "
        "Select-Object DeviceID, Model, Size, InterfaceType, MediaType | "
        "ConvertTo-Json -Depth 3"
    )
    _, out, _ = run(["powershell", "-NoProfile", "-Command", ps], check=True)
    data = json.loads(extract_json(out))
    return data if isinstance(data, list) else [data]


def list_block_devices() -> list[dict]:
    """lsblk inside WSL — sees disks attached via --mount and their partitions."""
    code, out, err = wsl_sh("lsblk -OJ")
    out = out.strip()
    if code != 0 or not out:
        raise RuntimeError(
            f"lsblk failed (exit={code}).\n"
            f"  stdout: {out[:300]!r}\n  stderr: {err[:300]!r}"
        )
    try:
        return json.loads(extract_json(out))["blockdevices"]
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"Failed to parse lsblk JSON: {e}\n  first 500 bytes: {out[:500]!r}"
        )


def disk_names(blocks: list[dict]) -> set[str]:
    return {d["name"] for d in blocks}


def partitions_of(disk: dict) -> list[dict]:
    out = []
    if disk.get("children"):
        for c in disk["children"]:
            out.append({**c, "_parent": disk["name"]})
    return out


def is_mounted(p: dict) -> bool:
    return any(m for m in (p.get("mountpoints") or []) if m)


def mount_target(p: dict) -> str | None:
    """First non-empty mountpoint of a partition, or None."""
    for m in (p.get("mountpoints") or []):
        if m:
            return m
    return None


# Identifiers for remembering selections and profiles (more stable than DeviceID).
def drive_key(d: dict) -> str:
    # Model+Size is more stable than DeviceID (\\.\PHYSICALDRIVE2 can change).
    return f"{d.get('Model') or '?'}|{d.get('Size') or 0}"


def part_key(p: dict) -> str:
    # label is the most stable; if missing, fall back to size and fs.
    return f"{p.get('label') or ''}|{p.get('size') or ''}|{p.get('fstype') or ''}"


def find_idx(items, target_key, key_fn) -> int:
    if not target_key:
        return 0
    for i, it in enumerate(items):
        if key_fn(it) == target_key:
            return i
    return 0


# ============================================================
# ENGINE: mount operations
# ============================================================

def attach_bare(device_id: str) -> tuple[bool, str]:
    """
    Bare-mount a physical disk so WSL exposes its partitions in lsblk.
    Automatically detaches and retries on ALREADY_ATTACHED.
    """
    code, out, err = run(["wsl", "--mount", device_id, "--bare"])
    if code != 0 and "ALREADY_ATTACHED" in (out + err).upper():
        run(["wsl", "--unmount", device_id])
        code, out, err = run(["wsl", "--mount", device_id, "--bare"])
    if code != 0:
        return False, (err or out).strip()
    return True, ""


def detach(device_id: str) -> tuple[bool, str]:
    """`wsl --unmount` a physical disk (also used to cancel bare mode)."""
    code, out, err = run(["wsl", "--unmount", device_id])
    return code == 0, (err or out).strip()


def _is_candidate(p: dict) -> bool:
    """A partition is mountable: not mounted, has a recognized filesystem."""
    return not is_mounted(p) and p.get("fstype") not in (None, "", "swap")


def enumerate_partitions(device_id: str) -> tuple[list[dict], str]:
    """
    Bare-mount a disk and return the list of mountable partitions.
    The filesystem type comes from lsblk — anything lsblk and
    `wsl --mount --type` understand is supported (ext4/ext3/btrfs/xfs/vfat/...),
    not just ext4.

    Returns (candidates, error_message). On error the list is empty.
    Note: the disk stays bare-mounted after this call; if the user does not
    proceed to mount, detach(device_id) must be called.
    """
    try:
        before = disk_names(list_block_devices())
    except Exception as e:
        return [], f"lsblk (before mount): {e}"

    ok, msg = attach_bare(device_id)
    if not ok:
        return [], (
            f"bare-mount failed: {msg}\n"
            "Likely: the disk is in use by Windows (has a mounted volume), it is the "
            "system disk, no administrator privileges, or a wrong disk name."
        )

    try:
        after = list_block_devices()
    except Exception as e:
        detach(device_id)
        return [], f"lsblk (after mount): {e}"

    new_disks = [d for d in after if d["name"] not in before]
    if not new_disks:
        # Fallback: show all disks if the diff caught nothing.
        new_disks = after

    parts: list[dict] = []
    for d in new_disks:
        parts.extend(partitions_of(d))
        if not d.get("children") and d.get("fstype"):
            parts.append({**d, "_parent": None})

    candidates = [p for p in parts if _is_candidate(p)]
    if not candidates:
        detach(device_id)
        return [], "No mountable partitions on the selected disk."
    return candidates, ""


def partition_number(part: dict) -> str | None:
    """Partition number from a name like sda2 / nvme0n1p3."""
    m = re.search(r"(\d+)$", part.get("name", ""))
    return m.group(1) if m else None


def wsl_block_exists(partname: str) -> bool:
    """True if /dev/<partname> is already a block device in WSL — i.e. the disk is
    already attached (e.g. browse-attached when its tree node was expanded), so no
    `wsl --mount --bare` is needed."""
    if not partname:
        return False
    code, out, _ = wsl_sh(f'test -b "/dev/{partname}" && echo __OK__')
    return code == 0 and "__OK__" in out


def ensure_attached(device_id: str) -> tuple[bool, str]:
    """
    Make sure the disk is attached to WSL (bare), WITHOUT detaching it if it is
    already attached — so partitions already mounted on it stay mounted. This is
    what lets several partitions of the SAME disk be mounted at once.
    """
    code, out, err = run(["wsl", "--mount", device_id, "--bare"])
    if code == 0 or "ALREADY_ATTACHED" in (out + err).upper():
        return True, ""
    return False, (err or out).strip()


def mount_partition(device_id: str, partname: str, fstype: str, name: str) -> tuple[bool, str, str]:
    """
    Mount a single partition under /mnt/wsl/<name>.

    The disk is attached once (bare); each partition is then mounted manually as
    root inside WSL (mount /dev/<partname> …). Unlike `wsl --mount --partition`
    (which can hold only one partition per disk at a time), this lets multiple
    partitions of one disk be mounted simultaneously. Returns (ok, mountpoint, msg).
    """
    # Only attach when the partition device isn't already present. If the disk was
    # browse-attached earlier (its tree node expanded), /dev/<partname> already
    # exists and a second `wsl --mount --bare` would fail with an inconsistent
    # "already attached"/"being used by another process" message on some WSL builds.
    if not wsl_block_exists(partname):
        ok, msg = ensure_attached(device_id)
        if not ok:
            return False, "", f"could not attach disk: {msg}"
    mp = f"/mnt/wsl/{name}"
    topt = f"-t {fstype} " if fstype else ""
    cmd = f'mkdir -p "{mp}" && mount {topt}"/dev/{partname}" "{mp}"'
    code, out, err = wsl_root_sh(cmd)
    if code != 0:
        return False, "", (err or out).strip()
    return True, mp, ""


def _is_path_mounted(name: str) -> bool:
    """True if /mnt/wsl/<name> still appears as a mountpoint in lsblk (the same
    source the UI reads), so unmount success is judged by what the UI will show."""
    mp = f"/mnt/wsl/{name}"
    return any(m.get("mountpoint") == mp for m in list_mounted())


def unmount_mount(name: str) -> tuple[bool, str]:
    """
    Unmount /mnt/wsl/<name> robustly. WSL keeps /mnt/wsl in a shared mount
    namespace; a plain `umount` in one namespace may not clear the mount that
    lsblk (and \\wsl$) actually show. So try several strategies — plain, inside
    the WSL init namespace (nsenter -t 1 -m), and lazy — and stop as soon as
    lsblk confirms the mountpoint is gone. The disk stays attached.
    """
    mp = f"/mnt/wsl/{name}"
    strategies = [
        f'umount "{mp}"',
        f'nsenter -t 1 -m -- umount "{mp}"',
        f'umount -l "{mp}"',
        f'nsenter -t 1 -m -- umount -l "{mp}"',
    ]
    last = ""
    for cmd in strategies:
        _, out, err = wsl_root_sh(cmd)
        last = (err or out).strip() or last
        if not _is_path_mounted(name):
            wsl_root_sh(f'rmdir "{mp}" 2>/dev/null; nsenter -t 1 -m -- rmdir "{mp}" 2>/dev/null; true')
            return True, ""
    if not _is_path_mounted(name):
        return True, ""
    return False, last or "umount did not take effect"


def unmount_disk(device_id: str) -> tuple[bool, str]:
    """Fully detach a physical disk from WSL."""
    return detach(device_id)


def partitions_by_lsblk_name(name: str) -> list[dict] | None:
    """Partitions of an already-attached disk identified by its lsblk name (e.g. sdc)."""
    try:
        for disk in list_block_devices():
            if disk.get("name") == name:
                return partitions_of(disk)
    except Exception:
        return None
    return None


def _size_to_bytes(s) -> float:
    """Parse an lsblk human size ('1.8T', '500G', '931.5G') into bytes (1024-based)."""
    s = str(s or "").strip().rstrip("B")
    if not s:
        return 0.0
    units = {"K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4, "P": 1024 ** 5}
    try:
        if s[-1] in units:
            return float(s[:-1]) * units[s[-1]]
        return float(s)
    except ValueError:
        return 0.0


def _match_disk_by_size(win_size: int):
    """Find the lsblk disk whose size best matches a Windows disk size (±6%)."""
    if not win_size:
        return None
    try:
        disks = list_block_devices()
    except Exception:
        return None
    best, best_diff = None, 0.06
    for d in disks:
        b = _size_to_bytes(d.get("size"))
        if b <= 0:
            continue
        diff = abs(b - win_size) / win_size
        if diff < best_diff:
            best, best_diff = d, diff
    return best


def _collect_disk_parts(disks: list[dict]) -> list[dict]:
    parts: list[dict] = []
    for d in disks:
        parts.extend(partitions_of(d))
        if not d.get("children") and d.get("fstype"):
            parts.append({**d, "_parent": None})
    return parts


def disk_partitions(drive: dict) -> tuple[list[dict], str]:
    """
    List a disk's partitions for browsing — robust to the disk being already
    attached to WSL (from an earlier browse/mount this session). Returns
    (parts, error); parts includes both mounted and free partitions.

    1) If a partition of this disk is already mounted by us -> read from lsblk.
    2) Otherwise bare-attach fresh and identify the disk via a before/after diff.
    3) If WSL says the disk is already attached (and we can't re-attach without
       risking a live mount) -> match the disk in lsblk by size.
    """
    device_id = drive["DeviceID"]
    win_size = drive.get("Size") or 0

    ap = attached_disk_partitions(device_id)
    if ap is not None:
        return ap, ""

    try:
        before = disk_names(list_block_devices())
    except Exception as e:
        return [], f"lsblk (before mount): {e}"

    code, out, err = run(["wsl", "--mount", device_id, "--bare"])
    blob = out + err
    if code == 0:
        try:
            after = list_block_devices()
        except Exception as e:
            detach(device_id)
            return [], f"lsblk (after mount): {e}"
        new = [d for d in after if d["name"] not in before]
        parts = _collect_disk_parts(new or after)
        if not parts:
            detach(device_id)
            return [], "No partitions found on the selected disk."
        return parts, ""

    # bare-mount failed. The most common reason here is that the disk is ALREADY
    # attached to WSL — we browse-attach a disk whenever its tree node is expanded,
    # and WSL then refuses a second `--mount --bare`. The refusal message is not
    # consistent across WSL builds ("ALREADY_ATTACHED" on some, "being used by
    # another process" on others), so don't match on the text. lsblk only lists
    # disks that ARE attached, so if a disk of this size shows up there it is ours
    # and already attached — use it instead of treating this as a hard failure.
    d = _match_disk_by_size(win_size)
    if d is not None:
        return _collect_disk_parts([d]), ""
    if "ALREADY_ATTACHED" in blob.upper():
        return [], ("Disk is already attached to WSL but could not be matched in lsblk.\n"
                    f"Press r to refresh, or detach it manually: wsl --unmount {device_id}")
    return [], (f"bare-mount failed: {blob.strip()}\n"
                "Likely the disk is in use by Windows or is the system disk.")


def list_mounted() -> list[dict]:
    """
    Live status: what is currently mounted under /mnt/wsl/.
    Source of truth is lsblk inside WSL (mountpoints). The Windows DeviceID
    needed to unmount is taken from active.json (lsblk does not know the
    Windows disk name).
    """
    try:
        blocks = list_block_devices()
    except Exception:
        return []

    result: list[dict] = []
    for disk in blocks:
        for p in [disk, *(disk.get("children") or [])]:
            mp = mount_target(p)
            if mp and mp.startswith("/mnt/wsl/"):
                result.append({
                    "name": mp.rsplit("/", 1)[-1],
                    "mountpoint": mp,
                    "source": p.get("name", ""),
                    "fstype": p.get("fstype") or "",
                    "label": p.get("label") or "",
                    "size": p.get("size") or "",
                })
    return result


# ============================================================
# ENGINE: open a mount in Explorer / WSL terminal
# ============================================================

def windows_unc_path(name: str) -> str:
    """\\\\wsl$\\<distro>\\mnt\\wsl\\<name> — a path Windows Explorer understands."""
    return f"\\\\wsl$\\{get_default_distro()}\\mnt\\wsl\\{name}"


def open_in_explorer(name: str) -> tuple[bool, str]:
    unc = windows_unc_path(name)
    try:
        # explorer.exe returns a non-zero code even on success — don't check it.
        subprocess.Popen(["explorer.exe", unc])
        return True, unc
    except Exception as e:
        return False, str(e)


def open_wsl_terminal(mountpoint: str) -> tuple[bool, str]:
    """Open a WSL terminal at `mountpoint`. Try Windows Terminal, then wsl."""
    try:
        subprocess.Popen(["wt", "wsl", "--cd", mountpoint])
        return True, "wt"
    except FileNotFoundError:
        pass
    try:
        subprocess.Popen(["cmd", "/c", "start", "wsl", "--cd", mountpoint])
        return True, "wsl"
    except Exception as e:
        return False, str(e)


# ============================================================
# STATE: state.json / profiles.json / active.json
# ============================================================

CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".wsl-mount-manager")
STATE_FILE = os.path.join(CONFIG_DIR, "state.json")
PROFILES_FILE = os.path.join(CONFIG_DIR, "profiles.json")
ACTIVE_FILE = os.path.join(CONFIG_DIR, "active.json")


def _read(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def _write(path: str, data) -> None:
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def load_state() -> dict:
    return _read(STATE_FILE, {})


def save_state(state: dict) -> None:
    _write(STATE_FILE, state)


def remember_drive(drive: dict) -> None:
    st = load_state()
    st["drive"] = drive_key(drive)
    save_state(st)


def remember_part(part: dict) -> None:
    st = load_state()
    st["part"] = part_key(part)
    save_state(st)


def remember_theme(name: str) -> None:
    st = load_state()
    st["theme"] = name
    save_state(st)


def saved_theme() -> str | None:
    name = load_state().get("theme")
    return name if isinstance(name, str) else None


def list_profiles() -> list[dict]:
    data = _read(PROFILES_FILE, [])
    return data if isinstance(data, list) else []


def add_profile(label: str, drive: dict, part: dict, name: str) -> dict:
    """Save a disk+partition+mount-name combo under a label. Same label overwrites."""
    prof = {
        "label": label,
        "drive_key": drive_key(drive),
        "part_key": part_key(part),
        "name": name,
        "fstype": part.get("fstype") or "ext4",
        "drive_model": drive.get("Model") or "?",   # for display in the UI
        "part_label": part.get("label") or "",
    }
    profs = [p for p in list_profiles() if p.get("label") != label]
    profs.append(prof)
    _write(PROFILES_FILE, profs)
    return prof


def remove_profile(label: str) -> None:
    profs = [p for p in list_profiles() if p.get("label") != label]
    _write(PROFILES_FILE, profs)


def resolve_profile(profile: dict, drives: list[dict], parts: list[dict]) -> tuple[dict | None, dict | None]:
    """Find the current disk (in drives) and partition (in parts) by saved keys."""
    drive = next((d for d in drives if drive_key(d) == profile.get("drive_key")), None)
    part = next((p for p in parts if part_key(p) == profile.get("part_key")), None)
    return drive, part


def get_active() -> dict:
    data = _read(ACTIVE_FILE, {})
    return data if isinstance(data, dict) else {}


def _entry_device(entry) -> str | None:
    """DeviceID from an active.json entry (dict) — tolerant of the legacy str form."""
    if isinstance(entry, dict):
        return entry.get("device_id")
    return entry


def set_active(name: str, device_id: str, partname: str = "",
               fstype: str = "", mountpoint: str = "") -> None:
    act = get_active()
    act[name] = {"device_id": device_id, "partname": partname, "fstype": fstype,
                 "mountpoint": mountpoint or f"/mnt/wsl/{name}"}
    _write(ACTIVE_FILE, act)


def clear_active(name: str) -> None:
    act = get_active()
    act.pop(name, None)
    _write(ACTIVE_FILE, act)


def device_for_mount(name: str) -> str | None:
    """Windows DeviceID of the disk whose partition is mounted under this name."""
    return _entry_device(get_active().get(name))


def disk_in_use(device_id: str) -> bool:
    """True if any partition of this disk is currently mounted (tracked in active)."""
    return any(_entry_device(e) == device_id for e in get_active().values())


def unique_mount_name(name: str, taken) -> str:
    """
    Return `name` if free, otherwise append the smallest numeric suffix not in
    `taken` (the set of mount names already in use under /mnt/wsl/).

    Several disks can be mounted at once, but WSL requires a distinct --name per
    simultaneous mount — reusing a name would collide at the same /mnt/wsl/ path
    and overwrite the previous mount.
    """
    name = name or "data"
    if name not in taken:
        return name
    i = 2
    while f"{name}{i}" in taken:
        i += 1
    return f"{name}{i}"


def attached_disk_partitions(device_id: str) -> list[dict] | None:
    """
    If `device_id` currently has a partition mounted by this tool (tracked in
    active.json), return that disk's partitions from the CURRENT lsblk output
    WITHOUT bare-mounting/detaching — re-attaching the disk (wsl --unmount +
    --bare) would tear down the live mount.

    Returns None if the disk is not known to be mounted, in which case the
    caller should fall back to enumerate_partitions().
    """
    names = {n for n, e in get_active().items() if _entry_device(e) == device_id}
    if not names:
        return None
    wanted = {f"/mnt/wsl/{n}" for n in names}
    try:
        blocks = list_block_devices()
    except Exception:
        return None
    for disk in blocks:
        for child in (disk.get("children") or []):
            mp = mount_target(child)
            if mp and mp in wanted:
                return partitions_of(disk)
    return None


def attached_partition_by_key(target_key: str) -> dict | None:
    """
    Find a partition matching `part_key` among the disks CURRENTLY attached to WSL
    (read straight from live lsblk), WITHOUT attaching anything. Lets a profile
    mount resolve its partition fast when the disk is already attached/browsed,
    skipping the slow `list_drives` + failing `wsl --mount --bare` path. Returns the
    partition dict (with `_parent` set) or None if not found among attached disks.
    """
    if not target_key:
        return None
    try:
        blocks = list_block_devices()
    except Exception:
        return None
    for d in blocks:
        for p in _collect_disk_parts([d]):
            if part_key(p) == target_key:
                return p
    return None


# Aliases so the app code can call engine.* / profiles.* — both names point to
# this same module (all names above are unique).
engine = profiles = sys.modules[__name__]


# ============================================================
# TUI (Textual)
# ============================================================

# Custom dark theme: near-black background, bright text accents.
MOUNTER_THEME = Theme(
    name="mounter-dark",
    background="#0d0d10",   # near-black background
    surface="#161619",      # panels slightly lighter than the background
    panel="#1e1e22",
    foreground="#E8E8E8",   # light primary text
    primary="#3CE88F",      # bright green — structure
    secondary="#FF7AC6",    # bright pink
    accent="#FFC247",       # bright amber — selection / important
    success="#3CE88F",
    warning="#FFC247",
    error="#FF5C6C",
    dark=True,
    # Force GREY scrollbars everywhere. By default Textual derives the scrollbar
    # color from `primary` (green here), which leaks into widgets we don't style
    # in CSS — the command palette list, the keys/help panel, etc. Setting the
    # theme's scrollbar variables greys every scrollbar across all screens.
    variables={
        "scrollbar": "#4a4a52",
        "scrollbar-hover": "#5e5e68",
        "scrollbar-active": "#7a7a86",
        "scrollbar-background": "#161619",
        "scrollbar-background-hover": "#161619",
        "scrollbar-background-active": "#161619",
        "scrollbar-corner-color": "#161619",
        # Keep this theme's blurred row cursor the same dark grey it has always been
        # (other themes use their own readable defaults — see the cursor CSS rules).
        "block-cursor-blurred-background": "#3f3f48",
        # Keep the vivid brand accents for coloured text (other themes resolve these
        # to their own readable values, picked up by MountApp._sync_accent).
        "text-success": "#3CE88F",
        "text-warning": "#FFC247",
        "text-secondary": "#FF7AC6",
    },
)

# Extra popular themes (registered alongside Textual's built-ins). The app's
# theme-aware accents (_sync_accent) and cursor colours keep them all readable.
EXTRA_THEMES = [
    Theme(name="tokyo-night-storm", dark=True,
          background="#24283b", surface="#1f2335", panel="#292e42", foreground="#c0caf5",
          primary="#7aa2f7", secondary="#bb9af7", accent="#ff9e64",
          success="#9ece6a", warning="#e0af68", error="#f7768e"),
    Theme(name="gruvbox-light", dark=False,
          background="#fbf1c7", surface="#ebdbb2", panel="#d5c4a1", foreground="#3c3836",
          primary="#076678", secondary="#8f3f71", accent="#af3a03",
          success="#79740e", warning="#b57614", error="#9d0006"),
    Theme(name="everforest", dark=True,
          background="#2d353b", surface="#343f44", panel="#3d484d", foreground="#d3c6aa",
          primary="#a7c080", secondary="#d699b6", accent="#e69875",
          success="#a7c080", warning="#dbbc7f", error="#e67e80"),
    Theme(name="kanagawa", dark=True,
          background="#1f1f28", surface="#16161d", panel="#2a2a37", foreground="#dcd7ba",
          primary="#7e9cd8", secondary="#957fb8", accent="#ffa066",
          success="#76946a", warning="#c0a36e", error="#c34043"),
    Theme(name="ayu-dark", dark=True,
          background="#0b0e14", surface="#0d1017", panel="#131721", foreground="#bfbdb6",
          primary="#59c2ff", secondary="#d2a6ff", accent="#ffb454",
          success="#aad94c", warning="#ffb454", error="#f26d78"),
    Theme(name="ayu-mirage", dark=True,
          background="#1f2430", surface="#242936", panel="#2d3343", foreground="#cccac2",
          primary="#73d0ff", secondary="#dfbfff", accent="#ffad66",
          success="#87d96c", warning="#ffd173", error="#ff6666"),
    Theme(name="night-owl", dark=True,
          background="#011627", surface="#0b2942", panel="#13344f", foreground="#d6deeb",
          primary="#82aaff", secondary="#c792ea", accent="#ffcb8b",
          success="#addb67", warning="#ecc48d", error="#ef5350"),
    Theme(name="github-dark", dark=True,
          background="#0d1117", surface="#161b22", panel="#21262d", foreground="#c9d1d9",
          primary="#58a6ff", secondary="#bc8cff", accent="#f0883e",
          success="#3fb950", warning="#d29922", error="#f85149"),
    Theme(name="github-light", dark=False,
          background="#ffffff", surface="#f6f8fa", panel="#eaeef2", foreground="#24292f",
          primary="#0969da", secondary="#8250df", accent="#bc4c00",
          success="#1a7f37", warning="#9a6700", error="#cf222e"),
    Theme(name="material", dark=True,
          background="#263238", surface="#2e3c43", panel="#37474f", foreground="#eeffff",
          primary="#82aaff", secondary="#c792ea", accent="#ffcb6b",
          success="#c3e88d", warning="#ffcb6b", error="#f07178"),
    Theme(name="synthwave-84", dark=True,
          background="#262335", surface="#2a2139", panel="#34294f", foreground="#f4eee4",
          primary="#ff7edb", secondary="#36f9f6", accent="#fede5d",
          success="#72f1b8", warning="#fede5d", error="#fe4450"),
    Theme(name="cobalt2", dark=True,
          background="#193549", surface="#15232d", panel="#1f4662", foreground="#ffffff",
          primary="#ffc600", secondary="#ff9d00", accent="#0088ff",
          success="#3ad900", warning="#ffc600", error="#ff628c"),
    Theme(name="oceanic-next", dark=True,
          background="#1b2b34", surface="#16242b", panel="#22343d", foreground="#cdd3de",
          primary="#6699cc", secondary="#c594c5", accent="#f99157",
          success="#99c794", warning="#fac863", error="#ec5f67"),
    Theme(name="palenight", dark=True,
          background="#292d3e", surface="#242736", panel="#333747", foreground="#a6accd",
          primary="#82aaff", secondary="#c792ea", accent="#f78c6c",
          success="#c3e88d", warning="#ffcb6b", error="#f07178"),
]

# Semantic accent colours for the CURRENT theme (filesystem=green, size/mountpoint=
# amber, label=pink). Resolved from the active theme's readable "$text-*" variables
# on every theme change (MountApp._sync_accent), so coloured text stays legible on
# light themes too — Rich markup can't reference $variables, so we inject hex.
# Defaults match mounter-dark.
ACCENT = {"green": "#3CE88F", "amber": "#FFC247", "pink": "#FF7AC6"}


def _gb(size_bytes) -> str:
    try:
        return f"{round((size_bytes or 0) / 1024**3, 1)} GB"
    except Exception:
        return "?"


def _disk_label(d: dict) -> str:
    """Disk label in the tree: model in white, size in amber (important), rest dim."""
    model = d.get("Model") or "?"
    return (f"💽 [b]{model}[/]   [b {ACCENT['amber']}]{_gb(d.get('Size'))}[/]"
            f"   [dim]{d.get('DeviceID', '')}[/]")


def _part_label(p: dict) -> str:
    """Partition label: name white, fs green, label pink, size dim."""
    bits = [f"[b]{p.get('name', '')}[/]", f"[b {ACCENT['green']}]{p.get('fstype') or '—'}[/]"]
    if p.get("label"):
        bits.append(f"[{ACCENT['pink']}]{p['label']}[/]")
    if p.get("size"):
        bits.append(f"[dim]{p['size']}[/]")
    return "   ".join(bits)


def _part_label_mounted(p: dict) -> str:
    """Label for an already-mounted partition (shown for context, not actionable)."""
    mp = mount_target(p) or ""
    fs = p.get("fstype") or "—"
    return (f"[{ACCENT['green']}]●[/] [b]{p.get('name', '')}[/]   "
            f"[{ACCENT['green']}]{fs}[/]   [dim]mounted · {mp}[/]")


class MountTree(Tree):
    """Tree where Enter performs the contextual action — expand/collapse a disk,
    or mount/unmount the selected partition. A mouse click only selects."""
    BINDINGS = [Binding("enter", "activate", "Mount / Unmount", show=False)]

    def action_activate(self) -> None:
        node = self.cursor_node
        if node is None:
            return
        data = node.data or {}
        if data.get("type") == "disk":
            node.collapse() if node.is_expanded else node.expand()
        else:
            self.app.activate_selection()


class ActivateTable(DataTable):
    """DataTable where Enter activates the focused selection (mount a profile /
    unmount a mount). A mouse click only selects."""
    BINDINGS = [Binding("enter", "activate", "Activate", show=False)]

    def action_activate(self) -> None:
        self.app.activate_selection()


class _HelpClose(Static):
    """Clickable ✕ that closes the keys/help panel."""
    def on_click(self) -> None:
        self.app.action_hide_help_panel()


class HelpPanelWithClose(HelpPanel):
    """Textual's keys/help panel with a clickable ✕ close button in the top-right."""
    DEFAULT_CSS = """
    /* Replace the default `vkey` left border — its keycap glyphs render as broken
       boxes in most terminal fonts. Use a plain thin line as the separator. */
    HelpPanelWithClose {
        border-left: solid #3d3d45;
    }
    /* A 1-row top bar that right-aligns ONLY the ✕ — so the strip left of the
       button is not clickable, and the button sits one cell in from the edge. */
    HelpPanelWithClose #help-topbar {
        dock: top;
        height: 1;
        align-horizontal: right;
        background: $surface;
    }
    HelpPanelWithClose #help-close {
        width: auto;
        height: 1;
        padding: 0 1;          /* a cell of breathing room each side of the glyph */
        color: $text-muted;
    }
    HelpPanelWithClose #help-close:hover {
        /* colour only — NOT bold: a bold glyph renders wider and clips at the edge */
        color: $error;
    }
    """

    def compose(self) -> ComposeResult:
        with Horizontal(id="help-topbar"):
            yield _HelpClose("✕", id="help-close")
        yield from super().compose()


class _ThemeClose(Static):
    """Clickable ✕ that closes the theme picker (keeping the current theme)."""
    def on_click(self) -> None:
        try:
            self.app.query_one(ThemePicker).action_keep()
        except Exception:
            pass


class _ThemeList(OptionList):
    """Theme list where Enter KEEPS the highlighted theme and closes the picker.
    A mouse click only previews (it posts OptionSelected, handled on the picker
    without closing) — so Enter and click intentionally differ."""
    BINDINGS = [Binding("enter", "confirm", "Keep", show=False)]

    def action_confirm(self) -> None:
        try:
            self.app.query_one(ThemePicker).action_keep()
        except Exception:
            pass


class ThemePicker(Vertical):
    """Theme switcher docked on the RIGHT — same spot as the keys panel — so the
    whole app stays visible and re-themes live as you move through the list:
      ↑↓ preview · Enter/click keep · Esc (or ✕) close.
    Esc reverts to the theme that was active when the picker opened."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    DEFAULT_CSS = """
    ThemePicker {
        split: right;
        width: 34;
        min-width: 26;
        max-width: 50;
        border-left: solid #3d3d45;   /* plain line, like the keys panel */
        background: $surface;
        padding: 0;
    }
    ThemePicker #theme-topbar { dock: top; height: 1; background: $surface; }
    ThemePicker #theme-title { width: 1fr; color: $accent; text-style: bold; padding: 0 1; }
    ThemePicker #theme-close { width: auto; padding: 0 1; color: $text-muted; }
    ThemePicker #theme-close:hover { color: $error; }
    ThemePicker #theme-hint { dock: bottom; height: 1; color: $text-muted; padding: 0 1; }
    ThemePicker OptionList {
        height: 1fr;
        background: $surface;
        border: none;
        padding: 0;
        overflow-x: hidden;          /* no horizontal scrollbar */
    }
    ThemePicker OptionList:focus { border: none; }
    /* amber selection, matching the tree/table cursor in the rest of the app */
    ThemePicker OptionList > .option-list--option-highlighted {
        background: $accent;
        color: $background;
        text-style: bold;
    }
    """

    def __init__(self, original: str) -> None:
        super().__init__()
        self._original = original   # theme to restore on cancel

    def compose(self) -> ComposeResult:
        with Horizontal(id="theme-topbar"):
            yield Static("Theme", id="theme-title")
            yield _ThemeClose("✕", id="theme-close")
        yield _ThemeList(
            *[Option(name, id=name) for name in sorted(self.app.available_themes)],
            id="theme-list")
        yield Static("↑↓ preview · enter keep · esc cancel", id="theme-hint")

    def on_mount(self) -> None:
        names = sorted(self.app.available_themes)
        ol = self.query_one(OptionList)
        if self._original in names:
            ol.highlighted = names.index(self._original)
        ol.focus()

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if event.option_id:
            self.app.theme = event.option_id   # live preview on the whole app
            self.app.refresh_theme_colors()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        # Clicking only applies the theme (preview) — it does NOT close the picker,
        # so you can keep trying others. Close with Enter (keep), ✕, or Esc (cancel).
        if event.option_id:
            self.app.theme = event.option_id
            self.app.refresh_theme_colors()

    def action_keep(self) -> None:
        profiles.remember_theme(self.app.theme)   # persist across restarts
        self._close()

    def action_cancel(self) -> None:
        self.app.theme = self._original        # revert the preview
        self.app.refresh_theme_colors()
        self._close()

    def _close(self) -> None:
        tree = None
        try:
            tree = self.app.query_one("#tree")
        except Exception:
            pass
        self.remove()
        if tree is not None:
            tree.focus()


class AppFooter(Footer):
    """Footer that pushes the global actions (Menu, Quit) to the RIGHT, beside the
    command-palette key — they're app-wide, not tied to any panel. The panel
    actions (mount/unmount/…/refresh) stay on the left. A flexible spacer inserted
    before the first global key fills the gap (the palette key is docked right)."""

    DEFAULT_CSS = """
    AppFooter .-footer-spacer { width: 1fr; height: 1; background: $footer-background; }
    """

    _RIGHT = {"menu", "quit"}

    def compose(self) -> ComposeResult:
        # super().compose() yields FooterKey/KeyGroup/FooterLabel in binding order;
        # insert one flexible spacer just before the first global key (FooterKey
        # carries an `.action` attribute).
        spaced = False
        for widget in super().compose():
            if not spaced and getattr(widget, "action", None) in self._RIGHT:
                yield Static("", classes="-footer-spacer")
                spaced = True
            yield widget


# All command titles the palette can show — used to size the title column so every
# description lines up in one column (titles are right-aligned within that column).
PALETTE_TITLES = ("Mount", "Unmount", "Open in Explorer", "Open in WSL terminal",
                  "Save profile", "Delete profile", "Refresh disks", "About",
                  "Theme", "Quit", "Keys", "Minimize", "Maximize", "Screenshot")
PALETTE_COL = max(len(t) for t in PALETTE_TITLES)


_PALETTE_GAP = 3                       # spaces between title column and description
_PALETTE_INDENT = PALETTE_COL + _PALETTE_GAP   # column where the description starts


def _palette_row(title, help_text: str = "", width: int | None = None) -> Text:
    """Right-align `title` in a fixed-width column, then the dim description — so
    every description starts at the same column across all palette entries. If
    `width` (the option's text width) is given, a long description is wrapped with
    a HANGING INDENT: continuation lines line up under the description column
    instead of falling back to the left edge."""
    title_text = title if isinstance(title, Text) else Text(title)
    pad = max(0, PALETTE_COL - len(title_text.plain))
    row = Text(" " * pad)
    row.append_text(title_text)
    if not help_text:
        return row
    desc_width = (width - _PALETTE_INDENT) if width else 0
    if desc_width >= 8:
        lines = textwrap.wrap(help_text, desc_width) or [help_text]
    else:
        lines = [help_text]
    row.append(" " * _PALETTE_GAP)
    row.append(lines[0], style="dim")
    for cont in lines[1:]:
        row.append("\n" + " " * _PALETTE_INDENT)
        row.append(cont, style="dim")
    return row


def _palette_width(app) -> int:
    """Approximate the palette option's text width (min of the 90-col max-width CSS
    cap and the terminal, less option padding + scrollbar) for description wrapping."""
    return min(90, app.size.width) - 6


class AppCommands(Provider):
    """Command-palette provider exposing the app's own actions for the panel that
    was focused when the palette opened — alongside Textual's system commands."""

    def _items(self):
        app = self.app
        # Provider.focused = the calling screen's focused widget (the panel that had
        # focus before the palette opened), not the palette input.
        for title, action, help_text in app.palette_actions(self.focused):
            yield title, getattr(app, f"action_{action}"), help_text

    async def discover(self):
        w = _palette_width(self.app)
        for title, callback, help_text in self._items():
            yield DiscoveryHit(_palette_row(title, help_text, w), callback, text=title)

    async def search(self, query: str):
        matcher = self.matcher(query)
        w = _palette_width(self.app)
        for title, callback, help_text in self._items():
            score = matcher.match(title)
            if score > 0:
                yield Hit(score, _palette_row(matcher.highlight(title), help_text, w),
                          callback, text=title)


class InlineSystemCommands(Provider):
    """Textual's system commands (theme/quit/keys/screenshot/maximize) rendered on
    ONE line each, with the description dimmed inline. Replaces Textual's default
    two-line (title over help) layout, which runs together and is hard to read."""

    def _commands(self):
        return list(self.app.get_system_commands(self.screen))

    async def discover(self):
        w = _palette_width(self.app)
        for cmd in self._commands():
            if cmd.discover:
                yield DiscoveryHit(_palette_row(cmd.title, cmd.help, w), cmd.callback, text=cmd.title)

    async def search(self, query: str):
        matcher = self.matcher(query)
        w = _palette_width(self.app)
        for cmd in self._commands():
            score = matcher.match(cmd.title)
            if score > 0:
                yield Hit(score, _palette_row(matcher.highlight(cmd.title), cmd.help, w),
                          cmd.callback, text=cmd.title)


class MenuScreen(ModalScreen, inherit_css=False):
    """Small popup menu (the footer 'Menu' button): theme, keys panel, screenshot,
    quit. Selecting an item runs it; Esc / click-away closes.

    inherit_css=False so it does NOT pick up Screen's opaque `background: $background`
    rule — that forced the backdrop opaque (the app showed as solid black around the
    menu). Without it, `background: transparent` truly lets the app show through."""

    BINDINGS = [Binding("escape", "dismiss", "Close")]

    DEFAULT_CSS = """
    /* transparent backdrop so the app stays fully visible under the menu */
    MenuScreen { layout: vertical; align: center middle; background: transparent; }
    MenuScreen > Vertical {
        width: 32;
        height: auto;
        border: solid $accent;
        background: $surface;
        padding: 0;
    }
    MenuScreen #menu-title {
        height: 1; padding: 0 1; background: $surface; color: $accent; text-style: bold;
    }
    MenuScreen OptionList {
        height: auto;
        max-height: 12;
        background: $surface;
        border: none;
        padding: 0;
        overflow-x: hidden;
    }
    MenuScreen OptionList:focus { border: none; }
    MenuScreen OptionList > .option-list--option-highlighted {
        background: $accent; color: $background; text-style: bold;
    }
    """

    ITEMS = [("Theme…", "theme"),
             ("Keys / help panel", "keys"),
             ("Save screenshot", "screenshot"),
             ("About", "about"),
             ("Quit", "quit")]

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("Menu", id="menu-title")
            yield OptionList(*[Option(t, id=a) for t, a in self.ITEMS], id="menu-list")

    def on_mount(self) -> None:
        self.query_one(OptionList).focus()

    def on_click(self, event) -> None:
        # Click outside the menu box closes the menu.
        box = self.query_one(Vertical).region
        if not box.contains(event.screen_x, event.screen_y):
            self.dismiss()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        action = event.option_id
        app = self.app
        self.dismiss()
        if action == "theme":
            app.action_change_theme()
        elif action == "keys":
            app.action_show_help_panel()
        elif action == "screenshot":
            app.action_screenshot()
        elif action == "about":
            app.action_about()
        elif action == "quit":
            app.action_quit()


class AboutScreen(ModalScreen, inherit_css=False):
    """Small centered popup with the app name, version and developer. Esc, Enter or
    a click anywhere closes it.

    inherit_css=False so it does NOT pick up Screen's opaque `background` rule (which
    would paint the backdrop solid); a translucent backdrop keeps the app visible."""

    BINDINGS = [Binding("escape", "dismiss", "Close"),
                Binding("enter", "dismiss", "Close", show=False)]

    DEFAULT_CSS = """
    AboutScreen { layout: vertical; align: center middle; background: $background 40%; }
    AboutScreen > Vertical {
        width: 50;
        height: auto;
        border: round $accent;
        background: $surface;
        padding: 1 2;
    }
    AboutScreen #about-title { height: 1; color: $accent; text-style: bold; }
    AboutScreen #about-body { height: auto; color: $foreground; padding: 1 0 0 0; }
    AboutScreen #about-hint { height: 1; color: $text-muted; padding: 1 0 0 0; }
    """

    def compose(self) -> ComposeResult:
        body = Text()
        body.append("Version    ", style="dim")
        body.append(f"{__version__}\n")
        body.append("Developer  ", style="dim")
        body.append(f"{__author__}\n")
        body.append("Email      ", style="dim")
        body.append(__email__)
        with Vertical():
            yield Static("WSL Mount Manager", id="about-title")
            yield Static(body, id="about-body")
            yield Static("Esc / Enter / click to close", id="about-hint")

    def on_click(self) -> None:
        self.dismiss()


class MountApp(App):
    TITLE = "WSL Mount Manager"
    SUB_TITLE = "mount Linux partitions into WSL"

    # Command palette = this app's contextual actions + system commands rendered
    # single-line (InlineSystemCommands replaces Textual's two-line system provider).
    COMMANDS = {AppCommands, InlineSystemCommands}

    CSS = """
    Screen {
        background: $background;
        scrollbar-color: #4a4a52;
        scrollbar-background: $surface;
    }

    #left  { width: 3fr; }
    #right { width: 2fr; }

    .panel {
        border: round #3d3d45;            /* grey panel outlines */
        border-title-color: $accent;
        border-title-align: left;
        background: $surface;
        padding: 0 1;
        margin: 1 1 0 1;
        height: 1fr;
    }
    .panel:focus-within { border: round #5c5c68; }   /* active panel: slightly brighter outline */
    #name-panel { height: 4; }
    #name-panel Input { border: none; background: $boost; color: $accent; text-style: bold; }

    /* grey scrollbars instead of green */
    Tree, DataTable {
        background: $surface;
        /* Fill the panel (DataTable defaults to height:auto, so without this a click
           on the empty area below the rows lands on the container, not the table, and
           the panel doesn't get focus the way the always-filled tree does). */
        height: 1fr;
        scrollbar-color: #4a4a52;
        scrollbar-color-hover: #5e5e68;
        scrollbar-color-active: #7a7a86;
        scrollbar-background: $surface;
        scrollbar-background-hover: $surface;
        scrollbar-background-active: $surface;
    }
    Tree { padding: 0 1; }
    /* selection cursor: amber on the FOCUSED panel, grey on inactive ones */
    /* Inactive (blurred) row cursor: theme-aware so light themes stay readable
       (a hardcoded dark grey made dark $foreground text invisible on light themes).
       mounter-dark pins block-cursor-blurred-background to its usual grey. */
    Tree > .tree--cursor { background: $block-cursor-blurred-background; color: $block-cursor-blurred-foreground; }
    Tree:focus > .tree--cursor { background: $accent; color: $background; text-style: bold; }
    DataTable > .datatable--cursor { background: $block-cursor-blurred-background; color: $block-cursor-blurred-foreground; }
    DataTable:focus > .datatable--cursor { background: $accent; color: $background; text-style: bold; }

    /* highlight footer keys on mouse hover */
    FooterKey:hover { background: $accent; color: $background; }
    FooterKey:hover .footer-key--key { background: $accent; color: $background; }
    FooterKey:hover .footer-key--description { background: $accent; color: $background; }

    /* single in-place status line (replaces stacking toast notifications) */
    #status { height: 1; padding: 0 2; background: $surface; color: $foreground; }

    /* command palette (Ctrl+P): cap width so its list isn't stretched full-screen.
       The CommandPalette screen already centres horizontally, so a max-width is
       enough — it stays full width on narrow terminals and centres on wide ones. */
    CommandPalette > Vertical { max-width: 90; }
    """

    BINDINGS = [
        ("m", "mount", "Mount"),
        ("u", "unmount", "Unmount"),
        ("e", "explorer", "Explorer"),
        ("t", "terminal", "Terminal"),
        ("p", "save_profile", "Save profile"),
        ("d", "delete_profile", "Delete profile"),
        ("r", "refresh", "Refresh"),
        ("ctrl+o", "menu", "Menu"),
        ("q", "quit", "Quit"),
        # Global actions — shown in the keys/help panel (show=False keeps the footer
        # uncluttered). Maximize/minimize act on the focused panel.
        Binding("plus", "maximize", "Maximize the focused panel", key_display="+", show=False),
        Binding("minus", "minimize", "Restore panel size", key_display="-", show=False),
        Binding("ctrl+t", "change_theme", "Change theme", show=False),
        Binding("ctrl+s", "screenshot", "Save a screenshot", show=False),
        Binding("question_mark", "keys_panel", "Keys / help panel", key_display="?", show=False),
        Binding("f1", "about", "About", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.profs: list[dict] = []
        self.mounted: list[dict] = []
        self.drives: list[dict] = []  # last enumerated Windows disks (cached for profiles)
        self.bare: set[str] = set()   # disks left in bare mode (for browsing partitions)
        self.busy = False

    # ----- layout -------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="main"):
            with Vertical(id="left"):
                with Vertical(id="tree-panel", classes="panel"):
                    yield MountTree("disks", id="tree")
                with Vertical(id="name-panel", classes="panel"):
                    yield Input(value="data", id="name",
                                placeholder="mount name in /mnt/wsl/")
            with Vertical(id="right"):
                with Vertical(id="mounted-panel", classes="panel"):
                    yield ActivateTable(id="mounted", cursor_type="row", zebra_stripes=True)
                with Vertical(id="profiles-panel", classes="panel"):
                    yield ActivateTable(id="profiles", cursor_type="row", zebra_stripes=True)
        yield Label("", id="status")
        yield AppFooter()

    def on_mount(self) -> None:
        self.register_theme(MOUNTER_THEME)
        for th in EXTRA_THEMES:
            self.register_theme(th)
        # Restore the theme chosen last session (if it still exists).
        saved = profiles.saved_theme()
        self.theme = saved if saved in self.available_themes else "mounter-dark"
        self._sync_accent()
        self.query_one("#tree-panel").border_title = "Disks & partitions  (Enter expands a disk · select a partition, then use the footer)"
        self.query_one("#name-panel").border_title = "Mount name  /mnt/wsl/"
        self.query_one("#mounted-panel").border_title = "Mounted now"
        self.query_one("#profiles-panel").border_title = "Profiles  (Enter / m — mount · d — delete)"

        tree = self.query_one("#tree", Tree)
        tree.show_root = False
        tree.guide_depth = 3

        self.query_one("#mounted", DataTable).add_columns("Name", "Mount point", "FS")
        self.query_one("#profiles", DataTable).add_columns("Profile", "Name", "FS")

        tree.focus()
        self.refresh_all()
        self.set_interval(5.0, self.refresh_mounted)

    # ----- status line (replaces stacking toast notifications) ----------
    def _set_status(self, message: str, severity: str = "information") -> None:
        try:
            label = self.query_one("#status", Label)
        except Exception:
            return
        color = {"error": "#FF5C6C", "warning": ACCENT["amber"]}.get(severity, "")
        label.update(Text(str(message).replace("\n", "  "), style=color or ""))

    def notify(self, message: str, *, title: str = "", severity: str = "information",
               timeout: float | None = None, markup: bool = True) -> None:
        # Route every notification to the single in-place status line instead of
        # stacking toast pop-ups in the bottom-right corner.
        self._set_status(message, severity)

    def action_show_help_panel(self) -> None:
        # Mount our subclass (adds a ✕ close button) INSIDE the main row, not on the
        # screen — so its `split: right` only shrinks the panels, leaving the status
        # line and the full-width Footer (palette bar) untouched below it.
        try:
            self.screen.query_one(HelpPanel)
        except Exception:
            self.query_one("#main", Horizontal).mount(HelpPanelWithClose())

    def action_keys_panel(self) -> None:
        # Toggle the keys / help panel (the '?' hotkey and the Menu / palette entry).
        if self.screen.query("HelpPanel"):
            self.action_hide_help_panel()
        else:
            self.action_show_help_panel()

    def action_maximize(self) -> None:
        # '+' — maximize the focused panel (delegate to the screen's action).
        self.screen.action_maximize()

    def action_minimize(self) -> None:
        # '-' — restore the maximized panel.
        self.screen.action_minimize()

    def action_change_theme(self) -> None:
        # Open a theme picker docked on the right (same place as the keys panel) so
        # the app stays visible and previews live. Toggle: a second invocation
        # cancels (reverts to the current theme).
        existing = self.query("ThemePicker")
        if existing:
            existing.first().action_cancel()
        else:
            self.query_one("#main", Horizontal).mount(ThemePicker(self.theme))

    def action_menu(self) -> None:
        # The footer 'Menu' button: theme / keys panel / screenshot / about / quit.
        if not isinstance(self.screen, MenuScreen):
            self.push_screen(MenuScreen())

    def action_about(self) -> None:
        # Show the About popup (app name, version, developer).
        if not isinstance(self.screen, AboutScreen):
            self.push_screen(AboutScreen())

    def get_key_display(self, binding) -> str:
        # Some key glyphs (notably ⏎ for Enter) render as a blank box in most
        # terminal fonts. Swap those for readable names in the footer / keys panel.
        display = super().get_key_display(binding)
        return display.replace("⏎", "enter").replace("⌫", "bksp")

    # ----- data loading -------------------------------------------------
    def action_refresh(self) -> None:
        self.refresh_all()

    @work(thread=True, exclusive=True, group="load")
    def refresh_all(self) -> None:
        for dev in list(self.bare):   # drop previous bare mounts before a clean enumeration
            engine.detach(dev)
        self.bare.clear()
        try:
            drives = engine.list_drives()
        except Exception as e:
            self.call_from_thread(self.notify, f"Disks: {e}", severity="error")
            drives = []
        mounted = engine.list_mounted()
        profs = profiles.list_profiles()
        self.call_from_thread(self._rebuild_tree, drives)
        self.call_from_thread(self._fill_mounted, mounted)
        self.call_from_thread(self._fill_profiles, profs)

    @work(thread=True, exclusive=True, group="status")
    def refresh_mounted(self) -> None:
        if self.busy:
            return
        mounted = engine.list_mounted()
        self.call_from_thread(self._fill_mounted, mounted)

    def _refresh_after(self, device_id: str | None) -> None:
        # After a mount/unmount: update the Mounted panel and reload the affected
        # disk's partitions IN PLACE — the tree stays expanded, only state changes.
        try:
            mounted = engine.list_mounted()
        except Exception:
            mounted = []
        self._fill_mounted(mounted)
        try:
            self._reload_disk(device_id)
        except Exception:
            pass

    def _reload_disk(self, device_id: str | None) -> None:
        if not device_id:
            return
        tree = self.query_one("#tree", Tree)
        for node in tree.root.children:
            data = node.data or {}
            if (data.get("type") == "disk"
                    and data.get("drive", {}).get("DeviceID") == device_id
                    and data.get("loaded") and node.is_expanded and data.get("lsblk")):
                parts = engine.partitions_by_lsblk_name(data["lsblk"]) or []
                self._populate_children(node, data["drive"], parts)
                return

    def _rebuild_tree(self, drives: list[dict]) -> None:
        self.drives = drives   # cache for fast profile mounting (avoids a 2nd list_drives)
        tree = self.query_one("#tree", Tree)
        tree.clear()
        for d in drives:
            tree.root.add(_disk_label(d), data={"type": "disk", "drive": d, "loaded": False})
        if drives:
            tree.cursor_line = 0   # highlight the first disk
        self.refresh_bindings()

    def _fill_mounted(self, mounted: list[dict], force: bool = False) -> None:
        # The 5 s background poll calls this repeatedly. DataTable.clear() resets
        # the cursor to row 0, so blindly rebuilding would wipe the user's
        # selection (and make 'p'/'u' act on the wrong row). Skip the rebuild when
        # nothing changed, and otherwise preserve the selection by mount name.
        # force=True rebuilds even when unchanged (used after a theme switch, to
        # re-render the cells with the new theme's accent colours).
        t = self.query_one("#mounted", DataTable)
        if not force and mounted == self.mounted:
            return
        prev_name = None
        if 0 <= t.cursor_row < len(self.mounted):
            prev_name = self.mounted[t.cursor_row]["name"]
        self.mounted = mounted
        t.clear()
        for m in mounted:
            name_cell = Text.from_markup(f"[{ACCENT['green']}]●[/] [b]{m['name']}[/]")
            mp_cell = Text.from_markup(f"[{ACCENT['amber']}]{m['mountpoint']}[/]")
            t.add_row(name_cell, mp_cell, m["fstype"] or "—")
        if prev_name is not None:
            idx = next((i for i, m in enumerate(mounted) if m["name"] == prev_name), None)
            if idx is not None:
                t.move_cursor(row=idx, scroll=False)

    def _fill_profiles(self, profs: list[dict], force: bool = False) -> None:
        # Same selection-preservation as _fill_mounted: a save/delete refills this
        # table, and clear() would snap the cursor back to row 0. Preserve the
        # selected profile by its label. force=True rebuilds even when unchanged
        # (after a theme switch — though profile cells aren't accent-coloured, kept
        # symmetric with _fill_mounted).
        t = self.query_one("#profiles", DataTable)
        if not force and profs == self.profs:
            return
        prev_label = None
        if 0 <= t.cursor_row < len(self.profs):
            prev_label = self.profs[t.cursor_row].get("label")
        self.profs = profs
        t.clear()
        for p in profs:
            t.add_row(p.get("label", ""), p.get("name", ""), p.get("fstype", ""))
        if prev_label is not None:
            idx = next((i for i, p in enumerate(profs) if p.get("label") == prev_label), None)
            if idx is not None:
                t.move_cursor(row=idx, scroll=False)

    # ----- theme-aware accent colours -----------------------------------
    def _sync_accent(self) -> None:
        """Resolve the current theme's readable accent text colours into ACCENT."""
        v = self.get_css_variables()
        ACCENT["green"] = v.get("text-success", ACCENT["green"])
        ACCENT["amber"] = v.get("text-warning", ACCENT["amber"])
        ACCENT["pink"] = v.get("text-secondary", ACCENT["pink"])

    def refresh_theme_colors(self) -> None:
        """After a theme switch, re-resolve accent colours and re-render the
        coloured content (tree labels + tables) so it stays legible — Rich markup
        bakes in hex, so it doesn't follow a theme change on its own."""
        self._sync_accent()
        self._fill_mounted(list(self.mounted), force=True)
        self._fill_profiles(list(self.profs), force=True)
        self._relabel_tree()

    def _relabel_tree(self) -> None:
        """Re-render tree node labels in place (keeps expansion + selection)."""
        try:
            tree = self.query_one("#tree", Tree)
        except Exception:
            return
        for disk_node in tree.root.children:
            data = disk_node.data or {}
            if data.get("type") != "disk":
                continue
            disk_node.set_label(_disk_label(data["drive"]))
            for child in list(disk_node.children):
                cd = child.data or {}
                if cd.get("type") == "part":
                    child.set_label(_part_label(cd["part"]))
                elif cd.get("type") == "mounted":
                    child.set_label(_part_label_mounted(cd["part"]))

    # ----- tree: expand / select / highlight ----------------------------
    def on_tree_node_expanded(self, event: Tree.NodeExpanded) -> None:
        data = event.node.data or {}
        if data.get("type") != "disk" or data.get("loaded"):
            return
        data["loaded"] = True
        event.node.add_leaf("[dim]⏳ loading…[/]")
        self._load_disk(event.node, data["drive"])

    def on_tree_node_collapsed(self, event: Tree.NodeCollapsed) -> None:
        data = event.node.data or {}
        if data.get("type") != "disk":
            return
        data["loaded"] = False
        event.node.remove_children()
        dev = data["drive"]["DeviceID"]
        if dev in self.bare:
            self.bare.discard(dev)
            self._detach(dev)

    @work(thread=True, group="tree")
    def _detach(self, dev: str) -> None:
        engine.detach(dev)

    @work(thread=True, exclusive=False, group="tree")
    def _load_disk(self, node, drive: dict) -> None:
        parts, err = engine.disk_partitions(drive)
        self.call_from_thread(self._show_parts, node, drive, parts, err)

    def _populate_children(self, node, drive: dict, parts: list[dict]) -> int:
        """(Re)build a disk node's partition children in place, keeping it expanded.
        Mounted partitions are shown marked; free ones are mountable. Returns the
        number of free (mountable) partitions."""
        node.remove_children()
        mountable = 0
        for p in parts:
            if engine.is_mounted(p):
                node.add_leaf(_part_label_mounted(p),
                              data={"type": "mounted", "part": p, "drive": drive})
            elif engine._is_candidate(p):
                node.add_leaf(_part_label(p), data={"type": "part", "part": p, "drive": drive})
                mountable += 1
        if not node.children:
            node.add_leaf("[dim]no mountable partitions[/]")
        if parts:
            node.data["lsblk"] = parts[0].get("_parent") or node.data.get("lsblk")
        self.refresh_bindings()
        return mountable

    def _show_parts(self, node, drive: dict, parts: list[dict], err: str) -> None:
        if err:
            node.remove_children()
            node.add_leaf(f"[red]{err.splitlines()[0]}[/]")
            self.notify(err, severity="error")
            return
        # Track only browse-attached disks (no active mounts) for later cleanup;
        # disks with live mounts must never be detached.
        if not profiles.disk_in_use(drive["DeviceID"]):
            self.bare.add(drive["DeviceID"])
        mountable = self._populate_children(node, drive, parts)
        self.notify(f"{len(parts)} partition(s), {mountable} mountable — select one, Enter to mount")

    def _focused_selection(self, focused=None) -> tuple:
        """
        Derive the current selection from the FOCUSED widget's cursor, so the
        non-focused panel's auto-highlight cannot hijack it. `focused` overrides
        self.focused (used by the command palette, where focus is on the palette
        but we want the panel that was focused when it opened).
        Returns ('part', part, drive) | ('mounted', mount, None) | (None, None, None).
        """
        f = focused if focused is not None else self.focused
        try:
            tree = self.query_one("#tree", Tree)
            mounted = self.query_one("#mounted", DataTable)
        except Exception:
            return (None, None, None)
        if f is tree:
            node = tree.cursor_node
            data = (node.data or {}) if node else {}
            if data.get("type") == "part":
                return ("part", data["part"], data["drive"])
            if data.get("type") == "mounted":
                p = data["part"]
                mp = engine.mount_target(p) or ""
                return ("mounted", {"name": mp.rsplit("/", 1)[-1], "mountpoint": mp,
                                    "fstype": p.get("fstype") or "", "label": p.get("label") or "",
                                    "size": p.get("size") or "", "source": p.get("name", "")}, None)
        elif f is mounted:
            m = self._mount_at(mounted.cursor_row)
            if m:
                return ("mounted", m, None)
        return (None, None, None)

    def on_tree_node_highlighted(self, event: Tree.NodeHighlighted) -> None:
        # Selecting a node never mounts; it just prefills the name and updates the
        # footer so the right action (m / u / e / t) is highlighted.
        data = event.node.data or {}
        if data.get("type") == "part":
            self.query_one("#name", Input).value = data["part"].get("label") or "data"
        self.refresh_bindings()

    def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        # A click or Enter only SELECTS — it must not mount. Mount/Unmount are
        # explicit: press the key the footer highlights. Enter on a disk node still
        # expands/collapses it (handled by the Tree itself).
        pass

    def on_descendant_focus(self, event) -> None:
        # Footer depends on which panel is focused — refresh it on focus changes.
        self.refresh_bindings()

    def _profiles_focused(self, focused=None) -> bool:
        f = focused if focused is not None else self.focused
        try:
            return f is self.query_one("#profiles", DataTable)
        except Exception:
            return False

    def palette_actions(self, focused) -> list[tuple[str, str, str]]:
        """[(title, action_name, help)] valid for `focused` panel — used to list the
        active panel's actions in the command palette."""
        kind = self._focused_selection(focused)[0]
        on_profile = self._profiles_focused(focused) and self._selected_profile() is not None
        items: list[tuple[str, str, str]] = []
        if kind == "part" or on_profile:
            items.append(("Mount", "mount", "Mount the selected partition / profile into WSL"))
        if kind == "mounted":
            items += [("Unmount", "unmount", "Unmount the selected mount"),
                      ("Open in Explorer", "explorer", "Open the mount in Windows Explorer"),
                      ("Open in WSL terminal", "terminal", "Open a WSL terminal at the mount")]
        if kind in ("part", "mounted"):
            items.append(("Save profile", "save_profile", "Save the selection as a reusable profile"))
        if on_profile:
            items.append(("Delete profile", "delete_profile", "Delete the selected profile"))
        items.append(("Refresh disks", "refresh", "Re-scan disks and current mounts"))
        items.append(("About", "about", "App version and developer"))
        return items

    def _selected_profile(self) -> dict | None:
        try:
            t = self.query_one("#profiles", DataTable)
        except Exception:
            return None
        idx = t.cursor_row
        return self.profs[idx] if 0 <= idx < len(self.profs) else None

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        # Contextual footer: HIDE actions that don't apply to the focused panel /
        # selection (return False), instead of dimming them. r and q always show.
        kind = self._focused_selection()[0]
        on_profile = self._profiles_focused() and self._selected_profile() is not None
        if action == "mount":
            return kind == "part" or on_profile
        if action == "save_profile":
            return kind in ("part", "mounted")
        if action == "delete_profile":
            return on_profile
        if action in ("unmount", "explorer", "terminal"):
            return kind == "mounted"
        return True

    # ----- mount / unmount actions --------------------------------------
    def _begin_op(self) -> bool:
        """Claim the single-operation slot synchronously, on the UI thread.

        Mount/unmount/save-profile workers share group="op" (exclusive). Setting
        ``busy`` here — before the worker thread starts — closes the window where a
        second keypress could dispatch (and exclusive-cancel) an op that had not
        yet set the flag from inside its own thread, and keeps the 5 s poll's
        busy-check reliable. The worker clears the flag in its ``finally``.
        Returns False (and warns) if an operation is already running.
        """
        if self.busy:
            self.notify("Busy — wait for the current operation to finish.", severity="warning")
            return False
        self.busy = True
        return True

    def activate_selection(self) -> None:
        # Shared Enter handler for the tree and the tables: act on the focused item.
        prof = self.query_one("#profiles", DataTable)
        if self.focused is prof:
            idx = prof.cursor_row
            if 0 <= idx < len(self.profs) and self._begin_op():
                self.mount_profile(self.profs[idx])
            return
        kind, a, b = self._focused_selection()
        if kind == "part":
            name = self.query_one("#name", Input).value.strip() or "data"
            if self._begin_op():
                self._do_mount(a, b, name)
        elif kind == "mounted":
            if self._begin_op():
                self._do_unmount(a)
        else:
            self.notify("Select a partition or a mounted item first.", severity="warning")

    def action_mount(self) -> None:
        if self._profiles_focused():
            p = self._selected_profile()
            if not p:
                self.notify("No profile selected.", severity="warning")
            elif self._begin_op():
                self.mount_profile(p)
            return
        kind, part, drive = self._focused_selection()
        if kind != "part":
            self.notify("Select a free partition (or a profile) to mount.", severity="warning")
            return
        name = self.query_one("#name", Input).value.strip() or "data"
        if self._begin_op():
            self._do_mount(part, drive, name)

    @work(thread=True, exclusive=True, group="op")
    def _do_mount(self, part: dict, drive: dict, name: str) -> None:
        # busy was claimed synchronously by the dispatcher (_begin_op); release here.
        try:
            partname = part.get("name")
            if not partname:
                self.call_from_thread(self.notify, "Partition has no device name.", severity="error")
                return
            fstype = part.get("fstype") or "ext4"
            taken = {m["name"] for m in engine.list_mounted()} | set(profiles.get_active().keys())
            uniq = engine.unique_mount_name(name, taken)
            if uniq != name:
                self.call_from_thread(self.notify, f"Name '{name}' is in use — mounting as '{uniq}'.",
                                      severity="warning")
            name = uniq
            self.call_from_thread(self.notify, f"Mounting {partname} → /mnt/wsl/{name} …")
            ok, mp, msg = engine.mount_partition(drive["DeviceID"], partname, fstype, name)
            if not ok:
                self.call_from_thread(self.notify, f"Error: {msg}", severity="error", timeout=8)
                return
            profiles.set_active(name, drive["DeviceID"], partname, fstype, mp)
            profiles.remember_drive(drive)
            profiles.remember_part(part)
            self.bare.discard(drive["DeviceID"])
            self.call_from_thread(self.notify, f"✓ Mounted: {mp}")
            self.call_from_thread(self._refresh_after, drive["DeviceID"])
        finally:
            self.busy = False

    # ----- tables: selection / Enter ------------------------------------
    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        # Selection is derived from focus; just refresh the footer so the right
        # actions light up for the highlighted mounted entry or profile.
        self.refresh_bindings()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        # A mouse click only selects (highlights). Acting is explicit: press Enter
        # (handled by ActivateTable.action_activate) or the footer keys.
        pass

    @work(thread=True, exclusive=True, group="op")
    def mount_profile(self, profile: dict) -> None:
        # busy was claimed synchronously by the dispatcher (_begin_op); release here.
        try:
            # Resolve the Windows disk from the cached drive list (no PowerShell on
            # the hot path); refresh once if a newly attached disk isn't cached yet.
            drives = self.drives or engine.list_drives()
            drive, _ = profiles.resolve_profile(profile, drives, [])
            if not drive:
                drives = engine.list_drives()
                drive, _ = profiles.resolve_profile(profile, drives, [])
            if not drive:
                self.call_from_thread(self.notify, f"Profile disk «{profile['label']}» not found.",
                                      severity="error")
                return
            # Fast path: if the disk is already attached/browsed, the partition is in
            # live lsblk — resolve it directly, skipping the slow failing bare-mount.
            part = engine.attached_partition_by_key(profile.get("part_key"))
            if part is None:
                # Disk not attached yet — enumerate it (this will bare-attach).
                parts, err = engine.disk_partitions(drive)
                if err:
                    self.call_from_thread(self.notify, err, severity="error", timeout=8)
                    return
                _, part = profiles.resolve_profile(profile, drives, parts)
            if not part:
                self.call_from_thread(self.notify, "Profile partition not found on the disk.",
                                      severity="error")
                return
            # Don't mount the same device twice (Linux would happily mount /dev/sdc2
            # at a second /mnt/wsl path); the tree panel can't do this, profiles could.
            mounted = engine.list_mounted()
            existing = next((m for m in mounted if m.get("source") == part["name"]), None)
            if existing:
                self.call_from_thread(
                    self.notify,
                    f"{part['name']} is already mounted as /mnt/wsl/{existing['name']} "
                    "— not mounting again.", severity="warning")
                return
            name = profile.get("name") or "data"
            fstype = part.get("fstype") or profile.get("fstype") or "ext4"
            taken = {m["name"] for m in mounted} | set(profiles.get_active().keys())
            name = engine.unique_mount_name(name, taken)
            self.call_from_thread(self.notify, f"Profile «{profile['label']}» → /mnt/wsl/{name} …")
            ok, mp, msg = engine.mount_partition(drive["DeviceID"], part["name"], fstype, name)
            if not ok:
                self.call_from_thread(self.notify, f"Error: {msg}", severity="error", timeout=8)
                return
            profiles.set_active(name, drive["DeviceID"], part["name"], fstype, mp)
            self.bare.discard(drive["DeviceID"])
            self.call_from_thread(self.notify, f"✓ Mounted: {mp}")
            self.call_from_thread(self._refresh_after, drive["DeviceID"])
        finally:
            self.busy = False

    # ----- save profile -------------------------------------------------
    def action_save_profile(self) -> None:
        kind, a, b = self._focused_selection()
        if kind == "part":
            part, drive = a, b
            name = self.query_one("#name", Input).value.strip() or "data"
            label = f"{(part.get('label') or part['name'])}→{name}"
            profiles.add_profile(label, drive, part, name)
            self.notify(f"✓ Profile saved: {label}  (Profiles panel: Enter mounts · d deletes)")
            self._fill_profiles(profiles.list_profiles())
        elif kind == "mounted":
            if self._begin_op():
                self._save_profile_from_mount(a)
        else:
            self.notify("Select a partition or a mounted item to save a profile.",
                        severity="warning")

    def _resolve_drive_for_mount(self, mount: dict) -> dict | None:
        """Find the Windows disk for a mounted entry: first via active.json, then
        (fallback) by matching the lsblk parent disk's size to a physical disk —
        enough for a profile, whose key is Model|Size anyway. Runs in a worker."""
        drives = engine.list_drives()
        device = profiles.device_for_mount(mount.get("name", ""))
        if device:
            d = next((x for x in drives if x["DeviceID"] == device), None)
            if d:
                return d
        source = mount.get("source", "")   # lsblk partition name, e.g. sdc3
        if source:
            try:
                for disk in engine.list_block_devices():
                    if any(c.get("name") == source for c in (disk.get("children") or [])):
                        disk_b = engine._size_to_bytes(disk.get("size"))
                        best, best_diff = None, 0.06
                        for x in drives:
                            wb = x.get("Size") or 0
                            if wb > 0 and abs(disk_b - wb) / wb < best_diff:
                                best, best_diff = x, abs(disk_b - wb) / wb
                        return best
            except Exception:
                pass
        return None

    @work(thread=True, exclusive=True, group="op")
    def _save_profile_from_mount(self, mount: dict) -> None:
        # Save a profile from an already-mounted entry: resolve its Windows disk,
        # take label/size/fstype from lsblk, keep the current mount name.
        # busy was claimed synchronously by the dispatcher (_begin_op); release here
        # — gating prevents this read-heavy worker from exclusive-cancelling an
        # in-flight mount/unmount in the shared "op" group.
        try:
            name = mount.get("name", "")
            drive = self._resolve_drive_for_mount(mount)
            if not drive:
                self.call_from_thread(
                    self.notify,
                    f"Can't resolve the Windows disk for “{name}” — profile not saved.",
                    severity="error")
                return
            part = {"name": mount.get("source", ""), "fstype": mount.get("fstype", ""),
                    "label": mount.get("label", ""), "size": mount.get("size", "")}
            label = f"{(mount.get('label') or name)}→{name}"
            profiles.add_profile(label, drive, part, name)
            self.call_from_thread(self.notify, f"✓ Profile saved: {label}")
            self.call_from_thread(self._fill_profiles, profiles.list_profiles())
        finally:
            self.busy = False

    def action_delete_profile(self) -> None:
        p = self._selected_profile()
        if not (self._profiles_focused() and p):
            self.notify("Focus the Profiles panel and select a profile to delete.",
                        severity="warning")
            return
        profiles.remove_profile(p.get("label", ""))
        # Pass the fresh list directly; _fill_profiles compares it against the
        # currently displayed self.profs to decide whether to rebuild. Assigning
        # self.profs first would make that comparison a no-op and skip the redraw.
        self._fill_profiles(profiles.list_profiles())
        self.notify(f"✓ Profile deleted: {p.get('label', '')}")
        self.refresh_bindings()

    # ----- actions on mounted entries -----------------------------------
    def _mount_at(self, idx: int) -> dict | None:
        return self.mounted[idx] if 0 <= idx < len(self.mounted) else None

    def _selected_mount(self) -> dict | None:
        # Current mounted selection — a mounted node in the tree or a row in the
        # Mounted panel, depending on which is focused.
        kind, mnt, _ = self._focused_selection()
        if kind != "mounted":
            self.notify("Select a mounted item (in the tree or the Mounted panel).",
                        severity="warning")
            return None
        return mnt

    def action_unmount(self) -> None:
        m = self._selected_mount()
        if m and self._begin_op():
            self._do_unmount(m)

    @work(thread=True, exclusive=True, group="op")
    def _do_unmount(self, m: dict) -> None:
        # busy was claimed synchronously by the dispatcher (_begin_op); release here.
        try:
            name = m["name"]
            device = profiles.device_for_mount(name)
            ok, msg = engine.unmount_mount(name)
            if not ok:
                self.call_from_thread(self.notify, f"Error: {msg}", severity="error", timeout=8)
                return
            profiles.clear_active(name)
            # disk stays attached so its other partitions/list survive; if nothing
            # of it is mounted anymore, mark it for cleanup on refresh/exit.
            if device and not profiles.disk_in_use(device):
                self.bare.add(device)
            self.call_from_thread(self.notify, f"✓ Unmounted: {name}")
            self.call_from_thread(self._refresh_after, device)
        finally:
            self.busy = False

    def action_explorer(self) -> None:
        m = self._selected_mount()
        if m:
            self._open_explorer(m)

    @work(thread=True, group="open")
    def _open_explorer(self, m: dict) -> None:
        # open_in_explorer resolves the WSL distro name (a blocking `wsl --list`),
        # so run it off the UI thread to keep the TUI responsive.
        ok, info = engine.open_in_explorer(m["name"])
        self.call_from_thread(self.notify, info if ok else f"Error: {info}",
                              severity="information" if ok else "error")

    def action_terminal(self) -> None:
        m = self._selected_mount()
        if m:
            ok, info = engine.open_wsl_terminal(m["mountpoint"])
            self.notify(f"Terminal: {info}" if ok else f"Error: {info}",
                        severity="information" if ok else "error")

    # ----- clean up bare mounts on exit ---------------------------------
    def on_unmount(self) -> None:
        for dev in list(self.bare):
            try:
                engine.detach(dev)
            except Exception:
                pass


def main() -> None:
    enable_vt()
    try:
        ensure_admin()
    except PermissionError as e:
        print(f"\033[31m{e}\033[0m")
        input("Press Enter to close. ")
        sys.exit(1)

    ok, msg = check_wsl_alive()
    if not ok:
        print(f"\033[31m{msg}\033[0m")
        input("Press Enter to close. ")
        sys.exit(1)

    MountApp().run()


if __name__ == "__main__":
    main()
