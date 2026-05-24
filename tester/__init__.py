"""`tester/` — offline harnesses that catch agent mistakes without hand-dialing.

Prototype layer that sits alongside the `evals/` suite. The first inhabitant is
the tool-receipt hallucination gate (see ``receipt_gate.py`` / ``recorder.py``);
the planned second is a Hypothesis state-machine fuzzer over the dispatcher.

See ``tester/README.md`` for the why, the council decision behind it, and the
roadmap.
"""
