#!/usr/bin/env python3
"""Steam Frame: terminal-managed gocryptfs private home and (Private) KDE launchers.

Install once:  ./frame_private_desktop.py --install-launcher
Run from the Frame '+' menu as "Private Desktop", or run the script in Konsole.
Unlocking applies --resolution 1920x1080 to the desktop by default; use
--resolution keep to leave it. Ctrl+C/SIGTERM exits, terminates private apps,
unmounts and removes launchers. A same-file watchdog attempts cleanup after
SIGKILL/crash. Preserves the encrypted vault. --remove-launcher undoes install.

Security: encryption at rest only. Your normal login user can access the
plaintext FUSE mount while open, and the host services are shared. Single-
instance applications may forward work into an already-running host process.
"""
from __future__ import annotations
import argparse
import configparser
import fcntl
import getpass
import hashlib
import json
import os
import re
import resource
import shlex
import shutil
import signal
import socket
import stat
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

APP = 'frame-private-terminal'
HOME = Path.home().resolve()
STATE = HOME / '.local/share/frame-private-desktop'
CIPHER = STATE / 'encrypted'
MOUNT = STATE / 'mount'
LAUNCH_DIR = HOME / '.local/share/applications'
MAP_PATH = MOUNT / '.local/state/frame-private-tray/launchers.json'
IPC = STATE / 'tray.sock'
LOCK = STATE / 'tray.lock'
PIDS = STATE / 'terminal-app-pids.json'
FUSE_PID = STATE / 'terminal-fuse-pid.json'
WATCHDOG_LOG = STATE / 'terminal-watchdog.log'
BIN = HOME / '.local/bin/gocryptfs-native'
LOADER = Path('/lib/ld-linux-aarch64.so.1')
HELPER = Path('/usr/bin/fusermount3')
GUIX_HELPER = Path('/run/setuid-programs/fusermount3')
PREFIX = 'frame-private-app-'
GUIX_ENV = HOME / '.local/bin/guix-env'
SETUP_LOG = STATE / 'dependency-install.log'
DESKTOP_ID = 'frame-private-desktop.desktop'
MENU_ENTRY = LAUNCH_DIR / DESKTOP_ID
MENU_WRAPPER = HOME / '.local/bin/frame-private-desktop-launch'
DEFAULT_RESOLUTION = '1920x1080'
os.umask(0o077)


def info(msg):
    print(f'[{APP}] {msg}', flush=True)


def guix_env_path():
    if GUIX_ENV.is_file() and os.access(GUIX_ENV, os.X_OK):
        return str(GUIX_ENV)
    cmd = shutil.which('guix-env')
    if cmd:
        return cmd
    raise PrivateError('Rootless guix-env not found under ~/.local/bin')


def setup_log(msg):
    mkdir_private(STATE)
    with SETUP_LOG.open('a', encoding='utf-8') as log:
        os.chmod(SETUP_LOG, 0o600)
        log.write(time.strftime('%Y-%m-%d %H:%M:%S ') + msg + '\n')
    info(msg)


def guix_run(*args, check=True):
    command = [guix_env_path(), *args]
    setup_log('Guix command: ' + shlex.join(command))
    with SETUP_LOG.open('a', encoding='utf-8') as log:
        r = subprocess.run(command, stdin=subprocess.DEVNULL,
                           stdout=log, stderr=subprocess.STDOUT)
    if check and r.returncode:
        raise PrivateError('Guix command failed: ' + shlex.join(command) +
                           '\nSee ' + str(SETUP_LOG))
    return r.returncode == 0


def guix_has_command(name):
    return guix_run('sh', '-c', 'command -v ' + shlex.quote(name) + ' >/dev/null 2>&1', check=False)


def guix_install_missing(packages):
    if packages:
        setup_log('Installing Guix packages: ' + ', '.join(packages))
        guix_run('guix', 'install', *packages)


class PrivateError(RuntimeError):
    pass


def mkdir_private(path):
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)


def mounted():
    return os.path.ismount(MOUNT)


def native_gocryptfs_works(path=BIN):
    if not LOADER.is_file() or not path.is_file():
        return False
    try:
        r = subprocess.run([str(LOADER), str(path), '-version'],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=15)
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def ensure_native_gocryptfs():
    # Exact v13 layout: the Guix ELF is copied and run with the SteamOS loader.
    if native_gocryptfs_works():
        return
    if not LOADER.is_file():
        raise PrivateError(f'Steam Frame ARM64 dynamic loader is missing: {LOADER}')
    if not guix_has_command('gocryptfs'):
        guix_install_missing(['gocryptfs'])
    if not guix_has_command('gocryptfs'):
        raise PrivateError(f'gocryptfs not available in Guix after installation. See {SETUP_LOG}')
    BIN.parent.mkdir(parents=True, exist_ok=True)
    tmp = BIN.with_name(BIN.name + f'.tmp-{os.getpid()}')
    tmp.unlink(missing_ok=True)
    try:
        # Pass $1 as a positional argument, not in a string of shell syntax.
        script = 'set -eu; src=$(readlink -f "$(command -v gocryptfs)"); install -m 755 "$src" "$1"'
        guix_run('sh', '-c', script, 'frame-private-tray', str(tmp))
        if not native_gocryptfs_works(tmp):
            raise PrivateError('Guix gocryptfs copy cannot run via SteamOS loader. '
                               f'Left the old binary untouched. See {SETUP_LOG}')
        os.replace(tmp, BIN)
        setup_log(f'Native gocryptfs installed/updated: {BIN}')
    finally:
        tmp.unlink(missing_ok=True)


def gocryptfs_cmd():
    ensure_native_gocryptfs()
    return [str(LOADER), str(BIN)]


