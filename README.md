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
  otherwise Guix's). Disable with `--no-share-clipboard`.
- **Lock** — `Ctrl+C` in the Konsole window stops the session, unmounts via
  `fusermount3 -u` (with retries), and refuses to pretend success if anything
  still holds the mount. `SIGTERM` is treated the same way.

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
scanner requires. Add `--no-share-clipboard` at install time to bake clipboard
sharing off into the wrapper. Undo anytime with
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
--share-clipboard             Bidirectional text clipboard sync (default)
--no-share-clipboard          Keep private clipboard data off the host
--reset-empty-vault           Archive and reinitialize a vault that contains
                              nothing but its config (refuses if it has data)
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
| `~/.local/share/frame-private-desktop/bin/` | in-vault startup shim (`private-plasma-start`, kwin wrappers, clipboard bridge) |
| `~/.local/bin/gocryptfs-native` | Guix `gocryptfs` ELF run via `/lib/ld-linux-aarch64.so.1` |

## Troubleshooting

- **"WAYLAND_DISPLAY isn't set" from the `+` menu** — fixed: the `+` menu
  launches Konsole from gamescope's outer session, which has no Wayland
  display; the script now auto-discovers the running nested Plasma compositor
  socket under the user runtime. (Still fails from a plain SSH shell, as it
  must.)
- **Menu window flashes open and closes** — fixed: the script now prepends
  `~/.local/bin` to `PATH` itself (menu-launched Konsole inherits Plasma's
  minimal `PATH`, which hides `guix-env`), and the launcher runs
  `konsole --hold`, so the window stays open on any error for you to read.
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
