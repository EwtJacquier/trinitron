#!/usr/bin/env python3
"""
native.py — Python CRT Webcam Viewer
Replicates index.html using PyQt5 + PyOpenGL + OpenCV + sounddevice.
Captures via V4L2 (YUYV, uncompressed) instead of MJPEG via getUserMedia.
"""

import sys
import os
import re
import time
import glob
import ctypes
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
    QOpenGLWidget,
)
from PyQt5.QtCore import Qt, QThread, pyqtSignal, QMutex, QMutexLocker, QTimer
from PyQt5.QtGui import QPalette, QColor, QSurfaceFormat

from OpenGL.GL import (
    glClearColor, glClear, glViewport, glGenBuffers, glBindBuffer,
    glBufferData, glGenTextures, glBindTexture, glTexImage2D,
    glTexParameteri, glGenFramebuffers, glBindFramebuffer,
    glFramebufferTexture2D, glDeleteFramebuffers, glDeleteTextures,
    glDeleteBuffers, glDrawArrays, glUseProgram, glActiveTexture,
    glEnableVertexAttribArray, glDisableVertexAttribArray,
    glVertexAttribPointer, glGetAttribLocation, glGetUniformLocation,
    glUniform1i, glUniform1f, glUniform2f,
    glGenVertexArrays, glBindVertexArray, glDeleteVertexArrays,
    glPixelStorei, glGetError, glGetString, glTexSubImage2D,
    GL_COLOR_BUFFER_BIT, GL_ARRAY_BUFFER, GL_STATIC_DRAW,
    GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_TEXTURE_MAG_FILTER,
    GL_TEXTURE_WRAP_S, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE,
    GL_LINEAR, GL_NEAREST, GL_RGB, GL_BGR, GL_RGBA8, GL_RGBA, GL_UNSIGNED_BYTE,
    GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_FLOAT, GL_FALSE,
    GL_TRIANGLE_STRIP, GL_TEXTURE0, GL_VERTEX_SHADER, GL_FRAGMENT_SHADER,
    GL_UNPACK_ALIGNMENT, GL_NO_ERROR, GL_VENDOR, GL_RENDERER, GL_VERSION,
)
from OpenGL.GL import shaders as gl_shaders

# ─── Constants ────────────────────────────────────────────────────────────────

CANVAS_W, CANVAS_H = 2562, 1440
ORIG_W, ORIG_H = 1920, 1080
DOWNSCALE_W, DOWNSCALE_H = 854, 480

SHADER_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'shaders')

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

# ─── Inline shaders ───────────────────────────────────────────────────────────

# Shared vertex shader (same as vertex.glsl but desktop-patched inline)
VERT_SRC = """\
#version 120
attribute vec4 a_position;
attribute vec2 a_texCoord;
varying vec2 v_texCoord;
void main() {
    gl_Position = a_position;
    v_texCoord = a_texCoord;
}
"""

# Post-process fragment shader: brightness + saturation
POST_FRAG_SRC = """\
#version 120
varying vec2 v_texCoord;
uniform sampler2D u_texture;
uniform float u_brightness;
uniform float u_saturation;
void main() {
    vec4 c = texture2D(u_texture, v_texCoord);
    c.rgb *= u_brightness;
    float luma = dot(c.rgb, vec3(0.2126, 0.7152, 0.0722));
    c.rgb = mix(vec3(luma), c.rgb, u_saturation);
    gl_FragColor = clamp(c, 0.0, 1.0);
}
"""

# ─── GLSL ES → Desktop GL patching ───────────────────────────────────────────

def patch_for_desktop(src: str) -> str:
    """Strip GLSL ES precision qualifiers and prepend #version 120."""
    src = re.sub(r'^\s*precision\s+(lowp|mediump|highp)\s+\w+\s*;\n?',
                 '', src, flags=re.MULTILINE)
    src = re.sub(r'\b(lowp|mediump|highp)\b\s+', '', src)
    return '#version 120\n' + src


def load_filter_shader(name: str) -> str:
    path = os.path.join(SHADER_DIR, f'{name}.glsl')
    with open(path, 'r') as f:
        return patch_for_desktop(f.read())

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
            img = cv2.flip(img, 0)
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

# ─── GL Widget ────────────────────────────────────────────────────────────────