def ensure_bwrap():
    if shutil.which('bwrap'):
        return
    if not guix_has_command('bwrap'):
        guix_install_missing(['bubblewrap'])
    if not guix_has_command('bwrap'):
        raise PrivateError('Bubblewrap unavailable after Guix installation; see dependency-install.log')
    # The Guix wrapper adds its profile's commands to PATH, but we must not
    # run a copied Guix ELF directly against SteamOS shared libraries.
    if not os.environ.get('_FRAME_PRIVATE_TERMINAL_GUIX'):
        print('Restarting under your existing guix-env to use Bubblewrap...', flush=True)
        env = os.environ.copy()
        env['_FRAME_PRIVATE_TERMINAL_GUIX'] = '1'
        os.execve(guix_env_path(), [guix_env_path(), 'python3', str(Path(__file__).resolve()), *sys.argv[1:]], env)
    if not shutil.which('bwrap'):
        raise PrivateError('Bubblewrap was installed but is unavailable in guix-env PATH')


def ensure_fuse_helper():
    """Use the host's trusted setuid FUSE helper at Guix's expected pathname."""
    if not HELPER.is_file() or not (HELPER.stat().st_mode & stat.S_ISUID):
        raise PrivateError('The SteamOS host requires a setuid /usr/bin/fusermount3')
    if GUIX_HELPER.exists() or GUIX_HELPER.is_symlink():
        if GUIX_HELPER.resolve() != HELPER.resolve():
            raise PrivateError('Unexpected /run/setuid-programs/fusermount3; refusing overwrite')
        return
    if GUIX_HELPER.parent.exists():
        s = GUIX_HELPER.parent.lstat()
        if not stat.S_ISDIR(s.st_mode) or s.st_uid != 0:
            raise PrivateError('Unsafe /run/setuid-programs ownership')
    if not sys.stdin.isatty():
        raise PrivateError('FUSE helper alias missing; run this app from an interactive terminal')
    if not shutil.which('sudo'):
        raise PrivateError('sudo is needed once per boot to create the FUSE helper alias')
    print('Creating temporary Guix FUSE helper alias (sudo may ask for your login password).', flush=True)
    for task in (["sudo", "install", "-d", "-m", "0755", str(GUIX_HELPER.parent)],
                 ["sudo", "ln", "-s", str(HELPER), str(GUIX_HELPER)]):
        subprocess.run(task, check=True)
    if GUIX_HELPER.resolve() != HELPER.resolve():
        raise PrivateError('FUSE helper setup failed')


