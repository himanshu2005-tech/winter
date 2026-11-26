"""Grounded, deduplicated human follow-ups for recently completed events."""
from __future__ import annotations
import datetime, hashlib, json, os, re, threading, time

_LOCK = threading.RLock()

def _path(base_dir):
    path=os.path.join(base_dir,"vault","system","followups.json")
    os.makedirs(os.path.dirname(path),exist_ok=True); return path

def _load(base_dir):
    try:
        with open(_path(base_dir),"r",encoding="utf-8") as handle: data=json.load(handle)
        return data if isinstance(data,list) else []
    except Exception: return []

def _save(base_dir,rows):
    target=_path(base_dir); temp=target+".tmp"
    with open(temp,"w",encoding="utf-8") as handle: json.dump(rows[-200:],handle,ensure_ascii=False,indent=2)
    os.replace(temp,target)

def _candidate_id(source,external_id,title,anchor):
    raw="\x1f".join((source,external_id,title,anchor))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]

def collect_candidates(base_dir):
    rows=[]
    try:
        from providers.google_api import recent_completed_events
        for event in recent_completed_events(base_dir,hours_back=48):
            title=" ".join(str(event.get("summary") or "").split())
            if not title or str(event.get("status"))=="cancelled": continue
            anchor=str(event.get("end") or event.get("start") or "")
            rows.append({"id":_candidate_id("calendar",str(event.get("id") or ""),title,anchor),
                "source":"calendar","external_id":str(event.get("id") or ""),"title":title,
                "start":str(event.get("start") or ""),"end":anchor,
                "description":str(event.get("description") or "")[:300],
                "location":str(event.get("location") or "")[:160]})
    except Exception as exc: print(f"[Follow-up] Calendar candidates unavailable: {exc}")
    try:
        import commitments
        for item in commitments._load(base_dir):
            if item.get("status") not in {"passed","done"}: continue
            anchor=str(item.get("hard_time") or item.get("created") or "")
            title=" ".join(str(item.get("what") or "").split())
            if title: rows.append({"id":_candidate_id("commitment",str(item.get("id") or ""),title,anchor),
                "source":"commitment","external_id":str(item.get("id") or ""),"title":title,
                "start":anchor,"end":anchor,"description":"","location":str(item.get("location") or "")[:160]})
    except Exception as exc: print(f"[Follow-up] Commitment candidates unavailable: {exc}")
    # Work that has stopped moving. The point of writing a pursuit down is
    # that it does not have to be remembered, and that only holds if
    # something notices when nothing has happened to it for days. The
    # candidate is the pursuit's current position, so the check-in can be
    # about the actual next step rather than a generic "how's it going".
    try:
        import pursuits
        for row in pursuits.stale(base_dir):
            title=" ".join(str(row.get("title") or "").split())
            if not title: continue
            anchor=str(row.get("updated") or row.get("created") or "")
            upcoming=pursuits.next_step(row)
            stuck=pursuits.blockers(row)
            detail=pursuits.progress_line(row)
            if stuck: detail+=f" Blocked on: {stuck[0]['what']} ({stuck[0]['reason']})."
            elif upcoming: detail+=f" Next: {upcoming['what']}."
            rows.append({"id":_candidate_id("pursuit",str(row.get("id") or ""),title,anchor),
                "source":"pursuit","external_id":str(row.get("id") or ""),"title":title,
                "start":anchor,"end":anchor,"description":detail[:300],"location":""})
    except Exception as exc: print(f"[Follow-up] Pursuit candidates unavailable: {exc}")
    with _LOCK: handled={row.get("candidate_id") for row in _load(base_dir)}
    return [row for row in rows if row["id"] not in handled]

def _extract_json(raw):
    text=str(raw or "").strip(); start,end=text.find("{"),text.rfind("}")
    if start<0 or end<start: return {}
    try: return json.loads(text[start:end+1])
    except Exception: return {}

