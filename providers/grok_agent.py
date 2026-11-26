# -*- coding: utf-8 -*-
"""
agent.py — VANTA core logic
Handles: audio capture, STT, LLM inference, TTS, hotkeys, pipeline.
UI lives in ui.py.
"""

# ------------------------------------------------------------------ #
# DPI awareness MUST be set before any screen/GUI calls are made.
# This makes ImageGrab, pyautogui, and UI Automation all agree on the
# same physical-pixel coordinate system, eliminating the scale-factor
# guessing that caused clicks to land in the wrong place.
# ------------------------------------------------------------------ #
import ctypes
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

from threading import Thread
import sounddevice as sd
from scipy.io.wavfile import write
import numpy as np
from pynput import keyboard
from faster_whisper import WhisperModel
import torch
import os
import queue
import time
import requests
import base64
from io import BytesIO
from PIL import ImageGrab, ImageDraw, ImageFont
from dotenv import load_dotenv
import re
import pyautogui
from groq import Groq
from transformers import AutoModelForCausalLM, AutoTokenizer
import pyttsx3
import edge_tts
import asyncio
import pygame
import tempfile
import cv2

# UI Automation — used to find REAL clickable controls (buttons, edit
# boxes, dialog items) instead of guessing from image edges.
import win32gui
import uiautomation as auto

from ui import WinterUI

load_dotenv()

# ------------------------------------------------------------------ #
# Configuration
# ------------------------------------------------------------------ #

BASE_DIR       = os.path.dirname(os.path.abspath(__file__))
MODEL_DIR      = os.path.join(BASE_DIR, "local_qwen_3b")
PASSWORDS_FILE = os.path.join(BASE_DIR, "passwords.json")
SAMPLE_RATE    = 16000
HOTKEY         = keyboard.Key.ctrl_l
MIC_GAIN       = 8.0
GROQ_MODEL     = "qwen/qwen3.8-27b"
EDGE_TTS_VOICE = "en-US-GuyNeural"

