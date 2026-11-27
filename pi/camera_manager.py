"""Camera manager for Raspberry Pi 5 AI Camera (IMX500) + Mock FPV fallback."""

import io
import math
import os
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime

try:
    import cv2
except ImportError:
    cv2 = None

try:
    import numpy as np
except ImportError:
    np = None

try:
    from PIL import Image, ImageDraw
except ImportError:
    Image = None
    ImageDraw = None

import config

SAFE_FILENAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
PHOTO_EXTS = (".jpg", ".jpeg", ".png")
VIDEO_EXTS = (".mp4", ".mjpg", ".avi")


class _FfmpegRecorder:
    """Pipes raw BGR24 frames into ffmpeg to produce iPhone-ready H.264 MP4."""

    def __init__(self, ffmpeg_bin, out_path, width, height, fps):
        cmd = [
            ffmpeg_bin,
            "-y",
            "-loglevel",
            "error",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "bgr24",
            "-s",
            "{0}x{1}".format(width, height),
            "-r",
            str(fps),
            "-i",
            "-",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-tune",
            "zerolatency",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            out_path,
        ]
        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def write(self, frame_bgr, _jpeg_bytes):
        if self._proc.stdin is None or frame_bgr is None:
            return
        try:
            self._proc.stdin.write(frame_bgr.tobytes())
        except OSError:
            pass

    def close(self):
        if self._proc.stdin is not None:
            try:
                self._proc.stdin.close()
            except OSError:
                pass
        try:
            self._proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                self._proc.kill()


class _OpenCvRecorder:
    """Writes BGR frames via OpenCV VideoWriter."""

    def __init__(self, out_path, width, height, fps):
        self._writer = None
        for codec in ("avc1", "mp4v"):
            fourcc = cv2.VideoWriter_fourcc(*codec)
            writer = cv2.VideoWriter(out_path, fourcc, float(fps), (width, height))
            if writer.isOpened():
                self._writer = writer
                break
            writer.release()
        if self._writer is None:
            raise RuntimeError("OpenCV VideoWriter failed to open output MP4")

    def write(self, frame_bgr, _jpeg_bytes):
        if self._writer is not None and frame_bgr is not None:
            self._writer.write(frame_bgr)

    def close(self):
        if self._writer is not None:
            self._writer.release()
            self._writer = None


class _MjpegRecorder:
    """Minimal fallback recorder writing sequential JPEG frames."""

    def __init__(self, out_path):
        self._file = open(out_path, "wb")

    def write(self, _frame_bgr, jpeg_bytes):
        if self._file is not None and jpeg_bytes:
            self._file.write(jpeg_bytes)

    def close(self):
        if self._file is not None:
            self._file.close()
            self._file = None


