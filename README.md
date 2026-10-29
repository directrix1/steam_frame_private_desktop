# Steam Frame Private Desktop

Unlock a **gocryptfs-encrypted private home** on your **Steam Frame** and
launch a **nested KDE Plasma desktop** inside it. The private desktop runs in
its own Bubblewrap namespace with the decrypted vault mounted over `HOME`, so
apps installed into the private session (Flatpak `--user`, Guix profile,
Konsole, Firefox, …) keep their data inside the encrypted vault. When the
session ends, the vault unmounts and nothing plaintext is left on disk.

## How it works

- **Unlock** — enter your passphrase in the terminal; `gocryptfs` mounts the
  vault at `~/.local/share/frame-private-desktop/mount/` using the SteamOS
  ARM64 loader plus a Guix-built `gocryptfs` binary (`-fg`, never the
  relocatable Guix pack wrapper).
- **Nested KDE** — a `bwrap` namespace is built (fresh `/run`, `--unshare-pid`,
  private D-Bus via `dbus-run-session`) with the vault bind-mounted over
  `HOME` and the rootless Guix store at `/gnu/store`. A `kwin_wayland_wrapper`
  shim sets the nested display size (**1920×1080 by default**), then
  `startplasma-wayland` brings up a full Plasma session composited onto your
  host desktop's Wayland socket — it appears on the Frame's virtual screen.
- **Real home** — your normal home is visible inside the namespace as
  `/host-home`; the private `~/.local/share/flatpak` is checked so it can never
  accidentally symlink to it. Host **system** Flatpaks (`/var/lib/flatpak`) are
  shared read-only; your private `flatpak --user` install (with its own Flathub
  remote) lives encrypted in the vault.
