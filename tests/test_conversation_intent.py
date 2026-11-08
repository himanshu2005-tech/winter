import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from conversation_intent import is_social_acknowledgement


def run():
    assert is_social_acknowledgement("Thanks vanta")
    assert is_social_acknowledgement("Okay, thank you!")
    assert not is_social_acknowledgement("Thanks, now scan D:\\Outrun")
    assert not is_social_acknowledgement("Please scan my Outrun folder")
    print("Conversation intent checks passed")


if __name__ == "__main__": run()
