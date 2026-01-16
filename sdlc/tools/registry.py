"""
Tool Registry for SDLC Agents.

Provides shared tools that all agents can use:
- File operations (read, write, edit)
- Shell commands (with allowlist)
- Code analysis (grep, find)

All tools integrate with audit logging for compliance.
"""

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ..audit import AuditLogger, AuditAction


@dataclass
class Tool:
    """
    A tool that agents can use.

    Attributes:
        name: Unique identifier
        description: What the tool does
        parameters: JSON schema for parameters
        requires_approval: Whether human approval is needed
        executor: Function that executes the tool
    """
    name: str
    description: str
    parameters: dict
    requires_approval: bool
    executor: Callable


class ToolRegistry:
    """
    Registry of available tools for agents.

    Provides a centralized place to define and manage tools,
    ensuring consistent behavior and audit logging.

    Usage:
        registry = ToolRegistry(audit_logger, allowed_commands, blocked_patterns)

        # Use a tool
        result = registry.execute("read_file", {"path": "config.py"})
    """

    def __init__(
        self,
        audit: AuditLogger,
        allowed_commands: List[str],
        blocked_patterns: List[str],
        project_root: Optional[str] = None
    ):
        """
        Initialize tool registry.

        Args:
            audit: Audit logger for recording tool usage
            allowed_commands: Shell commands that are allowed
            blocked_patterns: Patterns that block shell execution
            project_root: Root directory for file operations
        """
        self.audit = audit
        self.allowed_commands = allowed_commands
        self.blocked_patterns = blocked_patterns
        self.project_root = Path(project_root) if project_root else Path.cwd()
        self._tools: Dict[str, Tool] = {}

        # Register built-in tools
        self._register_builtin_tools()

    def _register_builtin_tools(self) -> None:
        """Register all built-in tools."""

        # Read file
        self.register(Tool(
            name="read_file",
            description="Read contents of a file",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to file"}
                },
                "required": ["path"]
            },
            requires_approval=False,
            executor=self._read_file
        ))

        # Write file
        self.register(Tool(
            name="write_file",
            description="Write content to a file (creates or overwrites)",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to file"},
                    "content": {"type": "string", "description": "Content to write"}
                },
                "required": ["path", "content"]
            },
            requires_approval=True,
            executor=self._write_file
        ))

        # Edit file
        self.register(Tool(
            name="edit_file",
            description="Edit a file by replacing text",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to file"},
                    "old_text": {"type": "string", "description": "Text to find"},
                    "new_text": {"type": "string", "description": "Text to replace with"}
                },
                "required": ["path", "old_text", "new_text"]
            },
            requires_approval=True,
            executor=self._edit_file
        ))

        # List files
        self.register(Tool(
            name="list_files",
            description="List files matching a pattern",
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Glob pattern (e.g., **/*.py)"},
                    "path": {"type": "string", "description": "Starting directory"}
                },
                "required": ["pattern"]
            },
            requires_approval=False,
            executor=self._list_files
        ))

        # Run shell command
        self.register(Tool(
            name="run_shell",
            description="Run a shell command",
            parameters={
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Command to run"}
                },
                "required": ["command"]
            },
            requires_approval=True,
            executor=self._run_shell
        ))

        # Search files (grep)
        self.register(Tool(
            name="search_files",
            description="Search for pattern in files",
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Regex pattern to search"},
                    "path": {"type": "string", "description": "Directory to search"},
                    "file_pattern": {"type": "string", "description": "File glob pattern"}
                },
                "required": ["pattern"]
            },
            requires_approval=False,
            executor=self._search_files
        ))

    def register(self, tool: Tool) -> None:
        """Register a tool."""
        self._tools[tool.name] = tool

    def get_tool(self, name: str) -> Optional[Tool]:
        """Get a tool by name."""
        return self._tools.get(name)

    def list_tools(self) -> List[Tool]:
        """List all registered tools."""
        return list(self._tools.values())

    def get_schemas(self) -> Dict[str, dict]:
        """Get parameter schemas for all tools."""
        return {
            name: tool.parameters
            for name, tool in self._tools.items()
        }

    def execute(
        self,
        tool_name: str,
        args: dict,
        agent_id: str = "unknown"
    ) -> dict:
        """
        Execute a tool.

        Args:
            tool_name: Name of tool to execute
            args: Arguments for the tool
            agent_id: ID of calling agent (for audit)

        Returns:
            Dict with "success" and "result" or "error"
        """
        tool = self.get_tool(tool_name)
        if not tool:
            return {"success": False, "error": f"Unknown tool: {tool_name}"}

        # Log tool call
        self.audit.log(
            action=AuditAction.TOOL_CALLED,
            agent_id=agent_id,
            input_data={"tool": tool_name, "args": args},
            reasoning=f"Executing tool {tool_name}"
        )

        try:
            result = tool.executor(**args)

            # Log result
            self.audit.log(
                action=AuditAction.TOOL_RESULT,
                agent_id=agent_id,
                input_data={"tool": tool_name},
                output_data={"success": True, "result_preview": str(result)[:200]},
                reasoning=f"Tool {tool_name} completed successfully"
            )

            return {"success": True, "result": result}

        except Exception as e:
            # Log error
            self.audit.log(
                action=AuditAction.TOOL_RESULT,
                agent_id=agent_id,
                input_data={"tool": tool_name},
                output_data={"success": False, "error": str(e)},
                reasoning=f"Tool {tool_name} failed",
                success=False,
                error_message=str(e)
            )

            return {"success": False, "error": str(e)}

    # Tool executors

    def _read_file(self, path: str) -> str:
        """Read file contents."""
        file_path = self._resolve_path(path)
        if not file_path.exists():
            raise FileNotFoundError(f"File not found: {path}")
        return file_path.read_text(encoding="utf-8")

    def _write_file(self, path: str, content: str) -> str:
        """Write content to file."""
        file_path = self._resolve_path(path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
        return f"Wrote {len(content)} characters to {path}"

    def _edit_file(self, path: str, old_text: str, new_text: str) -> str:
        """Edit file by replacing text."""
        file_path = self._resolve_path(path)
        if not file_path.exists():
            raise FileNotFoundError(f"File not found: {path}")

        content = file_path.read_text(encoding="utf-8")

        if old_text not in content:
            raise ValueError(f"Text not found in file: {old_text[:50]}...")

        count = content.count(old_text)
        if count > 1:
            raise ValueError(
                f"Found {count} occurrences of text. "
                "Please provide more context to make it unique."
            )

        new_content = content.replace(old_text, new_text)
        file_path.write_text(new_content, encoding="utf-8")

        return f"Replaced text in {path}"

    def _list_files(self, pattern: str, path: Optional[str] = None) -> List[str]:
        """List files matching pattern."""
        search_path = self._resolve_path(path) if path else self.project_root

        excluded = {
            "venv", "node_modules", ".git", "__pycache__",
            ".pytest_cache", "dist", "build", ".egg-info"
        }

        results = []
        for file_path in search_path.rglob(pattern.lstrip("*/")):
            if any(ex in file_path.parts for ex in excluded):
                continue
            try:
                rel_path = file_path.relative_to(self.project_root)
                results.append(str(rel_path))
            except ValueError:
                results.append(str(file_path))

        return sorted(results)

    def _run_shell(self, command: str) -> str:
        """Run shell command with safety checks."""
        # Check blocked patterns
        for pattern in self.blocked_patterns:
            if pattern in command:
                raise ValueError(f"Command blocked: contains '{pattern}'")

        # Check if command starts with allowed command
        cmd_parts = command.split()
        if not cmd_parts:
            raise ValueError("Empty command")

        base_cmd = cmd_parts[0]
        if base_cmd not in self.allowed_commands:
            raise ValueError(
                f"Command not allowed: {base_cmd}. "
                f"Allowed: {', '.join(self.allowed_commands)}"
            )

        # Run command
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(self.project_root)
        )

        output = result.stdout
        if result.stderr:
            output += f"\nSTDERR: {result.stderr}"
        if result.returncode != 0:
            output += f"\nReturn code: {result.returncode}"

        return output

    def _search_files(
        self,
        pattern: str,
        path: Optional[str] = None,
        file_pattern: str = "*.py"
    ) -> List[dict]:
        """Search for pattern in files."""
        import re

        search_path = self._resolve_path(path) if path else self.project_root
        regex = re.compile(pattern, re.IGNORECASE)

        excluded = {
            "venv", "node_modules", ".git", "__pycache__",
            ".pytest_cache", "dist", "build", ".egg-info"
        }

        results = []
        for file_path in search_path.rglob(file_pattern):
            if any(ex in file_path.parts for ex in excluded):
                continue

            try:
                content = file_path.read_text(encoding="utf-8", errors="ignore")
                for i, line in enumerate(content.splitlines(), 1):
                    if regex.search(line):
                        try:
                            rel_path = str(file_path.relative_to(self.project_root))
                        except ValueError:
                            rel_path = str(file_path)
                        results.append({
                            "file": rel_path,
                            "line": i,
                            "content": line.strip()[:200]
                        })
            except Exception:
                continue

        return results[:100]  # Limit results

    def _resolve_path(self, path: str) -> Path:
        """Resolve path relative to project root."""
        p = Path(path)
        if p.is_absolute():
            return p
        return self.project_root / p
