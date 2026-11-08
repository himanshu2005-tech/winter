import json
import networkx as nx
import os

def migrate_to_graph():
    # File paths
    alarms_path = r"d:\winter\vault\alarms.json"
    contacts_path = r"D:\winter\dist\WINTER_SYSTEM\vault\system\contacts.json"
    output_path = r"d:\winter\vault\system_graph.json"

    # Create directed graph
    G = nx.DiGraph()

    # 1. Central Himanshu Node
    G.add_node("himanshu", type="user", name="Himanshu")

    # 2. Category Nodes
    G.add_node("alarms_category", type="category", label="Alarms")
    G.add_node("contacts_category", type="category", label="Contacts")

    # 3. Link categories to himanshu
    G.add_edge("himanshu", "alarms_category", relation="HAS_CATEGORY")
    G.add_edge("himanshu", "contacts_category", relation="HAS_CATEGORY")

    # 4. Load Alarms
    if os.path.exists(alarms_path):
        with open(alarms_path, 'r', encoding='utf-8') as f:
            alarms = json.load(f)
            for alarm in alarms:
                alarm_id = f"alarm_{alarm.get('id')}"
                G.add_node(alarm_id, type="alarm", **alarm)
                G.add_edge("alarms_category", alarm_id, relation="HAS_ALARM")
    
    # 5. Load Contacts
    if os.path.exists(contacts_path):
        with open(contacts_path, 'r', encoding='utf-8') as f:
            contacts = json.load(f)
            for name, phone in contacts.items():
                contact_id = f"contact_{name.lower().replace(' ', '_')}"
                G.add_node(contact_id, type="contact", name=name, phone=phone)
                G.add_edge("contacts_category", contact_id, relation="HAS_CONTACT")

    # 6. Save Graph to JSON (Node-Link format)
    data = nx.node_link_data(G)
    
    # Ensure directory exists
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=4)
        
    print(f"Graph successfully generated and saved to {output_path}")
    print(f"Nodes: {G.number_of_nodes()}, Edges: {G.number_of_edges()}")

if __name__ == "__main__":
    migrate_to_graph()
