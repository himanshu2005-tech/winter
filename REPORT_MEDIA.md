# Rich reports

The existing `REPORT` and `RESULT_WINDOW` surfaces now support images and
interactive 3D alongside tables, formulas, diagrams, charts, code and action
buttons. No extra model, JavaScript package, CDN or Blender installation is
needed for the report viewer. Model-generated content remains data; arbitrary
HTML, scripts and Python are not executed by a report.

Use a Markdown report or a JSON rich document with a `blocks` array.
Structured blocks use `type: image` or `type: scene3d` with the same fields as
the fenced examples below. These formats are included in the model's action
catalogue and presentation resource description.

## Images

```image
{"path":"D:/winter/vault/example.png","caption":"An existing local image"}
```

Local PNG, JPEG, WebP and other Pillow-readable raster images are decoded,
resized to a maximum 2000×2000 bounding box and embedded in the report.
The viewer does not require a separate local file server. Input limits are
8 MB per file and 25 megapixels; broken or missing files show a visible error.
Raster base64 data URLs and standalone `![Caption](path)` Markdown also work.

An explicit `src: https://...` loads a remote image in the browser without a
referrer. It requires network access and reveals the client IP to that image
host; prefer local files for private work. The Python report server does not
fetch remote URLs. HTTP URLs, credential-bearing URLs, SVG data and arbitrary
file-serving routes are not supported. An unavailable remote image displays
an error rather than leaving an empty area.

## Interactive 3D

```scene3d
{"title":"A small neural connection","objects":[
  {"type":"sphere","position":[-1,0,0],"radius":0.4,"label":"Input","color":"#75baff"},
  {"type":"sphere","position":[1,0,0],"radius":0.4,"label":"Output","color":"#9adab5"},
  {"type":"line","points":[[-1,0,0],[1,0,0]],"color":"#ffffff"}
]}
```

Drag or use arrow keys to rotate; scroll or use `+`/`-` to zoom. Reset view
and wireframe controls are available. Drawing runs only after interaction or
resize, with no continuous rendering loop. Reports release resize observers
when their content is replaced.

Supported objects:

- `box`: `position` and positive three-number `size` (defaults to `[1,1,1]`).
- `sphere`: `position` and positive `radius` (default `0.5`).
- `line` / `polyline`: `points`, each an `[x,y,z]` coordinate.
- `mesh`: `vertices` and polygon `faces` using zero-based indices, or `path`
  pointing to an existing OBJ file smaller than 1 MB. OBJ vertex/face geometry
  supports positive and negative indices. Materials and textures are omitted.
- `ellipsoid`: positive XYZ `radii`.
- `cylinder` / `cone`: positive `radius`, `height`, optional `segments` (3–64).
  Cylinders also accept `radiusTop` / `radiusBottom`; the main axis is Y.
- `torus`: `radius`, smaller positive `tube`, optional `segments` (3–64) and
  `tube_segments` (3–32). The ring lies in the XZ plane.
- `plane`: positive `width` and `depth`, lying in the XZ plane.
- `group`: nonempty `children`, inheriting static transforms and optional
  color; at most six nested levels. Groups count toward the 128-object limit.

Static `position`, `rotation` (XYZ degrees) and positive `scale` vectors apply
to all shapes. Child transforms apply first, then parent groups. Animate child
geometry rather than group transforms. `cube` / `cuboid` and common Geometry
type names are accepted as geometric aliases. Semantic names such as “drone”
belong in `label` or `title`: the model must compose their geometry from parts
or provide an actual mesh. There is no special-case drone renderer.

The supported contract is generated from `report_geometry.contract()` and
inserted into the model's report catalogues. Explicit report actions validate
3D blocks before displaying or recording a success receipt; failures return
the offending type and contract to the model. Final REPORT answers have a
bounded repair path. Invalid geometry is held out of streaming previews.

All objects accept a six-digit hex `color` and optional `label`. Positions
translate object geometry. The viewer auto-centers and fits the scene. Limits:
128 objects, 8000 total vertices, 12000 faces, 64 indices per face, finite
coordinates within ±1e9. Invalid geometry shows a visible error.

This is an orthographic educational geometry viewer with depth-sorted faces,
not a full CAD/Blender engine. Intersecting or transparent complex meshes can
have occlusion artifacts. Blend, GLTF, animation, texture and material editing
remain build-tool tasks; they are not silently claimed as supported here.

## Existing visual formats

Markdown tables remain sortable. Display math uses Matplotlib mathtext
rendered to SVG, not a complete TeX installation. Diagrams support the existing
flowchart, sequence and pie grammar; unsupported Mermaid grammars remain
readable code instead of being misrepresented as rendered diagrams.
Formula SVGs have transparent backgrounds and theme-aware contrast; rendered
equations do not repeat their raw source as captions. Print styling uses dark
equations and text on a light page. Literal source remains in each image's alt
text, and unsupported formula syntax still has a readable source fallback.

