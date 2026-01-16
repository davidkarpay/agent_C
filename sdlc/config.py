"""
Configuration for the SDLC Framework.

Extends the base agent config with framework-wide settings
for compliance in regulated industries.
"""

import os
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class FrameworkConfig:
    """
    Framework-wide configuration.

    All settings have compliance-safe defaults.
    """
    # Paths
    project_root: str = "/mnt/c/agent_C"
    audit_log_path: str = ""  # Auto-generated if empty

    # Model settings (inherited by agents)
    default_model: str = "gemma2:2b"  # Fast, instruction-following
    complex_model: str = "llama3.1:8b"  # For complex analysis
    ollama_base_url: str = "http://localhost:11434"

    # Compliance settings
    require_approval: bool = True  # ALWAYS require human approval
    temperature: float = 0.0  # MUST be 0 for deterministic outputs
    auto_verify_audit: bool = True  # Verify audit chain on startup
    log_llm_requests: bool = True  # Log all LLM interactions

    # Agent limits
    max_iterations: int = 10
    max_tokens: int = 2048
    context_window: int = 8192

    # Safety
    allowed_shell_commands: List[str] = field(default_factory=lambda: [
        "git", "npm", "npx", "node", "python", "python3", "pip", "pip3",
        "pytest", "poetry", "cargo", "rustc", "go", "make", "cmake",
        "ls", "cat", "head", "tail", "grep", "find", "wc", "diff",
        "mkdir", "cp", "mv", "touch", "echo", "pwd", "which", "env",
    ])

    blocked_patterns: List[str] = field(default_factory=lambda: [
        "rm -rf /", "rm -rf ~", "rm -rf *",
        "sudo", "su ",
        "> /dev/", "| /dev/",
        "mkfs", "dd if=",
        ":(){", "fork",
        "chmod 777",
        "curl | sh", "wget | sh",
    ])

    def __post_init__(self):
        """Initialize computed values."""
        if not self.audit_log_path:
            audit_dir = Path(self.project_root) / ".sdlc" / "audit"
            audit_dir.mkdir(parents=True, exist_ok=True)
            self.audit_log_path = str(audit_dir / "audit.jsonl")

    def validate(self) -> List[str]:
        """Validate configuration for compliance."""
        issues = []

        if self.temperature != 0.0:
            issues.append(
                f"COMPLIANCE WARNING: temperature={self.temperature}. "
                "Must be 0 for deterministic outputs."
            )

        if not self.require_approval:
            issues.append(
                "COMPLIANCE WARNING: require_approval=False. "
                "Human-in-the-loop is required for regulated industries."
            )

        if not self.log_llm_requests:
            issues.append(
                "COMPLIANCE WARNING: log_llm_requests=False. "
                "All LLM interactions should be logged for audit."
            )

        return issues


# Default configuration
DEFAULT_CONFIG = FrameworkConfig()


def get_config(
    project_root: Optional[str] = None,
    **overrides
) -> FrameworkConfig:
    """
    Get framework configuration with optional overrides.

    Args:
        project_root: Override project root path
        **overrides: Additional config overrides

    Returns:
        FrameworkConfig instance
    """
    config_dict = {}

    if project_root:
        config_dict["project_root"] = project_root

    config_dict.update(overrides)

    return FrameworkConfig(**config_dict)
