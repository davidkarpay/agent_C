"""
Base Agent Class for SDLC Framework.

Provides common interface and compliance hooks for all specialized agents:
- Deterministic configuration (temperature=0)
- Structured input/output schemas
- Audit logging integration
- Approval gate hooks
- LLM interaction via Ollama
"""

import json
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Type

import requests

from ..audit import AuditLogger, AuditAction
from ..approval import ApprovalGate, ApprovalStatus, ApprovalResponse


class AgentPhase(Enum):
    """SDLC phases that agents can belong to."""
    REQUIREMENTS = "requirements"
    DESIGN = "design"
    IMPLEMENTATION = "implementation"
    TESTING = "testing"
    DEPLOYMENT = "deployment"
    MAINTENANCE = "maintenance"


@dataclass
class AgentConfig:
    """
    Configuration for an agent.

    Enforces compliance requirements through sensible defaults.
    """
    # Model settings - deterministic by default
    model_name: str = "gemma2:2b"  # Default to fast, instruction-following model
    temperature: float = 0.0  # MUST be 0 for deterministic outputs
    max_tokens: int = 2048
    context_window: int = 8192

    # Ollama settings
    ollama_base_url: str = "http://localhost:11434"

    # Agent behavior
    max_iterations: int = 10  # Prevent infinite loops
    require_approval: bool = True  # Human-in-the-loop by default
    auto_approve_reads: bool = True  # Only require approval for writes

    # Logging
    log_llm_requests: bool = True  # Log all LLM interactions

    def validate(self) -> List[str]:
        """Validate configuration, return list of warnings/errors."""
        issues = []

        if self.temperature != 0.0:
            issues.append(
                f"WARNING: temperature={self.temperature} is non-zero. "
                "Outputs will not be deterministic (compliance risk)."
            )

        if not self.require_approval:
            issues.append(
                "WARNING: require_approval=False. "
                "Agents will execute without human review (compliance risk)."
            )

        return issues


@dataclass
class AgentResult:
    """
    Result from an agent execution.

    Captures all information needed for audit and review.
    """
    agent_id: str
    success: bool
    output: Any  # The actual result (varies by agent type)
    reasoning: str  # Explanation of what was done and why
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    # Execution details
    iterations: int = 0
    duration_ms: int = 0
    model_name: Optional[str] = None

    # Error handling
    error_message: Optional[str] = None
    error_type: Optional[str] = None

    # Approval tracking
    approvals_requested: int = 0
    approvals_granted: int = 0
    approvals_denied: int = 0

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "agent_id": self.agent_id,
            "success": self.success,
            "output": self.output,
            "reasoning": self.reasoning,
            "timestamp": self.timestamp,
            "iterations": self.iterations,
            "duration_ms": self.duration_ms,
            "model_name": self.model_name,
            "error_message": self.error_message,
            "error_type": self.error_type,
            "approvals_requested": self.approvals_requested,
            "approvals_granted": self.approvals_granted,
            "approvals_denied": self.approvals_denied
        }


