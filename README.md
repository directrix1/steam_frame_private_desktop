# Frame Private Desktop

A one-file controller that gives your **Steam Frame** (or any SteamOS KDE
Wayland session) a **gocryptfs-encrypted private home** plus a set of
**"(Private)" application launchers** in the KDE menu. While unlocked, your
normal KDE/Plasma session is untouched; apps started through the (Private)
launchers run in their own Bubblewrap namespaces where `HOME` is the decrypted
vault instead of your real home. Lock it again with `Ctrl+C` and every private
app is stopped, the launchers disappear, and the vault unmounts.

## How it works

- **Unlock** — you enter a passphrase in the terminal; `gocryptfs` mounts the
  encrypted vault (created under `~/.local/share/frame-private-desktop/`) at a
  private mount point using the SteamOS ARM64 loader plus a Guix-built
  `gocryptfs` binary.
- **Launchers** — the controller scans system, Guix-profile, and private-home
  `.desktop` entries and regenerates matching `frame-private-app-*.desktop`
  entries in `~/.local/share/applications` whose names get a ` (Private)`
  suffix. Launching one sends an authenticated request (peer-UID-checked Unix
  socket) to the still-running controller.
- **Sandbox** — each private app runs under `bwrap` with the decrypted vault
  bind-mounted over `HOME`. The Guix store at `~/.local/share/guix` is mounted
  at `/gnu/store`; host Wayland, D-Bus, and PipeWire/Pulse sockets are passed
  through so apps appear on your desktop normally. The host user's real home is
  *not* visible inside the sandbox.
- **Lock** — `Ctrl+C` / `SIGTERM` stops recorded process groups (verified by
  PID start-time to avoid recycled-PID mistakes), removes the generated
  launchers, refreshes the KDE menu cache, and unmounts via the host
  `fusermount3` after verifying the mount really is gocryptfs.
- **Crash safety** — a watchdog running the *same script file* survives a
  `SIGKILL` of the controller and performs the same teardown.

## Requirements

- A **Steam Frame** running SteamOS (aarch64) inside its nested KDE Plasma
  Wayland session, or a comparable SteamOS KDE session.
- **[guix-rootless](https://github.com/directrix1/guix-rootless)** — the
  companion rootless GNU Guix installer. It provides `~/.local/bin/guix-env`
  and the rootless store at `~/.local/share/guix`. This script uses it to
  install and run `gocryptfs` and `bubblewrap` without touching the host
  system. Install that project first.
- **Konsole** (for the terminal controller) and a normal interactive login.
- `sudo` is asked for *once per boot*, only to create the
  `/run/setuid-programs/fusermount3` alias that Guix's FUSE mount expects.

## Install

```bash
git clone https://github.com/directrix1/steam_frame_private_desktop.git
cd steam_frame_private_desktop
./frame_private_desktop.py --install-launcher
```

`--install-launcher` writes two files:

| File | Purpose |
| --- | --- |
| `~/.local/bin/frame-private-desktop-launch` | Opens the controller in a Konsole window |
| `~/.local/share/applications/frame-private-desktop.desktop` | The `Private Desktop` entry, discoverable by the Frame `+` menu / "Add Library Shortcut" |

The `Exec` line is a single space-free absolute path, as the SteamOS non-Steam
scanner requires. Undo anytime with
`./frame_private_desktop.py --remove-launcher`.

## Use

1. On the Frame, press **+** and choose **Private Desktop** (or run the script
   in Konsole yourself). The terminal asks for your vault password — on first
   run it creates a new vault and prints a **recovery key: save it somewhere
   safe, it is never written to disk unencrypted**.
2. After unlock, the KDE menu fills with `… (Private)` launchers. Apps started
   from them see only the encrypted private home.
3. The desktop is resized to **1920×1080** on unlock by default (see
   [Resolution](#resolution)).
4. Leave the Konsole window open. Press `Ctrl+C` (or `kill -TERM`) to close
   private apps, remove the `(Private)` launchers, and lock the vault. The
   terminal prints a warning and refuses to pretend success if unmount fails.

## Resolution

The Frame shows the desktop on a virtual screen; the effective menu/desktop
resolution comes from the nested Plasma session. On unlock the controller runs
KScreen (`kscreen-doctor`) to switch the session output to `1920x1080`:

```bash
./frame_private_desktop.py                    # default: 1920x1080
./frame_private_desktop.py --resolution 2560x1440
./frame_private_desktop.py --resolution keep  # leave the desktop alone
```

`FRAME_PRIVATE_RESOLUTION` does the same thing via the environment. This is
best-effort: if KScreen reports no usable mode (some nested backends only
offer the window size), the current resolution is kept and the message is
logged. You can always resize manually in Konsole with `kscreen-doctor -o` to
inspect and `kscreen-doctor output.<name>.mode.<WxH>` to try a mode.

## Security model (read this)

- **Encryption at rest only.** While unlocked, your normal login user can read
  the plaintext mount, and private apps share host services (GPU, audio,
  Wayland, D-Bus, network). Single-instance apps may forward work into an
  already-running host process. This keeps other users and stolen devices out,
  not your own login session.
- The vault at `~/.local/share/frame-private-desktop/encrypted/` is **never**
  reset or overwritten; a vault directory without `gocryptfs.conf` is refused,
  never initialized over.
- Passphrases travel only through an inherited anonymous pipe
  (`-passfile /proc/self/fd/N`) — never argv, environment, or disk. Core dumps
  are disabled (`RLIMIT_CORE=0`) and `SIGQUIT` is trapped so it cannot dump
  core during teardown.
- Everything the controller owns lives under mode `0700`/`0600` paths and only
  files with the `frame-private-app-*` prefix (or the exact
  `frame-private-desktop.desktop` name) are ever modified or removed.

## State and files

| Path | Contents |
| --- | --- |
| `~/.local/share/frame-private-desktop/encrypted/` | gocryptfs vault (ciphertext) |
| `~/.local/share/frame-private-desktop/mount/` | plaintext mount (only while unlocked) |
| `~/.local/share/frame-private-desktop/terminal-app-pids.json` | live private-app process groups |
| `~/.local/share/frame-private-desktop/tray.sock` | controller IPC socket (peer-UID checked) |
| `~/.local/share/frame-private-desktop/tray.lock` | single-instance `flock` |
| `~/.local/bin/gocryptfs-native` | Guix `gocryptfs` ELF copy run via `/lib/ld-linux-aarch64.so.1` |

## Troubleshooting

- **"guix-env not found under ~/.local/bin"** — install
  [guix-rootless](https://github.com/directrix1/guix-rootless) first; this
  script shells out to it for `gocryptfs`/`bubblewrap`.
- **"FUSE helper alias missing"** — run the script from an interactive
  terminal once so `sudo` can create `/run/setuid-programs/fusermount3`.
- **"already mounted by an unknown controller"** — an older/other controller
  instance holds the vault; let it exit (it cleans up) instead of unmounting
  by hand.
- **Menu entry invisible on the Frame** — KDE caches menus (`kbuildsycoca6` is
  run for you); also try copying
  `~/.local/share/applications/frame-private-desktop.desktop` to `~/Desktop`,
  another directory the SteamOS scanner reads.
- **"STILL MOUNTED" on lock** — something still uses the mount (often a file
  manager); close it and `Ctrl+C` again. Never suspend believing it is locked
  when the warning appears.

## License

Same terms as the original script in this repository; add a license file if you
intend to redistribute.