def passfile_call(cmd, password, *, background=False, log=None):
    """Send passphrase via inherited anonymous pipe: never argv, env, or disk."""
    rd, wr = os.pipe()
    try:
        os.write(wr, password.encode('utf-8') + b'\n')
        os.close(wr)
        wr = -1
        full = [*cmd, '-passfile', f'/proc/self/fd/{rd}']
        if background:
            return subprocess.Popen(full, pass_fds=(rd,), stdin=subprocess.DEVNULL,
                                    stdout=log, stderr=subprocess.STDOUT)
        return subprocess.run(full, pass_fds=(rd,), stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    finally:
        os.close(rd)
        if wr >= 0:
            os.close(wr)


def private_runtime():
    base = Path(f'/run/user/{os.getuid()}')
    if not base.is_dir() or base.stat().st_uid != os.getuid():
        raise PrivateError('No valid host runtime directory')
    env_runtime = Path(os.environ.get('XDG_RUNTIME_DIR', str(base)))
    if not env_runtime.is_absolute() or not env_runtime.resolve().is_relative_to(base.resolve()):
        raise PrivateError('XDG_RUNTIME_DIR is not within the current user runtime')
    display = os.environ.get('WAYLAND_DISPLAY', '')
    if not display:
        raise PrivateError('Run inside your active KDE Wayland desktop session')
    wayland = Path(display) if display.startswith('/') else env_runtime / display
    if not wayland.is_socket() or not wayland.resolve().is_relative_to(base.resolve()):
        raise PrivateError('The current Plasma Wayland socket is missing')
    # Prefer the exact bus of the CURRENT Plasma session, not a second bus
    # that happens to exist in the Steam Frame nested_plasma runtime.
    addr = os.environ.get('DBUS_SESSION_BUS_ADDRESS', '')
    match = re.search(r'(?:^|;)unix:path=([^,;]+)', addr)
    bus = Path(match.group(1)) if match else base / 'bus'
    if not bus.is_socket():
        bus = env_runtime / 'bus'
    if not bus.is_socket() or not bus.resolve().is_relative_to(base.resolve()):
        raise PrivateError('No existing KDE session D-Bus socket found')
    return base, wayland, bus


def resolver_bind():
    target = Path('/etc/resolv.conf').resolve()
    if not target.is_file():
        raise PrivateError('Cannot find /etc/resolv.conf target')
    return [] if str(target) == '/etc/resolv.conf' else ['--ro-bind', str(target), str(target)]


def audio_sockets(base, wayland):
    dirs = [base, wayland.parent]
    if os.environ.get('PIPEWIRE_RUNTIME_DIR'):
        dirs.append(Path(os.environ['PIPEWIRE_RUNTIME_DIR']))
    result = {}
    for folder in dirs:
        try:
            folder = folder.resolve()
            if not folder.is_relative_to(base.resolve()):
                continue
            for name in ('pipewire-0', 'pulse/native'):
                p = folder / name
                if name not in result and p.is_socket() and p.stat().st_uid == os.getuid():
                    result[name] = p
        except OSError:
            continue
    return result


def sandbox_command(program, arguments):
    """One app in its own bwrap namespace, using host desktop *services*."""
    base, wayland, bus = private_runtime()
    guix = HOME / '.local/share/guix'
    if not (guix / 'gnu/store').is_dir() or not (guix / 'var/guix').is_dir():
        raise PrivateError('Rootless Guix store not found at ~/.local/share/guix')
    if not mounted():
        raise PrivateError('Encrypted home is locked')
    bwrap = shutil.which('bwrap')
    if not bwrap:
        raise PrivateError('The SteamOS host needs bubblewrap (bwrap)')

    uid = os.getuid()
    user = os.environ.get('USER', HOME.name)
    runtime = f'/run/user/{uid}'
    inside_wayland = 'frame-host-wayland'
    profile = '/opt/guix-profile'
    audio = audio_sockets(base, wayland)
    env = {
        'HOME': str(HOME), 'USER': user, 'LOGNAME': user,
        'SHELL': '/bin/sh',
        'PATH': f'{HOME}/.local/bin:{profile}/bin:/usr/local/bin:/usr/bin:/bin',
        'GUIX_PROFILE': profile,
        'XDG_RUNTIME_DIR': runtime,
        'WAYLAND_DISPLAY': inside_wayland,
        'DBUS_SESSION_BUS_ADDRESS': f'unix:path={runtime}/bus',
        'XDG_SESSION_TYPE': 'wayland', 'XDG_CURRENT_DESKTOP': 'KDE',
        'XDG_SESSION_DESKTOP': 'KDE', 'KDE_FULL_SESSION': 'true',
        'KDE_SESSION_VERSION': '6', 'QT_QPA_PLATFORM': 'wayland',
        'XDG_CONFIG_HOME': str(HOME / '.config'),
        'XDG_DATA_HOME': str(HOME / '.local/share'),
        'XDG_CACHE_HOME': str(HOME / '.cache'),
        'XDG_STATE_HOME': str(HOME / '.local/state'),
        'XDG_CONFIG_DIRS': '/etc/xdg',
        'XDG_DATA_DIRS': f'{HOME}/.local/share/flatpak/exports/share:/var/lib/flatpak/exports/share:{profile}/share:/usr/local/share:/usr/share',
        'FLATPAK_USER_DIR': f'{HOME}/.local/share/flatpak',
        'FLATPAK_SYSTEM_DIR': '/var/lib/flatpak',
        'LANG': os.environ.get('LANG', 'C.UTF-8'),
        'TMPDIR': '/tmp',
    }
    for extra in ('LC_ALL', 'LC_MESSAGES', 'TZ', 'GTK_THEME', 'QT_STYLE_OVERRIDE', 'XDG_MENU_PREFIX'):
        if extra in os.environ:
            env[extra] = os.environ[extra]
    if 'pulse/native' in audio:
        env['PULSE_SERVER'] = f'unix:{runtime}/pulse/native'
    # Xwayland fallback for non-Wayland-native applications.
    xdisplay = os.environ.get('DISPLAY', '')
    if re.fullmatch(r':\d+(?:\.\d+)?', xdisplay):
        env['DISPLAY'] = xdisplay
    if 'pipewire-0' in audio:
        env['PIPEWIRE_RUNTIME_DIR'] = runtime

    a = [bwrap, '--die-with-parent', '--new-session', '--unshare-pid', '--unshare-ipc',
         '--share-net', '--tmpfs', '/', '--ro-bind', '/usr', '/usr',
         '--ro-bind', '/etc', '/etc',
         '--symlink', 'usr/bin', '/bin', '--symlink', 'usr/lib', '/lib',
         '--symlink', 'usr/lib64', '/lib64', '--symlink', 'usr/sbin', '/sbin',
         '--dir', '/gnu', '--bind', str(guix / 'gnu/store'), '/gnu/store',
         '--dir', '/var', '--bind', str(guix / 'var/guix'), '/var/guix',
         '--ro-bind-try', '/var/lib/flatpak', '/var/lib/flatpak',
         '--dir', '/opt',
         '--symlink', f'/var/guix/profiles/per-user/{user}/guix-profile', profile,
         # This is the key: decrypted mount masks user's real home.
         # No /host-home bind; private apps do not get the parent user's files.
         '--dir', '/home', '--bind', str(MOUNT), str(HOME),
         '--tmpfs', '/run', '--dir', '/run/user', '--perms', '0700',
         '--dir', runtime,
         '--ro-bind', str(wayland), f'{runtime}/{inside_wayland}',
         '--ro-bind', str(bus), f'{runtime}/bus',
         '--tmpfs', '/tmp', '--dev', '/dev', '--proc', '/proc',
         '--dev-bind-try', '/dev/dri', '/dev/dri',
         '--dev-bind-try', '/dev/snd', '/dev/snd',
         '--dev-bind-try', '/dev/fuse', '/dev/fuse',
         '--ro-bind-try', '/sys', '/sys',
         '--chdir', str(HOME), '--clearenv']
    a += resolver_bind()
    if 'DISPLAY' in env and Path('/tmp/.X11-unix').is_dir():
        a += ['--ro-bind', '/tmp/.X11-unix', '/tmp/.X11-unix']
        auth = os.environ.get('XAUTHORITY', '')
        if auth and Path(auth).is_file():
            # The private home may hide ~/.Xauthority. Bind just that cookie
            # file into a private runtime path, rather than the host home.
            a += ['--ro-bind', auth, f'{runtime}/frame-xauthority']
            env['XAUTHORITY'] = f'{runtime}/frame-xauthority'
    if 'pulse/native' in audio:
        a += ['--dir', f'{runtime}/pulse']
    for name, sock in audio.items():
        a += ['--ro-bind', str(sock), f'{runtime}/{name}']
    # Some applications use system D-Bus; only pass its socket, not host /run.
    system_bus = Path('/run/dbus/system_bus_socket')
    if system_bus.is_socket():
        a += ['--dir', '/run/dbus', '--ro-bind', str(system_bus), '/run/dbus/system_bus_socket']
    for key, val in env.items():
        a += ['--setenv', key, val]
    # Guix programs run through the rootless Guix profile PATH automatically.
    return a + ['--', program, *arguments]


def read_desktop(path):
    try:
        cp = configparser.RawConfigParser(strict=False, interpolation=None)
        cp.optionxform = str
        cp.read(path, encoding='utf-8')
        if not cp.has_section('Desktop Entry'):
            return None
        d = dict(cp.items('Desktop Entry'))
        if d.get('Type', 'Application') != 'Application' or d.get('Hidden', '').lower() == 'true':
            return None
        if d.get('NoDisplay', '').lower() == 'true' or not d.get('Exec') or not d.get('Name'):
            return None
        if d.get('Exec', '').strip().startswith('env '):
            pass  # handled as an executable + args below
        return d
    except (OSError, UnicodeError, configparser.Error):
        return None


def is_parent_flatpak_entry(path, d):
    # Explicitly NEVER import the host's per-user Flatpaks.
    excluded = (HOME / '.local/share/flatpak').resolve()
    try:
        if path.resolve().is_relative_to(excluded):
            return True
    except (OSError, RuntimeError):
        return True
    if path.parent.resolve() == (HOME / '.local/share/applications').resolve():
        # System installs are represented separately by /var/lib/flatpak.
        # Private-user installs are represented by MOUNT/.local/share/flatpak.
        if re.search(r'(^|\s)(?:/\S+/)?flatpak\s+(?:\S+\s+)*run(?:\s|$)', d.get('Exec', '')):
            return True
    exe = d.get('Exec', '')
    if re.search(r'(^|\s)flatpak\s+(?:\S+\s+)*--user(?:\s|$)', exe):
        return True
    return False


def scan_launchers():
    sources = [
        MOUNT / '.local/share/applications',
        MOUNT / '.local/share/flatpak/exports/share/applications',
        HOME / '.local/share/applications',
        HOME / '.guix-profile/share/applications',
        Path('/var/lib/flatpak/exports/share/applications'),
        Path('/usr/local/share/applications'),
        Path('/usr/share/applications'),
    ]
    found, used_ids = {}, set()
    for folder in sources:
        if not folder.is_dir():
            continue
        for path in sorted(folder.rglob('*.desktop')):
            if path.name.startswith(PREFIX) or path.name == DESKTOP_ID:
                continue
            d = read_desktop(path)
            if not d or is_parent_flatpak_entry(path, d):
                continue
            # Prefer higher-priority XDG app definitions (including private overrides).
            appid = path.name
            if appid in used_ids:
                continue
            used_ids.add(appid)
            ident = hashlib.sha256(str(path).encode()).hexdigest()[:24]
            found[ident] = {'path': str(path), 'exec': d['Exec'],
                            'name': d['Name'], 'icon': d.get('Icon', 'application-x-executable'),
                            'categories': d.get('Categories', 'Utility;'),
                            'terminal': d.get('Terminal', 'false'),
                            'working_dir': d.get('Path', ''),
                            'startup_wm_class': d.get('StartupWMClass', '')}
    return found


def desktop_escape(s):
    return s.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '')