## Progressive writing and explorable concepts

An explicit `REPORT` streams as the model generates it. Markdown bodies in
`RESULT_WINDOW action="show"` can also stream; structured JSON waits until it
can be parsed. Ordinary chat and background work do not start a report preview.
The UI shows `Writing · draft`, then `Draft · checking`. The existing final
delivery/verification path replaces that status. A partial draft is never an
execution receipt. Interrupted generation retains an explicitly interrupted
draft; provider fallback starts a separate draft instead of splicing answers.

Updates are coalesced to at most one preview per 400 ms. Text and ordinary code
can appear midway through a code fence. JSON visual blocks, diagrams and display
math wait for their closing fence so the UI does not compile broken fragments.
The page preserves unchanged visuals, 3D interaction state and the reader's
scroll position. When the reader is already at the bottom it follows new text.
Expensive completed visuals are cached only within the current stream, bounded
to eight entries of at most 2 MB each (including their cache key).

Use `[[attention]]` to mark a concept as explorable. Clicking it sends a focused
explanation request, the report title and a bounded nearby source excerpt to
the ordinary VANTA input queue. The resulting answer can be another report.
No extra model request is made until the user clicks. Authentication and an
exact match against the current report's offered terms are required; stale or
invented terms are rejected. Inline code does not become a clickable concept.

## Animation and visual walkthroughs

Animations start **paused**. Play/pause, scrub and restart are available. Playback
stops when the report/tab becomes hidden, the visual leaves the viewport, or the
report is replaced; listeners/observers and animation frames are cleaned up.

A 3D object accepts `animation` as one track or an array of up to four tracks:

```json
{
  "type": "box",
  "animation": {
    "property": "rotation",
    "keyframes": [
      {"time": 0, "value": [0, 0, 0]},
      {"time": 4, "value": [0, 360, 0]}
    ]
  }
}
```

