#!/usr/bin/env python3
"""
native.py — Python CRT Webcam Viewer
Replicates index.html using PyQt5 + wgpu + OpenCV + sounddevice.
Captures via V4L2 (YUYV, uncompressed) instead of MJPEG via getUserMedia,
or a game window/monitor via the Wayland ScreenCast portal + PipeWire.
Renders via wgpu-py (Vulkan on Linux).
"""

import sys
import os
import re
import time
import glob
import faulthandler
import threading
import multiprocessing as _mp
from multiprocessing import shared_memory
import numpy as np

import cv2
import av

try:
    import sounddevice as sd
    AUDIO_AVAILABLE = True
except Exception:
    AUDIO_AVAILABLE = False

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout,
    QComboBox, QPushButton, QLabel, QSlider, QStackedWidget, QSizePolicy,
)
from PyQt5.QtCore import Qt, QThread, pyqtSignal, QMutex, QMutexLocker, QTimer, QEvent, QPoint
from PyQt5.QtGui import QPalette, QColor

import wgpu
# wgpu >= 0.17 uses rendercanvas; older versions use wgpu.gui.qt
try:
    from wgpu.gui.qt import WgpuWidget
except ImportError:
    from rendercanvas.qt import QRenderWidget as WgpuWidget

# ─── Constants ────────────────────────────────────────────────────────────────

CANVAS_W, CANVAS_H = 2562, 1440
ORIG_W, ORIG_H = 1920, 1080
DOWNSCALE_W, DOWNSCALE_H = 854, 480

SHADER_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'shaders')
WGSL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'shaders', 'wgsl')

FILTER_NAMES = ['original', 'downscale', 'crt', 'crtgrainy', 'blurrycrt',
                'blurrygrainycrt', 'sharpen', 'grainy']

FILTER_DISPLAY = {
    'original':       'Original',
    'downscale':      'Downscale',
    'crt':            'CRT',
    'crtgrainy':      'CRT Grainy',
    'blurrycrt':      'Blurry CRT',
    'blurrygrainycrt':'Blurry Grainy CRT',
    'sharpen':        'Sharpen',
    'grainy':         'Grainy',
}

# CSS filter equivalents: (brightness, saturation)
FILTER_POST = {
    'crt':            (1.4, 1.3),
    'crtgrainy':      (1.4, 1.3),
    'blurrycrt':      (1.4, 1.3),
    'blurrygrainycrt':(1.4, 1.3),
    'sharpen':        (1.0, 1.2),
    'grainy':         (1.0, 1.2),
    'original':       (1.0, 1.0),
    'downscale':      (1.0, 1.0),
}

FILTERS_WITH_TIME = {'crtgrainy', 'blurrygrainycrt', 'grainy'}

CAPTURE_FORMATS = {
    'YUYV 4:2:2': 'yuyv422',
    'NV12 4:2:0': 'nv12',
    'BGR 8-8-8':  'bgr24',
    'MJPEG':      'mjpeg',
}

ASPECT_RATIOS = {
    '16:9': (1920, 1080),
    '8:7':  (260,  240),
    '4:3':  (320,  240)
}

# ─── Screen-capture crash log ─────────────────────────────────────────────────
# Diagnostics for the Wayland portal/PipeWire path, which crashed on the picker
# dialog. faulthandler dumps the native + per-thread Python stack here on a
# segfault (SIGSEGV/SIGABRT) — the only way to see a non-Python crash.
SCREEN_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          'screencapture.log')
_screen_log_fh = None

def _screenlog(msg: str) -> None:
    global _screen_log_fh
    try:
        if _screen_log_fh is None:
            _screen_log_fh = open(SCREEN_LOG, 'a', buffering=1)
            faulthandler.enable(file=_screen_log_fh, all_threads=True)
        _screen_log_fh.write(f'{time.strftime("%H:%M:%S")} {msg}\n')
        print(msg, flush=True)
    except OSError:
        pass

# ─── WGSL shader loading ──────────────────────────────────────────────────────

def load_wgsl_shader(frag_name: str) -> str:
    """Concatenate vertex.wgsl + <frag_name>.wgsl into a single shader module."""
    with open(os.path.join(WGSL_DIR, 'vertex.wgsl')) as f:
        vert = f.read()
    with open(os.path.join(WGSL_DIR, f'{frag_name}.wgsl')) as f:
        frag = f.read()
    return vert + '\n' + frag

# ─── Device enumeration ───────────────────────────────────────────────────────

def enumerate_cameras():
    """Return list of (label, device_path) via sysfs — never opens the device."""
    cameras = []
    for dev_path in sorted(glob.glob('/dev/video*')):
        m = re.search(r'/dev/video(\d+)', dev_path)
        if not m:
            continue
        idx = int(m.group(1))
        sysfs_name = f'/sys/class/video4linux/video{idx}/name'
        try:
            with open(sysfs_name) as f:
                dev_name = f.read().strip()
        except OSError:
            continue  # no sysfs entry → not a real device node
        cameras.append((f'{dev_name} ({dev_path})', dev_path))
    return cameras


def enumerate_mics():
    """Return list of (label, device_index) for audio input devices."""
    if not AUDIO_AVAILABLE:
        return []
    try:
        return [
            (d['name'], i)
            for i, d in enumerate(sd.query_devices())
            if d['max_input_channels'] > 0
        ]
    except Exception:
        return []

# ─── Capture worker (subprocess) ─────────────────────────────────────────────

