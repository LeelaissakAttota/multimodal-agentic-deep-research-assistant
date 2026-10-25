"""Optional OpenTelemetry tracing for runs, agents and tools (``MDRA_OTEL_EXPORTER=console|otlp``).

``MDRA_TRACE_STORE=sqlite|jsonl`` additionally (or alone) keeps spans locally per run for the dashboard's
trace timeline (:mod:`deep_research.scale.spanstore`); spans opened inside :func:`run_context` carry the run id.

Off by default: every helper is a no-op unless tracing is enabled *and* ``opentelemetry-sdk`` is
installed. Span attributes pass through :func:`safe_attributes`: secret-like keys are dropped, values
are redacted (API keys, bearer tokens, ``key=value`` secrets, credentials in URLs) and truncated.
Prompts, report text and API keys are never attached.
"""

from __future__ import annotations

import contextvars
import threading
from collections.abc import AsyncIterator, Iterator, Mapping
from contextlib import asynccontextmanager, contextmanager
from typing import TYPE_CHECKING, Any

from deep_research.observability.redaction import is_sensitive_key, redact_text
from deep_research.scale.config import redact_url_credentials

if TYPE_CHECKING:
    from deep_research.tools.tool import Tool, ToolRequest, ToolResult

_MAX_VALUE = 256
_lock = threading.Lock()
_state: dict[str, Any] = {"configured": False, "tracer": None, "provider": None}
_run_id: contextvars.ContextVar[str] = contextvars.ContextVar("mdra_trace_run_id", default="")


def current_run_id() -> str:
    return _run_id.get()


@contextmanager
def run_context(run_id: str | None) -> Iterator[None]:
    """Spans started inside (including child tasks and ``to_thread`` work) are tagged ``mdra.run_id``."""
    token = _run_id.set(str(run_id or ""))
    try:
        yield
    finally:
        _run_id.reset(token)


class _Span:
    """Thin wrapper so callers can add attributes without importing OpenTelemetry."""

    def __init__(self, span: Any | None) -> None:
        self._span = span

    def set(self, key: str, value: Any) -> None:
        if self._span is not None:
            attrs = safe_attributes({key: value})
            if attrs:
                self._span.set_attributes(attrs)


def safe_attributes(attrs: Mapping[str, Any]) -> dict[str, Any]:
    """OTel-compatible attributes with secrets removed (never raises)."""
    out: dict[str, Any] = {}
    for key, value in attrs.items():
        if value is None or is_sensitive_key(str(key)):
            continue
        if isinstance(value, bool | int | float):
            out[str(key)] = value
        elif isinstance(value, list | tuple):
            out[str(key)] = [_clean(str(v)) for v in list(value)[:20]]
        else:
            out[str(key)] = _clean(str(value))
    return out


def _clean(text: str) -> str:
    return redact_text(redact_url_credentials(text))[:_MAX_VALUE]


def configure_tracing(*, exporter: Any | None = None, service_name: str | None = None, force: bool = False,
                      store: Any | None = None) -> bool:
    """Set up tracing once from ``MDRA_OTEL_*`` / ``MDRA_TRACE_*`` (or explicit exporter/store, for tests)."""
    with _lock:
        if _state["configured"] and not force:
            return _state["tracer"] is not None
        _state.update(configured=True, tracer=None)
        from deep_research.scale.config import ScaleSettings, has_module

        scale = ScaleSettings()
        mode = scale.otel_exporter if exporter is None else "custom"
        if store is None and scale.trace_store != "off":
            from deep_research.scale.spanstore import SpanStore

            store = SpanStore(scale.trace_dir, scale.trace_store)
        if (mode == "off" and store is None) or not has_module("opentelemetry.sdk"):
            return False
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter, SimpleSpanProcessor

        provider = TracerProvider(resource=Resource.create({"service.name": service_name or scale.otel_service_name}))
        if store is not None:
            from deep_research.scale.spanstore import make_processor

            provider.add_span_processor(make_processor(store))
        if mode == "off":
            pass
        elif exporter is not None:
            provider.add_span_processor(SimpleSpanProcessor(exporter))
        elif mode == "console":
            provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
        else:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

            provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=scale.otel_endpoint, timeout=5)))
        _state.update(provider=provider, tracer=provider.get_tracer("deep_research"))
        return True


def reset_tracing() -> None:
    """Forget the configuration (tests); flushes and shuts down any provider."""
    with _lock:
        provider = _state.get("provider")
        if provider is not None:
            provider.shutdown()
        _state.update(configured=False, tracer=None, provider=None)


def tracing_enabled() -> bool:
    return configure_tracing()


@contextmanager
def span(name: str, **attrs: Any) -> Iterator[_Span]:
    """Synchronous span; records exceptions by type only (messages may hold user data)."""
    if not configure_tracing():
        yield _Span(None)
        return
    from opentelemetry.trace import Status, StatusCode

    tracer = _state["tracer"]
    with tracer.start_as_current_span(name, attributes=safe_attributes(attrs), record_exception=False,
                                      set_status_on_exception=False) as current:
        try:
            yield _Span(current)
        except BaseException as exc:
            current.set_status(Status(StatusCode.ERROR, type(exc).__name__))
            current.set_attribute("error.type", type(exc).__name__)
            raise


@asynccontextmanager
async def aspan(name: str, **attrs: Any) -> AsyncIterator[_Span]:
    with span(name, **attrs) as s:
        yield s


async def traced_tool(tool: Tool, request: ToolRequest) -> ToolResult:
    """``await tool.execute(request)`` inside an ``mdra.tool`` span (tool id + parameter names only)."""
    params = getattr(request, "parameters", None) or {}
    async with aspan("mdra.tool", **{"mdra.tool": getattr(request, "tool_name", type(tool).__name__),
                                     "mdra.tool.params": sorted(str(k) for k in params)}) as s:
        result = await tool.execute(request)
        s.set("mdra.tool.success", bool(getattr(result, "success", True)))
        return result
