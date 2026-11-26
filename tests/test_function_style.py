"""Expose legacy function-style checks to the project's unittest runner.

The repository deliberately uses stdlib unittest, but a handful of modules
were written as bare pytest-style functions. ``unittest discover`` imported
those modules and reported zero tests, silently excluding important runtime,
output, map, route-rendering, and Neo4j checks. This adapter makes every
zero-argument ``test_*`` function a real FunctionTestCase without adding a
second test framework.
"""
from __future__ import annotations

import importlib
import inspect
import unittest


MODULES = (
    "test_conversation_output",
    "test_conversation_runtime",
    "test_maps_api",
    "test_neo4j_integration",
    "test_route_render",
)


def load_tests(_loader, _standard_tests, _pattern):
    suite = unittest.TestSuite()
    for name in MODULES:
        module = importlib.import_module(name)
        reachable = getattr(module, "_neo4j_reachable", None)
        for function_name, function in inspect.getmembers(module, inspect.isfunction):
            if not function_name.startswith("test_"):
                continue
            if inspect.signature(function).parameters:
                raise TypeError(
                    f"{name}.{function_name} needs fixtures and cannot be "
                    "run by the stdlib adapter")

            def run(fn=function, check=reachable):
                if check is not None and not check():
                    raise unittest.SkipTest("Neo4j is not reachable")
                fn()

            run.__name__ = f"{name}.{function_name}"
            suite.addTest(unittest.FunctionTestCase(
                run, description=f"{name}.{function_name}"))
    return suite
