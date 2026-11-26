"""Long-running microphone health probe. Stores metrics, never audio.

Usage: .venv\Scripts\python scripts\audio_soak_test.py --hours 4
"""
import argparse
import json
import math
import queue
import time
from pathlib import Path

import numpy as np
import sounddevice as sd


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--hours", type=float, default=4.0)
    parser.add_argument("--output", default="vault/system/audio_soak.jsonl")
    args = parser.parse_args()
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    health = {"frames": 0, "overflows": 0, "last_frame": time.monotonic(),
              "sum_sq": 0.0, "samples": 0, "restarts": 0}

    def callback(data, frames, timing, status):
        health["last_frame"] = time.monotonic()
        health["frames"] += int(frames)
        health["sum_sq"] += float(np.sum(np.square(data, dtype=np.float64)))
        health["samples"] += int(data.size)
        if status:
            health["overflows"] += int(bool(getattr(status, "input_overflow", False)))

    stream = None
    deadline = time.monotonic() + max(1.0, args.hours * 3600)
    last_log = 0.0
    try:
        while time.monotonic() < deadline:
            if stream is None or not stream.active or time.monotonic() - health["last_frame"] > 10:
                if stream is not None:
                    try: stream.close()
                    except Exception: pass
                stream = sd.InputStream(samplerate=16000, channels=1,
                                        dtype="float32", callback=callback,
                                        blocksize=0, latency="high")
                stream.start()
                health["restarts"] += 1
                health["last_frame"] = time.monotonic()
            if time.monotonic() - last_log >= 60:
                rms = math.sqrt(health["sum_sq"] / max(1, health["samples"]))
                row = {"timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                       "frames": health["frames"], "rms": round(rms, 8),
                       "overflows": health["overflows"],
                       "restarts": health["restarts"],
                       "last_frame_age_seconds": round(time.monotonic() - health["last_frame"], 3)}
                with output.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(row) + "\n")
                print(row)
                health["sum_sq"] = 0.0; health["samples"] = 0
                last_log = time.monotonic()
            time.sleep(1)
    finally:
        if stream is not None:
            stream.close()


if __name__ == "__main__":
    main()
