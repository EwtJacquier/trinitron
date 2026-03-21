#!/usr/bin/env python3
"""
native.py — Python CRT Webcam Viewer
Replicates index.html using PyQt5 + wgpu + OpenCV + sounddevice.
Captures via V4L2 (YUYV, uncompressed) instead of MJPEG via getUserMedia.
Renders via wgpu-py (Vulkan on Linux), enabling LSFG-VK frame generation:
  ENABLE_LSFG_VK=1 python native.py
"""

import sys
import os
import re
import time
import glob
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
                 options: dict, stop_event: _mp.Event) -> None:
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
        for frame in container.decode(stream):
            if stop_event.is_set():
                break
            img = frame.to_ndarray(format='bgr24')
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
        self._options = {
            'video_size':   '1920x1080',
            'framerate':    '60',
            'pixel_format': 'yuyv422',
        }

    def set_pixel_format(self, fmt: str):
        self._options = {**self._options, 'pixel_format': fmt}
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
                      self._stop_event),
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
                    frame = frame_buf.copy()
                    if first:
                        print(f'CaptureThread: first frame {frame.shape}',
                              flush=True)
                        first = False
                    self.frame_ready.emit(frame)
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

# ─── Audio passthrough ────────────────────────────────────────────────────────

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

    # ── Lifecycle ───────────────────────────────────────────────────────────

    def _rc_get_present_info(self, present_methods):
        """Force Vulkan screen present so LSFG-VK can intercept vkQueuePresentKHR."""
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
        device = self._device
        img_h, img_w = frame.shape[:2]
        bgra = np.ascontiguousarray(cv2.cvtColor(frame, cv2.COLOR_BGR2BGRA))

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
            bgra.tobytes(),
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

        if is_original:
            upload = frame
            tex_w, tex_h = float(ORIG_W), float(ORIG_H)
            canvas_w, canvas_h = ORIG_W, ORIG_H
        else:
            upload = cv2.resize(frame, (DOWNSCALE_W, DOWNSCALE_H),
                                interpolation=cv2.INTER_NEAREST)
            tex_w, tex_h = 640.0, 360.0   # intentional: matches web app
            canvas_w, canvas_h = CANVAS_W, CANVAS_H

        self._upload_webcam_frame(upload)
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
        sw, sh = self.width(), self.height()
        if sw <= 0 or sh <= 0:
            device.queue.submit([cmd.finish()])
            return
        vx, vy, vw, vh = self._letterbox(sw, sh)

        current_tex = self._ctx.get_current_texture()
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
        self.request_draw()  # re-schedule for continuous animation

        # ── FPS counter ───────────────────────────────────────────────────
        self._frame_count += 1
        elapsed = t_now - self._fps_timer
        if elapsed >= 1.0:
            fps = self._frame_count / elapsed
            self._frame_count = 0
            self._fps_timer = t_now
            lsfg = int(os.environ.get('ENABLE_LSFG_VK', '0'))
            if lsfg:
                mult = int(os.environ.get('LSFG_MULTIPLIER', '2'))
                self.fps_updated.emit(f'{fps:.0f}→{fps * mult:.0f} FPS')
            else:
                self.fps_updated.emit(f'{fps:.0f} FPS')

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

        layout.addWidget(QLabel('Capture Format'))
        self._fmt_combo = QComboBox()
        for label, fmt in CAPTURE_FORMATS.items():
            self._fmt_combo.addItem(label, fmt)
        self._fmt_combo.currentIndexChanged.connect(
            lambda i: self.format_changed.emit(self._fmt_combo.itemData(i)))
        layout.addWidget(self._fmt_combo)

        layout.addWidget(QLabel('Volume'))
        self._vol_slider = QSlider(Qt.Horizontal)
        self._vol_slider.setRange(0, 100)
        self._vol_slider.setValue(80)
        self._vol_slider.valueChanged.connect(
            lambda v: self.volume_changed.emit(v / 100.0))
        layout.addWidget(self._vol_slider)

        layout.addStretch()

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
    def __init__(self, camera_path: str, mic_idx: int, parent=None):
        super().__init__(parent)
        self.setStyleSheet('background: black;')

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

        # Capture thread
        self._capture = CaptureThread(camera_path)
        self._capture.frame_ready.connect(self._gl.on_frame)
        self._capture.start()

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
    start_requested = pyqtSignal(str, int)

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

        inner.addWidget(QLabel('Camera:'))
        self._cam_combo = QComboBox()
        cameras = enumerate_cameras()
        if cameras:
            for label, path in cameras:
                self._cam_combo.addItem(label, path)
        else:
            self._cam_combo.addItem('No camera found', '')
        inner.addWidget(self._cam_combo)

        inner.addWidget(QLabel('Microphone:'))
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

    def _on_start(self):
        cam_path = self._cam_combo.currentData()
        mic_idx = self._mic_combo.currentData()
        if cam_path:
            self.start_requested.emit(cam_path, mic_idx if mic_idx is not None else -1)

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

    def _on_start(self, cam_path: str, mic_idx: int):
        if self._viewer is not None:
            return  # guard against double-click / duplicate signal
        self._viewer = ViewerPage(cam_path, mic_idx)
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
