"""Dataset inspection and ML preprocessing for VANTA.

The previous version dropped duplicate rows and trimmed whitespace. That is
tidying, not preparation: a model trained on the output would still meet
columns stored as text, a target that leaks, an ID column acting as a feature,
and missing values it has no policy for.

Two principles shape everything here.

Nothing is ever done to the source file. Cleaning produces a preview alongside
it and a report of every decision; the original is only replaced when asked for
explicitly. A destroyed dataset is not recoverable from an apology.

Nothing silently guesses. Every operation is reported with its reason and its
cost in rows or columns, because "cleaned successfully" tells you nothing about
whether the result is still the data you meant to model.
"""

from __future__ import annotations

import json
import os
from datetime import datetime

# A column whose values are this unique is an identifier, not a feature.
ID_UNIQUENESS = 0.95
# Above this fraction missing, imputing invents more than it recovers.
DROP_COLUMN_MISSING = 0.60
# Correlation with the target this high usually means the target leaked in.
LEAKAGE_CORRELATION = 0.98


def _read(path: str):
    import pandas as pd
    ext = os.path.splitext(path)[1].lower()
    if ext in {".parquet", ".pq"}:
        return pd.read_parquet(path)
    if ext in {".json", ".jsonl"}:
        return pd.read_json(path, lines=ext == ".jsonl")
    if ext in {".xlsx", ".xls"}:
        return pd.read_excel(path)
    if ext in {".tsv", ".tab"}:
        return pd.read_csv(path, sep="\t")
    return pd.read_csv(path)


def _write(frame, path: str):
    ext = os.path.splitext(path)[1].lower()
    if ext in {".parquet", ".pq"}:
        frame.to_parquet(path, index=False)
    elif ext == ".jsonl":
        frame.to_json(path, orient="records", lines=True)
    else:
        frame.to_csv(path, index=False)


def _is_text(series):
    """Whether a column holds text.

    Not `dtype == object`: pandas 3 gives string columns a dedicated `str`
    dtype, so that comparison is False for every string column and the checks
    guarded by it never run at all.
    """
    from pandas.api.types import is_datetime64_any_dtype, is_numeric_dtype
    return not is_numeric_dtype(series) and not is_datetime64_any_dtype(series)


def _numeric_like(series):
    """Is this text column actually numbers? ('1,200', ' 3.5 ', '42')"""
    import pandas as pd
    sample = series.dropna().astype(str).str.strip().str.replace(",", "", regex=False)
    if sample.empty:
        return False
    converted = pd.to_numeric(sample, errors="coerce")
    return converted.notna().mean() >= 0.95


def _date_like(name, series):
    import pandas as pd
    if not any(hint in str(name).lower() for hint in ("date", "time", "day", "month", "year")):
        return False
    sample = series.dropna().astype(str).head(200)
    if sample.empty:
        return False
    try:
        converted = pd.to_datetime(sample, errors="coerce", format="mixed")
    except Exception:
        return False
    return converted.notna().mean() >= 0.9


def profile_csv(path: str) -> str:
    """What is in this file, and what is wrong with it."""
    df = _read(path)
    rows = len(df)
    columns = {}
    for name in df.columns:
        series = df[name]
        nulls = int(series.isna().sum())
        unique = int(series.nunique(dropna=True))
        entry = {
            "dtype": str(series.dtype),
            "missing": nulls,
            "missing_pct": round(100 * nulls / rows, 1) if rows else 0.0,
            "unique": unique,
        }
        if series.dtype.kind in "ifc":
            described = series.describe()
            entry.update({
                "min": round(float(described.get("min", 0)), 4),
                "max": round(float(described.get("max", 0)), 4),
                "mean": round(float(described.get("mean", 0)), 4),
                "std": round(float(described.get("std", 0) or 0), 4),
            })
        else:
            top = series.dropna().astype(str).value_counts().head(3)
            entry["most_common"] = {str(k): int(v) for k, v in top.items()}
        columns[str(name)] = entry
    details = {
        "path": os.path.abspath(path),
        "rows": rows,
        "columns": len(df.columns),
        "duplicate_rows": int(df.duplicated().sum()),
        "memory_mb": round(df.memory_usage(deep=True).sum() / 1024 / 1024, 2),
        "column_detail": columns,
    }
    return "[Dataset Profile]\n" + json.dumps(details, indent=2, default=str)


