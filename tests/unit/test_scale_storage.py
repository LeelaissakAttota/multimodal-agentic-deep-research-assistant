"""Artifact store: local-folder default, S3/MinIO via MDRA_S3_* (fake client), control API routes."""

from __future__ import annotations

import io
import os
import time
import warnings
from pathlib import Path
from typing import Any

import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from deep_research.control.api import build_control_app
from deep_research.control.runtime import RunStatus
from deep_research.control.service import DeepResearchControl
from deep_research.core.config import Settings
from deep_research.scale import backend_status
from deep_research.scale.config import ScaleSettings
from deep_research.scale.storage import LocalArtifactStore, S3ArtifactStore, build_artifact_store, safe_key, save_export

TOKEN = "s" * 32
H = {"Authorization": f"Bearer {TOKEN}"}
REPORT: dict[str, Any] = {"title": "Tea report", "content": "# Tea\n\nGreen tea [1].", "citations": [{"number": 1, "title": "S"}]}


class FakeS3:
    class Err(Exception):
        def __init__(self, code: str) -> None:
            super().__init__(code)
            self.response = {"Error": {"Code": code}}

    def __init__(self) -> None:
        self.buckets: dict[str, dict[str, bytes]] = {}
        self.calls: list[str] = []

    def head_bucket(self, Bucket: str) -> None:
        self.calls.append("head")
        if Bucket not in self.buckets:
            raise self.Err("404")

    def create_bucket(self, Bucket: str) -> None:
        self.calls.append("create")
        self.buckets[Bucket] = {}

    def put_object(self, Bucket: str, Key: str, Body: bytes, ContentType: str) -> None:
        self.buckets[Bucket][Key] = Body

    def get_object(self, Bucket: str, Key: str) -> dict[str, Any]:
        if Key not in self.buckets.get(Bucket, {}):
            raise self.Err("NoSuchKey")
        return {"Body": io.BytesIO(self.buckets[Bucket][Key])}

    def list_objects_v2(self, Bucket: str, Prefix: str, **kw: Any) -> dict[str, Any]:
        keys = sorted(k for k in self.buckets.get(Bucket, {}) if k.startswith(Prefix))
        return {"Contents": [{"Key": k, "Size": len(self.buckets[Bucket][k])} for k in keys], "IsTruncated": False}


