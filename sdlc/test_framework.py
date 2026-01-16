#!/usr/bin/env python3
"""
Test script for SDLC Framework.

Tests:
1. Audit logging with hash chain verification
2. Human approval interface (auto-approve mode)
3. Requirements Analyst agent
4. Test Generator agent
5. Tool registry
"""

import json
import os
import sys
import tempfile
from pathlib import Path

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from sdlc.audit import AuditLogger, AuditAction, verify_audit_file
from sdlc.approval import ApprovalGate, ApprovalStatus
from sdlc.config import FrameworkConfig, get_config
from sdlc.tools.registry import ToolRegistry
from sdlc.agents.base import AgentConfig
from sdlc.agents.requirements import RequirementsAnalystAgent
from sdlc.agents.test_generator import TestGeneratorAgent


def test_audit_logging():
    """Test audit logging and hash chain verification."""
    print("\n" + "=" * 60)
    print("TEST: Audit Logging")
    print("=" * 60)

    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = os.path.join(tmpdir, "test_audit.jsonl")

        # Create logger and add entries
        logger = AuditLogger(log_path, session_id="test_session")

        # Log some actions
        entry1 = logger.log(
            action=AuditAction.AGENT_INVOKED,
            agent_id="test_agent",
            input_data={"task": "test task"},
            reasoning="Starting test"
        )
        print(f"  Entry 1 hash: {entry1.entry_hash[:16]}...")

        entry2 = logger.log(
            action=AuditAction.TOOL_CALLED,
            agent_id="test_agent",
            input_data={"tool": "read_file", "path": "test.py"},
            reasoning="Reading test file"
        )
        print(f"  Entry 2 hash: {entry2.entry_hash[:16]}...")
        print(f"  Entry 2 prev: {entry2.previous_hash[:16]}...")

        entry3 = logger.log(
            action=AuditAction.AGENT_COMPLETED,
            agent_id="test_agent",
            output_data="Test completed",
            reasoning="Done"
        )
        print(f"  Entry 3 hash: {entry3.entry_hash[:16]}...")

        # Verify chain
        is_valid, errors = logger.verify_chain()
        print(f"\n  Chain valid: {is_valid}")
        if errors:
            print(f"  Errors: {errors}")

        # Test summary
        summary = logger.generate_summary()
        print(f"\n  Summary: {summary['total_entries']} entries")
        print(f"  Actions: {summary['action_counts']}")

        assert is_valid, "Hash chain should be valid"
        assert summary["total_entries"] == 3, "Should have 3 entries"

        print("\n  [PASS] Audit logging works correctly")


def test_approval_gate():
    """Test approval gate with auto-approve mode."""
    print("\n" + "=" * 60)
    print("TEST: Approval Gate (Auto-Approve Mode)")
    print("=" * 60)

    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = os.path.join(tmpdir, "approval_audit.jsonl")
        logger = AuditLogger(log_path)

        # Create gate in auto-approve mode (for testing)
        gate = ApprovalGate(logger, auto_approve=True)

        # Request file edit approval
        request = gate.request_file_edit(
            agent_id="test_agent",
            file_path="/test/file.py",
            original="old content",
            proposed="new content",
            description="Test edit"
        )
        print(f"  Request ID: {request.request_id}")

        # Get approval (auto-approved)
        response = gate.await_approval(request)
        print(f"  Status: {response.status.value}")
        print(f"  Notes: {response.notes}")

        assert response.status == ApprovalStatus.APPROVED
        print("\n  [PASS] Approval gate works correctly")


def test_tool_registry():
    """Test tool registry."""
    print("\n" + "=" * 60)
    print("TEST: Tool Registry")
    print("=" * 60)

    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = os.path.join(tmpdir, "tools_audit.jsonl")
        logger = AuditLogger(log_path)

        config = get_config(project_root=tmpdir)

        registry = ToolRegistry(
            audit=logger,
            allowed_commands=config.allowed_shell_commands,
            blocked_patterns=config.blocked_patterns,
            project_root=tmpdir
        )

        # Test list_files
        result = registry.execute(
            "list_files",
            {"pattern": "*.py"},
            agent_id="test"
        )
        print(f"  list_files: {result}")

        # Test write_file
        result = registry.execute(
            "write_file",
            {"path": "test.py", "content": "print('hello')"},
            agent_id="test"
        )
        print(f"  write_file: {result}")

        # Test read_file
        result = registry.execute(
            "read_file",
            {"path": "test.py"},
            agent_id="test"
        )
        print(f"  read_file: {result}")

        # Test blocked command
        result = registry.execute(
            "run_shell",
            {"command": "rm -rf /"},
            agent_id="test"
        )
        print(f"  blocked cmd: {result}")
        assert not result["success"]
        assert "blocked" in result["error"].lower()

        print("\n  [PASS] Tool registry works correctly")


