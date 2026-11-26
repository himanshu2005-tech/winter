# Action execution and audit

VANTA chooses capabilities with the existing model, resource selection, action
registry and planner. Execution code owns argument validation, ordering,
cancellation and evidence. No new model or provider is required.

## Execution guarantees

- Discovery preserves complete block examples, including closing tags and code
  newlines, and includes the exact input contract from the execution schema.
- A quote-aware envelope parser preserves source order. Examples nested inside
  a code, document or email body remain payload data.
- Each call receives its own body. Two code revisions in one response cannot
  accidentally execute the first revision twice.
- Legacy alarm operations use the same validated/authorized call list, and file
  saves use the parsed top-level envelopes. Quoted examples cannot bypass the
  input gate by being picked up by a later regex scan. Alarm deletion records
  its own success or failure receipt.
- Mixed sequences run in order, including legacy file saves and alarm actions.
  For example, save-then-read completes the save before attempting the read.
- If a prerequisite fails, awaits approval or returns uncertain evidence, later
  calls in the batch receive `Not Run` receipts. The model can inspect what
  happened and choose a recovery step without executing dependent work blindly.
- Abort between calls prevents later calls from starting. In-flight cancellation
  remains provider-specific; an external request already accepted by a service
  cannot be undone merely by cancelling the local task.
- Empty, malformed, partial and unverified receipts cannot complete outcomes or
  enter the completed-action replay cache. Identical legitimate read results are
  accepted as separate receipts.
- A successful shell command with no stdout returns its actual zero exit status.
- Python execution checks its exit status, supports abort while running, and
  removes temporary scripts on success, failure, timeout or cancellation.
- An unavailable model response has no planner JSON to repair, so no additional
  repair request is sent for an empty response or timeout marker.

## Repeatable checks

```powershell
$env:PYTHONPATH='D:\winter\.venv\Lib\site-packages;D:\winter'
python -m scripts.audit_actions --output reports/action-audit.json
python -m pytest -q tests/test_action_execution_quality.py tests/test_action_contract_matrix.py tests/test_capability_integrity.py tests/test_capability_equivalence.py tests/test_receipt_contracts.py tests/test_reliability_wiring.py tests/test_web_workflow_sequence.py tests/test_complex_task_suite.py
```

The inventory audit reads catalogs from source without importing the assistant
or opening the microphone. It checks every action's schema, callable example,
registration, policy and integration wiring without invoking the actions.
The JSON report distinguishes a documented receipt contract from a runtime
verifier. Neither structural check proves that an external account, camera,
website or provider is currently available.

Scenario tests cover action selection, parsing, execution order, argument errors,
receipts, cancellation and provider boundaries. Tests isolate communications and
consequential operations. Live account operations need separate, task-specific
verification with current credentials and provider availability.
