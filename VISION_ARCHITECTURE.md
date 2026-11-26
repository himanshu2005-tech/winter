# VANTA vision architecture

VANTA does not ask one model to do every visual job. Each layer contributes
evidence within its own limits.

## Screen

1. Accessibility/DOM/OCR identify application text and controls.
2. YOLO26s may ground common physical objects when that is relevant.
3. The configured vision LLM interprets UI, text, code, diagrams, charts and
   relationships.
4. The cognitive router chooses `objects`, `vlm`, or `hybrid`. Invalid choices
   fall back safely and the screen is captured only once.

## Camera

1. The camera produces one verified frame receipt.
2. YOLO26s and InsightFace run in parallel.
3. SCRFD-10GF detects and aligns faces.
4. ArcFace (`buffalo_l`, ResNet50@WebFace600K) compares only against templates
   explicitly enrolled by Himanshu.
5. The vision LLM receives the frame plus bounded local evidence and answers
   the original question.

Warm inference on the current Windows machine is approximately 0.4 seconds for
the combined YOLO/face grounding stage. Model initialization is paid by delayed
background warm-up after VANTA starts.

## Identity and graph memory

```
(:Entity {entity_type: "Person"})
  -[:HAS_FACE_TEMPLATE {sensitivity: "restricted"}]->
(:FaceTemplate {model: "buffalo_l", ciphertext: "v1:..."})
```

The embedding is AES-256-GCM encrypted with a random nonce. Its key is stored
in Windows Credential Manager, not Neo4j or `.env`. `FaceTemplate` is neither
an `Entity` nor a `Memory`, so ordinary RAG cannot retrieve it. Recognition can
retrieve a small public/private, reviewed subset of that Person's graph facts;
restricted/sensitive facts and direct contact details are excluded.

Identity decisions require both a similarity threshold and separation from
the runner-up. Ambiguous and low-confidence faces remain unknown. Face matches
are context only: they cannot approve an action, authenticate a user, or prove
that a live person is physically present.

## User commands

- `Remember Madhu's face` — exactly one sufficiently clear face must be in the
  frame. The temporary enrollment image is removed after embedding.
- `Which faces do you know?` — lists people and template counts, never vectors.
- `Forget Madhu's face` — approval-gated permanent template deletion; ordinary
  knowledge about Madhu remains intact.

The public InsightFace `buffalo_l` pretrained weights are licensed for
non-commercial research by default. Replace them with appropriately licensed
models before commercial deployment.
