"""
Multi-Agent SDLC Framework for Regulated Industries.

This framework provides specialized agents mapped to Software Development
Lifecycle phases for mission-critical software in regulated industries.

Non-Negotiable Compliance Requirements:
- Attorney-Client Privilege: 100% local execution, no cloud APIs
- Audit Trails: Every agent action logged with timestamp, input, output, reasoning
- Deterministic Outputs: Temperature=0, seeded operations, hash-verified reproducibility
- Human-in-the-Loop: All agents PROPOSE → human APPROVES → then EXECUTE
"""

__version__ = "0.1.0"
__author__ = "Local Agent Framework"

from .audit import AuditLogger, AuditEntry
from .approval import ApprovalGate, ApprovalStatus

__all__ = [
    "AuditLogger",
    "AuditEntry",
    "ApprovalGate",
    "ApprovalStatus",
]
