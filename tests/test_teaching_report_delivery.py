"""Teaching content must reach the reading window, not just speech."""
from types import SimpleNamespace
from unittest.mock import patch

from conversation_output import report_text, dashboard_response_text, wants_report_surface
from report_stream import ReportStream, report_prefix

LESSON = '''## Cross-attention
The decoder builds queries and attends to keys and values from the encoder.
Those representations provide context from the input sequence. Attention
weights depend on the example, the layer and the head; they are not fixed
word-to-word translation rules. A context vector is a weighted combination
of the values, not a direct lookup of a translated word.

```mermaid
graph LR
  Q[Decoder queries] --> A[Attention]
  K[Encoder keys] --> A
  V[Encoder values] --> A
```
'''


def test_legacy_tutor_envelope_is_visible_report_content():
    response = '<SAY>Here is the lesson.</SAY><TUTOR>' + LESSON + '</TUTOR>'
    assert report_text(response) == LESSON.strip()
    assert dashboard_response_text(response) == LESSON.strip()
    assert wants_report_surface(response)


def test_model_selected_teaching_recovers_unwrapped_structured_lesson():
    assert wants_report_surface(LESSON, catalogs=['TUTOR'])
    assert wants_report_surface(LESSON, catalogs=[])
    assert not wants_report_surface('Ready', catalogs=['TUTOR'])
    assert not wants_report_surface('Switching to study mode.', catalogs=['TUTOR'])


def test_legacy_teaching_streams_without_speech_or_control_markup():
    events = []
    stream = ReportStream(events.append)
    response = '<TUTOR>' + LESSON + '</TUTOR>'
    stream.feed(response)
    stream.finish(response)
    assert report_prefix(response) == (LESSON, True)
    assert events and all(event[0] == 'report_stream' for event in events)
    assert all('<TUTOR>' not in event[1]['content'] for event in events)


def test_tutor_in_reasoning_or_an_example_is_not_streamed():
    assert report_prefix('<think><TUTOR>Not public</TUTOR></think>') == ('', False)
    assert report_prefix('```xml\n<TUTOR>Example</TUTOR>\n```') == ('', False)


def test_clarification_confirmation_uses_existing_mode_controller():
    import agent
    with patch('mode_manager.pending_mode_proposal', return_value='study'), \
         patch('mode_manager.current_mode', return_value=SimpleNamespace(value='study')), \
         patch.object(agent, '_handle_mode_command', return_value=True) as control:
        assert agent._handle_clarification_mode_answer('yes') == 'study'
        control.assert_called_once_with('yes', source='clarification')
    with patch('mode_manager.pending_mode_proposal', return_value=None), \
         patch.object(agent, '_handle_mode_command') as control:
        assert agent._handle_clarification_mode_answer('yes') is None
        control.assert_not_called()
