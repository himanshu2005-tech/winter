import os
import subprocess
import threading
import pystray
from PIL import Image, ImageDraw

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

def launch_winter(icon, item):
    vbs_path = os.path.join(base_dir, 'start_silent.vbs')
    subprocess.Popen(['wscript.exe', vbs_path])
    threading.Thread(target=show_splash, daemon=True).start()

def quit_tray(icon, item):
    icon.stop()

def create_image():
    image = Image.new('RGBA', (64, 64), color=(0, 0, 0, 0))
    d = ImageDraw.Draw(image)
    # Draw a blue circle with a white core for the tray icon
    d.ellipse((4, 4, 60, 60), fill=(10, 132, 255))
    d.ellipse((20, 20, 44, 44), fill=(255, 255, 255))
    return image

menu = pystray.Menu(
    pystray.MenuItem("Launch WINTER", launch_winter, default=True),
    pystray.MenuItem("Quit Launcher", quit_tray)
)

icon = pystray.Icon("WINTER", create_image(), "WINTER Agent", menu=menu)
icon.run()
