"""
GLEIF LEI-CDF Golden Copy (CC0), full file (~450 MB zip). Parsed later for Germany only.

    python -m etl.sources.gleif fetch    # raw/gleif/<day>/lei2-golden-copy.csv.zip
"""

from __future__ import annotations

import sys

from etl.common import fetch
from etl.common.raw import run_dir

URL = "https://goldencopy.gleif.org/api/v2/golden-copies/publishes/lei2/latest.csv"


def main() -> None:
    if sys.argv[1:] != ["fetch"]:
        sys.exit(__doc__)
    out = run_dir("gleif") / "lei2-golden-copy.csv.zip"
    c = fetch.client(timeout=600)
    with c.stream("GET", URL) as resp:
        resp.raise_for_status()
        with out.open("wb") as f:
            for chunk in resp.iter_bytes(1 << 20):
                f.write(chunk)
    print(f"gleif: {out.stat().st_size:,} bytes -> {out}")


if __name__ == "__main__":
    main()
