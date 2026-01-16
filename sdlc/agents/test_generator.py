"""
Test Generator Agent.

SDLC Phase: Implementation

Capabilities:
- Generate pytest test skeletons from source code files
- Create tests from requirements documents with acceptance criteria
- Generate tests from function signatures
- Produce structured test files with fixtures and parametrization

Human Gate: Review and approve generated tests before writing to disk.
"""

import ast
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .base import BaseAgent, AgentConfig, AgentResult, AgentPhase
from ..audit import AuditLogger
from ..approval import ApprovalGate, ApprovalStatus


@dataclass
class TestCase:
    """A single test case."""
    name: str  # test_function_basic
    description: str  # What this test verifies
    function_under_test: str  # The function being tested
    test_type: str  # unit, edge_case, parametrized, error
    setup: Optional[str] = None  # Setup code if needed
    assertions: List[str] = field(default_factory=list)  # Expected assertions
    acceptance_criteria: Optional[str] = None  # Link to requirement


@dataclass
class TestFile:
    """Complete test file structure."""
    module_name: str  # test_utils
    source_module: str  # utils
    imports: List[str]  # Required imports
    fixtures: List[str]  # Pytest fixtures
    test_cases: List[TestCase]  # Test cases


class TestGeneratorAgent(BaseAgent):
    """
    Agent for generating pytest test skeletons.

    Takes source code, requirements documents, or function signatures
    and produces pytest test files with test skeletons.
    """

    agent_id = "test_generator"
    phase = AgentPhase.IMPLEMENTATION
    description = "Generates pytest test skeletons from source code or requirements"

    # Schema for LLM structured output
    TEST_SCHEMA = {
        "type": "object",
        "properties": {
            "module_name": {"type": "string"},
            "imports": {"type": "array", "items": {"type": "string"}},
            "fixtures": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "description": {"type": "string"},
                        "code": {"type": "string"}
                    },
                    "required": ["name", "description"]
                }
            },
            "test_cases": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "description": {"type": "string"},
                        "function_under_test": {"type": "string"},
                        "test_type": {
                            "type": "string",
                            "enum": ["unit", "edge_case", "parametrized", "error"]
                        },
                        "assertions": {"type": "array", "items": {"type": "string"}}
                    },
                    "required": ["name", "description", "function_under_test", "test_type"]
                }
            }
        },
        "required": ["module_name", "test_cases"]
    }

    SYSTEM_PROMPT = """You are a Test Engineer generating pytest test skeletons.

For each function/class provided, generate test cases that cover:
1. A basic "happy path" test - normal expected usage
2. Edge case tests - empty input, None values, boundary conditions
3. Error condition tests - invalid input that should raise exceptions

Output test case specifications as JSON. Each test should:
- Have a clear, descriptive name following pytest convention (test_function_scenario)
- Include a description explaining what it verifies
- Specify the function being tested
- List expected assertions as strings (these become TODO comments)
- Reference acceptance criteria when provided

IMPORTANT: Focus on TEST STRUCTURE, not implementation details.
Generate test skeletons that humans will fill in with actual test logic.

Keep test names concise and descriptive. Generate 2-4 tests per function."""

    def __init__(
        self,
        config: AgentConfig,
        audit: AuditLogger,
        approval: ApprovalGate,
        project_root: Optional[str] = None
    ):
        """
        Initialize Test Generator.

        Args:
            config: Agent configuration
            audit: Audit logger
            approval: Approval gate
            project_root: Root directory of project
        """
        super().__init__(config, audit, approval)
        self.project_root = Path(project_root) if project_root else Path.cwd()

    def execute(self, task_input: Any) -> AgentResult:
        """
        Generate test skeletons based on input.

        Args:
            task_input: Can be:
                - dict with "source_file": path to Python file
                - dict with "requirements": requirements document
                - dict with "functions": list of function signatures

        Returns:
            AgentResult containing generated test file content
        """
        # Determine input type and extract testable items
        if isinstance(task_input, dict):
            if "source_file" in task_input:
                return self._generate_from_source(task_input)
            elif "requirements" in task_input:
                return self._generate_from_requirements(task_input)
            elif "functions" in task_input:
                return self._generate_from_signatures(task_input)

        return AgentResult(
            agent_id=self.agent_id,
            success=False,
            output=None,
            reasoning="Invalid input format. Expected dict with 'source_file', 'requirements', or 'functions' key."
        )

    def _generate_from_source(self, task_input: dict) -> AgentResult:
        """Generate tests from a source code file."""
        source_path = task_input.get("source_file")
        test_output = task_input.get("test_output")

        # Resolve path
        if not Path(source_path).is_absolute():
            source_path = self.project_root / source_path

        source_path = Path(source_path)
        if not source_path.exists():
            return AgentResult(
                agent_id=self.agent_id,
                success=False,
                output=None,
                reasoning=f"Source file not found: {source_path}"
            )

        # Read and parse source code
        source_code = source_path.read_text(encoding="utf-8")

        # Extract functions and classes using AST
        testables = self._extract_testables(source_code, source_path.stem)

        if not testables:
            return AgentResult(
                agent_id=self.agent_id,
                success=False,
                output=None,
                reasoning=f"No testable functions or classes found in {source_path}"
            )

        # Build prompt for LLM
        prompt = self._build_source_prompt(source_path.stem, testables, source_code)

        # Generate test specs via LLM
        try:
            test_specs = self.call_llm_structured(
                prompt=prompt,
                schema=self.TEST_SCHEMA,
                system_prompt=self.SYSTEM_PROMPT
            )
        except Exception as e:
            return AgentResult(
                agent_id=self.agent_id,
                success=False,
                output=None,
                reasoning=f"Failed to generate test specs: {str(e)}",
                error_message=str(e),
                error_type=type(e).__name__
            )

        # Build test file content
        test_file = self._build_test_file(test_specs, source_path.stem)

        # Generate output path
        if not test_output:
            test_output = f"test_{source_path.stem}.py"

        # Request approval
        approval_response = self._request_test_approval(test_file, str(source_path))

        if approval_response.status == ApprovalStatus.REJECTED:
            return AgentResult(
                agent_id=self.agent_id,
                success=False,
                output={"test_file": test_file, "output_path": test_output},
                reasoning="Generated tests rejected by reviewer"
            )

        return AgentResult(
            agent_id=self.agent_id,
            success=True,
            output={
                "test_file": test_file,
                "output_path": test_output,
                "test_count": len(test_specs.get("test_cases", [])),
                "source_file": str(source_path)
            },
            reasoning=f"Generated {len(test_specs.get('test_cases', []))} test cases for {source_path.stem}"
        )

    def _generate_from_requirements(self, task_input: dict) -> AgentResult:
        """Generate tests from a requirements document."""
        requirements = task_input.get("requirements", {})
        test_output = task_input.get("test_output", "test_requirements.py")

        # Extract acceptance criteria
        criteria = []
        for req in requirements.get("requirements", []):
            req_id = req.get("id", "REQ-???")
            req_title = req.get("title", "Unknown")
            for ac in req.get("acceptance_criteria", []):
                criteria.append({
                    "requirement_id": req_id,
                    "requirement_title": req_title,
                    "criterion": ac
                })

        if not criteria:
            return AgentResult(
                agent_id=self.agent_id,
                success=False,
                output=None,
                reasoning="No acceptance criteria found in requirements document"
            )

        # Build prompt
        prompt = self._build_requirements_prompt(requirements.get("title", "Feature"), criteria)

        # Generate test specs
        try:
            test_specs = self.call_llm_structured(
                prompt=prompt,
                schema=self.TEST_SCHEMA,
                system_prompt=self.SYSTEM_PROMPT
            )
        except Exception as e:
            return AgentResult(
                agent_id=self.agent_id,
                success=False,
                output=None,
                reasoning=f"Failed to generate test specs: {str(e)}",
                error_message=str(e),
                error_type=type(e).__name__
            )

        # Build test file
        test_file = self._build_test_file(test_specs, "requirements")

        # Request approval
        approval_response = self._request_test_approval(test_file, "requirements document")

        if approval_response.status == ApprovalStatus.REJECTED:
            return AgentResult(
                agent_id=self.agent_id,
                success=False,
                output={"test_file": test_file, "output_path": test_output},
                reasoning="Generated tests rejected by reviewer"
            )

        return AgentResult(
            agent_id=self.agent_id,
            success=True,
            output={
                "test_file": test_file,
                "output_path": test_output,
                "test_count": len(test_specs.get("test_cases", [])),
                "criteria_count": len(criteria)
            },
            reasoning=f"Generated {len(test_specs.get('test_cases', []))} tests from {len(criteria)} acceptance criteria"
        )

    def _generate_from_signatures(self, task_input: dict) -> AgentResult:
        """Generate tests from function signatures."""
        functions = task_input.get("functions", [])
        test_output = task_input.get("test_output", "test_functions.py")

        if not functions:
            return AgentResult(
                agent_id=self.agent_id,
                success=False,
                output=None,
                reasoning="No function signatures provided"
            )

        # Build prompt
        prompt = self._build_signatures_prompt(functions)

        # Generate test specs
        try:
            test_specs = self.call_llm_structured(
                prompt=prompt,
                schema=self.TEST_SCHEMA,
                system_prompt=self.SYSTEM_PROMPT
            )
        except Exception as e:
            return AgentResult(
                agent_id=self.agent_id,
                success=False,
                output=None,
                reasoning=f"Failed to generate test specs: {str(e)}",
                error_message=str(e),
                error_type=type(e).__name__
            )

        # Build test file
        test_file = self._build_test_file(test_specs, "functions")

        # Request approval
        approval_response = self._request_test_approval(test_file, "function signatures")

        if approval_response.status == ApprovalStatus.REJECTED:
            return AgentResult(
                agent_id=self.agent_id,
                success=False,
                output={"test_file": test_file, "output_path": test_output},
                reasoning="Generated tests rejected by reviewer"
            )

        return AgentResult(
            agent_id=self.agent_id,
            success=True,
            output={
                "test_file": test_file,
                "output_path": test_output,
                "test_count": len(test_specs.get("test_cases", [])),
                "function_count": len(functions)
            },
            reasoning=f"Generated {len(test_specs.get('test_cases', []))} tests for {len(functions)} functions"
        )

    def _extract_testables(self, source_code: str, module_name: str) -> List[dict]:
        """Extract functions and classes from source code using AST."""
        testables = []

        try:
            tree = ast.parse(source_code)
        except SyntaxError as e:
            return []

        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                # Skip private functions (start with _)
                if node.name.startswith("_") and not node.name.startswith("__"):
                    continue

                # Extract function info
                args = [arg.arg for arg in node.args.args if arg.arg != "self"]
                docstring = ast.get_docstring(node) or ""

                testables.append({
                    "type": "function",
                    "name": node.name,
                    "args": args,
                    "docstring": docstring[:200] if docstring else "",
                    "is_method": False
                })

            elif isinstance(node, ast.ClassDef):
                # Skip private classes
                if node.name.startswith("_"):
                    continue

                class_docstring = ast.get_docstring(node) or ""
                methods = []

                for item in node.body:
                    if isinstance(item, ast.FunctionDef):
                        if item.name.startswith("_") and item.name != "__init__":
                            continue
                        args = [arg.arg for arg in item.args.args if arg.arg != "self"]
                        methods.append({
                            "name": item.name,
                            "args": args,
                            "docstring": (ast.get_docstring(item) or "")[:100]
                        })

                testables.append({
                    "type": "class",
                    "name": node.name,
                    "docstring": class_docstring[:200] if class_docstring else "",
                    "methods": methods
                })

        return testables

    def _build_source_prompt(
        self,
        module_name: str,
        testables: List[dict],
        source_code: str
    ) -> str:
        """Build prompt for generating tests from source code."""
        lines = [
            f"Generate pytest test skeletons for the module '{module_name}'.",
            "",
            "## Testable Items",
            ""
        ]

        for item in testables:
            if item["type"] == "function":
                lines.append(f"### Function: {item['name']}")
                lines.append(f"- Arguments: {', '.join(item['args']) if item['args'] else 'none'}")
                if item["docstring"]:
                    lines.append(f"- Description: {item['docstring']}")
                lines.append("")

            elif item["type"] == "class":
                lines.append(f"### Class: {item['name']}")
                if item["docstring"]:
                    lines.append(f"- Description: {item['docstring']}")
                lines.append("- Methods:")
                for method in item["methods"]:
                    lines.append(f"  - {method['name']}({', '.join(method['args'])})")
                lines.append("")

        # Add source code snippet (truncated)
        if len(source_code) < 2000:
            lines.extend([
                "## Source Code Reference",
                "```python",
                source_code,
                "```"
            ])

        lines.extend([
            "",
            "Generate test cases for each function and class method.",
            "Include happy path, edge cases, and error conditions.",
            f"Use module_name: 'test_{module_name}'"
        ])

        return "\n".join(lines)

    def _build_requirements_prompt(self, title: str, criteria: List[dict]) -> str:
        """Build prompt for generating tests from requirements."""
        lines = [
            f"Generate pytest test skeletons for feature: '{title}'",
            "",
            "## Acceptance Criteria to Test",
            ""
        ]

        for i, c in enumerate(criteria, 1):
            lines.append(f"{i}. [{c['requirement_id']}] {c['requirement_title']}")
            lines.append(f"   Criterion: {c['criterion']}")
            lines.append("")

        lines.extend([
            "Generate at least one test case for each acceptance criterion.",
            "Include the requirement ID in the test description.",
            "Use module_name: 'test_requirements'"
        ])

        return "\n".join(lines)

    def _build_signatures_prompt(self, functions: List[dict]) -> str:
        """Build prompt for generating tests from function signatures."""
        lines = [
            "Generate pytest test skeletons for the following functions:",
            ""
        ]

        for func in functions:
            name = func.get("name", "unknown")
            args = func.get("args", [])
            returns = func.get("returns", "unknown")
            lines.append(f"- {name}({', '.join(args)}) -> {returns}")

        lines.extend([
            "",
            "Generate test cases for each function.",
            "Include happy path, edge cases (empty/None values), and error conditions.",
            "Use module_name: 'test_functions'"
        ])

        return "\n".join(lines)

    def _build_test_file(self, test_specs: dict, source_module: str) -> str:
        """Convert test specifications to a pytest file."""
        lines = [
            f'"""Tests for {source_module} module."""',
            "",
            "import pytest",
            ""
        ]

        # Add custom imports
        for imp in test_specs.get("imports", []):
            lines.append(imp)
        if test_specs.get("imports"):
            lines.append("")

        # Add import for source module
        lines.append(f"# TODO: Update import path")
        lines.append(f"# from {source_module} import <functions_to_test>")
        lines.append("")

        # Add fixtures
        for fixture in test_specs.get("fixtures", []):
            fixture_name = fixture.get("name", "fixture")
            fixture_desc = fixture.get("description", "Test fixture")
            lines.extend([
                "@pytest.fixture",
                f"def {fixture_name}():",
                f'    """{fixture_desc}"""',
                "    # TODO: Implement fixture",
                "    pass",
                ""
            ])

        # Add test cases
        current_function = None
        for test in test_specs.get("test_cases", []):
            test_name = test.get("name", "test_unknown")
            description = test.get("description", "Test case")
            function_under_test = test.get("function_under_test", "")
            test_type = test.get("test_type", "unit")
            assertions = test.get("assertions", [])

            # Ensure test name starts with test_
            if not test_name.startswith("test_"):
                test_name = f"test_{test_name}"

            # Clean test name (remove invalid characters)
            test_name = re.sub(r'[^a-zA-Z0-9_]', '_', test_name)

            # Add class grouping comment if function changed
            if function_under_test and function_under_test != current_function:
                current_function = function_under_test
                lines.append(f"# Tests for {function_under_test}")
                lines.append("")

            # Generate test function
            if test_type == "parametrized":
                lines.extend([
                    '@pytest.mark.parametrize("input_val,expected", [',
                    "    # TODO: Add test cases",
                    "    # (input_value, expected_result),",
                    "])",
                    f"def {test_name}(input_val, expected):",
                ])
            else:
                lines.append(f"def {test_name}():")

            lines.append(f'    """{description}"""')

            # Add test type marker
            if test_type == "edge_case":
                lines.append("    # Edge case test")
            elif test_type == "error":
                lines.append("    # Error condition test")

            # Add assertions as TODO comments
            if assertions:
                lines.append("    # Expected assertions:")
                for assertion in assertions:
                    lines.append(f"    # - {assertion}")

            lines.extend([
                "    # TODO: Implement test",
                "    pass",
                ""
            ])

        return "\n".join(lines)

    def _request_test_approval(self, test_file: str, source: str) -> Any:
        """Request approval for generated tests."""
        # Count test functions
        test_count = test_file.count("\ndef test_")

        return self.request_approval(
            action_type="test_generation",
            proposal={"test_file_content": test_file[:5000]},  # Truncate for display
            description=f"Generated {test_count} test skeletons from {source}",
            context=f"Preview of generated test file:\n\n{test_file[:2000]}...",
            risk_level="low"
        )

    def write_test_file(self, test_content: str, output_path: str) -> str:
        """
        Write generated test file to disk.

        Args:
            test_content: Generated test file content
            output_path: Path to write test file

        Returns:
            Absolute path to written file
        """
        output = Path(output_path)
        if not output.is_absolute():
            output = self.project_root / output

        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(test_content, encoding="utf-8")

        return str(output)
