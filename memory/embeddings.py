"""Local, deterministic embeddings for VANTA's Neo4j vector index."""

from pathlib import Path
import threading

import numpy as np


MODEL_NAME = "all-MiniLM-L6-v2"
DIMENSIONS = 384

# How many texts go through the model at once.
#
# This used to be "all of them". `embed_texts` built one tensor for the whole
# list, and self-attention is quadratic in sequence length and linear in batch:
# at 256 tokens, MiniLM-L6 needs roughly batch x 12 heads x 256 x 256 x 4 bytes
# ~= 3 MB per item just for the attention scores. A vault re-index passes a few
# thousand items in one call, and onnxruntime asked the allocator for
# 10,185,867,264 bytes -- about 3,200 items -- and died:
#
#   onnxruntime ... Failed to allocate memory for requested buffer of size
#   10185867264
#   Exception in thread Thread-84 (index_vault)
#
# The indexer thread died with it, so nothing new was embedded and semantic
# recall silently kept answering from a stale index. 32 bounds the same work to
# about 100 MB and costs nothing in throughput -- the model is the same amount
# of arithmetic either way.
BATCH_SIZE = 32
MIN_BATCH_SIZE = 1
MAX_TOKENS = 256

_lock = threading.Lock()
_session = None
_tokenizer = None


def _is_memory_error(exc):
    """Whether this failure is the allocator giving up, not a bad input."""
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(mark in text for mark in
               ("failed to allocate", "out of memory", "bad_alloc",
                "memoryerror", "allocaterawinternal"))


def _model_dir() -> Path:
    return Path.home() / ".cache" / "chroma" / "onnx_models" / MODEL_NAME / "onnx"


def _load():
    global _session, _tokenizer
    if _session is not None:
        return _session, _tokenizer
    with _lock:
        if _session is not None:
            return _session, _tokenizer
        model_dir = _model_dir()
        model_path = model_dir / "model.onnx"
        if not model_path.exists():
            raise RuntimeError(
                f"Local embedding model is missing at {model_path}. "
                "VANTA will not send private memory to a cloud embedding API."
            )
        import onnxruntime as ort
        from transformers import AutoTokenizer
        _tokenizer = AutoTokenizer.from_pretrained(str(model_dir), local_files_only=True)
        _session = ort.InferenceSession(str(model_path), providers=["CPUExecutionProvider"])
    return _session, _tokenizer


def _embed_batch(session, tokenizer, batch):
    """One forward pass over a bounded group of texts."""
    encoded = tokenizer(batch, padding=True, truncation=True,
                        max_length=MAX_TOKENS, return_tensors="np")
    input_names = {item.name for item in session.get_inputs()}
    ort_inputs = {
        key: value.astype(np.int64)
        for key, value in encoded.items()
        if key in input_names
    }
    hidden = session.run(None, ort_inputs)[0]
    mask = encoded["attention_mask"].astype(np.float32)[..., None]
    pooled = (hidden * mask).sum(axis=1) / np.clip(mask.sum(axis=1), 1e-9, None)
    pooled = pooled / np.clip(
        np.linalg.norm(pooled, axis=1, keepdims=True), 1e-12, None)
    return [row.astype(float).tolist() for row in pooled]


def embed_texts(texts, batch_size=None):
    """Embed any number of texts, in bounded batches.

    The batch halves and retries if the allocator still refuses, down to one
    text at a time, so a machine with less memory than this one gets slower
    rather than getting an exception in a background thread and a vector index
    that quietly stops being updated.
    """
    clean = [str(text or "") for text in texts]
    if not clean:
        return []
    session, tokenizer = _load()
    size = max(MIN_BATCH_SIZE, int(batch_size or BATCH_SIZE))

    vectors = []
    index = 0
    while index < len(clean):
        batch = clean[index:index + size]
        try:
            vectors.extend(_embed_batch(session, tokenizer, batch))
            index += len(batch)
        except Exception as exc:
            if not _is_memory_error(exc) or size <= MIN_BATCH_SIZE:
                # Either a real bug, or one text that cannot be embedded even
                # on its own. Say which text, because "the index failed" with
                # several thousand candidates is not something anyone can act
                # on.
                raise RuntimeError(
                    f"embedding failed at item {index} of {len(clean)} "
                    f"(batch {size}, {len(batch[0])} chars): {exc}") from exc
            size = max(MIN_BATCH_SIZE, size // 2)
            print(f"[Embeddings] Allocator refused a batch; retrying at "
                  f"{size} per batch.")
    return vectors


def embed_text(text):
    return embed_texts([text])[0]