class GLWidget(QOpenGLWidget):
    fps_updated = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._mutex = QMutex()
        self._frame = None
        self._filter = 'original'
        self._aspect = (1920, 1080)

        # GL objects
        self._programs: dict = {}
        self._post_program = None
        self._vao = None
        self._vbo = None
        self._texture = 0
        self._fbo = 0
        self._fbo_texture = 0
        self._fbo_size = (0, 0)
        self._initialized = False

        self._start_time = time.time()
        self._frame_count = 0
        self._fps_timer = time.time()

    # ── Public API ──────────────────────────────────────────────────────────

    def set_filter(self, name: str):
        self._filter = name
        self.update()

    def set_aspect(self, ratio: tuple):
        self._aspect = ratio
        self.update()

    def on_frame(self, frame: np.ndarray):
        with QMutexLocker(self._mutex):
            self._frame = frame
        self.update()

    # ── GL lifecycle ────────────────────────────────────────────────────────

    def initializeGL(self):
        try:
            self._initializeGL_inner()
        except Exception as e:
            import traceback
            print('initializeGL FAILED:')
            traceback.print_exc()

    def _initializeGL_inner(self):
        print(f'GL vendor:   {glGetString(GL_VENDOR).decode()}')
        print(f'GL renderer: {glGetString(GL_RENDERER).decode()}')
        print(f'GL version:  {glGetString(GL_VERSION).decode()}')

        glClearColor(0.0, 0.0, 0.0, 1.0)

        # VAO — required in core profile; harmless in compat profile
        self._vao = int(glGenVertexArrays(1))
        glBindVertexArray(self._vao)

        # Compile filter programs
        for name in FILTER_NAMES:
            frag_src = load_filter_shader(name)
            prog = self._compile_program(VERT_SRC, frag_src)
            self._programs[name] = prog
            print(f'Compiled shader: {name}')

        # Compile post-process program
        self._post_program = self._compile_program(VERT_SRC, POST_FRAG_SRC)
        print('Compiled shader: post')

        # Full-screen quad VBO: (x, y, u, v) x 4 vertices
        # V is flipped so OpenCV rows appear correctly on screen
        verts = np.array([
            -1.0,  1.0,  0.0, 1.0,   # top-left
             1.0,  1.0,  1.0, 1.0,   # top-right
            -1.0, -1.0,  0.0, 0.0,   # bottom-left
             1.0, -1.0,  1.0, 0.0,   # bottom-right
        ], dtype=np.float32)
        self._vbo = int(glGenBuffers(1))
        glBindBuffer(GL_ARRAY_BUFFER, self._vbo)
        glBufferData(GL_ARRAY_BUFFER, verts.nbytes, verts, GL_STATIC_DRAW)
        glBindBuffer(GL_ARRAY_BUFFER, 0)

        self._texture = int(glGenTextures(1))
        self._texture_size = (0, 0)
        self._initialized = True
        print('initializeGL OK')

    def _compile_program(self, vert_src: str, frag_src: str):
        vert = gl_shaders.compileShader(vert_src, GL_VERTEX_SHADER)
        frag = gl_shaders.compileShader(frag_src, GL_FRAGMENT_SHADER)
        return gl_shaders.compileProgram(vert, frag)

    def _ensure_fbo(self, w: int, h: int):
        if self._fbo_size == (w, h):
            return
        if self._fbo:
            glDeleteFramebuffers(1, [self._fbo])
            glDeleteTextures(1, [self._fbo_texture])

        self._fbo_texture = int(glGenTextures(1))
        glBindTexture(GL_TEXTURE_2D, self._fbo_texture)
        glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA8, w, h, 0,
                     GL_RGBA, GL_UNSIGNED_BYTE, None)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR)
        glBindTexture(GL_TEXTURE_2D, 0)

        self._fbo = int(glGenFramebuffers(1))
        glBindFramebuffer(GL_FRAMEBUFFER, self._fbo)
        glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0,
                               GL_TEXTURE_2D, self._fbo_texture, 0)
        from OpenGL.GL import glCheckFramebufferStatus, GL_FRAMEBUFFER_COMPLETE
        status = glCheckFramebufferStatus(GL_FRAMEBUFFER)
        if status != GL_FRAMEBUFFER_COMPLETE:
            print(f'FBO incomplete: {status}')
        glBindFramebuffer(GL_FRAMEBUFFER, self.defaultFramebufferObject())
        self._fbo_size = (w, h)

    def resizeGL(self, w: int, h: int):
        pass  # handled in paintGL

    def paintGL(self):
        if not self._initialized:
            glClear(GL_COLOR_BUFFER_BIT)
            return
        try:
            self._paintGL_inner()
        except Exception:
            import traceback
            traceback.print_exc()

    def _paintGL_inner(self):
        t_now = time.time()

        with QMutexLocker(self._mutex):
            frame = self._frame

        if frame is None:
            glBindFramebuffer(GL_FRAMEBUFFER, self.defaultFramebufferObject())
            glClear(GL_COLOR_BUFFER_BIT)
            return

        # flip already applied in CaptureThread

        filter_name = self._filter
        is_original = (filter_name == 'original')

        # ── Prepare texture data ──────────────────────────────────────────
        if is_original:
            upload = frame
            tex_w, tex_h = float(ORIG_W), float(ORIG_H)
            canvas_w, canvas_h = ORIG_W, ORIG_H
            tex_filter = GL_NEAREST
        else:
            upload = cv2.resize(frame, (DOWNSCALE_W, DOWNSCALE_H),
                                interpolation=cv2.INTER_NEAREST)
            tex_w, tex_h = 640.0, 360.0   # intentional: matches web app
            canvas_w, canvas_h = CANVAS_W, CANVAS_H
            tex_filter = GL_LINEAR

        upload_bgr = np.ascontiguousarray(upload)
        img_h, img_w = upload_bgr.shape[:2]

        glPixelStorei(GL_UNPACK_ALIGNMENT, 1)
        glBindTexture(GL_TEXTURE_2D, self._texture)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, tex_filter)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, tex_filter)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE)
        if self._texture_size != (img_w, img_h):
            glTexImage2D(GL_TEXTURE_2D, 0, GL_RGB, img_w, img_h, 0,
                         GL_BGR, GL_UNSIGNED_BYTE, upload_bgr)
            self._texture_size = (img_w, img_h)
        else:
            glTexSubImage2D(GL_TEXTURE_2D, 0, 0, 0, img_w, img_h,
                            GL_BGR, GL_UNSIGNED_BYTE, upload_bgr)
        err = glGetError()
        if err != GL_NO_ERROR:
            print(f'glTex*Image2D error: {err}')
        glBindTexture(GL_TEXTURE_2D, 0)

        # ── Pass 1: filter → FBO ──────────────────────────────────────────
        self._ensure_fbo(canvas_w, canvas_h)

        glBindFramebuffer(GL_FRAMEBUFFER, self._fbo)
        glViewport(0, 0, canvas_w, canvas_h)
        glClear(GL_COLOR_BUFFER_BIT)

        prog = self._programs[filter_name]
        self._draw_quad(prog, self._texture, extras={
            'u_textureSize': (tex_w, tex_h),
            'u_time': (t_now - self._start_time) if filter_name in FILTERS_WITH_TIME else None,
        })

        # Restore Qt's internal FBO (NOT 0 — QOpenGLWidget uses its own FBO)
        qt_fbo = self.defaultFramebufferObject()
        glBindFramebuffer(GL_FRAMEBUFFER, qt_fbo)

        # ── Pass 2: post-process → screen with letterbox ──────────────────
        sw, sh = self.width(), self.height()
        vx, vy, vw, vh = self._letterbox(sw, sh)

        glViewport(0, 0, sw, sh)
        glClearColor(0.0, 0.0, 0.0, 1.0)
        glClear(GL_COLOR_BUFFER_BIT)
        glViewport(vx, vy, vw, vh)

        brightness, saturation = FILTER_POST.get(filter_name, (1.0, 1.0))
        self._draw_quad(self._post_program, self._fbo_texture, extras={
            'u_brightness': brightness,
            'u_saturation': saturation,
        })

        # ── FPS counter ───────────────────────────────────────────────────
        self._frame_count += 1
        elapsed = t_now - self._fps_timer
        if elapsed >= 1.0:
            fps = self._frame_count / elapsed
            self._frame_count = 0
            self._fps_timer = t_now
            self.fps_updated.emit(f'{fps:.0f} FPS')

    def _draw_quad(self, prog, texture, extras: dict):
        """Bind prog + texture, set uniforms in extras, draw fullscreen quad."""
        glBindVertexArray(self._vao)
        glUseProgram(prog)
        glBindBuffer(GL_ARRAY_BUFFER, self._vbo)

        stride = 16  # 4 floats * 4 bytes
        pos_loc = glGetAttribLocation(prog, 'a_position')
        tc_loc  = glGetAttribLocation(prog, 'a_texCoord')

        if pos_loc >= 0:
            glEnableVertexAttribArray(pos_loc)
            glVertexAttribPointer(pos_loc, 2, GL_FLOAT, GL_FALSE,
                                  stride, None)
        if tc_loc >= 0:
            glEnableVertexAttribArray(tc_loc)
            glVertexAttribPointer(tc_loc, 2, GL_FLOAT, GL_FALSE,
                                  stride, ctypes.c_void_p(8))

        glActiveTexture(GL_TEXTURE0)
        glBindTexture(GL_TEXTURE_2D, texture)
        u = glGetUniformLocation(prog, 'u_texture')
        if u >= 0:
            glUniform1i(u, 0)

        for name, val in extras.items():
            if val is None:
                continue
            loc = glGetUniformLocation(prog, name)
            if loc < 0:
                continue
            if isinstance(val, tuple):
                glUniform2f(loc, val[0], val[1])
            else:
                glUniform1f(loc, val)

        glDrawArrays(GL_TRIANGLE_STRIP, 0, 4)

        if pos_loc >= 0:
            glDisableVertexAttribArray(pos_loc)
        if tc_loc >= 0:
            glDisableVertexAttribArray(tc_loc)
        glBindBuffer(GL_ARRAY_BUFFER, 0)
        glUseProgram(0)

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

    def cleanup(self):
        self.makeCurrent()
        if self._texture:
            glDeleteTextures(1, [self._texture])
        if self._fbo:
            glDeleteFramebuffers(1, [self._fbo])
            glDeleteTextures(1, [self._fbo_texture])
        if self._vbo:
            glDeleteBuffers(1, [self._vbo])
        self.doneCurrent()

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

