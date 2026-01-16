"""
Requirements Analyst Agent.

SDLC Phase: Requirements

Capabilities:
- Parse natural language descriptions into structured requirements
- Extract acceptance criteria from user stories
- Cross-reference with existing codebase to find related code
- Flag ambiguities for human clarification
- Generate requirements documents in JSON/Markdown format

Human Gate: Review and approve requirements before design phase.
"""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .base import BaseAgent, AgentConfig, AgentResult, AgentPhase
from ..audit import AuditLogger
from ..approval import ApprovalGate, ApprovalStatus


@dataclass
class Requirement:
    """A single requirement extracted from user input."""
    id: str  # Unique identifier (e.g., REQ-001)
    title: str  # Short title
    description: str  # Full description
    type: str  # functional, non-functional, constraint
    priority: str  # must-have, should-have, nice-to-have
    acceptance_criteria: List[str]  # Testable criteria
    dependencies: List[str] = field(default_factory=list)  # Other requirement IDs
    related_files: List[str] = field(default_factory=list)  # Files that may need changes
    ambiguities: List[str] = field(default_factory=list)  # Questions to clarify


@dataclass
class RequirementsDocument:
    """Complete requirements document for a feature/task."""
    title: str
    summary: str
    requirements: List[Requirement]
    scope: str  # In-scope vs out-of-scope
    assumptions: List[str]
    risks: List[str]
    questions: List[str]  # Unresolved questions for stakeholder


