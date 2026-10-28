#!/usr/bin/env python3
"""Steam Frame private KDE desktop backed by gocryptfs (v13).

First use: installs/copies gocryptfs from rootless Guix if needed and initializes
an encrypted vault. Later uses: unlock -> nested KDE -> unmount.

Install once with --install-launcher to get a "Private Desktop" entry in the
Frame '+' menu (a Konsole wrapper with a space-free absolute Exec, as the
SteamOS scanner requires); --remove-launcher undoes it. The nested desktop
defaults to 1920x1080; override with --width/--height.

Uses the SteamOS dynamic linker plus gocryptfs -fg, as tested on Steam Frame.
It deliberately does not invoke the relocatable Guix pack wrapper for mounts.

Synchronizes text clipboards in both directions using wl-clipboard >=2.3
by default (disable with
--no-share-clipboard); this can expose passwords to host clipboard history.
Shares host SYSTEM Flatpak installations read-only, while keeping the
private --user Flatpak installation and application data encrypted.
The host steamos --user Flatpak installation is not imported.

Uses a volatile /run/setuid-programs/fusermount3 helper symlink because Guix's
build expects that pathname. May request sudo once per boot. Does not change
SteamOS /usr, /etc, PAM, or the original Guix wrapper.

Security model: encryption at rest ONLY. While unlocked, ordinary host programs
running as the same user can access the host-visible plaintext mount. A separate
bwrap mount namespace does not change that. Never suspend while unlocked.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import textwrap
import fcntl
import re
import os
from pathlib import Path
import resource
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import time

APP = "frame-private-desktop"
REL_STATE = Path(".local/share/frame-private-desktop")
NATIVE_BINARY = Path(".local/bin/gocryptfs-native")
HOST_LOADER = Path("/lib/ld-linux-aarch64.so.1")
HOST_HELPER = Path("/usr/bin/fusermount3")
GUIX_HELPER = Path("/run/setuid-programs/fusermount3")
DESKTOP_ID = "frame-private-desktop.desktop"
MENU_ENTRY = Path.home() / ".local/share/applications" / DESKTOP_ID
MENU_WRAPPER = Path.home() / ".local/bin/frame-private-desktop-launch"
LAUNCH_LOG = Path.home() / REL_STATE / "launch.log"


class LaunchError(Exception):
    pass


def info(message: str) -> None:
    print(f"[{APP}] {message}", flush=True)


def run(args: list[str], *, check: bool = True, **kwargs):
    return subprocess.run(args, check=check, **kwargs)


def command(name: str) -> str:
    found = shutil.which(name)
    if not found:
        raise LaunchError(f"Required executable not found on the SteamOS host: {name}")
    return found


def host_gocryptfs(binary: Path) -> list[str]:
    """Invoke the copied Guix ELF using SteamOS glibc, no Guix namespace.

    -fg is mandatory for mounts because a background self-exec would otherwise
    omit the manually specified loader and interpret -fg as a library name.
    """
    if not HOST_LOADER.is_file():
        raise LaunchError(f"SteamOS ARM64 dynamic linker missing: {HOST_LOADER}")
    return [str(HOST_LOADER), str(binary)]


def ensure_native_gocryptfs(home: Path) -> Path:
    binary = home / NATIVE_BINARY
    if not binary.is_file():
        command("guix-env")
        info("Native gocryptfs copy missing; retrieving it from rootless Guix ...")
        check = run(["guix-env", "sh", "-c", "command -v gocryptfs >/dev/null 2>&1"], check=False)
        if check.returncode != 0:
            info("Installing gocryptfs into the Guix profile ...")
            run(["guix-env", "guix", "install", "gocryptfs"])
        script = (
            'set -e; src=$(readlink -f "$(command -v gocryptfs)"); '
            'install -m 755 "$src" "$1"'
        )
        binary.parent.mkdir(parents=True, exist_ok=True)
        run(["guix-env", "sh", "-c", script, "frame-private", str(binary)])
    check = run(host_gocryptfs(binary) + ["-version"], check=False,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if check.returncode != 0:
        raise LaunchError(
            f"Native gocryptfs is not runnable via SteamOS loader: {binary}\n"
            f"{check.stderr.strip()}"
        )
    return binary


def ensure_host_fusermount_alias() -> None:
    """Provide Guix's hardcoded setuid-helper path using a volatile /run symlink."""
    if not HOST_HELPER.is_file() or not os.access(HOST_HELPER, os.X_OK):
        raise LaunchError(f"SteamOS FUSE helper is unavailable: {HOST_HELPER}")
    if not (HOST_HELPER.stat().st_mode & stat.S_ISUID):
        raise LaunchError(f"Host FUSE helper is unexpectedly not setuid: {HOST_HELPER}")

    parent = GUIX_HELPER.parent
    if parent.exists():
        st = parent.lstat()
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != 0:
            raise LaunchError(f"Unsafe helper directory: {parent} (expected root-owned directory)")

    if GUIX_HELPER.is_symlink() or GUIX_HELPER.exists():
        if GUIX_HELPER.resolve() != HOST_HELPER.resolve():
            raise LaunchError(f"Unexpected existing FUSE helper at {GUIX_HELPER}")
        return

    info("Guix's gocryptfs needs a temporary helper alias under /run; sudo may ask for your password.")
    command("sudo")
    run(["sudo", "install", "-d", "-m", "0755", str(parent)])
    run(["sudo", "ln", "-s", str(HOST_HELPER), str(GUIX_HELPER)])
    if GUIX_HELPER.resolve() != HOST_HELPER.resolve():
        raise LaunchError("Failed to create the temporary helper alias")


def ensure_empty_vault_reset(cipher: Path) -> None:
    if not (cipher / "gocryptfs.conf").is_file():
        raise LaunchError("Cannot reset: vault does not exist")
    names = {p.name for p in cipher.iterdir()}
    if names != {"gocryptfs.conf", "gocryptfs.diriv"}:
        raise LaunchError("Refusing to reset: vault contains data or unexpected files")
    suffix = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup = cipher.with_name(f"encrypted-exposed-backup-{suffix}")
    if backup.exists():
        raise LaunchError("Vault backup destination already exists")
    cipher.rename(backup)
    cipher.mkdir(mode=0o700)
    info(f"Previous EMPTY encrypted vault archived to: {backup}")