class BaseAgent(ABC):
    """
    Abstract base class for all SDLC agents.

    Provides:
    - Structured execution flow with compliance hooks
    - LLM interaction via Ollama with structured outputs
    - Audit logging for all actions
    - Human approval gates for significant actions

    Subclasses must implement:
    - agent_id: Unique identifier for this agent type
    - phase: Which SDLC phase this agent belongs to
    - execute(): Main agent logic

    Usage:
        class MyAgent(BaseAgent):
            agent_id = "my_agent"
            phase = AgentPhase.IMPLEMENTATION

            def execute(self, task_input):
                # Agent logic here
                return AgentResult(...)

        agent = MyAgent(config, audit_logger, approval_gate)
        result = agent.run(task_input)
    """

    # Subclasses must define these
    agent_id: str = "base_agent"
    phase: AgentPhase = AgentPhase.IMPLEMENTATION
    description: str = "Base agent class"

    def __init__(
        self,
        config: AgentConfig,
        audit: AuditLogger,
        approval: ApprovalGate
    ):
        """
        Initialize agent.

        Args:
            config: Agent configuration
            audit: Audit logger instance
            approval: Approval gate instance
        """
        self.config = config
        self.audit = audit
        self.approval = approval

        # Validate config and log warnings
        issues = config.validate()
        for issue in issues:
            self.audit.log(
                action=AuditAction.SYSTEM_OUTPUT,
                agent_id=self.agent_id,
                output_data=issue,
                reasoning="Configuration validation"
            )

        # Execution state
        self._iteration = 0
        self._start_time: Optional[float] = None
        self._approvals_requested = 0
        self._approvals_granted = 0
        self._approvals_denied = 0

    def run(self, task_input: Any) -> AgentResult:
        """
        Run the agent with full compliance wrapper.

        This is the main entry point. It:
        1. Logs agent invocation
        2. Calls execute() (implemented by subclass)
        3. Logs completion/failure
        4. Returns structured result

        Args:
            task_input: Input for the agent (varies by agent type)

        Returns:
            AgentResult with outcome and metadata
        """
        self._start_time = time.time()
        self._iteration = 0
        self._approvals_requested = 0
        self._approvals_granted = 0
        self._approvals_denied = 0

        # Log invocation
        self.audit.log(
            action=AuditAction.AGENT_INVOKED,
            agent_id=self.agent_id,
            input_data=task_input,
            reasoning=f"Starting {self.agent_id} for phase {self.phase.value}"
        )

        try:
            # Execute agent logic (implemented by subclass)
            result = self.execute(task_input)

            # Ensure result has correct metadata
            result.agent_id = self.agent_id
            result.iterations = self._iteration
            result.duration_ms = int((time.time() - self._start_time) * 1000)
            result.model_name = self.config.model_name
            result.approvals_requested = self._approvals_requested
            result.approvals_granted = self._approvals_granted
            result.approvals_denied = self._approvals_denied

            # Log completion
            self.audit.log(
                action=AuditAction.AGENT_COMPLETED,
                agent_id=self.agent_id,
                output_data=result.to_dict(),
                reasoning=result.reasoning,
                duration_ms=result.duration_ms,
                success=result.success
            )

            return result

        except Exception as e:
            duration_ms = int((time.time() - self._start_time) * 1000)

            # Log failure
            self.audit.log(
                action=AuditAction.AGENT_FAILED,
                agent_id=self.agent_id,
                output_data=str(e),
                reasoning=f"Agent failed with {type(e).__name__}",
                duration_ms=duration_ms,
                success=False,
                error_message=str(e)
            )

            return AgentResult(
                agent_id=self.agent_id,
                success=False,
                output=None,
                reasoning=f"Agent failed: {str(e)}",
                iterations=self._iteration,
                duration_ms=duration_ms,
                model_name=self.config.model_name,
                error_message=str(e),
                error_type=type(e).__name__,
                approvals_requested=self._approvals_requested,
                approvals_granted=self._approvals_granted,
                approvals_denied=self._approvals_denied
            )

    @abstractmethod
    def execute(self, task_input: Any) -> AgentResult:
        """
        Execute agent logic.

        Subclasses must implement this method with their specific behavior.

        Args:
            task_input: Input for the agent

        Returns:
            AgentResult with outcome
        """
        pass

    def call_llm(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        json_schema: Optional[dict] = None
    ) -> str:
        """
        Call the LLM via Ollama.

        Args:
            prompt: User prompt
            system_prompt: Optional system prompt
            json_schema: Optional JSON schema for structured output

        Returns:
            LLM response text
        """
        self._iteration += 1

        if self._iteration > self.config.max_iterations:
            raise RuntimeError(
                f"Agent exceeded maximum iterations ({self.config.max_iterations})"
            )

        # Build request
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        request_data = {
            "model": self.config.model_name,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": self.config.temperature,
                "num_predict": self.config.max_tokens
            }
        }

        # Add JSON schema for structured output
        if json_schema:
            request_data["format"] = json_schema

        # Log request
        if self.config.log_llm_requests:
            self.audit.log(
                action=AuditAction.LLM_REQUEST,
                agent_id=self.agent_id,
                input_data={
                    "model": self.config.model_name,
                    "prompt_length": len(prompt),
                    "has_system_prompt": system_prompt is not None,
                    "has_schema": json_schema is not None
                },
                reasoning=f"LLM call iteration {self._iteration}"
            )

        # Make request
        start_time = time.time()
        response = requests.post(
            f"{self.config.ollama_base_url}/api/chat",
            json=request_data,
            timeout=300  # 5 minute timeout
        )
        duration_ms = int((time.time() - start_time) * 1000)

        response.raise_for_status()
        result = response.json()

        response_text = result.get("message", {}).get("content", "")

        # Log response
        if self.config.log_llm_requests:
            self.audit.log(
                action=AuditAction.LLM_RESPONSE,
                agent_id=self.agent_id,
                output_data={
                    "response_length": len(response_text),
                    "model": result.get("model"),
                    "done": result.get("done")
                },
                reasoning=f"LLM response received",
                duration_ms=duration_ms
            )

        return response_text

    def call_llm_structured(
        self,
        prompt: str,
        schema: dict,
        system_prompt: Optional[str] = None
    ) -> dict:
        """
        Call LLM and parse structured JSON response.

        Args:
            prompt: User prompt
            schema: JSON schema for response
            system_prompt: Optional system prompt

        Returns:
            Parsed JSON response as dict
        """
        response = self.call_llm(prompt, system_prompt, json_schema=schema)

        try:
            return json.loads(response)
        except json.JSONDecodeError as e:
            # Try to extract JSON from response
            import re
            json_match = re.search(r'\{.*\}', response, re.DOTALL)
            if json_match:
                return json.loads(json_match.group())
            raise ValueError(f"Could not parse LLM response as JSON: {e}")

    def request_approval(
        self,
        action_type: str,
        proposal: Any,
        description: str,
        context: Optional[str] = None,
        risk_level: str = "medium"
    ) -> ApprovalResponse:
        """
        Request human approval for an action.

        Args:
            action_type: Type of action
            proposal: Proposed action details
            description: Human-readable description
            context: Additional context
            risk_level: low, medium, or high

        Returns:
            ApprovalResponse with human's decision
        """
        if not self.config.require_approval:
            # Auto-approve if approval disabled (not recommended!)
            return ApprovalResponse(
                request_id="auto",
                status=ApprovalStatus.APPROVED,
                notes="Auto-approved (approval disabled in config)"
            )

        self._approvals_requested += 1

        request = self.approval.request_generic(
            agent_id=self.agent_id,
            action_type=action_type,
            proposal=proposal,
            description=description,
            context=context,
            risk_level=risk_level
        )

        response = self.approval.await_approval(request)

        if response.status == ApprovalStatus.APPROVED:
            self._approvals_granted += 1
        elif response.status == ApprovalStatus.REJECTED:
            self._approvals_denied += 1
        elif response.status == ApprovalStatus.MODIFIED:
            self._approvals_granted += 1  # Modified counts as granted

        return response

    def request_file_edit_approval(
        self,
        file_path: str,
        original: str,
        proposed: str,
        description: str,
        context: Optional[str] = None
    ) -> ApprovalResponse:
        """
        Request approval for a file edit with diff view.

        Args:
            file_path: Path to file
            original: Original content
            proposed: Proposed new content
            description: What the edit does
            context: Why it's needed

        Returns:
            ApprovalResponse
        """
        if not self.config.require_approval:
            return ApprovalResponse(
                request_id="auto",
                status=ApprovalStatus.APPROVED,
                notes="Auto-approved (approval disabled)"
            )

        self._approvals_requested += 1

        request = self.approval.request_file_edit(
            agent_id=self.agent_id,
            file_path=file_path,
            original=original,
            proposed=proposed,
            description=description,
            context=context
        )

        response = self.approval.await_approval(request)

        if response.status == ApprovalStatus.APPROVED:
            self._approvals_granted += 1
        elif response.status == ApprovalStatus.REJECTED:
            self._approvals_denied += 1
        elif response.status == ApprovalStatus.MODIFIED:
            self._approvals_granted += 1

        return response

    def request_shell_approval(
        self,
        command: str,
        description: str,
        context: Optional[str] = None,
        risk_level: str = "medium"
    ) -> ApprovalResponse:
        """
        Request approval for a shell command.

        Args:
            command: Shell command
            description: What it does
            context: Why it's needed
            risk_level: low, medium, or high

        Returns:
            ApprovalResponse
        """
        if not self.config.require_approval:
            return ApprovalResponse(
                request_id="auto",
                status=ApprovalStatus.APPROVED,
                notes="Auto-approved (approval disabled)"
            )

        self._approvals_requested += 1

        request = self.approval.request_shell_command(
            agent_id=self.agent_id,
            command=command,
            description=description,
            context=context,
            risk_level=risk_level
        )

        response = self.approval.await_approval(request)

        if response.status == ApprovalStatus.APPROVED:
            self._approvals_granted += 1
        elif response.status == ApprovalStatus.REJECTED:
            self._approvals_denied += 1
        elif response.status == ApprovalStatus.MODIFIED:
            self._approvals_granted += 1

        return response

    def log_tool_call(self, tool_name: str, args: dict, result: Any) -> None:
        """
        Log a tool call for audit purposes.

        Args:
            tool_name: Name of tool called
            args: Arguments passed to tool
            result: Result from tool
        """
        self.audit.log(
            action=AuditAction.TOOL_CALLED,
            agent_id=self.agent_id,
            input_data={"tool": tool_name, "args": args},
            reasoning=f"Calling tool {tool_name}"
        )

        self.audit.log(
            action=AuditAction.TOOL_RESULT,
            agent_id=self.agent_id,
            input_data={"tool": tool_name},
            output_data=result,
            reasoning=f"Tool {tool_name} completed"
        )
