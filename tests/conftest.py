import os
import sys

# The daemon's pure routing module lives at the repo root.
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
