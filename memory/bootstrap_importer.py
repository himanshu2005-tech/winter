"""Dry-run-first importer for explicit-user bootstrap interviews.

No write occurs unless commit=True and approved_manifest_hash exactly matches
the generated manifest hash.
"""
from __future__ import annotations
import argparse, datetime as dt, hashlib, json, re
from pathlib import Path

SCHEMA_VERSION="vanta-bootstrap-interview-v1"
IMPORTER_VERSION="1.0"
FACT_SCHEMA_VERSION="2"
SENSITIVITIES={"public","private","sensitive","restricted"}

def _canonical(value): return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(",",":"))
def _hash(value): return hashlib.sha256((value if isinstance(value,str) else _canonical(value)).encode("utf-8")).hexdigest()
def _slug(value): return re.sub(r"[^a-z0-9]+","_",str(value).casefold()).strip("_")
def evidence_id(profile_version,question_id): return f"bootstrap:{_slug(profile_version)}:{_slug(question_id)}"

def validate_dataset(data):
    errors=[]
    if not isinstance(data,dict): return ["dataset must be a JSON object"]
    if data.get("schema_version") != SCHEMA_VERSION: errors.append(f"schema_version must be {SCHEMA_VERSION}")
    for key in ("profile_version","interview_id","subject_id","source","collected_at","expected_answer_count","answers"):
        if key not in data: errors.append(f"missing top-level field: {key}")
    answers=data.get("answers") or []
    if not isinstance(answers,list): return errors+["answers must be an array"]
    if data.get("expected_answer_count") != len(answers): errors.append("expected_answer_count does not match answers length")
    seen=set()
    for index,a in enumerate(answers):
        if not isinstance(a,dict): errors.append(f"answers[{index}] must be an object"); continue
        for key in ("question_id","question_text","answer_type","raw_answer","selected_options"):
            if key not in a: errors.append(f"answers[{index}] missing {key}")
        qid=str(a.get("question_id") or "")
        if not re.fullmatch(r"q\d{3}",qid): errors.append(f"invalid question_id: {qid or index}")
        if qid in seen: errors.append(f"duplicate question_id: {qid}")
        seen.add(qid)
        if a.get("raw_answer") is None: errors.append(f"{qid}: raw_answer must be present (use empty string only if intentionally blank)")
        if not isinstance(a.get("selected_options",[]),list): errors.append(f"{qid}: selected_options must be an array")
    return errors

def _classify(question):
    q=question.casefold()
    if re.search(r"\b(confirm|permission|allow|autonom|without asking|boundary|approval)\b",q):
        return "explicit_boundary","REQUIRES_CONFIRMATION_FOR"
    if re.search(r"\b(goal|career|future|want to achieve|financial independence)\b",q):
        return "explicit_goal","HAS_GOAL"
    if re.search(r"\b(pattern|procrast|stress|when you|tend to|usually|motivat|sleep|mood|response|struggle)\b",q):
        return "self_reported_pattern","SELF_REPORTS_PATTERN"
    if re.search(r"\b(priority|important|matters most|rank)\b",q):
        return "explicit_preference","VALUES_PRIORITY"
    return "explicit_preference","HAS_PREFERENCE"

def _sensitivity(question,answer):
    text=f"{question} {answer}".casefold()
    if re.search(r"\b(mental health|medical|diagnos|medication|sexual|trauma|suicid|password|credential)\b",text): return "restricted"
    if re.search(r"\b(health|heart|sleep|mood|relationship|lonely|finance|money|income|family)\b",text): return "sensitive"
    return "private"