def start_desktop_session(steam_socket: Path) -> Path | None:
    """Start the Frame's built-in nested Plasma desktop; return its socket.

    This is the command Steam's QAM 'Desktop' button runs (Steam spawns
    /usr/bin/steamos-nested-desktop under steam.service). We reuse it
    verbatim: gamescope's VR-laser button handling is only proven on that
    single focused desktop overlay, and its KWin relays a clean seat to
    anything nested inside (see README 'Controller input'). Returns None
    (after logging why) when the session can't be started.
    """
    launcher = Path("/usr/bin/steamos-nested-desktop")
    if not launcher.is_file():
        info("No /usr/bin/steamos-nested-desktop on this system; cannot start "
             "a Plasma desktop session automatically.")
        return None
    info("No Plasma desktop session is running; starting Steam's built-in one "
         "(the QAM 'Desktop' button's own command) so controller input is "
         "relayed reliably ...")
    log_path = Path.home() / ".local/share/frame-private-desktop/desktop-session.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = {"HOME": str(Path.home()),
           "USER": os.environ.get("USER") or "steamos",
           "LOGNAME": os.environ.get("LOGNAME") or os.environ.get("USER") or "steamos",
           "PATH": "/usr/local/bin:/usr/bin:/bin",
           "TERM": "linux",
           "XDG_RUNTIME_DIR": str(steam_socket.parent),
           "XDG_SESSION_TYPE": "wayland",
           "WAYLAND_DISPLAY": steam_socket.name}
    # A '+'-menu Konsole lives inside steam.service's cgroup, and children
    # inherit it: the first auto-started desktop was SIGTERMed by Steam's
    # teardown of the desktop the user had just closed, seconds after it
    # came up. systemd-run --user places the session under the user manager
    # instead - the same neighbourhood Steam's own session lives in - so
    # Steam's process-tree cleanup cannot reach it.
    launched = False
    if shutil.which("systemd-run"):
        bus = steam_socket.parent / "bus"
        argv = ["systemd-run", "--user", "--quiet", "--collect",
                f"--unit=frame-private-desktop-nested-{os.getpid()}",
                f"--property=StandardOutput=append:{log_path}",
                f"--property=StandardError=append:{log_path}"]
        argv += [f"--setenv={key}={value}" for key, value in env.items()]
        argv.append(str(launcher))
        run_env = dict(os.environ, XDG_RUNTIME_DIR=str(steam_socket.parent))
        if bus.is_socket():
            run_env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={bus}"
        try:
            with log_path.open("ab") as log:
                result = subprocess.run(argv, stdin=subprocess.DEVNULL,
                                        stdout=log, stderr=log, env=run_env,
                                        timeout=20)
            launched = result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            launched = False
        if not launched:
            info("systemd-run could not start the session outside Steam's "
                 "service cgroup; starting it directly instead.")
    if not launched:
        try:
            with log_path.open("ab") as log:
                subprocess.Popen([str(launcher)], stdin=subprocess.DEVNULL,
                                 stdout=log, stderr=log, env=env,
                                 start_new_session=True)
        except OSError as exc:
            info(f"Could not start the built-in desktop session ({exc}); "
                 "falling back to Steam's compositor.")
            return None
    desktop_dir = steam_socket.parent / "nested_plasma"
    deadline = time.time() + 25
    while time.time() < deadline:
        if desktop_dir.is_dir():
            for name in sorted(entry.name for entry in desktop_dir.iterdir()):
                candidate = desktop_dir / name
                if (re.fullmatch(r"wayland-\d+", name) and candidate.is_socket()
                        and candidate.stat().st_uid == os.getuid()):
                    # Steam's teardown raced (and killed) the first attempt a
                    # couple of seconds after its socket appeared; only trust
                    # a session that is still up after settling.
                    time.sleep(1.5)
                    if candidate.is_socket():
                        return candidate
                    info("The auto-started desktop session died just after it "
                         "came up (Steam's teardown likely raced this launch); "
                         "falling back to Steam's compositor.")
                    return None
        time.sleep(0.5)
    info(f"The built-in desktop session did not come up within 25 s (see {log_path}); "
         "falling back to Steam's compositor.")
    return None


