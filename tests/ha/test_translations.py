"""Packaging guard: HA loads custom-integration translations ONLY from
``translations/{lang}.json`` — it never reads a root ``strings.json``.

If ``translations/en.json`` is missing, ``Integration.has_translations`` is
False, no translations load, and every entity falls back to the device name
with numeric-suffix entity IDs (``..._2``, ``..._3``). This test fails loudly
if that file is ever deleted or loses its entity names.
"""

from __future__ import annotations

import json
from pathlib import Path

import custom_components.asf02 as pkg


def _en_path() -> Path:
    return Path(pkg.__file__).parent / "translations" / "en.json"


def test_translations_en_present():
    en = _en_path()
    assert en.is_file(), (
        "translations/en.json is required — HA ignores a root strings.json, "
        "so without it no entity names or flow labels load."
    )


def test_translations_entity_names():
    data = json.loads(_en_path().read_text(encoding="utf-8"))
    sensor = data["entity"]["sensor"]
    binary = data["entity"]["binary_sensor"]
    assert sensor["battery"]["name"] == "Battery"
    assert sensor["feed_count"]["name"] == "Feed count"
    assert sensor["schedule"]["name"] == "Schedule"
    assert sensor["device_time"]["name"] == "Device time"
    assert binary["feeding"]["name"] == "Feeding"
    assert binary["battery_low"]["name"] == "Battery low"
    assert binary["fed_recently"]["name"] == "Fed recently"