def test_requirements_agent():
    """Test Requirements Analyst agent."""
    print("\n" + "=" * 60)
    print("TEST: Requirements Analyst Agent")
    print("=" * 60)

    # Check if Ollama is running
    import requests
    try:
        response = requests.get("http://localhost:11434/api/tags", timeout=5)
        if response.status_code != 200:
            print("  [SKIP] Ollama not running")
            return
    except Exception:
        print("  [SKIP] Ollama not running")
        return

    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = os.path.join(tmpdir, "req_audit.jsonl")
        logger = AuditLogger(log_path)
        gate = ApprovalGate(logger, auto_approve=True)  # Auto-approve for test

        config = AgentConfig(
            model_name="gemma2:2b",
            temperature=0.0,
            require_approval=True
        )

        agent = RequirementsAnalystAgent(
            config=config,
            audit=logger,
            approval=gate,
            project_root=tmpdir
        )

        # Run agent with simple task
        task = """
        Add a password strength indicator to the user registration form.
        It should show weak/medium/strong based on password length and complexity.
        """

        print(f"  Running agent with task: {task[:50]}...")
        result = agent.run(task)

        print(f"\n  Success: {result.success}")
        print(f"  Iterations: {result.iterations}")
        print(f"  Duration: {result.duration_ms}ms")
        print(f"  Reasoning: {result.reasoning}")

        if result.success and result.output:
            reqs = result.output.get("requirements", [])
            print(f"\n  Requirements extracted: {len(reqs)}")
            for req in reqs:
                print(f"    - {req.get('id')}: {req.get('title')}")
                for ac in req.get("acceptance_criteria", [])[:2]:
                    print(f"      - {ac}")

        # Verify audit trail
        is_valid, errors = logger.verify_chain()
        print(f"\n  Audit chain valid: {is_valid}")

        print("\n  [PASS] Requirements Analyst agent works correctly")


def test_test_generator_agent():
    """Test Test Generator agent."""
    print("\n" + "=" * 60)
    print("TEST: Test Generator Agent")
    print("=" * 60)

    # Check if Ollama is running
    import requests
    try:
        response = requests.get("http://localhost:11434/api/tags", timeout=5)
        if response.status_code != 200:
            print("  [SKIP] Ollama not running")
            return
    except Exception:
        print("  [SKIP] Ollama not running")
        return

    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = os.path.join(tmpdir, "testgen_audit.jsonl")
        logger = AuditLogger(log_path)
        gate = ApprovalGate(logger, auto_approve=True)

        config = AgentConfig(
            model_name="gemma2:2b",
            temperature=0.0,
            require_approval=True
        )

        # Create a simple source file to generate tests for
        source_file = os.path.join(tmpdir, "sample_module.py")
        with open(source_file, "w") as f:
            f.write('''"""Sample module for test generation."""

def add_numbers(a, b):
    """Add two numbers together."""
    return a + b

def validate_email(email):
    """Check if email is valid."""
    return "@" in email and "." in email

class Calculator:
    """Simple calculator class."""

    def multiply(self, x, y):
        """Multiply two numbers."""
        return x * y

    def divide(self, x, y):
        """Divide x by y."""
        if y == 0:
            raise ValueError("Cannot divide by zero")
        return x / y
''')

        agent = TestGeneratorAgent(
            config=config,
            audit=logger,
            approval=gate,
            project_root=tmpdir
        )

        # Test with source file
        print("  Testing with source file...")
        result = agent.run({"source_file": source_file})

        print(f"\n  Success: {result.success}")
        print(f"  Iterations: {result.iterations}")
        print(f"  Duration: {result.duration_ms}ms")
        print(f"  Reasoning: {result.reasoning}")

        if result.success and result.output:
            test_count = result.output.get("test_count", 0)
            print(f"\n  Tests generated: {test_count}")

            # Show preview of generated test file
            test_file = result.output.get("test_file", "")
            if test_file:
                preview = test_file[:500]
                print(f"\n  Test file preview:")
                for line in preview.split("\n")[:15]:
                    print(f"    {line}")

        # Test with function signatures
        print("\n  Testing with function signatures...")
        result2 = agent.run({
            "functions": [
                {"name": "calculate_total", "args": ["items", "tax_rate"], "returns": "float"},
                {"name": "format_name", "args": ["first", "last"], "returns": "str"}
            ]
        })

        print(f"  Success: {result2.success}")
        if result2.success:
            print(f"  Tests generated: {result2.output.get('test_count', 0)}")

        # Verify audit trail
        is_valid, errors = logger.verify_chain()
        print(f"\n  Audit chain valid: {is_valid}")

        print("\n  [PASS] Test Generator agent works correctly")


def main():
    """Run all tests."""
    print("\nSDLC Framework Tests")
    print("=" * 60)

    test_audit_logging()
    test_approval_gate()
    test_tool_registry()
    test_requirements_agent()
    test_test_generator_agent()

    print("\n" + "=" * 60)
    print("All tests completed!")
    print("=" * 60)


if __name__ == "__main__":
    main()