def _v4l2_worker(camera_path: str, shm_name: str, frame_w: int,
                 options: dict, stop_event: _mp.Event,
                 use_dedup: bool = False) -> None:
    """Runs in a subprocess.  Opens v4l2, writes BGR24 frames to shared memory,
    signals each new frame by writing one byte to frame_w pipe.

    Running in a subprocess means the kernel can forcibly clean up all mmap'd
    v4l2 buffers and file descriptors when we SIGTERM/SIGKILL it — no segfault.
    """
    import signal as _sig

    # Set the stop event on SIGTERM so the decode loop exits cleanly before
    # container.close() is called — avoids ioctl(VIDIOC_QBUF) errors on
    # format switches.
    _sig.signal(_sig.SIGTERM, lambda *_: stop_event.set())

    shm = shared_memory.SharedMemory(name=shm_name)
    buf = np.ndarray((ORIG_H * ORIG_W * 3,), dtype=np.uint8, buffer=shm.buf)
    container = None
    try:
        container = av.open(camera_path, format='v4l2', options=options)
        stream = container.streams.video[0]
        print(f'[worker] {camera_path} {stream.width}x{stream.height} '
              f'@ {float(stream.average_rate):.0f}fps', flush=True)
        is_mjpeg = options.get('input_format') == 'mjpeg'

        if use_dedup:
            # Detect duplicate frames (capture cards repeat frames to maintain
            # output fps when the game's fps is lower) so the FPS counter and
            # render reflect the source's real cadence.
            # MJPEG threshold=6 to tolerate JPEG re-encode noise; raw formats use 1.
            dup_threshold = 6 if is_mjpeg else 1

            # 256 fixed random positions spread across the full frame.
            # Scattered fancy-indexing avoids strided access over the full 6 MB
            # array and eliminates int16 allocations per frame.
            _rng = np.random.default_rng(42)
            _rows = _rng.integers(0, ORIG_H, 256)
            _cols = _rng.integers(0, ORIG_W, 256)
            prev_sample = np.empty((256, 3), dtype=np.uint8)
            prev_sample_set = False

            def _new_frame(img: np.ndarray) -> bool:
                nonlocal prev_sample_set
                sample = img[_rows, _cols]   # 256 scattered pixels, no copy
                if prev_sample_set:
                    # Skip dedup when the frame is a solid/flat color (black
                    # screen, fade, loading screen) — all samples would match
                    # prev_sample and every frame would be dropped, causing a
                    # freeze. If internal spread of the sample is tiny the
                    # scene is flat and we let the frame through unconditionally.
                    flat = int(sample.max()) - int(sample.min()) < 15
                    if not flat and int(cv2.absdiff(sample, prev_sample).max()) < dup_threshold:
                        return False
                np.copyto(prev_sample, sample)
                prev_sample_set = True
                return True
        else:
            def _new_frame(img: np.ndarray) -> bool:
                return True

        if is_mjpeg:
            # Decode raw JPEG packets via libjpeg-turbo (same path as the
            # browser) — avoids libavcodec's YUV color-range mishandling.
            # Capture cards encode MJPEG with limited-range YUV (16-235);
            # cv2.LUT expands to full range (0-255) at SIMD speed.
            _lut = np.clip(
                (np.arange(256, dtype=np.float32) - 16.0) * (255.0 / 219.0),
                0, 255,
            ).astype(np.uint8)

            # 1080p JPEG decode costs ~16 ms on one core → caps a single thread
            # at ~60 fps and makes it fall behind. cv2.imdecode releases the GIL,
            # so a small thread pool decodes frames in parallel across cores.
            # Demux (cheap) stays on this thread; results are collected in
            # submission order, so dedup/LUT/shm stay serial and unchanged.
            # In-flight depth = thread count → adds ~depth frames of latency, so
            # keep it small. Override with TRINITRON_DECODE_THREADS.
            from concurrent.futures import ThreadPoolExecutor
            from collections import deque
            n_threads = int(os.environ.get('TRINITRON_DECODE_THREADS', 0)) or \
                min(4, os.cpu_count() or 4)

            def _decode_job(jpeg: bytes):
                return cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)

            packets = container.demux(stream)

            def _submit_next(pool, futures):
                # Copy the compressed bytes out of the packet before the next
                # demux frees them — the decode runs later on another thread.
                for pkt in packets:
                    if pkt.size == 0:
                        continue
                    futures.append(pool.submit(_decode_job, bytes(pkt)))
                    return True
                return False

            pool = ThreadPoolExecutor(max_workers=n_threads)
            futures: deque = deque()
            try:
                for _ in range(n_threads):       # prime the pipeline
                    if not _submit_next(pool, futures):
                        break
                while not stop_event.is_set() and futures:
                    img = futures.popleft().result()
                    _submit_next(pool, futures)  # refill to keep threads busy
                    if img is None:
                        continue
                    if not _new_frame(img):      # dedup before LUT — skip dupes
                        continue
                    cv2.LUT(img, _lut, dst=img)  # in-place: no 6 MB alloc per frame
                    h, w = img.shape[:2]
                    buf[:h * w * 3] = img.reshape(-1)
                    os.write(frame_w, b'\x01')
            finally:
                pool.shutdown(wait=False, cancel_futures=True)
        else:
            for frame in container.decode(stream):
                if stop_event.is_set():
                    break
                img = frame.to_ndarray(format='bgr24')
                if not _new_frame(img):
                    continue
                h, w = img.shape[:2]
                buf[:h * w * 3] = img.reshape(-1)
                os.write(frame_w, b'\x01')
    except Exception as e:
        if not stop_event.is_set():
            print(f'[worker] {e}', flush=True)
    finally:
        if container:
            try:
                container.close()
            except Exception:
                pass
        shm.close()
        try:
            os.close(frame_w)
        except OSError:
            pass


# ─── Capture thread ───────────────────────────────────────────────────────────

class CaptureThread(QThread):
    frame_ready = pyqtSignal(object)

    def __init__(self, camera_path: str):
        super().__init__()
        self.camera_path = camera_path
        self._running = True
        self._proc: _mp.Process | None = None
        self._stop_event: _mp.Event | None = None
        self._needs_downscale = False
        # Pre-allocated BGRA output buffers — reused every frame, no per-frame malloc
        self._bgra_ds   = np.empty((DOWNSCALE_H, DOWNSCALE_W, 4), dtype=np.uint8)
        self._bgra_orig = np.empty((ORIG_H,      ORIG_W,      4), dtype=np.uint8)
        self._framerate = '60'
        default_fmt = next(iter(CAPTURE_FORMATS.values()))
        if default_fmt == 'mjpeg':
            self._options = {
                'video_size':   '1920x1080',
                'framerate':    self._framerate,
                'input_format': 'mjpeg',
            }
        else:
            self._options = {
                'video_size':   '1920x1080',
                'framerate':    self._framerate,
                'pixel_format': default_fmt,
            }

    def set_needs_downscale(self, val: bool):
        self._needs_downscale = val

    def set_pixel_format(self, fmt: str):
        base = {k: v for k, v in self._options.items()
                if k not in ('pixel_format', 'input_format')}
        if fmt == 'mjpeg':
            # MJPEG is a compressed format; V4L2 requires input_format, not pixel_format.
            self._options = {**base, 'input_format': 'mjpeg'}
        else:
            self._options = {**base, 'pixel_format': fmt}
        if self._stop_event is not None:
            self._stop_event.set()

    def run(self):
        import select as _sel

        while self._running:
            shm = shared_memory.SharedMemory(create=True,
                                             size=ORIG_H * ORIG_W * 3)
            frame_r, frame_w = os.pipe()
            self._stop_event = _mp.Event()

            proc = _mp.Process(
                target=_v4l2_worker,
                args=(self.camera_path, shm.name, frame_w, self._options,
                      self._stop_event, True),
                daemon=True,
            )
            proc.start()
            os.close(frame_w)       # parent only reads
            self._proc = proc

            frame_buf = np.ndarray((ORIG_H, ORIG_W, 3),
                                   dtype=np.uint8, buffer=shm.buf)
            last_activity = time.time()
            first = True

            while self._running:
                ready = _sel.select([frame_r], [], [], 1.0)[0]
                if ready:
                    data = os.read(frame_r, 256)    # drain pipe
                    if not data:                    # worker closed write end (clean exit)
                        break
                    # Resize + BGR→BGRA here in the capture thread so the
                    # render thread only needs to call write_texture + GPU.
                    if self._needs_downscale:
                        small = cv2.resize(frame_buf,
                                           (DOWNSCALE_W, DOWNSCALE_H),
                                           interpolation=cv2.INTER_NEAREST)
                        cv2.cvtColor(small, cv2.COLOR_BGR2BGRA,
                                     dst=self._bgra_ds)
                        out = self._bgra_ds.copy()   # ~1.6 MB
                    else:
                        cv2.cvtColor(frame_buf, cv2.COLOR_BGR2BGRA,
                                     dst=self._bgra_orig)
                        out = self._bgra_orig.copy() # ~8 MB
                    if first:
                        print(f'CaptureThread: first frame {out.shape}',
                              flush=True)
                        first = False
                    self.frame_ready.emit(out)
                    last_activity = time.time()
                elif not proc.is_alive():
                    if self._running and not self._stop_event.is_set():
                        print(f'CaptureThread: worker exited '
                              f'(code {proc.exitcode}), reopening…', flush=True)
                    break
                elif time.time() - last_activity > 5.0:
                    print('CaptureThread: worker timeout, killing…', flush=True)
                    break

            # Force-release: SIGTERM → wait → SIGKILL if needed.
            # Kernel frees all mmap'd v4l2 buffers and fds — no segfault.
            proc.terminate()
            proc.join(2.0)
            if proc.is_alive():
                proc.kill()
                proc.join(1.0)

            os.close(frame_r)
            shm.close()
            shm.unlink()
            self._proc = None

            if self._running:
                time.sleep(0.5)

    def stop(self):
        self._running = False
        if self._stop_event is not None:
            self._stop_event.set()
        if self._proc and self._proc.is_alive():
            self._proc.terminate()
        self.wait()

