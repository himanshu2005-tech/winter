import os
import json
import sqlite3
from memory.graph_store import add_edge
from memory.entities import get_canonical_name, add_entity

def migrate():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    contacts_file = os.path.join(base_dir, 'vault', 'system', 'contacts.json')
    
    if not os.path.exists(contacts_file):
        print(f"Contacts file not found at {contacts_file}")
        return
        
    try:
        with open(contacts_file, 'r') as f:
            contacts = json.load(f)
    except Exception as e:
        print(f"Failed to read contacts JSON: {e}")
        return
        
    if not isinstance(contacts, dict):
        print("Invalid contacts format.")
        return
        
    print(f"Found {len(contacts)} contacts. Migrating to Graph DB...")
    
    migrated_count = 0
    for name, phone in contacts.items():
        try:
            # Add entity for the contact
            canonical = get_canonical_name(name)
            if canonical == name:
                add_entity(name, "Person")
                
            # Add relationships
            # Himanshu -> HAS_CONTACT -> Contact
            add_edge("Himanshu", "HAS_CONTACT", name, confidence=1.0, source_id="contacts.json")
            # Contact -> HAS_PHONE -> Phone Number
            add_edge(name, "HAS_PHONE", phone, confidence=1.0, source_id="contacts.json")
            
            migrated_count += 1
        except Exception as e:
            print(f"Failed to insert {name}: {e}")
            
    print(f"Successfully migrated {migrated_count} contacts to Graph DB.")
    
    backup_path = contacts_file + ".bak"
    os.rename(contacts_file, backup_path)
    print(f"Renamed old contacts file to {backup_path}")

if __name__ == "__main__":
    migrate()
