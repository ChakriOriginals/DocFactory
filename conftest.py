# The synth generator is a script directory (dev tooling), not an installed
# package — put it on sys.path so tests can import its modules.
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "data" / "synth"))