def desktop_entry(identity, d, script):
    # Don't carry through actions, DBusActivatable, TryExec or foreign Exec.
    # Force all invocations into this one script's authenticated IPC launcher.
    name = d['name'].replace('\n', ' ') + ' (Private)'
    exec_line = f'"{desktop_escape(str(script))}" --launch {identity} %U'
    # The script has a python3 shebang and is installed as executable.
    return ('[Desktop Entry]\nType=Application\n'
            f'Name={name}\n'
            f'Comment=Run with encrypted private home and Guix profile\n'
            f'Icon={d["icon"]}\n'
            f'Exec={exec_line}\n'
            f'Categories={d["categories"]}\n'
            'Terminal=false\nStartupNotify=true\nDBusActivatable=false\n')


def atomic_write(path, data, perm=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f'.tmp-{os.getpid()}')
    try:
        with tmp.open('w', encoding='utf-8') as f:
            os.chmod(tmp, perm)
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def remove_generated_launchers():
    # Only files under our exact basename prefix. Other launchers are untouched.
    if not LAUNCH_DIR.is_dir():
        return
    for path in LAUNCH_DIR.glob(PREFIX + '*.desktop'):
        if path.is_file() or path.is_symlink():
            path.unlink()
    update_kde_cache()


def update_kde_cache():
    command = shutil.which('kbuildsycoca6')
    if command:
        subprocess.Popen([command], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL, start_new_session=True)


def menu_entry_text():
    # The Frame '+' menu / Add-Library-Shortcut scanner wants: absolute Exec,
    # no spaces in the Exec line, Type=Application, no NoDisplay, no
    # Categories=Settings. The generated wrapper keeps the Exec to one token.
    return ('[Desktop Entry]\n'
            'Type=Application\n'
            'Version=1.0\n'
            'Name=Private Desktop\n'
            'GenericName=Encrypted desktop\n'
            'Comment=gocryptfs encrypted home with (Private) app launchers\n'
            f'Exec={MENU_WRAPPER}\n'
            'Icon=dialog-password\n'
            'Terminal=false\n'
            'Categories=Utility;System;\n'
            'StartupNotify=false\n')


def install_menu_entry():
    terminal = shutil.which('konsole') or shutil.which('xterm')
    if not terminal:
        raise PrivateError('Konsole not found; install it or run the script from a terminal yourself')
    script = Path(__file__).resolve()
    MENU_WRAPPER.parent.mkdir(parents=True, exist_ok=True)
    wrapper = ('#!/bin/sh\n'
               '# Opens the Frame Private Desktop controller in a Konsole window.\n'
               f'exec {shlex.quote(terminal)} -e {shlex.quote(str(script))}\n')
    atomic_write(MENU_WRAPPER, wrapper, 0o755)
    atomic_write(MENU_ENTRY, menu_entry_text(), 0o755)
    update_kde_cache()
    info(f'Installed menu launcher: {MENU_ENTRY} (Exec wrapper: {MENU_WRAPPER})')
    info('On the Frame: press + and pick "Private Desktop". If it is missing there, '
         'copy the .desktop file into ~/Desktop as well.')


