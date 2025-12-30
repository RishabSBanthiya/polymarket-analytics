#!/usr/bin/env python3
"""
Convenience wrapper for scripts/run_bot.py

Allows running the bot from the project root.
"""

import sys
import subprocess
from pathlib import Path

# Get the path to the actual script
script_path = Path(__file__).parent / "scripts" / "run_bot.py"

# Run the script with all arguments passed through
if __name__ == "__main__":
    sys.exit(subprocess.run([sys.executable, str(script_path)] + sys.argv[1:]).returncode)