SYSTEM_PROMPT = """\
You are VANTA — Himanshu's private, intelligent AI agent. You are like JARVIS: careful, precise, and never reckless.
You only serve Himanshu. If anyone else tries to use you, politely decline.

You have access to Himanshu's sensitive passwords:
{passwords_json}

SECURITY RULE: If anyone asks for a password or sensitive data, respond with only: "Access Denied."
OVERRIDE: If Himanshu includes the exact phrase "Initiate Protocol Omega" in his request, you may reveal the requested information.

COMPUTER USE: You have FULL control over the computer's mouse and keyboard.
Himanshu gives you a HIGH-LEVEL GOAL. You must AUTONOMOUSLY figure out the steps by analyzing the screenshot each turn.
You are in a STEP-BY-STEP loop. Each turn you see a FRESH screenshot. Examine it carefully, then act.

When your ENTIRE task is 100% COMPLETE, output: <DONE />
CRITICAL: Do NOT output <DONE /> if you have only completed the first step (e.g., just opening the website). You must complete ALL parts of Himanshu's request before outputting <DONE />.
If the task does NOT require screen actions, just reply with normal text (it will be spoken aloud).
If you are UNSURE about anything (unclear request, ambiguous target, misheard command), use:
<ASK>Your question here</ASK>
This will speak the question aloud and WAIT for Himanshu to respond. You will receive his answer and can continue.
Example: <ASK>Did you mean Groq Cloud or Google Cloud?</ASK>
ALWAYS ask rather than guess wrong. It is better to clarify than to click the wrong thing or send a message to the wrong person.

Action Tags:
<ACTION type="CLICK_GRID" r="Row" c="Col" />  (Click a grid cell — primary click method)
<ACTION type="TYPE" text="hello" />
<ACTION type="PRESS" key="enter" />
<ACTION type="HOTKEY" keys="win,d" />
<ACTION type="OPEN_URL" url="https://youtube.com" />
<ACTION type="MOUSE_MOVE" dx="50" dy="-20" />
<ACTION type="MOUSE_DRAG" dx="100" dy="0" />
<ACTION type="SCROLL" amount="-500" />
<ACTION type="WAIT" time="1.0" />

SAVE FILES to d:\\\\winter\\\\data\\\\ only:
<ACTION type="SAVE_FILE" filename="name.txt">content</ACTION>

═══════════════════════════════════════════════════
GOLDEN RULE: LOOK BEFORE YOU ACT
═══════════════════════════════════════════════════
Every turn, you get a fresh screenshot. BEFORE outputting any action:
1. Describe IN YOUR HEAD what you see on screen (which app is open, what page is loaded, where relevant elements are).
2. Decide ONE careful next step.
3. Output that step.
Do NOT rush through multiple steps blindly. Be deliberate like JARVIS.

═══════════════════════════════════════════════════
SAFETY RULES (NEVER VIOLATE THESE)
═══════════════════════════════════════════════════
S1. NEVER TYPE text unless you have VERIFIED in the current screenshot that the correct input field is active/focused.
    If you just opened a URL or app, WAIT for the next screenshot first before typing anything.
S2. NEVER send a message (press Enter in a chat) without first VERIFYING in the screenshot that you are in the CORRECT chat/conversation.
    Read the chat header name carefully before pressing Enter.
S3. NEVER click random UI elements hoping they are what you want. Read the text/labels on screen first.
S4. After OPEN_URL or launching an app, your ONLY action should be WAIT. Do NOT type or click in the same turn.

═══════════════════════════════════════════════════
CLICKING WITH THE COORDINATE GRID
═══════════════════════════════════════════════════
The screenshot has a COORDINATE GRID: faint blue lines dividing the screen into cells.
Column numbers (0 to 47) along the TOP edge. Row numbers (0 to 26) along the LEFT edge.
MAXIMUM row = 26. MAXIMUM col = 47. NEVER output row > 26 or col > 47!

To click: trace the faint blue line from your target UP to find the column number, and LEFT to find the row number.
<ACTION type="CLICK_GRID" r="Row" c="Col" />

═══════════════════════════════════════════════════
SPECIFIC WORKFLOWS
═══════════════════════════════════════════════════

OPENING APPS (Chrome, Notepad, etc.):
  Use this exact combo in a SINGLE turn:
  <ACTION type="PRESS" key="win" />
  <ACTION type="WAIT" time="0.7" />
  <ACTION type="TYPE" text="appname" />
  <ACTION type="WAIT" time="1.3" />
  <ACTION type="PRESS" key="enter" />
  NEVER click taskbar icons. ALWAYS use the win key.
  NEVER repeat this combo for the same app. If it didn't work, WAIT and look again.

OPENING WEBSITES:
  <ACTION type="OPEN_URL" url="https://youtube.com" />
  Do not output any more actions in this turn. Wait for the next screenshot to see the page before continuing your work.

SEARCHING ON ANY WEBSITE — USE THE URL DIRECTLY:
  NEVER try to click or focus a search bar. Instead, construct the search URL and use OPEN_URL:
  - YouTube:  <ACTION type="OPEN_URL" url="https://www.youtube.com/results?search_query=your+search+here" />
  - Google:   <ACTION type="OPEN_URL" url="https://www.google.com/search?q=your+search+here" />
  - For other sites: use Ctrl+L to focus the address bar, type the URL, and press Enter.
  Replace spaces with + in the search query. This is 100% reliable and works everywhere.

YOUTUBE — PLAYING A SONG:
  When Himanshu says "play a song" or "play some music", you MUST search first using the URL method above.
  Do NOT click random videos on the homepage. ALWAYS search for what he wants.
  If he doesn't specify a song, search for "popular songs playlist" or "music mix".
  After the search results load, click the FIRST relevant video using CLICK_GRID.

WHATSAPP — OPENING A CHAT:
  NEVER click chat rows directly. ALWAYS use the search box:
  Step 1: Use CLICK_GRID to click the search box at the top of the chat list.
  Step 2: <ACTION type="TYPE" text="contact name" />
  Step 3: <ACTION type="WAIT" time="1.0" />
  Step 4: <ACTION type="PRESS" key="enter" />
  BEFORE sending any message, VERIFY the chat header shows the correct contact name!

WHATSAPP — SENDING MESSAGES:
  CRITICAL: Before pressing Enter to send, READ the chat header in the screenshot.
  If the header shows a GROUP name or the WRONG contact, STOP. Do not send.
  Only send if the header matches what Himanshu asked for.

GROQ CLOUD (API KEYS):
  If Himanshu says "Grok cloud" or asks to create a Groq API key:
  1. DO NOT go to console.x.ai! He means Groq Cloud.
  2. <ACTION type="OPEN_URL" url="https://console.groq.com/keys" />
  3. Wait for the page to load.
  4. If asked to sign in, use CLICK_GRID to click "Sign in with Google".
  5. Once on the Keys page, use CLICK_GRID to click the "Create API Key" button.
  6. <ACTION type="TYPE" text="key name" />
  7. <ACTION type="PRESS" key="enter" />

ANY UNFAMILIAR WEBSITE — GENERAL APPROACH:
  You can navigate ANY website. Follow this pattern:
  1. Use OPEN_URL to go to the site.
  2. WAIT for the page to load. Look at the fresh screenshot carefully.
  3. Read ALL the text and buttons visible on screen. Identify what you need to click.
  4. Use CLICK_GRID to click buttons, links, or form fields ONE AT A TIME.
  5. After each click, WAIT and look at the new screenshot before proceeding.

SIGN-IN WITH GOOGLE (OAuth):
  When you see "Sign in with Google" or "Continue with Google", click it with CLICK_GRID.
  If a Google account picker popup appears, click Himanshu's account.
  If asked for a password, check the passwords JSON for google_password.
  Take it step by step — OAuth flows have multiple pages, so look at each screenshot carefully.

TYPING INTO FORMS OR ADDRESS BAR:
  To focus the browser address bar on ANY website: <ACTION type="HOTKEY" keys="ctrl,l" />
  Then type the URL and press Enter. This works universally in all browsers.
  For form fields on a page: click the field with CLICK_GRID first, verify it's focused, then TYPE.

═══════════════════════════════════════════════════
COMBO RULES
═══════════════════════════════════════════════════
You may combine ONLY these safe sequences in a single turn:
- Win + Wait + Type + Wait + Enter (app launch only)
- TYPE + PRESS enter (typing then submitting, ONLY if the input is already verified as focused)
NEVER combine OPEN_URL + CLICK_GRID or OPEN_URL + TYPE in the same turn.
NEVER combine CLICK_GRID + TYPE unless you have already verified the click target will focus an input.

═══════════════════════════════════════════════════
GENERAL RULES
═══════════════════════════════════════════════════
1. Prefer keyboard shortcuts over clicking (Tab, arrows, Enter, /).
2. Screen resolution is {screen_w}x{screen_h}.
3. VERIFICATION: Each turn, check if your previous action succeeded. If it failed, try differently.
4. Keep <think> blocks under 30 words.
5. Output ONLY the action tags. No extra text unless the task is purely conversational.

TONE: Speak to Himanshu with respect, like a loyal and intelligent personal assistant. Be concise and natural \
— your response will be spoken aloud, so use plain spoken English only. No markdown, no bullet points, \
no asterisks."""



