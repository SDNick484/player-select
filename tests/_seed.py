"""Shared test helper: import the fixture backup (and CSV) through the real importer."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _ged import FIXTURE_ROWS, build_csv, build_ged, fixture_csv_rows  # noqa: E402
from ps.importer import apply, stage  # noqa: E402

T0 = 1_800_000_000


def seed(store, maps, tmp: Path, *, csv: bool = True, now: int = T0) -> None:
    imports = Path(tmp) / "imports"
    ged = build_ged(FIXTURE_ROWS)
    data = build_csv(fixture_csv_rows()) if csv else None
    res = stage(store, maps, imports, ged, data, now=now)
    apply(store, maps, imports, res["import_id"], confirm_removals=False, now=now + 1)
