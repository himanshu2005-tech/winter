import sys
import os
import cv2
import time
import math
import threading
import tkinter as tk
import customtkinter as ctk
from PIL import Image, ImageDraw, ImageGrab, ImageFilter, ImageEnhance, ImageTk
import contextlib

import window_identity

def make_circle_mask(size):
    mask = Image.new('L', size, 0)
    draw = ImageDraw.Draw(mask)
    draw.ellipse((0, 0) + size, fill=255)
    return mask

def process_circular_frame(frame, size=240):
    h, w = frame.shape[:2]
    min_dim = min(h, w)
    start_y = (h - min_dim) // 2
    start_x = (w - min_dim) // 2
    cropped = frame[start_y:start_y+min_dim, start_x:start_x+min_dim]
    
    cropped = cv2.resize(cropped, (size, size))
    frame_rgb = cv2.cvtColor(cropped, cv2.COLOR_BGR2RGB)
    img = Image.fromarray(frame_rgb).convert("RGBA")
    
    mask = make_circle_mask((size, size))
    img.putalpha(mask)
    return img

def draw_liquid_wave(base_img, fill_percent, time_t, size=240):
    out = base_img.copy()
    draw = ImageDraw.Draw(out, "RGBA")
    
    base_y = size * (1.0 - fill_percent)
    
    points = [(0, size)]
    xs = []
    ys = []
    for x in range(0, size + 1, 10):
        # Amplitude tapers off as it gets full
        amp = 12 * math.sin(fill_percent * math.pi) 
        y = base_y + amp * math.sin(x * 0.04 + time_t)
        points.append((x, y))
        xs.append(x)
        ys.append(y)
    points.append((size, size))
    
    # Glassmorphic liquid: more translucent, lighter neon green
    draw.polygon(points, fill=(48, 209, 88, 140)) 
    
    # Add subtle glass specular highlight line on top of the wave
    if len(xs) > 1:
        specular_pts = list(zip(xs, ys))
        draw.line(specular_pts, fill=(255, 255, 255, 180), width=2)
    
    if fill_percent > 0.4:
        alpha = int(min(1.0, (fill_percent - 0.4) / 0.4) * 255)
        chk_draw = Image.new("RGBA", (size, size), (0,0,0,0))
        c_draw = ImageDraw.Draw(chk_draw)
        # Checkmark
        c_draw.line([(80, 125), (110, 155), (165, 90)], fill=(255, 255, 255, alpha), width=12, joint="curve")
        out = Image.alpha_composite(out, chk_draw)
        
    mask = make_circle_mask((size, size))
    out.putalpha(mask)
    return out

