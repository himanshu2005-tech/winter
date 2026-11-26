import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from conversation_output import (SPOKEN_WORD_LIMIT, action_turn_speech_text,
                                 report_text, bounded_spoken_fallback,
                                 canonical_response_text,
                                 dashboard_response_text,
                                 explicit_spoken_text,
                                 malformed_protocol_response,
                                 naturalize_user_framing,
                                 visible_response_text,
                                 narrates_unexecuted_action, speech_safe_text,
                                 split_speech_artifacts)


def test_speech_never_pronounces_markdown_markers():
    raw = "- **Memory matters**\n- *Cost*: `cheap`\n### Result\nA * B | C"
    spoken = speech_safe_text(raw)
    assert spoken == "Memory matters Cost: cheap Result A times B , C"
    for marker in ("*", "`", "#", "|"):
        assert marker not in spoken


def test_speech_keeps_link_labels_and_drops_code_blocks():
    raw = "Read [the guide](https://example.com).\n```python\nprint('*')\n```"
    assert speech_safe_text(raw) == "Read the guide."


def test_machine_readable_details_are_displayed_not_spoken():
    spoken, artifacts = split_speech_artifacts(
        "Your repository is https://github.com/example/project.")
    assert spoken == "Your repository is the link shown in Details."
    assert artifacts == [{"kind": "link", "value":
                          "https://github.com/example/project"}]


def test_markdown_link_keeps_human_label_for_speech():
    spoken, artifacts = split_speech_artifacts(
        "Open [your repository](https://github.com/example/project).")
    assert spoken == "Open your repository."
    assert artifacts[0]["value"] == "https://github.com/example/project"


def test_phone_and_spaced_path_are_details_not_speech():
    spoken, artifacts = split_speech_artifacts(
        'Call +91 98765 43210; the file is "D:\\My Files\\report.pdf".')
    assert "+91 98765 43210" not in spoken
    assert "D:\\My Files\\report.pdf" not in spoken
    assert {item["kind"] for item in artifacts} == {"phone", "path"}


def test_protocol_scratch_is_rejected_before_it_can_become_silence():
    assert malformed_protocol_response("<action? can't mix case? Use exact XML.")
    assert malformed_protocol_response("< S? > no, action.")
    assert not malformed_protocol_response("Google Borg traces are workload datasets.")
    assert not malformed_protocol_response('<ACTION type="SEARCH_WEB" query="Borg" />')
    assert not malformed_protocol_response('<SAY>Please repeat that.</SAY><DONE />')

def run():
    # An action turn used to send the spoken line to the dashboard and drop the
    # REPORT, so VANTA would say "too long to read out, it is on the
    # dashboard" and put nothing there. The detail lived only in the console.
    action_turn = (
        "<SAY>Opening the portal now.</SAY>"
        "<ACTION type=\"BROWSER_GOTO\" url=\"https://x.com\" />"
        "<REPORT>Found 12 listings, deadline 15 Oct.</REPORT>"
        "<SAY>Twelve listings, details on the dashboard.</SAY>"
    )
    assert report_text(action_turn) == "Found 12 listings, deadline 15 Oct."
    # Speech still stops at the first ACTION, so TTS never reads the report.
    assert action_turn_speech_text(action_turn) == "Opening the portal now."
    # No report means no detail, rather than the whole cleaned response.
    assert report_text('<SAY>hi</SAY><ACTION type=\"X\" />') == ""
    assert report_text("") == ""
    # Several reports in one turn are joined, not just the first.
    two = "<REPORT>first</REPORT><REPORT>second</REPORT>"
    assert report_text(two) == "first\n\nsecond"

    response=("Sir, based on our interactions, you are a final-year student.\n\n"
              "<SAY>Is there anything else you would like to know, sir?</SAY>")
    visible=visible_response_text(response)
    assert "final-year student" in visible
    assert "anything else" in visible
    assert "<SAY>" not in visible
    assert visible_response_text("Working now. <ACTION type=\READ_EMAILS\ query=\is:unread\ />") == "Working now."
    assert narrates_unexecuted_action("Sir, I am now retrieving your unread emails.")
    assert narrates_unexecuted_action("Sir, I will investigate this opportunity further.")
    assert narrates_unexecuted_action("I'll look into the job alert for you.")
    assert narrates_unexecuted_action("I will now process them and identify the important ones.")
    assert narrates_unexecuted_action("I'll keep that in mind.")
    assert narrates_unexecuted_action("I will remember that.")
    malformed='[Contacts] synced. <ACTION type="READ_EMAILS" query="subject:'
    assert action_turn_speech_text(malformed) == ""
    sequenced=("<SAY>Take a slow breath.</SAY>"
               "<ACTION type=\"SPOTIFY_PLAY\" query=\"ambient\" />"
               "<SAY>The music is playing.</SAY>")
    assert action_turn_speech_text(sequenced) == "Take a slow breath."
    assert "<ACTION" not in visible_response_text(malformed)
    assert not narrates_unexecuted_action(response)
    duplicated=("Sir, I found several job-related emails. Would you like me to investigate further? "
                "<SAY>Sir, I found a few job opportunities. Would you like me to look into them?</SAY>")
    rendered=canonical_response_text(duplicated)
    assert rendered == "Sir, I found several job-related emails. Would you like me to investigate further?"
    assert rendered.count("Would you like") == 1
    status=("Sir, I reviewed your system and found two useful emails. "
            "<SAY>Sir, your systems are nominal and two emails warrant attention.</SAY>")
    assert canonical_response_text(status) == "Sir, I reviewed your system and found two useful emails."
    separated=(
        "<REPORT>Detailed email analysis. First message has billing impact. "
        "Second message is promotional. Open the billing console before Friday.</REPORT>"
        "<SAY>One billing email needs attention before Friday; the other is promotional.</SAY>"
    )
    assert dashboard_response_text(separated).startswith("Detailed email analysis.")
    assert "promotional.</SAY>" not in dashboard_response_text(separated)
    assert explicit_spoken_text(separated) == (
        "One billing email needs attention before Friday; the other is promotional."
    )
    two_say = "<SAY>First update.</SAY><SAY>Second update.</SAY>"
    assert explicit_spoken_text(two_say) == "First update.\n\nSecond update."
    long_report=" ".join(f"word{i}" for i in range(100))
    fallback=bounded_spoken_fallback(long_report)
    assert len(fallback.split()) == SPOKEN_WORD_LIMIT
    assert fallback.endswith(".")
    assert dashboard_response_text(f"<REPORT>{long_report}</REPORT>") == long_report
    assert naturalize_user_framing("The user is likely studying. The user's chart is open.") == (
        "You are likely studying. Your chart is open."
    )
    assert "the user" not in dashboard_response_text(
        "<REPORT>The user wants a conclusion from the chart.</REPORT>"
    ).lower()
    print("Conversation output checks passed")

if __name__ == "__main__": run()
