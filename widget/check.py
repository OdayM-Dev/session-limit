"""Prints the packages the ring draws with that this Python lacks, as pip names.

The plugin runs it before starting the ring, so a missing package is named
in Claude rather than failing silently under pythonw.
"""

import importlib.util

NEEDED = {"numpy": "numpy", "PIL": "pillow"}  # module: what pip installs it as

print(" ".join(pip for module, pip in NEEDED.items() if importlib.util.find_spec(module) is None))
