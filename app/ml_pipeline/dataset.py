"""
AEOS ML Pipeline — Dataset Loader
Loads data from CSV, JSON, or inline dicts.
Produces a DatasetMeta record with sha256-based version ID.
"""

from __future__ import annotations
import hashlib
import json
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path

from app.core.config import settings
from app.core.logger import get_logger

log = get_logger(__name__)


@dataclass
class DatasetMeta:
    id: str                         # sha256[:16] of serialized data
    name: str
    source: str
    row_count: int
    feature_columns: list[str]
    target_column: str | None
    created_at: str


class DatasetLoader:

    def load_csv(self, path: str, target_col: str | None = None) -> tuple:
        import pandas as pd
        log.info("Loading CSV dataset", extra={"ctx_path": path})
        df = pd.read_csv(path)
        meta = self._make_meta(df, name=Path(path).stem, source=path, target_col=target_col)
        return df, meta

    def load_json(self, path: str, target_col: str | None = None) -> tuple:
        import pandas as pd
        log.info("Loading JSON dataset", extra={"ctx_path": path})
        df = pd.read_json(path)
        meta = self._make_meta(df, name=Path(path).stem, source=path, target_col=target_col)
        return df, meta

    def load_inline(
        self,
        data: list[dict],
        name: str = "inline",
        target_col: str | None = None,
    ) -> tuple:
        import pandas as pd
        log.info("Loading inline dataset", extra={"ctx_rows": len(data)})
        df = pd.DataFrame(data)
        meta = self._make_meta(df, name=name, source="inline", target_col=target_col)
        return df, meta

    def save_meta(self, meta: DatasetMeta) -> None:
        registry_path = Path(settings.ml_dataset_path)
        registry_path.mkdir(parents=True, exist_ok=True)
        meta_file = registry_path / f"{meta.id}.json"
        meta_file.write_text(json.dumps(asdict(meta), indent=2))
        log.debug("Dataset meta saved", extra={"ctx_id": meta.id})

    # ── Internal ───────────────────────────────────────────────────────────────

    def _make_meta(self, df, name: str, source: str, target_col: str | None) -> DatasetMeta:
        feature_cols = [c for c in df.columns if c != target_col] if target_col else list(df.columns)
        return DatasetMeta(
            id=self._compute_hash(df),
            name=name,
            source=source,
            row_count=len(df),
            feature_columns=feature_cols,
            target_column=target_col,
            created_at=datetime.now(timezone.utc).isoformat(),
        )

    def _compute_hash(self, df) -> str:
        import pandas as pd
        raw = df.to_json(orient="records")
        return hashlib.sha256(raw.encode()).hexdigest()[:16]
