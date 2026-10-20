"""Run the portable offline tests with the package import path configured."""
from pathlib import Path
import sys
import unittest

if __name__ == "__main__":
    package = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(package / "scripts"))
    suite = unittest.defaultTestLoader.discover(str(package / "tests"))
    result = unittest.TextTestRunner(verbosity=2 if "-v" in sys.argv[1:] else 1).run(suite)
    raise SystemExit(0 if result.wasSuccessful() else 1)
