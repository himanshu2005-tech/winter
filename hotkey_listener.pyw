import os
import subprocess
import threading
from pynput import keyboard

base_dir = os.path.dirname(os.path.abspath(__file__))

def show_splash():
    import tkinter as tk
    splash = tk.Tk()
    splash.overrideredirect(True)
    splash.attributes('-topmost', True)
    splash.config(bg='#1c1c1e')
    
    sw = splash.winfo_screenwidth()
    w, h = 220, 44
    x = (sw - w) // 2
    y = 60
    splash.geometry(f"{w}x{h}+{x}+{y}")
    
    lbl = tk.Label(splash, text="Initializing WINTER...", fg="#f5f5f7", bg="#1c1c1e", font=("Segoe UI", 12, "bold"))
    lbl.pack(expand=True, fill="both")
    
    splash.after(4500, splash.destroy)
    splash.mainloop()

def launch_winter():
    vbs_path = os.path.join(base_dir, 'start_silent.vbs')
    subprocess.Popen(['wscript.exe', vbs_path])
    threading.Thread(target=show_splash, daemon=True).start()

with keyboard.GlobalHotKeys({'<ctrl>+<alt>+w': launch_winter}) as h:
    h.join()
