"""Compatibility API mapping VANTA's legacy SQL-shaped calls to Neo4j."""
import re

from memory.neo4j_store import store

def init_state_db(): store.ensure_schema()
def _sql(query): return " ".join(str(query).lower().split())

def execute_write(query,params=()):
    q=_sql(query)
    if q.startswith("insert into alarms") and " select " not in q:
        v=list(params)+[None]*7
        store.query("MERGE (a:Alarm {id:$id}) SET a.label=$label,a.fire_at=$fire_at,a.recurrence=$recurrence,a.enabled=true,a.created_by=$created_by,a.source=$source,a.status='pending',a.created_at=coalesce(a.created_at,toString(datetime()))",id=v[0],label=v[1],fire_at=v[2],recurrence=v[3],created_by=v[4],source=v[5]); return
    if q.startswith("insert into alarms") and " select " in q:
        new_id,fire_at,old_id=params
        store.query("MATCH (old:Alarm {id:$old_id}) MERGE (a:Alarm {id:$id}) SET a.label=old.label,a.fire_at=$fire_at,a.recurrence=old.recurrence,a.enabled=old.enabled,a.created_by=old.created_by,a.source=old.source,a.status='pending',a.created_at=toString(datetime())",old_id=old_id,id=new_id,fire_at=fire_at); return
    if q.startswith("update alarms set status"):
        status=q.split("status = '",1)[1].split("'",1)[0]
        store.query("MATCH (a:Alarm {id:$id}) SET a.status=$status,a.updated_at=toString(datetime())",id=params[0],status=status); return
    if q.startswith("insert into approvals"):
        i,t,p,s=params
        store.query("MERGE (a:Approval {id:$id}) SET a.action_type=$type,a.payload=$payload,a.inference_source=$source,a.status='pending',a.created_at=toString(datetime()),a.expires_at=toString(datetime()+duration('PT1H'))",id=i,type=t,payload=p,source=s); return
    if q.startswith("update approvals set status"):
        status="approved" if "approved" in q else "rejected"
        store.query("MATCH (a:Approval {id:$id}) SET a.status=$status,a.decided_at=toString(datetime())",id=params[0],status=status); return
    if q.startswith("insert into action_log"):
        i,t,p,s,o,v=params
        store.query("MERGE (a:ActionLog {id:$id}) SET a.action_type=$type,a.payload=$payload,a.inference_source=$source,a.outcome=$outcome,a.verified_flag=$verified,a.created_at=toString(datetime())",id=i,type=t,payload=p,source=s,outcome=o,verified=bool(v)); return
    if "into notified" in q:
        i,s=params
        store.query("MERGE (n:Notification {key:$key}) SET n.id=$id,n.source=$source,n.created_at=coalesce(n.created_at,toString(datetime()))",key=f"{s}:{i}",id=i,source=s); return
    raise ValueError(f"Unsupported legacy state write: {q}")