# ─── Screen capture (Wayland / PipeWire via xdg-desktop-portal) ───────────────

class ScreenCaptureThread(QThread):
    """Captures a window or monitor on Wayland through the xdg-desktop-portal
    ScreenCast interface + PipeWire, emitting BGRA frames exactly like
    CaptureThread so the renderer is source-agnostic.

    The portal pops a system 'share' dialog where the user picks which window
    (or screen) to capture — set the game to a small/480p window and pick it to
    preview a CRT filter over it.
    """

    frame_ready = pyqtSignal(object)
    error = pyqtSignal(str)
    size_changed = pyqtSignal(int, int)   # captured content (w, h) → letterbox aspect

    def __init__(self):
        super().__init__()
        self._needs_downscale = False
        self._loop = None         # currently-running GLib loop (handshake or pipeline)
        self._pipeline = None
        self._bus = None          # keep the D-Bus connection alive → keeps the session
        self._last_size = (0, 0)
        self._crop = None         # [x0,y0,x1,y1] union bbox of non-black content
        self._raw_size = (0, 0)   # stream size; changing it resets the crop
        self._frame_n = 0
        self._autocrop = os.environ.get('TRINITRON_NO_AUTOCROP', '') == ''

    def set_needs_downscale(self, val: bool):
        self._needs_downscale = val

    def _update_crop(self, bgr: np.ndarray) -> None:
        """Grow the content bbox to include all non-black pixels. A low-res
        windowed game renders into the top-left of a larger capture buffer with
        black padding; the union over frames converges to the fixed render
        area (the padding is structurally black, so it never expands into it)."""
        g = bgr.max(axis=2)
        rows = np.where(g.max(axis=1) > 16)[0]
        cols = np.where(g.max(axis=0) > 16)[0]
        if len(rows) == 0 or len(cols) == 0:
            return
        x0, y0 = int(cols[0]), int(rows[0])
        x1, y1 = int(cols[-1]) + 1, int(rows[-1]) + 1
        if self._crop is None:
            self._crop = [x0, y0, x1, y1]
        else:
            c = self._crop
            c[0], c[1] = min(c[0], x0), min(c[1], y0)
            c[2], c[3] = max(c[2], x1), max(c[3], y1)

    # API-compatible no-op: screen capture has no V4L2 pixel-format knob.
    def set_pixel_format(self, fmt: str):
        pass

    def run(self):
        _screenlog(f'==== screen capture start  log={SCREEN_LOG} ====')
        try:
            import gi
            gi.require_version('Gst', '1.0')
            from gi.repository import Gst, GLib, Gio
        except Exception as e:
            _screenlog(f'import failed: {e}')
            self.error.emit(f'GStreamer/PyGObject unavailable: {e}')
            return

        Gst.init(None)
        self._Gst = Gst
        _screenlog('Gst.init OK')

        # Dispatch portal D-Bus signals on this thread's own GMainContext so we
        # never fight Qt's glib integration on the main thread.
        ctx = GLib.MainContext.new()
        ctx.push_thread_default()
        try:
            fd, node_id = self._portal_handshake(GLib, Gio, ctx)

            # videorate caps the stream to 60 fps BEFORE the (CPU-bound)
            # videoconvert — the ScreenCast portal can push frames at the
            # monitor's refresh (e.g. 164 Hz), and converting every one pegs a
            # core. drop-only never duplicates, so slower sources pass through.
            _screenlog(f'parse_launch pipewiresrc fd={fd} path={node_id}')
            self._pipeline = Gst.parse_launch(
                f'pipewiresrc fd={fd} path={node_id} keepalive-time=1000 '
                f'resend-last=true ! videorate drop-only=true ! '
                f'video/x-raw,framerate=60/1 ! '
                f'videoconvert ! video/x-raw,format=BGRA ! '
                f'appsink name=sink emit-signals=true max-buffers=2 drop=true sync=false'
            )
            sink = self._pipeline.get_by_name('sink')
            sink.connect('new-sample', self._on_sample)
            self._pipeline.set_state(Gst.State.PLAYING)
            _screenlog('pipeline PLAYING')

            self._loop = GLib.MainLoop.new(ctx, False)
            self._loop.run()
            _screenlog('loop exited')
        except Exception as e:
            import traceback
            _screenlog(f'EXCEPTION: {e}\n{traceback.format_exc()}')
            self.error.emit(f'Screen capture failed: {e}')
        finally:
            if self._pipeline is not None:
                self._pipeline.set_state(Gst.State.NULL)
            ctx.pop_thread_default()

    def _on_sample(self, sink):
        Gst = self._Gst
        sample = sink.emit('pull-sample')
        if sample is None:
            return Gst.FlowReturn.OK
        buf = sample.get_buffer()
        s = sample.get_caps().get_structure(0)
        w = s.get_value('width')
        h = s.get_value('height')
        if (w, h) != self._raw_size:    # stream resolution changed → re-detect
            self._raw_size = (w, h)
            self._crop = None
            self._frame_n = 0
        ok, info = buf.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.FlowReturn.OK
        try:
            stride = len(info.data) // h          # respects PipeWire row padding
            frame = np.frombuffer(info.data, np.uint8).reshape(h, stride)
            bgra = frame[:, :w * 4].reshape(h, w, 4)

            # Trim black padding so a low-res windowed game gets centered +
            # height-filled instead of stuck top-left. Re-detect every 15 frames
            # (the union only grows, converging to the fixed render area).
            if self._autocrop:
                if self._frame_n % 15 == 0:
                    self._update_crop(bgra[:, :, :3])
                self._frame_n += 1
                if self._crop:
                    x0, y0, x1, y1 = self._crop
                    bgra = bgra[y0:y1, x0:x1]

            ch, cw = bgra.shape[:2]
            if (cw, ch) != self._last_size:
                self._last_size = (cw, ch)
                self.size_changed.emit(cw, ch)   # drive the letterbox aspect

            if self._needs_downscale:
                small = cv2.resize(bgra[:, :, :3], (DOWNSCALE_W, DOWNSCALE_H),
                                   interpolation=cv2.INTER_NEAREST)
                out = cv2.cvtColor(small, cv2.COLOR_BGR2BGRA)
            else:
                out = bgra.copy()                  # contiguous copy before unmap
            self.frame_ready.emit(out)
        finally:
            buf.unmap(info)
        return Gst.FlowReturn.OK

    # ── xdg-desktop-portal ScreenCast handshake ──────────────────────────────
    def _portal_handshake(self, GLib, Gio, ctx):
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self._bus = bus
        sender = bus.get_unique_name()[1:].replace('.', '_')
        self._tok = 0

        PORTAL = 'org.freedesktop.portal.Desktop'
        OBJ = '/org/freedesktop/portal/desktop'
        SC = 'org.freedesktop.portal.ScreenCast'

        def token():
            self._tok += 1
            return f'trinitron{self._tok}'

        def do_request(method, build_body):
            """Call a portal method that returns a Request handle, block until
            its Response signal fires, and return the results dict."""
            htok = token()
            req_path = f'{OBJ}/request/{sender}/{htok}'
            holder = {}
            inner = GLib.MainLoop.new(ctx, False)
            self._loop = inner

            def on_response(conn, snd, path, ifc, sig, params):
                code, results = params.unpack()
                holder['code'] = code
                holder['results'] = results
                inner.quit()

            sub = bus.signal_subscribe(
                PORTAL, 'org.freedesktop.portal.Request', 'Response',
                req_path, None, Gio.DBusSignalFlags.NONE, on_response)
            try:
                bus.call_sync(PORTAL, OBJ, SC, method, build_body(htok),
                              GLib.VariantType('(o)'), Gio.DBusCallFlags.NONE,
                              -1, None)
                inner.run()
            finally:
                bus.signal_unsubscribe(sub)
            if holder.get('code', 2) != 0:
                raise RuntimeError(f'{method} cancelled (code {holder.get("code")})')
            return holder['results']

        # 1. CreateSession → results carry the session handle.
        _screenlog('CreateSession…')
        res = do_request('CreateSession', lambda htok: GLib.Variant('(a{sv})', [{
            'handle_token':         GLib.Variant('s', htok),
            'session_handle_token': GLib.Variant('s', token()),
        }]))
        session = res['session_handle']
        _screenlog(f'session={session}')

        # 2. SelectSources: types 3 = monitor|window, single source, cursor hidden.
        _screenlog('SelectSources…')
        do_request('SelectSources', lambda htok: GLib.Variant('(oa{sv})', [session, {
            'handle_token': GLib.Variant('s', htok),
            'types':        GLib.Variant('u', 3),
            'multiple':     GLib.Variant('b', False),
            'cursor_mode':  GLib.Variant('u', 1),
        }]))

        # 3. Start: shows the GNOME share dialog; results carry the PipeWire node.
        _screenlog('Start (opening picker dialog)…')
        res = do_request('Start', lambda htok: GLib.Variant('(osa{sv})', [session, '', {
            'handle_token': GLib.Variant('s', htok),
        }]))
        streams = res.get('streams') or []
        _screenlog(f'Start returned streams={streams}')
        if not streams:
            raise RuntimeError('no window/screen was selected')
        node_id = streams[0][0]

        # 4. OpenPipeWireRemote: direct call returning a Unix fd (not a Request).
        _screenlog('OpenPipeWireRemote…')
        var, fd_list = bus.call_with_unix_fd_list_sync(
            PORTAL, OBJ, SC, 'OpenPipeWireRemote',
            GLib.Variant('(oa{sv})', [session, {}]),
            GLib.VariantType('(h)'), Gio.DBusCallFlags.NONE, -1, None, None)
        fd = fd_list.get(var.unpack()[0])
        _screenlog(f'pipewire fd={fd} node={node_id}')
        return fd, node_id

    def stop(self):
        if self._loop is not None:
            self._loop.quit()
        self.wait()