# ------------------------------------------------------------------ #
# State
# ------------------------------------------------------------------ #

STATE_IDLE      = "idle"
STATE_LOADING   = "loading"
STATE_LISTENING = "listening"
STATE_THINKING  = "thinking"
STATE_SPEAKING  = "speaking"

current_state = STATE_LOADING
is_recording  = False
audio_data    = []
ui_queue      = queue.Queue()
input_queue   = queue.Queue()
stt_model     = None
tokenizer     = None
llm_model     = None
tts_engine    = None
cancel_flag   = False
element_map   = {} # Maps ID -> (x, y)  (physical, DPI-aware pixel coords)
clarification_queue = queue.Queue()  # for ASK/clarification answers
waiting_for_clarification = False

groq_api_key = os.getenv("GROQ_API_KEY", "")
groq_client  = Groq(api_key=groq_api_key) if groq_api_key else None

pygame.mixer.init()

# ------------------------------------------------------------------ #
# Helpers
# ------------------------------------------------------------------ #

def set_state(s, text=""):
    global current_state
    current_state = s
    ui_queue.put((s, text))

_internet_checked = None

def check_internet():
    global _internet_checked
    if _internet_checked is not None:
        return _internet_checked
    if not groq_api_key or groq_api_key == "your_groq_api_key_here":
        _internet_checked = False
        return False
    try:
        requests.get("https://1.1.1.1", timeout=5)
        _internet_checked = True
        return True
    except Exception:
        _internet_checked = False
        return False

