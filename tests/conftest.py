"""Makes the project importable when running pytest without packaging.

This shim disappears in Phase 8 when the platform becomes an installable
package with proper pyproject.toml.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
