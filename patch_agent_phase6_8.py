with open('d:\\winter\\agent.py', 'r', encoding='utf-8') as f:
    content = f.read()

target1 = '''HIMANSHU'S HARD PREFERENCES (NEVER violate these):'''
injection1 = '''HIMANSHU'S HARD PREFERENCES (NEVER violate these):
- Wealth & Monetization Core Directive: Your ultimate directive is to help Himanshu get rich. Every time you write code, suggest features, or analyze a problem for his startup 'outrun-this', you MUST evaluate it through the lens of scalability, recurring revenue, and monetization. Push him to monetize aggressively and prioritize profitable actions.'''

if target1 in content:
    content = content.replace(target1, injection1)
    print("Injected Wealth Directive into SYSTEM_PROMPT")

target2 = '''    return (
        f"Current Time: {now.strftime('%A, %d %B %Y %H:%M')} | Session uptime: VANTA has been running since startup.\\n"
        f"--- SYSTEM TELEMETRY ---\\n{telemetry_data}\\n\\n"
        f"--- UPCOMING CALENDAR EVENTS (next 48h) ---\\n{cal_data}\\n\\n"
        f"--- ACTIVE ALARMS ---\\n{alarm_data}"
    )'''

injection2 = '''    return (
        f"Current Time: {now.strftime('%A, %d %B %Y %H:%M')} | Session uptime: VANTA has been running since startup.\\n"
        f"--- SYSTEM TELEMETRY ---\\n{telemetry_data}\\n\\n"
        f"--- UPCOMING CALENDAR EVENTS (next 48h) ---\\n{cal_data}\\n\\n"
        f"--- ACTIVE ALARMS ---\\n{alarm_data}\\n\\n"
        f"[MISSION: WEALTH GENERATION] -> Constantly optimize for Himanshu's financial success and outrun-this profitability."
    )'''

if target2 in content:
    content = content.replace(target2, injection2)
    print("Injected Wealth Mission into gather_context")

target3 = '''        # 0b. Create one morning agenda briefing per day.  It enters the same'''
injection3 = '''        # 0a. Phase 6: Proactive Grill Me Loop (Interactive DB Building)
        # Every 15 minutes, check if we've been idle, then inject a grill request
        if 'last_grill_check' not in locals():
            last_grill_check = now
        if now - last_grill_check > 900:  # 15 minutes
            last_grill_check = now
            # inject proactive event
            input_queue.put(("proactive", "[SYSTEM EVENT: PROACTIVE GRILL] You are currently idle. Review Himanshu's Graph DB. Identify a gap in your knowledge regarding his startup 'outrun-this', his daily workflow, or his monetization strategy. Ask him exactly ONE short, engaging question to extract this information to expand your database."))

        # 0b. Create one morning agenda briefing per day.  It enters the same'''

if target3 in content:
    content = content.replace(target3, injection3)
    print("Injected Proactive Grill Me Loop into proactivity_worker")

with open('d:\\winter\\agent.py', 'w', encoding='utf-8') as f:
    f.write(content)
