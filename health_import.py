# -*- coding: utf-8 -*-
"""
health_import.py — turning a phone health export into something VANTA can use.

HEALTH mode could change its tone but had nothing to reason over. This is the
data source for it.

The exports are large. An Apple Health `export.xml` is routinely 100-500 MB and
holds hundreds of thousands of records; a year of heart-rate samples alone runs
to six figures. So this never loads the file into memory and never puts raw
records anywhere near a prompt. It streams the file, rolls it up by day, keeps
the summaries, and throws the samples away.

What gets stored:
  - a handful of durable facts (typical sleep, resting heart rate, daily steps)
    marked `sensitive`, so the existing memory governance only surfaces them
    when the question is actually about health
  - monthly summaries as searchable memories, so "how was my sleep in June"
    has something to find
  - every fact carries the export and its date range as evidence

Formats detected automatically: Apple Health (export.xml or export.zip),
Samsung Health (CSV folder or zip), Google Fit / Takeout (CSV).
"""

import csv
import datetime
import hashlib
import os
import statistics
import zipfile
from collections import defaultdict

# Apple's identifiers, mapped to a short name and how a day's samples combine.
# "sum" for things you accumulate, "mean" for things you sample.
_APPLE_METRICS = {
    "HKQuantityTypeIdentifierStepCount": ("steps", "sum", "steps"),
    "HKQuantityTypeIdentifierDistanceWalkingRunning": ("distance_km", "sum", "km"),
    "HKQuantityTypeIdentifierFlightsClimbed": ("flights_climbed", "sum", "flights"),
    "HKQuantityTypeIdentifierActiveEnergyBurned": ("active_calories", "sum", "kcal"),
    "HKQuantityTypeIdentifierAppleExerciseTime": ("exercise_minutes", "sum", "min"),
    "HKQuantityTypeIdentifierHeartRate": ("heart_rate", "mean", "bpm"),
    "HKQuantityTypeIdentifierRestingHeartRate": ("resting_heart_rate", "mean", "bpm"),
    "HKQuantityTypeIdentifierHeartRateVariabilitySDNN": ("hrv", "mean", "ms"),
    "HKQuantityTypeIdentifierRespiratoryRate": ("respiratory_rate", "mean", "breaths/min"),
    "HKQuantityTypeIdentifierOxygenSaturation": ("blood_oxygen", "mean", "%"),
    "HKQuantityTypeIdentifierBodyMass": ("weight_kg", "mean", "kg"),
    "HKQuantityTypeIdentifierBodyFatPercentage": ("body_fat", "mean", "%"),
    "HKQuantityTypeIdentifierVO2Max": ("vo2_max", "mean", "ml/kg/min"),
}

# Which summaries are worth remembering as facts, and under what predicate.
_FACT_PREDICATES = {
    "sleep_hours": ("SLEEPS_HOURS", "typically sleeps {value} hours a night"),
    "steps": ("DAILY_STEPS", "averages {value} steps a day"),
    "resting_heart_rate": ("RESTING_HEART_RATE", "has a resting heart rate around {value} bpm"),
    "weight_kg": ("BODY_WEIGHT_KG", "weighs about {value} kg"),
    "exercise_minutes": ("DAILY_EXERCISE_MINUTES", "exercises about {value} minutes a day"),
    "hrv": ("HEART_RATE_VARIABILITY", "has an average HRV of {value} ms"),
}


def _day(value):
    """Apple writes '2026-06-01 07:14:22 +0530'."""
    try:
        return value[:10]
    except Exception:
        return ""


