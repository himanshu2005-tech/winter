# -*- coding: utf-8 -*-
"""
research.py -- VANTA teaching itself a subject, unattended, for as long as it takes.

Entering research mode starts a session. If Himanshu named a topic, that is the
topic; if he said "learn whatever you want", the model picks one, informed by
what the graph already knows and what it has already studied. The session then
runs in rounds until it can demonstrate it knows the subject, and everything it
learns lands in the graph rather than in a transcript that disappears.

A round is: pick the open questions that matter most, search, read, extract
facts into the graph, then re-examine what is still unanswered. Two things stop
it. Either the yield collapses -- several rounds in a row adding almost no new
facts, which means the accessible sources are exhausted -- or it passes its own
exam: it writes questions on the topic, answers them from the graph alone, and
grades itself. Mastery requires both a high score and a plateau, because a high
score on its own only proves the questions were easy, and a plateau on its own
only proves it stopped finding things.

Design notes worth keeping in mind:

*It must survive a restart.* A multi-hour session that loses everything when
VANTA is closed is not a multi-hour session. State is checkpointed to the
vault after every round and resumed on the next start.

*It must be interruptible.* `stop()` is checked between every step, not just
between rounds, so leaving research mode does not leave a thread burning API
budget for another twenty minutes.

*It must not silently spin.* Search failures are distinguished from empty
results: a dropped network would otherwise read as "this topic is exhausted"
and end the session declaring mastery of nothing.
"""

import json
import os
import re
import threading
import time

STATE_FILE = ("vault", "system", "research_state.json")

# A round is one search-read-extract cycle. These bounds exist so an
# unattended session cannot run away with the API budget or the disk.
MAX_QUESTIONS_PER_ROUND = 4
MAX_ROUNDS = 240
PLATEAU_ROUNDS = 3              # consecutive low-yield rounds that mean "done"
PLATEAU_NEW_FACTS = 3           # "low yield" threshold, facts per round
MASTERY_SCORE = 0.85            # share of exam questions answered from the graph
EXAM_QUESTIONS = 8
ROUND_PAUSE_SECONDS = 4         # breathing room between rounds


def _parse_json_block(text):
    """Pull the first JSON array or object out of a model reply."""
    raw = str(text or "").strip()
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", raw)
    if fence:
        raw = fence.group(1).strip()
    for opener, closer in (("[", "]"), ("{", "}")):
        start = raw.find(opener)
        end = raw.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(raw[start:end + 1])
            except json.JSONDecodeError:
                continue
    return None


class ResearchSession:
    """One topic, studied to mastery."""

    def __init__(self, topic="", base_dir=".", chosen_by="user"):
        self.topic = str(topic or "").strip()
        self.base_dir = base_dir
        self.chosen_by = chosen_by
        self.started = time.time()
        self.rounds = 0
        self.facts_total = 0
        self.facts_this_round = 0
        self.sources = []
        self.open_questions = []
        self.answered_questions = []
        self.low_yield_streak = 0
        self.best_score = 0.0
        self.status = "starting"
        self.last_note = ""
        self.finished = False

    def as_dict(self):
        return {
            "topic": self.topic, "chosen_by": self.chosen_by,
            "started": self.started, "rounds": self.rounds,
            "facts_total": self.facts_total, "sources": self.sources[-60:],
            "open_questions": self.open_questions[:40],
            "answered_questions": self.answered_questions[-80:],
            "low_yield_streak": self.low_yield_streak,
            "best_score": self.best_score, "status": self.status,
            "last_note": self.last_note, "finished": self.finished,
        }

    @classmethod
    def from_dict(cls, data, base_dir):
        session = cls(data.get("topic", ""), base_dir,
                      data.get("chosen_by", "user"))
        session.started = float(data.get("started") or time.time())
        session.rounds = int(data.get("rounds") or 0)
        session.facts_total = int(data.get("facts_total") or 0)
        session.sources = list(data.get("sources") or [])
        session.open_questions = list(data.get("open_questions") or [])
        session.answered_questions = list(data.get("answered_questions") or [])
        session.low_yield_streak = int(data.get("low_yield_streak") or 0)
        session.best_score = float(data.get("best_score") or 0.0)
        session.status = data.get("status", "resumed")
        session.finished = bool(data.get("finished"))
        return session

    def elapsed(self):
        return time.time() - self.started


