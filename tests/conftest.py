"""Pytest configuration for test suite."""

import sys
from pathlib import Path

# Add runtime directory containing 'src' to sys.path
_RUNTIME_DIR = Path(__file__).resolve().parent.parent / "skills" / "audit-orchestrator" / "scripts" / "runtime"
if _RUNTIME_DIR.is_dir() and str(_RUNTIME_DIR) not in sys.path:
    sys.path.insert(0, str(_RUNTIME_DIR))