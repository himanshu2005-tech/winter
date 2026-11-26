with open('d:\\winter\\actions.py', 'r', encoding='utf-8') as f:
    content = f.read()

target = '''            elif action_type == 'SEARCH_WEB':'''

injection = '''            elif action_type == 'DEEP_RESEARCH':
                topic = attrs.get('topic', '')
                if topic:
                    msg = f"Autonomous Deep Research: '{topic}'"
                    print(f"[Action] {msg}")
                    ui_queue.put(("action", msg))
                    ui_queue.put(("info", f"Executing deep web search for:\\n{topic}..."))

                    try:
                        import requests
                        tavily_key = os.getenv("TAVILY_API_KEY")
                        if not tavily_key:
                            raise Exception("Tavily API key is missing in your .env file!")
                        
                        payload = {
                            "api_key": tavily_key,
                            "query": topic,
                            "search_depth": "advanced",
                            "include_answer": True,
                            "include_raw_content": True
                        }
                        response = requests.post("https://api.tavily.com/search", json=payload, timeout=25)
                        response.raise_for_status()
                        data = response.json()
                        
                        summary = data.get("answer", "")
                        results = data.get("results", [])
                        
                        raw_content = ""
                        if summary:
                            raw_content += f"[Summary]\\n{summary}\\n\\n"
                        for res in results:
                            raw_content += f"[Content from {res.get('title')}]\\n{res.get('raw_content') or res.get('content', '')}\\n\\n"
                            
                        if not raw_content.strip():
                            raise Exception("No deep content found.")
                            
                        # 1. Save to ChromaDB
                        import memory
                        memory.save_knowledge(topic, raw_content)
                        
                        # 2. Queue for Graph DB (Research Mode)
                        from memory.consolidator import queue_for_consolidation
                        # we pass the current turn_id as source_id for traceability
                        queue_for_consolidation(raw_content, source_id=turn_id, mode="research")
                        
                        execute_actions._last_output = f"[Success] Deep Research complete. {len(raw_content.split())} words fetched, stored in ChromaDB, and enqueued for Graph extraction."
                        ui_queue.put(("info", execute_actions._last_output))
                    except Exception as e:
                        err = f"[Error] Deep Research failed: {e}"
                        execute_actions._last_output = err
                        print(err)
                        ui_queue.put(("info", f"> DEEP_RESEARCH\\n\\n{err}"))

            elif action_type == 'SEARCH_WEB':'''

if target in content:
    content = content.replace(target, injection)
    with open('d:\\winter\\actions.py', 'w', encoding='utf-8') as f:
        f.write(content)
    print("Patched actions.py with DEEP_RESEARCH")
else:
    print("Target block not found in actions.py")