def choose_candidate(candidates,call_llm_fn):
    if not candidates: return None
    compact=[{k:row.get(k) for k in ("id","source","title","start","end","description","location")} for row in candidates[:12]]
    prompt=("Choose whether VANTA should make one warm human follow-up about a recently completed event. "
      "Good candidates involved effort, uncertainty, emotion, evaluation, health, travel, an important meeting, "
      "or a personally meaningful outcome. Ignore routine reminders, birthdays, holidays, passive blocks, meals, "
      "and events where asking would feel mechanical. Never invent an event. Return JSON only: "
      "{\"ask\":true|false,\"id\":\"candidate id or empty\",\"question\":\"one short natural question or empty\",\"reason\":\"brief\"}. "
      "Ask how it went, not facts already known. Candidates:\n"+json.dumps(compact,ensure_ascii=False))
    raw=call_llm_fn([{"role":"system","content":"You are VANTA's bounded social-judgment selector. JSON only."},
                     {"role":"user","content":prompt}],task_level="routine")
    decision=_extract_json(raw)
    if not decision.get("ask"): return None
    candidate=next((row for row in candidates if row["id"]==str(decision.get("id") or "")),None)
    question=" ".join(str(decision.get("question") or "").split())
    if not candidate or not question or "?" not in question or len(question)>220: return None
    return {**candidate,"question":question,"reason":str(decision.get("reason") or "")[:200]}

def mark_asked(base_dir,candidate,now=None):
    now=now or datetime.datetime.now()
    with _LOCK:
        rows=_load(base_dir)
        if any(row.get("candidate_id")==candidate["id"] for row in rows): return False
        rows.append({"candidate_id":candidate["id"],"source":candidate["source"],
          "external_id":candidate.get("external_id",""),"title":candidate["title"],
          "anchor":candidate.get("end") or candidate.get("start"),"question":candidate["question"],
          "reason":candidate.get("reason",""),"asked_at":now.isoformat(timespec="seconds"),
          "answer":None,"answered_at":None})
        _save(base_dir,rows)
    return True

def record_answer_if_pending(base_dir,answer,source_id="user_stated",now=None):
    cleaned=" ".join(str(answer or "").split())
    if not cleaned: return False
    # A fresh command is not an answer to the last social question.
    if re.match(r"(?i)^(?:open|close|set|create|show|read|search|play|send|add|delete|remove|move|run|switch|start|stop|cancel|approve|reject)\b", cleaned):
        return False
    now=now or datetime.datetime.now(); chosen=None
    with _LOCK:
        rows=_load(base_dir)
        for row in reversed(rows):
            if row.get("answer"): continue
            try: asked=datetime.datetime.fromisoformat(row["asked_at"])
            except Exception: continue
            if now-asked>datetime.timedelta(hours=12): continue
            row["answer"]=cleaned[:1000]; row["answered_at"]=now.isoformat(timespec="seconds")
            chosen=dict(row); _save(base_dir,rows); break
    if not chosen: return False
    try:
        from memory.graph_store import add_edge,get_current_edges
        title=chosen["title"]; outcome=cleaned[:300]
        add_edge(title,"HAS_OUTCOME",outcome,confidence=1.0,source_id=source_id,
          fact_class="explicit_user_fact",evidence_type="user_stated",authority=100,
          sensitivity="private",autonomy_eligible=False,autonomy_scope="advisory",
          review_status="accepted",evidence_quote=outcome,evidence_ref=f"followup:{chosen['candidate_id']}")
        if not any(s==title and p=="HAS_OUTCOME" and o==outcome for s,p,o in get_current_edges(title)):
            raise RuntimeError("outcome read-back failed")
        print(f"[Follow-up] Verified outcome for '{title}'.")
    except Exception as exc: print(f"[Follow-up] Could not persist outcome: {exc}")
    return True

def status(base_dir,limit=20):
    rows=_load(base_dir)[-limit:]
    if not rows: return "No event follow-ups have been recorded."
    lines=["Recent event follow-ups:"]
    for row in reversed(rows):
        state="answered" if row.get("answer") else "awaiting answer"
        lines.append(f"- {row.get('title','Event')} — {state} — asked {row.get('asked_at','')}")
    return "\n".join(lines)

def worker(base_dir,input_queue,call_llm_fn,is_busy=None,poll_seconds=1800):
    print("[Follow-up] Relationship loop started."); time.sleep(20)
    while True:
        try:
            if not (is_busy and is_busy()):
                choice=choose_candidate(collect_candidates(base_dir),call_llm_fn)
                if choice and mark_asked(base_dir,choice):
                    input_queue.put(("proactive","[SYSTEM EVENT: HUMAN FOLLOW-UP] "
                      f"A completed event deserves one natural check-in. Event: '{choice['title']}'. "
                      f"Ask exactly this once: {choice['question']} Do not add another question or unrelated briefing."))
        except Exception as exc: print(f"[Follow-up] Worker error: {exc}")
        time.sleep(max(300,int(poll_seconds)))