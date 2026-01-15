#!/usr/bin/env python3
"""Local tool-calling coding assistant using Ollama."""

import json
import sys
import ollama
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.syntax import Syntax

from config import (
    MODEL_NAME,
    MODEL_NAME_FAST,
    OLLAMA_BASE_URL,
    TEMPERATURE,
    MAX_TOOL_ITERATIONS,
    MAX_OUTPUT_TOKENS,
)
from tools import TOOL_SCHEMA, execute_tool, ToolResult
from prompts import SYSTEM_PROMPT, TOOL_RESULT_TEMPLATE, CONVERSATION_STARTER


console = Console()


class LocalAgent:
    """A local coding assistant with tool-calling capabilities."""

    def __init__(self, model: str = MODEL_NAME, fast_mode: bool = False):
        self.model = MODEL_NAME_FAST if fast_mode else model
        self.messages = []
        self.client = ollama.Client(host=OLLAMA_BASE_URL)

        # Initialize with system prompt
        self.messages.append({
            "role": "system",
            "content": SYSTEM_PROMPT
        })

    def _call_model(self, user_message: str = None) -> dict:
        """Call the model and get a structured response."""
        if user_message:
            self.messages.append({
                "role": "user",
                "content": user_message
            })

        try:
            response = self.client.chat(
                model=self.model,
                messages=self.messages,
                format=TOOL_SCHEMA,
                options={
                    "temperature": TEMPERATURE,
                    "num_predict": MAX_OUTPUT_TOKENS,
                }
            )

            content = response["message"]["content"]

            # Parse JSON response
            try:
                parsed = json.loads(content)
            except json.JSONDecodeError:
                # If JSON parsing fails, treat as a respond action
                parsed = {"action": "respond", "response": content}

            # Add assistant message to history
            self.messages.append({
                "role": "assistant",
                "content": content
            })

            return parsed

        except Exception as e:
            console.print(f"[red]Error calling model: {e}[/red]")
            return {"action": "respond", "response": f"Error: {e}"}

    def _display_thinking(self, thinking: str):
        """Display the model's thinking."""
        if thinking:
            console.print(Panel(
                thinking,
                title="[dim]Thinking[/dim]",
                border_style="dim",
                padding=(0, 1)
            ))

    def _display_tool_call(self, action: str, **kwargs):
        """Display a tool call."""
        relevant_args = {k: v for k, v in kwargs.items() if v and k != "thinking"}
        args_str = json.dumps(relevant_args, indent=2) if relevant_args else ""

        console.print(f"[cyan]→ {action}[/cyan]", end="")
        if args_str:
            console.print(Syntax(args_str, "json", theme="monokai", line_numbers=False))
        else:
            console.print()

    def _display_tool_result(self, result: ToolResult):
        """Display a tool result."""
        if result.success:
            if result.output:
                # Check if it looks like code
                if "\n" in result.output and ("def " in result.output or "class " in result.output or "import " in result.output):
                    console.print(Syntax(result.output, "python", theme="monokai", line_numbers=True))
                else:
                    console.print(Panel(result.output, border_style="green", padding=(0, 1)))
        else:
            console.print(f"[red]Error: {result.error}[/red]")

    def _display_response(self, response: str):
        """Display the agent's response to the user."""
        console.print()
        console.print(Markdown(response))
        console.print()

    def process_message(self, user_message: str) -> str:
        """Process a user message and return the final response."""
        iterations = 0

        while iterations < MAX_TOOL_ITERATIONS:
            iterations += 1

            # Call model (with user message on first iteration, without on subsequent)
            if iterations == 1:
                parsed = self._call_model(user_message)
            else:
                parsed = self._call_model()

            # Extract fields
            thinking = parsed.get("thinking", "")
            action = parsed.get("action", "respond")

            # Display thinking
            self._display_thinking(thinking)

            # Handle respond action (end of loop)
            if action == "respond":
                response = parsed.get("response", "")
                self._display_response(response)
                return response

            # Display and execute tool call
            tool_args = {k: v for k, v in parsed.items() if k not in ["action", "thinking"]}
            self._display_tool_call(action, **tool_args)
            result = execute_tool(action, **tool_args)
            self._display_tool_result(result)

            # Add tool result to conversation
            tool_result_msg = TOOL_RESULT_TEMPLATE.format(
                action=action,
                result=result.output if result.success else f"Error: {result.error}"
            )
            self.messages.append({
                "role": "user",
                "content": tool_result_msg
            })

        # Max iterations reached
        console.print("[yellow]Max iterations reached. Stopping.[/yellow]")
        return "I've reached the maximum number of steps. Please provide more guidance."

    def chat(self):
        """Run the interactive chat loop."""
        console.print(Panel(
            CONVERSATION_STARTER,
            title="[bold blue]Local Agent[/bold blue]",
            border_style="blue"
        ))

        while True:
            try:
                console.print()
                user_input = console.input("[bold green]You:[/bold green] ").strip()

                if not user_input:
                    continue

                if user_input.lower() in ["exit", "quit", "q"]:
                    console.print("[dim]Goodbye![/dim]")
                    break

                if user_input.lower() == "clear":
                    # Reset conversation but keep system prompt
                    self.messages = [self.messages[0]]
                    console.print("[dim]Conversation cleared.[/dim]")
                    continue

                if user_input.lower() == "fast":
                    self.model = MODEL_NAME_FAST
                    console.print(f"[dim]Switched to fast mode: {self.model}[/dim]")
                    continue

                if user_input.lower() == "slow":
                    self.model = MODEL_NAME
                    console.print(f"[dim]Switched to normal mode: {self.model}[/dim]")
                    continue

                console.print()
                self.process_message(user_input)

            except KeyboardInterrupt:
                console.print("\n[dim]Interrupted. Type 'exit' to quit.[/dim]")
            except Exception as e:
                console.print(f"[red]Error: {e}[/red]")


def main():
    """Main entry point."""
    import argparse

    parser = argparse.ArgumentParser(description="Local coding assistant with tool calling")
    parser.add_argument("--fast", action="store_true", help=f"Use faster model ({MODEL_NAME_FAST})")
    parser.add_argument("--model", type=str, default=MODEL_NAME, help="Model to use")
    parser.add_argument("prompt", nargs="*", help="Initial prompt (optional)")

    args = parser.parse_args()

    # Check if Ollama is running
    try:
        client = ollama.Client(host=OLLAMA_BASE_URL)
        client.list()
    except Exception as e:
        console.print(f"[red]Cannot connect to Ollama at {OLLAMA_BASE_URL}[/red]")
        console.print("[yellow]Make sure Ollama is running: OLLAMA_CONTEXT_LENGTH=8192 ollama serve[/yellow]")
        sys.exit(1)

    # Create agent
    agent = LocalAgent(model=args.model, fast_mode=args.fast)

    # If prompt provided on command line, run single shot
    if args.prompt:
        prompt = " ".join(args.prompt)
        agent.process_message(prompt)
    else:
        # Interactive mode
        agent.chat()


if __name__ == "__main__":
    main()