class AudioPassthrough:
    def __init__(self, input_device: int):
        self._volume = 1.0
        self._lock = threading.Lock()
        self._stream = None
        self._input_device = input_device

    def start(self):
        if not AUDIO_AVAILABLE:
            return
        try:
            in_info = sd.query_devices(self._input_device)
            ch = min(int(in_info['max_input_channels']), 2)
            # Try samplerates in order of preference
            for sr in [int(in_info['default_samplerate']), 48000, 44100, 16000]:
                try:
                    stream = sd.Stream(
                        device=(self._input_device, None),
                        samplerate=sr,
                        channels=ch,
                        blocksize=2048,
                        callback=self._callback,
                        latency='high',
                    )
                    stream.start()
                    self._stream = stream
                    print(f'Audio: {in_info["name"]} @ {sr}Hz {ch}ch')
                    return
                except Exception:
                    continue
            print('Audio error: no working samplerate found')
        except Exception as e:
            print(f'Audio error: {e}')

    def _callback(self, indata, outdata, frames, time_info, status):
        with self._lock:
            vol = self._volume
        ch_in = indata.shape[1]
        ch_out = outdata.shape[1]
        if ch_in == ch_out:
            outdata[:] = indata * vol
        elif ch_in == 1 and ch_out == 2:
            outdata[:, 0] = indata[:, 0] * vol
            outdata[:, 1] = indata[:, 0] * vol
        else:
            outdata[:] = indata[:, :ch_out] * vol

    def set_volume(self, vol: float):
        with self._lock:
            self._volume = vol

    def stop(self):
        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None

# ─── Wgpu Renderer ────────────────────────────────────────────────────────────

