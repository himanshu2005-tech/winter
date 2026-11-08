"""
whatsapp.py - VANTA WhatsApp Send Module
Uses pywhatkit to send WhatsApp messages via WhatsApp Web.
Contacts stored in vault/system/contacts.json: {"Name": "+91XXXXXXXXXX"}
"""
import os
import json
import time

CONTACTS_FILE = None

def _get_contacts_file(base_dir=None):
    global CONTACTS_FILE
    if CONTACTS_FILE is None:
        if base_dir:
            CONTACTS_FILE = os.path.join(base_dir, "vault", "contacts.json")
        else:
            CONTACTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vault", "contacts.json")
    return CONTACTS_FILE

def _load_contacts(base_dir=None):
    try:
        from memory.contacts import get_all_contacts
        return get_all_contacts()
    except Exception as e:
        print(f"[WhatsApp] Failed to load contacts from Graph DB: {e}")
        return {}

def add_contact(name, phone, base_dir=None):
    """Add a contact. phone must include country code e.g. +91XXXXXXXXXX"""
    path = _get_contacts_file(base_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    contacts = _load_contacts(base_dir)
    contacts[name.strip()] = phone.strip()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(contacts, f, indent=2)
    return f"Contact saved: {name} -> {phone}"

def send_message(contact_name, message, base_dir=None):
    """
    Send a WhatsApp message to a contact by name.
    Falls back to phone number if contact_name looks like a number.
    """
    contacts = _load_contacts(base_dir)
    
    # Check if it's a direct number
    if contact_name.startswith("+"):
        phone = contact_name
    else:
        # Case-insensitive lookup
        phone = None
        for name, num in contacts.items():
            if name.lower() == contact_name.lower():
                phone = num
                break
        if not phone:
            return f"[WhatsApp] Contact '{contact_name}' not found. Add them to vault/system/contacts.json first."
    
    try:
        import pywhatkit
        # Scheduling for "one minute from now" returned before the message was
        # actually sent, so VANTA reported success while a late browser tab was
        # still waiting. The instant API blocks through the send keystroke.
        pywhatkit.sendwhatmsg_instantly(
            phone_no=phone,
            message=message,
            wait_time=15,
            tab_close=True,
            close_time=3
        )
        return f"WhatsApp message sent to {contact_name}: \"{message}\""
    except ImportError:
        return "[WhatsApp] pywhatkit not installed. Run: pip install pywhatkit"
    except Exception as e:
        return f"[WhatsApp] Failed to send message: {e}"