def show_lockscreen(input_queue=None):
    import sys
    import crypto_env
    
    if getattr(sys, 'frozen', False):
        base_dir = os.path.dirname(sys.executable)
    else:
        base_dir = os.path.dirname(os.path.abspath(__file__))

    config_path = os.path.join(base_dir, "config.bin")
    crypto_env.load_encrypted_env(config_path)
    AUTH_PASSWORD = os.getenv("AUTH_PASSWORD", "Madhu")
    ref_img = os.path.join(base_dir, "vault", "owner_face.jpg")
    
    window_identity.configure_process()
    root = ctk.CTk()
    root.withdraw()
    window_identity.apply_to(root, base_dir=base_dir)
    root.attributes('-fullscreen', True)
    root.attributes('-topmost', True)
    root.overrideredirect(True)
    window_identity.hide_from_taskbar(root)
    root.title("Face ID")
    root.attributes('-alpha', 0.0)
    
    sw = root.winfo_screenwidth()
    sh = root.winfo_screenheight()
    
    # Use a raw tkinter Canvas for TRUE transparency compositing
    # CTk labels create ugly solid rectangular backgrounds for transparent images
    canvas = tk.Canvas(root, width=sw, height=sh, highlightthickness=0, bg="black")
    canvas.pack(fill="both", expand=True)
    root.deiconify()
    
    # 1. Grab screen and blur for perfect Glassmorphism
    try:
        screen = ImageGrab.grab(bbox=(0, 0, sw, sh))
        screen = screen.filter(ImageFilter.GaussianBlur(30))
        enhancer = ImageEnhance.Brightness(screen)
        screen = enhancer.enhance(0.85) # Brighter so the glass is actually visible!
        bg_photo = ImageTk.PhotoImage(screen)
        canvas.create_image(0, 0, image=bg_photo, anchor="nw")
    except Exception:
        pass # Fallback to black if screen grab fails

    # Face ID assist light. These screen-edge panels illuminate the user in a
    # dark room while leaving the centre of the lock screen readable.
    torch_width = max(72, min(140, sw // 12))
    torch_height = max(72, min(140, sh // 10))
    torch_edges = [
        canvas.create_rectangle(0, 0, sw, torch_height, fill="#FFFFFF", outline="", state="hidden"),
        canvas.create_rectangle(0, sh - torch_height, sw, sh, fill="#FFFFFF", outline="", state="hidden"),
        canvas.create_rectangle(0, torch_height, torch_width, sh - torch_height, fill="#FFFFFF", outline="", state="hidden"),
        canvas.create_rectangle(sw - torch_width, torch_height, sw, sh - torch_height, fill="#FFFFFF", outline="", state="hidden"),
    ]
    torch_modes = [("Off", None), ("White", "#FFFFFF"), ("Warm", "#FFF4D6")]
    torch_mode_index = 0
        
    cx = sw // 2
    cy = sh // 2
    
    # 2. Text and UI Elements on Canvas (Spaced out and sized for minimal elegance)
    title_id = canvas.create_text(cx, cy - 165, text="VANTA", font=("Segoe UI", 16, "bold"), fill="white")
    
    # Arc bounding box
    arc_r = 130
    
    # Create glassmorphic frosted ring background
    ring_size = (arc_r * 2) + 20
    ring_img = Image.new("RGBA", (ring_size, ring_size), (0, 0, 0, 0))
    ring_draw = ImageDraw.Draw(ring_img)
    ring_draw.ellipse((10, 10, ring_size-10, ring_size-10), outline=(255, 255, 255, 40), width=6)
    
    # Outer glow / rim light for extra premium feel
    ring_draw.ellipse((9, 9, ring_size-9, ring_size-9), outline=(255, 255, 255, 20), width=1)
    
    canvas.ring_photo = ImageTk.PhotoImage(ring_img)
    ring_bg_id = canvas.create_image(cx, cy, image=canvas.ring_photo, anchor="center")
    
    # Dynamic foreground arc
    arc_id = canvas.create_arc(cx - arc_r, cy - arc_r, cx + arc_r, cy + arc_r, 
                               start=90, extent=0, outline="#0A84FF", width=6, style="arc")
                               
    # Video Image placeholder
    vid_id = canvas.create_image(cx, cy, anchor="center")
    
    # Passcode Entry Pill (True Glassmorphism)
    w, h = 220, 44
    r = h // 2
    pill_y = cy + 180
    
    # Create a semi-transparent pill image for the frosted glass background
    pill_img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    pill_draw = ImageDraw.Draw(pill_img)
    pill_draw.rounded_rectangle((0, 0, w, h), radius=r, fill=(255, 255, 255, 25), outline=(255, 255, 255, 60), width=1)
    canvas.pill_photo = ImageTk.PhotoImage(pill_img)
    pill_bg_id = canvas.create_image(cx, pill_y, image=canvas.pill_photo, anchor="center")
    
    passcode_str = ""
    passcode_text_id = canvas.create_text(cx, pill_y, text="Passcode", font=("Segoe UI", 14), fill="#98989D")
    
    status_id = canvas.create_text(cx, cy + 240, text="Scanning...", font=("Segoe UI", 12), fill="#8E8E93")

    # Canvas-native button avoids mixing widget stacking rules with the glass UI.
    torch_button_y = cy + 285
    torch_button_bg = canvas.create_rectangle(cx - 58, torch_button_y - 16, cx + 58, torch_button_y + 16,
                                              fill="#1C1C1E", outline="#636366", width=1)
    torch_button_text = canvas.create_text(cx, torch_button_y, text="Torch: Off",
                                           font=("Segoe UI", 11, "bold"), fill="#F5F5F7")

    def toggle_torch(event=None):
        nonlocal torch_mode_index
        torch_mode_index = (torch_mode_index + 1) % len(torch_modes)
        mode_name, color = torch_modes[torch_mode_index]
        visible = "normal" if color else "hidden"
        for edge in torch_edges:
            if color:
                canvas.itemconfig(edge, fill=color)
            canvas.itemconfig(edge, state=visible)
        canvas.itemconfig(torch_button_text, text=f"Torch: {mode_name}")
        canvas.itemconfig(torch_button_bg, fill="#3A3A3C" if color else "#1C1C1E")
        if not unlocked:
            canvas.itemconfig(status_id,
                              text=f"Torch {mode_name.lower()} — improving Face ID lighting" if color else "Scanning...",
                              fill="#FFFFFF" if color else "#8E8E93")

    canvas.tag_bind(torch_button_bg, "<Button-1>", toggle_torch)
    canvas.tag_bind(torch_button_text, "<Button-1>", toggle_torch)
    
    cap = cv2.VideoCapture(0)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
    unlocked = False
    stop_event = threading.Event()
    
    def handle_key(event):
        
        nonlocal passcode_str
        if unlocked: return
        
        if event.keysym == 'BackSpace':
            passcode_str = passcode_str[:-1]
        elif event.keysym == 'Return':
            check_passcode()
        elif len(event.char) == 1 and event.char.isprintable():
            passcode_str += event.char
            
        if len(passcode_str) > 0:
            canvas.itemconfig(passcode_text_id, text="●" * len(passcode_str), fill="white", font=("Segoe UI", 12))
        else:
            canvas.itemconfig(passcode_text_id, text="Passcode", fill="#98989D", font=("Segoe UI", 14))
            
    root.bind("<Key>", handle_key)
    
    # ---------------- ANIMATIONS ---------------- #
    def fade_in():
        alpha = root.attributes('-alpha')
        if alpha < 1.0:
            root.attributes('-alpha', min(1.0, alpha + 0.08))
            root.after(16, fade_in)
    fade_in()
    
    def shake_ui():
        offsets = [15, -15, 10, -10, 5, -5, 0]
        def perform_shake(step=0, prev_dx=0):
            if step < len(offsets) and root.winfo_exists():
                dx = offsets[step]
                shift = dx - prev_dx
                canvas.move("all", shift, 0)
                root.after(30, lambda: perform_shake(step+1, dx))
        perform_shake()
        
    def slide_up_and_close(step=0, max_steps=25):
        if not root.winfo_exists(): return
        
        t = step / max_steps
        current_alpha = 1.0 - t
        
        # Safely move elements up by 15px each step
        canvas.move("all", 0, -15)
        
        root.attributes('-alpha', max(0.0, current_alpha))
        
        if step < max_steps:
            root.after(16, lambda: slide_up_and_close(step+1, max_steps))
        else:
            close_success()
            
    def unlock_sequence(method="Face ID", username=None):
        nonlocal unlocked, pct_target
        if unlocked: return
        unlocked = True
        
        if method != "Telegram":
            try:
                from providers.telegram_bot import send_telegram_photo
                import datetime
                dt = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                caption = f"⚠️ VANTA Unlocked\nMethod: {method}\nTime: {dt}"
                
                if last_frame_img:
                    photo_path = os.path.join(base_dir, "vault", "intruder_snapshot.jpg")
                    bg = Image.new("RGB", last_frame_img.size, (0, 0, 0))
                    bg.paste(last_frame_img, mask=last_frame_img.split()[3])
                    bg.save(photo_path, "JPEG")
                    threading.Thread(target=send_telegram_photo, args=(caption, photo_path), daemon=True).start()
            except Exception as e:
                print(f"[Security] Alert failed: {e}")
                
        canvas.itemconfig(status_id, text="") # Hide text, the liquid checkmark speaks for itself!
        
        pct_target = 100
        canvas.itemconfig(arc_id, outline="#34C759")
        canvas.itemconfig(pill_bg_id, state="hidden")
        canvas.itemconfig(passcode_text_id, state="hidden")
        
        def liquid_fill_anim(step=0):
            nonlocal current_vid_photo
            max_steps = 45 
            if step <= max_steps and root.winfo_exists():
                fill_pct = step / max_steps
                base = last_frame_img if last_frame_img else Image.new("RGBA", (240, 240), (30, 30, 32, 255))
                liquid_img = draw_liquid_wave(base, fill_pct, time_t=step*0.35)
                
                current_vid_photo = ImageTk.PhotoImage(liquid_img)
                canvas.itemconfig(vid_id, image=current_vid_photo)
                root.after(16, lambda: liquid_fill_anim(step+1))
            else:
                def show_greeting():
                    if username:
                        canvas.itemconfig(title_id, state="hidden")
                        canvas.itemconfig(arc_id, state="hidden")
                        canvas.itemconfig(vid_id, state="hidden")
                        canvas.itemconfig(ring_bg_id, state="hidden")
                        canvas.create_text(cx, cy, text=f"Hi {username}", font=("Segoe UI", 36, "bold"), fill="white")
                        root.after(1500, slide_up_and_close)
                    else:
                        slide_up_and_close()
                
                # Let the full green liquid sit for a brief moment before fading to the greeting
                root.after(600, show_greeting)
                
        liquid_fill_anim(0)
        
    def check_passcode():
        nonlocal passcode_str
        pwd = passcode_str
        if not pwd: return
        
        if pwd == AUTH_PASSWORD:
            unlock_sequence(method="Passcode")
        else:
            canvas.itemconfig(status_id, text="Incorrect Passcode", fill="#FF3B30")
            passcode_str = ""
            canvas.itemconfig(passcode_text_id, text="Passcode", fill="#98989D", font=("Segoe UI", 14))
            root.after(2000, lambda: canvas.itemconfig(status_id, text="Scanning...", fill="#8E8E93") if not unlocked else None)
            shake_ui()
    
    arc_after_id = video_after_id = telegram_after_id = None

    def close_success():
        nonlocal arc_after_id, video_after_id, telegram_after_id
        stop_event.set()
        if cap.isOpened(): cap.release()
        for after_id in (arc_after_id,video_after_id,telegram_after_id):
            if after_id:
                try: root.after_cancel(after_id)
                except Exception: pass
        # Animations also schedule short-lived callbacks. Cancel every Tcl
        # after job before destroying the interpreter.
        try:
            for after_id in root.tk.call("after", "info"):
                try: root.after_cancel(after_id)
                except Exception: pass
        except Exception:
            pass
        with contextlib.redirect_stderr(open(os.devnull, 'w')):
            try: root.destroy()
            except Exception: pass
    pct_target = 0.0
    pct_current = 0.0
    
    def update_arc_loop():
        nonlocal pct_current, unlocked, arc_after_id
        try:
            if root.winfo_exists():
                pct_current += (pct_target - pct_current) * 0.15 # Faster, smoother 60fps interpolation
                
                extent_deg = -(pct_current / 100.0) * 359.9
                if abs(extent_deg) < 1: extent_deg = -1 
                canvas.itemconfig(arc_id, extent=extent_deg)
                
                arc_after_id = root.after(16, update_arc_loop)
        except Exception:
            pass

    def check_telegram():
        nonlocal telegram_after_id
        if input_queue and not unlocked and root.winfo_exists():
            try:
                import queue
                while True: # drain all pending messages
                    msg = input_queue.get_nowait()
                    if msg.startswith("[Telegram]:"):
                        text = msg.replace("[Telegram]:", "").strip()
                        if text == AUTH_PASSWORD:
                            root.after(0, lambda: unlock_sequence(method="Telegram"))
                            break
            except queue.Empty:
                pass
            except Exception:
                pass
            telegram_after_id = root.after(500, check_telegram)
            
    check_telegram()

    def check_face():
        nonlocal pct_target
        faces_dir = os.path.join(base_dir, "vault", "faces")
        if not os.path.exists(faces_dir) or len(os.listdir(faces_dir)) == 0:
            root.after(0, lambda: canvas.itemconfig(status_id, text="No Face ID setup. Use passcode."))
            return
            
        try:
            # Force DeepFace to use the pendrive vault for weights instead of C:\Users\user\.deepface
            os.environ["DEEPFACE_HOME"] = os.path.join(base_dir, "vault")
            from deepface import DeepFace
            
            # Pre-load all registered faces into memory to eliminate disk I/O during checking
            known_faces = []
            for f in os.listdir(faces_dir):
                if f.lower().endswith(".jpg"):
                    name = os.path.splitext(f)[0]
                    img_arr = cv2.imread(os.path.join(faces_dir, f))
                    known_faces.append((name, img_arr))
                    
            if not known_faces:
                root.after(0, lambda: canvas.itemconfig(status_id, text="No Face ID setup. Use passcode."))
                return

            # Warm up the model once before entering the loop
            DeepFace.build_model("Facenet512")
            
            while not stop_event.is_set() and not unlocked:
                ret, frame = cap.read()
                if not ret:
                    time.sleep(0.5)
                    continue
                
                try:
                    # Downscale large frames to prevent MTCNN from exhausting RAM
                    if frame.shape[1] > 800:
                        scale = 800 / frame.shape[1]
                        frame = cv2.resize(frame, (800, int(frame.shape[0] * scale)))
                        
                    pct = pct_target + 5
                    if pct > 95: pct = 95
                    pct_target = pct
                    
                    matched_user = None
                    for name, img_arr in known_faces:
                        result = DeepFace.verify(
                            img1_path=frame, 
                            img2_path=img_arr, 
                            model_name="Facenet512",
                            detector_backend="mtcnn",
                            enforce_detection=False,
                            silent=True
                        )
                        if result.get("verified", False):
                            matched_user = name
                            break
                            
                    if matched_user:
                        root.after(0, lambda: unlock_sequence(method="Face ID", username=matched_user))
                        break
                except Exception as e:
                    import traceback
                    traceback.print_exc()
                    if cap.isOpened(): cap.release()
                    root.after(0, lambda: canvas.itemconfig(status_id, text="Face ID Error. Use passcode.", fill="#FF3B30"))
                    break
                
                time.sleep(0.1) # Shorter sleep for snappier feeling
        except Exception as e:
            import traceback
            traceback.print_exc()
            if cap.isOpened(): cap.release()
            root.after(0, lambda: canvas.itemconfig(status_id, text="Face ID Error. Use passcode.", fill="#FF3B30"))
            
    # Keep reference to images to prevent garbage collection
    current_vid_photo = None
    last_frame_img = None
    
    def update_video_loop():
        nonlocal current_vid_photo, last_frame_img, video_after_id
        if not stop_event.is_set() and not unlocked and root.winfo_exists():
            try:
                ret, frame = cap.read()
                if ret:
                    last_frame_img = process_circular_frame(frame, size=240)
                    current_vid_photo = ImageTk.PhotoImage(last_frame_img)
                    canvas.itemconfig(vid_id, image=current_vid_photo)
            except Exception:
                pass
            video_after_id = root.after(30, update_video_loop)
    
    update_arc_loop()
    update_video_loop()
    threading.Thread(target=check_face, daemon=True).start()
    
    def on_close(event=None):
        stop_event.set()
        if cap.isOpened(): cap.release()
        with contextlib.redirect_stderr(open(os.devnull, 'w')):
            os._exit(0)
            
    root.protocol("WM_DELETE_WINDOW", on_close)
    root.bind("<Escape>", on_close)
    
    root.lift()
    # Removed automatic focus_force so the placeholder is visible initially
    root.mainloop()
    
    if not unlocked:
        with contextlib.redirect_stderr(open(os.devnull, 'w')):
            sys.exit(0)

if __name__ == "__main__":
    show_lockscreen()
