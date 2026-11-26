with open('d:\\winter\\ui.py', 'r', encoding='utf-8') as f:
    content = f.read()
content = content.replace('STATE_THINKING:  "THINKING",', 'STATE_THINKING:  "PROCESSING",')
with open('d:\\winter\\ui.py', 'w', encoding='utf-8') as f:
    f.write(content)
print("ui.py patched: THINKING -> PROCESSING")


with open('d:\\winter\\lockscreen.py', 'r', encoding='utf-8') as f:
    content = f.read()
target = '''    def update_arc_loop():
        nonlocal pct_current, unlocked
        if root.winfo_exists():
            pct_current += (pct_target - pct_current) * 0.15 # Faster, smoother 60fps interpolation
            
            extent_deg = -(pct_current / 100.0) * 359.9
            if abs(extent_deg) < 1: extent_deg = -1 
            canvas.itemconfig(arc_id, extent=extent_deg)
            
            root.after(16, update_arc_loop)'''

injection = '''    def update_arc_loop():
        nonlocal pct_current, unlocked
        try:
            if root.winfo_exists():
                pct_current += (pct_target - pct_current) * 0.15 # Faster, smoother 60fps interpolation
                
                extent_deg = -(pct_current / 100.0) * 359.9
                if abs(extent_deg) < 1: extent_deg = -1 
                canvas.itemconfig(arc_id, extent=extent_deg)
                
                root.after(16, update_arc_loop)
        except Exception:
            pass'''
content = content.replace(target, injection)
with open('d:\\winter\\lockscreen.py', 'w', encoding='utf-8') as f:
    f.write(content)
print("lockscreen.py patched: arc loop exception handled")


with open('d:\\winter\\agent.py', 'r', encoding='utf-8') as f:
    content = f.read()

target2 = '''                        except Exception as e:
                            print(f"[Memory] Could not persist direct reply: {e}")
                return

        print(f"[Agent] Reached max {MAX_AGENT_STEPS} steps, stopping.")
        set_state(STATE_IDLE)

    except Exception as e:
        import traceback
        print(f"[Pipeline error] {e}")
        traceback.print_exc()
        # For transient Windows API errors (e.g. Invalid window handle), try to continue
        err_str = str(e)
        if "1400" in err_str or "GetWindow" in err_str or "Invalid window handle" in err_str:
            print("[Pipeline] Transient UIA error — ignoring and staying ready.")
        set_state(STATE_IDLE)'''

injection2 = '''                        except Exception as e:
                            print(f"[Memory] Could not persist direct reply: {e}")
                if current_state != STATE_LISTENING:
                    set_state(STATE_IDLE)
                return

        print(f"[Agent] Reached max {MAX_AGENT_STEPS} steps, stopping.")
        if current_state != STATE_LISTENING:
            set_state(STATE_IDLE)

    except Exception as e:
        import traceback
        print(f"[Pipeline error] {e}")
        traceback.print_exc()
        # For transient Windows API errors (e.g. Invalid window handle), try to continue
        err_str = str(e)
        if "1400" in err_str or "GetWindow" in err_str or "Invalid window handle" in err_str:
            print("[Pipeline] Transient UIA error — ignoring and staying ready.")
        else:
            speak(f"Sir, I encountered a pipeline error: {err_str}", mute=False)
        if current_state != STATE_LISTENING:
            set_state(STATE_IDLE)'''
content = content.replace(target2, injection2)
with open('d:\\winter\\agent.py', 'w', encoding='utf-8') as f:
    f.write(content)
print("agent.py patched: state resets and error vocalization added")
