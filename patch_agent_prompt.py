with open('d:\\winter\\agent.py', 'r', encoding='utf-8') as f:
    content = f.read()

target = '''<ACTION type="SEARCH_WEB" query="who won the 2024 super bowl" />'''

injection = '''<ACTION type="SEARCH_WEB" query="who won the 2024 super bowl" />

- If Himanshu asks you to "prepare" or "research" a topic in-depth, use the DEEP_RESEARCH action. This will autonomously scrape the web and permanently commit the findings to your Chroma and Graph databases. You do NOT need to read the output; it is saved for your future retrieval.
<ACTION type="DEEP_RESEARCH" topic="Amazon ML Challenge 2026" />'''

if target in content:
    content = content.replace(target, injection)
    with open('d:\\winter\\agent.py', 'w', encoding='utf-8') as f:
        f.write(content)
    print("Patched agent.py with DEEP_RESEARCH instructions")
else:
    print("Target block not found in agent.py")