def normalize_dataset(data):
    errors=validate_dataset(data)
    if errors: raise ValueError("Dataset validation failed:\n- "+"\n- ".join(errors))
    evidence=[]; facts=[]; subject=str(data["subject_id"])
    for answer in data["answers"]:
        qid=answer["question_id"]; eid=evidence_id(data["profile_version"],qid)
        payload={k:answer.get(k) for k in ("question_id","question_text","answer_type","raw_answer","selected_options","user_notes")}
        ahash=_hash(payload)
        evidence.append({"id":eid,"identity":f"{data['profile_version']}:{qid}","content_hash":ahash,
            "profile_version":data["profile_version"],"interview_id":data["interview_id"],"question_id":qid,
            "title":answer["question_text"][:300],"content":_canonical(payload),"source":data["source"],
            "timestamp":data["collected_at"],"kind":"bootstrap_answer","topic":"personal_profile",
            "authority":100,"confidence":1.0,"sensitivity":_sensitivity(answer["question_text"],str(answer["raw_answer"])),
            "evidence_type":"explicit_user_answer","immutable":True,"semantic_retrievable":False})
        fact_class,predicate=_classify(answer["question_text"])
        values=list(answer.get("selected_options") or [])
        if not values: values=[str(answer["raw_answer"]).strip()]
        values=[str(v).strip() for v in values if str(v).strip()]
        for rank,value in enumerate(values,1):
            claim_key=f"{_slug(subject)}:{predicate}:{qid}"
            fid="fact_"+_hash({"claim_key":claim_key,"value":value,"source_id":eid})[:32]
            needs_confirmation=fact_class=="explicit_boundary" or bool(re.search(r"\b(send|spend|delete|contact|share)\b",value,re.I))
            facts.append({"fact_id":fid,"subject":subject,"predicate":predicate,"object":value,
                "fact_class":fact_class,"evidence_type":"explicit_user_answer","authority":100,"confidence":1.0,
                "source_id":eid,"sensitivity":_sensitivity(answer["question_text"],value),
                "autonomy_eligible":fact_class in {"explicit_preference","explicit_goal"} and not needs_confirmation,
                "autonomy_scope":"advisory","confirmation_required":needs_confirmation,"review_status":"accepted",
                "claim_key":claim_key,"valid_from":data["collected_at"],"valid_to":None,
                "supersedes_fact_id":None,"superseded_by_fact_id":None,"correction_reason":None,
                "operation":"add_value" if len(values)>1 else "replace_single","priority_rank":rank if answer["answer_type"]=="ranking" else None})
    profile_content=_canonical({"profile_version":data["profile_version"],"subject_id":subject,
        "generated_views":data.get("generated_views",{}),"answer_evidence_ids":[e["id"] for e in evidence]})
    profile={"id":f"profile:{_slug(subject)}:{_slug(data['profile_version'])}","kind":"personal_profile",
        "topic":"personal_profile","source":data["source"],"source_id":data["interview_id"],
        "profile_version":data["profile_version"],"subject_id":subject,"title":f"{subject} personal profile {data['profile_version']}",
        "content":profile_content,"content_hash":_hash(profile_content),"timestamp":data["collected_at"],
        "authority":95,"confidence":1.0,"sensitivity":"private","semantic_retrievable":True,"status":"active"}
    return evidence,facts,profile

def build_manifest(data, existing_facts=None):
    evidence,facts,profile=normalize_dataset(data); existing_facts=existing_facts or []
    conflicts=[]
    by_claim={}
    for row in existing_facts: by_claim.setdefault(row.get("claim_key"),[]).append(row)
    for fact in facts:
        current=by_claim.get(fact["claim_key"],[])
        if current and fact["operation"]=="replace_single":
            conflicts.append({"claim_key":fact["claim_key"],"existing_fact_ids":[r.get("fact_id") for r in current],"resolution":"supersede_exact_current_claim"})
    mutations={"create_immutable_evidence":evidence,"create_profile_memory":profile,"create_facts":facts,"supersessions":conflicts}
    core={"importer_version":IMPORTER_VERSION,"fact_schema_version":FACT_SCHEMA_VERSION,"profile_version":data["profile_version"],
        "interview_id":data["interview_id"],"expected_answer_count":data["expected_answer_count"],
        "imported_answer_count":len(evidence),"generated_fact_count":len(facts),"mutations":mutations}
    core["manifest_hash"]=_hash(core)
    core["import_run_id"]=f"import:{_slug(data['interview_id'])}:{core['manifest_hash'][:16]}"
    core["status"]="dry_run"
    return core

def render_manifest(manifest):
    counts={}
    for f in manifest["mutations"]["create_facts"]: counts[f["fact_class"]]=counts.get(f["fact_class"],0)+1
    lines=["VANTA bootstrap import dry-run",f"Manifest: {manifest['manifest_hash']}",
        f"Profile: {manifest['profile_version']}",f"Answers: {manifest['imported_answer_count']}/{manifest['expected_answer_count']}",
        f"Candidate facts: {manifest['generated_fact_count']}",f"Conflicts: {len(manifest['mutations']['supersessions'])}","Fact classes:"]
    lines.extend(f"- {k}: {v}" for k,v in sorted(counts.items()))
    lines.append("No database writes performed.")
    return "\n".join(lines)

def plan_claim_operation(current, candidates, operation, target_fact_ids=None, now="now"):
    """Pure mutation planner used by import/review tooling and regression tests."""
    active=[dict(row) for row in current if row.get("valid_to") is None]
    targets=set(target_fact_ids or [])
    expire=[]; create=[]
    if operation=="add_value": create=list(candidates)
    elif operation=="replace_single": expire=active; create=list(candidates[:1])
    elif operation=="replace_set": expire=active; create=list(candidates)
    elif operation in {"remove_value","suspend_value"}:
        if not targets: raise ValueError(f"{operation} requires exact target_fact_ids")
        expire=[row for row in active if row.get("fact_id") in targets]
        if len(expire)!=len(targets): raise ValueError("one or more target facts are not current")
    elif operation=="confidence_update":
        if len(targets)!=1 or len(candidates)!=1: raise ValueError("confidence_update requires one target and one replacement")
        expire=[row for row in active if row.get("fact_id") in targets]
        if len(expire)!=1: raise ValueError("confidence target is not current")
        create=[dict(candidates[0],supersedes_fact_id=expire[0]["fact_id"])]
    else: raise ValueError(f"unsupported claim operation: {operation}")
    for row in expire:
        row["valid_to"]=now
        if create: row["superseded_by_fact_id"]=create[0].get("fact_id")
    return {"expire":expire,"create":create}

