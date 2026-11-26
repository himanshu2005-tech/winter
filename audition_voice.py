# -*- coding: utf-8 -*-
"""
audition_voice.py -- hear the candidates instead of trusting a description.

    python audition_voice.py                 # the shortlist
    python audition_voice.py --all           # every English female voice
    python audition_voice.py --set 3         # keep candidate 3
    python audition_voice.py "say this line" # audition on your own sentence

This exists because the voice was changed four times on the strength of words
like "warm", "mature" and "confident" -- none of which I can hear. Each change
was argued from Microsoft's metadata and each one was wrong in a way only a
listener could tell: Sonia slowed and pitched down read as breathy, Libby as
young, and Aria turned out to be tagged News, Novel, which is to say a
newsreader, which is why it had no emotion in it.

The metadata is still useful, so it is printed beside each sample. But the
decision belongs to the person who can hear it, and this makes that decision
cost ninety seconds instead of a round trip per guess.

ON EMOTION, HONESTLY
--------------------
edge-tts takes rate, volume and pitch and nothing else. Azure's emotional
styles -- empathetic, gentle, calm, sad -- go through mstts:express-as in SSML,
which edge-tts does not expose, so they are not reachable from here at any
price. Within keyless TTS, "emotion" means picking a voice whose own delivery
carries it, and the Conversation/Copilot voices are the expressive ones because
they were built to talk to someone rather than read at them.

For real style control the route is Azure Speech with a key: its free tier
covers 500,000 characters a month, which is far more than this uses, and it
would allow style="empathetic" and style degree. That is a key and an account,
which is the only reason it is not already here.
"""

import asyncio
import os
import sys
import tempfile
import time

#: Known-good candidates, best first on metadata. Everything here is free and
#: keyless; the comment is Microsoft's own tagging, not an opinion.
SHORTLIST = [
    ("en-US-AvaMultilingualNeural", "Expressive, Caring, Pleasant", "Conversation, Copilot"),
    ("en-US-EmmaMultilingualNeural", "Cheerful, Clear, Conversational", "Conversation, Copilot"),
    ("en-US-JennyNeural", "Friendly, Considerate, Comfort", "General"),
    ("en-GB-SoniaNeural", "Friendly, Positive", "General"),
    ("en-IE-EmilyNeural", "Friendly, Positive", "General"),
    ("en-AU-NatashaNeural", "Friendly, Positive", "General"),
    ("en-GB-LibbyNeural", "Friendly, Positive", "General"),
    ("en-US-AriaNeural", "Positive, Confident", "News, Novel"),
]

#: Long enough to hear a cadence, and the kind of thing it will actually say.
SAMPLE = ("Good evening, sir. Your assessment reports at nine tomorrow, and "
          "everything you asked me to check is done. Rest if you can.")


def _candidates(show_all):
    if not show_all:
        return SHORTLIST
    import edge_tts

    async def go():
        return await edge_tts.list_voices()

    voices = asyncio.run(go())
    rows = []
    for voice in voices:
        name = voice["ShortName"]
        if not name.startswith(("en-GB", "en-US", "en-AU", "en-IE", "en-IN")):
            continue
        if voice.get("Gender") != "Female":
            continue
        tag = voice.get("VoiceTag", {}) or {}
        rows.append((name,
                     ", ".join(tag.get("VoicePersonalities", []) or []),
                     ", ".join(tag.get("ContentCategories", []) or [])))
    return rows


def _say(voice, text, rate, pitch):
    import edge_tts
    path = os.path.join(tempfile.gettempdir(),
                        f"audition_{abs(hash(voice))}_{int(time.time()*1000)}.mp3")

    async def go():
        speaker = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch)
        await asyncio.wait_for(speaker.save(path), timeout=30)

    asyncio.run(go())
    return path


def _play(path):
    try:
        import pygame
        pygame.mixer.init()
        pygame.mixer.music.load(path)
        pygame.mixer.music.play()
        while pygame.mixer.music.get_busy():
            time.sleep(0.1)
        pygame.mixer.music.unload()
        return True
    except Exception as exc:
        print(f"      (could not play it here: {exc})")
        return False


def main(argv):
    show_all = "--all" in argv
    argv = [item for item in argv if item != "--all"]

    if "--set" in argv:
        index = argv.index("--set")
        try:
            wanted = int(argv[index + 1])
        except (IndexError, ValueError):
            print("Usage: python audition_voice.py --set <number>")
            return 1
        rows = _candidates(show_all)
        if not 1 <= wanted <= len(rows):
            print(f"Pick a number between 1 and {len(rows)}.")
            return 1
        chosen = rows[wanted - 1][0]
        print(f"Add this line to .env:\n\n    VANTA_TTS_VOICE={chosen}\n")
        print("Nothing else changes -- rate and pitch stay where they are.")
        return 0

    spoken = next((item for item in argv[1:] if not item.startswith("-")), SAMPLE)

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    try:
        import voice as voice_module
        rate = voice_module._delivery_rate(1.0)
        pitch = voice_module.pitch_name()
        current = voice_module.voice_name()
    except Exception:
        rate, pitch, current = "-3%", "+0Hz", ""

    rows = _candidates(show_all)
    print(f"Auditioning {len(rows)} voices at rate {rate}, pitch {pitch}.")
    print(f"Saying: {spoken!r}\n")

    for number, (name, personality, categories) in enumerate(rows, 1):
        marker = "  <- in use now" if name == current else ""
        print(f"  {number}. {name}{marker}")
        print(f"      {personality}  |  suited to: {categories}")
        try:
            path = _say(name, spoken, rate, pitch)
        except Exception as exc:
            print(f"      (failed: {str(exc)[:70]})")
            continue
        _play(path)
        try:
            os.remove(path)
        except OSError:
            pass
        time.sleep(0.3)

    print(f"\nKeep one with:  python audition_voice.py --set <number>")
    print("Slower and calmer:  VANTA_TTS_RATE_TRIM=-8")
    print("Note: pitch below 0 plus a slowed rate is what read as breathy "
          "before, so change one at a time.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
