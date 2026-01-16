"""
Human Approval Interface for Regulated Industries.

Provides the critical human-in-the-loop component for compliance:
- Display agent proposals with diff view
- Accept/Reject/Modify workflow
- Require explicit approval before execution
- Track approval decisions in audit log

All agents must: PROPOSE → human APPROVES → then EXECUTE
"""

import difflib
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional, List, Dict

from .audit import AuditLogger, AuditAction


class ApprovalStatus(Enum):
    """Status of an approval request."""
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    MODIFIED = "modified"


@dataclass
class ApprovalRequest:
    """
    A request for human approval.

    Contains all information needed for a human to make an informed decision.
    """
    request_id: str  # Unique identifier
    agent_id: str  # Which agent is requesting
    action_type: str  # What type of action (edit_file, run_shell, etc.)
    description: str  # Human-readable description of proposed action
    proposal: Any  # The actual proposed change (file content, command, etc.)
    context: Optional[str] = None  # Additional context for decision
    risk_level: str = "medium"  # low, medium, high
    reversible: bool = True  # Can this action be undone?

    # For file edits - show diff
    original_content: Optional[str] = None
    proposed_content: Optional[str] = None

    # Metadata
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    status: ApprovalStatus = ApprovalStatus.PENDING
    reviewer_notes: Optional[str] = None


