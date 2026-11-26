import re
from memory.graph_store import get_neighborhood
from memory import search_vault

def _extract_potential_entities(query):
    """
    Very naive entity extraction: capitalize words that look like proper nouns,
    and include 'Himanshu' by default to ensure self-context.
    """
    entities = ["Himanshu"]
    words = [w.strip('.,!?()[]{}"\'') for w in query.split()]
    for w in words:
        if w.istitle() and w not in ["What", "Where", "When", "Why", "Who", "How", "Is", "Are", "Can", "Could", "Would", "Should", "Do", "Does"]:
            entities.append(w)
    return list(set(entities))

def get_memory_context(query, max_tokens=1000):
    """
    Fuses the SQLite Graph Database with the ChromaDB Semantic Vault.
    1. Extracts entities from the query.
    2. Pulls a 2-hop graph neighborhood for those entities.
    3. Runs a ChromaDB vector search for unstructured episodes.
    4. Merges them within the token budget (approximate characters).
    """
    # 1. Entity linking
    entities = _extract_potential_entities(query)
    
    # 2. Graph Neighborhood (2 hops)
    graph_edges = get_neighborhood(entities, hops=2)
    graph_context = "=== GRAPH FACTS (High Trust) ===\n"
    if not graph_edges:
        graph_context += "No relevant relational facts found.\n"
    else:
        for subj, pred, obj in graph_edges:
            graph_context += f"[{subj}] --{pred}--> [{obj}]\n"
            
    # 3. Episodic Memory (ChromaDB)
    # We use a tighter n_results since the graph already provides the precise facts.
    chroma_results = search_vault(query, n_results=2)
    chroma_context = "=== EPISODIC MEMORY (Fuzzy/Unstructured) ===\n"
    if not chroma_results or "The vault is currently empty" in chroma_results:
        chroma_context += "No relevant episodes found.\n"
    else:
        chroma_context += chroma_results + "\n"
        
    # 4. Merging
    merged = f"{graph_context}\n{chroma_context}"
    
    # Very rough token approximation (4 chars per token)
    max_chars = max_tokens * 4
    if len(merged) > max_chars:
        merged = merged[:max_chars] + "\n...[Context Truncated to fit budget]..."
        
    return merged