def _parse_apple(handle):
    """Stream export.xml, accumulating per-day values. Never holds the file."""
    import xml.etree.ElementTree as ET

    # Running [count, total] per metric per day rather than every sample.
    # Keeping the raw values would make memory grow with the number of records
    # -- around a million of them in a real year-long export -- instead of with
    # the number of days, which is at most a few thousand.
    daily = defaultdict(lambda: defaultdict(lambda: [0, 0.0]))
    sleep_spans = defaultdict(float)
    workouts = defaultdict(int)
    seen = 0

    context = ET.iterparse(handle, events=("start", "end"))
    _first, root = next(context)

    for event, element in context:
        if event != "end":
            continue
        tag = element.tag
        if tag == "Record":
            record_type = element.get("type", "")
            start = element.get("startDate", "")
            if record_type == "HKCategoryTypeIdentifierSleepAnalysis":
                value = element.get("value", "")
                # Older exports use InBed; newer ones split asleep by stage.
                if "Asleep" in value:
                    try:
                        begin = datetime.datetime.strptime(start[:19], "%Y-%m-%d %H:%M:%S")
                        end = datetime.datetime.strptime(
                            element.get("endDate", "")[:19], "%Y-%m-%d %H:%M:%S")
                        hours = (end - begin).total_seconds() / 3600
                        if 0 < hours < 24:
                            # Sleep starting before dawn belongs to the night before.
                            night = begin.date() - (datetime.timedelta(days=1)
                                                    if begin.hour < 12 else datetime.timedelta())
                            sleep_spans[night.isoformat()] += hours
                    except Exception:
                        pass
            elif record_type in _APPLE_METRICS:
                name, _how, _unit = _APPLE_METRICS[record_type]
                try:
                    bucket = daily[_day(start)][name]
                    bucket[0] += 1
                    bucket[1] += float(element.get("value", 0))
                except (TypeError, ValueError):
                    pass
            seen += 1
        elif tag == "Workout":
            workouts[_day(element.get("startDate", ""))] += 1
            seen += 1
        # Clearing the element alone is not enough: ElementTree keeps every
        # parsed child attached to the root, so the whole document still ends
        # up in memory. Dropping the root's accumulated children is what makes
        # a 400 MB export parse in a few MB.
        if tag in ("Record", "Workout"):
            element.clear()
            root.clear()

    for night, hours in sleep_spans.items():
        daily[night]["sleep_hours"] = [1, round(hours, 2)]
    for day, count in workouts.items():
        daily[day]["workouts"] = [1, float(count)]
    return daily, seen


def _collapse(daily):
    """One number per metric per day, using each metric's own rule."""
    how = {name: rule for name, rule, _ in _APPLE_METRICS.values()}
    how.update({"sleep_hours": "sum", "workouts": "sum"})
    collapsed = {}
    for day, metrics in daily.items():
        if not day:
            continue
        row = {}
        for name, bucket in metrics.items():
            count, total = bucket
            if not count:
                continue
            row[name] = round(total if how.get(name) == "sum" else total / count, 2)
        if row:
            collapsed[day] = row
    return collapsed


def _parse_csv_folder(folder):
    """Samsung Health and Google Fit both export day-per-row CSVs."""
    daily = defaultdict(dict)
    rows_seen = 0
    aliases = {
        "step_count": "steps", "steps": "steps", "total_steps": "steps",
        "calorie": "active_calories", "calories (kcal)": "active_calories",
        "active_calories (kcal)": "active_calories",
        "heart_rate": "heart_rate", "average heart rate (bpm)": "heart_rate",
        "sleep_duration": "sleep_hours", "weight": "weight_kg",
        "distance": "distance_km", "distance (m)": "distance_km",
        "move minutes count": "exercise_minutes",
    }
    for root, _dirs, files in os.walk(folder):
        for filename in files:
            if not filename.lower().endswith(".csv"):
                continue
            path = os.path.join(root, filename)
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as handle:
                    sample = handle.read(4096)
                    handle.seek(0)
                    if "," not in sample:
                        continue
                    reader = csv.DictReader(handle)
                    for row in reader:
                        day = ""
                        for key in row:
                            low = (key or "").strip().lower()
                            if low in ("date", "day", "start_time", "create_time", "com.samsung.health.step_count.create_time"):
                                day = str(row[key] or "")[:10]
                                break
                        if not day or not day[:4].isdigit():
                            continue
                        for key, value in row.items():
                            metric = aliases.get((key or "").strip().lower())
                            if not metric or value in (None, ""):
                                continue
                            try:
                                number = float(value)
                            except (TypeError, ValueError):
                                continue
                            if metric == "distance_km" and number > 1000:
                                number /= 1000
                            if metric == "sleep_hours" and number > 24:
                                number /= 60 if number < 1500 else 3600
                            daily[day][metric] = round(number, 2)
                        rows_seen += 1
            except Exception as exc:
                print(f"[Health] Skipped {filename}: {exc}")
    return daily, rows_seen


