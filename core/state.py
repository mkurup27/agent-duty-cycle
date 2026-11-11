"""The agent's session state.

Deliberately plain Python types (dicts, lists, ints, strings). Core ships NO
serialization code: the always-on and pause/resume arms never serialize this
object, they simply keep it in memory. The stateless arm has to write its own
(de)serializer, and that code is counted as the "engineering tax".

Note for the write-up: choosing plain types is a concession that makes the
stateless arm's job easier than it is for most real agents (which hold
framework objects, open clients, iterators and the like). The measured tax
is therefore a lower bound.
"""

from dataclasses import dataclass, field


@dataclass
class AgentState:
    agent_id: str = "agent-0"
    # Chat transcript sent to the model on every call (bounded).
    history: list = field(default_factory=list)
    # incident_id -> {status, severity, service, summary, plan, opened_at, decided_at, decision}
    incidents: dict = field(default_factory=dict)
    feed_cursor: int = 0
    events_processed: int = 0
    counts: dict = field(default_factory=dict)          # event type -> count
    processed_ids: list = field(default_factory=list)   # recent ids, for dedupe
    seed: dict = field(default_factory=dict)            # {bytes, sha256} when seeded
