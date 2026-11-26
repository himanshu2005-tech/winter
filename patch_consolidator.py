with open('d:\\winter\\memory\\consolidator.py', 'r', encoding='utf-8') as f:
    content = f.read()

target = '''# Strict vocabulary for predicates (to prevent ontology sprawl)
ALLOWED_PREDICATES = {
    "LIVES_IN", "WORKS_AT", "STUDIES_AT", "LIKES", "DISLIKES", 
    "KNOWS", "HAS_GOAL", "IS_WORKING_ON", "HAS_PREFERENCE",
    "RELATED_TO", "OWNS", "EDUCATION_LEVEL"
}'''

injection = '''# Strict vocabulary for personal predicates
PERSONAL_PREDICATES = {
    "LIVES_IN", "WORKS_AT", "STUDIES_AT", "LIKES", "DISLIKES", 
    "KNOWS", "HAS_GOAL", "IS_WORKING_ON", "HAS_PREFERENCE",
    "RELATED_TO", "OWNS", "EDUCATION_LEVEL", "HAS_CONTACT", "HAS_PHONE"
}

# Broader vocabulary for deep research
RESEARCH_PREDICATES = {
    "IS_A", "REQUIRES", "PRODUCES", "USES", "PART_OF", "HAS_PROPERTY",
    "RELATED_TO", "IMPLEMENTS", "OPTIMIZES", "SUPPORTS", "DEPENDS_ON"
}'''

content = content.replace(target, injection)


target2 = '''            task = _consolidation_queue.get()
            if task is None:
                break
                
            user_text, source_id = task
            
            # The prompt strictness is our "Trust Rule" enforcer.
            prompt = (
                "You are an exact fact extraction engine.\\n"
                "Extract explicitly stated, high-confidence facts about the user from the following text.\\n"
                "Do NOT infer facts that are not explicitly stated by the user.\\n"
                "Format as a JSON array of objects with 'subject', 'predicate', 'object', and 'confidence' (0.0 to 1.0).\\n"
                "The 'subject' should almost always be the user (e.g., 'Himanshu') or an entity the user owns.\\n"
                f"Allowed predicates: {', '.join(ALLOWED_PREDICATES)}\\n"
                "Example output: [{\\"subject\\": \\"Himanshu\\", \\"predicate\\": \\"LIKES\\", \\"object\\": \\"Coffee\\", \\"confidence\\": 1.0}]\\n"
                "Return ONLY valid JSON. If no explicit facts are present, return []."
            )'''

injection2 = '''            task = _consolidation_queue.get()
            if task is None:
                break
                
            if len(task) == 3:
                user_text, source_id, mode = task
            else:
                user_text, source_id = task
                mode = "personal"
                
            if mode == "research":
                prompt = (
                    "You are an exact knowledge graph extraction engine for domain research.\\n"
                    "Extract factual, high-confidence relationships from the following text.\\n"
                    "Format as a JSON array of objects with 'subject', 'predicate', 'object', and 'confidence' (0.0 to 1.0).\\n"
                    f"Allowed predicates: {', '.join(RESEARCH_PREDICATES)}\\n"
                    "Example output: [{\\"subject\\": \\"Machine Learning\\", \\"predicate\\": \\"REQUIRES\\", \\"object\\": \\"Data\\", \\"confidence\\": 1.0}]\\n"
                    "Return ONLY valid JSON. If no facts are present, return []."
                )
                allowed_set = RESEARCH_PREDICATES
            else:
                prompt = (
                    "You are an exact fact extraction engine.\\n"
                    "Extract explicitly stated, high-confidence facts about the user from the following text.\\n"
                    "Do NOT infer facts that are not explicitly stated by the user.\\n"
                    "Format as a JSON array of objects with 'subject', 'predicate', 'object', and 'confidence' (0.0 to 1.0).\\n"
                    "The 'subject' should almost always be the user (e.g., 'Himanshu') or an entity the user owns.\\n"
                    f"Allowed predicates: {', '.join(PERSONAL_PREDICATES)}\\n"
                    "Example output: [{\\"subject\\": \\"Himanshu\\", \\"predicate\\": \\"LIKES\\", \\"object\\": \\"Coffee\\", \\"confidence\\": 1.0}]\\n"
                    "Return ONLY valid JSON. If no explicit facts are present, return []."
                )
                allowed_set = PERSONAL_PREDICATES'''

content = content.replace(target2, injection2)

# Also need to fix ALLOWED_PREDICATES reference below
target3 = '''                if pred not in ALLOWED_PREDICATES:'''
injection3 = '''                if pred not in allowed_set:'''
content = content.replace(target3, injection3)

target4 = '''def queue_for_consolidation(user_text, source_id="user_stated"):
    """Pipes user text into the background queue to extract and commit facts."""
    if user_text.strip():
        _consolidation_queue.put((user_text, source_id))'''

injection4 = '''def queue_for_consolidation(user_text, source_id="user_stated", mode="personal"):
    """Pipes user text into the background queue to extract and commit facts."""
    if user_text.strip():
        _consolidation_queue.put((user_text, source_id, mode))'''

content = content.replace(target4, injection4)


with open('d:\\winter\\memory\\consolidator.py', 'w', encoding='utf-8') as f:
    f.write(content)
print("Patched consolidator.py")
