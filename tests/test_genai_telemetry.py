"""Unit tests for GenAI telemetry conventions and incident milestone events."""

from __future__ import annotations

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from common import telemetry


def test_genai_attributes_and_milestone_event():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    telemetry.set_tracer_provider_for_testing(provider)

    try:
        tracer = telemetry.get_tracer("test")
        with tracer.start_as_current_span("agent.investigate") as span:
            telemetry.record_genai_attributes(
                span,
                agent_name="observer",
                operation_name="triage",
                model="test-model",
            )
            telemetry.record_incident_milestone_event(
                phase="triage",
                summary="Investigating CrashLoopBackOff",
                attributes={"service": "payments-api"},
            )

        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        recorded = spans[0]
        assert recorded.attributes["gen_ai.system"] == "adk"
        assert recorded.attributes["gen_ai.agent.name"] == "observer"
        assert recorded.attributes["gen_ai.operation.name"] == "triage"
        assert recorded.attributes["gen_ai.request.model"] == "test-model"

        # Verify span event
        assert len(recorded.events) == 1
        event = recorded.events[0]
        assert event.name == "incident.milestone.triage"
        assert event.attributes["phase"] == "triage"
        assert event.attributes["summary"] == "Investigating CrashLoopBackOff"
        assert event.attributes["service"] == "payments-api"
    finally:
        provider.shutdown()
        telemetry.set_tracer_provider_for_testing(None)
