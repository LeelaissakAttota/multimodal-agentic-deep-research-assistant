"""Offline tests for the free STT router, providers, guard and wiring."""

from __future__ import annotations

import asyncio
import base64
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from deep_research.core.config import Settings
from deep_research.core.free_model_guard import FreeModelGuard, ModelCapability
from deep_research.core.provider_wiring import build_provider_gateway
from deep_research.errors.base import ModelError
from deep_research.models.free_stt_router import (
    FreeSTTProviderRouter,
    HuggingFaceTranscriptionProvider,
    NVIDIATranscriptionProvider,
    STTProvider,
)
from deep_research.models.guarded_gateway import GuardedModelGateway
from deep_research.models.model_gateway import ModelRequest, ModelResponse

WAV = "data:audio/wav;base64," + base64.b64encode(b"RIFFfake").decode()
KEY = SecretStr("test-key")


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def client(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def nvidia(handler: Any, key: SecretStr | None = KEY) -> NVIDIATranscriptionProvider:
    return NVIDIATranscriptionProvider(api_key=key, endpoint="https://nvidia.test/v1", http_client=client(handler))


def hf(handler: Any, key: SecretStr | None = KEY, max_requests: int = 20) -> HuggingFaceTranscriptionProvider:
    return HuggingFaceTranscriptionProvider(api_key=key, http_client=client(handler), max_requests=max_requests)


def ok(text: str) -> Any:
    return lambda req: httpx.Response(200, json={"text": text})


def status(code: int) -> Any:
    return lambda req: httpx.Response(code, json={"error": "x"})


def req(audio: str | None = WAV) -> ModelRequest:
    return ModelRequest(prompt="t", audio_data=audio)


def test_primary_provider_success() -> None:
    router = FreeSTTProviderRouter([hf(ok("hf")), nvidia(ok("nv"))])
    assert [p.provider_id for p in router.providers] == ["nvidia", "huggingface"]
    resp = run(router.transcribe(req()))
    assert resp.text == "nv" and resp.model_id == "nvidia/parakeet-tdt-0.6b"


@pytest.mark.parametrize("code", [401, 402, 429, 500, 400])
def test_fallback_on_http_error(code: int) -> None:
    router = FreeSTTProviderRouter([nvidia(status(code)), hf(ok("fallback"))])
    assert run(router.transcribe(req())).text == "fallback"


def test_fallback_on_network_error() -> None:
    def boom(r: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=r)

    router = FreeSTTProviderRouter([nvidia(boom), hf(ok("fb"))])
    assert run(router.transcribe(req())).text == "fb"


def test_unavailable_provider_skipped_and_fail_closed() -> None:
    router = FreeSTTProviderRouter([nvidia(ok("x"), key=None), hf(status(503))])
    with pytest.raises(ModelError) as exc:
        run(router.transcribe(req()))
    assert exc.value.error_code == "no_free_stt_provider"
    outcomes = [a["outcome"] for a in exc.value.details["attempts"]]
    assert outcomes == ["skipped_unavailable", "failed"]
    assert "test-key" not in repr(exc.value.details)


@pytest.mark.parametrize("body", [{"nope": 1}, {"text": ""}, {"text": 5}])
def test_malformed_responses(body: dict[str, Any]) -> None:
    router = FreeSTTProviderRouter([hf(lambda r: httpx.Response(200, json=body))])
    with pytest.raises(ModelError):
        run(router.transcribe(req()))


@pytest.mark.parametrize("audio", [None, "", "https://x/a.wav", "data:audio/wav;base64,!!!"])
def test_invalid_audio_payload(audio: str | None) -> None:
    router = FreeSTTProviderRouter([hf(ok("x"))])
    with pytest.raises(ModelError) as exc:
        run(router.transcribe(req(audio)))
    assert exc.value.error_code == "invalid_stt_request"


def test_hf_budget_and_402_disable() -> None:
    p = hf(ok("a"), max_requests=1)
    run(p.transcribe(b"x", "wav"))
    assert not p.is_available()
    p2 = hf(status(402))
    with pytest.raises(ModelError):
        run(p2.transcribe(b"x", "wav"))
    assert not p2.is_available()


def test_base_provider_fails_closed_and_router_falls_back() -> None:
    class Bare(STTProvider):
        provider_id = "nvidia"
        model_id = "nvidia/parakeet-tdt-0.6b"

    with pytest.raises(ModelError) as exc:
        run(Bare().transcribe(b"x", "wav"))
    assert exc.value.error_code == "stt_provider_not_implemented"
    assert run(FreeSTTProviderRouter([Bare(), hf(ok("ok"))]).transcribe(req())).text == "ok"


def test_unapproved_provider_and_empty_router_rejected() -> None:
    class Rogue(STTProvider):
        provider_id = "paid"
        model_id = "whisper"

    with pytest.raises(ModelError):
        Rogue()
    with pytest.raises(ModelError):
        FreeSTTProviderRouter([])


def test_generate_not_supported_and_health() -> None:
    router = FreeSTTProviderRouter([hf(ok("x"))])
    with pytest.raises(ModelError):
        run(router.generate(ModelRequest(prompt="hi")))
    assert run(router.health_check()) is True


def test_guarded_gateway_enforces_capability() -> None:
    inner = FreeSTTProviderRouter([hf(ok("g"))])
    g = GuardedModelGateway(inner, model_id="free_stt_router", provider="free_stt_router",
                            capability=ModelCapability.AUDIO_TRANSCRIPTION, guard=FreeModelGuard())
    assert run(g.transcribe(req())).text == "g"
    with pytest.raises(ModelError):
        run(g.generate(ModelRequest(prompt="x")))
    bad = ModelRequest(prompt="t", audio_data=WAV, parameters={"required_capability": "vision"})
    with pytest.raises(ModelError):
        run(g.transcribe(bad))


def test_guard_rejects_paid_models() -> None:
    guard = FreeModelGuard()
    guard.check(provider="openrouter", model_id="x/y:free", capability=ModelCapability.TEXT_GENERATION)
    with pytest.raises(ModelError) as exc:
        guard.check(provider="openrouter", model_id="openai/gpt-4o", capability=ModelCapability.TEXT_GENERATION)
    assert exc.value.error_code == "paid_model_rejected"


def test_wiring_offline_without_keys() -> None:
    s = Settings(_env_file=None)
    assert build_provider_gateway(s, capability=ModelCapability.AUDIO_TRANSCRIPTION) is None
    assert build_provider_gateway(s) is None
    assert build_provider_gateway(s, capability=None) is None


def test_wiring_builds_guarded_stt_router() -> None:
    s = Settings(_env_file=None, huggingface_api_key=SecretStr("k"))
    g = build_provider_gateway(s, capability=ModelCapability.AUDIO_TRANSCRIPTION)
    assert isinstance(g, GuardedModelGateway)
    t = build_provider_gateway(Settings(_env_file=None, openrouter_api_key=SecretStr("k")))
    assert isinstance(t, GuardedModelGateway)


def test_model_response_type() -> None:
    assert isinstance(run(hf(ok("z")).transcribe(b"x", "mp3")), ModelResponse)
