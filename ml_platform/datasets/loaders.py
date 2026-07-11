"""
ML Platform — Dataset Layer: Concrete Loaders
=============================================
One loader per data format. All extend BaseDataset.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator

from ml_platform.datasets.base import (
    BaseDataset, BaseStreamingDataset,
    DatasetFormat, DatasetMetadata, DatasetRecord, DatasetSplit, ValidationStatus,
)


# ── CSV ────────────────────────────────────────────────────────────────────────

class CSVDatasetLoader(BaseDataset):
    """
    Loads delimited text files (CSV, TSV, pipe-separated).
    Uses pandas under the hood; configurable via kwargs.
    """

    def load(
        self,
        source: str,
        target_col: str | None = None,
        split: DatasetSplit = DatasetSplit.FULL,
        **kwargs,
    ) -> DatasetRecord:
        import pandas as pd
        df = pd.read_csv(source, **kwargs)
        metadata = self._build_metadata(df, source, target_col, split, DatasetFormat.CSV)
        return DatasetRecord(metadata=metadata, data=df)

    def validate(self, record: DatasetRecord) -> ValidationStatus:
        # TODO: null-ratio checks, dtype consistency, target class balance
        return ValidationStatus.PENDING

    def save_metadata(self, metadata: DatasetMetadata) -> None:
        # TODO: write to dataset registry store (JSON + index)
        pass

    def _build_metadata(self, df, source, target_col, split, fmt) -> DatasetMetadata:
        import pandas as pd
        feature_cols = [c for c in df.columns if c != target_col] if target_col else list(df.columns)
        raw = df.to_json(orient="records").encode()
        return DatasetMetadata(
            dataset_id=self.compute_hash(raw),
            name=Path(source).stem,
            version=self.make_version(),
            format=fmt,
            source=source,
            row_count=len(df),
            feature_columns=feature_cols,
            target_column=target_col,
            schema={c: str(df[c].dtype) for c in df.columns},
            split=split,
        )


# ── Parquet ────────────────────────────────────────────────────────────────────

class ParquetDatasetLoader(BaseDataset):
    """
    Loads columnar Parquet files.
    Supports partitioned datasets (directory of .parquet files).
    """

    def load(
        self,
        source: str,
        target_col: str | None = None,
        split: DatasetSplit = DatasetSplit.FULL,
        columns: list[str] | None = None,
        **kwargs,
    ) -> DatasetRecord:
        import pandas as pd
        df = pd.read_parquet(source, columns=columns, **kwargs)
        feature_cols = [c for c in df.columns if c != target_col] if target_col else list(df.columns)
        raw = df.to_json(orient="records").encode()
        metadata = DatasetMetadata(
            dataset_id=self.compute_hash(raw),
            name=Path(source).stem,
            version=self.make_version(),
            format=DatasetFormat.PARQUET,
            source=source,
            row_count=len(df),
            feature_columns=feature_cols,
            target_column=target_col,
            schema={c: str(df[c].dtype) for c in df.columns},
            split=split,
        )
        return DatasetRecord(metadata=metadata, data=df)

    def validate(self, record: DatasetRecord) -> ValidationStatus:
        # TODO: schema drift check against registered schema version
        return ValidationStatus.PENDING

    def save_metadata(self, metadata: DatasetMetadata) -> None:
        pass


# ── JSON ───────────────────────────────────────────────────────────────────────

class JSONDatasetLoader(BaseDataset):
    """
    Loads JSON / JSONL datasets.
    Supports flat JSON arrays and newline-delimited (JSONL) format.
    """

    def load(
        self,
        source: str,
        target_col: str | None = None,
        split: DatasetSplit = DatasetSplit.FULL,
        orient: str = "records",
        **kwargs,
    ) -> DatasetRecord:
        import pandas as pd
        if source.endswith(".jsonl"):
            df = pd.read_json(source, lines=True, **kwargs)
        else:
            df = pd.read_json(source, orient=orient, **kwargs)
        feature_cols = [c for c in df.columns if c != target_col] if target_col else list(df.columns)
        raw = df.to_json(orient="records").encode()
        metadata = DatasetMetadata(
            dataset_id=self.compute_hash(raw),
            name=Path(source).stem,
            version=self.make_version(),
            format=DatasetFormat.JSON,
            source=source,
            row_count=len(df),
            feature_columns=feature_cols,
            target_column=target_col,
            schema={c: str(df[c].dtype) for c in df.columns},
            split=split,
        )
        return DatasetRecord(metadata=metadata, data=df)

    def validate(self, record: DatasetRecord) -> ValidationStatus:
        return ValidationStatus.PENDING

    def save_metadata(self, metadata: DatasetMetadata) -> None:
        pass


# ── Images ─────────────────────────────────────────────────────────────────────

class ImageDatasetLoader(BaseDataset):
    """
    Loads image datasets from a directory.

    Expected directory layout (ImageFolder convention):
        root/
            class_a/  img1.jpg  img2.png ...
            class_b/  img3.jpg ...

    Returns data as list[dict] with keys: {path, label, class_name}
    """

    SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".webp"}

    def load(
        self,
        source: str,
        split: DatasetSplit = DatasetSplit.FULL,
        recursive: bool = True,
        **kwargs,
    ) -> DatasetRecord:
        root = Path(source)
        records: list[dict] = []
        class_names = sorted([d.name for d in root.iterdir() if d.is_dir()])
        class_to_idx = {name: idx for idx, name in enumerate(class_names)}

        for class_dir in root.iterdir():
            if not class_dir.is_dir():
                continue
            label = class_to_idx.get(class_dir.name, -1)
            glob = class_dir.rglob("*") if recursive else class_dir.glob("*")
            for img_path in glob:
                if img_path.suffix.lower() in self.SUPPORTED_EXTENSIONS:
                    records.append({
                        "path": str(img_path),
                        "label": label,
                        "class_name": class_dir.name,
                    })

        content_hash = self.compute_hash(json.dumps(sorted(r["path"] for r in records)).encode())
        metadata = DatasetMetadata(
            dataset_id=content_hash,
            name=root.name,
            version=self.make_version(),
            format=DatasetFormat.IMAGES,
            source=source,
            row_count=len(records),
            feature_columns=["path"],
            target_column="label",
            schema={"path": "str", "label": "int", "class_name": "str"},
            split=split,
            tags={"num_classes": str(len(class_names))},
        )
        return DatasetRecord(metadata=metadata, data=records)

    def validate(self, record: DatasetRecord) -> ValidationStatus:
        # TODO: check class imbalance, verify files exist, check corrupted images
        return ValidationStatus.PENDING

    def save_metadata(self, metadata: DatasetMetadata) -> None:
        pass


# ── Text ───────────────────────────────────────────────────────────────────────

class TextDatasetLoader(BaseDataset):
    """
    Loads plain text datasets (one document per line, or full files).
    Returns data as list[dict] with keys: {text, label (optional)}
    """

    def load(
        self,
        source: str,
        label_col: str | None = None,
        split: DatasetSplit = DatasetSplit.FULL,
        encoding: str = "utf-8",
        **kwargs,
    ) -> DatasetRecord:
        path = Path(source)
        lines = path.read_text(encoding=encoding).splitlines()
        records = [{"text": line, "idx": i} for i, line in enumerate(lines) if line.strip()]
        content_hash = self.compute_hash(path.read_bytes())
        metadata = DatasetMetadata(
            dataset_id=content_hash,
            name=path.stem,
            version=self.make_version(),
            format=DatasetFormat.TEXT,
            source=source,
            row_count=len(records),
            feature_columns=["text"],
            target_column=label_col,
            schema={"text": "str", "idx": "int"},
            split=split,
        )
        return DatasetRecord(metadata=metadata, data=records)

    def validate(self, record: DatasetRecord) -> ValidationStatus:
        # TODO: check encoding, language detection, min/max token length
        return ValidationStatus.PENDING

    def save_metadata(self, metadata: DatasetMetadata) -> None:
        pass


# ── Streaming ──────────────────────────────────────────────────────────────────

class StreamingDatasetLoader(BaseStreamingDataset):
    """
    Memory-safe loader for datasets that exceed available RAM.
    Reads Parquet, CSV, or JSONL in chunks via an iterator.
    Designed for integration with PyTorch DataLoader / tf.data.

    Usage:
        loader = StreamingDatasetLoader()
        for batch_df in loader.stream("huge.parquet", batch_size=1024):
            process(batch_df)
    """

    def stream(
        self,
        source: str,
        batch_size: int = 1024,
        **kwargs,
    ) -> Iterator[Any]:
        path = Path(source)
        suffix = path.suffix.lower()

        if suffix == ".csv":
            yield from self._stream_csv(source, batch_size, **kwargs)
        elif suffix == ".parquet":
            yield from self._stream_parquet(source, batch_size, **kwargs)
        elif suffix in {".json", ".jsonl"}:
            yield from self._stream_jsonl(source, batch_size, **kwargs)
        else:
            raise ValueError(f"Streaming not supported for: {suffix}")

    def estimate_size(self, source: str) -> int:
        # TODO: read metadata/footer without full scan (Parquet row group stats)
        return -1

    def _stream_csv(self, source: str, batch_size: int, **kwargs) -> Iterator[Any]:
        import pandas as pd
        for chunk in pd.read_csv(source, chunksize=batch_size, **kwargs):
            yield chunk

    def _stream_parquet(self, source: str, batch_size: int, **kwargs) -> Iterator[Any]:
        import pandas as pd
        import pyarrow.parquet as pq
        pf = pq.ParquetFile(source)
        for batch in pf.iter_batches(batch_size=batch_size):
            yield batch.to_pandas()

    def _stream_jsonl(self, source: str, batch_size: int, **kwargs) -> Iterator[Any]:
        import pandas as pd
        for chunk in pd.read_json(source, lines=True, chunksize=batch_size, **kwargs):
            yield chunk


# ── Registry / Factory ─────────────────────────────────────────────────────────

_LOADER_MAP: dict[DatasetFormat, type[BaseDataset]] = {
    DatasetFormat.CSV:     CSVDatasetLoader,
    DatasetFormat.PARQUET: ParquetDatasetLoader,
    DatasetFormat.JSON:    JSONDatasetLoader,
    DatasetFormat.IMAGES:  ImageDatasetLoader,
    DatasetFormat.TEXT:    TextDatasetLoader,
}


def get_loader(format: DatasetFormat) -> BaseDataset:
    """Factory: return the correct loader for a given format."""
    cls = _LOADER_MAP.get(format)
    if cls is None:
        raise ValueError(f"No loader registered for format: {format}")
    return cls()
