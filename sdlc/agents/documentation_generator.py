"""
Documentation Generator Agent.

SDLC Phase: Implementation

Capabilities:
- Generate docstrings for functions and classes
- Create module-level documentation
- Generate README sections from code analysis
- Produce API documentation in various formats

Human Gate: Review and approve generated documentation before writing.
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
class DocstringSpec:
    """Specification for a single docstring."""
    target_name: str  # Function/class name
    target_type: str  # function, class, method, module
    docstring: str  # Generated docstring content
    line_number: int  # Where to insert/replace


@dataclass
class DocumentationOutput:
    """Complete documentation output."""
    doc_type: str  # docstrings, readme, api
    content: str  # Generated content
    target_file: Optional[str] = None  # File to modify/create
    docstrings: List[DocstringSpec] = field(default_factory=list)


class DocGeneratorAgent(BaseAgent):
    """
    Agent for generating documentation.

    Takes source code files and produces:
    - Docstrings for functions/classes/methods
    - Module-level documentation
    - README sections
    - API documentation
    """

    agent_id = "documentation_generator"
    phase = AgentPhase.IMPLEMENTATION
    description = "Generates documentation from source code analysis"

    # Schema for docstring generation
    DOCSTRING_SCHEMA = {
        "type": "object",
        "properties": {
            "module_docstring": {"type": "string"},
            "docstrings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "type": {"type": "string", "enum": ["function", "class", "method"]},
                        "docstring": {"type": "string"},
                        "params": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "name": {"type": "string"},
                                    "type": {"type": "string"},
                                    "description": {"type": "string"}
                                },
                                "required": ["name", "description"]
                            }
                        },
                        "returns": {"type": "string"},
                        "raises": {"type": "array", "items": {"type": "string"}}
                    },
                    "required": ["name", "type", "docstring"]
                }
            }
        },
        "required": ["docstrings"]
    }

    # Schema for README generation
    README_SCHEMA = {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "description": {"type": "string"},
            "installation": {"type": "string"},
            "usage": {"type": "string"},
            "api_overview": {"type": "string"},
            "examples": {"type": "array", "items": {"type": "string"}},
            "configuration": {"type": "string"},
            "contributing": {"type": "string"}
        },
        "required": ["title", "description", "usage"]
    }

    DOCSTRING_SYSTEM_PROMPT = """You are a Technical Writer generating Python docstrings.

Generate clear, concise docstrings following Google style format:
- One-line summary (imperative mood: "Return", "Calculate", not "Returns")
- Blank line if there's more content
- Args section with parameter descriptions
- Returns section describing return value
- Raises section for exceptions

Example format:
```
Calculate the sum of two numbers.

Args:
    a: First number to add.
    b: Second number to add.

Returns:
    The sum of a and b.

Raises:
    TypeError: If inputs are not numeric.
```

Be concise but complete. Focus on WHAT the code does and WHY, not HOW.
Do not repeat the function signature in the docstring."""

    README_SYSTEM_PROMPT = """You are a Technical Writer generating README documentation.

Create clear, helpful documentation that includes:
- A concise title and description
- Installation instructions
- Basic usage examples
- API overview for main functions/classes
- Configuration options if applicable