def _summarise(collapsed):
    """Overall and recent averages per metric."""
    if not collapsed:
        return {}, ("", "")
    days = sorted(collapsed)
    last_date = datetime.date.fromisoformat(days[-1])
    first_date = datetime.date.fromisoformat(days[0])
    cutoff = (last_date - datetime.timedelta(days=29)).isoformat()
    series = defaultdict(list)
    recent = defaultdict(list)
    for day in days:
        for name, value in collapsed[day].items():
            series[name].append(value)
            if day >= cutoff:
                recent[name].append(value)
    summary = {}
    for name, values in series.items():
        if not values:
            continue
        summary[name] = {
            "average": round(statistics.mean(values), 1),
            "recent_average": round(statistics.mean(recent[name]), 1) if recent.get(name) else None,
            "min": round(min(values), 1),
            "max": round(max(values), 1),
            "days": len(values),
            "recent_days": len(recent.get(name) or []),
            "recent_coverage": round(len(recent.get(name) or []) / 30, 2),
            "overall_coverage": round(
                len(values) / max(1, (last_date - first_date).days + 1), 2),
        }
    return summary, (days[0], days[-1])


def _monthly_notes(collapsed):
    months = defaultdict(lambda: defaultdict(list))
    for day, metrics in collapsed.items():
        for name, value in metrics.items():
            months[day[:7]][name].append(value)
    notes = []
    for month in sorted(months):
        parts = []
        month_start = datetime.date.fromisoformat(month + "-01")
        next_month = ((month_start.replace(day=28) +
                       datetime.timedelta(days=4)).replace(day=1))
        possible = min((next_month - month_start).days,
                       max(1, (datetime.date.today() - month_start).days + 1))
        for name, values in sorted(months[month].items()):
            coverage = min(1.0, len(values) / possible)
            parts.append(f"{name.replace('_', ' ')} averaged "
                         f"{round(statistics.mean(values), 1)} across "
                         f"{len(values)}/{possible} days (coverage {coverage:.0%})")
        notes.append((month, f"Health summary for {month}: " + "; ".join(parts) + "."))
    return notes