class CameraManager:
    """Manages live MJPEG streaming, IMX500 AI overlays, photos, and videos."""

    def __init__(
        self,
        width=config.CAM_WIDTH,
        height=config.CAM_HEIGHT,
        fps=config.CAM_FPS,
        jpeg_quality=config.JPEG_QUALITY,
        ai_model_path=config.AI_MODEL_PATH,
        ai_threshold=config.AI_THRESHOLD,
        ai_enabled=config.AI_ENABLED_DEFAULT,
        mock=config.MOCK_MODE,
        photos_dir=config.PHOTOS_DIR,
        videos_dir=config.VIDEOS_DIR,
    ):
        self.width = width
        self.height = height
        self.fps = fps
        self.jpeg_quality = jpeg_quality
        self.ai_model_path = ai_model_path
        self.ai_threshold = ai_threshold
        self.photos_dir = photos_dir
        self.videos_dir = videos_dir
        os.makedirs(self.photos_dir, exist_ok=True)
        os.makedirs(self.videos_dir, exist_ok=True)

        self._lock = threading.Lock()
        self._rec_lock = threading.Lock()
        self._frame_cond = threading.Condition(self._lock)
        self._stop_event = threading.Event()
        self._thread = None

        self._picam2 = None
        self._imx500 = None
        self._intrinsics = None
        self._mock = bool(mock)
        self._ai_enabled = bool(ai_enabled)
        self._ai_available = bool(mock)
        self._detections = []
        self._last_detections = []
        self._last_det_ts = 0.0

        self._latest_jpeg = None
        self._frame_seq = 0
        self._recorder = None
        self._recording_filename = None
        self._recording_path = None
        self._recording_started_at = None

    def start(self):
        """Initialize camera hardware (or mock generator) and start capture loop."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        if not self._mock:
            self._init_real_camera()
        self._thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._thread.start()

    def close(self):
        """Stop video recording, capture thread, and Picamera2 hardware."""
        self.stop_recording()
        self._stop_event.set()
        with self._frame_cond:
            self._frame_cond.notify_all()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
        if self._picam2 is not None:
            try:
                self._picam2.stop()
                self._picam2.close()
            except Exception:
                pass
            self._picam2 = None

    def _init_real_camera(self):
        try:
            from picamera2 import Picamera2
        except ImportError:
            self._mock = True
            self._ai_available = True
            return

        try:
            from picamera2.devices import IMX500
        except ImportError:
            IMX500 = None

        try:
            if IMX500 is not None and os.path.exists(self.ai_model_path):
                self._imx500 = IMX500(self.ai_model_path)
                self._intrinsics = getattr(self._imx500, "network_intrinsics", None)
                self._picam2 = Picamera2(self._imx500.camera_num)
                self._ai_available = True
            else:
                self._picam2 = Picamera2()
                self._ai_available = False

            cam_cfg = self._picam2.create_video_configuration(
                main={"size": (self.width, self.height), "format": "RGB888"},
                controls={"FrameRate": self.fps},
            )
            self._picam2.configure(cam_cfg)
            self._picam2.start()
        except Exception:
            if self._picam2 is not None:
                try:
                    self._picam2.close()
                except Exception:
                    pass
                self._picam2 = None
            self._imx500 = None
            self._mock = True
            self._ai_available = True

    def set_ai_enabled(self, enabled):
        """Enable or disable live AI detection bounding-box overlay."""
        with self._lock:
            self._ai_enabled = bool(enabled)
            return self._ai_enabled

    def get_status(self):
        """Return JSON-serializable camera, AI, and recording state."""
        with self._lock:
            rec_duration = 0.0
            if self._recorder is not None and self._recording_started_at is not None:
                rec_duration = max(0.0, time.monotonic() - self._recording_started_at)
            return {
                "mock": self._mock,
                "ai_enabled": self._ai_enabled,
                "ai_available": self._ai_available,
                "detection_count": len(self._detections) if self._ai_enabled else 0,
                "detections": list(self._detections) if self._ai_enabled else [],
                "recording": self._recorder is not None,
                "recording_filename": self._recording_filename,
                "recording_seconds": round(rec_duration, 1),
                "width": self.width,
                "height": self.height,
            }

    def _capture_loop(self):
        interval = 1.0 / max(1, self.fps)
        while not self._stop_event.is_set():
            started = time.monotonic()
            if self._mock:
                frame_bgr, jpeg_bytes, detections = self._render_mock_frame(started)
            else:
                frame_bgr, jpeg_bytes, detections = self._capture_real_frame()

            if jpeg_bytes is not None:
                with self._frame_cond:
                    self._latest_jpeg = jpeg_bytes
                    self._detections = detections
                    self._frame_seq += 1
                    self._frame_cond.notify_all()
                with self._rec_lock:
                    if self._recorder is not None:
                        self._recorder.write(frame_bgr, jpeg_bytes)

            elapsed = time.monotonic() - started
            sleep_for = max(0.005, interval - elapsed)
            self._stop_event.wait(sleep_for)

    def _encode_bgr_to_jpeg(self, frame_bgr):
        if cv2 is not None:
            ok, encoded = cv2.imencode(
                ".jpg",
                frame_bgr,
                [int(cv2.IMWRITE_JPEG_QUALITY), self.jpeg_quality],
            )
            return bytes(encoded) if ok else None
        if Image is not None and frame_bgr is not None:
            rgb = frame_bgr[:, :, ::-1]
            img = Image.fromarray(rgb, mode="RGB")
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=self.jpeg_quality)
            return buf.getvalue()
        return None

    def _capture_real_frame(self):
        if self._picam2 is None:
            return (None, None, [])
        request = self._picam2.capture_request()
        try:
            frame_bgr = request.make_array("main")
            metadata = request.get_metadata()
        finally:
            request.release()

        detections = []
        with self._lock:
            ai_on = self._ai_enabled

        if ai_on and self._imx500 is not None:
            detections = self._extract_imx500_detections(metadata)
            if detections:
                frame_bgr = frame_bgr.copy()
                self._draw_detections_cv2(frame_bgr, detections)

        jpeg_bytes = self._encode_bgr_to_jpeg(frame_bgr)
        return (frame_bgr, jpeg_bytes, detections)

    def _extract_imx500_detections(self, metadata):
        np_outputs = self._imx500.get_outputs(metadata, add_batch=True)
        now = time.monotonic()
        if np_outputs is None or len(np_outputs) < 3:
            if now - self._last_det_ts < 0.25:
                return self._last_detections
            return []

        boxes = np_outputs[0][0]
        scores = np_outputs[1][0]
        classes = np_outputs[2][0]

        if getattr(self._intrinsics, "bbox_normalization", False):
            _, input_h = self._imx500.get_input_size()
            if input_h:
                boxes = boxes / float(input_h)
        if getattr(self._intrinsics, "bbox_order", "yx") == "xy":
            boxes = boxes[:, [1, 0, 3, 2]]

        labels = getattr(self._intrinsics, "labels", None) or []
        results = []
        for box, score, category in zip(boxes, scores, classes):
            conf = float(score)
            if conf < self.ai_threshold:
                continue
            coords = self._imx500.convert_inference_coords(
                box, metadata, self._picam2
            )
            x, y, w, h = [int(v) for v in coords]
            cat_idx = int(category)
            label = (
                labels[cat_idx]
                if 0 <= cat_idx < len(labels) and labels[cat_idx] != "-"
                else "obj #{0}".format(cat_idx)
            )
            results.append(
                {"label": str(label), "score": round(conf, 2), "box": [x, y, w, h]}
            )

        self._last_detections = results
        self._last_det_ts = now
        return results

    def _draw_detections_cv2(self, frame_bgr, detections):
        if cv2 is None:
            return
        accent = (230, 210, 40)
        for det in detections:
            x, y, w, h = det["box"]
            x2, y2 = x + w, y + h
            cv2.rectangle(frame_bgr, (x, y), (x2, y2), accent, 2)
            text = "{0} {1:.0%}".format(det["label"], det["score"])
            (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 0.55, 2)
            label_y = max(th + 8, y)
            cv2.rectangle(
                frame_bgr, (x, label_y - th - 8), (x + tw + 10, label_y), accent, -1
            )
            cv2.putText(
                frame_bgr,
                text,
                (x + 5, label_y - 5),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.55,
                (15, 20, 30),
                2,
                cv2.LINE_AA,
            )

    def _render_mock_frame(self, now):
        w, h = self.width, self.height
        horizon = int(h * 0.44)
        with self._lock:
            ai_on = self._ai_enabled

        detections = []
        if ai_on:
            bx = int(w * (0.26 + 0.16 * math.sin(now * 0.7)))
            by = int(h * 0.48)
            detections.append(
                {"label": "buoy", "score": 0.94, "box": [bx, by, 92, 118]}
            )
            bx2 = int(w * (0.62 - 0.12 * math.cos(now * 0.5)))
            by2 = int(h * 0.42)
            detections.append(
                {"label": "boat", "score": 0.88, "box": [bx2, by2, 148, 84]}
            )

        if np is not None:
            frame = np.zeros((h, w, 3), dtype=np.uint8)
            frame[:horizon, :] = (38, 22, 14)   # deep twilight sky (BGR)
            frame[horizon:, :] = (56, 36, 12)   # marine water (BGR)
        else:
            frame = None

        if cv2 is not None and frame is not None:
            cv2.line(frame, (0, horizon), (w, horizon), (140, 115, 70), 2)
            for idx in range(6):
                ry = horizon + 25 + idx * 42
                shift = int(28 * math.sin(now * 2.0 + idx))
                cv2.line(
                    frame,
                    (60 + shift, ry),
                    (w - 60 + shift, ry),
                    (92, 68, 28),
                    1,
                )
            cx, cy = w // 2, h // 2
            cv2.circle(frame, (cx, cy), 18, (180, 220, 255), 1)
            cv2.line(frame, (cx - 28, cy), (cx + 28, cy), (180, 220, 255), 1)
            cv2.line(frame, (cx, cy - 28), (cx, cy + 28), (180, 220, 255), 1)
            stamp = datetime.now().strftime("SIM CAM  %H:%M:%S")
            cv2.putText(
                frame,
                stamp,
                (18, 30),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.65,
                (210, 230, 250),
                2,
                cv2.LINE_AA,
            )
            if ai_on:
                self._draw_detections_cv2(frame, detections)
            return (frame, self._encode_bgr_to_jpeg(frame), detections)

        if Image is not None:
            img = Image.new("RGB", (w, h), (14, 22, 38))
            draw = ImageDraw.Draw(img)
            draw.rectangle([0, 0, w, horizon], fill=(14, 22, 38))
            draw.rectangle([0, horizon, w, h], fill=(12, 36, 56))
            draw.line([(0, horizon), (w, horizon)], fill=(70, 115, 140), width=2)
            for idx in range(6):
                ry = horizon + 25 + idx * 42
                shift = int(28 * math.sin(now * 2.0 + idx))
                draw.line([(60 + shift, ry), (w - 60 + shift, ry)], fill=(28, 68, 92))
            cx, cy = w // 2, h // 2
            draw.ellipse([cx - 18, cy - 18, cx + 18, cy + 18], outline=(200, 230, 255))
            draw.text((18, 18), datetime.now().strftime("SIM CAM %H:%M:%S"), fill=(220, 235, 250))
            if ai_on:
                for det in detections:
                    x, y, bw, bh = det["box"]
                    draw.rectangle([x, y, x + bw, y + bh], outline=(40, 210, 230), width=2)
                    draw.text((x + 4, max(4, y - 14)), "{0} {1:.0%}".format(det["label"], det["score"]), fill=(40, 210, 230))
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=self.jpeg_quality)
            frame_bgr = np.asarray(img)[:, :, ::-1].copy() if np is not None else None
            return (frame_bgr, buf.getvalue(), detections)

        return (None, None, [])

    def frame_generator(self):
        """Yield multipart MJPEG chunks whenever new frames arrive."""
        last_seq = -1
        while not self._stop_event.is_set():
            with self._frame_cond:
                while (
                    self._frame_seq == last_seq
                    and not self._stop_event.is_set()
                ):
                    self._frame_cond.wait(timeout=1.0)
                if self._stop_event.is_set():
                    break
                jpeg = self._latest_jpeg
                last_seq = self._frame_seq
            if jpeg:
                yield (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n"
                    + "Content-Length: {0}\r\n\r\n".format(len(jpeg)).encode("ascii")
                    + jpeg
                    + b"\r\n"
                )

    def _unique_path(self, directory, prefix, ext):
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        candidate = "{0}_{1}{2}".format(prefix, stamp, ext)
        full = os.path.join(directory, candidate)
        idx = 1
        while os.path.exists(full):
            candidate = "{0}_{1}_{2}{3}".format(prefix, stamp, idx, ext)
            full = os.path.join(directory, candidate)
            idx += 1
        return candidate, full

    def capture_photo(self):
        """Save the latest frame as a JPEG photo without interrupting streaming."""
        with self._lock:
            jpeg = self._latest_jpeg
        if not jpeg:
            raise RuntimeError("No camera frame available yet")
        filename, full_path = self._unique_path(self.photos_dir, "photo", ".jpg")
        with open(full_path, "wb") as handle:
            handle.write(jpeg)
        return self._build_media_entry("photos", filename, full_path)

    def start_recording(self):
        """Start background video recording alongside the live MJPEG stream."""
        with self._rec_lock:
            with self._lock:
                if self._recorder is None:
                    ffmpeg_bin = shutil.which("ffmpeg")
                    if ffmpeg_bin is not None:
                        filename, full_path = self._unique_path(
                            self.videos_dir, "video", ".mp4"
                        )
                        recorder = _FfmpegRecorder(
                            ffmpeg_bin, full_path, self.width, self.height, self.fps
                        )
                    elif cv2 is not None:
                        filename, full_path = self._unique_path(
                            self.videos_dir, "video", ".mp4"
                        )
                        recorder = _OpenCvRecorder(
                            full_path, self.width, self.height, self.fps
                        )
                    else:
                        filename, full_path = self._unique_path(
                            self.videos_dir, "video", ".mjpg"
                        )
                        recorder = _MjpegRecorder(full_path)

                    self._recorder = recorder
                    self._recording_filename = filename
                    self._recording_path = full_path
                    self._recording_started_at = time.monotonic()

        return self.get_status()

    def stop_recording(self):
        """Finalize the active video recording and return saved media info or None."""
        with self._rec_lock:
            with self._lock:
                recorder = self._recorder
                filename = self._recording_filename
                full_path = self._recording_path
                self._recorder = None
                self._recording_filename = None
                self._recording_path = None
                self._recording_started_at = None

            if recorder is None:
                return None
            recorder.close()

        if full_path and os.path.isfile(full_path):
            return self._build_media_entry("videos", filename, full_path)
        return None

    def _build_media_entry(self, kind, filename, full_path):
        st = os.stat(full_path)
        return {
            "kind": kind,
            "filename": filename,
            "size": st.st_size,
            "mtime": st.st_mtime,
            "url": "/media/{0}/{1}".format(kind, filename),
            "download_url": "/media/{0}/{1}?download=1".format(kind, filename),
        }

    def list_media(self):
        """List saved photos and videos ordered newest-first."""
        items = []
        with self._lock:
            active_video = self._recording_filename

        for kind, directory, exts in (
            ("photos", self.photos_dir, PHOTO_EXTS),
            ("videos", self.videos_dir, VIDEO_EXTS),
        ):
            if not os.path.isdir(directory):
                continue
            for fname in os.listdir(directory):
                if kind == "videos" and fname == active_video:
                    continue
                if not fname.lower().endswith(exts):
                    continue
                full_path = os.path.join(directory, fname)
                if os.path.isfile(full_path):
                    items.append(self._build_media_entry(kind, fname, full_path))

        items.sort(key=lambda item: item["mtime"], reverse=True)
        return items

    def resolve_media_file(self, kind, filename):
        """Safely resolve a media file path or return None if invalid/missing."""
        if kind not in ("photos", "videos"):
            return None
        if not filename or not SAFE_FILENAME_RE.match(filename) or ".." in filename:
            return None
        base = self.photos_dir if kind == "photos" else self.videos_dir
        full_path = os.path.join(base, filename)
        if os.path.isfile(full_path):
            return full_path
        return None

    def delete_media(self, kind, filename):
        """Delete a media file unless it is currently being recorded."""
        with self._lock:
            if kind == "videos" and filename == self._recording_filename:
                return (False, "recording_active")
        full_path = self.resolve_media_file(kind, filename)
        if full_path is None:
            return (False, "not_found")
        os.remove(full_path)
        return (True, "deleted")
