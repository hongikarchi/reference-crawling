"""Run one WP2.9 stage, locally and fail-closed."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from canonical.vnext_identity_registry import main

if __name__ == "__main__":
    main()
