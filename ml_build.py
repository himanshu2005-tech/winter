# -*- coding: utf-8 -*-
"""Resumable Kaggle-to-model jobs for VANTA Build Mode.

The LLM selects the goal and may name a dataset/target.  This module owns the
repeatable mechanics: credential isolation, bounded download, safe extraction,
data inspection, cleaning, training, evaluation and durable receipts.  Long
work runs off the conversational thread and can be inspected or cancelled.
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

_LOCK = threading.RLock()
_JOBS = {}
MAX_DOWNLOAD_BYTES = int(os.getenv("VANTA_KAGGLE_MAX_BYTES", str(750 * 1024 * 1024)))
MAX_EXTRACTED_BYTES = int(os.getenv("VANTA_KAGGLE_MAX_EXTRACTED_BYTES", str(2 * 1024**3)))


def _now():
    return datetime.now(timezone.utc).isoformat()


def _root(base_dir):
    path = Path(base_dir) / "vault" / "system" / "ml_jobs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _record_path(base_dir, job_id):
    return _root(base_dir) / f"{job_id}.json"


def _write(base_dir, row):
    row["updated_at"] = _now()
    path = _record_path(base_dir, row["id"])
    temporary = path.with_name(path.name + f".{threading.get_ident()}.tmp")
    with _LOCK:
        temporary.write_text(json.dumps(row, indent=2, ensure_ascii=False,
                                        default=str), encoding="utf-8")
        # Windows indexers/test readers can briefly hold the destination.
        # Keep the record atomic, but tolerate that short sharing violation.
        for attempt in range(30):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if attempt == 29:
                    raise
                time.sleep(.025)


def _read(base_dir, job_id):
    return json.loads(_record_path(base_dir, job_id).read_text(encoding="utf-8"))


def _slug(value):
    cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "-", str(value or "").strip()).strip("-")
    return cleaned[:48] or "ml-project"


def _kaggle(args, timeout=1800):
    if not os.getenv("KAGGLE_API_TOKEN", "").strip():
        raise RuntimeError("KAGGLE_API_TOKEN is not configured in VANTA's environment.")
    command = [sys.executable, "-m", "kaggle"] + list(args)
    child_env = os.environ.copy()
    # Kaggle titles frequently contain emoji; Windows' legacy console codec
    # otherwise makes the official CLI exit after printing only part of CSV.
    child_env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    done = subprocess.run(command, capture_output=True, text=True, env=child_env,
                          encoding="utf-8", errors="replace", timeout=timeout,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if done.returncode:
        # Kaggle errors are useful, but command lines and environment are not
        # included, so the token can never enter a receipt.
        detail = (done.stderr or done.stdout or "Kaggle command failed").strip()
        raise RuntimeError(detail[-1800:])
    return done.stdout


def search_datasets(query, limit=8):
    output = _kaggle(["datasets", "list", "--search", str(query),
                      "--file-type", "csv", "--max-size", str(MAX_DOWNLOAD_BYTES),
                      "--sort-by", "votes", "--csv"], timeout=60)
    rows = []
    for row in csv.DictReader(io.StringIO(output)):
        ref = row.get("ref") or row.get("Ref")
        if not ref:
            continue
        rows.append({"ref": ref, "title": row.get("title") or row.get("Title") or ref,
                     "size": row.get("size") or row.get("Size"),
                     "votes": row.get("votes") or row.get("Votes") or row.get("voteCount")})
        if len(rows) >= int(limit):
            break
    return rows


def _safe_extract(archive, destination):
    destination = Path(destination).resolve()
    total = 0
    with zipfile.ZipFile(archive) as zipped:
        for member in zipped.infolist():
            total += member.file_size
            target = (destination / member.filename).resolve()
            if destination != target and destination not in target.parents:
                raise RuntimeError(f"Unsafe path in Kaggle archive: {member.filename}")
            if total > MAX_EXTRACTED_BYTES:
                raise RuntimeError("Dataset expands beyond VANTA's configured safety limit.")
        zipped.extractall(destination)


def _dataset_file(root):
    allowed = {".csv", ".tsv", ".parquet", ".pq", ".json", ".jsonl", ".xlsx", ".xls"}
    files = [p for p in Path(root).rglob("*") if p.is_file() and p.suffix.lower() in allowed]
    if not files:
        raise RuntimeError("The downloaded dataset has no supported tabular file.")
    # Training on the largest table is a better default than README samples.
    return max(files, key=lambda path: path.stat().st_size)


def _infer_target(frame, requested=""):
    if requested:
        if requested not in frame.columns:
            raise RuntimeError(f"Target column {requested!r} is not in the dataset.")
        return requested
    names = {str(name).casefold(): name for name in frame.columns}
    for candidate in ("target", "label", "class", "outcome", "response", "y"):
        if candidate in names:
            return names[candidate]
    last = frame.columns[-1]
    unique = frame[last].nunique(dropna=True)
    if 1 < unique <= max(20, int(len(frame) * 0.2)):
        return last
    raise RuntimeError(
        "The data downloaded and profiled correctly, but its prediction target is ambiguous. "
        "Run ML_BUILD again with target=\"column name\"; no model was guessed.")


def _train(data_path, target, model_kind, output_dir, seed=42, max_rows=250000):
    import joblib
    import numpy as np
    import pandas as pd
    from sklearn.compose import ColumnTransformer
    from sklearn.impute import SimpleImputer
    from sklearn.metrics import (accuracy_score, f1_score, mean_absolute_error,
                                 mean_squared_error, r2_score)
    from sklearn.model_selection import train_test_split
    from sklearn.neural_network import MLPClassifier, MLPRegressor
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import OneHotEncoder, StandardScaler
    import dataset_tools

    frame = dataset_tools._read(str(data_path))
    if len(frame) > max_rows:
        frame = frame.sample(max_rows, random_state=seed)
    target = _infer_target(frame, target)
    frame = frame[frame[target].notna()].drop_duplicates()
    y = frame.pop(target)
    # Identifier-like strings memorize examples rather than generalise.
    drop = [name for name in frame.columns
            if frame[name].dtype.kind not in "ifcb" and frame[name].nunique(dropna=True) / max(1, len(frame)) >= .95]
    frame = frame.drop(columns=drop)
    if frame.empty:
        raise RuntimeError("No usable feature columns remain after leakage/identifier checks.")
    categorical = list(frame.select_dtypes(exclude=[np.number, "bool"]).columns)
    numeric = [name for name in frame.columns if name not in categorical]
    transforms = []
    if numeric:
        transforms.append(("numeric", Pipeline([
            ("impute", SimpleImputer(strategy="median")),
            ("scale", StandardScaler()),
        ]), numeric))
    if categorical:
        transforms.append(("categorical", Pipeline([
            ("impute", SimpleImputer(strategy="most_frequent")),
            ("encode", OneHotEncoder(handle_unknown="ignore", min_frequency=2)),
        ]), categorical))
    classification = (not pd.api.types.is_numeric_dtype(y)
                      or y.nunique(dropna=True) <= 20)
    stratify = y if classification and y.value_counts().min() >= 2 else None
    x_train, x_test, y_train, y_test = train_test_split(
        frame, y, test_size=.2, random_state=seed, stratify=stratify)
    hidden = (128, 64, 32) if "neural" in model_kind.lower() else (64, 32)
    estimator = (MLPClassifier(hidden_layer_sizes=hidden, max_iter=300,
                               early_stopping=True, random_state=seed)
                 if classification else
                 MLPRegressor(hidden_layer_sizes=hidden, max_iter=300,
                              early_stopping=True, random_state=seed))
    pipeline = Pipeline([("prepare", ColumnTransformer(transforms)),
                         ("model", estimator)])
    pipeline.fit(x_train, y_train)
    prediction = pipeline.predict(x_test)
    if classification:
        metrics = {"accuracy": float(accuracy_score(y_test, prediction)),
                   "f1_weighted": float(f1_score(y_test, prediction, average="weighted"))}
        task = "classification"
    else:
        metrics = {"mae": float(mean_absolute_error(y_test, prediction)),
                   "rmse": float(mean_squared_error(y_test, prediction) ** .5),
                   "r2": float(r2_score(y_test, prediction))}
        task = "regression"
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    model_path = output_dir / "model.joblib"
    joblib.dump(pipeline, model_path)
    report = {"task": task, "target": str(target), "model": type(estimator).__name__,
              "rows": int(len(frame)), "train_rows": int(len(x_train)),
              "test_rows": int(len(x_test)), "features": list(map(str, frame.columns)),
              "dropped_identifier_columns": list(map(str, drop)), "metrics": metrics,
              "model_path": str(model_path)}
    (output_dir / "metrics.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def start(base_dir, query, dataset="", target="", project="", model="neural_network"):
    import projects
    if not str(query or dataset).strip():
        raise ValueError("ML_BUILD needs a dataset reference or a search query.")
    project_name = _slug(project or f"ml-{query}")
    project_dir = Path(projects.project_path(base_dir, project_name))
    project_dir.mkdir(parents=True, exist_ok=True)
    job_id = "ml-" + uuid.uuid4().hex[:12]
    row = {"id": job_id, "status": "queued", "stage": "queued",
           "query": query, "dataset": dataset, "target": target,
           "model": model, "project": project_name, "project_dir": str(project_dir),
           "created_at": _now(), "updated_at": _now(), "error": "", "artifacts": {}}
    cancel_event = threading.Event()
    with _LOCK:
        _JOBS[job_id] = cancel_event
    _write(base_dir, row)

    def checkpoint(stage, **updates):
        row.update(stage=stage, **updates)
        _write(base_dir, row)
        if cancel_event.is_set():
            raise InterruptedError("ML build cancelled.")

    def worker():
        try:
            checkpoint("dataset_search", status="running")
            chosen = dataset.strip()
            candidates = []
            if not chosen:
                candidates = search_datasets(query, limit=8)
                if not candidates:
                    raise RuntimeError("Kaggle returned no suitable CSV dataset.")
                chosen = candidates[0]["ref"]
            if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", chosen):
                raise RuntimeError("Kaggle dataset must be owner/dataset-name.")
            checkpoint("download", dataset=chosen, candidates=candidates[:5])
            raw_dir = project_dir / "data" / "raw"
            raw_dir.mkdir(parents=True, exist_ok=True)
            before = set(raw_dir.iterdir())
            _kaggle(["datasets", "download", chosen, "--path", str(raw_dir),
                     "--quiet"], timeout=1800)
            downloaded = [p for p in raw_dir.iterdir() if p not in before]
            for archive in downloaded:
                if archive.suffix.lower() == ".zip":
                    _safe_extract(archive, raw_dir)
            source = _dataset_file(raw_dir)
            checkpoint("profile", source_file=str(source))
            import dataset_tools
            profile = dataset_tools.profile_csv(str(source))
            frame = dataset_tools._read(str(source))
            selected_target = _infer_target(frame, target)
            checkpoint("clean", target=selected_target,
                       profile=json.loads(profile.split("\n", 1)[1]))
            clean_dir = project_dir / "data" / "processed"
            preview, cleaning = dataset_tools.create_cleaning_preview(
                str(source), str(clean_dir), target=selected_target)
            checkpoint("train", cleaned_file=preview,
                       cleaning=json.loads(cleaning.split("\n", 1)[1]))
            report = _train(preview, selected_target, model,
                            project_dir / "artifacts")
            readme = (f"# {project_name}\n\nAutomated VANTA Build Mode ML project.\n\n"
                      f"- Kaggle dataset: `{chosen}`\n- Target: `{selected_target}`\n"
                      f"- Task: {report['task']}\n- Model: {report['model']}\n"
                      f"- Metrics: `{json.dumps(report['metrics'])}`\n\n"
                      "Raw source data is preserved under `data/raw`; cleaned data and "
                      "the fitted pipeline are separate artifacts.\n")
            (project_dir / "README.md").write_text(readme, encoding="utf-8")
            checkpoint("verified", status="completed",
                       artifacts={"model": report["model_path"],
                                  "metrics": str(project_dir / "artifacts" / "metrics.json"),
                                  "readme": str(project_dir / "README.md")},
                       result=report)
        except InterruptedError as exc:
            checkpoint("cancelled", status="cancelled", error=str(exc))
        except Exception as exc:
            checkpoint("failed", status="failed", error=str(exc))
        finally:
            with _LOCK:
                _JOBS.pop(job_id, None)

    threading.Thread(target=worker, daemon=True, name=job_id).start()
    return (f"[Success] Started ML build {job_id} in {project_name}. "
            "It will search/download Kaggle data, preserve the raw files, clean a copy, "
            "train, evaluate and save the model in the background.")


def status(base_dir, job_id):
    row = _read(base_dir, job_id)
    result = {key: row.get(key) for key in
              ("id", "status", "stage", "project", "dataset", "target",
               "result", "artifacts", "error") if row.get(key) not in (None, "", {})}
    return "[ML Build Status]\n" + json.dumps(result, indent=2, ensure_ascii=False)


def cancel(base_dir, job_id):
    with _LOCK:
        event = _JOBS.get(job_id)
    if event is None:
        row = _read(base_dir, job_id)
        return f"[Attention] ML build {job_id} is not running ({row['status']})."
    event.set()
    return f"[Success] Cancellation requested for {job_id}."


def list_jobs(base_dir, limit=20):
    rows = []
    for path in sorted(_root(base_dir).glob("ml-*.json"),
                       key=lambda p: p.stat().st_mtime, reverse=True)[:int(limit)]:
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
            rows.append(f"- {item['id']}: {item['status']} / {item['stage']} ({item['project']})")
        except Exception:
            continue
    return "[ML Builds]\n" + ("\n".join(rows) if rows else "No ML builds recorded.")