class RequirementsAnalystAgent(BaseAgent):
    """
    Agent for parsing and structuring requirements.

    Takes natural language feature requests or bug reports and produces
    structured requirements documents with acceptance criteria.
    """

    agent_id = "requirements_analyst"
    phase = AgentPhase.REQUIREMENTS
    description = "Parses user stories into structured requirements with acceptance criteria"

    # Schema for LLM structured output
    REQUIREMENTS_SCHEMA = {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "summary": {"type": "string"},
            "requirements": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "title": {"type": "string"},
                        "description": {"type": "string"},
                        "type": {"type": "string", "enum": ["functional", "non-functional", "constraint"]},
                        "priority": {"type": "string", "enum": ["must-have", "should-have", "nice-to-have"]},
                        "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
                        "ambiguities": {"type": "array", "items": {"type": "string"}}
                    },
                    "required": ["id", "title", "description", "type", "priority", "acceptance_criteria"]
                }
            },
            "scope": {"type": "string"},
            "assumptions": {"type": "array", "items": {"type": "string"}},
            "risks": {"type": "array", "items": {"type": "string"}},
            "questions": {"type": "array", "items": {"type": "string"}}
        },
        "required": ["title", "summary", "requirements", "scope", "assumptions", "risks", "questions"]
    }

    SYSTEM_PROMPT = """You are a Requirements Analyst for a software development team.

Your job is to take natural language feature requests, bug reports, or task descriptions
and produce structured requirements documents.

For each input, you must:
1. Identify the core requirements (what needs to be built/fixed)
2. Classify each requirement as functional, non-functional, or constraint
3. Assign priority (must-have, should-have, nice-to-have)
4. Write clear, testable acceptance criteria for each requirement
5. Note any ambiguities or questions that need clarification
6. List assumptions you're making
7. Identify potential risks

Be thorough but concise. Focus on clarity and testability.
If something is unclear, list it as a question rather than making assumptions.

Output your analysis as a structured JSON document matching the provided schema."""

    def __init__(
        self,
        config: AgentConfig,
        audit: AuditLogger,
        approval: ApprovalGate,
        project_root: Optional[str] = None
    ):
        """
        Initialize Requirements Analyst.

        Args:
            config: Agent configuration
            audit: Audit logger
            approval: Approval gate
            project_root: Root directory of project (for code cross-reference)
        """
        super().__init__(config, audit, approval)
        self.project_root = Path(project_root) if project_root else None

    def execute(self, task_input: Any) -> AgentResult:
        """
        Analyze input and produce requirements document.

        Args:
            task_input: Can be:
                - str: Natural language description
                - dict: {"description": str, "context": str, "existing_code": list}

        Returns:
            AgentResult containing RequirementsDocument
        """
        # Parse input
        if isinstance(task_input, str):
            description = task_input
            context = ""
            existing_files = []
        else:
            description = task_input.get("description", "")
            context = task_input.get("context", "")
            existing_files = task_input.get("existing_code", [])

        # If project root provided, scan for relevant files
        related_files = []
        if self.project_root:
            related_files = self._find_related_files(description)

        # Build prompt with context
        prompt = self._build_prompt(description, context, related_files, existing_files)

        # Call LLM for requirements extraction
        try:
            parsed_requirements = self.call_llm_structured(
                prompt=prompt,
                schema=self.REQUIREMENTS_SCHEMA,
                system_prompt=self.SYSTEM_PROMPT
            )
        except Exception as e:
            return AgentResult(
                agent_id=self.agent_id,
                success=False,
                output=None,
                reasoning=f"Failed to parse requirements: {str(e)}",
                error_message=str(e),
                error_type=type(e).__name__
            )

        # Convert to RequirementsDocument
        requirements_doc = self._build_document(parsed_requirements, related_files)

        # Request approval if there are questions/ambiguities
        if requirements_doc.questions or any(r.ambiguities for r in requirements_doc.requirements):
            approval_response = self._request_requirements_approval(requirements_doc)

            if approval_response.status == ApprovalStatus.REJECTED:
                return AgentResult(
                    agent_id=self.agent_id,
                    success=False,
                    output=requirements_doc,
                    reasoning="Requirements rejected by reviewer"
                )
            elif approval_response.status == ApprovalStatus.MODIFIED:
                # Handle modifications if provided
                if approval_response.modified_proposal:
                    requirements_doc = self._apply_modifications(
                        requirements_doc,
                        approval_response.modified_proposal
                    )

        return AgentResult(
            agent_id=self.agent_id,
            success=True,
            output=self._document_to_dict(requirements_doc),
            reasoning=f"Extracted {len(requirements_doc.requirements)} requirements with "
                     f"{sum(len(r.acceptance_criteria) for r in requirements_doc.requirements)} "
                     f"acceptance criteria"
        )

    def _build_prompt(
        self,
        description: str,
        context: str,
        related_files: List[str],
        existing_files: List[str]
    ) -> str:
        """Build the analysis prompt."""
        prompt_parts = [
            "Analyze the following feature request/task and produce a structured requirements document.",
            "",
            "## Task Description",
            description,
        ]

        if context:
            prompt_parts.extend([
                "",
                "## Additional Context",
                context
            ])

        if related_files:
            prompt_parts.extend([
                "",
                "## Potentially Related Files in Codebase",
                "\n".join(f"- {f}" for f in related_files[:20])  # Limit to 20
            ])

        if existing_files:
            prompt_parts.extend([
                "",
                "## Existing Code References",
                "\n".join(f"- {f}" for f in existing_files)
            ])

        prompt_parts.extend([
            "",
            "Produce a complete requirements document in JSON format.",
            "Include at least one requirement with clear acceptance criteria.",
            "Note any questions that need clarification before implementation."
        ])

        return "\n".join(prompt_parts)

    def _find_related_files(self, description: str) -> List[str]:
        """Find files in project that might be related to the description."""
        if not self.project_root or not self.project_root.exists():
            return []

        related = []
        keywords = self._extract_keywords(description)

        # Scan Python files for keyword matches
        for py_file in self.project_root.rglob("*.py"):
            # Skip common non-code directories
            if any(part in py_file.parts for part in [
                "venv", "node_modules", ".git", "__pycache__", "dist", "build"
            ]):
                continue

            try:
                content = py_file.read_text(errors="ignore").lower()
                rel_path = str(py_file.relative_to(self.project_root))

                # Check for keyword matches
                matches = sum(1 for kw in keywords if kw in content)
                if matches > 0:
                    related.append((rel_path, matches))
            except Exception:
                continue

        # Sort by match count and return top files
        related.sort(key=lambda x: x[1], reverse=True)
        return [f for f, _ in related[:10]]

    def _extract_keywords(self, text: str) -> List[str]:
        """Extract potential keywords from text for code search."""
        # Simple keyword extraction - lowercase words, filter common words
        words = text.lower().split()
        common = {
            "the", "a", "an", "is", "are", "was", "were", "be", "been",
            "have", "has", "had", "do", "does", "did", "will", "would",
            "could", "should", "may", "might", "must", "shall", "can",
            "to", "of", "in", "for", "on", "with", "at", "by", "from",
            "as", "into", "through", "during", "before", "after", "above",
            "below", "between", "under", "again", "further", "then", "once",
            "here", "there", "when", "where", "why", "how", "all", "each",
            "few", "more", "most", "other", "some", "such", "no", "nor",
            "not", "only", "own", "same", "so", "than", "too", "very",
            "just", "also", "now", "and", "but", "or", "if", "this", "that",
            "these", "those", "i", "we", "you", "it", "they", "add", "new",
            "create", "make", "need", "want", "feature", "function", "method"
        }

        keywords = [
            w.strip(".,!?;:'\"()[]{}") for w in words
            if len(w) > 2 and w not in common
        ]
        return list(set(keywords))[:15]  # Limit to 15 unique keywords

    def _build_document(
        self,
        parsed: dict,
        related_files: List[str]
    ) -> RequirementsDocument:
        """Convert parsed JSON to RequirementsDocument."""
        requirements = []

        for req_data in parsed.get("requirements", []):
            req = Requirement(
                id=req_data.get("id", f"REQ-{len(requirements)+1:03d}"),
                title=req_data.get("title", "Untitled"),
                description=req_data.get("description", ""),
                type=req_data.get("type", "functional"),
                priority=req_data.get("priority", "should-have"),
                acceptance_criteria=req_data.get("acceptance_criteria", []),
                ambiguities=req_data.get("ambiguities", []),
                related_files=related_files[:5]  # Add related files to each requirement
            )
            requirements.append(req)

        return RequirementsDocument(
            title=parsed.get("title", "Requirements Document"),
            summary=parsed.get("summary", ""),
            requirements=requirements,
            scope=parsed.get("scope", "To be determined"),
            assumptions=parsed.get("assumptions", []),
            risks=parsed.get("risks", []),
            questions=parsed.get("questions", [])
        )

    def _request_requirements_approval(
        self,
        doc: RequirementsDocument
    ) -> Any:
        """Request approval for the requirements document."""
        # Format document for review
        review_text = self._format_for_review(doc)

        return self.request_approval(
            action_type="requirements_approval",
            proposal=self._document_to_dict(doc),
            description=f"Requirements document: {doc.title}",
            context=f"Summary: {doc.summary}\n\nQuestions to resolve:\n" +
                   "\n".join(f"- {q}" for q in doc.questions),
            risk_level="low"
        )

    def _format_for_review(self, doc: RequirementsDocument) -> str:
        """Format document for human review."""
        lines = [
            f"# {doc.title}",
            "",
            f"**Summary:** {doc.summary}",
            "",
            f"**Scope:** {doc.scope}",
            "",
            "## Requirements",
            ""
        ]

        for req in doc.requirements:
            lines.extend([
                f"### {req.id}: {req.title}",
                f"**Type:** {req.type} | **Priority:** {req.priority}",
                "",
                req.description,
                "",
                "**Acceptance Criteria:**"
            ])
            for ac in req.acceptance_criteria:
                lines.append(f"- [ ] {ac}")

            if req.ambiguities:
                lines.append("\n**Ambiguities:**")
                for amb in req.ambiguities:
                    lines.append(f"- {amb}")

            if req.related_files:
                lines.append("\n**Related Files:**")
                for f in req.related_files:
                    lines.append(f"- `{f}`")

            lines.append("")

        if doc.assumptions:
            lines.extend(["## Assumptions", ""])
            for assumption in doc.assumptions:
                lines.append(f"- {assumption}")
            lines.append("")

        if doc.risks:
            lines.extend(["## Risks", ""])
            for risk in doc.risks:
                lines.append(f"- {risk}")
            lines.append("")

        if doc.questions:
            lines.extend(["## Questions to Resolve", ""])
            for q in doc.questions:
                lines.append(f"- {q}")

        return "\n".join(lines)

    def _document_to_dict(self, doc: RequirementsDocument) -> dict:
        """Convert document to dictionary for serialization."""
        return {
            "title": doc.title,
            "summary": doc.summary,
            "requirements": [
                {
                    "id": r.id,
                    "title": r.title,
                    "description": r.description,
                    "type": r.type,
                    "priority": r.priority,
                    "acceptance_criteria": r.acceptance_criteria,
                    "dependencies": r.dependencies,
                    "related_files": r.related_files,
                    "ambiguities": r.ambiguities
                }
                for r in doc.requirements
            ],
            "scope": doc.scope,
            "assumptions": doc.assumptions,
            "risks": doc.risks,
            "questions": doc.questions
        }

    def _apply_modifications(
        self,
        doc: RequirementsDocument,
        modifications: dict
    ) -> RequirementsDocument:
        """Apply human modifications to the document."""
        # Simple replacement for now - could be more sophisticated
        if "title" in modifications:
            doc.title = modifications["title"]
        if "summary" in modifications:
            doc.summary = modifications["summary"]
        if "scope" in modifications:
            doc.scope = modifications["scope"]
        if "assumptions" in modifications:
            doc.assumptions = modifications["assumptions"]
        if "risks" in modifications:
            doc.risks = modifications["risks"]
        if "questions" in modifications:
            doc.questions = modifications["questions"]
        if "requirements" in modifications:
            # Replace requirements entirely if provided
            new_reqs = []
            for req_data in modifications["requirements"]:
                new_reqs.append(Requirement(
                    id=req_data.get("id"),
                    title=req_data.get("title"),
                    description=req_data.get("description"),
                    type=req_data.get("type"),
                    priority=req_data.get("priority"),
                    acceptance_criteria=req_data.get("acceptance_criteria", []),
                    ambiguities=req_data.get("ambiguities", []),
                    dependencies=req_data.get("dependencies", []),
                    related_files=req_data.get("related_files", [])
                ))
            doc.requirements = new_reqs

        return doc

    def export_markdown(self, doc: RequirementsDocument, output_path: str) -> str:
        """Export requirements document to Markdown file."""
        content = self._format_for_review(doc)

        with open(output_path, "w", encoding="utf-8") as f:
            f.write(content)

        return output_path

    def export_json(self, doc: RequirementsDocument, output_path: str) -> str:
        """Export requirements document to JSON file."""
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(self._document_to_dict(doc), f, indent=2)

        return output_path
