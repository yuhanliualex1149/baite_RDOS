"""Compatibility entry; keep install_runner.py beside this file when downloading."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import install_runner as implementation

if __name__ == "__main__":
    implementation.main()
else:
    sys.modules[__name__] = implementation
