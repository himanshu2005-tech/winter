import sys
import os

# Add the parent directory to sys.path so we can import 'memory'


def seed():
    """Write the foundational entities and facts into the graph.

    This used to run at import time, so anything that so much as imported
    this module re-seeded the graph as a side effect -- including a test
    run or a tooling sweep that imports every module to check it loads.
    """
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

    from memory.graph_store import add_edge
    from memory.entities import add_entity
    from memory.core_profile import get_core_profile_summary

    print("=== Hybrid Graph Database Seeding ===")
    print("1. Adding entity aliases...")
    add_entity("Amrita Vishwa Vidyapeetham", "University", ["Amrita"])
    add_entity("Himanshu", "Person")

    print("\n2. Inserting foundational facts...")
    res1 = add_edge("Himanshu", "STUDIES_AT", "Amrita Vishwa Vidyapeetham", confidence=1.0)
    res2 = add_edge("Himanshu", "EDUCATION_LEVEL", "UG", confidence=1.0)
    print(f" -> {res1}")
    print(f" -> {res2}")

    print("\n3. Testing Supersession (Updating Education Level)...")
    # This should supersede the old edge
    res3 = add_edge("Himanshu", "EDUCATION_LEVEL", "B.Tech", confidence=1.0)
    print(f" -> {res3}")

    print("\n4. Fetching Core Profile Summary (What VANTA will see):")
    summary = get_core_profile_summary()
    print("-" * 40)
    print(summary)
    print("-" * 40)
    print("\nSeeding complete. Run start.bat to use the new Hybrid Memory System!")



if __name__ == "__main__":
    seed()
