import json
import time
import zipfile
from pathlib import Path

import ml_build


def _wait(base, job_id, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        row = json.loads((Path(base) / "vault" / "system" / "ml_jobs" /
                          f"{job_id}.json").read_text(encoding="utf-8"))
        if row["status"] in {"completed", "failed", "cancelled"}:
            return row
        time.sleep(.05)
    raise AssertionError("ML job did not settle")


def test_kaggle_build_downloads_cleans_trains_and_saves_artifact(tmp_path, monkeypatch):
    monkeypatch.setenv("KAGGLE_API_TOKEN", "test-token-never-logged")

    def fake_kaggle(args, timeout=0):
        assert "test-token-never-logged" not in " ".join(args)
        destination = Path(args[args.index("--path") + 1])
        destination.mkdir(parents=True, exist_ok=True)
        content = "x,color,target\n" + "\n".join(
            f"{i},{'red' if i % 2 else 'blue'},{i % 2}" for i in range(80))
        with zipfile.ZipFile(destination / "data.zip", "w") as archive:
            archive.writestr("train.csv", content)
        return ""

    monkeypatch.setattr(ml_build, "_kaggle", fake_kaggle)
    result = ml_build.start(str(tmp_path), "binary sample",
                            dataset="owner/sample", target="target",
                            project="sample-model")
    job_id = result.split("ML build ", 1)[1].split(" ", 1)[0]
    row = _wait(tmp_path, job_id)
    assert row["status"] == "completed", row.get("error")
    assert Path(row["artifacts"]["model"]).is_file()
    assert Path(row["artifacts"]["metrics"]).is_file()
    assert row["result"]["target"] == "target"


def test_ambiguous_target_fails_honestly(tmp_path, monkeypatch):
    monkeypatch.setenv("KAGGLE_API_TOKEN", "secret")

    def fake_kaggle(args, timeout=0):
        destination = Path(args[args.index("--path") + 1])
        destination.mkdir(parents=True, exist_ok=True)
        content = "first,second\n" + "\n".join(f"{i},{i * 1.5}" for i in range(50))
        with zipfile.ZipFile(destination / "data.zip", "w") as archive:
            archive.writestr("data.csv", content)
        return ""

    monkeypatch.setattr(ml_build, "_kaggle", fake_kaggle)
    result = ml_build.start(str(tmp_path), "ambiguous",
                            dataset="owner/ambiguous", project="ambiguous")
    job_id = result.split("ML build ", 1)[1].split(" ", 1)[0]
    row = _wait(tmp_path, job_id)
    assert row["status"] == "failed"
    assert "target is ambiguous" in row["error"]


def test_safe_extract_rejects_zip_slip(tmp_path):
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("../escape.csv", "x\n1")
    try:
        ml_build._safe_extract(archive, tmp_path / "out")
    except RuntimeError as exc:
        assert "Unsafe path" in str(exc)
    else:
        raise AssertionError("zip-slip archive was accepted")
