"""Invoke an injected model callable once, including older narrow test adapters."""
import inspect


def invoke(call, messages, **options):
    try:
        parameters = inspect.signature(call).parameters
    except (TypeError, ValueError):
        return call(messages, **options)
    if not any(p.kind == inspect.Parameter.VAR_KEYWORD for p in parameters.values()):
        options = {name: value for name, value in options.items() if name in parameters}
    return call(messages, **options)