@dataclass
class ApprovalResponse:
    """
    Human's response to an approval request.
    """
    request_id: str
    status: ApprovalStatus
    modified_proposal: Optional[Any] = None  # If modified, the new proposal
    notes: Optional[str] = None  # Reviewer's notes
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class ApprovalGate:
    """
    Human approval gate for agent actions.

    Ensures all significant actions require explicit human approval
    before execution. Integrates with audit logging for compliance.

    Usage:
        gate = ApprovalGate(audit_logger)

        # Request approval for file edit
        request = gate.request_file_edit(
            agent_id="code_generator",
            file_path="/path/to/file.py",
            original="old content",
            proposed="new content",
            description="Add error handling to function"
        )

        # Get human decision (blocks until decision made)
        response = gate.await_approval(request)

        if response.status == ApprovalStatus.APPROVED:
            # Execute the action
            pass
        elif response.status == ApprovalStatus.MODIFIED:
            # Execute with modifications
            modified_content = response.modified_proposal
    """

    def __init__(
        self,
        audit_logger: AuditLogger,
        auto_approve: bool = False,
        approval_callback: Optional[Callable[[ApprovalRequest], ApprovalResponse]] = None
    ):
        """
        Initialize approval gate.

        Args:
            audit_logger: Logger for recording approval decisions
            auto_approve: If True, auto-approve all requests (ONLY for testing!)
            approval_callback: Custom callback for getting approval
                              (defaults to CLI prompts)
        """
        self.audit = audit_logger
        self.auto_approve = auto_approve
        self.approval_callback = approval_callback or self._cli_approval
        self._pending_requests: Dict[str, ApprovalRequest] = {}
        self._request_counter = 0

    def _generate_request_id(self) -> str:
        """Generate unique request ID."""
        self._request_counter += 1
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        return f"req_{timestamp}_{self._request_counter:04d}"

    def request_file_edit(
        self,
        agent_id: str,
        file_path: str,
        original: str,
        proposed: str,
        description: str,
        context: Optional[str] = None
    ) -> ApprovalRequest:
        """
        Request approval for a file edit.

        Args:
            agent_id: Agent requesting the edit
            file_path: Path to file being edited
            original: Original file content
            proposed: Proposed new content
            description: Human-readable description
            context: Additional context

        Returns:
            ApprovalRequest object
        """
        request = ApprovalRequest(
            request_id=self._generate_request_id(),
            agent_id=agent_id,
            action_type="edit_file",
            description=description,
            proposal={"file_path": file_path, "content": proposed},
            context=context,
            original_content=original,
            proposed_content=proposed,
            risk_level="medium",
            reversible=True
        )

        self._pending_requests[request.request_id] = request

        # Log approval request
        self.audit.log(
            action=AuditAction.APPROVAL_REQUESTED,
            agent_id=agent_id,
            input_data={
                "request_id": request.request_id,
                "action_type": "edit_file",
                "file_path": file_path,
                "description": description
            },
            reasoning=context
        )

        return request

    def request_shell_command(
        self,
        agent_id: str,
        command: str,
        description: str,
        context: Optional[str] = None,
        risk_level: str = "medium"
    ) -> ApprovalRequest:
        """
        Request approval for a shell command.

        Args:
            agent_id: Agent requesting execution
            command: Shell command to execute
            description: Human-readable description
            context: Additional context
            risk_level: low, medium, or high

        Returns:
            ApprovalRequest object
        """
        # Determine reversibility
        irreversible_patterns = ["rm ", "delete", "drop", "truncate", "format"]
        reversible = not any(p in command.lower() for p in irreversible_patterns)

        request = ApprovalRequest(
            request_id=self._generate_request_id(),
            agent_id=agent_id,
            action_type="run_shell",
            description=description,
            proposal={"command": command},
            context=context,
            risk_level=risk_level,
            reversible=reversible
        )

        self._pending_requests[request.request_id] = request

        # Log approval request
        self.audit.log(
            action=AuditAction.APPROVAL_REQUESTED,
            agent_id=agent_id,
            input_data={
                "request_id": request.request_id,
                "action_type": "run_shell",
                "command": command,
                "description": description,
                "risk_level": risk_level
            },
            reasoning=context
        )

        return request

    def request_generic(
        self,
        agent_id: str,
        action_type: str,
        proposal: Any,
        description: str,
        context: Optional[str] = None,
        risk_level: str = "medium",
        reversible: bool = True
    ) -> ApprovalRequest:
        """
        Request approval for any action.

        Args:
            agent_id: Agent requesting the action
            action_type: Type of action
            proposal: The proposed action details
            description: Human-readable description
            context: Additional context
            risk_level: low, medium, or high
            reversible: Whether action can be undone

        Returns:
            ApprovalRequest object
        """
        request = ApprovalRequest(
            request_id=self._generate_request_id(),
            agent_id=agent_id,
            action_type=action_type,
            description=description,
            proposal=proposal,
            context=context,
            risk_level=risk_level,
            reversible=reversible
        )

        self._pending_requests[request.request_id] = request

        # Log approval request
        self.audit.log(
            action=AuditAction.APPROVAL_REQUESTED,
            agent_id=agent_id,
            input_data={
                "request_id": request.request_id,
                "action_type": action_type,
                "description": description,
                "risk_level": risk_level
            },
            reasoning=context
        )

        return request

    def await_approval(self, request: ApprovalRequest) -> ApprovalResponse:
        """
        Wait for human approval of a request.

        Args:
            request: The approval request

        Returns:
            ApprovalResponse with human's decision
        """
        if self.auto_approve:
            response = ApprovalResponse(
                request_id=request.request_id,
                status=ApprovalStatus.APPROVED,
                notes="Auto-approved (testing mode)"
            )
        else:
            response = self.approval_callback(request)

        # Update request status
        request.status = response.status
        request.reviewer_notes = response.notes

        # Log the decision
        if response.status == ApprovalStatus.APPROVED:
            action = AuditAction.APPROVAL_GRANTED
        elif response.status == ApprovalStatus.REJECTED:
            action = AuditAction.APPROVAL_DENIED
        else:
            action = AuditAction.APPROVAL_MODIFIED

        self.audit.log(
            action=action,
            agent_id=request.agent_id,
            input_data={"request_id": request.request_id},
            output_data={
                "status": response.status.value,
                "notes": response.notes,
                "modified": response.modified_proposal is not None
            },
            reasoning=response.notes
        )

        # Remove from pending
        self._pending_requests.pop(request.request_id, None)

        return response

    def _cli_approval(self, request: ApprovalRequest) -> ApprovalResponse:
        """
        Default CLI-based approval interface.

        Presents the request to the user via terminal and waits for decision.
        """
        print("\n" + "=" * 70)
        print("APPROVAL REQUIRED")
        print("=" * 70)
        print(f"Request ID: {request.request_id}")
        print(f"Agent: {request.agent_id}")
        print(f"Action: {request.action_type}")
        print(f"Risk Level: {request.risk_level.upper()}")
        print(f"Reversible: {'Yes' if request.reversible else 'NO - IRREVERSIBLE'}")
        print("-" * 70)
        print(f"Description: {request.description}")

        if request.context:
            print(f"\nContext: {request.context}")

        # Show diff for file edits
        if request.original_content is not None and request.proposed_content is not None:
            print("\n--- Proposed Changes ---")
            self._display_diff(request.original_content, request.proposed_content)

        # Show command for shell execution
        if request.action_type == "run_shell":
            print(f"\nCommand: {request.proposal.get('command', 'N/A')}")

        # Show generic proposal
        if request.action_type not in ["edit_file", "run_shell"]:
            print(f"\nProposal: {json.dumps(request.proposal, indent=2, default=str)}")

        print("\n" + "-" * 70)
        print("Options:")
        print("  [a] Approve - Execute as proposed")
        print("  [r] Reject  - Do not execute")
        print("  [m] Modify  - Edit the proposal before executing")
        print("  [v] View    - Show more details")
        print("-" * 70)

        while True:
            try:
                choice = input("Your decision [a/r/m/v]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print("\nApproval cancelled.")
                return ApprovalResponse(
                    request_id=request.request_id,
                    status=ApprovalStatus.REJECTED,
                    notes="Cancelled by user"
                )

            if choice == "a":
                notes = input("Notes (optional, press Enter to skip): ").strip()
                return ApprovalResponse(
                    request_id=request.request_id,
                    status=ApprovalStatus.APPROVED,
                    notes=notes or None
                )

            elif choice == "r":
                notes = input("Reason for rejection: ").strip()
                return ApprovalResponse(
                    request_id=request.request_id,
                    status=ApprovalStatus.REJECTED,
                    notes=notes
                )

            elif choice == "m":
                modified = self._get_modification(request)
                notes = input("Notes on modification: ").strip()
                return ApprovalResponse(
                    request_id=request.request_id,
                    status=ApprovalStatus.MODIFIED,
                    modified_proposal=modified,
                    notes=notes
                )

            elif choice == "v":
                self._display_full_details(request)

            else:
                print("Invalid choice. Please enter 'a', 'r', 'm', or 'v'.")

    def _display_diff(self, original: str, proposed: str) -> None:
        """Display a unified diff between original and proposed content."""
        original_lines = original.splitlines(keepends=True)
        proposed_lines = proposed.splitlines(keepends=True)

        diff = difflib.unified_diff(
            original_lines,
            proposed_lines,
            fromfile="original",
            tofile="proposed",
            lineterm=""
        )

        for line in diff:
            if line.startswith("+") and not line.startswith("+++"):
                print(f"\033[92m{line}\033[0m", end="")  # Green
            elif line.startswith("-") and not line.startswith("---"):
                print(f"\033[91m{line}\033[0m", end="")  # Red
            elif line.startswith("@@"):
                print(f"\033[96m{line}\033[0m", end="")  # Cyan
            else:
                print(line, end="")
        print()

    def _display_full_details(self, request: ApprovalRequest) -> None:
        """Display full details of a request."""
        print("\n--- Full Request Details ---")
        print(f"Request ID: {request.request_id}")
        print(f"Timestamp: {request.timestamp}")
        print(f"Agent ID: {request.agent_id}")
        print(f"Action Type: {request.action_type}")
        print(f"Risk Level: {request.risk_level}")
        print(f"Reversible: {request.reversible}")
        print(f"Description: {request.description}")
        print(f"Context: {request.context}")
        print(f"Proposal: {json.dumps(request.proposal, indent=2, default=str)}")

        if request.original_content:
            print(f"\nOriginal Content Length: {len(request.original_content)} chars")
        if request.proposed_content:
            print(f"Proposed Content Length: {len(request.proposed_content)} chars")
        print()

    def _get_modification(self, request: ApprovalRequest) -> Any:
        """Get user's modification to a proposal."""
        if request.action_type == "run_shell":
            print(f"\nOriginal command: {request.proposal.get('command')}")
            modified = input("Enter modified command: ").strip()
            return {"command": modified}

        elif request.action_type == "edit_file":
            print("\nFor file modifications, you can:")
            print("1. Enter a file path containing the modified content")
            print("2. Enter 'edit' to open in $EDITOR")

            choice = input("Choice: ").strip().lower()

            if choice == "edit":
                import tempfile
                import subprocess
                import os

                # Write proposed content to temp file
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    suffix=".py",
                    delete=False
                ) as f:
                    f.write(request.proposed_content or "")
                    temp_path = f.name

                # Open in editor
                editor = os.environ.get("EDITOR", "nano")
                subprocess.call([editor, temp_path])

                # Read modified content
                with open(temp_path, "r") as f:
                    modified_content = f.read()

                os.unlink(temp_path)
                return {
                    "file_path": request.proposal.get("file_path"),
                    "content": modified_content
                }

            else:
                # Read from specified file
                try:
                    with open(choice, "r") as f:
                        modified_content = f.read()
                    return {
                        "file_path": request.proposal.get("file_path"),
                        "content": modified_content
                    }
                except Exception as e:
                    print(f"Error reading file: {e}")
                    return request.proposal

        else:
            # Generic modification - ask for JSON
            print(f"\nCurrent proposal: {json.dumps(request.proposal, indent=2)}")
            print("Enter modified proposal as JSON (or 'skip' to keep original):")
            modified_json = input().strip()

            if modified_json.lower() == "skip":
                return request.proposal

            try:
                return json.loads(modified_json)
            except json.JSONDecodeError as e:
                print(f"Invalid JSON: {e}")
                return request.proposal

    def get_pending_requests(self) -> List[ApprovalRequest]:
        """Get all pending approval requests."""
        return list(self._pending_requests.values())

    def cancel_request(self, request_id: str) -> bool:
        """Cancel a pending approval request."""
        if request_id in self._pending_requests:
            request = self._pending_requests.pop(request_id)
            self.audit.log(
                action=AuditAction.APPROVAL_DENIED,
                agent_id=request.agent_id,
                input_data={"request_id": request_id},
                output_data={"status": "cancelled"},
                reasoning="Request cancelled"
            )
            return True
        return False
