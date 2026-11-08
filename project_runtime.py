# -*- coding: utf-8 -*-
"""Persistent executable-project adapters.

The action layer talks to this module, never directly to Blender or a future
runtime.  An adapter owns how code is applied, how the live project is opened,
and what evidence proves its current state.  Adding another project type is a
registration, not another branch in the agent loop.
"""

import importlib
import threading


class ProjectRuntimeError(Exception):
    pass


_lock = threading.RLock()
_adapters = {"blender": "blender_projects"}


def register(name, module):
    key = str(name or "").strip().casefold()
    if not key or not module:
        raise ValueError("adapter name and module are required")
    with _lock:
        _adapters[key] = module


def adapters():
    with _lock:
        return tuple(sorted(_adapters))


def _load(name):
    key = str(name or "").strip().casefold()
    with _lock:
        module_name = _adapters.get(key)
    if not module_name:
        raise ProjectRuntimeError(
            f"Unknown project adapter '{name}'. Available: "
            + ", ".join(adapters()))
    module = importlib.import_module(module_name)
    for method in ("apply", "status", "open_project"):
        if not callable(getattr(module, method, None)):
            raise ProjectRuntimeError(
                f"Adapter '{key}' does not implement {method}().")
    return module


def apply(base_dir, adapter, project, code, instruction="", timeout=120,
          open_after=True, interrupt_event=None):
    return _load(adapter).apply(
        base_dir, project, code, instruction=instruction, timeout=timeout,
        open_after=open_after, interrupt_event=interrupt_event)


def status(base_dir, adapter, project, timeout=30):
    return _load(adapter).status(base_dir, project, timeout=timeout)


def open_project(base_dir, adapter, project, timeout=30):
    return _load(adapter).open_project(base_dir, project, timeout=timeout)


def present(base_dir, adapter, project, enabled=True, timeout=30):
    runtime = _load(adapter)
    method = getattr(runtime, "present", None)
    if not callable(method):
        raise ProjectRuntimeError(
            f"Adapter '{adapter}' does not support presentation mode.")
    return method(base_dir, project, enabled=enabled, timeout=timeout)
