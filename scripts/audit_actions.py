"""Audit the complete action inventory without invoking provider operations.

Run from the repository root: python -m scripts.audit_actions
--output writes an optional JSON artifact. This checks capability contracts,
not authenticated live execution of every upstream API operation.
"""
import argparse
import ast
import json
from pathlib import Path


def audit():
    import action_attrs
    import action_bus
    import action_protocol
    import action_registry
    import action_schema
    import actions
    import capabilities
    import capability_migration
    import integrity
    import integration_contracts
    import autonomy
    import mode_manager

    # Importing agent opens the microphone at module scope. Read its static
    # catalogs instead: an inventory audit must not start the assistant.
    root = Path(__file__).resolve().parents[1]
    catalogs = {}
    tree = ast.parse((root / 'agent.py').read_text(encoding='utf-8'))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == 'ACTION_CATALOGS'
                for t in node.targets):
            catalogs = ast.literal_eval(node.value)
            break
    if not catalogs:
        raise RuntimeError('Could not read the declared action catalogs')
    catalogs.update(capabilities.catalog_overlay())
    source = (root / 'actions.py').read_text(encoding='utf-8')
    executable = (action_bus.BUS.names() | integrity.executable_names(source)
                  | set(actions.POST_LOOP_ACTIONS) | set(actions.DEFERRED_ACTIONS))
    modes = {p.label.lower(): set(p.blocked_actions) |
             capabilities.mode_blocked_names(p.label)
             for p in mode_manager.POLICIES.values()}
    inventory = capability_migration.build(
        catalogs, schema_specs=action_schema.ACTION_SPECS,
        authorized=autonomy.KNOWN_ACTIONS,
        confirmation_required=autonomy.CONFIRMATION_REQUIRED_ACTIONS,
        proactive_blocked=autonomy.PROACTIVE_BLOCKED_ACTIONS,
        mode_policies=modes, executable=executable)
    action_registry.configure(capability_migration.generated_catalogs(inventory))
    problems = integrity.check(
        catalogs, source, (root / 'mode_manager.py').read_text(encoding='utf-8'),
        set(action_schema.ACTION_SPECS), set(autonomy.KNOWN_ACTIONS),
        executable=executable | set(capabilities.CAPABILITIES),
        indexed={entry['name'] for entry in action_registry._entries})
    verified = set(action_bus.BUS.verified_names())
    records = []
    for name, descriptor in sorted(inventory.items()):
        syntax = descriptor.get('syntax') or ''
        envelopes = action_protocol.extract(syntax)
        error = None
        if envelopes:
            attrs = action_attrs.parse(envelopes[0].attributes)
            attrs['type'] = name
            error = action_schema.validate(name, attrs, has_body=bool(envelopes[0].body))
        record = dict(
            name=name, category=descriptor.get('catalogue'),
            registered_handler=action_bus.BUS.handles(name),
            input_schema=name in action_schema.ACTION_SPECS,
            authorized=descriptor.get('authorized'),
            confirmation=descriptor.get('confirmation'),
            modes=descriptor.get('allowed_modes', descriptor.get('modes')),
            documented_example=bool(envelopes), example_error=error,
            receipt_contract=bool(action_bus.BUS.contract(name)),
            runtime_receipt_verifier=name in verified,
            live_execution_tested=False,
        )
        records.append(record)
    available = (action_bus.BUS.names() | set(actions.POST_LOOP_ACTIONS)
                 | set(actions.DEFERRED_ACTIONS) | set(capabilities.CAPABILITIES))
    return dict(
        action_count=len(records), bus_action_count=len(action_bus.BUS.names()),
        schema_count=sum(row['input_schema'] for row in records),
        example_error_count=sum(bool(row['example_error']) for row in records),
        receipt_contract_count=sum(row['receipt_contract'] for row in records),
        runtime_verifier_count=sum(row['runtime_receipt_verifier'] for row in records),
        integration_gaps=integration_contracts.missing(available),
        integrity_problems=[dict(kind=p.kind, action=p.action, detail=p.detail) for p in problems],
        actions=records,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    report = audit()
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps({key: value for key, value in report.items() if key != 'actions'}, indent=2))
    for row in report['actions']:
        if row['example_error']:
            print(row['name'] + ': ' + row['example_error'])
    return int(bool(report['example_error_count'] or report['integrity_problems'] or report['integration_gaps']))


if __name__ == '__main__':
    raise SystemExit(main())
