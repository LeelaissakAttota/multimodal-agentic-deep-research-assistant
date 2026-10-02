"""AudioTranscriptionTool - audio-to-text research tool (G-14).

Architecture fit
----------------
* Implements the ``Tool`` ABC from ``tools/tool.py``.
* Identifier: ``"audio_transcription"`` (canonical routing contract).
* Capability: ``ToolCapability.TRANSCRIBE`` on ``Modality.AUDIO``.
* Routes transcription requests through the existing ``ModelGateway``
  abstraction (``GuardedModelGateway`` → ``FreeModelGuard`` → provider).
* Explicitly requires ``ModelCapability.AUDIO_TRANSCRIPTION`` - models
  without that capability are rejected before the provider is contacted.
* No arbitrary network access; audio input is validated locally before
  any model call is made.
* Provenance is preserved using the existing ``Evidence`` and ``Source``
  domain models; no metadata is fabricated.

Audio input
-----------
Audio input is passed through ``ToolRequest.parameters`` as:

``audio_data`` (required)
    A ``data:<media-type>;base64,<data>`` URL string containing the audio
    payload.  The tool validates the MIME type and base64 encoding but does
    NOT fetch remote URLs.  Only the MIME types listed in
    ``_ALLOWED_AUDIO_PREFIXES`` are accepted.

``prompt`` (optional)
    A natural-language transcription hint passed to the model along with the
    audio.  Defaults to ``"Transcribe the following audio accurately."``.

``source_title`` (optional)
    A caller-supplied human-readable label for the audio source.  Used only
    in provenance metadata; never fetched.

Supported audio MIME types
--------------------------
* ``audio/wav``
* ``audio/wave``
* ``audio/x-wav``
* ``audio/mp3``
* ``audio/mpeg``
* ``audio/ogg``
* ``audio/webm``
* ``audio/flac``
* ``audio/mp4``
* ``audio/x-m4a``

Size limit
----------
The encoded ``audio_data`` string must not exceed ``_MAX_AUDIO_BYTES_ENCODED``
(10 MiB as base64 characters).  This is a defensive upper bound; real
transcription APIs impose their own limits.

Security boundaries
-------------------
* No HTTP requests inside this file.
* Validation rejects empty, oversized, or malformed/unsupported audio data
  before the model gateway is contacted.
* API keys never appear in error messages (the guarded gateway enforces this).
* ``ModelCapability.AUDIO_TRANSCRIPTION`` is checked by the guard before any
  provider call; policy denial is converted to a structured
  ``ToolResult(success=False)``.

Provider contract
-----------------
When a gateway is explicitly injected, the tool uses that gateway directly.
When no gateway is injected (the common case), the tool attempts to auto-
configure a ``FreeSTTProviderRouter`` from application settings.  The router
selects the first usable approved free STT provider (NVIDIA, Hugging Face,
or OpenRouter if a verified free model exists) and fails closed when none
are available - no paid provider is ever used automatically.

The approved free STT provider/router implements the dedicated
``POST /audio/transcriptions`` endpoint with the contract:
    {
        "model": "<model-id>",
        "input_audio": {
            "data": "<raw-base64>",   # raw base64, no data URL header
            "format": "<format>"      # e.g. "wav", "mp3"
        }
    }
and expects a ``{"text": "..."}`` JSON response.

Audio input is still passed as a ``data:<audio-mime>;base64,<data>`` URL;
the router extracts the raw base64 payload and format identifier before
calling the provider.

"""

from __future__ import annotations

import base64
import time
from typing import Any, Optional

from pydantic import Field

