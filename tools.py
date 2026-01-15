"""Tool definitions and executors for the local agent."""

import os
import glob
import subprocess
from typing import Optional
from pydantic import BaseModel, Field

from config import (
    ALLOWED_SHELL_COMMANDS,
    BLOCKED_PATTERNS,
    PROJECT_DIR,
)


# Tool call schema for Ollama structured outputs
TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "thinking": {
            "type": "string",
            "description": "Your reasoning about what to do next"
        },
        "action": {
            "type": "string",
            "enum": ["read_file", "edit_file", "write_file", "run_shell", "list_files", "respond"],
            "description": "The action to take"
        },
        "path": {
            "type": "string",
            "description": "File path for read_file, edit_file, write_file"
        },
        "pattern": {
            "type": "string",
            "description": "Glob pattern for list_files (e.g., '*.py', 'src/**/*.js')"
        },
        "old_text": {
            "type": "string",
            "description": "Text to find for edit_file"
        },
        "new_text": {
            "type": "string",
            "description": "Replacement text for edit_file"
        },
        "content": {
            "type": "string",
            "description": "Content for write_file"
        },
        "command": {
            "type": "string",
            "description": "Shell command for run_shell"
        },
        "response": {
            "type": "string",
            "description": "Your response to the user when action is 'respond'"
        }
    },
    "required": ["action"]
}


class ToolResult(BaseModel):
    """Result of executing a tool."""
    success: bool
    output: str
    error: Optional[str] = None


def read_file(path: str) -> ToolResult:
    """Read contents of a file."""
    try:
        # Resolve to absolute path
        if not os.path.isabs(path):
            path = os.path.join(PROJECT_DIR, path)

        if not os.path.exists(path):
            return ToolResult(success=False, output="", error=f"File not found: {path}")

        if not os.path.isfile(path):
            return ToolResult(success=False, output="", error=f"Not a file: {path}")

        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            content = f.read()

        # Truncate very large files
        if len(content) > 50000:
            content = content[:50000] + "\n\n... [truncated, file too large]"

        return ToolResult(success=True, output=content)

    except Exception as e:
        return ToolResult(success=False, output="", error=str(e))


def edit_file(path: str, old_text: str, new_text: str) -> ToolResult:
    """Edit a file by replacing old_text with new_text."""
    try:
        if not os.path.isabs(path):
            path = os.path.join(PROJECT_DIR, path)

        if not os.path.exists(path):
            return ToolResult(success=False, output="", error=f"File not found: {path}")

        with open(path, 'r', encoding='utf-8') as f:
            content = f.read()

        if old_text not in content:
            return ToolResult(
                success=False,
                output="",
                error=f"Text to replace not found in file. Make sure old_text matches exactly."
            )

        # Check for multiple occurrences
        count = content.count(old_text)
        if count > 1:
            return ToolResult(
                success=False,
                output="",
                error=f"Found {count} occurrences of old_text. Please provide more context to make it unique."
            )

        new_content = content.replace(old_text, new_text)

        with open(path, 'w', encoding='utf-8') as f:
            f.write(new_content)

        return ToolResult(success=True, output=f"Successfully edited {path}")

    except Exception as e:
        return ToolResult(success=False, output="", error=str(e))


def write_file(path: str, content: str) -> ToolResult:
    """Write content to a file (creates or overwrites)."""
    try:
        if not os.path.isabs(path):
            path = os.path.join(PROJECT_DIR, path)

        # Safety check: warn if outside project directory
        if not path.startswith(PROJECT_DIR):
            return ToolResult(
                success=False,
                output="",
                error=f"Cannot write outside project directory: {PROJECT_DIR}"
            )

        # Create parent directories if needed
        parent_dir = os.path.dirname(path)
        if parent_dir and not os.path.exists(parent_dir):
            os.makedirs(parent_dir)

        with open(path, 'w', encoding='utf-8') as f:
            f.write(content)

        return ToolResult(success=True, output=f"Successfully wrote to {path}")

    except Exception as e:
        return ToolResult(success=False, output="", error=str(e))