def find_outer_wayland(attach: str = "auto") -> tuple[Path, Path]:
    """Accept Steam Frame's nested_plasma runtime without trusting arbitrary paths.

    Steam Frame's Desktop runs another Plasma Wayland compositor using
    XDG_RUNTIME_DIR=/run/user/<uid>/nested_plasma rather than /run/user/<uid>.
    Store our plaintext FUSE mount under the *base* runtime directory so it
    survives nested Plasma being restarted and can be cleaned up predictably.
    """
    base = Path(f"/run/user/{os.getuid()}")
    if not base.is_dir():
        raise LaunchError(f"Missing host runtime directory: {base}")
    st = base.stat()
    if st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) & 0o077:
        raise LaunchError(f"Unsafe ownership or permissions on {base}")

    raw = os.environ.get("XDG_RUNTIME_DIR")
    runtime = Path(raw) if raw else base
    if not runtime.is_absolute() or not runtime.is_dir():
        raise LaunchError(f"Invalid XDG_RUNTIME_DIR: {raw!r}")
    if not runtime.resolve().is_relative_to(base.resolve()):
        raise LaunchError(
            f"XDG_RUNTIME_DIR={runtime} is outside the SteamOS user runtime {base}"
        )

    display = os.environ.get("WAYLAND_DISPLAY", "")
    if display and attach == "auto":
        socket = Path(display) if display.startswith("/") else runtime / display
        if not socket.is_socket() or not socket.resolve().is_relative_to(base.resolve()):
            raise LaunchError(f"Host Wayland socket isn't accessible under {base}: {socket}")
        return base, socket
    # A '+'-menu launch starts Konsole from gamescope's outer session, which
    # has no WAYLAND_DISPLAY at all. Discover a running compositor instead of
    # failing. Prefer the Plasma desktop session, and start Steam's own
    # built-in one when it isn't up: there is no input magic in it - Steam's
    # desktop is just another kwin_wayland client on Steam's compositor socket
    # - but it IS the one surface where Valve has made VR-laser clicks work,
    # and its KWin relays a clean seat to everything nested inside. Nesting
    # directly on Steam's gamescope compositor instead exposes a gamescope/
    # SteamVR bug: a button press that starts on one VR overlay can get its
    # release delivered to whichever overlay the laser reaches first (Valve's
    # own code comments and their --vr-overlay-enable-click-stabilization
    # flag document exactly this), which is the 'left click held down after
    # first click' symptom. --attach=steam forces the direct route anyway.
    # The existing checks (real socket, owned by us, under the user runtime)
    # apply to all.
    desktop = (base / "nested_plasma", re.compile(r"wayland-\d+\Z"), "Plasma desktop compositor")
    steam = (base, re.compile(r"gamescope-\d+\Z"), "Steam session compositor (headset display)")
    other = (base, re.compile(r"wayland-\d+\Z"), "compositor")

    def first_socket(candidate):
        directory, pattern, label = candidate
        if not directory.is_dir():
            return None
        for name in sorted(entry.name for entry in directory.iterdir()):
            found = directory / name
            if (pattern.fullmatch(name) and found.is_socket()
                    and found.stat().st_uid == os.getuid()):
                return found
        return None

    if attach in ("auto", "desktop") and first_socket(desktop) is None:
        steam_socket = first_socket(steam)
        if steam_socket is not None:
            start_desktop_session(steam_socket)
    candidates = {"desktop": (desktop,),
                  "steam": (steam,),
                  "auto": (desktop, steam, other)}[attach]
    for candidate in candidates:
        found = first_socket(candidate)
        if found is not None:
            info(f"WAYLAND_DISPLAY is unset; attaching to the {candidate[2]} at {found}")
            if candidate[2].startswith("Steam"):
                info("Tip: controller input is only fully reliable inside the "
                     "Plasma desktop session; the private desktop nests there "
                     "whenever one is running.")
            os.environ["XDG_RUNTIME_DIR"] = str(found.parent)
            return base, found
    raise LaunchError(
        f"WAYLAND_DISPLAY isn't set and no compositor for --attach={attach} "
        f"exists under {base}. From the '+' menu: start the Desktop panel for "
        "reliable input, or log out of Plasma to use Steam's compositor directly. "
        "Either way this cannot run from a plain SSH shell."
    )


