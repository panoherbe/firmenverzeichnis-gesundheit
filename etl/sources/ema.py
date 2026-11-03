"""
EMA Article 57 product data (XLSX, ~8 MB): marketing-authorisation holders (MAH) per product and country.
We keep only MAHs holding at least one authorisation in Germany; the file has names only (no address).

    python -m etl.sources.ema fetch      # raw/ema/<day>/article-57-product-data.xlsx
"""

from __future__ import annotations

import sys

from etl.common import fetch
from etl.common.raw import run_dir

URL = "https://www.ema.europa.eu/en/documents/other/article-57-product-data_en.xlsx"


def main() -> None:
    if sys.argv[1:] != ["fetch"]:
        sys.exit(__doc__)
    out = run_dir("ema") / "article-57-product-data.xlsx"
    c = fetch.client(timeout=300)
    resp = fetch.get(c, URL)
    resp.raise_for_status()
    out.write_bytes(resp.content)
    print(f"ema: {len(resp.content):,} bytes -> {out}")


if __name__ == "__main__":
    main()