class WgpuRenderer(WgpuWidget):
    fps_updated = pyqtSignal(str)
    initialized = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.set_update_mode('ondemand', max_fps=60)
        self._mutex = QMutex()
        self._frame = None
        self._filter = 'original'
        self._aspect = (1920, 1080)

        self._device = None
        self._ctx = None
        self._pipelines = {}           # filter_name → GPURenderPipeline
        self._post_pipeline = None
        self._sampler = None
        self._webcam_tex = None
        self._webcam_tex_view = None
        self._webcam_tex_size = (0, 0)
        self._inter_tex = None
        self._inter_tex_view = None
        self._inter_tex_size = (0, 0)
        self._filter_uniform_buf = None
        self._post_uniform_buf = None
        self._filter_bgl = None
        self._post_bgl = None
        self._filter_bind_group = None
        self._post_bind_group = None
        self._vertex_buf = None

        self._start_time = time.time()
        self._frame_count = 0
        self._fps_timer = time.time()
        self._initialized = False
        self._swapchain_fmt = 'bgra8unorm'
        self._dbg_screen = False   # log letterbox numbers for screen-capture debug
        self._dbg_n = 0

    # ── Public API ──────────────────────────────────────────────────────────

    def set_filter(self, name: str):
        self._filter = name

    def set_aspect(self, ratio: tuple):
        self._aspect = ratio

    def on_frame(self, frame: np.ndarray):
        with QMutexLocker(self._mutex):
            self._frame = frame
        if self._initialized:
            self.request_draw()
        # ── FPS counter (counts real incoming frames, not render calls) ───────
        self._frame_count += 1
        elapsed = time.time() - self._fps_timer
        if elapsed >= 1.0:
            fps = self._frame_count / elapsed
            self._frame_count = 0
            self._fps_timer = time.time()
            self.fps_updated.emit(f'{fps:.0f} FPS')

    # ── Lifecycle ───────────────────────────────────────────────────────────

    def _rc_get_present_info(self, present_methods):
        """Use a native Vulkan screen-present surface for low-latency output."""
        if 'screen' in present_methods:
            surface_ids = self._get_surface_ids()
            if surface_ids:
                self.setAttribute(Qt.WA_PaintOnScreen, True)
                return {'method': 'screen', **surface_ids}
        return super()._rc_get_present_info(present_methods)

    def showEvent(self, event):
        super().showEvent(event)
        if not self._initialized:
            QTimer.singleShot(0, self._init_wgpu)

    def _init_wgpu(self):
        try:
            adapter = wgpu.gpu.request_adapter_sync(
                canvas=self, power_preference='high-performance'
            )
            self._device = adapter.request_device_sync()
            device = self._device
            print(f'wgpu adapter: {adapter.info}')

            self._ctx = self.get_context('wgpu')
            for fmt in ('bgra8unorm', 'rgba8unorm', 'bgra8unorm-srgb', 'rgba8unorm-srgb'):
                try:
                    self._ctx.configure(device=device, format=fmt)
                    self._swapchain_fmt = fmt
                    print(f'Swapchain format: {fmt}')
                    break
                except Exception:
                    continue

            self._sampler = device.create_sampler(
                min_filter='linear', mag_filter='linear',
                address_mode_u='clamp-to-edge', address_mode_v='clamp-to-edge',
            )

            self._filter_uniform_buf = device.create_buffer(
                size=16, usage=wgpu.BufferUsage.UNIFORM | wgpu.BufferUsage.COPY_DST
            )
            self._post_uniform_buf = device.create_buffer(
                size=16, usage=wgpu.BufferUsage.UNIFORM | wgpu.BufferUsage.COPY_DST
            )

            # Vertex buffer: 4 verts × (pos.xy + uv.xy) × f32 = 64 bytes
            # Standard WGSL UVs: (0,0) = top-left. cv2.flip removed from worker.
            verts = np.array([
                -1.0,  1.0,  0.0, 0.0,   # top-left
                 1.0,  1.0,  1.0, 0.0,   # top-right
                -1.0, -1.0,  0.0, 1.0,   # bottom-left
                 1.0, -1.0,  1.0, 1.0,   # bottom-right
            ], dtype=np.float32)
            self._vertex_buf = device.create_buffer_with_data(
                data=verts.tobytes(),
                usage=wgpu.BufferUsage.VERTEX,
            )

            self._filter_bgl = device.create_bind_group_layout(entries=[
                {'binding': 0, 'visibility': wgpu.ShaderStage.FRAGMENT,
                 'texture': {'sample_type': 'float', 'view_dimension': '2d'}},
                {'binding': 1, 'visibility': wgpu.ShaderStage.FRAGMENT,
                 'sampler': {'type': 'filtering'}},
                {'binding': 2, 'visibility': wgpu.ShaderStage.FRAGMENT,
                 'buffer': {'type': 'uniform'}},
            ])
            self._post_bgl = device.create_bind_group_layout(entries=[
                {'binding': 0, 'visibility': wgpu.ShaderStage.FRAGMENT,
                 'texture': {'sample_type': 'float', 'view_dimension': '2d'}},
                {'binding': 1, 'visibility': wgpu.ShaderStage.FRAGMENT,
                 'sampler': {'type': 'filtering'}},
                {'binding': 2,
                 'visibility': wgpu.ShaderStage.VERTEX | wgpu.ShaderStage.FRAGMENT,
                 'buffer': {'type': 'uniform'}},
            ])

            for name in FILTER_NAMES:
                self._pipelines[name] = self._make_filter_pipeline(name)
                print(f'Compiled wgpu pipeline: {name}')
            self._post_pipeline = self._make_post_pipeline()
            print('Compiled wgpu pipeline: post')

            self._initialized = True
            self.request_draw(self._draw)
            print('WgpuRenderer init OK')
            self.initialized.emit()
        except Exception:
            import traceback
            print('WgpuRenderer init FAILED:')
            traceback.print_exc()

    def _make_filter_pipeline(self, name: str):
        device = self._device
        module = device.create_shader_module(code=load_wgsl_shader(name))
        layout = device.create_pipeline_layout(bind_group_layouts=[self._filter_bgl])
        return device.create_render_pipeline(
            layout=layout,
            vertex={
                'module': module,
                'entry_point': 'vs_main',
                'buffers': [{
                    'array_stride': 16,
                    'step_mode': 'vertex',
                    'attributes': [
                        {'format': 'float32x2', 'offset': 0, 'shader_location': 0},
                        {'format': 'float32x2', 'offset': 8, 'shader_location': 1},
                    ],
                }],
            },
            fragment={
                'module': module,
                'entry_point': 'fs_main',
                'targets': [{'format': 'rgba8unorm'}],
            },
            primitive={'topology': 'triangle-strip'},
        )

    def _make_post_pipeline(self):
        device = self._device
        with open(os.path.join(WGSL_DIR, 'post.wgsl')) as f:
            code = f.read()
        module = device.create_shader_module(code=code)
        layout = device.create_pipeline_layout(bind_group_layouts=[self._post_bgl])
        return device.create_render_pipeline(
            layout=layout,
            vertex={
                'module': module,
                'entry_point': 'vs_main',
                'buffers': [{
                    'array_stride': 16,
                    'step_mode': 'vertex',
                    'attributes': [
                        {'format': 'float32x2', 'offset': 0, 'shader_location': 0},
                        {'format': 'float32x2', 'offset': 8, 'shader_location': 1},
                    ],
                }],
            },
            fragment={
                'module': module,
                'entry_point': 'fs_main',
                'targets': [{'format': self._swapchain_fmt}],
            },
            primitive={'topology': 'triangle-strip'},
        )

    def _upload_webcam_frame(self, frame: np.ndarray):
        """frame is already BGRA — converted in CaptureThread."""
        device = self._device
        img_h, img_w = frame.shape[:2]

        if self._webcam_tex_size != (img_w, img_h):
            self._webcam_tex = device.create_texture(
                size=(img_w, img_h, 1),
                format='bgra8unorm',
                usage=wgpu.TextureUsage.TEXTURE_BINDING | wgpu.TextureUsage.COPY_DST,
            )
            self._webcam_tex_view = self._webcam_tex.create_view()
            self._webcam_tex_size = (img_w, img_h)
            self._filter_bind_group = None  # invalidate

        device.queue.write_texture(
            {'texture': self._webcam_tex, 'mip_level': 0, 'origin': (0, 0, 0)},
            frame,  # already BGRA, passed directly — no extra copy
            {'bytes_per_row': img_w * 4, 'rows_per_image': img_h},
            (img_w, img_h, 1),
        )

    def _ensure_inter_texture(self, w: int, h: int):
        if self._inter_tex_size == (w, h):
            return
        device = self._device
        self._inter_tex = device.create_texture(
            size=(w, h, 1),
            format='rgba8unorm',
            usage=wgpu.TextureUsage.RENDER_ATTACHMENT | wgpu.TextureUsage.TEXTURE_BINDING,
        )
        self._inter_tex_view = self._inter_tex.create_view()
        self._inter_tex_size = (w, h)
        self._post_bind_group = None  # invalidate

    def _get_filter_bind_group(self):
        if self._filter_bind_group is None:
            self._filter_bind_group = self._device.create_bind_group(
                layout=self._filter_bgl,
                entries=[
                    {'binding': 0, 'resource': self._webcam_tex_view},
                    {'binding': 1, 'resource': self._sampler},
                    {'binding': 2, 'resource': {
                        'buffer': self._filter_uniform_buf, 'offset': 0, 'size': 16}},
                ],
            )
        return self._filter_bind_group

    def _get_post_bind_group(self):
        if self._post_bind_group is None:
            self._post_bind_group = self._device.create_bind_group(
                layout=self._post_bgl,
                entries=[
                    {'binding': 0, 'resource': self._inter_tex_view},
                    {'binding': 1, 'resource': self._sampler},
                    {'binding': 2, 'resource': {
                        'buffer': self._post_uniform_buf, 'offset': 0, 'size': 16}},
                ],
            )
        return self._post_bind_group

    def _letterbox(self, sw: int, sh: int):
        aw, ah = self._aspect
        target_ar = aw / ah
        screen_ar = sw / sh
        if screen_ar > target_ar:
            vh = sh
            vw = int(sh * target_ar)
        else:
            vw = sw
            vh = int(sw / target_ar)
        vx = (sw - vw) // 2
        vy = (sh - vh) // 2
        return vx, vy, vw, vh

    def _draw(self):
        if not self._initialized:
            return

        device = self._device
        t_now = time.time()

        with QMutexLocker(self._mutex):
            frame = self._frame

        if frame is None:
            current_tex = self._ctx.get_current_texture()
            view = current_tex.create_view()
            cmd = device.create_command_encoder()
            rp = cmd.begin_render_pass(color_attachments=[{
                'view': view, 'resolve_target': None,
                'clear_value': (0, 0, 0, 1), 'load_op': 'clear', 'store_op': 'store',
            }])
            rp.end()
            device.queue.submit([cmd.finish()])
            return

        filter_name = self._filter
        is_original = (filter_name == 'original')

        # frame is already BGRA at the correct resolution (resized in CaptureThread)
        if is_original:
            tex_w, tex_h = float(ORIG_W), float(ORIG_H)
            canvas_w, canvas_h = ORIG_W, ORIG_H
        else:
            tex_w, tex_h = 640.0, 360.0   # intentional: matches web app
            canvas_w, canvas_h = CANVAS_W, CANVAS_H

        self._upload_webcam_frame(frame)
        self._ensure_inter_texture(canvas_w, canvas_h)

        t_elapsed = t_now - self._start_time
        filter_data = np.array([tex_w, tex_h, t_elapsed, 0.0], dtype=np.float32)
        device.queue.write_buffer(self._filter_uniform_buf, 0, filter_data.tobytes())

        brightness, saturation = FILTER_POST.get(filter_name, (1.0, 1.0))
        post_data = np.array([brightness, saturation, 0.0, 0.0], dtype=np.float32)
        device.queue.write_buffer(self._post_uniform_buf, 0, post_data.tobytes())

        cmd = device.create_command_encoder()

        # ── Pass 1: filter → inter_tex ────────────────────────────────────
        rp1 = cmd.begin_render_pass(color_attachments=[{
            'view': self._inter_tex_view,
            'resolve_target': None,
            'clear_value': (0, 0, 0, 1),
            'load_op': 'clear',
            'store_op': 'store',
        }])
        rp1.set_pipeline(self._pipelines[filter_name])
        rp1.set_bind_group(0, self._get_filter_bind_group())
        rp1.set_vertex_buffer(0, self._vertex_buf)
        rp1.set_viewport(0, 0, canvas_w, canvas_h, 0, 1)
        rp1.draw(4)
        rp1.end()

        # ── Pass 2: post → swapchain with letterbox ───────────────────────
        # Letterbox in the swapchain's OWN pixel space (get_current_texture),
        # not self.width()/height(): the native Vulkan surface size can differ
        # from the Qt logical widget size, which would offset the viewport and
        # leave black margins.
        current_tex = self._ctx.get_current_texture()
        sw, sh = current_tex.size[0], current_tex.size[1]
        if sw <= 0 or sh <= 0:
            device.queue.submit([cmd.finish()])
            return
        vx, vy, vw, vh = self._letterbox(sw, sh)

        if self._dbg_screen and self._dbg_n % 120 == 0:
            fh, fw = frame.shape[:2]
            _screenlog(f'[draw] frame={fw}x{fh} aspect={self._aspect} '
                       f'swapchain={sw}x{sh} widget={self.width()}x{self.height()} '
                       f'letterbox=({vx},{vy},{vw},{vh}) filter={filter_name}')
        self._dbg_n += 1

        sc_view = current_tex.create_view()
        rp2 = cmd.begin_render_pass(color_attachments=[{
            'view': sc_view,
            'resolve_target': None,
            'clear_value': (0, 0, 0, 1),
            'load_op': 'clear',
            'store_op': 'store',
        }])
        rp2.set_pipeline(self._post_pipeline)
        rp2.set_bind_group(0, self._get_post_bind_group())
        rp2.set_vertex_buffer(0, self._vertex_buf)
        rp2.set_viewport(vx, vy, vw, vh, 0, 1)
        rp2.draw(4)
        rp2.end()

        device.queue.submit([cmd.finish()])
        if filter_name in FILTERS_WITH_TIME:
            self.request_draw()  # re-schedule only for time-animated shaders

    def cleanup(self):
        pass  # wgpu resources are released via GC

