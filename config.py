"""Configuration for the local tool-calling agent."""

# Model settings
MODEL_NAME = "gemma2:2b"  # Primary model - best instruction following for tool calls
MODEL_NAME_FAST = "gemma2:2b"  # Same model (already fast)
MODEL_NAME_LARGE = "llama3.2:latest"  # Use --model llama3.2:latest for complex tasks

# Ollama settings
OLLAMA_BASE_URL = "http://localhost:11434"
CONTEXT_WINDOW = 8192  # Must match OLLAMA_CONTEXT_LENGTH env var
TEMPERATURE = 0  # Deterministic for reliable tool calls

# Agent settings
MAX_TOOL_ITERATIONS = 3  # Keep low for small models that don't stop well
MAX_OUTPUT_TOKENS = 2048

# Safety settings
ALLOWED_SHELL_COMMANDS = [
    "git", "npm", "npx", "node", "python", "python3", "pip", "pip3",
    "pytest", "poetry", "cargo", "rustc", "go", "make", "cmake",
    "ls", "cat", "head", "tail", "grep", "find", "wc", "diff",
    "mkdir", "cp", "mv", "touch", "echo", "pwd", "which", "env",
    "ollama", "curl", "wget",
]

BLOCKED_PATTERNS = [
    "rm -rf /", "rm -rf ~", "rm -rf *",
    "sudo", "su ",
    "> /dev/", "| /dev/",
    "mkfs", "dd if=",
    ":(){", "fork",
    "chmod 777",
    "curl | sh", "wget | sh",
]

# Project directory - only allow writes within this directory without confirmation
PROJECT_DIR = "/mnt/c/agent_C"