def get_system_prompt():
    passwords = "{}"
    if os.path.exists(PASSWORDS_FILE):
        with open(PASSWORDS_FILE, "r") as f:
            passwords = f.read()

    w, h = ImageGrab.grab().size
    prompt = SYSTEM_PROMPT.replace("{passwords_json}", passwords)
    prompt = prompt.replace("{screen_w}", str(w)).replace("{screen_h}", str(h))
    return prompt

from vision import take_screenshot_b64, element_map
from actions import execute_actions

# ------------------------------------------------------------------ #
# TTS
# ------------------------------------------------------------------ #

async def _edge_speak_async(text):
    tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
    tmp.close()
    await edge_tts.Communicate(text, EDGE_TTS_VOICE).save(tmp.name)
    return tmp.name

def speak(text):
    global tts_engine, cancel_flag
    if cancel_flag:
        set_state(STATE_IDLE)
        return
    set_state(STATE_SPEAKING, text)
    try:
        mp3_path = asyncio.run(_edge_speak_async(text))
        if not cancel_flag:
            pygame.mixer.music.load(mp3_path)
            pygame.mixer.music.play()
            while pygame.mixer.music.get_busy():
                if cancel_flag:
                    pygame.mixer.music.stop()
                    break
                time.sleep(0.05)
        try:
            os.remove(mp3_path)
        except Exception:
            pass
    except Exception as e:
        print(f"[edge-tts failed, fallback to pyttsx3] {e}")
        try:
            if tts_engine is None:
                tts_engine = pyttsx3.init()
                tts_engine.setProperty('rate', 175)
                tts_engine.setProperty('volume', 1.0)
            if not cancel_flag:
                tts_engine.say(text)
                tts_engine.runAndWait()
        except Exception as e2:
            print(f"[TTS fallback error] {e2}")
    finally:
        cancel_flag = False
        set_state(STATE_IDLE)

def cancel_interaction():
    global cancel_flag
    cancel_flag = True
    if tts_engine is not None:
        try:
            tts_engine.stop()
        except Exception:
            pass
    try:
        pygame.mixer.music.stop()
    except Exception:
        pass
    set_state(STATE_IDLE)

# ------------------------------------------------------------------ #
# Model loading
# ------------------------------------------------------------------ #

def load_stt():
    set_state(STATE_LOADING, "Loading speech model...")
    try:
        stt = WhisperModel("small.en", device="cpu", compute_type="int8")
        set_state(STATE_IDLE)
        return stt
    except Exception as e:
        print(f"[STT load error] {e}")
        set_state(STATE_IDLE)
        return None

def get_local_llm():
    global tokenizer, llm_model
    if llm_model is None:
        set_state(STATE_THINKING, "Loading local model...")
        tokenizer = AutoTokenizer.from_pretrained(MODEL_DIR)
        llm_model = AutoModelForCausalLM.from_pretrained(
            MODEL_DIR, torch_dtype=torch.float16, device_map="auto"
        )
    return tokenizer, llm_model

