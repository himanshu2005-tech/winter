import re

with open('d:\\winter\\actions.py', 'r', encoding='utf-8') as f:
    content = f.read()

target = '''        try:
            if action_type == 'BEGIN_VISIBLE_SESSION':'''

injection = '''
        # --- PHASE 2: Approvals Intercept ---
        import uuid
        import json
        
        is_consequential = action_type in {'SEND_TELEGRAM', 'SEND_EMAIL', 'RUN_COMMAND', 'ADD_CALENDAR_EVENT', 'DELETE_CALENDAR_EVENT'}
        if action_type == 'ADD_ALARM' and origin != 'user':
            is_consequential = True
            
        payload_json = json.dumps(attrs)
        
        if is_consequential:
            action_id = str(uuid.uuid4())
            from memory.state_db import execute_write
            execute_write(
                """INSERT INTO approvals (id, action_type, payload, inference_source, status, expires_at) 
                   VALUES (?, ?, ?, ?, 'pending', datetime('now', '+1 hour'))""",
                (action_id, action_type, payload_json, origin)
            )
            execute_actions._last_output = f"[Approval Required] Queued {action_type} for manual approval."
            ui_queue.put(("info", f"VANTA requires approval to execute: {action_type}\\nCheck dashboard or say 'approve that'."))
            
            # --- PHASE 2: Action Log (Pending) ---
            execute_write(
                """INSERT INTO action_log (id, action_type, payload, inference_source, outcome, verified_flag)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (str(uuid.uuid4()), action_type, payload_json, origin, 'pending_approval', False)
            )
            continue
            
'''

new_content = content.replace(target, injection + target)


target_end = '''            ui_queue.put(("info", f"> WROTE FILE\\n\\nError: {e}"))

    clean_text = re.sub(r'<[A-Za-z0-9_]+(?:\\s+[^>]*)?/>', '', response_text)'''

injection_end = '''            ui_queue.put(("info", f"> WROTE FILE\\n\\nError: {e}"))

        # --- PHASE 2: Action Log (Executed) ---
        try:
            from memory.state_db import execute_write
            import uuid
            import json
            outcome_str = str(getattr(execute_actions, '_last_output', 'Executed'))
            verified = '[Action Error]' not in outcome_str and 'Failed' not in outcome_str
            execute_write(
                """INSERT INTO action_log (id, action_type, payload, inference_source, outcome, verified_flag)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (str(uuid.uuid4()), action_type, json.dumps(attrs), origin, outcome_str, verified)
            )
        except Exception as log_e:
            print(f"[Audit Error] Failed to log action {action_type}: {log_e}")

    clean_text = re.sub(r'<[A-Za-z0-9_]+(?:\\s+[^>]*)?/>', '', response_text)'''

new_content = new_content.replace(target_end, injection_end)

with open('d:\\winter\\actions.py', 'w', encoding='utf-8') as f:
    f.write(new_content)
print('Patched actions.py for Phase 2!')