- **Clipboard** — bidirectional *text* clipboard sync between host and private
  desktop using `wl-clipboard` ≥ 2.3 (SteamOS's copy is used when new enough,
  otherwise Guix's). Default is context-aware: **on** when launched from a
  terminal inside the desktop, **off** when launched from the `+` menu (where
  the outer session is Steam, not a desktop worth trading clipboard with).
  Override either way with `--share-clipboard` / `--no-share-clipboard`.
- **Lock** — `Ctrl+C` in the Konsole window stops the session, unmounts via
  `fusermount3 -u` (with retries), and refuses to pretend success if anything
  still holds the mount. `SIGTERM` is treated the same way.

## Where it opens and how the controllers work

| Launched from | Result |
|---|---|
| Frame `+` menu | Its **own window in the VR environment** |
| KDE application menu, inside the Plasma desktop | **Fullscreen** over the desktop until you quit |

**VR-controller clicks.** The Frame has no real mouse: gamescope (Steam's
compositor, which runs the headset session) turns each controller trigger
pull into a *touchscreen tap*, then decides per window what to deliver
(see `OpenVRBackend.cpp`, `wlserver.cpp` and `Utils/Process.cpp` in
[gamescope](https://github.com/ValveSoftware/gamescope)):

- **Native Wayland window, no Steam app ID** (any `+`-menu app) - raw touch
  is passed through. A nested KWin then sees finger touches, so clicks don't
  register or release properly.
- **Window with a Steam app ID** (from an `app-steam-app<ID>-*.scope` cgroup
  or a Steam `reaper` parent) - real mouse clicks, but Steam treats the window
  as a game and shows it in theater mode.
- **X11 window on gamescope's Xwayland** (`DISPLAY=:0`) - gamescope enables
  touch-pointer emulation for non-Steam X11 windows, so taps become ordinary
  mouse clicks, with a visible cursor, in a normal window.

Steam's own desktop (`/usr/bin/steamos-nested-desktop`, the QAM's Desktop
button) uses the third route: its KWin runs with the **X11 windowed backend**
on `:0`. A `+`-menu launch of the private desktop does exactly the same:
`DISPLAY=:0` (socket bound into the sandbox; gamescope's Xwayland needs no
auth) and no `WAYLAND_DISPLAY`, so KWin picks its X11 backend. The parent
Wayland socket is still bound for the clipboard bridge.

`--attach=desktop` nests inside the Plasma desktop instead; it is started
automatically if it isn't running (same command as the QAM button, logged to
`~/.local/share/frame-private-desktop/desktop-session.log`).

**Fullscreen from the KDE menu** uses a temporary KWin window rule in
`~/.config/kwinrulesrc` matching the nested window's title ("KDE Wayland
Compositor"; its window class is empty), with Force fullscreen. It is added
after unlock, applied via the desktop KWin's `reconfigure`, and removed when
the session ends; any rules you already have are preserved. `--windowed`
opens a normal window instead.

## Requirements

- A **Steam Frame** running SteamOS (aarch64), launched from inside its nested
  KDE desktop (Konsole), not a plain SSH shell.
- **[guix-rootless](https://github.com/directrix1/guix-rootless)** — the
  companion rootless GNU Guix installer. It provides `~/.local/bin/guix-env`
  and the store at `~/.local/share/guix`; this tool uses it to install
  `gocryptfs` and (if needed) `wl-clipboard`. Install that project first.
- Host packages already present on SteamOS: `bwrap`, `kwin_wayland`,
  `kwin_wayland_wrapper`, `startplasma-wayland`, `dbus-run-session`,
  `fusermount3` (setuid), Konsole.
- `sudo` is asked for *once per boot*, only to create the
  `/run/setuid-programs/fusermount3` alias that Guix's FUSE mount expects.

## Quick install (one-liner)

Already have [guix-rootless](https://github.com/directrix1/guix-rootless)
installed? Run this in a Konsole terminal on the Frame to clone the repo and
install the `+` menu entry in one go:

```bash
git clone https://github.com/directrix1/steam_frame_private_desktop.git ~/steam_frame_private_desktop && ~/steam_frame_private_desktop/frame_private_desktop.py --install-launcher
```

## Install

```bash
git clone https://github.com/directrix1/steam_frame_private_desktop.git
cd steam_frame_private_desktop
./frame_private_desktop.py --install-launcher
```

`--install-launcher` writes two files:

| File | Purpose |
| --- | --- |
| `~/.local/bin/frame-private-desktop-launch` | Opens the session in a Konsole window (needed for the passphrase prompt) |
| `~/.local/share/applications/frame-private-desktop.desktop` | The `Private Desktop` entry for the Frame `+` menu / "Add Library Shortcut" |

The `Exec` line is a single space-free absolute path, as the SteamOS non-Steam
scanner requires. Clipboard sharing then follows the context-aware default at
launch time (on in-desktop, off from the menu); pass `--share-clipboard` or
`--no-share-clipboard` alongside `--install-launcher` to bake an explicit
choice into the wrapper instead. Undo anytime with
`./frame_private_desktop.py --remove-launcher` (`--make-launcher` is accepted
as a legacy alias for `--install-launcher`).

## Use

1. On the Frame, press **+** and choose **Private Desktop** (or run the script
   in Konsole yourself). First run initializes a new vault — **save the
   printed master key somewhere off the headset**.
2. Enter your passphrase; the nested Plasma desktop appears on the Frame's
   virtual screen at **1920×1080**. Install apps inside it as normal (Discover
   with the private Flathub, `guix-env`/`guix-pkg` from a private Konsole, …).
3. Leave the Konsole window open. Press `Ctrl+C` (or `kill -TERM` the script)
   to end the session and lock the vault.

## Display size

The nested desktop defaults to `1920x1080`:

```bash
./frame_private_desktop.py                    # 1920x1080
./frame_private_desktop.py --width 2560 --height 1440
```

Any size from 640×480 up to 8192×8192 is accepted. The size is applied to the
nested KWin output; the Frame scales that image onto your virtual screen, so
1080p gives crisp desktop/UI rendering in the headset.

## Command line

```
--width W / --height H        Nested display size (default 1920x1080)
--install-launcher            Install the Frame '+' menu entry and exit
--remove-launcher             Remove it and exit
--share-clipboard             Force clipboard sync on (desktop default: on)
--no-share-clipboard          Off (the '+'-menu default; the outer session there
                              is Steam, so the bridge is usually pointless)
--reset-empty-vault           Archive and reinitialize a vault that contains
                              nothing but its config (refuses if it has data)
--attach auto|steam|desktop   '+'-menu target: own VR window (auto/steam, the
                              default) or inside the Plasma desktop (desktop)
--windowed                    From the KDE menu: normal window, not fullscreen
```

## Security model (read this)

- **Encryption at rest only.** While unlocked, any process running as your
  login user can read the plaintext mount at
  `~/.local/share/frame-private-desktop/mount/`. The bwrap namespace is
  *convenience scoping*, not a hardened sandbox — it shares the network, GPU,
  and your host Wayland compositor. **Never suspend or power off the headset
  while the session is unlocked.**
- Clipboard sync is text-only but genuinely two-way: passwords copied inside
  the private desktop land in the host clipboard history. Use
  `--no-share-clipboard` if that matters.
- `/host-home` is deliberately bind-mounted so you can move files across;
  remove that bind in `build_private_kde()` if you want stricter separation.
- Passphrases are entered interactively by `gocryptfs` on the terminal — they
  never pass through argv, environment, or logs. Core dumps are disabled
  (`RLIMIT_CORE=0`) and `SIGQUIT` (which core-dumps) is neutralized.
- The vault is never overwritten: a non-empty cipher directory without
  `gocryptfs.conf` aborts with an error instead of re-initializing.
- `/usr/bin/fusermount3` is verified setuid, and the `/run/setuid-programs`
  alias is created only against that exact trusted binary — an unexpected
  existing path is refused, never replaced.

## State and files

| Path | Contents |
| --- | --- |
| `~/.local/share/frame-private-desktop/encrypted/` | gocryptfs vault (ciphertext) |
| `~/.local/share/frame-private-desktop/mount/` | plaintext mount (only while unlocked) |
| `~/.local/share/frame-private-desktop/session.lock` | single-instance `flock` |
| `~/.local/share/frame-private-desktop/launch.log` | every `+`-menu launch: full output of the controller *and* the nested Plasma session, with exit codes |
| `~/.local/share/frame-private-desktop/bin/` | in-vault startup shim (`private-plasma-start`, kwin wrappers, clipboard bridge) |
| `~/.local/bin/gocryptfs-native` | Guix `gocryptfs` ELF run via `/lib/ld-linux-aarch64.so.1` |

## Troubleshooting

- **No `sudo` password prompt from the `+` menu** — expected: the FUSE helper
  alias under `/run/setuid-programs` is created once per boot, so only the
  first launch after each reboot asks for your login password.
- **`rc=120` in `launch.log`** — that code comes from SteamOS'
  `kwin_wayland_wrapper` after it stops respawning a dying KWin. The
  controller now labels what ended the session (exit code, fatal signal, or
  repeated KWin deaths with timestamps via the supervised compositor shim).
- **Vault unlocks but no desktop appears (or anything from a `+`-menu launch)**
  — read `~/.local/share/frame-private-desktop/launch.log`: every menu launch
  appends the controller's *and* the nested Plasma session's complete output
  plus the exit code.
- **"WAYLAND_DISPLAY isn't set" from the `+` menu** - expected: the `+` menu
  launches Konsole from gamescope's session, which has only `DISPLAY=:0`. The
  script runs the private KWin on that X11 display (see *Where it opens*).
  (Still fails from a plain SSH shell, as it must.)
- **Menu window flashes open and closes** — fixed: the script now prepends
  `~/.local/bin` to `PATH` itself (menu-launched Konsole inherits Plasma's
  minimal `PATH`, which hides `guix-env`). The window closes by itself when
  the private desktop quits cleanly, and on a failed launch it stays open
  (waiting for Enter) so the error is readable; everything is always in
  `launch.log` too.
- **"guix-env was not found"** — install
  [guix-rootless](https://github.com/directrix1/guix-rootless) first.
- **"Launch from a terminal"** — the passphrase needs a real TTY; use Konsole,
  not a bare SSH session (or use the `+` menu entry, which opens Konsole).
- **"A previous decrypted mount is already active"** — a crashed run left the
  mount; check nothing is using it, then `fusermount3 -u ~/.local/share/frame-private-desktop/mount`.
- **"STILL MOUNTED" on exit** — close apps/terminals inside the private desktop
  and exit again. Do not leave the headset unattended until it locks.
- **Clipboard errors** — needs `wl-clipboard` ≥ 2.3; the tool installs the Guix
  copy automatically, or start with `--no-share-clipboard`.
- **Menu entry invisible on the Frame** — `kbuildsycoca6` runs for you; also
  try copying `~/.local/share/applications/frame-private-desktop.desktop` into
  `~/Desktop`, another directory the SteamOS scanner reads.

## License

Same terms as the original script in this repository; add a license file if you
intend to redistribute.