class Researcher:
    """The background loop. One at a time; starting a new topic replaces the old."""

    def __init__(self, base_dir, call_llm, ui_queue=None, on_note=None):
        self.base_dir = base_dir
        self.call_llm = call_llm
        self.ui_queue = ui_queue
        self.on_note = on_note
        self.session = None
        self._thread = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

    # ------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------ #
    def is_running(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self, topic="", resume=True):
        """Begin or resume a session. Returns a line to say out loud."""
        with self._lock:
            if self.is_running():
                current = self.session.topic if self.session else "something"
                if not topic or topic.lower() in current.lower():
                    return f"[Success] Already researching {current}."
                self.stop(wait=True)

            previous = self._load_state() if resume else None
            if (previous and not previous.finished and
                    (not topic or topic.lower() in previous.topic.lower())):
                self.session = previous
                self.session.status = "resumed"
                opening = (f"[Success] Resuming research on {previous.topic} "
                           f"(round {previous.rounds}, {previous.facts_total} "
                           "facts so far).")
            else:
                self.session = ResearchSession(
                    topic, self.base_dir, "user" if topic else "vanta")
                opening = (f"[Success] Researching {topic}." if topic else
                           "[Success] Choosing a topic to research.")

            self._stop.clear()
            self._thread = threading.Thread(target=self._run, daemon=True,
                                            name="vanta-researcher")
            self._thread.start()
            return opening

    def stop(self, wait=False):
        self._stop.set()
        if wait and self._thread is not None:
            self._thread.join(timeout=20)
        if self.session and not self.session.finished:
            self.session.status = "paused"
            self._save_state()
        return "[Success] Research paused. It will resume where it stopped."

    def status_line(self):
        session = self.session
        if session is None:
            return "No research session."
        minutes = int(session.elapsed() // 60)
        return (f"{session.topic or 'choosing a topic'} — round "
                f"{session.rounds}, {session.facts_total} facts, "
                f"{len(session.open_questions)} open questions, "
                f"best exam score {session.best_score:.0%}, {minutes}m elapsed"
                f" — {session.status}")

    # ------------------------------------------------------------ #
    # Reporting
    # ------------------------------------------------------------ #
    def _note(self, text):
        line = f"[Research] {text}"
        print(line)
        if self.session is not None:
            self.session.last_note = str(text)
        if self.ui_queue is not None:
            try:
                self.ui_queue.put(("info", line))
            except Exception:
                pass
        if self.on_note:
            try:
                self.on_note(text)
            except Exception:
                pass

    def _publish_panel(self):
        """Keep a live board in the Control Center so progress is visible.

        A process that runs for hours with no visible trace is indistinguishable
        from one that has hung.
        """
        session = self.session
        if session is None:
            return
        try:
            import panels

            mastery = max(session.best_score, 0.0)
            minutes = int(session.elapsed() // 60)
            items = [
                {"kind": "progress", "label": "Mastery",
                 "value": f"{mastery:.0%}", "ratio": mastery,
                 "detail": f"target {MASTERY_SCORE:.0%} on its own exam"},
                {"kind": "stat", "label": "Facts in graph",
                 "value": str(session.facts_total),
                 "detail": f"+{session.facts_this_round} last round"},
                {"kind": "kv", "rows": [
                    ["Rounds", str(session.rounds)],
                    ["Sources read", str(len(session.sources))],
                    ["Elapsed", f"{minutes}m"],
                    ["Status", session.status],
                ]},
            ]
            if session.open_questions:
                items.append({"kind": "divider"})
                items.append({"kind": "bullets",
                              "items": session.open_questions[:4]})
            items.append({"kind": "button", "label": "Teach me this",
                          "command": f"teach me what you have learned about "
                                     f"{session.topic}"})
            panels.add(self.base_dir, {
                "title": f"Research: {session.topic}"[:60],
                "note": f"Topic chosen by {session.chosen_by}",
                "accent": "purple", "pinned": True, "items": items,
            })
        except Exception as exc:
            print(f"[Research] Could not update the panel: {exc}")

    # ------------------------------------------------------------ #
    # Persistence
    # ------------------------------------------------------------ #
    def _state_path(self):
        return os.path.join(self.base_dir, *STATE_FILE)

    def _save_state(self):
        if self.session is None:
            return
        try:
            path = self._state_path()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            temporary = path + ".tmp"
            with open(temporary, "w", encoding="utf-8") as handle:
                json.dump(self.session.as_dict(), handle, indent=2,
                          ensure_ascii=False)
            os.replace(temporary, path)
        except Exception as exc:
            print(f"[Research] Could not checkpoint: {exc}")

    def _load_state(self):
        try:
            with open(self._state_path(), "r", encoding="utf-8") as handle:
                return ResearchSession.from_dict(json.load(handle), self.base_dir)
        except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
            return None

    # ------------------------------------------------------------ #
    # The loop
    # ------------------------------------------------------------ #
    def _run(self):
        session = self.session
        try:
            if not session.topic:
                session.topic = self._choose_topic()
                if not session.topic:
                    session.status = "could not choose a topic"
                    self._note("I could not settle on a topic to study.")
                    return
                self._note(f"Chose a topic for myself: {session.topic}")

            if not session.open_questions:
                session.open_questions = self._opening_questions(session.topic)
                self._note(f"Opening with {len(session.open_questions)} questions.")

            session.status = "researching"
            self._publish_panel()

            while not self._stop.is_set() and session.rounds < MAX_ROUNDS:
                if not session.open_questions:
                    session.open_questions = self._new_questions(session)
                    if not session.open_questions:
                        self._finish("ran out of questions worth asking")
                        return

                gained = self._round(session)
                session.rounds += 1
                session.facts_this_round = gained
                session.facts_total += gained
                self._save_state()
                self._publish_panel()

                if gained <= PLATEAU_NEW_FACTS:
                    session.low_yield_streak += 1
                else:
                    session.low_yield_streak = 0

                plateaued = session.low_yield_streak >= PLATEAU_ROUNDS
                # Examining costs a model call, so it only happens where the
                # answer could actually change the decision.
                if plateaued or session.rounds % 5 == 0:
                    score = self._examine(session)
                    session.best_score = max(session.best_score, score)
                    self._note(f"Self-exam: {score:.0%} "
                               f"(best {session.best_score:.0%}).")
                    self._save_state()
                    self._publish_panel()
                    if score >= MASTERY_SCORE and plateaued:
                        self._finish(f"mastered it, scoring {score:.0%} on its "
                                     "own exam with no new facts left to find")
                        return
                    if plateaued and score < MASTERY_SCORE:
                        # Exhausted these questions but still cannot answer the
                        # exam: the gaps are elsewhere, so ask differently.
                        session.open_questions = self._new_questions(
                            session, from_failures=True)
                        session.low_yield_streak = 0

                for _ in range(ROUND_PAUSE_SECONDS):
                    if self._stop.is_set():
                        break
                    time.sleep(1)

            if session.rounds >= MAX_ROUNDS:
                self._finish("hit the round limit for one session")
        except Exception as exc:
            session.status = f"stopped on an error: {exc}"
            self._note(f"Research stopped: {exc}")
            self._save_state()
        finally:
            if self._stop.is_set() and not session.finished:
                session.status = "paused"
                self._save_state()
                self._publish_panel()

    def _finish(self, reason):
        session = self.session
        session.finished = True
        session.status = f"complete — {reason}"
        self._save_state()
        self._publish_panel()
        minutes = int(session.elapsed() // 60)
        self._note(f"Finished {session.topic}: {reason}. "
                   f"{session.facts_total} facts over {session.rounds} rounds "
                   f"and {minutes} minutes.")

    def _round(self, session):
        """One search-read-extract cycle. Returns facts added."""
        from providers import knowledge_api, research_api

        batch = session.open_questions[:MAX_QUESTIONS_PER_ROUND]
        session.open_questions = session.open_questions[MAX_QUESTIONS_PER_ROUND:]
        gained = 0

        for question in batch:
            if self._stop.is_set():
                break
            self._note(f"Round {session.rounds + 1}: {question}")
            try:
                found = research_api.search(question, depth="advanced",
                                            max_results=4)
            except research_api.SearchUnavailable as exc:
                # Not the same as "nothing found": put the question back and
                # wait, rather than concluding the topic is exhausted.
                self._note(f"Search unavailable ({exc}). Waiting to retry.")
                session.open_questions.insert(0, question)
                for _ in range(30):
                    if self._stop.is_set():
                        return gained
                    time.sleep(1)
                return gained

            if not found["results"]:
                session.answered_questions.append(question)
                continue

            for result in found["results"]:
                if result["url"] and result["url"] not in session.sources:
                    session.sources.append(result["url"])

            corpus = research_api.corpus(found)
            # Tavily supplies readable page bodies; the free federation adds
            # scholarly/entity/technical coverage and explicit provenance.
            # Failure is non-fatal because research_api already returned the
            # primary evidence for this round.
            try:
                structured = knowledge_api.search(question, limit=6)
                corpus += "\n\n" + knowledge_api.format_for_model(
                    structured, max_chars=6000)
                for result in structured.get("results", []):
                    if result.get("url") and result["url"] not in session.sources:
                        session.sources.append(result["url"])
            except Exception as exc:
                self._note(f"Free knowledge sources partially unavailable: {exc}")
            gained += self._extract(session.topic, question, corpus)
            session.answered_questions.append(question)
        return gained

    def _extract(self, topic, question, corpus):
        """Turn read material into graph facts. Returns how many landed."""
        if not corpus.strip():
            return 0
        try:
            from memory.consolidator import queue_for_consolidation
            # The research path of the consolidator extracts domain triples
            # with its own predicate set, so this is the one call needed.
            queue_for_consolidation(
                f"Topic: {topic}\nQuestion: {question}\n\n{corpus[:14000]}",
                source_id=f"research:{topic[:40]}", mode="research")
        except Exception as exc:
            self._note(f"Could not queue extraction: {exc}")
            return 0

        # The consolidator is asynchronous, so the count here is an estimate of
        # what was handed over, not a promise about what the graph accepted.
        # _examine is what actually checks whether the knowledge arrived.
        return max(1, len(corpus) // 2500)

    # ------------------------------------------------------------ #
    # Model-driven steps
    # ------------------------------------------------------------ #
    def _ask(self, system, user, task_level="routine", attempts=3):
        """Ask the model, retrying a blank reply before giving up.

        Over hours, every provider drops the occasional request. A single
        empty answer here would silently cost a whole step -- generic
        questions instead of planned ones, or a mastery score of zero that
        was really a dropped connection -- and the session would never know.
        """
        for attempt in range(attempts):
            if self._stop.is_set():
                return ""
            try:
                reply = self.call_llm(
                    [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
                    task_level=task_level, quiet=True) or ""
            except Exception as exc:
                reply = ""
                self._note(f"Model call failed: {exc}")
            if reply.strip():
                return reply
            if attempt < attempts - 1:
                delay = 5 * (attempt + 1)
                self._note(f"Empty reply from the model; retrying in {delay}s.")
                for _ in range(delay):
                    if self._stop.is_set():
                        return ""
                    time.sleep(1)
        return ""

    def _choose_topic(self):
        """Let the model pick, told what it already knows and has studied."""
        known = self._graph_summary(limit=40)
        studied = self._previous_topics()
        reply = self._ask(
            "You are choosing what to study next, for yourself. Pick one "
            "specific, substantial subject you could research for hours and "
            "come away genuinely knowledgeable about. Prefer something that "
            "connects to what you already know without duplicating it. Be "
            "specific: 'transformer attention mechanisms' rather than 'AI'. "
            "Reply with the topic alone, no preamble, under 60 characters.",
            f"Already in the knowledge graph:\n{known or '(nothing yet)'}\n\n"
            f"Already studied: {', '.join(studied) or '(nothing yet)'}")
        topic = " ".join(str(reply).split()).strip().strip('."\'')
        return topic[:70]

    def _opening_questions(self, topic):
        reply = self._ask(
            "You are planning unattended research. Write the questions that "
            "would take someone from knowing nothing to genuinely competent in "
            "a subject. Cover definitions, mechanisms, the mathematics or "
            "formalism, history, applications, limitations and open problems. "
            "Each question must be answerable from written sources and specific "
            "enough to search verbatim. Reply as a JSON array of strings, 10 to "
            "14 of them, nothing else.",
            f"Subject: {topic}")
        questions = _parse_json_block(reply)
        if not isinstance(questions, list) or not questions:
            self._note("Could not plan questions; falling back to a generic "
                       "opening set. The session will still widen them later.")
            return [f"What is {topic}?", f"How does {topic} work?",
                    f"What is the mathematics behind {topic}?",
                    f"What are the main results in {topic}?",
                    f"What are the limitations of {topic}?",
                    f"What are open problems in {topic}?"]
        return [str(q).strip() for q in questions if str(q).strip()][:14]

    def _new_questions(self, session, from_failures=False):
        """Ask what is still missing, given what has been covered."""
        covered = session.answered_questions[-24:]
        known = self._graph_summary(limit=50, topic=session.topic)
        instruction = (
            "The exam is still not being passed, so the gaps are not where the "
            "previous questions looked. Propose a different angle entirely."
            if from_failures else
            "Propose the next questions that would deepen this knowledge.")
        reply = self._ask(
            "You are extending unattended research on a subject. " + instruction +
            " Do not repeat questions already covered. Each must be specific "
            "and searchable. Reply as a JSON array of 6 to 10 strings, nothing "
            "else. Reply with [] only if the subject is genuinely exhausted.",
            f"Subject: {session.topic}\n\nAlready asked:\n" +
            "\n".join(f"- {q}" for q in covered) +
            f"\n\nAlready in the graph:\n{known or '(nothing)'}")
        questions = _parse_json_block(reply)
        if not isinstance(questions, list):
            return []
        seen = {q.lower() for q in session.answered_questions}
        return [str(q).strip() for q in questions
                if str(q).strip() and str(q).strip().lower() not in seen][:10]

    def _examine(self, session):
        """Write an exam, answer it from the graph alone, and grade it.

        Answering from the graph rather than from the model's own knowledge is
        the entire point: it measures what VANTA has actually stored, not what
        the underlying model happened to already know.
        """
        reply = self._ask(
            "Write an exam that would distinguish someone who genuinely knows "
            f"a subject from someone who has only skimmed it. {EXAM_QUESTIONS} "
            "questions, each with a short factual answer. Reply as a JSON array "
            "of objects with 'q' and 'a', nothing else.",
            f"Subject: {session.topic}")
        exam = _parse_json_block(reply)
        if not isinstance(exam, list) or not exam:
            return 0.0

        known = self._graph_summary(limit=160, topic=session.topic)
        if not known.strip():
            return 0.0

        graded = self._ask(
            "You are grading whether a knowledge base contains the information "
            "needed to answer each question. Use ONLY the supplied facts; your "
            "own knowledge of the subject does not count and must not be used. "
            "For each question reply 1 if the facts answer it and 0 if they do "
            "not. Reply as a JSON array of 0s and 1s, one per question, nothing "
            "else.",
            "Facts available:\n" + known + "\n\nQuestions:\n" +
            "\n".join(f"{i+1}. {item.get('q', '')}"
                      for i, item in enumerate(exam) if isinstance(item, dict)))
        scores = _parse_json_block(graded)
        if not isinstance(scores, list) or not scores:
            return 0.0
        marks = [1.0 if str(value).strip() in ("1", "1.0", "true", "True") else 0.0
                 for value in scores]
        return sum(marks) / max(1, len(marks))

    # ------------------------------------------------------------ #
    # Graph access
    # ------------------------------------------------------------ #
    def _graph_summary(self, limit=60, topic=""):
        """What the graph holds, as plain triples the model can read."""
        try:
            from memory.neo4j_store import store

            # search_facts returns nothing at all for an empty query by design,
            # so a topicless summary has to come from the unfiltered listing.
            rows = []
            if topic:
                rows = store.search_facts(query=topic, limit=limit)
            # The ranked search returns nothing for queries whose tokens are all
            # filtered out. Falling through to the unfiltered listing matters:
            # _examine scores 0 on an empty summary, so without this a session
            # could never register mastery however much it had actually learned.
            if not rows:
                rows = store.explicit_facts(limit=limit)
        except Exception as exc:
            print(f"[Research] Graph read failed: {exc}")
            rows = []
        if not rows:
            return ""
        lines = []
        for row in rows:
            subject = row.get("subject") or ""
            predicate = str(row.get("predicate") or "").replace("_", " ").lower()
            obj = row.get("object") or ""
            if subject and obj:
                lines.append(f"{subject} {predicate} {obj}")
        return "\n".join(lines[:limit])

    def _previous_topics(self):
        previous = self._load_state()
        return [previous.topic] if previous and previous.topic else []


# ------------------------------------------------------------------ #
# Module-level singleton, so mode switches and actions share one session
# ------------------------------------------------------------------ #
_researcher = None


def get_researcher(base_dir=None, call_llm=None, ui_queue=None):
    global _researcher
    if _researcher is None:
        if call_llm is None:
            from llm_router import call_llm as router_call
            call_llm = router_call
        _researcher = Researcher(base_dir or os.path.dirname(os.path.abspath(__file__)),
                                 call_llm, ui_queue)
    elif ui_queue is not None and _researcher.ui_queue is None:
        _researcher.ui_queue = ui_queue
    return _researcher