from deep_research.core.free_model_guard import ModelCapability
from deep_research.domain.modality import Modality
from deep_research.errors.base import ModelError
from deep_research.evidence.evidence import Evidence
from deep_research.evidence.source import Source
from deep_research.models.model_gateway import ModelGateway, ModelRequest
from deep_research.tools.definition import (
    ToolCapability,
    ToolDefinition,
    ToolInput,
    ToolOutput,
)
from deep_research.tools.tool import Tool, ToolRequest, ToolResult

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Canonical tool identifier used in routing contracts and registry lookups.
AUDIO_TRANSCRIPTION_TOOL_ID: str = "audio_transcription"

#: Maximum encoded audio size accepted (10 MiB as base64 characters).
#: Defensive upper bound; real transcription APIs impose their own limits.
_MAX_AUDIO_BYTES_ENCODED: int = 10 * 1024 * 1024  # 10 MiB in base64 chars

#: Recognised data-URL MIME type prefixes for audio.
_ALLOWED_AUDIO_PREFIXES: tuple[str, ...] = (
    "data:audio/wav;base64,",
    "data:audio/wave;base64,",
    "data:audio/x-wav;base64,",
    "data:audio/mp3;base64,",
    "data:audio/mpeg;base64,",
    "data:audio/ogg;base64,",
    "data:audio/webm;base64,",
    "data:audio/flac;base64,",
    "data:audio/mp4;base64,",
    "data:audio/x-m4a;base64,",
)

#: Default transcription prompt.
_DEFAULT_PROMPT: str = "Transcribe the following audio accurately."


# ---------------------------------------------------------------------------
# Tool input / output schemas
# ---------------------------------------------------------------------------


class AudioTranscriptionInput(ToolInput):
    """Input schema for AudioTranscriptionTool."""

    audio_data: str = Field(
        ...,
        description=(
            "Audio as a data URL: 'data:<audio-mime>;base64,<data>'. "
            "Remote URLs are not accepted. "
            "Supported MIME types: audio/wav, audio/mp3, audio/mpeg, "
            "audio/ogg, audio/webm, audio/flac, audio/mp4, audio/x-m4a."
        ),
    )
    prompt: str = Field(
        default=_DEFAULT_PROMPT,
        description="Natural-language transcription hint for the model.",
    )
    source_title: Optional[str] = Field(
        default=None,
        description="Optional human-readable label for the audio source (provenance only).",
    )


class AudioTranscriptionOutput(ToolOutput):
    """Output schema for AudioTranscriptionTool."""

    transcript: str = Field(..., description="Transcription text produced by the model.")
    model_id: str = Field(..., description="Model identifier that produced the transcription.")


# ---------------------------------------------------------------------------
# AudioTranscriptionTool
# ---------------------------------------------------------------------------


