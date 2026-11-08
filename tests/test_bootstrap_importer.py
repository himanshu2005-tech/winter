import copy,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from autonomy import action_allowed,confirmation_required
from memory.bootstrap_importer import build_manifest,normalize_dataset,plan_claim_operation,validate_dataset
from memory.orchestrator import _filter_facts,_format_graph_groups
from memory import bootstrap_importer

def synthetic():
    return {"schema_version":"vanta-bootstrap-interview-v1","profile_version":"test-v1",
      "interview_id":"synthetic-test","subject_id":"test-person","source":"explicit_user_interview",
      "collected_at":"2026-01-01T00:00:00Z","expected_answer_count":3,
      "answers":[
        {"question_id":"q001","question_text":"What learning style do you prefer?","answer_type":"multi_select","raw_answer":"Examples and building","selected_options":["examples","building"],"user_notes":None},
        {"question_id":"q002","question_text":"What requires confirmation?","answer_type":"single_select","raw_answer":"send communication","selected_options":["send communication"],"user_notes":None},
        {"question_id":"q003","question_text":"What stress pattern do you notice?","answer_type":"free_text","raw_answer":"I overthink","selected_options":[],"user_notes":None}],
      "generated_views":{"natural_language_profile":"Synthetic only","previous_structured_summary":{}}}

def run():
    data=synthetic(); assert validate_dataset(data)==[]
    m1=build_manifest(data); m2=build_manifest(copy.deepcopy(data))
    assert m1["manifest_hash"]==m2["manifest_hash"]
    evidence,facts,profile=normalize_dataset(data)
    assert len(evidence)==3 and all(not x["semantic_retrievable"] for x in evidence)
    assert profile["status"]=="active" and profile["semantic_retrievable"]
    changed=copy.deepcopy(data); changed["answers"][0]["raw_answer"]="different"
    assert build_manifest(changed)["manifest_hash"]!=m1["manifest_hash"]
    assert evidence[0]["id"]==normalize_dataset(changed)[0][0]["id"]
    assert evidence[0]["content_hash"]!=normalize_dataset(changed)[0][0]["content_hash"]

    current=[{"fact_id":"old-a","valid_to":None},{"fact_id":"old-b","valid_to":None}]
    assert len(plan_claim_operation(current,[{"fact_id":"new"}],"replace_single")["expire"])==2
    assert len(plan_claim_operation(current,[{"fact_id":"new"}],"add_value")["expire"])==0
    assert len(plan_claim_operation(current,[],"remove_value",["old-a"])["expire"])==1
    assert len(plan_claim_operation(current,[],"suspend_value",["old-b"])["expire"])==1
    assert len(plan_claim_operation(current,[{"fact_id":"n1"},{"fact_id":"n2"}],"replace_set")["create"])==2
    updated=plan_claim_operation(current,[{"fact_id":"new","confidence":.8}],"confidence_update",["old-a"])
    assert updated["create"][0]["supersedes_fact_id"]=="old-a"

    rows=[
      {"subject":"x","predicate":"P","object":"public","fact_class":"explicit_user_fact","sensitivity":"public","review_status":"accepted"},
      {"subject":"x","predicate":"P","object":"restricted","fact_class":"explicit_user_fact","sensitivity":"restricted","review_status":"accepted"},
      {"subject":"x","predicate":"P","object":"pending","fact_class":"derived_inference","sensitivity":"private","review_status":"pending_confirmation"},
      {"subject":"x","predicate":"P","object":"legacy","confidence":1.0},]
    normal=_filter_facts(rows,"general planning","user")
    assert {r["object"] for r in normal}=={"public","legacy"}
    possible=_filter_facts(rows,"what possible pattern might exist","user")
    assert "pending" in {r["object"] for r in possible}
    proactive=_filter_facts(rows,"medical status","proactive")
    assert "restricted" not in {r["object"] for r in proactive}
    text=_format_graph_groups(possible); assert "UNCONFIRMED INFERENCES" in text

    assert not action_allowed("MADE_UP_ACTION","user",memory_permits=True)[0]
    assert not action_allowed("SEND_EMAIL","proactive",memory_permits=True)[0]
    assert confirmation_required("SEND_EMAIL")
    assert action_allowed("READ_EMAILS","user",memory_permits=False)[0]
    import inspect
    assert "ON CREATE SET r += $props" in inspect.getsource(bootstrap_importer._tx_commit)
    print("Bootstrap importer checks passed")

if __name__=="__main__": run()
