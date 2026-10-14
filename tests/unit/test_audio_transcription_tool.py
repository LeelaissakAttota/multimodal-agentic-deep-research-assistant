"""Offline tests for AudioTranscriptionTool with fake gateways."""

from __future__ import annotations

import asyncio
import base64
from typing import Any

import pytest

from deep_research.errors.base import ModelError
from deep_research.models.model_gateway import ModelGateway, ModelRequest, ModelResponse
from deep_research.tools import audio_transcription_tool as mod
from deep_research.tools.audio_transcription_tool import AudioTranscriptionTool
from deep_research.tools.tool import ToolRequest

WAV = "data:audio/wav;base64," + base64.b64encode(b"RIFFfake").decode()


class FakeGateway(ModelGateway):
    def __init__(self, result: Any = "Hello world.") -> None:
        self.result = result
        self.calls: list[ModelRequest] = []

    async def generate(self, request: ModelRequest) -> ModelResponse:
        raise AssertionError("generate must not be used for audio")

    async def transcribe(self, request: ModelRequest) -> ModelResponse:
        self.calls.append(request)
        if isinstance(self.result, BaseException):
            raise self.result
        return ModelResponse(text=self.result, model_id="test/fake")

    async def health_check(self) -> bool:
        return True


class TextOnlyGateway(ModelGateway):
    async def generate(self, request: ModelRequest) -> ModelResponse:
        return ModelResponse(text="x", model_id="t")

    async def health_check(self) -> bool:
        return True


def run_tool(tool: AudioTranscriptionTool, **params: Any) -> Any:
    return asyncio.run(tool.execute(ToolRequest(tool_name="audio_transcription", parameters=params)))


def test_success_with_provenance() -> None:
    gw = FakeGateway()
    res = run_tool(AudioTranscriptionTool(gateway=gw), audio_data=WAV, source_title="clip")
    assert res.success and res.output["transcript"] == "Hello world."
    assert res.output["evidence_ids"] and res.output["source_ids"]
    assert gw.calls[0].audio_data == WAV
    assert gw.calls[0].parameters["required_capability"] == "audio_transcription"


@pytest.mark.parametrize(
    "params,kind",
    [
        ({}, "missing_audio_input"),
        ({"audio_data": 5}, "invalid_audio_type"),
        ({"audio_data": "  "}, "empty_audio_input"),
        ({"audio_data": "https://x/a.wav"}, "unsupported_audio_format"),
        ({"audio_data": "data:image/png;base64,AAAA"}, "unsupported_audio_format"),
    ],
)
def test_input_validation(params: dict[str, Any], kind: str) -> None:
    gw = FakeGateway()
    res = run_tool(AudioTranscriptionTool(gateway=gw), **params)
    assert not res.success and res.metadata["failure_kind"] == kind
    assert gw.calls == []


def test_size_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mod, "_MAX_AUDIO_BYTES_ENCODED", 16)
    gw = FakeGateway()
    res = run_tool(AudioTranscriptionTool(gateway=gw), audio_data=WAV + "A" * 32)
    assert res.metadata["failure_kind"] == "audio_too_large" and gw.calls == []


def test_default_size_limit_rejects_oversized() -> None:
    big = "data:audio/wav;base64," + "A" * (mod._MAX_AUDIO_BYTES_ENCODED + 4)
    res = run_tool(AudioTranscriptionTool(gateway=FakeGateway()), audio_data=big)
    assert res.metadata["failure_kind"] == "audio_too_large"


@pytest.mark.parametrize(
    "error,kind",
    [
        (ModelError("bad", error_code="no_free_stt_provider"), "model_error"),
        (TimeoutError(), "timeout"),
        (RuntimeError("boom"), "unexpected"),
    ],
)
def test_gateway_errors(error: BaseException, kind: str) -> None:
    res = run_tool(AudioTranscriptionTool(gateway=FakeGateway(error)), audio_data=WAV)
    assert not res.success and res.metadata["failure_kind"] == kind


def test_empty_transcript_not_fabricated() -> None:
    res = run_tool(AudioTranscriptionTool(gateway=FakeGateway("   ")), audio_data=WAV)
    assert res.metadata["failure_kind"] == "empty_response"


def test_text_only_gateway_fails_closed() -> None:
    res = run_tool(AudioTranscriptionTool(gateway=TextOnlyGateway()), audio_data=WAV)
    assert not res.success and res.metadata["error_code"] == "transcription_not_supported"


def test_no_gateway_degrades(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("MADRA_NVIDIA_API_KEY", "MADRA_HUGGINGFACE_API_KEY", "MADRA_OPENROUTER_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    tool = AudioTranscriptionTool()
    if tool._gateway is not None:
        pytest.skip("live STT key configured in environment")
    res = run_tool(tool, audio_data=WAV)
    assert res.metadata["failure_kind"] == "no_gateway"
    assert asyncio.run(tool.health_check()) is False


def test_definition() -> None:
    d = AudioTranscriptionTool(gateway=FakeGateway()).get_definition()
    assert d.identifier == "audio_transcription"
