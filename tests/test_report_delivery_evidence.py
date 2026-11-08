import json

import pytest

import critic
from planner import Plan
from report_evidence import prepare
import report_html


REPORT='# Synthetic report\n\n| Phase | Result |\n|---|---|\n| A | Complete |\n\n```mermaid\ntimeline\n title Example lifecycle\n Phase 1 : First event\n Phase 2 : Second event\n```\n\n'+('Useful context. '*200)+'\nFinal section with uncertainty notes.'


def test_prepared_report_is_code_compiled_content_evidence_not_a_file_claim():
    receipt=prepare(REPORT,request='Show me a report on this topic')
    assert receipt['ok'] and receipt['type']=='REPORT_PREPARED'
    assert receipt['report_artifact']['counts']['tables']==1
    assert receipt['report_artifact']['counts']['diagrams']==1
    assert receipt['report_artifact']['state']=='prepared'
    assert 'not a file export' in receipt['output']


def test_critic_sees_full_report_and_original_request_without_bypassing_fact_review():
    plan=Plan({'goal':'Research and report','steps':['Read sources','Compose report'],
               'deliverable':'An in-app report','success_criteria':['A report with a timeline']})
    seen=[]
    def reviewer(messages,**kwargs):
        seen.append(messages[1]['content'])
        return json.dumps({'verdict':'fail','reason':'Still missing primary-source verification','missing':['Primary source'],'next_action':'Read the source'})
    verdict=critic.review(plan,[prepare(REPORT,request='Report this topic; no export requested')],REPORT,reviewer)
    assert verdict.failed
    assert 'Final section with uncertainty notes.' in seen[0]
    assert 'ORIGINAL USER REQUEST: Report this topic; no export requested' in seen[0]
    assert 'does not need a .md' in seen[0] and 'no factual corroboration' in seen[0]


def test_invalid_visual_cannot_receive_prepared_success_evidence():
    with pytest.raises(ValueError):
        prepare('```scene3d\n{"objects":[{"type":"unsupported"}]}\n```')


def test_timeline_is_a_rendered_diagram_with_escaped_content():
    result=report_html.render_timeline('timeline\n title <script>\n 2000 : <script>alert(1)</script>\n : Another event')
    assert '<svg' in result and '<script>' not in result and '&lt;script&gt;' in result
    assert report_html.render_timeline('timeline\n Invalid row') is None