def audit(path: str, target: str | None = None) -> str:
    """Everything that would hurt a model, ranked by how much it matters."""
    import numpy as np
    import pandas as pd

    df = _read(path)
    rows = len(df)
    if not rows:
        return "[Dataset Audit]\nThe file has no rows."

    blocking, warnings, notes = [], [], []

    duplicates = int(df.duplicated().sum())
    if duplicates:
        warnings.append(f"{duplicates} exact duplicate rows ({100*duplicates/rows:.1f}%) "
                        "— these inflate any score computed after a random split.")

    for name in df.columns:
        series = df[name]
        nulls = int(series.isna().sum())
        share = nulls / rows
        unique = int(series.nunique(dropna=True))
        if unique <= 1:
            detail = f" and {100*share:.0f}% missing" if share else ""
            warnings.append(
                f"'{name}' has a single value{detail} — it carries no information.")
            continue
        if share >= DROP_COLUMN_MISSING:
            warnings.append(f"'{name}' is {100*share:.0f}% missing — imputing it would "
                            "invent more than it recovers.")
        elif share > 0:
            notes.append(f"'{name}' is {100*share:.1f}% missing and needs an imputation policy.")

        if _is_text(series) and unique / rows >= ID_UNIQUENESS:
            warnings.append(f"'{name}' is unique in {100*unique/rows:.0f}% of rows — this "
                            "looks like an identifier, not a feature. Used as one it will "
                            "memorise the training set.")
        if _is_text(series) and _numeric_like(series):
            blocking.append(f"'{name}' holds numbers stored as text — it will be treated "
                            "as a category unless converted.")
        if _is_text(series) and _date_like(name, series):
            notes.append(f"'{name}' looks like a date stored as text.")
        if _is_text(series):
            kinds = set(series.dropna().map(type))
            if len(kinds) > 1:
                warnings.append(f"'{name}' mixes types: "
                                f"{', '.join(sorted(k.__name__ for k in kinds))}.")

    if target and target in df.columns:
        labels = df[target]
        if labels.isna().any():
            blocking.append(f"The target '{target}' is missing in "
                            f"{int(labels.isna().sum())} rows. Those rows cannot be trained on.")
        if labels.nunique(dropna=True) <= 20:
            counts = labels.value_counts(normalize=True)
            smallest = counts.min()
            if smallest < 0.05:
                warnings.append(
                    f"Class imbalance in '{target}': the rarest class is "
                    f"{100*smallest:.1f}% of rows. Accuracy will be misleading; "
                    "stratify the split and prefer balanced metrics.")
            notes.append(f"'{target}' has {labels.nunique()} classes — "
                         f"{'binary' if labels.nunique() == 2 else 'multiclass'} classification.")
        else:
            notes.append(f"'{target}' is continuous — a regression problem.")

        numeric = df.select_dtypes(include=[np.number])
        if target in numeric.columns and len(numeric.columns) > 1:
            correlation = numeric.corr(numeric_only=True)[target].drop(target).abs()
            for name, value in correlation.items():
                if value >= LEAKAGE_CORRELATION:
                    blocking.append(
                        f"'{name}' correlates {value:.3f} with the target — almost "
                        "certainly leakage. A model using it will look perfect and fail "
                        "in production.")
        for name in df.columns:
            if name == target:
                continue
            try:
                if df.groupby(name, dropna=True)[target].nunique().max() == 1 \
                        and df[name].nunique() > 1 and df[name].nunique() < rows * 0.5:
                    blocking.append(f"'{name}' determines '{target}' exactly — leakage.")
            except Exception:
                continue
    elif target:
        blocking.append(f"There is no column called '{target}' in this file.")

    report = {
        "path": os.path.abspath(path),
        "rows": rows,
        "columns": len(df.columns),
        "target": target,
        "blocking": blocking,
        "warnings": warnings,
        "notes": notes,
        "verdict": ("not ready to train" if blocking else
                    "usable, with caveats" if warnings else "ready"),
    }
    return "[Dataset Audit]\n" + json.dumps(report, indent=2, default=str)


