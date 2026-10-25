# bong_strats/__main__.py
"""
CLI entry point for running the MIMIR Quant Strategy Studio directly via:
    python -m bong_strats
"""

import sys
from pathlib import Path

# Add project root
root = Path(__file__).resolve().parent.parent
if str(root) not in sys.path:
    sys.path.insert(0, str(root))

from scripts.quick_strategy import main

if __name__ == "__main__":
    main()
