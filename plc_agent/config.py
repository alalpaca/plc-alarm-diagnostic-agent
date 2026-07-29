"""
PLC Agent - Configuration Module

Loads settings from .env file and environment variables.
"""
import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env file from project root
PROJECT_ROOT = Path(__file__).parent.parent
load_dotenv(PROJECT_ROOT / ".env")


# --- LLM Configuration ---
OPENAI_API_BASE = os.getenv("OPENAI_API_BASE", "https://your-api-gateway.example.com")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
MODEL_NAME = os.getenv("MODEL_NAME", "gpt-4o")

# --- Knowledge Base (legacy single-path, kept for backward compatibility) ---
KNOWLEDGE_BASE_PATH = PROJECT_ROOT / os.getenv("KNOWLEDGE_BASE_PATH", "plc_knowledge_out")

# --- Multi-Program Knowledge Base Registry ---
# Each entry: key=program identifier, value=dict with name, path, type
# type: "alarm" = alarm-focused program, "control" = main control logic program
#       "global" = all programs merged with cross-program trace capability
PROGRAM_REGISTRY: dict[str, dict] = {
    "WH201_CG1": {
        "name": "WH201 CG1 全局 (29个程序合并)",
        "path": PROJECT_ROOT / "plc_knowledge_out_WH201_CG1",
        "type": "global",
    },
    "WH202_CG1": {
        "name": "WH202 CG1 报警程序 (001 Alarm)",
        "path": PROJECT_ROOT / "plc_knowledge_out",
        "type": "alarm",
    },
}

# Default program key
DEFAULT_PROGRAM_KEY = os.getenv("DEFAULT_PROGRAM_KEY", "WH201_CG1")

# --- Server ---
API_HOST = os.getenv("API_HOST", "0.0.0.0")
API_PORT = int(os.getenv("API_PORT", "8000"))


def validate_config():
    """Validate that all required configuration is present."""
    errors = []
    if not OPENAI_API_KEY:
        errors.append("OPENAI_API_KEY is not set. Please set it in .env file.")
    # Validate all registered program paths
    for key, cfg in PROGRAM_REGISTRY.items():
        if not cfg["path"].exists():
            errors.append(f"Knowledge base path for '{key}' does not exist: {cfg['path']}")
    if errors:
        raise ValueError("Configuration errors:\n" + "\n".join(f"  - {e}" for e in errors))


if __name__ == "__main__":
    # Quick config check
    print(f"API Base: {OPENAI_API_BASE}")
    print(f"Model:    {MODEL_NAME}")
    print(f"Default Program: {DEFAULT_PROGRAM_KEY}")
    print(f"API Key:  {'***' + OPENAI_API_KEY[-4:] if OPENAI_API_KEY else 'NOT SET'}")
    print(f"\nRegistered Programs:")
    for key, cfg in PROGRAM_REGISTRY.items():
        exists = cfg["path"].exists()
        print(f"  [{key}] {cfg['name']} (type={cfg['type']}, exists={exists})")
        print(f"         path: {cfg['path']}")
