from __future__ import annotations
import json
from pathlib import Path

DEFAULTS={
    "email":{"airouter":False,"dashboard":False,"speech":False},
    "contacts":{"airouter":False,"dashboard":False,"speech":False},
    "screen":{"airouter":True,"dashboard":True,"speech":True},
    "calendar":{"airouter":True,"dashboard":True,"speech":True},
    "memory":{"airouter":True,"dashboard":True,"speech":True},
}

class PrivacyPolicy:
    def __init__(self,config_path):
        self.path=Path(config_path); self.rules={key:dict(value) for key,value in DEFAULTS.items()}
        try:
            raw=json.loads(self.path.read_text(encoding="utf-8"))
            for data_class,targets in raw.get("privacy",{}).items():
                self.rules.setdefault(data_class,{}) .update({k:bool(v) for k,v in targets.items()})
        except Exception: pass

    def allows(self,data_class,destination):
        return bool(self.rules.get(data_class,{}).get(destination,False))

    def require(self,data_class,destination):
        if not self.allows(data_class,destination):
            raise PermissionError(f"Privacy policy blocks {data_class} data from {destination}.")