class AudioTranscriptionTool(Tool):
    """Audio-to-text research tool backed by an injected ``ModelGateway``.

    Constructor args
    ----------------
    gateway : ModelGateway | None
        The model gateway to use for transcription requests.  Must be
        configured with ``ModelCapability.AUDIO_TRANSCRIPTION`` enforcement
        (e.g. a ``GuardedModelGateway`` with
        ``capability=ModelCapability.AUDIO_TRANSCRIPTION``).

        When ``None`` the tool returns a structured ``success=False`` result
        (graceful degradation) - this supports environments where no
        transcription provider is configured.

    The no-arg constructor (required by ``ToolRegistry.register``) sets
    ``gateway=None`` which activates the graceful-degradation path.

    Usage in tests
    --------------
    Inject a fake ``ModelGateway`` for deterministic offline testing::

        class FakeAudioGateway(ModelGateway):
            async def generate(self, request):
                return ModelResponse(text="Hello world.", model_id="test/fake")
            async def health_check(self): return True

        tool = AudioTranscriptionTool(gateway=FakeAudioGateway())
    """

    def __init__(self, gateway: ModelGateway | None = None) -> None:
        if gateway is None:
            # Try to build a live gateway from settings; if none is available,
            # stay with None (graceful degradation).
            from deep_research.core.config import settings
            from deep_research.core.provider_wiring import build_provider_gateway
            gateway = build_provider_gateway(settings, capability=ModelCapability.AUDIO_TRANSCRIPTION)
        self._gateway = gateway

    # ------------------------------------------------------------------
    # Tool ABC interface
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        return AUDIO_TRANSCRIPTION_TOOL_ID

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            identifier=AUDIO_TRANSCRIPTION_TOOL_ID,
            name="Audio Transcription Tool",
            description=(
                "Accepts a base64-encoded audio data URL and uses a "
                "transcription-capable model to produce an accurate text transcript. "
                "Requires ModelCapability.AUDIO_TRANSCRIPTION."
            ),
            modality=Modality.AUDIO,
            capabilities=[ToolCapability.TRANSCRIBE],
            input_schema=AudioTranscriptionInput,
            output_schema=AudioTranscriptionOutput,
            cost_class="FREE",
            network_required=True,
            auth_required=True,
            reliability="MEDIUM",
            metadata={"required_model_capability": ModelCapability.AUDIO_TRANSCRIPTION.value},
        )

    async def health_check(self) -> bool:
        """Healthy only when a gateway is configured and reports healthy."""
        if self._gateway is None:
            return False
        try:
            return await self._gateway.health_check()
        except Exception:
            return False

    async def execute(self, request: ToolRequest) -> ToolResult:
        """Execute an audio transcription request.

        Parameters are read from ``request.parameters``:
        - ``audio_data`` (str, required): data URL audio payload.
        - ``prompt`` (str, optional): transcription hint.
        - ``source_title`` (str, optional): provenance label.

        Returns
        -------
        ToolResult
            ``success=True``  - transcript text + provenance.
            ``success=False`` - structured failure with actionable message.
        """
        start_ms = time.monotonic() * 1000

        # ── 1. Validate audio input ─────────────────────────────────────────
        validation_result = self._validate_input(request.parameters, start_ms)
        if validation_result is not None:
            return validation_result

        audio_data: str = request.parameters["audio_data"]
        prompt: str = request.parameters.get("prompt", _DEFAULT_PROMPT)
        source_title: Optional[str] = request.parameters.get("source_title")

        # ── 2. Check gateway configured ────────────────────────────────────
        if self._gateway is None:
            return ToolResult(
                success=False,
                error=(
                    "AudioTranscriptionTool has no model gateway configured. "
                    "Inject a transcription-capable ModelGateway to enable audio transcription."
                ),
                tool_name=AUDIO_TRANSCRIPTION_TOOL_ID,
                execution_time_ms=_elapsed(start_ms),
                metadata={
                    "failure_kind": "no_gateway",
                    "tool_identifier": AUDIO_TRANSCRIPTION_TOOL_ID,
                },
            )

        # ── 3. Call the model gateway (AUDIO_TRANSCRIPTION enforced by guard) ─
        model_request = ModelRequest(
            prompt=prompt,
            audio_data=audio_data,
            parameters={"required_capability": ModelCapability.AUDIO_TRANSCRIPTION.value},
        )

        try:
            model_response = await self._gateway.transcribe(model_request)
        except ModelError as exc:
            safe_message = _sanitize_error_message(exc)
            return ToolResult(
                success=False,
                error=f"Transcription model request failed: {safe_message}",
                tool_name=AUDIO_TRANSCRIPTION_TOOL_ID,
                execution_time_ms=_elapsed(start_ms),
                metadata={
                    "failure_kind": "model_error",
                    "error_code": getattr(exc, "error_code", "unknown"),
                    "tool_identifier": AUDIO_TRANSCRIPTION_TOOL_ID,
                },
            )
        except TimeoutError:
            return ToolResult(
                success=False,
                error="Transcription model request timed out.",
                tool_name=AUDIO_TRANSCRIPTION_TOOL_ID,
                execution_time_ms=_elapsed(start_ms),
                metadata={
                    "failure_kind": "timeout",
                    "tool_identifier": AUDIO_TRANSCRIPTION_TOOL_ID,
                },
            )
        except Exception as exc:
            return ToolResult(
                success=False,
                error="Transcription model request encountered an unexpected error.",
                tool_name=AUDIO_TRANSCRIPTION_TOOL_ID,
                execution_time_ms=_elapsed(start_ms),
                metadata={
                    "failure_kind": "unexpected",
                    "exception_type": type(exc).__name__,
                    "tool_identifier": AUDIO_TRANSCRIPTION_TOOL_ID,
                },
            )

        # ── 4. Validate provider response - never fabricate ────────────────
        if not model_response.text or not model_response.text.strip():
            return ToolResult(
                success=False,
                error="Transcription model returned an empty response.",
                tool_name=AUDIO_TRANSCRIPTION_TOOL_ID,
                execution_time_ms=_elapsed(start_ms),
                metadata={
                    "failure_kind": "empty_response",
                    "model_id": model_response.model_id,
                    "tool_identifier": AUDIO_TRANSCRIPTION_TOOL_ID,
                },
            )

        # ── 5. Build provenance ─────────────────────────────────────────────
        source, evidence = _build_provenance(
            transcript=model_response.text,
            model_id=model_response.model_id,
            source_title=source_title,
            tool_identifier=AUDIO_TRANSCRIPTION_TOOL_ID,
            prompt=prompt,
        )

        return ToolResult(
            success=True,
            output={
                "transcript": model_response.text,
                "model_id": model_response.model_id,
                "source_title": source_title,
                "summary": model_response.text,
                "evidence_ids": [evidence.id],
                "source_ids": [source.id],
                "evidence_objects": [evidence],
                "source_objects": [source],
            },
            tool_name=AUDIO_TRANSCRIPTION_TOOL_ID,
            execution_time_ms=_elapsed(start_ms),
            metadata={
                "tool_identifier": AUDIO_TRANSCRIPTION_TOOL_ID,
                "model_id": model_response.model_id,
                "provenance": "audio_transcription_tool",
            },
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _validate_input(
        self,
        parameters: dict[str, Any],
        start_ms: float,
    ) -> ToolResult | None:
        """Validate audio input. Returns a failure ToolResult or None on success."""
        audio_data = parameters.get("audio_data")

        # Missing
        if audio_data is None:
            return ToolResult(
                success=False,
                error="Missing required parameter 'audio_data'.",
                tool_name=AUDIO_TRANSCRIPTION_TOOL_ID,
                execution_time_ms=_elapsed(start_ms),
                metadata={
                    "failure_kind": "missing_audio_input",
                    "tool_identifier": AUDIO_TRANSCRIPTION_TOOL_ID,
                },
            )

        # Wrong type
        if not isinstance(audio_data, str):
            return ToolResult(
                success=False,
                error="Parameter 'audio_data' must be a string (data URL).",
                tool_name=AUDIO_TRANSCRIPTION_TOOL_ID,
                execution_time_ms=_elapsed(start_ms),
                metadata={
                    "failure_kind": "invalid_audio_type",
                    "tool_identifier": AUDIO_TRANSCRIPTION_TOOL_ID,
                },
            )

        # Empty
        if not audio_data.strip():
            return ToolResult(
                success=False,
                error="Parameter 'audio_data' must not be empty.",
                tool_name=AUDIO_TRANSCRIPTION_TOOL_ID,
                execution_time_ms=_elapsed(start_ms),
                metadata={
                    "failure_kind": "empty_audio_input",
                    "tool_identifier": AUDIO_TRANSCRIPTION_TOOL_ID,
                },
            )

        # Oversized (defence-in-depth - before any model call)
        if len(audio_data) > _MAX_AUDIO_BYTES_ENCODED:
            return ToolResult(
                success=False,
                error=(
                    f"Audio data exceeds the maximum allowed size "
                    f"({_MAX_AUDIO_BYTES_ENCODED // (1024 * 1024)} MiB encoded)."
                ),
                tool_name=AUDIO_TRANSCRIPTION_TOOL_ID,
                execution_time_ms=_elapsed(start_ms),
                metadata={
                    "failure_kind": "audio_too_large",
                    "tool_identifier": AUDIO_TRANSCRIPTION_TOOL_ID,
                },
            )

        # Representation check: must be a supported audio data URL
        if not _is_supported_audio_representation(audio_data):
            return ToolResult(
                success=False,
                error=(
                    "Unsupported audio representation. "
                    "Provide a data URL of the form 'data:audio/<type>;base64,...'. "
                    "Supported types: wav, mp3, mpeg, ogg, webm, flac, mp4, x-m4a."
                ),
                tool_name=AUDIO_TRANSCRIPTION_TOOL_ID,
                execution_time_ms=_elapsed(start_ms),
                metadata={
                    "failure_kind": "unsupported_audio_format",
                    "tool_identifier": AUDIO_TRANSCRIPTION_TOOL_ID,
                },
            )

        return None  # valid


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _is_supported_audio_representation(audio_data: str) -> bool:
    """Return True when audio_data is a recognised audio data URL."""
    # Only data URLs with an explicitly approved audio MIME type are accepted.
    # Remote URLs (http://, https://) and plain base64 strings (without a
    # proper audio MIME type header) are rejected - the tool requires an
    # explicit MIME type to forward the correct format identifier to the provider.
    stripped = audio_data.strip()

    # Reject obvious remote URLs
    lower = stripped.lower()
    if lower.startswith("http://") or lower.startswith("https://"):
        return False

    # Recognised audio data URL?
    for prefix in _ALLOWED_AUDIO_PREFIXES:
        if stripped.lower().startswith(prefix.lower()):
            b64_part = stripped[len(prefix):]
            return _is_valid_base64(b64_part)

    # Reject any other data: URL (unknown media type, or non-audio)
    if stripped.lower().startswith("data:"):
        return False

    # Reject plain base64 without explicit audio MIME type
    # (we need the MIME type to determine the audio format for the provider)
    return False


def _is_valid_base64(data: str) -> bool:
    """Return True when *data* is non-empty valid base64."""
    if not data.strip():
        return False
    try:
        # Add padding if necessary
        padded = data + "=" * (-len(data) % 4)
        base64.b64decode(padded, validate=True)
        return True
    except Exception:
        return False

def _build_provenance(
    transcript: str,
    model_id: str,
    source_title: Optional[str],
    tool_identifier: str,
    prompt: str,
) -> tuple[Source, Evidence]:
    """Create a (Source, Evidence) pair for a successful transcription result.

    No URL is fabricated - ``source.url`` stays ``None`` for local/embedded audio.
    """
    source = Source(
        url=None,  # never fabricated for local/embedded audio data
        title=source_title,
        description=f"Audio transcribed via {tool_identifier}",
        metadata={
            "tool_identifier": tool_identifier,
            "model_id": model_id,
            "prompt": prompt,
        },
    )
    evidence = Evidence(
        source_id=source.id,
        content=transcript,
        content_type="audio_transcript",
        metadata={
            "tool_identifier": tool_identifier,
            "model_id": model_id,
            "provenance": "audio_transcription_tool",
        },
    )
    return source, evidence


def _sanitize_error_message(exc: ModelError) -> str:
    """Return a safe error message from a ModelError.

    Deliberately excludes:
    - ``exc.details`` (may contain model_id or partial request info)
    - any credential / Authorization header value
    - The error_code is safe to include (it is a structured short string).
    """
    code = getattr(exc, "error_code", None)
    msg = exc.message if exc.message else "Model error"
    if code:
        return f"{msg} [error_code={code}]"
    return msg


def _elapsed(start_ms: float) -> float:
    return time.monotonic() * 1000 - start_ms
