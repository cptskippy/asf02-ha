"""Install test dependencies: the asf02 library plus the requirements of
the Home Assistant components this integration depends on (their
manifest-level requirements are installed by HA at runtime, not by pip)."""

import json
import subprocess
import sys
from pathlib import Path

import homeassistant

ha_file = homeassistant.__file__
assert ha_file is not None
components = Path(ha_file).parent / "components"
requirements = {
    requirement
    for name in ("bluetooth", "usb")
    for requirement in json.loads((components / name / "manifest.json").read_text()).get(
        "requirements", []
    )
}
subprocess.check_call([sys.executable, "-m", "pip", "install", "asf02<0.3", *sorted(requirements)])
