with open('d:\\winter\\providers\\google_api.py', 'r', encoding='utf-8') as f:
    content = f.read()

target = '''        contacts_path = os.path.join(base_dir, 'vault', 'system', 'contacts.json')
        # Merge with existing contacts if any
        if os.path.exists(contacts_path):
            with open(contacts_path, 'r', encoding='utf-8') as f:
                try:
                    existing = json.load(f)
                    contacts_dict.update(existing)
                except Exception:
                    pass
        
        with open(contacts_path, 'w', encoding='utf-8') as f:
            json.dump(contacts_dict, f, indent=4)
            
        print(f"[Contacts] Synced {len(connections)} Google Contacts to vault.")
        return len(contacts_dict)'''

injection = '''        # --- PHASE 3: Sync to Graph DB ---
        from memory.graph_store import add_edge
        from memory.entities import get_canonical_name, add_entity
        for name, phone in contacts_dict.items():
            try:
                canonical = get_canonical_name(name)
                if canonical == name:
                    add_entity(name, "Person")
                add_edge("User", "HAS_CONTACT", name, confidence=1.0, source_id="google_contacts_sync")
                add_edge(name, "HAS_PHONE", phone, confidence=1.0, source_id="google_contacts_sync")
            except Exception as e:
                print(f"[Contacts Sync] Failed to insert {name}: {e}")
        
        # Clear the TTL cache to force a refresh on next read
        try:
            import memory.contacts
            memory.contacts._cache_timestamp = 0
        except Exception:
            pass
            
        print(f"[Contacts] Successfully synced {len(contacts_dict)} contacts to Graph DB.")
        return len(contacts_dict)'''

content = content.replace(target, injection)

with open('d:\\winter\\providers\\google_api.py', 'w', encoding='utf-8') as f:
    f.write(content)
print('Patched google_api.py')