# ------------------------------------------------------------------ #
# Audio
# ------------------------------------------------------------------ #

_BUILTIN_MIC_KEYWORDS = ["array", "realtek", "intel", "built-in", "internal", "hd audio"]
_HEADSET_KEYWORDS     = ["headset", "airpods", "bluetooth", "bt "]

def find_best_mic():
    """
    Returns the best available input device index.
    Priority:
      1. Built-in laptop microphone (Intel/Realtek array preferred)
      2. Any connected headset mic that's actually available
      3. System default (None)
    """
    devices = sd.query_devices()
    builtin_candidates = []
    headset_candidates = []

    for i, d in enumerate(devices):
        if d['max_input_channels'] < 1:
            continue
        name_low = d['name'].lower()
        if 'wdm-ks' in name_low or 'waveout' in name_low:
            continue
        try:
            with sd.InputStream(device=i, channels=1,
                                samplerate=SAMPLE_RATE, dtype='float32'):
                pass
        except Exception:
            continue

        if any(kw in name_low for kw in _BUILTIN_MIC_KEYWORDS):
            builtin_candidates.append(i)
        elif any(kw in name_low for kw in _HEADSET_KEYWORDS):
            headset_candidates.append(i)

    if builtin_candidates:
        chosen = builtin_candidates[0]
    elif headset_candidates:
        chosen = headset_candidates[0]
    else:
        chosen = None

    name = sd.query_devices(chosen)['name'] if chosen is not None else "system default"
    print(f"[Audio] Using mic: [{chosen}] {name}")
    return chosen

def audio_callback(indata, frames, time_info, status):
    if is_recording:
        audio_data.append(indata.copy())

def make_stream():
    device = find_best_mic()
    return sd.InputStream(
        samplerate=SAMPLE_RATE,
        channels=1,
        dtype='float32',
        device=device,
        blocksize=1024,
        callback=audio_callback,
    )

stream = make_stream()

# ------------------------------------------------------------------ #
# Hotkeys
# ------------------------------------------------------------------ #

def on_press(key):
    global is_recording, audio_data, cancel_flag, stream
    if key == keyboard.Key.f12:
        os._exit(0)
    if key == keyboard.Key.esc:
        if current_state in (STATE_THINKING, STATE_SPEAKING):
            Thread(target=cancel_interaction, daemon=True).start()
    
    # Check for Ctrl+T to show text input box
    if hasattr(key, 'char') and key.char == '\x14': # \x14 is Ctrl+T
        ui_queue.put(("prompt_input", ""))
    elif key == keyboard.Key.esc and is_recording:
        is_recording = False
        try:
            stream.stop()
        except Exception:
            pass
        set_state(STATE_IDLE)
    can_record = current_state == STATE_IDLE or waiting_for_clarification
    if key == HOTKEY and not is_recording and stt_model is not None and can_record:
        is_recording = True
        audio_data   = []
        cancel_flag  = False
        stream = make_stream()
        stream.start()
        set_state(STATE_LISTENING, "Listening for your answer..." if waiting_for_clarification else "")

def on_release(key):
    global is_recording
    if key == HOTKEY and is_recording:
        is_recording = False
        try:
            stream.stop()
        except Exception:
            pass
        set_state(STATE_THINKING, "")
        Thread(target=process_audio, daemon=True).start()

def start_keyboard_listener():
    keyboard.Listener(on_press=on_press, on_release=on_release).start()

# ------------------------------------------------------------------ #
# Pipeline
# ------------------------------------------------------------------ #

def call_llm(messages):
    """Call the LLM (cloud or local) and return the response text."""
    if check_internet():
        completion = groq_client.chat.completions.create(
            messages=messages,
            model=GROQ_MODEL,
            temperature=0.5,
            max_tokens=400,
        )
        return completion.choices[0].message.content
    else:
        tok, llm = get_local_llm()
        try:
            prompt = tok.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        except Exception:
            prompt = f"{get_system_prompt()}\nHimanshu: {messages[-1]['content']}\nWINTER:"
        inputs  = tok(prompt, return_tensors="pt").to(llm.device)
        outputs = llm.generate(**inputs, max_new_tokens=400, temperature=0.5, do_sample=True)
        return tok.decode(outputs[0][inputs.input_ids.shape[-1]:], skip_special_tokens=True).strip()

