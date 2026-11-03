"""
CORDIS Horizon Europe projects (CC BY 4.0, ~27 MB zip): German private companies (activityType PRC) taking part in
health projects. Adds R&D biotech and digital-health SMEs that are neither device manufacturers nor MA holders.

    python -m etl.sources.cordis fetch   # raw/cordis/<day>/cordis-HORIZONprojects-csv.zip
"""

from __future__ import annotations

import sys

from etl.common import fetch
from etl.common.raw import run_dir

URL = "https://cordis.europa.eu/data/cordis-HORIZONprojects-csv.zip"


def main() -> None:
    if sys.argv[1:] != ["fetch"]:
        sys.exit(__doc__)
    out = run_dir("cordis") / "cordis-HORIZONprojects-csv.zip"
    c = fetch.client(timeout=300)
    resp = fetch.get(c, URL)
    resp.raise_for_status()
    out.write_bytes(resp.content)
    print(f"cordis: {len(resp.content):,} bytes -> {out}")


if __name__ == "__main__":
    main()