# ─── Viewer page ──────────────────────────────────────────────────────────────

class ViewerPage(QWidget):
    def __init__(self, camera_path: str, mic_idx: int, parent=None):
        super().__init__(parent)
        self.setStyleSheet('background: black;')

        self._gl = GLWidget(self)
        self._gl.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        # FPS label (top-left, hidden by default)
        self._fps_label = QLabel('-- FPS', self)
        self._fps_label.setStyleSheet(
            'color: white; background: rgba(0,0,0,128); '
            'padding: 4px; font-size: 13px; border-radius: 3px;'
        )
        self._fps_label.adjustSize()
        self._fps_label.move(10, 10)
        self._fps_label.setVisible(False)
        self._gl.fps_updated.connect(self._on_fps)

        # "Waiting for signal" overlay — visible until first FPS update
        self._waiting_label = QLabel('Waiting for signal\u2026', self)
        self._waiting_label.setStyleSheet(
            'color: white; font-size: 18px; background: transparent;'
        )
        self._waiting_label.setAlignment(Qt.AlignCenter)
        self._gl.fps_updated.connect(lambda _: self._waiting_label.setVisible(False))

        # Sidebar (hidden by default)
        self._sidebar = SidebarPanel(self)
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
        self._toggle_btn = QPushButton('☰', self)
        self._toggle_btn.setFixedSize(44, 44)
        self._toggle_btn.setStyleSheet(_btn_style)
        self._toggle_btn.clicked.connect(self._on_toggle)

        # Fullscreen button (next to sidebar toggle)
        self._fs_btn = QPushButton('⛶', self)
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
        self.setMouseTracking(True)
        self._gl.setMouseTracking(True)

        self._layout_widgets()

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

    def _hide_ui(self):
        self._ui_visible = False
        self._toggle_btn.setVisible(False)
        self._fs_btn.setVisible(False)
        self._sidebar.setVisible(False)
        self._fps_label.setVisible(False)
        self.setCursor(Qt.BlankCursor)

    def _show_ui(self):
        if not self._ui_visible:
            self._ui_visible = True
            self._toggle_btn.setVisible(True)
            self._fs_btn.setVisible(True)
            if self._fps_received:
                self._fps_label.setVisible(True)
            self.setCursor(Qt.ArrowCursor)
        self._hide_timer.start()

    def mouseMoveEvent(self, event):
        self._show_ui()
        super().mouseMoveEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._layout_widgets()

    def cleanup(self):
        self._capture.stop()
        if self._audio:
            self._audio.stop()
        self._gl.cleanup()

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
    # Request OpenGL 2.1 compatibility profile (supports attribute/varying/gl_FragColor)
    fmt = QSurfaceFormat()
    fmt.setVersion(2, 1)
    fmt.setProfile(QSurfaceFormat.CompatibilityProfile)
    fmt.setSwapInterval(0)  # disable VSync — let render run as fast as possible
    QSurfaceFormat.setDefaultFormat(fmt)

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