Properties are `position` (translation relative to static geometry), `rotation`
(XYZ degrees about the object's center), `scale` (positive XYZ vector) and
`opacity` (0–1). Tracks interpolate linearly, hold endpoint values, and have
2–64 keyframes with strictly increasing times between 0 and 300 seconds.
The view includes the translation/scale envelope so movement stays visible.

A fenced `animation` JSON block (or structured `type: animation`) can combine
existing visuals into a playable educational sequence:

```animation
{"title":"How data flows","frames":[
  {"caption":"Observe","duration":2,"blocks":[
    {"type":"markdown","text":"Start with the [[input]]."}]},
  {"caption":"Transform","duration":3,"blocks":[
    {"type":"diagram","source":"graph LR\n A[Input] --> B[Transform] --> C[Output]"}]
  }
]}
```

Limits are 20 frames, 12 existing visual blocks per frame, 300 total seconds and
4 MB rendered content. Nested animation sequences and action blocks inside
frames are not accepted. This supports animated models and visual lessons;
arbitrary applications, executable UI code, physics engines and every possible
media format are not implied by this grammar. New renderers can extend it.

## Questionnaires and self-study quizzes

Use a fenced `questionnaire` JSON object or a structured block with
`type: questionnaire`. Model catalogues and discovery describe this renderer.

```questionnaire
{"title":"Plan your study session","layout":"stepper","questions":[
  {"id":"pace","type":"single_choice","label":"What pace suits you?",
   "options":[{"id":"guided","label":"Guided practice"},{"id":"deep","label":"Deep dive"}]},
  {"id":"hours","type":"rating","label":"Hours available","min":1,"max":5},
  {"id":"notes","type":"textarea","label":"Anything to focus on?","required":false}
]}
```

Available inputs: `single_choice`, `multiple_choice`, `text`, `textarea`,
`number`, `rating` and `date`. Question IDs are unique; option IDs are unique
within their question. The default is a stepper with Back/Next and answered
progress; `layout: all` displays the entire questionnaire. Required answers,
choice membership, dates, numeric bounds/steps and text lengths are validated
again on the server. No options or user answers are preselected.

The user reviews the collected answers and explicitly selects **Submit
answers** before anything enters VANTA's normal conversation queue. These are
answer data, never direct tool commands or permission grants. Memory retention
uses the existing conversation policy; rendering a form alone does not write
facts or import a bootstrap profile.

Completed forms survive unchanged streaming updates. Browser-tab
`sessionStorage` keeps unfinished answers across reloads under the current
report token and exact schema hash. This is browser session storage, not a
claim of encrypted or disk-free storage. Closing the tab/session or starting
VANTA with a new token is not a permanent draft archive.

The server binds submissions to its current rendered schema; posted schema
definitions are ignored. Submission IDs are idempotent, including retries
after a lost acknowledgement and subsequent report replacement. The last
64 accepted receipts are kept in memory per report-server process. Different
answers with a reused submission ID are refused. A failed queue delivery
remains retryable, and the UI keeps entered answers on failure.

With `mode: quiz`, choice/numeric questions may supply `correct` (an option ID,
option-ID array or number) and optional `explanation`. The receipt shows an
**answer-key score** and explanations. Open text is sent to VANTA for feedback
and is not automatically marked right/wrong. This is self-study, not a secure
exam system: the report/source includes the supplied answer key.

Limits: 100 questions, 20 options per choice question, 4000 characters per text
field, and 24000 characters across the normalized answer JSON. Questionnaires
are standalone blocks, not nested inside animated frames. No extra model call
is made for selecting options, navigating or reviewing; submitting queues the
normal VANTA conversation response.

## Interactive concept explorers

An `explorer` is a live mathematical/diagram view, not a picture of a chart.
Sliders change computed curves, vectors, shapes and displayed values locally;
they do not spend model calls. For example:

```explorer
{"title":"Change the curvature",
 "parameters":[{"id":"a","label":"Curvature a","min":0,"max":4,"step":0.1,"value":1}],
 "plot":{"x_range":[-3,3],"y_range":[0,12],"x_label":"x","y_label":"f(x)"},
 "curves":[{"label":"f(x) = ax²","expression":"a*x**2"}],
 "outputs":[{"label":"Second derivative","expression":"2*a"}]}
```

An explorer can have eight parameters, eight curves, 60 shapes and 12 numeric
outputs. Shape types are point/label (`x,y`), circle (`x,y,radius`), line/arrow
(`x,y,x2,y2`) and rect (`x,y,width,height`). Numeric shape coordinates may be
expressions in parameter IDs and `t`; curves additionally use `x`. Turning
`plot.axes` off creates a clean diagram stage. An optional `duration` up to
300 seconds adds paused playback and a time variable `t`, enabling waves,
moving vectors and other equation-driven illustrations.

Expressions support arithmetic, comparisons, a Python-style conditional and
the documented elementary math functions. Python AST validation compiles them
to an arithmetic data tree. The browser interprets that tree; it never uses
eval/new Function or executes Python. Expressions are capped at 240 characters,
80 syntax nodes and 12 levels. Parameters/plot bounds/constants must be finite
and within ±1e6; plot spans must be at least 1e-9. Rescale axes/units for much
smaller or larger systems. Values outside the mathematical domain or finite
±1e12 display range are omitted and explicitly labelled undefined. Curves are
sampled illustrations, not validated physical simulations or numerical proofs.

Default colors follow the report theme. Theme changes and resizes redraw the
canvas. Playback pauses offscreen/in background and releases its observers
when replaced. Streaming updates preserve unchanged views and slider values.

## One concept, complementary views

Use a `lesson` block with `layout: tabs`, `accordion` or `flashcards` and
`sections: [{title, blocks: [...]}]`. Sections compose existing renderers,
including equations, images, 3D, explorers, diagrams, tables and questionnaires.
This allows an intuition/math/explore/code/check sequence without opening
separate windows. Tabs support arrow keys, Home and End. Accordion sections
and flashcards use native, keyboard-accessible reveal controls.

Lessons support 12 sections, 16 blocks per section and 4 MB rendered content;
nesting is bounded. Questionnaires remain prohibited inside animation frames.
Reports should choose complementary views that explain the concept rather
than showing every available widget on every answer.

## Code and algorithm walkthroughs

A `walkthrough` block contains `title`, `code`, and
`steps: [{title, explanation, lines: [one-based line numbers], variables: {name:value}}]`.
Back/Next highlights the relevant code and updates the explanation and variable
table. Step explanations can instead use `blocks` for rich visual content.
Limits: 300 code lines, 50 steps, 30 variables per step, 4 MB rendered content.

These are explicitly **illustrative** traces supplied by the model. The report
does not execute the shown program or certify its variable values. VANTA must
use the existing build/execution tools for actual execution evidence. Rich
code, mathematical expressions and user input remain separate capabilities.

The browser report is the rich rendering path. If it cannot launch or
`VANTA_REPORT_WINDOW=0`, the older Tk fallback cannot offer interactive 3D.
Normal report visibility rules are unchanged: adding media does not open
windows for routine action receipts or empty status messages.

## Verification

Run the report suites using the repository's test Python/environment:

```powershell
$env:PYTHONPATH='D:\winter\.venv\Lib\site-packages;D:\winter'
python -m pytest -q tests/test_report_media.py tests/test_report_interactions.py tests/test_report_and_border.py tests/test_report_view.py tests/test_report_replaces_blackboard.py
```

The media suite opens a fresh headless Chromium context against a temporary
loopback report server. It checks painted pixels, local image loading, SVG
diagrams, keyboard/pointer rotation, wheel zoom, wireframe, reset, narrow
viewport fitting and report replacement under the actual content security
policy. No personal account or production graph is used.