def test_default_is_local_folder_and_nothing_written(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    store = build_artifact_store()
    assert isinstance(store, LocalArtifactStore) and store.root == Path("data") / "artifacts"
    assert backend_status()[2]["state"] == "off"
    assert not (tmp_path / "data").exists()


@pytest.mark.parametrize("bad", ["../x", "/etc/passwd", "a/../../b", "C:\\x\\y", "a//b", "", ".hidden", "a/b c"])
def test_unsafe_keys_rejected(bad: str, tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        LocalArtifactStore(tmp_path).put(bad, b"x")


def test_local_roundtrip_windows_style_key(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path / "art")
    assert safe_key("runs\\r1\\a.md") == "runs/r1/a.md"
    info = store.put("runs\\r1\\a.md", b"hello", "text/markdown")
    assert info == {"key": "runs/r1/a.md", "size": 5, "store": "local", "content_type": "text/markdown"}
    assert (tmp_path / "art" / "runs" / "r1" / "a.md").read_bytes() == b"hello"
    assert store.get("runs/r1/a.md") == b"hello"
    assert [x["key"] for x in store.list("runs/r1/")] == ["runs/r1/a.md"]
    with pytest.raises(KeyError):
        store.get("runs/r1/missing.md")


def test_s3_store_with_fake_client() -> None:
    fake = FakeS3()
    store = S3ArtifactStore("mdra", prefix="team", client=fake)
    store.put("runs/r1/a.md", b"hi")
    assert fake.calls == ["head", "create"] and fake.buckets["mdra"] == {"team/runs/r1/a.md": b"hi"}
    store.put("runs/r1/b.md", b"x")
    assert fake.calls == ["head", "create"]  # bucket checked once
    assert store.get("runs/r1/a.md") == b"hi"
    assert [x["key"] for x in store.list("runs/")] == ["runs/r1/a.md", "runs/r1/b.md"]
    with pytest.raises(KeyError):
        store.get("runs/nope.md")


def test_s3_selected_by_env_and_secrets_hidden(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("boto3")
    monkeypatch.setenv("MDRA_S3_BUCKET", "mdra-art")
    monkeypatch.setenv("MDRA_S3_ENDPOINT", "http://127.0.0.1:9")
    monkeypatch.setenv("MDRA_S3_ACCESS_KEY", "AKIAEXAMPLEKEY")
    monkeypatch.setenv("MDRA_S3_SECRET_KEY", "verysecretvalue123")
    store = build_artifact_store()
    assert isinstance(store, S3ArtifactStore) and store.bucket == "mdra-art" and store.prefix == "mdra/"
    text = repr(ScaleSettings()) + str(backend_status())
    assert "verysecretvalue123" not in text and "AKIAEXAMPLEKEY" not in text


def test_save_export_key() -> None:
    fake = FakeS3()
    info = save_export(S3ArtifactStore("b", client=fake), "run/../1", REPORT, "md")
    assert info["key"].startswith("runs/run1/") and info["key"].endswith(".md")


def _client(tmp_path: Path, store: Any) -> tuple[TestClient, DeepResearchControl]:
    control = DeepResearchControl(Settings(_env_file=None, research_mode="offline"), config_dir=tmp_path / "cfg",
                                  assistant_db=tmp_path / "a.db")
    return TestClient(build_control_app(control.manager, control.secrets, admin_token=TOKEN, title="t", artifacts=store)), control


def test_api_save_list_get(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path / "art")
    client, control = _client(tmp_path, store)
    assert client.post("/control/runs/r1/artifacts", json={}, headers=H).status_code == 404
    saved = client.post("/control/runs/r1/artifacts?format=md", json={"report": REPORT}, headers=H)
    assert saved.status_code == 201 and saved.json()["store"] == "local"
    key = saved.json()["key"]
    assert [a["key"] for a in client.get("/control/artifacts?run_id=r1", headers=H).json()] == [key]
    got = client.get(f"/control/artifacts/{key}", headers=H)
    assert got.status_code == 200 and b"Green tea" in got.content
    assert client.get("/control/artifacts/runs/x/../../secret", headers=H).status_code == 404
    assert client.get("/control/artifacts", headers={}).status_code == 401
    assert client.get("/control/artifacts?run_id=../x", headers=H).status_code == 422
    run = control.manager.create({"objective": "What is green tea?"})
    for _ in range(300):
        if run.status == RunStatus.COMPLETED:
            break
        time.sleep(0.05)
    assert client.post(f"/control/runs/{run.run_id}/artifacts?format=json", json={}, headers=H).status_code == 201
    assert any(e["event"] == "artifact_saved" for e in control.manager.audit.tail(20))


def test_api_store_unavailable_is_503(tmp_path: Path) -> None:
    class Broken:
        name = "s3"

        def put(self, *a: Any, **k: Any) -> Any:
            raise ConnectionError("http://user:pw@minio:9000 refused")

        def list(self, prefix: str = "") -> Any:
            raise ConnectionError("x")

        def get(self, key: str) -> Any:
            raise ConnectionError("x")

    client, _ = _client(tmp_path, Broken())
    resp = client.post("/control/runs/r1/artifacts", json={"report": REPORT}, headers=H)
    assert resp.status_code == 503 and "pw" not in resp.text and "minio" not in resp.text
    assert client.get("/control/artifacts", headers=H).status_code == 503


@pytest.mark.skipif(not os.environ.get("MDRA_TEST_S3_BUCKET"), reason="set MDRA_TEST_S3_* to test real MinIO/S3")
def test_real_s3_roundtrip() -> None:
    scale = ScaleSettings(s3_bucket=os.environ["MDRA_TEST_S3_BUCKET"], s3_endpoint=os.environ.get("MDRA_TEST_S3_ENDPOINT", ""),
                          s3_access_key=os.environ.get("MDRA_TEST_S3_ACCESS_KEY"), s3_secret_key=os.environ.get("MDRA_TEST_S3_SECRET_KEY"))
    store = S3ArtifactStore.from_settings(scale)
    store.put("tests/hello.txt", b"hello")
    assert store.get("tests/hello.txt") == b"hello"


def test_dashboard_client_methods(tmp_path: Path) -> None:
    import httpx

    from deep_research.dashboard.core import ControlClient

    tc, _ = _client(tmp_path, LocalArtifactStore(tmp_path / "art"))

    def handler(request: httpx.Request) -> httpx.Response:
        resp = tc.request(request.method, request.url.path, params=request.url.params,
                          headers=dict(request.headers), content=request.content)
        return httpx.Response(resp.status_code, content=resp.content, headers=resp.headers)

    api = ControlClient("http://test", TOKEN, transport=httpx.MockTransport(handler))
    info = api.save_artifact("r9", "md", REPORT)
    assert info["key"].startswith("runs/r9/") and [a["key"] for a in api.list_artifacts("r9")] == [info["key"]]
