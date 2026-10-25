"""Artifact store for exports: S3/MinIO when ``MDRA_S3_BUCKET`` is set, else a local folder.

Nothing is written unless an export is explicitly saved (API ``POST /control/runs/{id}/artifacts``
or the dashboard's "Save to artifact store" button), so default behaviour is unchanged.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from deep_research.scale.config import ScaleSettings

logger = logging.getLogger(__name__)

_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
MAX_ARTIFACT_BYTES = 50 * 1024 * 1024


def safe_key(key: str) -> str:
    """Validate a relative ``a/b/c.ext`` key (no absolute paths, ``..``, backslashes or odd characters)."""
    parts = key.replace("\\", "/").split("/")
    if not parts or len(parts) > 8 or any(not _SEGMENT.match(p) or p in (".", "..") or ".." in p for p in parts):
        raise ValueError("invalid artifact key")
    return "/".join(parts)


class ArtifactStore(Protocol):
    name: str

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> dict[str, Any]: ...

    def get(self, key: str) -> bytes: ...

    def list(self, prefix: str = "") -> list[dict[str, Any]]: ...


class LocalArtifactStore:
    """Files under ``root`` (default ``data/artifacts``; ``data/`` is git-ignored). Windows-safe paths."""

    name = "local"

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def _path(self, key: str) -> Path:
        path = self.root.joinpath(*safe_key(key).split("/"))
        root = self.root.resolve()
        if root not in path.resolve().parents:
            raise ValueError("invalid artifact key")
        return path

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> dict[str, Any]:
        if len(data) > MAX_ARTIFACT_BYTES:
            raise ValueError("artifact too large")
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".part")
        tmp.write_bytes(data)
        tmp.replace(path)
        return {"key": safe_key(key), "size": len(data), "store": self.name, "content_type": content_type}

    def get(self, key: str) -> bytes:
        path = self._path(key)
        if not path.is_file():
            raise KeyError(key)
        return path.read_bytes()

    def list(self, prefix: str = "") -> list[dict[str, Any]]:
        if not self.root.is_dir():
            return []
        out = []
        for path in sorted(self.root.rglob("*")):
            if path.is_file() and not path.name.endswith(".part"):
                key = path.relative_to(self.root).as_posix()
                if key.startswith(prefix):
                    stat = path.stat()
                    out.append({"key": key, "size": stat.st_size, "store": self.name,
                                "modified": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat()})
        return out


class S3ArtifactStore:
    """S3-compatible bucket (MinIO locally). Credentials come from ``MDRA_S3_*`` and are never logged."""

    name = "s3"

    def __init__(self, bucket: str, *, prefix: str = "", client: Any = None, create_bucket: bool = True,
                 fallback: LocalArtifactStore | None = None) -> None:
        self.bucket = bucket
        self.fallback = fallback
        self.prefix = safe_key(prefix.strip("/")) + "/" if prefix.strip("/") else ""
        self.client = client
        self._create = create_bucket
        self._checked = False

    @classmethod
    def from_settings(cls, scale: ScaleSettings) -> S3ArtifactStore:
        import boto3
        from botocore.config import Config

        client = boto3.client(
            "s3",
            endpoint_url=scale.s3_endpoint or None,
            region_name=scale.s3_region,
            aws_access_key_id=scale.secret("s3_access_key") or None,
            aws_secret_access_key=scale.secret("s3_secret_key") or None,
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"},
                          connect_timeout=3, read_timeout=30, retries={"max_attempts": 2}),
        )
        return cls(scale.s3_bucket, prefix=scale.s3_prefix, client=client,
                   fallback=LocalArtifactStore(Path(scale.artifacts_dir)))

    def _degrade(self, op: str, exc: Exception) -> LocalArtifactStore:
        """Log (exception type only - messages can embed endpoint credentials) and return the local fallback."""
        if self.fallback is None:
            raise exc
        logger.warning("S3 artifact %s failed (%s); falling back to local store at %s",
                       op, type(exc).__name__, self.fallback.root)
        return self.fallback

    def _ensure_bucket(self) -> None:
        if self._checked:
            return
        try:
            self.client.head_bucket(Bucket=self.bucket)
        except Exception as exc:
            code = str(getattr(exc, "response", {}).get("Error", {}).get("Code", ""))
            if not (self._create and code in ("404", "NoSuchBucket", "NotFound")):
                raise
            self.client.create_bucket(Bucket=self.bucket)
        self._checked = True

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> dict[str, Any]:
        if len(data) > MAX_ARTIFACT_BYTES:
            raise ValueError("artifact too large")
        k = safe_key(key)
        try:
            self._ensure_bucket()
            self.client.put_object(Bucket=self.bucket, Key=self.prefix + k, Body=data, ContentType=content_type)
        except Exception as exc:
            out = self._degrade("put", exc).put(k, data, content_type)
            return {**out, "fallback": True}
        return {"key": k, "size": len(data), "store": self.name, "content_type": content_type}

    def get(self, key: str) -> bytes:
        try:
            obj = self.client.get_object(Bucket=self.bucket, Key=self.prefix + safe_key(key))
        except Exception as exc:
            if str(getattr(exc, "response", {}).get("Error", {}).get("Code", "")) in ("NoSuchKey", "404"):
                if self.fallback is None:
                    raise KeyError(key) from None
                return self.fallback.get(key)
            return self._degrade("get", exc).get(key)
        return bytes(obj["Body"].read())

    def list(self, prefix: str = "") -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        token: str | None = None
        while len(out) < 10_000:
            kw: dict[str, Any] = {"Bucket": self.bucket, "Prefix": self.prefix + prefix}
            if token:
                kw["ContinuationToken"] = token
            page = self.client.list_objects_v2(**kw)
            for item in page.get("Contents", []):
                modified = item.get("LastModified")
                out.append({"key": str(item["Key"])[len(self.prefix):], "size": int(item.get("Size", 0)), "store": self.name,
                            "modified": modified.isoformat() if hasattr(modified, "isoformat") else str(modified or "")})
            if not page.get("IsTruncated"):
                break
            token = page.get("NextContinuationToken")
        return out

    def ping(self) -> None:
        self.client.head_bucket(Bucket=self.bucket)


def build_artifact_store(scale: ScaleSettings | None = None) -> ArtifactStore:
    s = scale or ScaleSettings()
    if s.s3_bucket:
        try:
            return S3ArtifactStore.from_settings(s)
        except ImportError:
            logger.warning("MDRA_S3_BUCKET is set but boto3 is not installed (pip install '.[scale]'); "
                           "using local artifact store at %s", s.artifacts_dir)
    return LocalArtifactStore(Path(s.artifacts_dir))


def save_export(store: ArtifactStore, run_id: str, report: dict[str, Any], fmt: str) -> dict[str, Any]:
    """Render ``report`` as ``fmt`` and store it at ``runs/<run_id>/<filename>``."""
    from deep_research.export import FORMATS, export_filename, export_report

    data = export_report(report, fmt)
    rid = re.sub(r"[^A-Za-z0-9_-]", "", run_id)[:64] or "run"
    name = re.sub(r"\.{2,}", ".", re.sub(r"[^A-Za-z0-9._-]", "_", export_filename(report, fmt, rid)))[:120].lstrip("._")
    name = name or f"report.{fmt}"
    return store.put(f"runs/{rid}/{name}", data, FORMATS[fmt][0])
