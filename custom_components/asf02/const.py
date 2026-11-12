"""Constants for the ASF02 pet feeder integration."""

from homeassistant.const import Platform

DOMAIN = "asf02"
VERSION = "0.1.0"

CONF_ADDRESS = "address"
CONF_TOKEN = "token"
CONF_TZ_OFFSET = "tz_offset_seconds"

DEFAULT_POLL_MINUTES = 15
MIN_POLL_MINUTES = 1
MAX_POLL_MINUTES = 60

OPTION_POLL_MINUTES = "poll_minutes"

BATTERY_LOW_THRESHOLD = 410

EVENT_FEED = "asf02_feed"

# Portions number entity / feed button bounds (match the feed service).
PORTIONS_MIN = 1
PORTIONS_MAX = 99
DEFAULT_PORTIONS = 1

# Schedule slot editor: which slot the editor entities target (options) and
# the slot portion bounds (match the feed service / Portions number).
OPTION_SCHEDULE_SLOT = "schedule_slot"
SCHEDULE_SLOT_MIN = 1
SCHEDULE_SLOT_MAX = 10
DEFAULT_SCHEDULE_SLOT = 1
SCHEDULE_PORTION_MIN = 1
SCHEDULE_PORTION_MAX = 99
DEFAULT_SCHEDULE_PORTION = 1

# Local schedule model persistence. The device's schedule read is
# unreliable on in-use firmware, so HA keeps a local copy of the schedule
# (the source of truth for writes) and refreshes it from write
# confirmations and any non-empty reads.
SCHEDULE_STORE_VERSION = 1

# Mid-feed feed() polling / completion-wait tuning.
FEED_SECONDS_PER_PORTION = 5.1  # measured 2026-09-18
FEED_WAIT_POLL_INTERVAL = 3.0  # s between status() polls
FEED_WAIT_MARGIN = 10.0  # s added to the remaining-estimate fallback
FEED_WAIT_TIMEOUT = 900.0  # s hard cap for wait=True

MANUFACTURER = "Hong Kong M Spacer Limited"
MODEL = "CWH01L-A1"

PLATFORMS: list[Platform] = [
    Platform.BUTTON,
    Platform.NUMBER,
    Platform.SENSOR,
    Platform.TIME,
]
