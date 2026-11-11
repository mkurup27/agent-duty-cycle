"""Shared ops-triage agent used identically by all three deployment arms.

Nothing in this package knows which arm it runs in. State lives in an
in-memory AgentState plus a local workspace directory. How (and whether)
that state survives between events is the arm's problem, which is exactly
the variable the experiment measures.
"""

__version__ = "0.1.0"