# ─── Sidebar panel ────────────────────────────────────────────────────────────

class SidebarPanel(QWidget):
    filter_changed = pyqtSignal(str)
    aspect_changed = pyqtSignal(tuple)
    volume_changed = pyqtSignal(float)
    format_changed = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(220)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet('background: rgba(0,0,0,217); color: white;')

        layout = QVBoxLayout(self)
        layout.setContentsMargins(15, 70, 15, 15)
        layout.setSpacing(10)

        layout.addWidget(QLabel('Console'))
        self._console_combo = QComboBox()
        for name in ASPECT_RATIOS:
            self._console_combo.addItem(name)
        self._console_combo.currentTextChanged.connect(
            lambda t: self.aspect_changed.emit(ASPECT_RATIOS[t]))
        layout.addWidget(self._console_combo)

        layout.addWidget(QLabel('Filter'))
        self._filter_combo = QComboBox()
        for name in FILTER_NAMES:
            self._filter_combo.addItem(FILTER_DISPLAY[name], name)
        self._filter_combo.currentIndexChanged.connect(
            lambda i: self.filter_changed.emit(self._filter_combo.itemData(i)))
        layout.addWidget(self._filter_combo)

        self._fmt_label = QLabel('Capture Format')
        layout.addWidget(self._fmt_label)
        self._fmt_combo = QComboBox()
        for label, fmt in CAPTURE_FORMATS.items():
            self._fmt_combo.addItem(label, fmt)
        self._fmt_combo.currentIndexChanged.connect(
            lambda i: self.format_changed.emit(self._fmt_combo.itemData(i)))
        layout.addWidget(self._fmt_combo)

        self._vol_label = QLabel('Volume')
        layout.addWidget(self._vol_label)
        self._vol_slider = QSlider(Qt.Horizontal)
        self._vol_slider.setRange(0, 100)
        self._vol_slider.setValue(80)
        self._vol_slider.valueChanged.connect(
            lambda v: self.volume_changed.emit(v / 100.0))
        layout.addWidget(self._vol_slider)

        layout.addStretch()

    def set_screen_mode(self):
        """Screen capture has no V4L2 format and no audio — hide those controls."""
        for w in (self._fmt_label, self._fmt_combo,
                  self._vol_label, self._vol_slider):
            w.setVisible(False)

