"""
Immutable Audit Logging for Regulated Industries.

Provides tamper-evident audit trails with:
- SHA-256 hash chain verification (each entry includes hash of previous)
- Immutable entries with timestamp, agent, action, input, output, reasoning
- JSON Lines format for compliance-ready export
- Verification functions to detect tampering
"""

import hashlib
import json
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, List, Iterator
from enum import Enum


class AuditAction(Enum):
    """Types of auditable actions."""
    AGENT_INVOKED = "agent_invoked"
    AGENT_COMPLETED = "agent_completed"
    AGENT_FAILED = "agent_failed"
    TOOL_CALLED = "tool_called"
    TOOL_RESULT = "tool_result"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_GRANTED = "approval_granted"
    APPROVAL_DENIED = "approval_denied"
    APPROVAL_MODIFIED = "approval_modified"
    LLM_REQUEST = "llm_request"
    LLM_RESPONSE = "llm_response"
    USER_INPUT = "user_input"
    SYSTEM_OUTPUT = "system_output"


@dataclass(frozen=True)
class AuditEntry:
    """
    Immutable audit log entry.

    The frozen=True ensures entries cannot be modified after creation,
    which is critical for compliance with legal audit requirements.
    """
    # Core fields
    timestamp: str  # ISO 8601 format in UTC
    action: str  # AuditAction value
    agent_id: str  # Which agent performed the action
    session_id: str  # Groups related actions

    # Content fields
    input_data: Optional[str] = None  # What was provided to the action
    output_data: Optional[str] = None  # What was produced
    reasoning: Optional[str] = None  # Why this action was taken (for transparency)

    # Chain fields
    sequence_num: int = 0  # Order within session
    previous_hash: str = ""  # SHA-256 of previous entry (empty for first)
    entry_hash: str = ""  # SHA-256 of this entry (computed)

    # Metadata
    model_name: Optional[str] = None  # LLM model used, if any
    duration_ms: Optional[int] = None  # Execution time
    success: bool = True  # Whether action succeeded
    error_message: Optional[str] = None  # Error details if failed

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "AuditEntry":
        """Create from dictionary."""
        return cls(**data)


