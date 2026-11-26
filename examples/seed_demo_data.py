"""Copy the demo sales CSV into the agent's workspace.

The data tools can only read files inside WORKSPACE_DIR, so run this once
before trying the data-analysis examples:

    python examples/seed_demo_data.py

Then ask Dot Lab, for example:

    "Analyse demo/demo_sales.csv: rank branches by gross profit, show the monthly
     revenue trend, and save a short report."

The data is fully synthetic (8 branches, 8 categories, Jan-Jun 2026). It was
generated with the data generator from https://github.com/Anjub004/retail-analytics-toolkit
(MIT licence) using its fixed random seed, then aggregated to daily sales per
branch and category.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import Settings  # noqa: E402

SOURCE = Path(__file__).resolve().parent / "data" / "demo_sales.csv"


def main() -> None:
    settings = Settings.from_env()
    target = settings.workspace_dir / "demo" / "demo_sales.csv"
    if target.exists():
        print(f"Already present: {target}")
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(SOURCE, target)
    print(f"Copied demo data to {target}")
    print("Workspace path for the agent: demo/demo_sales.csv")


if __name__ == "__main__":
    main()