def remove_menu_entry():
    MENU_ENTRY.unlink(missing_ok=True)
    MENU_WRAPPER.unlink(missing_ok=True)
    update_kde_cache()
    info('Removed the "Private Desktop" menu entry and its wrapper.')


def make_launchers(script):
    if not mounted():
        raise PrivateError('Cannot generate launchers while locked')
    mapping = scan_launchers()
    if not mapping:
        raise PrivateError('No launchers discovered; check the system desktop entries and Guix profile')
    mkdir_private(MAP_PATH.parent)
    atomic_write(MAP_PATH, json.dumps(mapping, ensure_ascii=False, indent=2), 0o600)
    LAUNCH_DIR.mkdir(parents=True, exist_ok=True)
    expected = set()
    for ident, d in mapping.items():
        target = LAUNCH_DIR / f'{PREFIX}{ident}.desktop'
        expected.add(target.name)
        atomic_write(target, desktop_entry(ident, d, script), 0o644)
    for path in LAUNCH_DIR.glob(PREFIX + '*.desktop'):
        if path.name not in expected:
            path.unlink()
    update_kde_cache()
    return len(mapping)


def parse_exec(s, d, urls):
    """Expand common freedesktop Exec substitutions WITHOUT invoking a shell."""
    try:
        parts = shlex.split(s, posix=True)
    except ValueError as exc:
        raise PrivateError('Invalid .desktop Exec entry: ' + str(exc)) from exc
    out, used_files = [], False
    files = list(urls)
    for t in parts:
        if t in ('%f', '%u'):
            used_files = True
            if files:
                out.append(files[0])
            continue
        if t in ('%F', '%U'):
            used_files = True
            out.extend(files)
            continue
        if t == '%i':
            if d.get('icon'):
                out.extend(['--icon', d['icon']])
            continue
        if t == '%c':
            out.append(d['name'])
            continue
        if t == '%k':
            out.append(d['path'])
            continue
        # Unknown freedesktop field codes are ignored. %% means a literal %.
        t = t.replace('%%', '\uFFF0')
        t = re.sub(r'%[a-zA-Z]', '', t).replace('\uFFF0', '%')
        if t:
            out.append(t)
    if not used_files:
        out.extend(files)
    # Guix-generated host .desktop entries often call the outer `guix-env`
    # wrapper. We are ALREADY inside the private Guix profile, and that host
    # wrapper is hidden by the private HOME bind, so remove just that prefix.
    if out and Path(out[0]).name == 'guix-env':
        out = out[1:]
    # GUIX profile symlink is masked by private HOME. Inside the namespace the
    # equivalent real rootless Guix profile is mounted at /opt/guix-profile.
    if out and out[0].startswith(str(HOME / '.guix-profile') + '/'):
        out[0] = '/opt/guix-profile/' + out[0][len(str(HOME / '.guix-profile')) + 1:]
    if not out:
        raise PrivateError('Empty .desktop Exec entry')
    return out


def active_launch_by_id(ident, file_args):
    if not re.fullmatch('[0-9a-f]{24}', ident):
        raise PrivateError('Invalid launcher ID')
    if not mounted() or not MAP_PATH.is_file():
        raise PrivateError('Private home locked or launcher metadata missing')
    mapping = json.loads(MAP_PATH.read_text(encoding='utf-8'))
    d = mapping.get(ident)
    if d is None:
        raise PrivateError('This private launcher no longer exists; refresh launchers')
    argv = parse_exec(d['exec'], d, file_args)
    # Explicitly separate the executable and its args; never run shell=True.
    return sandbox_command(argv[0], argv[1:])


def send_request(ident, urls):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(5)
        s.connect(str(IPC))
        s.sendall((json.dumps({'id': ident, 'args': urls}) + '\n').encode())
        data = b''
        while b'\n' not in data and len(data) < 8192:
            part = s.recv(4096)
            if not part:
                break
            data += part
    if not data:
        raise PrivateError('Tray did not respond')
    reply = json.loads(data.split(b'\n')[0])
    if not reply.get('ok'):
        raise PrivateError(reply.get('error', 'Launch failed'))




def start_ticks(pid):
    """Linux process start-time ticks prevent killing a recycled PID."""
    try:
        return int(Path(f'/proc/{pid}/stat').read_text().rpartition(') ')[2].split()[19])
    except (OSError, ValueError, IndexError):
        return None


