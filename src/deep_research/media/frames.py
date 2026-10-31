"""Keyframe sampling: ``ffmpeg``/``ffprobe`` when on PATH, else PyAV, else a clear skip note."""

from __future__ import annotations

import importlib.util
import io
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from deep_research.media.errors import MediaError, MediaUnavailable

FRAME_WIDTH = 640


def ffmpeg_path() -> str | None:
    return shutil.which("ffmpeg")


def av_available() -> bool:
    return importlib.util.find_spec("av") is not None


def frame_backend() -> str | None:
    if ffmpeg_path() and shutil.which("ffprobe"):
        return "ffmpeg"
    return "pyav" if av_available() else None


def sample_times(duration: float, count: int) -> list[float]:
    """``count`` evenly spaced timestamps, centred in equal slices (never 0 or the very end)."""

    if duration <= 0 or count <= 0:
        return []
    step = duration / count
    return [round(step * (i + 0.5), 2) for i in range(count)]


def probe(path: Path) -> dict[str, Any]:
    """``{"duration": seconds, "has_video": bool}``."""

    backend = frame_backend()
    if backend == "ffmpeg":
        cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type", "-of", "json", str(path)]
        try:
            out = subprocess.run(cmd, capture_output=True, check=True, timeout=60).stdout
            info = json.loads(out or b"{}")
        except (subprocess.SubprocessError, ValueError, OSError) as exc:
            raise MediaError("ffprobe could not read the media file.", "invalid_media") from exc
        streams = info.get("streams") or []
        return {"duration": float((info.get("format") or {}).get("duration") or 0),
                "has_video": any(s.get("codec_type") == "video" for s in streams)}
    if backend == "pyav":
        import av

        try:
            with av.open(str(path)) as container:
                duration = float(container.duration or 0) / 1_000_000
                return {"duration": duration, "has_video": bool(container.streams.video)}
        except Exception as exc:  # PyAV raises its own FFmpegError hierarchy
            raise MediaError("PyAV could not read the media file.", "invalid_media") from exc
    raise MediaUnavailable("ffmpeg on PATH or PyAV to read video", 'pip install -e ".[media]" (or install ffmpeg)')


def extract_frames(path: Path, times: list[float]) -> list[tuple[float, bytes]]:
    """JPEG bytes per timestamp (frames that fail to decode are skipped)."""

    backend = frame_backend()
    if backend is None:
        raise MediaUnavailable("ffmpeg on PATH or PyAV for keyframes", 'pip install -e ".[media]" (or install ffmpeg)')
    return _ffmpeg_frames(path, times) if backend == "ffmpeg" else _pyav_frames(path, times)


def _ffmpeg_frames(path: Path, times: list[float]) -> list[tuple[float, bytes]]:
    frames: list[tuple[float, bytes]] = []
    for t in times:
        cmd = ["ffmpeg", "-nostdin", "-v", "error", "-ss", f"{t:.2f}", "-i", str(path), "-frames:v", "1",
               "-vf", f"scale='min({FRAME_WIDTH},iw)':-2", "-f", "image2", "-c:v", "mjpeg", "pipe:1"]
        try:
            data = subprocess.run(cmd, capture_output=True, check=True, timeout=60).stdout
        except (subprocess.SubprocessError, OSError):
            continue
        if data:
            frames.append((t, data))
    return frames


def _pyav_frames(path: Path, times: list[float]) -> list[tuple[float, bytes]]:
    import av

    frames: list[tuple[float, bytes]] = []
    with av.open(str(path)) as container:
        stream = container.streams.video[0]
        for t in times:
            try:
                container.seek(int(t * 1_000_000), any_frame=False, backward=True)
                frame: Any = next(container.decode(stream))  # PyAV frames are untyped
                image = frame.to_image()
                image.thumbnail((FRAME_WIDTH, FRAME_WIDTH))
                buf = io.BytesIO()
                image.convert("RGB").save(buf, format="JPEG", quality=80)
                frames.append((t, buf.getvalue()))
            except Exception:  # noqa: BLE001 - one bad frame must not sink the video
                continue
    return frames


def extract_audio(path: Path, out_dir: Path) -> Path:
    """16 kHz mono MP3 for API transcription (smaller upload); returns ``path`` when ffmpeg is missing."""

    if not ffmpeg_path():
        return path
    target = out_dir / "audio.mp3"
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(path), "-vn", "-ac", "1", "-ar", "16000",
           "-b:a", "48k", str(target)]
    try:
        subprocess.run(cmd, capture_output=True, check=True, timeout=1800)
    except (subprocess.SubprocessError, OSError):
        return path
    return target if target.exists() and target.stat().st_size else path
