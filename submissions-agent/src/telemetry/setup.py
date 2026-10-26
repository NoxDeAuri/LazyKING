"""
OpenTelemetry SDK setup.

Opt-in: set OTEL_ENABLED=true to activate. Off by default — zero overhead
when disabled because the SDK's no-op tracer runs when no provider is set.

Exporters (pick one via env):
  OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317  → OTLP/gRPC (Jaeger, Tempo, etc.)
  (nothing set)                                       → console exporter

Jaeger quick-start (Docker):
  docker run -d --name jaeger -p 16686:16686 -p 4317:4317 jaegertracing/all-in-one:latest
  open http://localhost:16686

Config:
  OTEL_ENABLED=true
  OTEL_SERVICE_NAME=submissions-agent   (default)
  OTEL_EXPORTER_OTLP_ENDPOINT=...       (optional; console if unset)
"""

import os

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource, SERVICE_NAME
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

_initialized = False


def enabled() -> bool:
    return os.getenv("OTEL_ENABLED", "").lower() in ("1", "true", "yes")


def setup_telemetry(service_name: str | None = None) -> None:
    """
    Configure the OTel SDK. Call once at app startup.
    Safe to call multiple times — subsequent calls are no-ops.
    """
    global _initialized
    if _initialized or not enabled():
        return

    svc = service_name or os.getenv("OTEL_SERVICE_NAME", "submissions-agent")
    resource = Resource(attributes={SERVICE_NAME: svc})
    provider = TracerProvider(resource=resource)

    otlp_endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    if otlp_endpoint:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        exporter = OTLPSpanExporter(endpoint=otlp_endpoint, insecure=True)
        print(f"[otel] exporting traces → {otlp_endpoint}")
    else:
        exporter = ConsoleSpanExporter()
        print("[otel] exporting traces → console (set OTEL_EXPORTER_OTLP_ENDPOINT for Jaeger)")

    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

    # Auto-instrument httpx — covers Ollama, Anthropic SDK, and RAG embedder
    try:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        HTTPXClientInstrumentor().instrument()
        print("[otel] httpx auto-instrumented (covers all LLM + embedding calls)")
    except ImportError:
        print("[otel] httpx auto-instrumentation unavailable — install opentelemetry-instrumentation-httpx")

    _initialized = True
    print(f"[otel] telemetry active — service={svc!r}")