def compute_entry_hash(entry: AuditEntry) -> str:
    """
    Compute SHA-256 hash of an audit entry.

    The hash includes all fields EXCEPT entry_hash itself,
    which allows verification after the hash is stored.
    """
    # Create dict without the hash field
    data = entry.to_dict()
    data.pop("entry_hash", None)

    # Canonical JSON serialization (sorted keys, no extra whitespace)
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"))

    # SHA-256 hash
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class AuditLogger:
    """
    Immutable audit logger with hash chain verification.

    Each entry includes the SHA-256 hash of the previous entry,
    creating a tamper-evident chain. Any modification to historical
    entries will break the hash chain and be detectable.

    Usage:
        logger = AuditLogger("/path/to/audit.jsonl")
        logger.log(
            action=AuditAction.TOOL_CALLED,
            agent_id="requirements_analyst",
            input_data='{"tool": "read_file", "path": "config.py"}',
            reasoning="Need to read config to understand project settings"
        )

        # Verify integrity
        is_valid, errors = logger.verify_chain()
    """

    def __init__(
        self,
        log_path: str,
        session_id: Optional[str] = None,
        auto_verify: bool = True
    ):
        """
        Initialize audit logger.

        Args:
            log_path: Path to JSON Lines audit file
            session_id: Unique session identifier (auto-generated if not provided)
            auto_verify: Verify chain integrity on startup
        """
        self.log_path = Path(log_path)
        self.session_id = session_id or self._generate_session_id()
        self._sequence_num = 0
        self._last_hash = ""

        # Ensure log directory exists
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

        # Load existing entries to continue chain
        if self.log_path.exists():
            self._load_chain_state()
            if auto_verify:
                is_valid, errors = self.verify_chain()
                if not is_valid:
                    raise ValueError(
                        f"Audit log integrity check failed: {errors}"
                    )

    def _generate_session_id(self) -> str:
        """Generate unique session ID."""
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        random_suffix = hashlib.sha256(
            os.urandom(32)
        ).hexdigest()[:8]
        return f"session_{timestamp}_{random_suffix}"

    def _load_chain_state(self) -> None:
        """Load last entry's hash and sequence number to continue chain."""
        last_entry = None
        for entry in self._read_entries():
            last_entry = entry

        if last_entry:
            self._sequence_num = last_entry.sequence_num
            self._last_hash = last_entry.entry_hash

    def _read_entries(self) -> Iterator[AuditEntry]:
        """Read all entries from log file."""
        if not self.log_path.exists():
            return

        with open(self.log_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    data = json.loads(line)
                    yield AuditEntry.from_dict(data)

    def log(
        self,
        action: AuditAction,
        agent_id: str,
        input_data: Optional[Any] = None,
        output_data: Optional[Any] = None,
        reasoning: Optional[str] = None,
        model_name: Optional[str] = None,
        duration_ms: Optional[int] = None,
        success: bool = True,
        error_message: Optional[str] = None
    ) -> AuditEntry:
        """
        Log an auditable action.

        Args:
            action: Type of action being logged
            agent_id: Identifier of the agent performing action
            input_data: Input to the action (will be JSON serialized)
            output_data: Output from the action (will be JSON serialized)
            reasoning: Explanation of why this action was taken
            model_name: LLM model name if applicable
            duration_ms: Execution time in milliseconds
            success: Whether the action succeeded
            error_message: Error details if action failed

        Returns:
            The created AuditEntry
        """
        self._sequence_num += 1

        # Serialize complex data types
        if input_data is not None and not isinstance(input_data, str):
            input_data = json.dumps(input_data, default=str)
        if output_data is not None and not isinstance(output_data, str):
            output_data = json.dumps(output_data, default=str)

        # Create entry without hash first
        entry = AuditEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            action=action.value,
            agent_id=agent_id,
            session_id=self.session_id,
            input_data=input_data,
            output_data=output_data,
            reasoning=reasoning,
            sequence_num=self._sequence_num,
            previous_hash=self._last_hash,
            entry_hash="",  # Placeholder
            model_name=model_name,
            duration_ms=duration_ms,
            success=success,
            error_message=error_message
        )

        # Compute hash
        entry_hash = compute_entry_hash(entry)

        # Create final entry with hash (need new instance due to frozen)
        final_entry = AuditEntry(
            timestamp=entry.timestamp,
            action=entry.action,
            agent_id=entry.agent_id,
            session_id=entry.session_id,
            input_data=entry.input_data,
            output_data=entry.output_data,
            reasoning=entry.reasoning,
            sequence_num=entry.sequence_num,
            previous_hash=entry.previous_hash,
            entry_hash=entry_hash,
            model_name=entry.model_name,
            duration_ms=entry.duration_ms,
            success=entry.success,
            error_message=entry.error_message
        )

        # Append to log file
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(final_entry.to_dict()) + "\n")

        # Update chain state
        self._last_hash = entry_hash

        return final_entry

    def verify_chain(self) -> tuple[bool, List[str]]:
        """
        Verify the integrity of the audit log chain.

        Returns:
            Tuple of (is_valid, list_of_errors)
        """
        errors = []
        previous_hash = ""
        expected_sequence = 0

        for entry in self._read_entries():
            expected_sequence += 1

            # Check sequence continuity
            if entry.sequence_num != expected_sequence:
                errors.append(
                    f"Sequence gap at {entry.sequence_num}, expected {expected_sequence}"
                )

            # Check previous hash linkage
            if entry.previous_hash != previous_hash:
                errors.append(
                    f"Hash chain broken at sequence {entry.sequence_num}: "
                    f"expected previous_hash '{previous_hash[:16]}...', "
                    f"got '{entry.previous_hash[:16]}...'"
                )

            # Verify entry's own hash
            computed_hash = compute_entry_hash(entry)
            if computed_hash != entry.entry_hash:
                errors.append(
                    f"Entry tampered at sequence {entry.sequence_num}: "
                    f"computed hash '{computed_hash[:16]}...', "
                    f"stored hash '{entry.entry_hash[:16]}...'"
                )

            previous_hash = entry.entry_hash

        return (len(errors) == 0, errors)

    def get_entries(
        self,
        session_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        action: Optional[AuditAction] = None,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None
    ) -> List[AuditEntry]:
        """
        Query audit entries with filters.

        Args:
            session_id: Filter by session
            agent_id: Filter by agent
            action: Filter by action type
            since: Filter entries after this time
            until: Filter entries before this time

        Returns:
            List of matching entries
        """
        results = []

        for entry in self._read_entries():
            # Apply filters
            if session_id and entry.session_id != session_id:
                continue
            if agent_id and entry.agent_id != agent_id:
                continue
            if action and entry.action != action.value:
                continue
            if since:
                entry_time = datetime.fromisoformat(entry.timestamp)
                if entry_time < since:
                    continue
            if until:
                entry_time = datetime.fromisoformat(entry.timestamp)
                if entry_time > until:
                    continue

            results.append(entry)

        return results

    def export_session(
        self,
        session_id: Optional[str] = None,
        output_path: Optional[str] = None
    ) -> str:
        """
        Export a session's audit trail to a standalone file.

        Args:
            session_id: Session to export (defaults to current)
            output_path: Output file path (auto-generated if not provided)

        Returns:
            Path to exported file
        """
        session_id = session_id or self.session_id
        entries = self.get_entries(session_id=session_id)

        if not output_path:
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            output_path = str(
                self.log_path.parent / f"audit_export_{session_id}_{timestamp}.jsonl"
            )

        with open(output_path, "w", encoding="utf-8") as f:
            for entry in entries:
                f.write(json.dumps(entry.to_dict()) + "\n")

        return output_path

    def generate_summary(self, session_id: Optional[str] = None) -> dict:
        """
        Generate a summary of audit activity.

        Args:
            session_id: Session to summarize (defaults to current)

        Returns:
            Summary dictionary with statistics
        """
        entries = self.get_entries(session_id=session_id or self.session_id)

        if not entries:
            return {"total_entries": 0}

        action_counts = {}
        agent_counts = {}
        total_duration = 0
        failed_count = 0

        for entry in entries:
            # Count by action
            action_counts[entry.action] = action_counts.get(entry.action, 0) + 1

            # Count by agent
            agent_counts[entry.agent_id] = agent_counts.get(entry.agent_id, 0) + 1

            # Sum durations
            if entry.duration_ms:
                total_duration += entry.duration_ms

            # Count failures
            if not entry.success:
                failed_count += 1

        return {
            "session_id": session_id or self.session_id,
            "total_entries": len(entries),
            "first_timestamp": entries[0].timestamp,
            "last_timestamp": entries[-1].timestamp,
            "action_counts": action_counts,
            "agent_counts": agent_counts,
            "total_duration_ms": total_duration,
            "failed_count": failed_count,
            "success_rate": (len(entries) - failed_count) / len(entries) * 100
        }


def verify_audit_file(log_path: str) -> tuple[bool, List[str]]:
    """
    Standalone function to verify an audit log file.

    Args:
        log_path: Path to JSON Lines audit file

    Returns:
        Tuple of (is_valid, list_of_errors)
    """
    logger = AuditLogger(log_path, auto_verify=False)
    return logger.verify_chain()