def is_command_safe(command: str) -> tuple[bool, str]:
    """Check if a shell command is safe to execute."""
    # Check blocked patterns
    for pattern in BLOCKED_PATTERNS:
        if pattern in command.lower():
            return False, f"Blocked pattern detected: {pattern}"

    # Check if command starts with an allowed program
    cmd_parts = command.strip().split()
    if not cmd_parts:
        return False, "Empty command"

    base_cmd = cmd_parts[0]
    # Handle paths like /usr/bin/python
    base_cmd = os.path.basename(base_cmd)

    if base_cmd not in ALLOWED_SHELL_COMMANDS:
        return False, f"Command '{base_cmd}' not in allowed list. Allowed: {', '.join(ALLOWED_SHELL_COMMANDS)}"

    return True, ""


def run_shell(command: str) -> ToolResult:
    """Execute a shell command with safety checks."""
    try:
        is_safe, reason = is_command_safe(command)
        if not is_safe:
            return ToolResult(success=False, output="", error=f"Command blocked: {reason}")

        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=60,
            cwd=PROJECT_DIR
        )

        output = result.stdout
        if result.stderr:
            output += f"\n[stderr]: {result.stderr}"

        # Truncate long output
        if len(output) > 10000:
            output = output[:10000] + "\n\n... [truncated]"

        if result.returncode != 0:
            return ToolResult(
                success=False,
                output=output,
                error=f"Command exited with code {result.returncode}"
            )

        return ToolResult(success=True, output=output or "(no output)")

    except subprocess.TimeoutExpired:
        return ToolResult(success=False, output="", error="Command timed out after 60 seconds")
    except Exception as e:
        return ToolResult(success=False, output="", error=str(e))


EXCLUDED_DIRS = {'venv', 'node_modules', '.git', '__pycache__', '.pytest_cache', 'dist', 'build', '.egg-info'}


def list_files(pattern: str) -> ToolResult:
    """List files matching a glob pattern."""
    try:
        # Handle relative patterns
        if not os.path.isabs(pattern):
            pattern = os.path.join(PROJECT_DIR, pattern)

        files = glob.glob(pattern, recursive=True)

        # Filter out excluded directories
        files = [f for f in files if not any(excl in f.split(os.sep) for excl in EXCLUDED_DIRS)]

        if not files:
            return ToolResult(success=True, output="No files found matching pattern")

        # Sort and format
        files = sorted(files)

        # Show relative paths
        rel_files = []
        for f in files:
            if f.startswith(PROJECT_DIR):
                rel_files.append(os.path.relpath(f, PROJECT_DIR))
            else:
                rel_files.append(f)

        output = "\n".join(rel_files)

        if len(rel_files) > 100:
            output = "\n".join(rel_files[:100]) + f"\n\n... and {len(rel_files) - 100} more files"

        return ToolResult(success=True, output=output)

    except Exception as e:
        return ToolResult(success=False, output="", error=str(e))


def execute_tool(action: str, **kwargs) -> ToolResult:
    """Execute a tool based on the action name."""
    tools = {
        "read_file": lambda: read_file(kwargs.get("path", "")),
        "edit_file": lambda: edit_file(
            kwargs.get("path", ""),
            kwargs.get("old_text", ""),
            kwargs.get("new_text", "")
        ),
        "write_file": lambda: write_file(
            kwargs.get("path", ""),
            kwargs.get("content", "")
        ),
        "run_shell": lambda: run_shell(kwargs.get("command", "")),
        "list_files": lambda: list_files(kwargs.get("pattern", "*")),
    }

    if action == "respond":
        # Not a tool - this is the model's response to the user
        return ToolResult(success=True, output=kwargs.get("response", ""))

    if action not in tools:
        return ToolResult(success=False, output="", error=f"Unknown action: {action}")

    return tools[action]()
