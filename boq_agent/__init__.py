"""BOQ -> Work Package agent.

Pipeline:  ingest (Excel/PDF) -> structure (sections / items) -> LLM classification
(local Gemma via Ollama, flowchart rules as system prompt) -> packaging plan with
special-case proposals (user approve/deny) -> PDF snapshots per work package.

The resulting `Session` (see models.py) is persisted as JSON so that downstream
modules (e.g. the future RFQ drafter in `boq_agent.rfq`) can consume the work packages.
"""

__version__ = "0.1.0"
