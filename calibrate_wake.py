# -*- coding: utf-8 -*-
"""
calibrate_wake.py -- find out why the wake word is not firing.

Run it, say the name a few times, and it prints what actually happened: how loud
each utterance was, whether it was even detected as speech, what the transcriber
heard, and whether that would have woken VANTA.

    python calibrate_wake.py            # listen for 30 seconds
    python calibrate_wake.py 60         # listen for 60

"It's not listening" has three completely different causes and they need
opposite fixes, which is why guessing is a waste of time:

* **Too quiet.** The utterance never clears the energy gate, so nothing is even
  transcribed. Lower VANTA_WAKE_RMS.
* **Heard as something else.** A one-word utterance gives a small model no
  sentence to lean on, so it guesses at spelling -- and a made-up name is the
  worst case. The fix is to add whatever it actually writes to the accepted
  spellings, or to use a bigger gate model.
* **Heard correctly but rejected.** The name was not at the start, so it read as
  talking about VANTA rather than to it.

This prints which one it is. Nothing it hears is written to disk or sent
anywhere; it uses the same local gate as the wake word itself.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import identity
import wakeword


def main(seconds=30.0):
    import numpy as np
    import sounddevice as sd
    import stt

    word = identity.WAKE_WORD
    print(f"Wake word: '{word}'")
    shipped = wakeword.pretrained_name(word)
    custom = wakeword.custom_model_path()
    if custom:
        print(f"A trained model exists ({os.path.basename(custom)}), so the real "
              f"detector is that, not the speech gate this tool measures.")
    elif shipped:
        print(f"A pretrained model exists ({shipped}), so the real detector is "
              f"that, not the speech gate this tool measures.")
    else:
        print("No trained model, so the speech gate below is what is actually "
              "deciding.")

    print(f"Loading the local gate ({stt.gate_model_name()})...")
    if not stt.gate_ready():
        print("The local transcriber will not load, so the wake word cannot "
              "work at all. Push-to-talk is unaffected.")
        return 1

    gate = wakeword.SpeechGateEngine(word)
    print(f"\nEnergy threshold: {wakeword.SPEECH_RMS:.4f}  "
          f"(VANTA_WAKE_RMS)")
    print(f"Speech ends after: {wakeword.SILENCE_END:.2f}s of quiet\n")
    print(f"Say '{word}' a few times. Try it alone, and as "
          f"'{word}, mute'. Listening for {seconds:.0f}s...\n")

    state = {"speech": [], "quiet": 0.0, "peak": 0.0, "loudest_seen": 0.0,
             "utterances": 0, "would_fire": 0}
    block = 1024
    rate = wakeword.SAMPLE_RATE

    def handle(indata, _frames, _time_info, _status):
        frame = indata.copy().flatten()
        level = wakeword._rms(frame)
        state["loudest_seen"] = max(state["loudest_seen"], level)
        seconds_per_block = len(frame) / float(rate)

        if level >= wakeword.SPEECH_RMS:
            state["quiet"] = 0.0
            state["peak"] = max(state["peak"], level)
            if len(state["speech"]) * block < wakeword.MAX_SPEECH * rate:
                state["speech"].append(frame)
            return

        if not state["speech"]:
            return
        state["quiet"] += seconds_per_block
        if state["quiet"] < wakeword.SILENCE_END:
            return

        samples = np.concatenate(state["speech"]).astype("float32")
        peak = state["peak"]
        state["speech"] = []
        state["quiet"] = 0.0
        state["peak"] = 0.0
        duration = len(samples) / float(rate)
        state["utterances"] += 1

        if not (wakeword.MIN_SPEECH <= duration <= wakeword.MAX_SPEECH):
            print(f"  [{duration:.2f}s peak {peak:.4f}] too "
                  f"{'short' if duration < wakeword.MIN_SPEECH else 'long'} "
                  f"to be the wake word -- not transcribed")
            return

        heard = stt.transcribe_samples_local(samples, hotwords=word)
        fires = gate.matches(heard)
        command = gate.remainder(heard) if fires else ""
        verdict = "WOULD WAKE" if fires else "ignored"
        if fires:
            state["would_fire"] += 1
        extra = f"  then: {command!r}" if command else ""
        print(f"  [{duration:.2f}s peak {peak:.4f}] heard {heard!r:34} "
              f"-> {verdict}{extra}")

    try:
        with sd.InputStream(samplerate=rate, channels=1, dtype="float32",
                            blocksize=block, callback=handle):
            deadline = time.time() + seconds
            while time.time() < deadline:
                time.sleep(0.1)
    except KeyboardInterrupt:
        # Stopping early is the normal way to use this, not an error.
        print("\n(stopped)")

    print(f"\n{state['utterances']} utterance(s), {state['would_fire']} would "
          f"have woken it.")
    print(f"Loudest audio seen: {state['loudest_seen']:.4f} "
          f"(threshold {wakeword.SPEECH_RMS:.4f})")

    if state["utterances"] == 0:
        if state["loudest_seen"] < wakeword.SPEECH_RMS:
            print(f"\nNothing cleared the energy gate. Set "
                  f"VANTA_WAKE_RMS={state['loudest_seen'] * 0.5:.4f} and retry.")
        else:
            print("\nAudio arrived but never resolved into an utterance. Check "
                  "the microphone is the one you are speaking into.")
    elif state["would_fire"] == 0:
        print(f"\nIt is hearing you but not recognising the name. Two ways "
              f"forward:\n"
              f"  1. Add whatever it wrote above to _spelling_variants() in "
              f"wakeword.py.\n"
              f"  2. Use a bigger gate: VANTA_WAKE_STT_MODEL=base.en (slower, "
              f"more accurate).\n"
              f"  3. Or switch to a phrase with a pretrained detector, which "
              f"needs none of this:\n"
              f"     VANTA_WAKE_WORD=\"hey jarvis\"")
    else:
        print("\nWorking. If it still misses in normal use, the gap is usually "
              "the energy threshold in a quieter room.")
    return 0


if __name__ == "__main__":
    duration = 30.0
    if len(sys.argv) > 1:
        try:
            duration = float(sys.argv[1])
        except ValueError:
            pass
    raise SystemExit(main(duration))
