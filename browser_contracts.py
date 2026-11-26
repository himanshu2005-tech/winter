"""Shared browser-boundary contracts, independent of any site or model."""
import time
import queue
from contextlib import contextmanager
from contextvars import ContextVar


_interrupt = ContextVar('browser_interrupt', default=None)


@contextmanager
def execution_scope(interrupt_event):
    token = _interrupt.set(interrupt_event)
    try:
        yield
    finally:
        _interrupt.reset(token)


def current_interrupt():
    return _interrupt.get()


def wait_for_reply(reply, waiting, args):
    deadline = time.monotonic() + waiting
    while True:
        event = args.get('_interrupt_event')
        if event is not None and event.is_set():
            raise InterruptedError('Browser request cancelled; inspect the page before retrying any submitted operation')
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise queue.Empty
        try:
            return reply.get(timeout=min(0.1, remaining))
        except queue.Empty:
            continue


STABLE_REFERENCES_JS = r"""
const referenceFor = el => {
  const key = Symbol.for('vanta.element.references');
  const owner = el.ownerDocument.defaultView;
  if (!owner[key]) owner[key] = {
    prefix: 'e' + Array.from(crypto.getRandomValues(new Uint8Array(6)),
      b => b.toString(16).padStart(2, '0')).join('') + '-',
    next: 1, elements: new WeakMap()
  };
  const state = owner[key];
  if (!state.elements.has(el)) state.elements.set(el, state.prefix + state.next++);
  const ref = state.elements.get(el);
  el.setAttribute('data-vanta-ref', ref);
  return ref;
};
"""

CLICK_OBSERVER_JS = r"""el => {
  const key = Symbol.for('vanta.click.observer');
  if (!el[key]) {
    el[key] = {count: 0};
    el.addEventListener('click', () => el[key].count++, true);
  }
  return el[key].count;
}"""
CLICK_COUNT_JS = r"""el => {
  const state = el[Symbol.for('vanta.click.observer')];
  return el.isConnected && state ? state.count : null;
}"""

# No entered values: receipts expose field names and validation feedback only.
VALIDATION_JS = r"""(target, markedOnly = false) => {
  let root = target && (target.form || target.closest('form'));
  if (target && !root) return [];
  root = root || document;
  return Array.from(root.querySelectorAll('input,select,textarea,[aria-invalid="true"]'))
    .filter(el => !el.disabled &&
      ((!markedOnly && el.willValidate && el.validity && !el.validity.valid) ||
       el.getAttribute('aria-invalid') === 'true'))
    .slice(0, 30).map(el => ({
      field: el.getAttribute('aria-label') || el.name || el.id || el.tagName.toLowerCase(),
      message: el.validationMessage || 'The application marks this field invalid'
    }));
}"""


def check_deadline(args):
    """A caller that gave up must not leave an unstarted mutation in the queue."""
    event = args.get('_interrupt_event')
    if event is not None and event.is_set():
        raise InterruptedError('Browser request cancelled; no further steps were started')
    deadline = args.get('_request_deadline')
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError(
            'Request expired before this step could run; no further steps were '
            'started. Inspect BROWSER_SNAPSHOT before retrying.')
