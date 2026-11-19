"""Unit tests for KOF 2002 Character Forge core and deterministic skills."""
import sys
from pathlib import Path

# Ensure G:\KOF is in sys.path when test runner discovers tests
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