def execute_read(query,params=()):
    q=_sql(query)
    if "from alarms where id" in q:
        # Legacy callers use both ``id = ?`` and literal diagnostic queries.
        # The old shim assumed a bound parameter and always returned exactly
        # two columns, so ``SELECT status ... id='x'`` both crashed and could
        # not answer what was selected. Preserve SQL-shaped column order while
        # keeping the actual storage entirely in Neo4j.
        selected = q.split("from", 1)[0]
        fields = [name for name in (
            "id", "fire_at", "label", "recurrence", "status", "enabled",
            "created_by", "source",
        ) if re.search(rf"\b{re.escape(name)}\b", selected)]
        if not fields:
            raise ValueError(f"Alarm query selects no supported fields: {q}")
        if params:
            alarm_id = params[0]
        else:
            literal = re.search(r"\bid\s*=\s*'([^']+)'", q)
            if not literal:
                raise ValueError("Alarm id lookup requires an id parameter")
            alarm_id = literal.group(1)
        rows=store.query(
            "MATCH (a:Alarm {id:$id}) RETURN "
            + ",".join(f"a.{name} AS {name}" for name in fields),
            id=alarm_id)
        return [tuple(r.get(name) for name in fields) for r in rows]
    if "from alarms" in q:
        conditions=[]
        kwargs={}
        if "status = 'pending'" in q:
            conditions.append("a.status='pending'")
        status_in = re.search(r"status\s+in\s*\(([^)]*)\)", q)
        if status_in:
            statuses = re.findall(r"'([^']+)'", status_in.group(1))
            if statuses:
                conditions.append("a.status IN $statuses")
                kwargs["statuses"] = statuses
        if "enabled = 1" in q: conditions.append("a.enabled=true")
        cypher="MATCH (a:Alarm)"+(" WHERE "+" AND ".join(conditions) if conditions else "")
        if "fire_at like" in q: cypher+=(" AND " if conditions else " WHERE ")+"a.fire_at STARTS WITH $prefix"
        fields=[name for name in (
            "id", "fire_at", "label", "recurrence", "status", "enabled",
            "created_by", "source",
        ) if name in q.split("from",1)[0]]
        if not fields:
            raise ValueError(f"Alarm query selects no supported fields: {q}")
        cypher+=" RETURN "+",".join(f"a.{name} AS {name}" for name in fields)
        if "order by fire_at" in q:
            cypher+=" ORDER BY a.fire_at" + (" DESC" if "order by fire_at desc" in q else "")
        limit_match = re.search(r"\blimit\s+(\d+)\b", q)
        if limit_match:
            cypher += f" LIMIT {int(limit_match.group(1))}"
        if "fire_at like" in q:
            kwargs["prefix"] = str(params[0]).rstrip("%")
        rows=store.query(cypher,**kwargs)
        return [tuple(r.get(name) for name in fields) for r in rows]
    if "from approvals" in q:
        fields=[name for name in ("id","action_type","payload") if name in q.split("from",1)[0]]
        # A lookup by id is a different question from "what is waiting", and
        # answering the second when asked the first broke power control
        # outright. SHUTDOWN verifies consent by looking up the opaque id of a
        # row a human settled as approved; this shim returned the newest
        # *pending* row instead, which after settling is never that row. The
        # approval was consumed and the action refused every time.
        if "where id" in q or "id=?" in q.replace(" ",""):
            wanted_status="approved" if "'approved'" in q else (
                "rejected" if "'rejected'" in q else "pending")
            approval_id=params[0] if params else ""
            action_type=params[1] if len(params)>1 else None
            cypher=("MATCH (a:Approval {id:$id}) WHERE a.status=$status "
                    "AND (a.expires_at IS NULL OR a.expires_at > toString(datetime()))")
            if action_type is not None and "action_type" in q:
                cypher+=" AND a.action_type=$action_type"
            cypher+=" RETURN "+",".join(f"a.{name} AS {name}" for name in fields)
            rows=store.query(cypher,id=approval_id,status=wanted_status,
                             action_type=action_type)
            return [tuple(r.get(name) for name in fields) for r in rows]
        # Expiry is written on every approval and was never read, so a request
        # stayed live forever: a DELETE_PROJECT_FILE queued five days earlier
        # was still approvable, and because only the newest is offered at a
        # time, repeated approvals worked backwards through a stack of things
        # Himanshu had long forgotten agreeing to consider. An hour-old
        # permission is not permission.
        rows=store.query("MATCH (a:Approval {status:'pending'}) WHERE a.expires_at IS NULL OR a.expires_at > toString(datetime()) RETURN "+",".join(f"a.{name} AS {name}" for name in fields)+" ORDER BY a.created_at DESC LIMIT 1")
        return [tuple(r.get(name) for name in fields) for r in rows]
    if "from notified" in q:
        rows=store.query("MATCH (n:Notification {source:$source}) RETURN n.id AS id",source="gmail")
        return [(r["id"],) for r in rows]
    if "from schema_version" in q: return [(3,)]
    raise ValueError(f"Unsupported legacy state read: {q}")
