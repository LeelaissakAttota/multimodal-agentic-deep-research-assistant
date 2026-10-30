"""Phase 6 edge cases (Testing Bot): defaults, secret hygiene, clips, A/B, artifacts, queue."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import warnings
from pathlib import Path
from typing import Any

import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from deep_research.control.api import build_control_app
from deep_research.control.clips import ClipStore, clean_page_url
from deep_research.control.service import DeepResearchControl
from deep_research.core.config import Settings
from deep_research.scale.config import ScaleSettings, backend_status
from deep_research.scale.experiments import Experiment, Variant, load_experiment
from deep_research.scale.queue import build_queue
from deep_research.scale.storage import LocalArtifactStore, build_artifact_store, safe_key
from deep_research.scale.tracing import safe_attributes
from tests.subprocess_env import minimal_env

ROOT = Path(__file__).resolve().parents[2]
TOKEN = "q" * 32
H = {"Authorization": f"Bearer {TOKEN}"}
SECRET = "Sup3rS3cretPw"


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import os

    for k in list(os.environ):
        if k.startswith("MDRA_"):
            monkeypatch.delenv(k)
    monkeypatch.setenv("DEEP_RESEARCH_ENV_FILE", str(tmp_path / "none.env"))


def _scale(**kw: Any) -> ScaleSettings:
    return ScaleSettings(_env_file=None, **kw)  # type: ignore[call-arg]


# ---- defaults ----------------------------------------------------------------------------------

def test_defaults_everything_off(clean_env: None) -> None:
    s = _scale()
    assert build_queue(s) is None
    assert isinstance(build_artifact_store(s), LocalArtifactStore)
    assert s.otel_exporter == "off" and s.ab_config == "" and s.neo4j_uri == ""
    assert {r["state"] for r in backend_status(s)} == {"off"}


def test_default_import_needs_no_optional_backends() -> None:
    code = (
        "import sys\n"
        "for m in ('redis','rq','psycopg','psycopg2','boto3','botocore','neo4j',"
        "          'opentelemetry.sdk','opentelemetry.exporter'):\n"
        "    sys.modules[m] = None\n"
        "import os\n"
        "[os.environ.pop(k) for k in list(os.environ) if k.startswith('MDRA_')]\n"
        "from deep_research.scale.config import ScaleSettings, backend_status\n"
        "from deep_research.scale.queue import build_queue\n"
        "from deep_research.scale.storage import build_artifact_store\n"
        "from deep_research.scale.tracing import configure_tracing, span\n"
        "from deep_research.scale.graph import build_graph\n"
        "import deep_research.control.api, deep_research.api.research_service\n"
        "s = ScaleSettings(_env_file=None)\n"
        "assert build_queue(s) is None\n"
        "assert type(build_artifact_store(s)).__name__ == 'LocalArtifactStore'\n"
        "assert configure_tracing(force=True) is False\n"
        "with span('x', a=1):\n    pass\n"
        "g = build_graph([])\n"
        "print('OK')\n"
    )
    env = minimal_env({"PYTHONPATH": os.pathsep.join([str(ROOT / "src"), str(ROOT)]),
                       "DEEP_RESEARCH_ENV_FILE": str(ROOT / "nonexistent.env")})
    proc = subprocess.run([sys.executable, "-P", "-c", code], capture_output=True, text=True, env=env, cwd=ROOT, timeout=120)
    assert proc.returncode == 0 and "OK" in proc.stdout, proc.stderr[-2000:]


# ---- secrets -----------------------------------------------------------------------------------

@pytest.mark.parametrize("value", [
    f"postgresql://user:{SECRET}@db:5432/x", f"https://u:{SECRET}@h/p",
    f"redis://:{SECRET}@redis:6379/0", f"rediss://{SECRET}@redis:6380", f"amqp+ssl://u:{SECRET}@mq/v",
    f"bolt://neo4j:{SECRET}@neo:7687", f"mongodb+srv://u:p@{SECRET}@c/x", f"postgresql://:{SECRET}@db/x?sslmode=require",
    f"see redis://:{SECRET}@a:1/0 and https://u:{SECRET}@b/c",
])
def test_span_attributes_redact_url_passwords(value: str) -> None:
    out = safe_attributes({"mdra.url": value, "items": [value]})
    assert SECRET not in json.dumps(out)


def test_span_attributes_drop_sensitive_keys() -> None:
    out = safe_attributes({"api_key": SECRET, "token": SECRET, "password": SECRET, "authorization": SECRET,
                           "s3_secret_key": SECRET, "ok": 1})
    assert SECRET not in json.dumps(out) and out.get("ok") == 1


def test_span_attribute_values_truncated() -> None:
    out = safe_attributes({"v": "a" * 10_000})
    assert len(out["v"]) <= 256


def test_backend_status_never_leaks_secrets(clean_env: None) -> None:
    s = _scale(redis_url=f"redis://:{SECRET}@r:6379/0", database_url=f"postgresql://u:{SECRET}@db/x",
               s3_bucket="b", s3_access_key="AKIA" + SECRET, s3_secret_key=SECRET,
               neo4j_uri=f"bolt://n:{SECRET}@neo:7687", neo4j_password=SECRET, otel_exporter="otlp",
               otel_endpoint=f"https://u:{SECRET}@otel:4318/v1/traces")
    assert SECRET not in json.dumps(backend_status(s))


def test_scale_settings_repr_hides_url_credentials() -> None:
    s = _scale(neo4j_uri=f"bolt://n:{SECRET}@neo:7687", otel_endpoint=f"https://u:{SECRET}@otel:4318/v1/traces")
    assert SECRET not in repr(s) and SECRET not in str(s)
    assert "bolt://[REDACTED]@neo:7687" in repr(s)
    assert s.neo4j_uri.endswith("@neo:7687")  # real value still usable by the driver


def test_invalid_ab_config_status_no_secret(clean_env: None) -> None:
    s = _scale(ab_config=json.dumps({"name": "x", "variants": [{"name": "a", "overrides": {"openrouter_api_key": SECRET}}]}))
    row = [r for r in backend_status(s) if r["name"] == "A/B experiments"][0]
    assert row["state"] == "invalid" and SECRET not in row["detail"]


# ---- A/B ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("key", ["openrouter_api_key", "tavily_api_key", "database_url", "control_token",
                                 "research_mode", "blocked_domains", "__class__"])
def test_ab_cannot_override_non_allowlisted(key: str) -> None:
    cfg = {"name": "e", "variants": [{"name": "a", "overrides": {key: "x"}}]}
    with pytest.raises(ValueError):
        load_experiment(json.dumps(cfg))


@pytest.mark.parametrize("raw", ["", "{", "[]", "null", '{"name":"e"}', '{"name":"e","variants":[]}',
                                 '{"name":"e","variants":[{"name":"a","weight":-1}]}',
                                 '{"name":"e","variants":[{"name":"a","weight":0}]}',
                                 '{"name":"e","variants":[{"name":"a","overrides":[1]}]}',
                                 '{"name":"e","variants":[{"name":"a","overrides":{"agent_max_plan_tasks":"lots"}}]}'])
def test_ab_malformed_config_raises_value_error(raw: str) -> None:
    with pytest.raises(ValueError):
        load_experiment(raw)


def test_ab_assignment_deterministic_and_balanced() -> None:
    exp = Experiment("e", (Variant("a", 1.0), Variant("b", 3.0)))
    assert all(exp.assign(f"r{i}") == exp.assign(f"r{i}") for i in range(50))
    counts = {"a": 0, "b": 0}
    for i in range(4000):
        counts[exp.assign(f"run-{i}").name] += 1
    assert 800 < counts["a"] < 1200


# ---- artifacts ---------------------------------------------------------------------------------

@pytest.mark.parametrize("key", ["../x", "a/../../x", "/etc/passwd", "a\\..\\b", "C:/x", "a//b", "", ".hidden",
                                 "a/./b", "x\x00y", "a/" * 9 + "b", "%2e%2e/x", "~/x"])
def test_artifact_key_traversal_rejected(key: str, tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path / "art")
    with pytest.raises(ValueError):
        store.put(key, b"x")
    assert not (tmp_path / "x").exists()


def test_local_artifact_roundtrip(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path / "art")
    store.put("runs/r1/report.md", b"hi")
    assert store.get("runs/r1/report.md") == b"hi" and safe_key("a\\b") == "a/b"
    with pytest.raises(KeyError):
        store.get("runs/r1/missing.md")


def test_s3_configured_without_boto3_falls_back_to_local(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "boto3", None)
    store = build_artifact_store(_scale(s3_bucket="b", artifacts_dir=str(tmp_path / "a")))
    assert isinstance(store, LocalArtifactStore)


# ---- queue -------------------------------------------------------------------------------------

def test_queue_redis_url_without_deps_raises_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("deep_research.scale.config.has_module", lambda n: False)
    with pytest.raises(RuntimeError) as exc:
        build_queue(_scale(redis_url=f"redis://:{SECRET}@r:6379/0"))
    assert SECRET not in str(exc.value)


# ---- clips -------------------------------------------------------------------------------------

def _client(tmp_path: Path, host: tuple[str, int] = ("127.0.0.1", 5000)) -> TestClient:
    control = DeepResearchControl(Settings(_env_file=None, research_mode="offline"), config_dir=tmp_path / "cfg",
                                  assistant_db=tmp_path / "a.db")
    clips = ClipStore(tmp_path / "cfg" / "clips.jsonl")
    return TestClient(build_control_app(control.manager, control.secrets, admin_token=TOKEN, title="t", clips=clips),
                      client=host)


@pytest.mark.parametrize("host", ["10.0.0.5", "192.168.1.2", "8.8.8.8", "127.0.0.2.evil", "::ffff:8.8.8.8"])
def test_clips_reject_non_loopback(tmp_path: Path, host: str) -> None:
    r = _client(tmp_path, (host, 1)).post("/control/clips", json={"url": "https://example.com/a"}, headers=H)
    assert r.status_code == 403


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong" + "x" * 30}, {"Authorization": TOKEN}])
def test_clips_require_token(tmp_path: Path, headers: dict[str, str]) -> None:
    client = _client(tmp_path)
    assert client.post("/control/clips", json={"url": "https://example.com/a"}, headers=headers).status_code in (401, 403)
    assert client.get("/control/clips", headers=headers).status_code in (401, 403)


@pytest.mark.parametrize("url", ["javascript:alert(1)//xxxx", "data:text/html,<script>x</script>", "file:///etc/passwd",
                                 "ftp://example.com/x", "http:///nohost", "https://exa mple.com/x",
                                 "chrome://settings/abc"])
def test_clips_reject_bad_schemes(tmp_path: Path, url: str) -> None:
    assert _client(tmp_path).post("/control/clips", json={"url": url}, headers=H).status_code == 422


def test_clips_size_limits_and_credentials_stripped(tmp_path: Path) -> None:
    client = _client(tmp_path)
    assert client.post("/control/clips", json={"url": "https://e.com/" + "a" * 3000}, headers=H).status_code == 422
    assert client.post("/control/clips", json={"url": "https://e.com/a", "selection": "x" * 20_001},
                       headers=H).status_code == 422
    r = client.post("/control/clips", json={"url": f"https://u:{SECRET}@e.com/a#frag", "title": "<script>x</script>"},
                    headers=H)
    assert r.status_code == 201
    body = r.json()
    assert SECRET not in json.dumps(body) and body["url"] == "https://e.com/a"
    assert r.headers["content-type"].startswith("application/json")
    assert SECRET not in (tmp_path / "cfg" / "clips.jsonl").read_text()


def test_clean_page_url_ipv6_host_keeps_brackets() -> None:
    assert clean_page_url("http://[::1]:8080/x") == "http://[::1]:8080/x"
    assert clean_page_url("https://u:p@[2001:db8::1]/a?q=1#f") == "https://[2001:db8::1]/a?q=1"
    assert clean_page_url("http://example.com:81/x") == "http://example.com:81/x"


# ---- extension ---------------------------------------------------------------------------------

def test_extension_no_remote_code() -> None:
    ext = ROOT / "extensions" / "mdra-chrome"
    m = json.loads((ext / "manifest.json").read_text())
    assert "unsafe-eval" not in json.dumps(m) and "externally_connectable" not in m
    assert "web_accessible_resources" not in m
    for f in ext.glob("*"):
        t = f.read_text(encoding="utf-8")
        assert "eval(" not in t and "new Function" not in t and "https://" not in t.replace("https://", "", 0) or f.suffix == ".md"
        assert "<script src=\"http" not in t


def test_scale_settings_url_fields_never_leak_in_status_or_str() -> None:
    s = _scale(neo4j_uri=f"neo4j+s://:{SECRET}@neo:7687", otel_endpoint=f"http://{SECRET}@otel:4318/v1/traces",
               s3_endpoint=f"http://k:{SECRET}@minio:9000")
    text = repr(s) + str(s) + str(backend_status(s)) + f"{s!r}"
    assert SECRET not in text and "[REDACTED]" in repr(s)


def test_minimal_env_keeps_windows_essentials_and_drops_app_keys() -> None:
    fake = {"SystemRoot": r"C:\\Windows", "SystemDrive": "C:", "windir": r"C:\\Windows", "Path": r"C:\\Python",
            "PATHEXT": ".COM;.EXE", "ComSpec": "cmd.exe", "TEMP": "t", "TMP": "t", "USERPROFILE": "u",
            "MDRA_REDIS_URL": f"redis://:{SECRET}@r", "DEEP_RESEARCH_OPENROUTER_API_KEY": SECRET}
    env = minimal_env({"X": "1"}, source=fake)
    assert {"SystemRoot", "SystemDrive", "windir", "Path", "PATHEXT", "ComSpec", "TEMP", "TMP", "USERPROFILE"} <= set(env)
    assert env["X"] == "1" and SECRET not in str(env)
