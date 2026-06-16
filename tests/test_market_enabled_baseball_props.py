import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

os.environ.setdefault("PINNACLE_API_KEY", "test-key")

from unittest.mock import patch

from pmev import config
from pmev.config import market_enabled


def test_kalshi_baseball_props_enabled_by_default():
    assert market_enabled("kalshi", "Baseball", "MLB", "player_prop") is True


def test_kalshi_baseball_props_toggle_off_disables():
    # The existing SPORT_TOGGLES row IS the on/off switch the matcher honors.
    toggles = dict(config.SPORT_TOGGLES)
    toggles[("kalshi", "Baseball", "player_prop")] = False
    with patch.object(config, "SPORT_TOGGLES", toggles):
        assert market_enabled("kalshi", "Baseball", "MLB", "player_prop") is False
