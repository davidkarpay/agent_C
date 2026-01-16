"""
SDLC Phase Agents.

Specialized agents for each phase of the software development lifecycle:
- Requirements Analyst: Parse requirements, flag ambiguities
- Test Generator: Generate pytest test skeletons
- Architecture Advisor: Design proposals, interface contracts
- Code Generator: Boilerplate, tests, documentation
- Test Orchestrator: Test execution, coverage analysis
- Release Manager: Changelog, versioning, deployment
- Issue Triager: Bug analysis, root cause hypotheses
"""

from .base import BaseAgent, AgentConfig, AgentResult, AgentPhase
from .requirements import RequirementsAnalystAgent
from .test_generator import TestGeneratorAgent

__all__ = [
    "BaseAgent",
    "AgentConfig",
    "AgentResult",
    "AgentPhase",
    "RequirementsAnalystAgent",
    "TestGeneratorAgent",
]
