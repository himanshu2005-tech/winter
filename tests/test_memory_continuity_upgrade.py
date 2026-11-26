"""Full evidence and bounded chat must be separate; no live graph writes."""
from unittest.mock import Mock, patch
import session_context
from conversation_output import narrates_unexecuted_action


def test_long_user_turn_is_preserved_as_evidence_with_a_bounded_chat_excerpt():
    source = 'First preference.\n' + 'Detailed explanation. ' * 160 + '\nFinal fact: I speak Telugu.'
    with patch.object(session_context, '_graph_store', Mock()) as graph, \
         patch.object(session_context, '_turns', __import__('collections').deque(maxlen=5)):
        session_context.record_user(source)
        assert graph.upsert_dialogue_turn.call_args.args[3] == source
        assert len(session_context._turns[-1]['user']) <= 1500
        assert 'I speak Telugu' in session_context._turns[-1]['user']


def test_full_assistant_answer_keeps_its_last_question_in_followup_context():
    from collections import deque
    turns = deque(maxlen=5)
    answer = 'Here is the full explanation. ' * 150 + ' Shall we build the project together?'
    with patch.object(session_context, '_graph_store', Mock()) as graph, \
         patch.object(session_context, 'save_conversation'), \
         patch.object(session_context, '_turns', turns):
        session_context.record_user('Help me decide what to build')
        session_context.record_assistant(answer)
        assert graph.upsert_dialogue_turn.call_args.args[3] == answer
        session_context.record_user('do it')
        context = session_context.contextualize_user_input('do it')
        assert 'Shall we build the project together?' in context
        assert len(turns[0]['assistant']) <= 1500


def test_quoted_memory_example_is_not_a_claim_of_an_executed_action():
    response = 'You can mention facts naturally, such as "remember that I prefer examples". What would help you most?'
    assert not narrates_unexecuted_action(response)
    assert not narrates_unexecuted_action('What should I remember about your learning preferences?')
    assert narrates_unexecuted_action("I'll remember that.")
    assert narrates_unexecuted_action('I will now search your files.')


def test_configured_stronger_model_is_kept_separate_from_airouter():
    import os
    import llm_router
    from providers import chatgpt_auth
    with patch.dict(os.environ, {'CHATGPT_MODEL': 'gpt-6.1-sol', 'CHATGPT_MODEL_ROUTER': '',
                                 'WINTER_MODEL_ROUTER': 'zai/glm-5.3-flash'}), \
         patch.object(chatgpt_auth, 'list_models', return_value=[{'slug': 'gpt-6.1-sol'}]):
        assert chatgpt_auth.preferred_model() == 'gpt-6.1-sol'
        assert llm_router._model_for_provider('chatgpt', 'cognitive_router') == 'gpt-6.1-sol'
