from memory.graph_store import get_current_edges
from memory.entities import get_canonical_name

def get_core_profile_summary():
    """
    Builds an always-on summary of core facts (edges) for the system prompt.
    Caps at approximately ~300 tokens (1200 chars).
    """
    subject = get_canonical_name("Himanshu")
    edges = get_current_edges(subject=subject)
    
    if not edges:
        return "Core Profile is currently empty."
        
    lines = ["=== CORE PROFILE (Current Facts) ==="]
    
    # Sort by predicate for neatness
    edges.sort(key=lambda x: x[1])
    
    for subj, pred, obj in edges:
        lines.append(f"[{subj}] --{pred}--> [{obj}]")
        
    summary = "\n".join(lines)
    
    # Cap at ~300 tokens (~1200 characters) to preserve context for Vision/Screenshots
    if len(summary) > 1200:
        summary = summary[:1150] + "\n...[Profile truncated for brevity]..."
        
    return summary