def kwin_wrapper(mount: Path, width: int, height: int) -> None:
    """Wrap the REAL KWin Wayland launcher, retaining its D-Bus handshake.

    Plasma's classic startup waits for org.kde.KWinWrapper before launching
    kded6 and plasmashell.  Calling kwin_wayland directly bypasses registration
    of that service and leaves the Plasma session at a black KWin window.
    """
    bin_dir = mount / ".local/share/frame-private-desktop/bin"
    bin_dir.mkdir(parents=True, exist_ok=True)

    real_kwin = Path("/usr/bin/kwin_wayland")
    real_wrapper = Path("/usr/bin/kwin_wayland_wrapper")
    if not (real_kwin.is_file() and os.access(real_kwin, os.X_OK)):
        raise LaunchError(f"Missing host KDE compositor: {real_kwin}")
    if not (real_wrapper.is_file() and os.access(real_wrapper, os.X_OK)):
        raise LaunchError(f"Missing host KDE compositor wrapper: {real_wrapper}")

    # Replace v8's obsolete kwin_wayland shim, which added duplicate arguments.
    kwin = bin_dir / "kwin_wayland"
    kwin.write_text(
        '#!/bin/sh\nexec /usr/bin/kwin_wayland "$@"\n',
        encoding="utf-8",
    )
    kwin.chmod(0o700)

    wrapper = bin_dir / "kwin_wayland_wrapper"
    wrapper.write_text(
        '#!/bin/sh\n'
        '# Supervise SteamOS\' wrapper: its exit code 120 only means "gave up\n'
        '# respawning KWin"; log every death with a timestamp so launch.log shows\n'
        '# how often and when KWin actually died, and retry a few times before\n'
        '# letting the private session end.\n'
        'i=1\n'
        'while [ "$i" -le 3 ]; do\n'
        f'    /usr/bin/kwin_wayland_wrapper --width {width} --height {height} --no-lockscreen "$@"\n'
        '    rc=$?\n'
        '    printf \'[frame-kwin] compositor attempt %s exited rc=%s at %s\\n\' "$i" "$rc" "$(date +%H:%M:%S)" >&2\n'
        '    [ "$rc" -eq 0 ] && exit 0\n'
        '    i=$((i + 1)); sleep 2\n'
        'done\n'
        'exit 120\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o700)


def ensure_classic_plasma_startup(mount: Path) -> None:
    """Force Plasma's non-systemd startup in the PRIVATE encrypted home.

    Plasma 6 reads [General] systemdBoot from ~/.config/startkderc.
    A new D-Bus session has no org.freedesktop.systemd1 user manager.
    Preserve existing settings/other groups if a config file already exists.
    """
    cfg = mount / ".config/startkderc"
    if cfg.is_symlink():
        raise LaunchError(f"Refusing to edit symlink: {cfg}")
    if cfg.exists() and not cfg.is_file():
        raise LaunchError(f"Unexpected KDE configuration type: {cfg}")
    if not cfg.exists():
        cfg.write_text("[General]\nsystemdBoot=false\n", encoding="utf-8")
        cfg.chmod(0o600)
        info("Configured private Plasma to use classic (non-systemd) startup.")
        return

    contents = cfg.read_text(encoding="utf-8")
    lines = contents.splitlines(keepends=True)
    result = []
    in_general = False
    seen_general = False
    wrote_setting = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if in_general and not wrote_setting:
                result.append("systemdBoot=false\n")
                wrote_setting = True
            in_general = stripped == "[General]"
            seen_general = seen_general or in_general
        if in_general and re.match(r"^\s*systemdBoot\s*=", line):
            if not wrote_setting:
                result.append("systemdBoot=false\n")
                wrote_setting = True
            continue
        result.append(line)
    if in_general and not wrote_setting:
        result.append("systemdBoot=false\n")
    if not seen_general:
        result.insert(0, "[General]\nsystemdBoot=false\n\n")
    new_contents = "".join(result)
    if new_contents != contents:
        cfg.write_text(new_contents, encoding="utf-8")
        info("Configured private Plasma to use classic (non-systemd) startup.")



def _clipboard_version(output: str) -> tuple[int, int, int] | None:
    match = re.search(r"(?:^|\s)(\d+)\.(\d+)\.(\d+)(?:\s|$)", output)
    return tuple(map(int, match.groups())) if match else None


def ensure_clipboard_tools() -> str:
    """Select wl-clipboard 2.3+ for Plasma's ext-data-control protocol.

    2.2.x can appear to work yet silently fail to read a clipboard on KWin.
    Prefer SteamOS's binaries when new enough, otherwise Guix's installed pair.
    Return the executable directory as seen *inside* our private bwrap namespace.
    """
    host = Path('/usr/bin')
    if all((host / n).is_file() for n in ('wl-paste', 'wl-copy')):
        result = run([str(host / 'wl-paste'), '--version'], check=False,
                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        ver = _clipboard_version(result.stdout)
        if result.returncode == 0 and ver and ver >= (2, 3, 0):
            info(f'Using SteamOS wl-clipboard {".".join(map(str, ver))} for clipboard bridging.')
            return '/usr/bin'

    command('guix-env')
    def guix_version() -> tuple[int, int, int] | None:
        proc = run(['guix-env', 'sh', '-c',
                    'command -v wl-copy >/dev/null && command -v wl-paste >/dev/null && wl-paste --version'],
                   check=False, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        return _clipboard_version(proc.stdout) if proc.returncode == 0 else None

    ver = guix_version()
    if ver is None:
        info('Installing wl-clipboard in Guix for bidirectional clipboard ...')
        run(['guix-env', 'guix', 'install', 'wl-clipboard'])
        ver = guix_version()
    if ver and ver >= (2, 3, 0):
        info(f'Using Guix wl-clipboard {".".join(map(str, ver))} for clipboard bridging.')
        return '/opt/guix-profile/bin'
    raise LaunchError(
        'Clipboard requires wl-clipboard >=2.3.0 for KDE Plasma. '
        f'The available Guix version is {ver or "missing"}; SteamOS does not supply a compatible version. '
        'Update the wl-clipboard package in Guix, or temporarily launch with --no-share-clipboard. '
        'No encrypted data was changed.'
    )


def write_clipboard_bridge(mount: Path) -> None:
    """Write a two-way text clipboard bridge with useful non-sensitive diagnostics.

    The private startup wrapper runs the bridge before Plasma. It waits for
    KWin's private socket, then mirrors changes in either direction. It never
    logs clipboard contents.
    """
    script = mount / '.local/share/frame-private-desktop/clipboard-bridge.py'
    if script.is_symlink():
        raise LaunchError(f'Refusing to overwrite symlink: {script}')
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(textwrap.dedent(r"""#!/usr/bin/env python3
import os
import subprocess
import sys
import time
from pathlib import Path

RUNTIME = Path(os.environ['XDG_RUNTIME_DIR'])
OUTER = 'frame-outer-wayland'
INNER = 'wayland-0'
BIN = Path(os.environ['FRAME_CLIP_BIN'])
MAX_BYTES = 1024 * 1024
STATES = {OUTER: 'parent', INNER: 'private'}
last_error = {}

def log(message):
    print('[frame-clipboard] ' + message, file=sys.stderr, flush=True)

def env_for(display):
    env = os.environ.copy()
    env['WAYLAND_DISPLAY'] = display
    return env

def report_error(display, op, reason):
    key = (display, op)
    if last_error.get(key) != reason:
        log(f'{STATES[display]} {op} failed: {reason}')
        last_error[key] = reason

def paste(display):
    # `--type text` auto-selects an available text MIME type, rather than
    # requiring one exact charset spelling. -n avoids adding a newline.
    try:
        result = subprocess.run(
            [str(BIN / 'wl-paste'), '--no-newline', '--type', 'text'],
            env=env_for(display), stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        report_error(display, 'read', type(exc).__name__)
        return None
    if result.returncode != 0:
        # An empty clipboard is routine, not an error; protocol errors are.
        error = result.stderr.decode('utf-8', errors='replace').strip()[:180]
        if error and 'Nothing is copied' not in error:
            report_error(display, 'read', error)
        return None
    if len(result.stdout) > MAX_BYTES:
        report_error(display, 'read', 'text exceeds the 1 MiB clipboard limit')
        return None
    return result.stdout

holders = []

def copy(display, data):
    # wl-copy forks a daemon to OWN the selection until the next copy replaces
    # it. That daemon keeps our stderr pipe open, so waiting for EOF (as
    # subprocess.run does) times out and its kill then destroys a perfectly
    # successful copy -- which previously looked like "write failed" and made
    # the mirror loop retry every cycle. Still-running after we fed the data
    # therefore means success: the daemon holds the selection.
    try:
        proc = subprocess.Popen(
            [str(BIN / 'wl-copy'), '--type', 'text/plain;charset=utf-8'],
            input=data, env=env_for(display), stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
    except OSError as exc:
        report_error(display, 'write', type(exc).__name__)
        return False
    holders[:] = [h for h in holders if h.poll() is None]  # reap finished daemons
    try:
        _, stderr = proc.communicate(timeout=3)
    except subprocess.TimeoutExpired:
        holders.append(proc)  # detached on purpose: it owns the clipboard now
        return True
    if proc.returncode != 0:
        error = stderr.decode('utf-8', errors='replace').strip()[:180]
        report_error(display, 'write', error or f'exit code {proc.returncode}')
        return False
    return True

log(f'clipboard bridge booting; using {BIN}, waiting for private Wayland socket')
while not (RUNTIME / INNER).is_socket():
    time.sleep(.25)

previous = {OUTER: paste(OUTER), INNER: paste(INNER)}
# Content we ourselves last wrote into each side; never bounce it back.
last_written = {OUTER: None, INNER: None}
log('both Wayland sockets available; monitoring bidirectionally')
# Make it possible to copy from the host *before* entering private KDE.
if previous[OUTER] is not None and previous[OUTER] != previous[INNER]:
    if copy(INNER, previous[OUTER]):
        previous[INNER] = previous[OUTER]
        last_written[INNER] = previous[OUTER]
        log('initial parent -> private clipboard synchronized')

while (RUNTIME / INNER).is_socket():
    sampled = {OUTER: paste(OUTER), INNER: paste(INNER)}
    changed_outer = sampled[OUTER] is not None and sampled[OUTER] != previous[OUTER]
    changed_inner = sampled[INNER] is not None and sampled[INNER] != previous[INNER]
    if changed_outer or changed_inner:
        # If changes coincide, prefer the private selection.
        source, dest = (INNER, OUTER) if changed_inner else (OUTER, INNER)
        if (sampled[source] != sampled[dest]
                and sampled[source] != last_written[dest]
                and copy(dest, sampled[source])):
            sampled[dest] = sampled[source]
            last_written[dest] = sampled[source]
            log(f'{STATES[source]} -> {STATES[dest]} clipboard synchronized')
    previous = sampled
    time.sleep(.35)
log('private Wayland socket closed, exiting')
"""), encoding='utf-8')
    script.chmod(0o700)


def host_resolver_bind() -> list[str]:
    """Restore DNS when /etc/resolv.conf resolves into masked /run.

    We never expose the host runtime directory wholesale. Only the actual
    resolver configuration file is made visible at its original path.
    """
    path = Path("/etc/resolv.conf")
    try:
        actual = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise LaunchError(f"Cannot resolve host /etc/resolv.conf: {exc}") from exc
    if not actual.is_file():
        raise LaunchError(f"Host DNS configuration is not a regular file: {actual}")
    if actual == path:
        return []  # Already visible under the readonly /etc mount.
    info(f"Exposing resolver target inside private sandbox: {actual}")
    return ["--ro-bind", str(actual), str(actual)]


def prepare_flatpak_bootstrap(mount: Path, *, share_clipboard: bool) -> None:
    """Configure only a private, encrypted --user Flathub remote if missing.

    Runs *within* the separate D-Bus/bwrap environment. In particular it does
    not touch ~/.local/share/flatpak belonging to the normal steamos desktop.
    Startup must proceed even when the network or Flatpak is unavailable.
    """
    bin_dir = mount / ".local/share/frame-private-desktop/bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "private-plasma-start"
    if script.is_symlink():
        raise LaunchError(f"Refusing to overwrite symlink: {script}")
    script.write_text(
        "#!/bin/sh\n"
        "# Everything below runs inside private bwrap and the private DBus session.\n"
        "if command -v flatpak >/dev/null 2>&1; then\n"
        "    if ! flatpak remotes --user --columns=name 2>/dev/null | grep -Fxq flathub; then\n"
        "        echo '[frame-private-desktop] Adding Flathub to encrypted Flatpak profile (first run only).' >&2\n"
        "        timeout 25s flatpak remote-add --user --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo || echo '[frame-private-desktop] WARNING: Flathub setup failed; retry from a private Konsole.' >&2\n"
        "    fi\n"
        "    marker=\"$HOME/.local/state/frame-private-desktop/flathub-appstream-ready\"\n"
        "    if [ ! -e \"$marker\" ] && flatpak remotes --user --columns=name 2>/dev/null | grep -Fxq flathub; then\n"
        "        mkdir -p \"$(dirname \"$marker\")\"\n"
        "        ( timeout 120s flatpak update --user --appstream --noninteractive && touch \"$marker\" ) >\"$HOME/.cache/frame-private-flatpak-appstream.log\" 2>&1 &\n"
        "    fi\n"
        "fi\n" +
        ("python3 \"$HOME/.local/share/frame-private-desktop/clipboard-bridge.py\" &\n"
         "clip_pid=$!\n" if share_clipboard else "") +
        ("trap 'kill \"$clip_pid\" 2>/dev/null || true' EXIT\n" if share_clipboard else "") +
        "/usr/bin/startplasma-wayland\n",
        encoding="utf-8",
    )
    script.chmod(0o700)


def find_host_audio_sockets(runtime_base: Path, wayland_socket: Path) -> dict[str, Path]:
    """Locate host audio sockets, never bind an entire host runtime directory."""
    runtime_dirs = [runtime_base, wayland_socket.parent]
    extra = os.environ.get("PIPEWIRE_RUNTIME_DIR")
    if extra:
        runtime_dirs.append(Path(extra))
    seen: set[Path] = set()
    found: dict[str, Path] = {}
    for root in runtime_dirs:
        try:
            root = root.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if root in seen or not root.is_relative_to(runtime_base.resolve()):
            continue
        seen.add(root)
        for name in ("pipewire-0", "pulse/native"):
            candidate = root / name
            try:
                if name not in found and candidate.is_socket() and candidate.stat().st_uid == os.getuid():
                    found[name] = candidate
            except OSError:
                pass
    return found


def build_private_kde(home: Path, mount: Path, wayland_socket: Path, runtime_base: Path, width: int, height: int, clipboard_bin: str | None) -> list[str]:
    prefix = home / ".local/share/guix"
    store = prefix / "gnu/store"
    profiles = prefix / "var/guix"
    if not store.is_dir() or not profiles.is_dir():
        raise LaunchError("Can't find backing rootless Guix store/profile under ~/.local/share/guix")

    uid = os.getuid()
    inner_runtime = f"/run/user/{uid}"
    inner_socket = "frame-outer-wayland"
    private_path = f"{home}/.local/share/frame-private-desktop/bin"
    inner_profile = "/opt/guix-profile"
    lang = os.environ.get("LANG", "C.UTF-8")
    env = {
        "HOME": str(home), "USER": "steamos", "LOGNAME": "steamos", "SHELL": "/bin/bash",
        "PATH": f"{private_path}:{inner_profile}/bin:/usr/local/bin:/usr/bin:/bin",
        "XDG_RUNTIME_DIR": inner_runtime,
        "WAYLAND_DISPLAY": inner_socket,
        "XDG_SESSION_TYPE": "wayland", "XDG_CURRENT_DESKTOP": "KDE", "XDG_SESSION_DESKTOP": "KDE",
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_DATA_HOME": str(home / ".local/share"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "XDG_STATE_HOME": str(home / ".local/state"),
        "XDG_CONFIG_DIRS": "/etc/xdg",
        "XDG_DATA_DIRS": f"{home}/.local/share/flatpak/exports/share:/var/lib/flatpak/exports/share:{inner_profile}/share:/usr/local/share:/usr/share",
        "FLATPAK_USER_DIR": f"{home}/.local/share/flatpak",
        "FLATPAK_SYSTEM_DIR": "/var/lib/flatpak",
        "QT_QPA_PLATFORM": "wayland", "LANG": lang,
        "KWIN_WAYLAND_NO_PERMISSION_CHECKS": "1",
    }
    if clipboard_bin:
        env['FRAME_CLIP_BIN'] = clipboard_bin
    for key in ("LC_ALL", "LC_MESSAGES", "TZ"):
        if key in os.environ:
            env[key] = os.environ[key]

    audio = find_host_audio_sockets(runtime_base, wayland_socket)
    if "pipewire-0" in audio:
        env["PIPEWIRE_RUNTIME_DIR"] = inner_runtime
    if "pulse/native" in audio:
        env["PULSE_SERVER"] = f"unix:{inner_runtime}/pulse/native"
    if audio:
        info("Connecting private desktop to host audio: " + ", ".join(sorted(audio)))
    else:
        info("WARNING: No host PipeWire/PulseAudio socket found. Audio may remain unavailable.")

    args = [
        command("bwrap"),
        "--die-with-parent", "--unshare-pid", "--unshare-ipc", "--new-session",
        "--share-net",  # Firefox and web apps need connectivity
        "--tmpfs", "/",
        "--ro-bind", "/usr", "/usr",
        "--ro-bind", "/etc", "/etc",
        "--symlink", "usr/bin", "/bin",
        "--symlink", "usr/lib", "/lib",
        "--symlink", "usr/lib64", "/lib64",
        "--symlink", "usr/sbin", "/sbin",
        "--dir", "/gnu", "--bind", str(store), "/gnu/store",
        "--dir", "/var", "--bind", str(profiles), "/var/guix",
        # Expose only the host's SYSTEM Flatpak repository, not its user
        # installation. Read-only prevents this session altering system apps.
        "--ro-bind-try", "/var/lib/flatpak", "/var/lib/flatpak",
        "--dir", "/opt",
        "--symlink", "/var/guix/profiles/per-user/steamos/guix-profile", inner_profile,
        # Bind the ORIGINAL home before masking /home/steamos with the private
        # decrypted mount. This guarantees the intended /host-home source.
        "--dir", "/home", "--bind", str(home), "/host-home",
        "--bind", str(mount), str(home),
        "--tmpfs", "/run", "--dir", "/run/user",
        "--perms", "0700", "--dir", inner_runtime,
        "--ro-bind", str(wayland_socket), f"{inner_runtime}/{inner_socket}",
        "--tmpfs", "/tmp", "--perms", "1777", "--dir", "/tmp/.X11-unix", "--dev", "/dev",
        "--dev-bind-try", "/dev/fuse", "/dev/fuse",
        "--dev-bind-try", "/dev/dri", "/dev/dri",
        "--dev-bind-try", "/dev/snd", "/dev/snd",
        "--ro-bind-try", "/sys", "/sys",
        "--proc", "/proc", "--chdir", str(home), "--clearenv",
    ]
    # /etc/resolv.conf is frequently a symlink into /run on SteamOS.
    # Our private tmpfs /run would otherwise leave it dangling (offline browsers).
    args += host_resolver_bind()
    if "pulse/native" in audio:
        args += ["--dir", f"{inner_runtime}/pulse"]
    for name, src in audio.items():
        args += ["--ro-bind", str(src), f"{inner_runtime}/{name}"]
    for key, val in env.items():
        args += ["--setenv", key, val]
    args += ["--", "dbus-run-session", "--", str(home / ".local/share/frame-private-desktop/bin/private-plasma-start")]
    return args


def run_session(args: list[str]) -> int:
    child = subprocess.Popen(args, start_new_session=True)
    try:
        status = child.wait()
    except KeyboardInterrupt:
        info("Stopping the private KDE session ...")
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            return child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait()
            return 130
    # The SteamOS kwin_wayland_wrapper respawns a dead KWin and reports its own
    # code once it gives up (KWIN_RESTART_COUNT); 120 there means KWin kept
    # dying. Log who really ended the session so launch.log self-identifies it.
    if status < 0:
        name = signal.Signals(-status).name
        info(f"Private session was killed by signal {name} (-{status}); "
             "an external process (Steam session cleanup, OOM killer) sent it.")
    elif status == 120:
        info("Private session exited 120: SteamOS' kwin_wayland_wrapper gave up "
             "respawning KWin (KWin exited repeatedly). See messages above.")
    else:
        info(f"Private session exited with code {status}.")
    return status


def mount_foreground(binary: Path, cipher: Path, mount: Path) -> subprocess.Popen:
    """Start the *working* non-daemonized gocryptfs command and wait for FUSE."""
    cmd = host_gocryptfs(binary) + ["-fg", str(cipher), str(mount)]
    info("Enter your encrypted desktop passphrase when prompted.")
    # Keep the controlling terminal: gocryptfs needs it for the passphrase.
    child = subprocess.Popen(cmd)
    try:
        # Password is read interactively by gocryptfs. Do not impose an artificial
        # timeout on someone entering a passphrase through the headset keyboard.
        while not os.path.ismount(mount):
            rc = child.poll()
            if rc is not None:
                raise LaunchError(f"gocryptfs exited without mounting (exit status {rc})")
            time.sleep(0.15)
        if child.poll() is not None:
            raise LaunchError("gocryptfs exited immediately after mount")
        return child
    except BaseException:
        # This also catches Ctrl+C while the password is being entered.
        # Don't leave an unlocked mount just because the launcher was interrupted.
        if os.path.ismount(mount):
            unmount(mount)
        if not os.path.ismount(mount):
            stop_foreground(child)
        raise


def stop_foreground(child: subprocess.Popen) -> None:
    if child.poll() is not None:
        return
    try:
        child.wait(timeout=3)
    except subprocess.TimeoutExpired:
        # Safe to stop only once the mount has been cleanly detached.
        child.terminate()
        try:
            child.wait(timeout=3)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=3)


def unmount(mount: Path) -> None:
    if not os.path.ismount(mount):
        return
    for _ in range(7):
        result = run(["/usr/bin/fusermount3", "-u", str(mount)], check=False,
                     stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        if result.returncode == 0 and not os.path.ismount(mount):
            info("Encrypted desktop has been locked (FUSE unmounted).")
            return
        time.sleep(1)
    raise LaunchError(
        f"The decrypted home is STILL MOUNTED at {mount}. "
        "Stop processes using it and run: fusermount3 -u '" + str(mount) + "'. "
        "Do not leave the headset unattended until it is unmounted."
    )


def refresh_kde_menus() -> None:
    sycoca = shutil.which("kbuildsycoca6")
    if sycoca:
        subprocess.Popen([sycoca], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                         start_new_session=True)


def menu_entry_text() -> str:
    # The Frame '+' menu / "Add Library Shortcut" scanner wants: a single-token
    # absolute Exec with no spaces or quotes, Type=Application, no NoDisplay,
    # no Categories=Settings. The generated wrapper supplies the Konsole window
    # that the gocryptfs passphrase prompt needs (Terminal=false for the same
    # reason: we open the terminal ourselves).
    return ("[Desktop Entry]\n"
            "Type=Application\n"
            "Version=1.0\n"
            "Name=Private Desktop\n"
            "GenericName=Encrypted desktop\n"
            "Comment=Unlock and launch a nested encrypted KDE desktop\n"
            f"Exec={MENU_WRAPPER}\n"
            "Icon=system-lock-screen\n"
            "Terminal=false\n"
            "Categories=Utility;Security;\n"
            "StartupNotify=false\n")


def install_menu_entry(share_clipboard: bool | None, width: int | None,
                       height: int | None) -> None:
    script = Path(__file__).resolve()
    if any(ch in str(script) for ch in ('"', '\n', '\r', '\\')):
        raise LaunchError("Script pathname is not safe to embed in the launcher wrapper")
    terminal = shutil.which("konsole") or shutil.which("xterm")
    if not terminal:
        raise LaunchError("Konsole not found; install it or run this script from a terminal yourself")
    python3 = shutil.which("python3") or "/usr/bin/python3"
    MENU_WRAPPER.parent.mkdir(parents=True, exist_ok=True)
    flags = {None: "", True: " --share-clipboard", False: " --no-share-clipboard"}[share_clipboard]
    if width is not None and height is not None:
        flags += f" --width {width} --height {height}"
    # All output (ours plus the nested session's inherited stdout/stderr) is
    # teed to LAUNCH_LOG so a failed launch leaves a readable trail. bash is
    # used for PIPESTATUS so the log records the script's own exit code, not
    # tee's. The interactive sudo/gocryptfs prompts are unaffected: they read
    # the Konsole TTY directly; only the visible echo flows through tee.
    q = shlex.quote
    log = q(str(LAUNCH_LOG))
    runner = shutil.which("bash") or shutil.which("sh")
    collect = f"code=\"${{PIPESTATUS[0]}}\"; " if runner.endswith("bash") else "code=$?; "
    inner = (f"{q(python3)} {q(str(script))}{flags} 2>&1 | tee -a {log}; "
             f"{collect}printf '=== session ended rc=%s ===\\n' \"$code\" >> {log}; "
             f"if [ \"$code\" != 0 ]; then printf '\\nPrivate Desktop exited with code %s.\\nFull log: {log}\\nPress Enter to close this window.\\n' \"$code\"; read -r _ || true; fi; "
             'exit "$code"')
    wrapper = ("#!/bin/sh\n"
               "# Opens the Frame private KDE desktop controller in a Konsole window.\n"
               "# The window closes by itself when the desktop quits cleanly; on a\n"
               "# nonzero exit it waits for Enter so the error stays readable. ALL\n"
               "# output is also appended to launch.log:\n"
               "#   ~/.local/share/frame-private-desktop/launch.log\n"
               f"LOG={q(str(LAUNCH_LOG))}\n"
               'mkdir -p "${LOG%/*}" 2>/dev/null\n'
               "printf '\\n=== %s Private Desktop launch (pid %s) ===\\n' \"$(date -Is)\" \"$$\" >> \"$LOG\"\n"
               f"exec {q(terminal)} -e {q(runner)} -c {q(inner)}\n")
    MENU_WRAPPER.write_text(wrapper, encoding="utf-8")
    MENU_WRAPPER.chmod(0o755)
    MENU_ENTRY.parent.mkdir(parents=True, exist_ok=True)
    MENU_ENTRY.write_text(menu_entry_text(), encoding="utf-8")
    MENU_ENTRY.chmod(0o755)
    refresh_kde_menus()
    info(f"Installed menu launcher: {MENU_ENTRY} (Exec wrapper: {MENU_WRAPPER})")
    info('On the Frame: press + and pick "Private Desktop". If it is missing there, '
         "copy the .desktop file into ~/Desktop as well.")


def remove_menu_entry() -> None:
    MENU_ENTRY.unlink(missing_ok=True)
    MENU_WRAPPER.unlink(missing_ok=True)
    refresh_kde_menus()
    info('Removed the "Private Desktop" menu entry and its wrapper.')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--width", type=int, default=None)
    parser.add_argument("--height", type=int, default=None)
    parser.add_argument("--install-launcher", "--make-launcher", dest="install_launcher",
                        action="store_true",
                        help="Install the Frame '+' menu entry (Konsole wrapper + .desktop) and exit")
    parser.add_argument("--remove-launcher", action="store_true",
                        help="Remove the installed Frame '+' menu entry and exit")
    clipboard = parser.add_mutually_exclusive_group()
    clipboard.add_argument("--share-clipboard", dest="share_clipboard", action="store_true", default=None,
                           help="Enable bidirectional TEXT clipboard sync (default when running "
                                "inside the KDE desktop; off by default from the '+' menu)")
    clipboard.add_argument("--no-share-clipboard", dest="share_clipboard", action="store_false",
                           help="Disable clipboard sharing to keep private clipboard data off the host")
    parser.add_argument("--attach", choices=("auto", "desktop", "steam"), default="auto",
                        help="Which compositor to nest on when none is given: 'auto' prefers the "
                             "Plasma desktop session (reliable controller input), falling back to "
                             "Steam's own compositor; 'desktop'/'steam' force one (default: auto)")
    parser.add_argument("--reset-empty-vault", action="store_true",
                        help="Archive and reinitialize an EMPTY vault; don't use routinely")
    args = parser.parse_args()
    if os.geteuid() == 0:
        raise LaunchError("Run as steamos, not root")
    os.umask(0o077)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    # Menu/GameMode launchers inherit Plasma's minimal PATH without ~/.local/bin,
    # where guix-rootless puts guix-env. Prepend it so command() can find it.
    local_bin = str(Path.home() / ".local/bin")
    if local_bin not in os.environ.get("PATH", "").split(os.pathsep):
        os.environ["PATH"] = local_bin + os.pathsep + os.environ.get("PATH", "")

    home = Path.home().resolve()
    if args.install_launcher:
        install_menu_entry(args.share_clipboard, args.width, args.height)
        return 0
    if args.remove_launcher:
        remove_menu_entry()
        return 0
    args.width = args.width if args.width is not None else 1920
    args.height = args.height if args.height is not None else 1080
    if not (640 <= args.width <= 8192 and 480 <= args.height <= 8192):
        parser.error("Window dimensions must be between 640x480 and 8192x8192")
    if not sys.stdin.isatty():
        raise LaunchError("Launch from a terminal so the passphrase can be entered securely")

    # A '+'-menu launch has no WAYLAND_DISPLAY: its parent is gamescope's
    # Steam session, not the KDE desktop the bridge would sync with. Default
    # clipboard sharing OFF there; explicit --share-clipboard still enables it.
    menu_launch = not os.environ.get("WAYLAND_DISPLAY", "")
    if args.share_clipboard is None:
        args.share_clipboard = not menu_launch
        if not args.share_clipboard:
            info("Launched from the '+' menu: clipboard sharing is off by default. "
                 "Pass --share-clipboard to bridge with the desktop session anyway.")

    runtime_base, wayland_socket = find_outer_wayland(args.attach)
    state = home / REL_STATE
    state.mkdir(parents=True, exist_ok=True)
    state.chmod(0o700)
    lock_file = state / "session.lock"
    with lock_file.open("a+b") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise LaunchError("A private session launcher is already running") from exc

        cipher = state / "encrypted"
        cipher.mkdir(parents=True, exist_ok=True)
        cipher.chmod(0o700)
        # Prefer the writable ext4 filesystem where manual -fg FUSE testing
        # succeeded, rather than the /run/user tmpfs that has not been tested.
        mount = state / "mount"
        # A force-killed session leaves the FUSE connection dead: the
        # mountpoint exists but EVERY access - stat, chmod, iterdir - raises
        # ENOTCONN. Sweep such orphans before anything else touches them, so
        # a killed session can never wedge later launches.
        try:
            mount.lstat()
        except FileNotFoundError:
            pass
        except OSError as exc:
            info(f"Clearing dead leftover mount at {mount} ({exc.strerror}) ...")
            subprocess.run(["fusermount3", "-u", str(mount)], check=False)
            # os.path.ismount reports False for dead mounts, so probe
            # /proc/self/mountinfo instead of trusting it.
            if any(str(mount) in line for line in Path("/proc/self/mountinfo").read_text().splitlines()):
                subprocess.run(["umount", "-l", str(mount)], check=False)
        mount.mkdir(parents=True, exist_ok=True)
        mount.chmod(0o700)
        if os.path.ismount(mount):
            raise LaunchError(f"A previous decrypted mount is already active: {mount}; refusing to proceed")
        if any(mount.iterdir()):
            raise LaunchError(f"Mountpoint isn't empty: {mount}")

        if args.reset_empty_vault:
            ensure_empty_vault_reset(cipher)
        clipboard_bin = ensure_clipboard_tools() if args.share_clipboard else None
        binary = ensure_native_gocryptfs(home)
        command("bwrap")
        command("startplasma-wayland")
        command("kwin_wayland")
        command("dbus-run-session")
        ensure_host_fusermount_alias()

        if not (cipher / "gocryptfs.conf").is_file():
            if any(cipher.iterdir()):
                raise LaunchError("Cipher directory is nonempty but lacks gocryptfs.conf")
            info("First run: initialize your encrypted vault. Save the master key OFF the headset.")
            run(host_gocryptfs(binary) + ["-init", str(cipher)])
        else:
            info("Existing encrypted vault found. Enter your passphrase to unlock.")

        fuse_child = None
        mounted = False
        try:
            fuse_child = mount_foreground(binary, cipher, mount)
            mounted = True
            for rel in (".config", ".cache", ".local/share", ".local/state", "Documents", "Downloads"):
                (mount / rel).mkdir(parents=True, exist_ok=True)
            ensure_classic_plasma_startup(mount)
            kwin_wrapper(mount, args.width, args.height)
            # Protect the private Flatpak installation against accidental
            # symlinks back into /host-home (regular SteamOS user's data).
            for rel in (".local/share/flatpak", ".var"):
                private_location = mount / rel
                if private_location.is_symlink():
                    raise LaunchError(f"Private Flatpak path must not be a symlink: {private_location}")
                private_location.mkdir(parents=True, exist_ok=True)
            if args.share_clipboard:
                write_clipboard_bridge(mount)
                info("WARNING: Bidirectional text clipboard can expose passwords to unencrypted KDE clipboard history.")
            prepare_flatpak_bootstrap(mount, share_clipboard=args.share_clipboard)
            info("Unlocked. Starting KDE; host SYSTEM Flatpaks are read-only, user Flatpaks are private, /host-home is available.")
            # Compositor sessions can be torn down while the passphrase is
            # typed (observed: Steam's teardown killed an auto-started
            # desktop seconds after its socket appeared). Re-validate right
            # before the socket is bound into bwrap instead of dying with
            # 'Can't find source path' when that races this launch.
            if not wayland_socket.is_socket():
                info(f"The parent compositor socket vanished ({wayland_socket}); "
                     "re-discovering the compositor to attach to ...")
                runtime_base, wayland_socket = find_outer_wayland(args.attach)
            cmd = build_private_kde(home, mount, wayland_socket, runtime_base, args.width, args.height, clipboard_bin)
            return run_session(cmd)
        finally:
            if mounted or os.path.ismount(mount):
                unmount(mount)
            if fuse_child is not None:
                stop_foreground(fuse_child)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        sys.exit(main())
    except (LaunchError, subprocess.CalledProcessError, OSError) as exc:
        print(f"[{APP}] ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print(f"[{APP}] Interrupted", file=sys.stderr)
        sys.exit(130)