def active_groups():
    """Only return process groups still owned by the recorded process lifetime."""
    try:
        rows = json.loads(PIDS.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return []
    result = []
    for record in rows if isinstance(rows, list) else []:
        try:
            pid, tick = int(record['pid']), int(record['start'])
            if pid > 1 and pid != os.getpid() and start_ticks(pid) == tick:
                result.append((pid, tick))
        except (TypeError, KeyError, ValueError):
            continue
    return result


def stop_private_apps():
    groups = active_groups()
    for pgid, ticks in groups:
        try:
            if start_ticks(pgid) == ticks:
                os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except PermissionError:
            info(f'Could not terminate process group {pgid}')
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline and any(start_ticks(p) == t for p, t in groups):
        time.sleep(.15)
    for pgid, ticks in groups:
        try:
            if start_ticks(pgid) == ticks:
                os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError:
            info(f'Could not force-stop process group {pgid}')


def verified_fuse_mount():
    """Never attempt to unmount an unrelated mount placed at our path."""
    if not mounted():
        return False
    target = str(MOUNT).replace(' ', '\\040')
    try:
        for row in Path('/proc/self/mountinfo').read_text().splitlines():
            left, sep, right = row.partition(' - ')
            fields = left.split()
            if sep and len(fields) > 4 and fields[4] == target:
                typ = right.split()[0]
                if typ == 'fuse.gocryptfs' or typ == 'fuse' or typ.startswith('fuse.gocryptfs'):
                    return True
                raise PrivateError(f'Refusing to unmount unexpected filesystem ({typ}) at {MOUNT}')
    except OSError as e:
        raise PrivateError(f'Cannot verify decrypted FUSE mount: {e}') from e
    raise PrivateError('Mount is present but cannot verify that it is gocryptfs')


def unmount_private(*, retries=12):
    if not mounted():
        return
    verified_fuse_mount()
    if not HELPER.is_file():
        raise PrivateError(f'Host unmount helper unavailable: {HELPER}')
    last_error = ''
    for _ in range(retries):
        result = subprocess.run([str(HELPER), '-u', str(MOUNT)],
                                capture_output=True, text=True, timeout=10)
        if not mounted():
            return
        last_error = (result.stderr or result.stdout).strip()
        time.sleep(.5)
    raise PrivateError('Decrypted home is STILL MOUNTED; close applications using it. '\
                       + last_error + '\nDo not suspend or reboot assuming it is encrypted.')


def disable_old_gui_autostart():
    # These are entries produced by the previous two versions, not user launchers.
    for filename in ('frame-private-tray.desktop', 'frame-private-home.desktop'):
        for location in (LAUNCH_DIR, HOME / '.config/autostart'):
            (location / filename).unlink(missing_ok=True)


def prepare_decrypted_directories():
    for rel in ('.config', '.local/share', '.local/state', '.cache', '.var',
                'Downloads', 'Documents', '.local/share/flatpak'):
        path = MOUNT / rel
        if path.is_symlink():
            raise PrivateError('Private home has unexpected symlink: ' + str(path))
        mkdir_private(path)


def unlock_home():
    mkdir_private(STATE)
    mkdir_private(CIPHER)
    mkdir_private(MOUNT)
    if mounted():
        raise PrivateError('Encrypted home is already mounted')
    if any(MOUNT.iterdir()):
        raise PrivateError(f'Decrypted mount directory is not empty: {MOUNT}')
    ensure_bwrap()
    ensure_native_gocryptfs()
    ensure_fuse_helper()
    new = not (CIPHER / 'gocryptfs.conf').is_file()
    if new and any(CIPHER.iterdir()):
        raise PrivateError('Encrypted directory has contents but no gocryptfs.conf. Refusing to overwrite it.')
    prompt = 'Choose password for NEW encrypted home: ' if new else 'Encrypted home password: '
    password = getpass.getpass(prompt)
    if not password:
        raise PrivateError('Empty passwords are not supported')
    if new and password != getpass.getpass('Repeat new password: '):
        raise PrivateError('Password confirmation does not match')
    cmd = gocryptfs_cmd()
    if new:
        info('Initializing a NEW encrypted vault. Existing vaults are never reset.')
        init = passfile_call([*cmd, '-init', str(CIPHER)], password)
        if init.returncode:
            raise PrivateError('Vault initialization failed:\n' + init.stdout[-2000:])
        # The initialization output may contain a recovery master key; show it
        # only on the terminal, never save it to an unencrypted file.
        if init.stdout.strip():
            print('\nSAVE THESE NEW-VAULT RECOVERY DETAILS SECURELY:\n' + init.stdout + '\n', flush=True)
    logpath = STATE / 'gocryptfs.log'
    with logpath.open('ab') as log:
        os.chmod(logpath, 0o600)
        child = passfile_call([*cmd, '-fg', str(CIPHER), str(MOUNT)], password,
                              background=True, log=log)
    deadline = time.monotonic() + 30
    while not mounted():
        if child.poll() is not None:
            raise PrivateError(f'gocryptfs mount failed (exit {child.returncode}); see {logpath}')
        if time.monotonic() >= deadline:
            child.terminate()
            raise PrivateError(f'gocryptfs mount timed out; see {logpath}')
        time.sleep(.15)
    if child.poll() is not None:
        raise PrivateError(f'gocryptfs stopped unexpectedly; see {logpath}')
    prepare_decrypted_directories()
    return child


class TerminalSession:
    def __init__(self, script):
        self.script = script
        self.procs = {}
        self.children_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.closing = threading.Event()
        self.server = None
        self.server_thread = None
        self.fuse = None
        self.watchdog = None
        self.watchdog_pipe = None

    def save_pids_locked(self):
        rows = [{'pid': p.pid, 'start': start_ticks(p.pid)}
                for p in self.procs.values() if p.poll() is None]
        rows = [p for p in rows if p['start'] is not None]
        atomic_write(PIDS, json.dumps(rows), 0o600)

    def spawn(self, cmd):
        with self.children_lock:
            if self.closing.is_set() or not mounted():
                raise PrivateError('Private home is shutting down or unavailable')
            child = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     start_new_session=True, close_fds=True)
            self.procs[child.pid] = child
            self.save_pids_locked()

    def serve(self):
        while not self.closing.is_set():
            try:
                conn, _ = self.server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with conn:
                conn.settimeout(5)
                try:
                    creds = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
                    _pid, peer_uid, _gid = struct.unpack('3i', creds)
                    if peer_uid != os.getuid():
                        raise PrivateError('Unauthorized launcher request')
                    line = bytearray()
                    while b'\n' not in line and len(line) < 65536:
                        block = conn.recv(4096)
                        if not block:
                            break
                        line.extend(block)
                    data = json.loads(bytes(line).split(b'\n')[0])
                    ident = data.get('id')
                    args = data.get('args', [])
                    if (not isinstance(args, list) or len(args) > 100 or
                            not all(isinstance(v, str) and len(v) <= 8192 for v in args)):
                        raise PrivateError('Invalid launcher arguments')
                    cmd = active_launch_by_id(ident, args)
                    self.spawn(cmd)
                    answer = {'ok': True}
                except Exception as e:
                    answer = {'ok': False, 'error': str(e)}
                    info('Private launcher error: ' + str(e))
                try:
                    conn.sendall((json.dumps(answer) + '\n').encode())
                except OSError:
                    pass

    def start_server(self):
        # This socket belongs to us only while the global lock is held.
        IPC.unlink(missing_ok=True)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(IPC))
        os.chmod(IPC, 0o600)
        server.listen(16)
        server.settimeout(.5)
        self.server = server
        self.server_thread = threading.Thread(target=self.serve, daemon=True)
        self.server_thread.start()

    def start_watchdog(self):
        reader, writer = os.pipe()
        log = WATCHDOG_LOG.open('ab')
        os.chmod(WATCHDOG_LOG, 0o600)
        try:
            self.watchdog = subprocess.Popen(
                [sys.executable, str(self.script), '--watchdog', str(reader)],
                pass_fds=(reader,), close_fds=True, start_new_session=True,
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
            self.watchdog_pipe = writer
            writer = -1
        finally:
            log.close()
            os.close(reader)
            if writer != -1:
                os.close(writer)

    def refresh_pids(self):
        with self.children_lock:
            for pid, child in list(self.procs.items()):
                if child.poll() is not None:
                    self.procs.pop(pid, None)
            self.save_pids_locked()

    def close_server(self):
        self.closing.set()
        if self.server:
            self.server.close()
        if self.server_thread:
            self.server_thread.join(timeout=3)
        IPC.unlink(missing_ok=True)

    def cleanup(self):
        self.close_server()
        errors = []
        # Remove launchers FIRST so nothing new can be started during teardown.
        try:
            remove_generated_launchers()
        except Exception as e:
            errors.append('Removing launchers: ' + str(e))
        try:
            stop_private_apps()
            unmount_private()
        except Exception as e:
            errors.append('Unmounting: ' + str(e))
        if not mounted():
            PIDS.unlink(missing_ok=True)
            FUSE_PID.unlink(missing_ok=True)
            if self.fuse and self.fuse.poll() is None:
                self.fuse.terminate()
                try:
                    self.fuse.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.fuse.kill()
            self.fuse = None
            info('Private home locked; (Private) launchers removed.')
        else:
            info('WARNING: decrypted home remains mounted!')
        # Watchdog should run its own attempt if cleanup failed.
        if self.watchdog_pipe is not None:
            try:
                if not errors and not mounted():
                    os.write(self.watchdog_pipe, b'CLEAN\n')
            except OSError:
                pass
            os.close(self.watchdog_pipe)
            self.watchdog_pipe = None
        if errors:
            raise PrivateError('; '.join(errors))


def watchdog_main(read_fd):
    # A separate process running this SAME FILE survives SIGKILL of the parent.
    # No password or plaintext files are given to it, only a pipe for liveness.
    try:
        complete = b''
        while True:
            data = os.read(read_fd, 128)
            if not data:
                break
            complete += data
        if complete == b'CLEAN\n':
            return 0
        mkdir_private(STATE)
        with LOCK.open('a+b') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                # Another live instance owns the lock and is responsible for
                # recovery. Never kill a newer session's applications.
                return 0
            info('Main process ended unexpectedly; watchdog starting cleanup.')
            IPC.unlink(missing_ok=True)
            remove_generated_launchers()
            stop_private_apps()
            unmount_private(retries=16)
            if not mounted():
                PIDS.unlink(missing_ok=True)
                try:
                    fuse = json.loads(FUSE_PID.read_text())
                    pid, ticks = int(fuse['pid']), fuse['start']
                    if ticks is not None and start_ticks(pid) == ticks:
                        os.kill(pid, signal.SIGTERM)
                except (OSError, ValueError, TypeError, KeyError, ProcessLookupError):
                    pass
                FUSE_PID.unlink(missing_ok=True)
                info('Watchdog cleanup completed.')
        return 0
    except Exception as e:
        info('WATCHDOG CLEANUP INCOMPLETE: ' + str(e))
        return 2
    finally:
        os.close(read_fd)


def recover_previous_mount():
    # A killed previous invocation could have left a gocryptfs mount and stale
    # launchers. The lock assures no other copy of this controller is live.
    remove_generated_launchers()
    if mounted():
        if not FUSE_PID.is_file():
            raise PrivateError('The encrypted home is already mounted by an unknown or older '
                               'controller. Exit that controller and unmount it first; '
                               'refusing to interrupt another private desktop.')
        info('Found a previous terminal-controller mount; recovering before unlocking.')
        stop_private_apps()
        unmount_private(retries=16)
    PIDS.unlink(missing_ok=True)
    FUSE_PID.unlink(missing_ok=True)
    IPC.unlink(missing_ok=True)


def setup_dependencies():
    if not LOADER.is_file():
        raise PrivateError(f'Steam Frame ARM64 loader is missing: {LOADER}')
    # No Qt, PyQt, Tk, or XWayland plugin installations required.
    ensure_bwrap()
    ensure_native_gocryptfs()
    ensure_fuse_helper()


def parse_resolution(spec):
    spec = (spec or '').strip().lower()
    if spec in ('', 'keep', 'none', 'off', 'native'):
        return None
    match = re.fullmatch(r'(\d{2,5})x(\d{2,5})', spec)
    if not match:
        raise PrivateError('--resolution expects WxH like 1920x1080, or "keep"')
    return int(match.group(1)), int(match.group(2))


def _active_mode_is(ksd, width, height):
    try:
        listing = subprocess.run([ksd, '-o'], stdin=subprocess.DEVNULL,
                                 capture_output=True, text=True, timeout=15).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ''
    if re.search(rf'{width}x{height}@[\d.]+\*', listing):
        return f'{width}x{height}'
    if listing:
        current = re.findall(r'(\d{2,5}x\d{2,5})@[\d.]+\*', listing)
        if current:
            return current[0]
    return ''


def apply_resolution(width, height):
    """Best-effort resize of the nested Plasma output via KScreen (kscreen-doctor).

    This changes the resolution of the desktop UI that the Frame shows on its
    virtual screen. Never fatal: a session KScreen cannot resize keeps its
    current mode and the private desktop still works.
    """
    ksd = shutil.which('kscreen-doctor')
    if not ksd:
        info('Resolution: kscreen-doctor not found; keeping the current desktop resolution.')
        return
    current = _active_mode_is(ksd, width, height)
    if current == f'{width}x{height}':
        info(f'Desktop already at {width}x{height}.')
        return
    try:
        listing = subprocess.run([ksd, '-o'], stdin=subprocess.DEVNULL,
                                 capture_output=True, text=True, timeout=15).stdout
    except (OSError, subprocess.TimeoutExpired):
        listing = ''
    outputs = sorted(set(re.findall(r'(?<![A-Za-z0-9])([A-Za-z]+-[A-Za-z0-9.]+)(?![\w-])', listing)))
    scopes = ['all'] + outputs[:6]
    target = f'{width}x{height}'
    for scope in scopes:
        for mode in (target, f'{target}@60', f'{target}@60.00'):
            try:
                r = subprocess.run([ksd, f'output.{scope}.mode.{mode}'],
                                   stdin=subprocess.DEVNULL, capture_output=True,
                                   text=True, timeout=20)
            except (OSError, subprocess.TimeoutExpired):
                continue
            if r.returncode == 0 and _active_mode_is(ksd, width, height) == target:
                info(f'Desktop resolution set to {target} (output {scope}).')
                return
    info(f'Could not switch the desktop to {target} (currently {current or "unknown"}); '
         'keeping the existing resolution. Try again, use --resolution WxH, '
         'or run kscreen-doctor manually in Konsole.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--launch', nargs='+', metavar='ID_OR_URI',
                        help=argparse.SUPPRESS)
    parser.add_argument('--watchdog', type=int, help=argparse.SUPPRESS)
    parser.add_argument('--resolution', default=os.environ.get('FRAME_PRIVATE_RESOLUTION',
                                                               DEFAULT_RESOLUTION),
                        metavar='WxH', help='Desktop resolution applied on unlock '
                        f'(default: {DEFAULT_RESOLUTION}; "keep" leaves it untouched)')
    parser.add_argument('--install-launcher', action='store_true',
                        help='Install the Frame "+" menu entry (Konsole wrapper + .desktop) and exit')
    parser.add_argument('--remove-launcher', action='store_true',
                        help='Remove the installed Frame "+" menu entry and exit')
    args = parser.parse_args()
    os.umask(0o077)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    if os.geteuid() == 0:
        raise PrivateError('Never run the private-home controller as root')
    if args.launch:
        send_request(args.launch[0], args.launch[1:])
        return 0
    if args.watchdog is not None:
        return watchdog_main(args.watchdog)
    if args.install_launcher:
        install_menu_entry()
        return 0
    if args.remove_launcher:
        remove_menu_entry()
        return 0
    resolution = parse_resolution(args.resolution)
    if not sys.stdin.isatty():
        raise PrivateError('Start this controller from a real terminal (Konsole).')
    script = Path(__file__).resolve()
    mkdir_private(STATE)
    # Exclusive flock, held until mount is removed.
    with LOCK.open('a+b') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise PrivateError('A private-home controller is already running')
        disable_old_gui_autostart()
        recover_previous_mount()
        setup_dependencies()
        app = TerminalSession(script)
        ready = False

        def handle_signal(signum, _frame):
            if not ready:
                raise KeyboardInterrupt
            info(f'Received {signal.Signals(signum).name}; locking...')
            app.stop_event.set()

        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT):
            signal.signal(signum, handle_signal)
        try:
            app.start_watchdog()
            info('Unlocking private home. Ctrl+C (or kill -TERM) will lock it.')
            app.fuse = unlock_home()
            atomic_write(FUSE_PID, json.dumps({'pid': app.fuse.pid,
                                               'start': start_ticks(app.fuse.pid)}), 0o600)
            if resolution:
                # Runs in the host Plasma session (needs its D-Bus), never inside the
                # app sandbox; a failure here must not abort the unlock.
                try:
                    apply_resolution(*resolution)
                except Exception as e:
                    info('Resolution adjustment failed: ' + str(e))
            app.start_server()
            count = make_launchers(script)
            info(f'Unlocked; {count} (Private) launchers are available in your KDE menu.')
            info('Leave this terminal running. Press Ctrl+C to lock and quit.')
            ready = True
            while not app.stop_event.wait(.5):
                app.refresh_pids()
        finally:
            # SIGTERM or further Ctrl+C during teardown must not interrupt
            # the unmount and leave a false impression that the vault locked.
            for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT):
                signal.signal(signum, signal.SIG_IGN)
            info('Closing private apps, removing launchers and unmounting...')
            app.cleanup()
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print('\nInterrupted.', file=sys.stderr)
        sys.exit(130)
    except (OSError, PrivateError, RuntimeError, subprocess.SubprocessError) as e:
        print(f'{APP}: {e}', file=sys.stderr, flush=True)
        sys.exit(1)
