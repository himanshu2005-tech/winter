import stt


def test_failed_gate_can_be_rearmed_without_restarting_process():
    sentinel = object()
    old = (stt._gate_model, stt._gate_failed, stt._gate_failed_at)
    try:
        stt._gate_model = sentinel
        stt._gate_failed = True
        stt._gate_failed_at = 123.0
        stt.retry_gate_after_failure()
        assert stt._gate_model is None
        assert stt._gate_failed is False
        assert stt._gate_failed_at == 0.0
    finally:
        stt._gate_model, stt._gate_failed, stt._gate_failed_at = old