def import_health(base_dir, path, store_facts=True):
    """Parse a phone health export and keep what is worth keeping."""
    raw = str(path or "").strip().strip('"').strip("'")
    if not raw:
        return "[Error] IMPORT_HEALTH needs the path to your export file or folder."
    if not os.path.exists(raw):
        # A dead end invites the model to try another guessed path. Naming the
        # recovery makes it look instead.
        return (f"[Error] There is no file at {raw}. If Himanshu did not give you "
                "that exact path, you invented it — do not try another guess. Use "
                "FIND_FILES pattern=\"export\" to locate the real export, or ask "
                "him where he put it.")

    daily, seen, kind = None, 0, ""
    try:
        if raw.lower().endswith(".zip"):
            with zipfile.ZipFile(raw) as archive:
                inner = [n for n in archive.namelist() if n.lower().endswith("export.xml")]
                if inner:
                    kind = "Apple Health"
                    with archive.open(inner[0]) as handle:
                        daily, seen = _parse_apple(handle)
                else:
                    import tempfile
                    with tempfile.TemporaryDirectory() as tmp:
                        archive.extractall(tmp)
                        kind = "CSV export"
                        daily, seen = _parse_csv_folder(tmp)
        elif os.path.isdir(raw):
            # Apple exports are commonly unzipped as
            # export/apple_health_export/export.xml. A directory is therefore
            # not evidence of CSV format: discover the payload first.
            apple_xml = []
            for folder, _dirs, files in os.walk(raw):
                apple_xml.extend(os.path.join(folder, name) for name in files
                                 if name.lower() == "export.xml")
            if apple_xml:
                # Refuse ambiguity instead of silently combining exports from
                # different devices or dates.
                if len(apple_xml) > 1:
                    return ("[Error] More than one Apple Health export.xml was found in "
                            f"{raw}. Give me the exact file to import.")
                kind = "Apple Health"
                with open(apple_xml[0], "rb") as handle:
                    daily, seen = _parse_apple(handle)
            else:
                kind = "CSV export"
                daily, seen = _parse_csv_folder(raw)
        elif raw.lower().endswith(".xml"):
            kind = "Apple Health"
            with open(raw, "rb") as handle:
                daily, seen = _parse_apple(handle)
        elif raw.lower().endswith(".csv"):
            kind = "CSV export"
            daily, seen = _parse_csv_folder(os.path.dirname(raw) or ".")
        else:
            return f"[Error] I don't recognise {os.path.basename(raw)}. Give me the export.zip, export.xml, or the folder of CSVs."
    except zipfile.BadZipFile:
        return "[Error] That zip file is corrupt or incomplete. Try exporting again."
    except Exception as exc:
        return f"[Error] Could not read the export: {exc}"

    collapsed = _collapse(daily) if kind == "Apple Health" else {
        day: metrics for day, metrics in (daily or {}).items() if metrics
    }
    if not collapsed:
        return (f"[Error] I read {seen} records from that {kind} export but found no "
                "daily metrics I recognise. Tell me which app produced it.")

    summary, (first, last) = _summarise(collapsed)
    evidence = f"{kind} export covering {first} to {last} ({len(collapsed)} days, {seen} records)"

    # The graph is the authoritative personal-data layer. Keep one node per
    # meaningful day/metric, linked to an immutable import manifest. IDs are
    # deterministic, so importing the same export again updates nothing twice.
    graph_observations = 0
    if store_facts:
        try:
            from memory.neo4j_store import store
            source_stat = os.stat(raw)
            identity = f"{os.path.abspath(raw)}|{source_stat.st_size}|{int(source_stat.st_mtime)}"
            import_id = "health_import_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
            rows = []
            for day, metrics in sorted(collapsed.items()):
                for metric, value in sorted(metrics.items()):
                    obs_id = "health_obs_" + hashlib.sha256(
                        f"{import_id}|{day}|{metric}".encode("utf-8")).hexdigest()[:28]
                    rows.append({
                        "id": obs_id, "date": day, "metric": metric,
                        "value": float(value), "sensitivity": "sensitive",
                        "evidence_type": "device_measured", "source_id": import_id,
                        "authority": 90, "confidence": 0.95,
                    })
            graph_observations = store.upsert_health_observations({
                "id": import_id, "kind": kind, "source_path": os.path.abspath(raw),
                "first_date": first, "last_date": last, "raw_record_count": int(seen),
                "daily_metric_count": len(rows), "sensitivity": "sensitive",
                "content_hash": hashlib.sha256(identity.encode("utf-8")).hexdigest(),
            }, rows)
        except Exception as exc:
            print(f"[Health] Could not store daily graph observations: {exc}")

    stored = []
    if store_facts:
        try:
            from memory.graph_store import add_edge
            for metric, (predicate, phrasing) in _FACT_PREDICATES.items():
                data = summary.get(metric)
                if not data:
                    continue
                use_recent = (data["recent_average"] is not None and
                              data["recent_coverage"] >= 0.5)
                if not use_recent and data["overall_coverage"] < 0.5:
                    continue
                value = data["recent_average"] if use_recent else data["average"]
                add_edge(
                    "Himanshu", predicate, str(value), confidence=0.95,
                    source_id=f"health_import:{last}",
                    fact_class="explicit_user_fact", evidence_type="device_measured",
                    # Health data is only surfaced when the question is about
                    # health; the governance filter keys off this.
                    sensitivity="sensitive",
                    evidence_quote=f"{phrasing.format(value=value)} — from {evidence}",
                    evidence_ref=f"health:{last}",
                )
                stored.append(f"{metric}={value}")
        except Exception as exc:
            print(f"[Health] Could not store facts: {exc}")

    remembered = 0
    if store_facts:
        try:
            from memory.neo4j_store import store
            items = [{
                "id": f"health_{month}", "kind": "knowledge", "topic": "health",
                "source": raw, "source_id": f"health_import:{month}",
                "title": f"Health summary {month}", "content": text, "authority": 70,
            } for month, text in _monthly_notes(collapsed)]
            if items:
                store.upsert_memory_batch(items)
                remembered = len(items)
        except Exception as exc:
            print(f"[Health] Could not store monthly summaries: {exc}")

    lines = [f"[Success] Read {seen:,} records from your {kind} export "
             f"({first} to {last}, {len(collapsed)} days)."]
    for metric in sorted(summary):
        data = summary[metric]
        recent = (f", last 30 days {data['recent_average']}"
                  if data["recent_average"] is not None else "")
        lines.append(f"  {metric.replace('_', ' ')}: average {data['average']}{recent} "
                     f"(range {data['min']}-{data['max']}, {data['days']} days; "
                     f"recent coverage {data['recent_coverage']:.0%}, overall "
                     f"coverage {data['overall_coverage']:.0%})")
    if stored:
        lines.append(f"Kept as facts: {', '.join(stored)}.")
    if remembered:
        lines.append(f"Stored {remembered} monthly summaries you can ask about.")
    if graph_observations:
        lines.append(f"Stored {graph_observations:,} daily metric observations in the graph.")
    return "\n".join(lines)