def _tx_commit(tx,manifest):
    now=dt.datetime.now(dt.timezone.utc).isoformat()
    for e in manifest["mutations"]["create_immutable_evidence"]:
        row=tx.run("MATCH (m:Memory {id:$id}) RETURN m.content_hash AS hash",id=e["id"]).single()
        if row and row["hash"] != e["content_hash"]: raise ValueError(f"Immutable evidence conflict: {e['identity']}")
        if not row:
            tx.run("CREATE (m:Memory) SET m=$props,m.created_at=datetime()",props=e).consume()
    p=manifest["mutations"]["create_profile_memory"]
    row=tx.run("MATCH (m:Memory {id:$id}) RETURN m.content_hash AS hash",id=p["id"]).single()
    if row and row["hash"] != p["content_hash"]: raise ValueError(f"Profile version conflict: {p['profile_version']}")
    if not row:
        tx.run("MATCH (old:Memory {kind:'personal_profile',status:'active'}) SET old.status='historical',old.deactivated_at=datetime()",).consume()
        tx.run("CREATE (m:Memory) SET m=$props,m.created_at=datetime()",props=p).consume()
    for f in manifest["mutations"]["create_facts"]:
        if f["operation"]=="replace_single":
            old=list(tx.run("MATCH (:Entity)-[r:FACT]->(:Entity) WHERE r.claim_key=$claim AND r.valid_to IS NULL AND coalesce(r.fact_id,r.id)<>$fid RETURN coalesce(r.fact_id,r.id) AS id",claim=f["claim_key"],fid=f["fact_id"]))
            for record in old:
                tx.run("MATCH ()-[r:FACT]->() WHERE coalesce(r.fact_id,r.id)=$id SET r.valid_to=$now,r.superseded_by_fact_id=$newid,r.correction_reason='bootstrap profile replacement'",id=record["id"],now=now,newid=f["fact_id"]).consume()
                f["supersedes_fact_id"]=record["id"]
        tx.run("MERGE (s:Entity {key:$skey}) ON CREATE SET s.name=$subject,s.entity_type='Person',s.created_at=datetime() MERGE (o:Entity {key:$okey}) ON CREATE SET o.name=$object,o.entity_type='ProfileValue',o.created_at=datetime() MERGE (s)-[r:FACT {id:$fid}]->(o) ON CREATE SET r += $props,r.created_at=datetime()",skey=_slug(f["subject"]),subject=f["subject"],okey=_slug(f["object"]),object=f["object"],fid=f["fact_id"],props=f).consume()
    run={"id":manifest["import_run_id"],"importer_version":IMPORTER_VERSION,"schema_version":FACT_SCHEMA_VERSION,
        "profile_version":manifest["profile_version"],"expected_answer_count":manifest["expected_answer_count"],
        "imported_answer_count":manifest["imported_answer_count"],"generated_fact_count":manifest["generated_fact_count"],
        "manifest_hash":manifest["manifest_hash"],"status":"committed","started_at":now,"completed_at":dt.datetime.now(dt.timezone.utc).isoformat(),
        "validation_results":_canonical({"valid":True}),"verification_results":_canonical({"pending_post_commit":True})}
    tx.run("MERGE (i:ImportRun {id:$id}) ON CREATE SET i=$props",id=run["id"],props=run).consume()
    return run

def commit_manifest(manifest, approved_manifest_hash, graph_store):
    import copy
    manifest = copy.deepcopy(manifest)
    if not approved_manifest_hash or approved_manifest_hash != manifest["manifest_hash"]:
        raise PermissionError("Commit requires the exact approved manifest hash")
    core = {k:v for k,v in manifest.items() if k not in {'manifest_hash','import_run_id','status'}}
    if _hash(core) != approved_manifest_hash:
        raise ValueError("Manifest changed after review; generate and approve a new dry run")
    expected_run = f"import:{_slug(manifest['interview_id'])}:{approved_manifest_hash[:16]}"
    if manifest.get('import_run_id') != expected_run:
        raise ValueError("ImportRun identity does not match reviewed manifest")
    driver=graph_store._get_driver()
    with driver.session(database=graph_store._database()) as session:
        return session.execute_write(lambda tx:_tx_commit(tx,manifest))

def load_dataset(path): return json.loads(Path(path).read_text(encoding="utf-8"))

def main(argv=None):
    parser=argparse.ArgumentParser(); parser.add_argument("dataset"); parser.add_argument("--commit",action="store_true"); parser.add_argument("--approve-manifest")
    parser.add_argument("--manifest-json")
    args=parser.parse_args(argv); data=load_dataset(args.dataset); manifest=build_manifest(data)
    print(render_manifest(manifest))
    if args.manifest_json: Path(args.manifest_json).write_text(json.dumps(manifest,indent=2,ensure_ascii=False),encoding="utf-8")
    if args.commit:
        from memory.neo4j_store import store
        receipt=commit_manifest(manifest,args.approve_manifest,store); print("Committed import run:",receipt["id"])
    return 0

if __name__=="__main__": raise SystemExit(main())
