# Trinitron

WebGL/Vulkan CRT viewer that applies real-time retro filters to a video feed.
Two front-ends share the same shaders:

- **`index.html`** — browser version (getUserMedia + WebGL).
- **`native.py`** — Linux desktop app (PyQt5 + wgpu/Vulkan + V4L2), which can
  capture either a webcam/capture card or a game window.

## Running

```bash
python3 native.py
```

The viewer shows whatever frame rate the capture card sends — there is no FPS
cap. Duplicate frames (which capture cards emit to pad their output rate when
the source is slower) are dropped so the counter reflects the real source
cadence.

## Capturing a game window (Wayland)

Besides a webcam, `native.py` can capture a **window or monitor** as its video
source — handy for previewing a CRT/retro filter over a game (e.g. run the game
in a 480p window and watch it through the `crt` filter).

On the setup screen pick **Source → "Game window (screen capture)"**, press
Start, and the desktop's *share* dialog appears to choose the window/screen.

How it works (Wayland only): it uses the
`org.freedesktop.portal.ScreenCast` portal to negotiate a PipeWire stream, then
a GStreamer pipeline (`pipewiresrc ! videoconvert ! appsink`) pulls BGRA frames
into the same render path as the webcam. Requirements (all standard on a modern
desktop):

- A Wayland session with `xdg-desktop-portal` + a ScreenCast backend
  (e.g. `xdg-desktop-portal-gnome` on GNOME).
- The GStreamer PipeWire plugin: `gstreamer1.0-pipewire` (provides
  `pipewiresrc`).
- PyGObject (`python3-gi`) with GStreamer (`gir1.2-gstreamer-1.0`).

> X11 sessions aren't wired up (the portal path is Wayland-only here). If the
> portal is cancelled or unavailable, the viewer shows the error and stays on
> "Waiting for signal".

## Desktop launcher (Linux)

A desktop shortcut was created so the app shows up in the application menu /
launcher. It's a standard freedesktop `.desktop` entry placed in the per-user
applications directory:

```bash
~/.local/share/applications/Trinitron.desktop
```

Contents:

```ini
[Desktop Entry]
Type=Application
Name=Trinitron
Comment=CRT webcam viewer with retro filters
Exec=/usr/bin/python3 /home/ewerton/projects/trinitron/native.py
Path=/home/ewerton/projects/trinitron
Icon=camera-web
Terminal=false
Categories=AudioVideo;Video;
StartupWMClass=Trinitron
```

Key fields:

- **`Exec`** — absolute path to the interpreter and script (launchers don't
  inherit your shell's `cwd` or `PATH`, so absolute paths are required).
- **`Path`** — working directory, so the app finds the `shaders/` folder
  relative to itself.
- **`Terminal=false`** — runs without a terminal window.
- **`StartupWMClass=Trinitron`** — matches the Qt window title so the launcher
  groups the running window under this icon in the dock/taskbar.

After creating or editing the file, refresh the menu cache (most desktops pick
it up automatically; if not):

```bash
update-desktop-database ~/.local/share/applications
```