def create_cleaning_preview(path: str, output_dir: str | None = None,
                            target: str | None = None, impute: str = "median",
                            drop_outliers: bool = False,
                            drop_high_missing: bool = True) -> tuple[str, str]:
    """Produce a cleaned copy beside the source and report every decision."""
    import numpy as np
    import pandas as pd

    source = os.path.abspath(path)
    df = _read(source)
    before_rows, before_cols = len(df), len(df.columns)
    operations: list[str] = []

    if target and target in df.columns:
        missing_target = int(df[target].isna().sum())
        if missing_target:
            df = df[df[target].notna()]
            operations.append(f"Dropped {missing_target} rows with no '{target}' — "
                              "a row without a label cannot be trained on.")

    duplicates = int(df.duplicated().sum())
    if duplicates:
        df = df.drop_duplicates()
        operations.append(f"Removed {duplicates} exact duplicate rows.")

    for name in list(df.select_dtypes(include=["object", "string"]).columns):
        stripped = df[name].astype("string").str.strip()
        if not stripped.equals(df[name].astype("string")):
            df[name] = stripped
            operations.append(f"Trimmed whitespace in '{name}'.")

    for name in list(df.select_dtypes(include=["object", "string"]).columns):
        if _numeric_like(df[name]):
            df[name] = pd.to_numeric(
                df[name].astype(str).str.strip().str.replace(",", "", regex=False),
                errors="coerce")
            operations.append(f"Converted '{name}' from text to numeric.")
        elif _date_like(name, df[name]):
            df[name] = pd.to_datetime(df[name], errors="coerce", format="mixed")
            operations.append(f"Parsed '{name}' as dates.")

    for name in list(df.columns):
        if name == target:
            continue
        if df[name].nunique(dropna=True) <= 1:
            df = df.drop(columns=[name])
            operations.append(f"Dropped '{name}' — it held a single value.")

    if drop_high_missing:
        for name in list(df.columns):
            if name == target:
                continue
            share = df[name].isna().mean()
            if share >= DROP_COLUMN_MISSING:
                df = df.drop(columns=[name])
                operations.append(f"Dropped '{name}' — {100*share:.0f}% missing.")

    imputed = []
    for name in list(df.columns):
        if name == target or not df[name].isna().any():
            continue
        series = df[name]
        if series.dtype.kind in "ifc":
            value = series.median() if impute == "median" else series.mean()
            df[name] = series.fillna(value)
            imputed.append(f"{name} (numeric -> {impute} {round(float(value), 4)})")
        else:
            mode = series.mode(dropna=True)
            if not mode.empty:
                df[name] = series.fillna(mode.iloc[0])
                imputed.append(f"{name} (category -> most common '{mode.iloc[0]}')")
    if imputed:
        operations.append("Filled missing values: " + "; ".join(imputed) + ".")

    if drop_outliers:
        numeric = df.select_dtypes(include=[np.number]).columns.drop(
            target, errors="ignore")
        removed_total = 0
        for name in numeric:
            q1, q3 = df[name].quantile(0.25), df[name].quantile(0.75)
            spread = q3 - q1
            if spread <= 0:
                continue
            mask = (df[name] < q1 - 3 * spread) | (df[name] > q3 + 3 * spread)
            removed_total += int(mask.sum())
            df = df[~mask]
        if removed_total:
            operations.append(f"Removed {removed_total} extreme outlier rows "
                              "(beyond 3x IQR). Verify these were errors, not rare truth.")

    target_dir = output_dir or os.path.join(os.path.dirname(source), "vanta_previews")
    os.makedirs(target_dir, exist_ok=True)
    stem, ext = os.path.splitext(os.path.basename(source))
    preview_path = os.path.join(target_dir, f"{stem}.vanta_preview{ext or '.csv'}")
    _write(df, preview_path)

    report = {
        "source": source,
        "preview": preview_path,
        "rows_before": before_rows, "rows_after": int(len(df)),
        "columns_before": before_cols, "columns_after": int(len(df.columns)),
        "operations": operations or ["Nothing needed changing."],
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "next_step": ("Review the preview, then KEEP_DATASET_PREVIEW to adopt it. "
                      "The source file has not been touched."),
    }
    if before_rows and len(df) / before_rows < 0.9:
        report["caution"] = (
            f"This removed {100*(1-len(df)/before_rows):.0f}% of the rows. "
            "Check that is what you intended before keeping it.")
    return preview_path, "[Dataset Cleaning Preview]\n" + json.dumps(report, indent=2, default=str)


def split_dataset(path: str, target: str | None = None, test_size: float = 0.2,
                  validation_size: float = 0.0, stratify: bool = True,
                  seed: int = 42, output_dir: str | None = None) -> str:
    """Split into train/validation/test, stratified where that makes sense."""
    from sklearn.model_selection import train_test_split

    source = os.path.abspath(path)
    df = _read(source)
    if target and target not in df.columns:
        return f"[Error] There is no column called '{target}' in this file."

    labels = df[target] if (target and stratify) else None
    # Stratifying on a continuous target, or on a class with one member, fails.
    if labels is not None and (labels.nunique() > 20 or labels.value_counts().min() < 2):
        labels = None

    train, test = train_test_split(df, test_size=test_size, random_state=seed,
                                   stratify=labels)
    parts = {"train": train, "test": test}
    if validation_size > 0:
        inner = df[target] if (target and labels is not None) else None
        stratify_inner = train[target] if inner is not None else None
        share = validation_size / (1 - test_size)
        train, validation = train_test_split(train, test_size=share, random_state=seed,
                                             stratify=stratify_inner)
        parts = {"train": train, "validation": validation, "test": test}

    target_dir = output_dir or os.path.join(os.path.dirname(source), "vanta_splits")
    os.makedirs(target_dir, exist_ok=True)
    stem, ext = os.path.splitext(os.path.basename(source))
    written = {}
    for name, frame in parts.items():
        out = os.path.join(target_dir, f"{stem}.{name}{ext or '.csv'}")
        _write(frame, out)
        written[name] = {"path": out, "rows": int(len(frame))}

    report = {
        "source": source, "target": target, "seed": seed,
        "stratified": labels is not None,
        "splits": written,
    }
    if target and labels is None and stratify:
        report["note"] = ("Not stratified: the target is continuous or has a class with "
                          "fewer than two rows.")
    return "[Dataset Split]\n" + json.dumps(report, indent=2, default=str)


def keep_preview(preview_path: str, destination_path: str) -> str:
    import shutil

    preview = os.path.abspath(preview_path)
    destination = os.path.abspath(destination_path)
    if not os.path.isfile(preview):
        raise FileNotFoundError(f"Preview does not exist: {preview}")
    if preview == destination:
        return "Preview already is the requested destination."
    shutil.copy2(preview, destination)
    return f"Kept reviewed dataset at: {destination}"