# ─── Overlay window ───────────────────────────────────────────────────────────

class OverlayWindow(QWidget):
    """Frameless transparent top-level window that floats over the wgpu surface.
    Holds all HUD widgets so they are composited by the window manager above
    the Vulkan swapchain (which owns its own native X11 window).
    """

    def __init__(self, viewport: QWidget, show_ui_cb):
        super().__init__(
            None,
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool,
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setMouseTracking(True)
        self._viewport = viewport
        self._show_ui_cb = show_ui_cb
        QApplication.instance().applicationStateChanged.connect(
            self._on_app_state_changed)

    def _on_app_state_changed(self, state):
        if state == Qt.ApplicationActive:
            if self._viewport.isVisible():
                self.sync()
                self.show()
        else:
            self.hide()

    def sync(self):
        if not self._viewport.isVisible():
            return
        tl = self._viewport.mapToGlobal(QPoint(0, 0))
        self.setGeometry(tl.x(), tl.y(),
                         self._viewport.width(), self._viewport.height())

    def eventFilter(self, obj, event):
        t = event.type()
        if t in (QEvent.Move, QEvent.Resize, QEvent.WindowStateChange):
            self.sync()
        elif t == QEvent.Hide:
            self.hide()
        elif t == QEvent.Show:
            self.sync()
            self.show()
        return False

    def mouseMoveEvent(self, event):
        self._show_ui_cb()
        super().mouseMoveEvent(event)


# ─── Viewer page ──────────────────────────────────────────────────────────────

class ViewerPage(QWidget):
    def __init__(self, source: str, device: str, mic_idx: int, parent=None):
        super().__init__(parent)
        self.setStyleSheet('background: black;')
        self._source = source

        self._gl = WgpuRenderer(self)
        self._gl.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._gl.fps_updated.connect(self._on_fps)
        self._gl.initialized.connect(self._on_renderer_init)

        # Transparent overlay window for all HUD widgets
        self._overlay = OverlayWindow(self, self._show_ui)

        # FPS label (top-left, hidden by default)
        self._fps_label = QLabel('-- FPS', self._overlay)
        self._fps_label.setStyleSheet(
            'color: white; background: rgba(0,0,0,128); '
            'padding: 4px; font-size: 13px; border-radius: 3px;'
        )
        self._fps_label.adjustSize()
        self._fps_label.move(10, 10)
        self._fps_label.setVisible(False)

        # "Waiting for signal" label — visible until first FPS update
        self._waiting_label = QLabel('Waiting for signal\u2026', self._overlay)
        self._waiting_label.setStyleSheet(
            'color: white; font-size: 18px; background: transparent;'
        )
        self._waiting_label.setAlignment(Qt.AlignCenter)
        self._gl.fps_updated.connect(lambda _: self._waiting_label.setVisible(False))

        # Sidebar (hidden by default)
        self._sidebar = SidebarPanel(self._overlay)
        self._sidebar.setVisible(False)
        self._sidebar.filter_changed.connect(self._gl.set_filter)
        self._sidebar.filter_changed.connect(
            lambda f: self._capture.set_needs_downscale(f != 'original'))
        self._sidebar.aspect_changed.connect(self._gl.set_aspect)
        self._sidebar.volume_changed.connect(self._on_volume)
        self._sidebar.format_changed.connect(self._on_format)

        _btn_style = (
            'QPushButton { background: rgba(0,0,0,179); color: white; '
            'border: 1px solid rgba(255,255,255,77); border-radius: 4px; '
            'font-size: 18px; }'
            'QPushButton:hover { background: rgba(40,40,40,230); }'
        )

        # Sidebar toggle button (top-right)
        self._toggle_btn = QPushButton('☰', self._overlay)
        self._toggle_btn.setFixedSize(44, 44)
        self._toggle_btn.setStyleSheet(_btn_style)
        self._toggle_btn.clicked.connect(self._on_toggle)

        # Fullscreen button (next to sidebar toggle)
        self._fs_btn = QPushButton('⛶', self._overlay)
        self._fs_btn.setFixedSize(44, 44)
        self._fs_btn.setStyleSheet(_btn_style)
        self._fs_btn.clicked.connect(self._on_fullscreen)

        # Capture thread — webcam (V4L2) or game window (Wayland screen capture)
        if source == 'screen':
            self._capture = ScreenCaptureThread()
            self._capture.error.connect(self._on_capture_error)
            # Fit the captured content: letterbox to its own aspect (centered,
            # height-filling) instead of the fixed Console aspect. Bound method
            # (QObject receiver) → runs on the GUI thread via a queued connection.
            self._capture.size_changed.connect(self._on_screen_size)
            self._gl._dbg_screen = True
            # Screen capture has no pixel format / mic / volume — hide those.
            self._sidebar.set_screen_mode()
        else:
            self._capture = CaptureThread(device)
        self._capture.frame_ready.connect(self._gl.on_frame)
        # Start capture only AFTER wgpu/Vulkan finishes initializing. Starting
        # the screen-capture thread (Gst.init + Wayland portal) concurrently with
        # wgpu surface creation races on the (non-thread-safe) Wayland display
        # and segfaults. Deferring serializes the two GPU/Wayland inits.
        self._capture_started = False

        # Audio
        self._audio = None
        if mic_idx >= 0 and AUDIO_AVAILABLE:
            self._audio = AudioPassthrough(mic_idx)
            self._audio.set_volume(0.8)
            self._audio.start()

        # Auto-hide UI after 3 s of mouse inactivity
        self._ui_visible = True
        self._fps_received = False
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.setInterval(3000)
        self._hide_timer.timeout.connect(self._hide_ui)
        self._hide_timer.start()

        self._layout_widgets()

    def _on_renderer_init(self):
        self._overlay.sync()
        self._overlay.show()
        if not self._capture_started:
            self._capture_started = True
            self._capture.start()

    def _on_fps(self, text: str):
        self._fps_label.setText(text)
        self._fps_received = True
        self._fps_label.setVisible(self._ui_visible)
        self._fps_label.adjustSize()

    def _on_toggle(self):
        self._sidebar.setVisible(not self._sidebar.isVisible())

    def _on_fullscreen(self):
        win = self.window()
        if win.isFullScreen():
            win.showMaximized()
        else:
            win.showFullScreen()

    def _on_format(self, fmt: str):
        self._waiting_label.setVisible(True)
        self._capture.set_pixel_format(fmt)

    def _on_capture_error(self, msg: str):
        print(msg, flush=True)
        self._waiting_label.setText(msg)
        self._waiting_label.setVisible(True)

    def _on_screen_size(self, w: int, h: int):
        _screenlog(f'[size] captured frame {w}x{h} → set_aspect')
        self._gl.set_aspect((w, h))

    def _on_volume(self, vol: float):
        if self._audio:
            self._audio.set_volume(vol)

    def _layout_widgets(self):
        w, h = self.width(), self.height()
        self._gl.setGeometry(0, 0, w, h)
        self._waiting_label.setGeometry(0, 0, w, h)
        self._fs_btn.move(w - 59, 15)
        self._toggle_btn.move(w - 59 - 49, 15)
        self._sidebar.move(w - 220, 0)
        self._sidebar.resize(220, h)
        self._overlay.sync()

    def _hide_ui(self):
        self._ui_visible = False
        self._toggle_btn.setVisible(False)
        self._fs_btn.setVisible(False)
        self._sidebar.setVisible(False)
        self._fps_label.setVisible(False)
        self._overlay.setCursor(Qt.BlankCursor)

    def _show_ui(self):
        if not self._ui_visible:
            self._ui_visible = True
            self._toggle_btn.setVisible(True)
            self._fs_btn.setVisible(True)
            if self._fps_received:
                self._fps_label.setVisible(True)
            self._overlay.setCursor(Qt.ArrowCursor)
        self._hide_timer.start()

    def showEvent(self, event):
        super().showEvent(event)
        self.window().installEventFilter(self._overlay)
        QTimer.singleShot(0, self._overlay.sync)

    def hideEvent(self, event):
        super().hideEvent(event)
        self._overlay.hide()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._layout_widgets()

    def cleanup(self):
        self._capture.stop()
        if self._audio:
            self._audio.stop()
        self._gl.cleanup()
        self._overlay.close()

# ─── Setup page ───────────────────────────────────────────────────────────────

class SetupPage(QWidget):
    start_requested = pyqtSignal(str, str, int)  # source ('camera'|'screen'), device, mic_idx

    def __init__(self, parent=None):
        super().__init__(parent)

        outer = QVBoxLayout(self)
        outer.setAlignment(Qt.AlignCenter)

        card = QWidget(self)
        card.setFixedWidth(340)
        card.setStyleSheet(
            'QWidget { background: rgba(0,0,0,204); border-radius: 8px; }'
            'QLabel  { color: white; font-size: 13px; background: transparent; }'
        )
        inner = QVBoxLayout(card)
        inner.setContentsMargins(20, 20, 20, 20)
        inner.setSpacing(8)

        title = QLabel('Trinitron')
        title.setStyleSheet('font-size: 22px; font-weight: bold; color: white; '
                            'background: transparent;')
        title.setAlignment(Qt.AlignCenter)
        inner.addWidget(title)

        inner.addWidget(QLabel('Source:'))
        self._src_combo = QComboBox()
        self._src_combo.addItem('Camera', 'camera')
        self._src_combo.addItem('Game window (screen capture)', 'screen')
        self._src_combo.currentIndexChanged.connect(self._on_source_changed)
        inner.addWidget(self._src_combo)

        self._cam_label = QLabel('Camera:')
        inner.addWidget(self._cam_label)
        self._cam_combo = QComboBox()
        cameras = enumerate_cameras()
        if cameras:
            for label, path in cameras:
                self._cam_combo.addItem(label, path)
        else:
            self._cam_combo.addItem('No camera found', '')
        inner.addWidget(self._cam_combo)

        self._mic_label = QLabel('Microphone:')
        inner.addWidget(self._mic_label)
        self._mic_combo = QComboBox()
        self._mic_combo.addItem('No audio', -1)
        for label, idx in enumerate_mics():
            self._mic_combo.addItem(label, idx)
        inner.addWidget(self._mic_combo)

        start_btn = QPushButton('Start')
        start_btn.setStyleSheet(
            'QPushButton { background: #555; color: white; padding: 8px; '
            'border-radius: 4px; font-size: 14px; }'
            'QPushButton:hover { background: #666; }'
        )
        start_btn.clicked.connect(self._on_start)
        inner.addWidget(start_btn)

        outer.addWidget(card, alignment=Qt.AlignCenter)

    def _on_source_changed(self):
        is_cam = self._src_combo.currentData() == 'camera'
        # Screen capture picks its target in the portal dialog → hide camera combo,
        # and has no audio → hide the mic picker.
        self._cam_label.setVisible(is_cam)
        self._cam_combo.setVisible(is_cam)
        self._mic_label.setVisible(is_cam)
        self._mic_combo.setVisible(is_cam)

    def _on_start(self):
        source = self._src_combo.currentData()
        cam_path = self._cam_combo.currentData() or ''
        mic_idx = self._mic_combo.currentData()
        mic_idx = mic_idx if mic_idx is not None else -1
        if source == 'camera' and not cam_path:
            return
        self.start_requested.emit(source, cam_path, mic_idx)

# ─── Main window ──────────────────────────────────────────────────────────────

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle('Trinitron')

        self._stack = QStackedWidget()
        self.setCentralWidget(self._stack)

        self._setup_page = SetupPage()
        self._setup_page.start_requested.connect(self._on_start)
        self._stack.addWidget(self._setup_page)

        self._viewer: ViewerPage | None = None
        self.showMaximized()

    def _on_start(self, source: str, device: str, mic_idx: int):
        if self._viewer is not None:
            return  # guard against double-click / duplicate signal
        self._viewer = ViewerPage(source, device, mic_idx)
        self._stack.addWidget(self._viewer)
        self._stack.setCurrentWidget(self._viewer)

    def closeEvent(self, event):
        if self._viewer:
            self._viewer.cleanup()
        super().closeEvent(event)

# ─── Entry point ──────────────────────────────────────────────────────────────

def main():
    app = QApplication(sys.argv)
    app.setStyle('Fusion')

    pal = QPalette()
    pal.setColor(QPalette.Window,          QColor(18, 18, 18))
    pal.setColor(QPalette.WindowText,      Qt.white)
    pal.setColor(QPalette.Base,            QColor(30, 30, 30))
    pal.setColor(QPalette.AlternateBase,   QColor(45, 45, 45))
    pal.setColor(QPalette.Text,            Qt.white)
    pal.setColor(QPalette.Button,          QColor(45, 45, 45))
    pal.setColor(QPalette.ButtonText,      Qt.white)
    pal.setColor(QPalette.Highlight,       QColor(70, 70, 120))
    pal.setColor(QPalette.HighlightedText, Qt.white)
    app.setPalette(pal)

    win = MainWindow()
    sys.exit(app.exec_())


if __name__ == '__main__':
    main()
