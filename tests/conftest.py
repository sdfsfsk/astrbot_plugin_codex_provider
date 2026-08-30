"""Test import and isolated AstrBot data setup for the plugin repository."""

import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

PLUGIN_PARENT = Path(__file__).resolve().parents[2]
if str(PLUGIN_PARENT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_PARENT))

_PREVIOUS_ASTRBOT_ROOT = os.environ.get("ASTRBOT_ROOT")
_TEST_ROOT = TemporaryDirectory(prefix="astrbot-codex-tests-")
os.environ["ASTRBOT_ROOT"] = _TEST_ROOT.name


def pytest_sessionfinish() -> None:
    """Restore the caller environment and remove isolated AstrBot test data."""
    if _PREVIOUS_ASTRBOT_ROOT is None:
        os.environ.pop("ASTRBOT_ROOT", None)
    else:
        os.environ["ASTRBOT_ROOT"] = _PREVIOUS_ASTRBOT_ROOT
    _TEST_ROOT.cleanup()