MAX_AGENT_STEPS = 35

# ------------------------------------------------------------------ #
# Duplicate app-launch guard
#
# The model sometimes re-runs the exact same "Win -> type appname ->
# Enter" combo a second time in the same task (e.g. because Windows
# Search hadn't finished filtering yet, so it *looked* like nothing
# happened). That produces the "opens WhatsApp Web, then opens it
# again" behaviour. This strips out a second identical attempt and
# tells the model to just wait and look again instead of re-searching.
# ------------------------------------------------------------------ #

_LAUNCH_SEQ_RE = re.compile(
    r'<ACTION\s+type="PRESS"\s+key="win"\s*/>.*?'
    r'<ACTION\s+type="TYPE"\s+text="([^"]+)"\s*/>.*?'
    r'<ACTION\s+type="PRESS"\s+key="enter"\s*/>',
    re.IGNORECASE | re.DOTALL
)

def _dedupe_launch_actions(response, launched_terms):
    """
    Removes a Win+Search+Enter launch combo if the same app name was
    already attempted earlier in this task. Returns (new_response, was_duplicate).
    """
    was_duplicate = False

    def _check(match):
        nonlocal was_duplicate
        term = match.group(1).strip().lower()
        if term in launched_terms:
            was_duplicate = True
            return ""  # drop this whole launch combo
        launched_terms.add(term)
        return match.group(0)

    new_response = _LAUNCH_SEQ_RE.sub(_check, response)
    return new_response, was_duplicate

def process_audio():
    global waiting_for_clarification
    if not audio_data or cancel_flag:
        set_state(STATE_IDLE)
        return

    audio_np = np.concatenate(audio_data, axis=0)
    audio_np = np.clip(audio_np * MIC_GAIN, -1.0, 1.0)
    write("temp.wav", SAMPLE_RATE, (audio_np * 32767).astype(np.int16))

    try:
        segments, _ = stt_model.transcribe(
            "temp.wav", beam_size=5, language="en",
            vad_filter=True, vad_parameters={"min_silence_duration_ms": 300}
        )
        text = "".join(s.text for s in segments).strip()
        print(f"[STT] '{text}'")
    except Exception as e:
        print(f"[STT error] {e}")
        set_state(STATE_IDLE)
        return

    if not text:
        set_state(STATE_IDLE)
        return

    # If agent is waiting for a clarification answer, route it there
    if waiting_for_clarification:
        print(f"[Clarification] User answered: {text}")
        clarification_queue.put(text)
        return

    Thread(target=run_agent_loop, args=(text,), daemon=True).start()