Use Markdown formatting. Be concise but informative.
Focus on helping users get started quickly."""

    def __init__(
        self,
        config: AgentConfig,
        audit: AuditLogger,
        approval: ApprovalGate,
        project_root: Optional[str] = None
    ):
        """
        Initialize Documentation Generator.

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
        Generate documentation based on input.

        Args:
            task_input: Can be:
                - dict with "source_file": path to generate docstrings for
                - dict with "readme": True and optional "files" list
                - dict with "module": path to document as API

        Returns:
            AgentResult containing generated documentation
        """
        if isinstance(task_input, dict):
            if "source_file" in task_input:
                return self._generate_docstrings(task_input)
            elif "readme" in task_input:
                return self._generate_readme(task_input)
            elif "api_doc" in task_input:
                return self._generate_api_doc(task_input)

        return AgentResult(
            agent_id=self.agent_id,
            success=False,
            output=None,
            reasoning="Invalid input. Expected dict with 'source_file', 'readme', or 'api_doc' key."
        )

    def _generate_docstrings(self, task_input: dict) -> AgentResult:
        """Generate docstrings for a source file."""
        source_path = task_input.get("source_file")
        style = task_input.get("style", "google")  # google, numpy, sphinx

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

        # Read and parse source
        source_code = source_path.read_text(encoding="utf-8")

        # Extract items needing documentation
        items = self._extract_undocumented(source_code)

        if not items:
            return AgentResult(
                agent_id=self.agent_id,
                success=True,
                output={"message": "All items already documented", "docstrings": []},
                reasoning="No undocumented functions or classes found"
            )

        # Build prompt
        prompt = self._build_docstring_prompt(source_path.stem, items, source_code)

        # Generate docstrings via LLM
        try:
            doc_specs = self.call_llm_structured(
                prompt=prompt,
                schema=self.DOCSTRING_SCHEMA,
                system_prompt=self.DOCSTRING_SYSTEM_PROMPT
            )
        except Exception as e:
            return AgentResult(
                agent_id=self.agent_id,
                success=False,
                output=None,
                reasoning=f"Failed to generate docstrings: {str(e)}",
                error_message=str(e),
                error_type=type(e).__name__
            )

        # Build output with docstrings
        docstrings = doc_specs.get("docstrings", [])
        module_doc = doc_specs.get("module_docstring", "")

        # Format as insertable content
        formatted_output = self._format_docstrings(docstrings, style)

        # Request approval
        approval_response = self._request_doc_approval(
            formatted_output,
            f"docstrings for {source_path.name}",
            len(docstrings)
        )

        if approval_response.status == ApprovalStatus.REJECTED:
            return AgentResult(
                agent_id=self.agent_id,
                success=False,
                output={"docstrings": formatted_output},
                reasoning="Generated docstrings rejected by reviewer"
            )

        return AgentResult(
            agent_id=self.agent_id,
            success=True,
            output={
                "docstrings": formatted_output,
                "module_docstring": module_doc,
                "count": len(docstrings),
                "source_file": str(source_path),
                "raw_specs": docstrings
            },
            reasoning=f"Generated {len(docstrings)} docstrings for {source_path.name}"
        )

    def _generate_readme(self, task_input: dict) -> AgentResult:
        """Generate README documentation."""
        files = task_input.get("files", [])
        project_name = task_input.get("project_name", self.project_root.name)
        output_file = task_input.get("output", "README.md")

        # If no files specified, scan for Python files
        if not files:
            files = self._find_main_files()

        # Read file contents for context
        file_contents = {}
        for f in files[:5]:  # Limit to 5 files
            path = self.project_root / f if not Path(f).is_absolute() else Path(f)
            if path.exists():
                try:
                    content = path.read_text(encoding="utf-8")
                    file_contents[str(f)] = content[:2000]  # Truncate
                except Exception:
                    pass

        if not file_contents:
            return AgentResult(
                agent_id=self.agent_id,
                success=False,
                output=None,
                reasoning="No source files found to document"
            )

        # Build prompt
        prompt = self._build_readme_prompt(project_name, file_contents)

        # Generate README via LLM
        try:
            readme_specs = self.call_llm_structured(
                prompt=prompt,
                schema=self.README_SCHEMA,
                system_prompt=self.README_SYSTEM_PROMPT
            )
        except Exception as e:
            return AgentResult(
                agent_id=self.agent_id,
                success=False,
                output=None,
                reasoning=f"Failed to generate README: {str(e)}",
                error_message=str(e),
                error_type=type(e).__name__
            )

        # Format as Markdown
        readme_content = self._format_readme(readme_specs, project_name)

        # Request approval
        approval_response = self._request_doc_approval(
            readme_content,
            "README.md",
            1
        )

        if approval_response.status == ApprovalStatus.REJECTED:
            return AgentResult(
                agent_id=self.agent_id,
                success=False,
                output={"readme": readme_content},
                reasoning="Generated README rejected by reviewer"
            )

        return AgentResult(
            agent_id=self.agent_id,
            success=True,
            output={
                "readme": readme_content,
                "output_file": output_file,
                "sections": list(readme_specs.keys())
            },
            reasoning=f"Generated README with {len(readme_specs)} sections"
        )

    def _generate_api_doc(self, task_input: dict) -> AgentResult:
        """Generate API documentation for a module."""
        module_path = task_input.get("api_doc")
        output_format = task_input.get("format", "markdown")

        # Resolve path
        if not Path(module_path).is_absolute():
            module_path = self.project_root / module_path

        module_path = Path(module_path)
        if not module_path.exists():
            return AgentResult(
                agent_id=self.agent_id,
                success=False,
                output=None,
                reasoning=f"Module not found: {module_path}"
            )

        # Read and parse
        source_code = module_path.read_text(encoding="utf-8")
        items = self._extract_all_items(source_code)

        if not items:
            return AgentResult(
                agent_id=self.agent_id,
                success=False,
                output=None,
                reasoning="No documentable items found in module"
            )

        # Generate API documentation
        api_doc = self._format_api_doc(module_path.stem, items, output_format)

        # Request approval
        approval_response = self._request_doc_approval(
            api_doc,
            f"API documentation for {module_path.name}",
            len(items)
        )

        if approval_response.status == ApprovalStatus.REJECTED:
            return AgentResult(
                agent_id=self.agent_id,
                success=False,
                output={"api_doc": api_doc},
                reasoning="Generated API doc rejected by reviewer"
            )

        return AgentResult(
            agent_id=self.agent_id,
            success=True,
            output={
                "api_doc": api_doc,
                "format": output_format,
                "item_count": len(items),
                "module": str(module_path)
            },
            reasoning=f"Generated API documentation with {len(items)} items"
        )

    def _extract_undocumented(self, source_code: str) -> List[dict]:
        """Extract functions and classes without docstrings."""
        items = []

        try:
            tree = ast.parse(source_code)
        except SyntaxError:
            return []

        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                if node.name.startswith("_") and not node.name.startswith("__"):
                    continue

                docstring = ast.get_docstring(node)
                if not docstring:
                    args = [arg.arg for arg in node.args.args if arg.arg != "self"]
                    items.append({
                        "type": "function",
                        "name": node.name,
                        "args": args,
                        "line": node.lineno,
                        "has_return": self._has_return(node)
                    })

            elif isinstance(node, ast.ClassDef):
                if node.name.startswith("_"):
                    continue

                docstring = ast.get_docstring(node)
                methods = []
                for item in node.body:
                    if isinstance(item, ast.FunctionDef):
                        method_doc = ast.get_docstring(item)
                        if not method_doc and not item.name.startswith("_"):
                            args = [a.arg for a in item.args.args if a.arg != "self"]
                            methods.append({
                                "name": item.name,
                                "args": args,
                                "has_return": self._has_return(item)
                            })

                if not docstring or methods:
                    items.append({
                        "type": "class",
                        "name": node.name,
                        "line": node.lineno,
                        "needs_class_doc": not docstring,
                        "undocumented_methods": methods
                    })

        return items

    def _extract_all_items(self, source_code: str) -> List[dict]:
        """Extract all documentable items from source code."""
        items = []

        try:
            tree = ast.parse(source_code)
        except SyntaxError:
            return []

        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                if node.name.startswith("_") and not node.name.startswith("__"):
                    continue

                args = [arg.arg for arg in node.args.args if arg.arg != "self"]
                docstring = ast.get_docstring(node) or ""

                items.append({
                    "type": "function",
                    "name": node.name,
                    "args": args,
                    "docstring": docstring,
                    "line": node.lineno
                })

            elif isinstance(node, ast.ClassDef):
                if node.name.startswith("_"):
                    continue

                docstring = ast.get_docstring(node) or ""
                methods = []

                for item in node.body:
                    if isinstance(item, ast.FunctionDef):
                        if item.name.startswith("_") and item.name != "__init__":
                            continue
                        method_args = [a.arg for a in item.args.args if a.arg != "self"]
                        method_doc = ast.get_docstring(item) or ""
                        methods.append({
                            "name": item.name,
                            "args": method_args,
                            "docstring": method_doc
                        })

                items.append({
                    "type": "class",
                    "name": node.name,
                    "docstring": docstring,
                    "methods": methods,
                    "line": node.lineno
                })

        return items

    def _has_return(self, node: ast.FunctionDef) -> bool:
        """Check if function has a return statement with a value."""
        for child in ast.walk(node):
            if isinstance(child, ast.Return) and child.value is not None:
                return True
        return False

    def _build_docstring_prompt(
        self,
        module_name: str,
        items: List[dict],
        source_code: str
    ) -> str:
        """Build prompt for docstring generation."""
        lines = [
            f"Generate docstrings for the following items in module '{module_name}':",
            ""
        ]

        for item in items:
            if item["type"] == "function":
                args_str = ", ".join(item["args"]) if item["args"] else "none"
                lines.append(f"### Function: {item['name']}")
                lines.append(f"- Arguments: {args_str}")
                lines.append(f"- Has return value: {item['has_return']}")
                lines.append("")

            elif item["type"] == "class":
                lines.append(f"### Class: {item['name']}")
                if item.get("needs_class_doc"):
                    lines.append("- Needs class docstring")
                if item.get("undocumented_methods"):
                    lines.append("- Undocumented methods:")
                    for method in item["undocumented_methods"]:
                        args_str = ", ".join(method["args"]) if method["args"] else "none"
                        lines.append(f"  - {method['name']}({args_str})")
                lines.append("")

        # Add source code context (truncated)
        if len(source_code) < 3000:
            lines.extend([
                "## Source Code",
                "```python",
                source_code,
                "```"
            ])

        return "\n".join(lines)

    def _build_readme_prompt(self, project_name: str, file_contents: dict) -> str:
        """Build prompt for README generation."""
        lines = [
            f"Generate README documentation for the project '{project_name}'.",
            "",
            "## Source Files",
            ""
        ]

        for filename, content in file_contents.items():
            lines.append(f"### {filename}")
            lines.append("```python")
            lines.append(content)
            lines.append("```")
            lines.append("")

        lines.extend([
            "Generate a complete README with:",
            "- Project title and description",
            "- Installation instructions",
            "- Usage examples",
            "- API overview of main functions/classes"
        ])

        return "\n".join(lines)

    def _format_docstrings(self, docstrings: List[dict], style: str) -> str:
        """Format docstrings for display/insertion."""
        lines = []

        for doc in docstrings:
            name = doc.get("name", "unknown")
            doc_type = doc.get("type", "function")
            docstring = doc.get("docstring", "")
            params = doc.get("params", [])
            returns = doc.get("returns", "")
            raises = doc.get("raises", [])

            lines.append(f"# {doc_type.title()}: {name}")
            lines.append('"""')

            # Main description
            lines.append(docstring)

            # Parameters
            if params:
                lines.append("")
                lines.append("Args:")
                for param in params:
                    param_type = param.get("type", "")
                    type_str = f" ({param_type})" if param_type else ""
                    lines.append(f"    {param['name']}{type_str}: {param['description']}")

            # Returns
            if returns:
                lines.append("")
                lines.append("Returns:")
                lines.append(f"    {returns}")

            # Raises
            if raises:
                lines.append("")
                lines.append("Raises:")
                for exc in raises:
                    lines.append(f"    {exc}")

            lines.append('"""')
            lines.append("")

        return "\n".join(lines)

    def _format_readme(self, specs: dict, project_name: str) -> str:
        """Format README specs as Markdown."""
        lines = [
            f"# {specs.get('title', project_name)}",
            "",
            specs.get("description", ""),
            ""
        ]

        if specs.get("installation"):
            lines.extend([
                "## Installation",
                "",
                specs["installation"],
                ""
            ])

        if specs.get("usage"):
            lines.extend([
                "## Usage",
                "",
                specs["usage"],
                ""
            ])

        if specs.get("api_overview"):
            lines.extend([
                "## API Overview",
                "",
                specs["api_overview"],
                ""
            ])

        if specs.get("examples"):
            lines.extend([
                "## Examples",
                ""
            ])
            for example in specs["examples"]:
                lines.append(example)
                lines.append("")

        if specs.get("configuration"):
            lines.extend([
                "## Configuration",
                "",
                specs["configuration"],
                ""
            ])

        if specs.get("contributing"):
            lines.extend([
                "## Contributing",
                "",
                specs["contributing"],
                ""
            ])

        return "\n".join(lines)

    def _format_api_doc(
        self,
        module_name: str,
        items: List[dict],
        output_format: str
    ) -> str:
        """Format API documentation."""
        lines = [
            f"# API Reference: {module_name}",
            "",
            "## Contents",
            ""
        ]

        # Table of contents
        for item in items:
            if item["type"] == "class":
                lines.append(f"- [{item['name']}](#{item['name'].lower()})")
            else:
                lines.append(f"- [{item['name']}()](#{item['name'].lower()})")
        lines.append("")

        # Detailed documentation
        for item in items:
            if item["type"] == "class":
                lines.append(f"## {item['name']}")
                lines.append("")
                if item.get("docstring"):
                    lines.append(item["docstring"])
                    lines.append("")

                if item.get("methods"):
                    lines.append("### Methods")
                    lines.append("")
                    for method in item["methods"]:
                        args_str = ", ".join(method["args"]) if method["args"] else ""
                        lines.append(f"#### `{method['name']}({args_str})`")
                        lines.append("")
                        if method.get("docstring"):
                            lines.append(method["docstring"])
                        else:
                            lines.append("*No documentation available.*")
                        lines.append("")

            elif item["type"] == "function":
                args_str = ", ".join(item["args"]) if item["args"] else ""
                lines.append(f"## `{item['name']}({args_str})`")
                lines.append("")
                if item.get("docstring"):
                    lines.append(item["docstring"])
                else:
                    lines.append("*No documentation available.*")
                lines.append("")

        return "\n".join(lines)

    def _find_main_files(self) -> List[str]:
        """Find main Python files in project."""
        main_files = []

        # Look for common entry points
        for pattern in ["__init__.py", "main.py", "app.py", "cli.py", "*.py"]:
            for f in self.project_root.glob(pattern):
                if f.is_file() and not f.name.startswith("test_"):
                    rel_path = f.relative_to(self.project_root)
                    if "venv" not in str(rel_path) and "__pycache__" not in str(rel_path):
                        main_files.append(str(rel_path))
                        if len(main_files) >= 5:
                            return main_files

        return main_files

    def _request_doc_approval(
        self,
        content: str,
        doc_type: str,
        item_count: int
    ) -> Any:
        """Request approval for generated documentation."""
        return self.request_approval(
            action_type="documentation_generation",
            proposal={"content_preview": content[:5000]},
            description=f"Generated {doc_type} ({item_count} items)",
            context=f"Preview:\n\n{content[:2000]}...",
            risk_level="low"
        )

    def write_documentation(self, content: str, output_path: str) -> str:
        """
        Write generated documentation to disk.

        Args:
            content: Documentation content
            output_path: Path to write to

        Returns:
            Absolute path to written file
        """
        output = Path(output_path)
        if not output.is_absolute():
            output = self.project_root / output

        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(content, encoding="utf-8")

        return str(output)