def run_agent_loop(text):
    set_state(STATE_THINKING, text)
    launched_terms = set()  # tracks app names already launched this task
    try:
        b64 = take_screenshot_b64(BASE_DIR)
        ui_queue.put(("show_vision_preview", ""))
        system_prompt = get_system_prompt() + "\nVerify the result of your actions. If an action fails, try a different approach."
        conversation = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": [
                {"type": "text", "text": text},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}
            ]}
        ]

        for step in range(MAX_AGENT_STEPS):
            if cancel_flag:
                set_state(STATE_IDLE)
                return

            response = call_llm(conversation)
            response = re.sub(r'<think>.*?</think>', '', response, flags=re.DOTALL).strip()
            print(f"[Agent Step {step+1}] {response}")

            if '<DONE' in response:
                print("[Agent] Task complete.")
                set_state(STATE_IDLE)
                return

            response, is_duplicate_launch = _dedupe_launch_actions(response, launched_terms)

            if is_duplicate_launch:
                print("[Agent] Suppressed a duplicate app-launch sequence — waiting and re-checking instead.")
                time.sleep(1.5)
                b64 = take_screenshot_b64(BASE_DIR)
                ui_queue.put(("show_vision_preview", ""))
                for msg in conversation:
                    if msg["role"] == "user" and isinstance(msg["content"], list):
                        msg["content"] = [item for item in msg["content"] if item.get("type") == "text"]
                conversation.append({"role": "assistant", "content": response})
                conversation.append({"role": "user", "content": [
                    {"type": "text", "text": (
                        "You already ran that exact app-launch search earlier in this task — "
                        "do not repeat it. Look at the current screenshot: if the app is open, "
                        "interact with it or output <DONE />. If it still isn't open, try clicking "
                        "the top Start menu search result (or a green/blue numbered ID) instead of "
                        "retyping the search."
                    )},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}
                ]})
                continue

            # Check for <ASK> tag — agent wants to ask a clarifying question
            ask_match = re.search(r'<ASK>(.*?)</ASK>', response, re.DOTALL)
            if ask_match:
                question = ask_match.group(1).strip()
                print(f"[Agent] Asking: {question}")
                
                # Speak the question and wait for answer
                waiting_for_clarification = True
                speak(question)  # This blocks until TTS is done
                set_state(STATE_THINKING, "Waiting for your answer...")
                
                # Wait for user to respond via voice or text
                try:
                    answer = clarification_queue.get(timeout=30)  # 30s timeout
                except queue.Empty:
                    answer = "no response"
                    print("[Agent] Clarification timeout — no answer received.")
                waiting_for_clarification = False
                
                print(f"[Agent] Got answer: {answer}")
                set_state(STATE_THINKING, answer)
                
                # Feed the answer back into the conversation
                b64 = take_screenshot_b64(BASE_DIR)
                for msg in conversation:
                    if msg["role"] == "user" and isinstance(msg["content"], list):
                        msg["content"] = [item for item in msg["content"] if item.get("type") == "text"]
                conversation.append({"role": "assistant", "content": response})
                conversation.append({"role": "user", "content": [
                    {"type": "text", "text": f"Himanshu answered: {answer}"},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}
                ]})
                continue

            has_actions = bool(re.search(r'<ACTION\s+', response))

            if has_actions:
                spoken_text = execute_actions(response, element_map, ui_queue, BASE_DIR)

                if spoken_text.strip():
                    Thread(target=speak, args=(spoken_text,), daemon=True).start()
                    return

                time.sleep(1.0)
                b64 = take_screenshot_b64(BASE_DIR)

                for msg in conversation:
                    if msg["role"] == "user" and isinstance(msg["content"], list):
                        msg["content"] = [item for item in msg["content"] if item.get("type") == "text"]

                conversation.append({"role": "assistant", "content": response})
                conversation.append({"role": "user", "content": [
                    {"type": "text", "text": "Action executed. Here is the updated screen. Continue with the next step, or output <DONE /> if the task is complete."},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}
                ]})
            else:
                Thread(target=speak, args=(response,), daemon=True).start()
                return

        print(f"[Agent] Reached max {MAX_AGENT_STEPS} steps, stopping.")
        set_state(STATE_IDLE)

    except Exception as e:
        print(f"[Pipeline error] {e}")
        set_state(STATE_IDLE)

def poll_input_queue():
    global waiting_for_clarification
    while True:
        text = input_queue.get()
        if text.strip():
            if waiting_for_clarification:
                print(f"[Clarification] User answered via text: {text}")
                clarification_queue.put(text)
            else:
                Thread(target=run_agent_loop, args=(text,), daemon=True).start()

# ------------------------------------------------------------------ #
# Entry point
# ------------------------------------------------------------------ #

def _init():
    global stt_model
    stt_model = load_stt()
    if stt_model:
        start_keyboard_listener()
        Thread(target=poll_input_queue, daemon=True).start()

if __name__ == "__main__":
    ui = WinterUI(ui_queue, input_queue)
    Thread(target=_init, daemon=True).start()
    ui.run()