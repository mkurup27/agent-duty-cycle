"""Phase timing.

All agent-side timestamps are wall-clock (time.time()) for log correlation,
but every *duration* the analysis uses is computed from time.perf_counter()
deltas inside one process, so clock skew between the driver and the agent
never contaminates a latency number. Cross-machine overhead (cold start,
network, resume) is derived as: driver round-trip minus agent-internal time.
"""

import os
import time

# Recorded once per process. Lets every response say whether this event was
# served by a fresh process (cold) and how long that process has lived.
PROCESS_START_WALL = time.time()
PROCESS_START_PERF = time.perf_counter()
_INVOCATIONS = 0


def process_info():
    global _INVOCATIONS
    _INVOCATIONS += 1
    return {
        "pid": os.getpid(),
        "process_uptime_s": round(time.perf_counter() - PROCESS_START_PERF, 4),
        "invocation_in_process": _INVOCATIONS,
        "cold_process": _INVOCATIONS == 1,
    }


class PhaseTimer:
    """Marks named phases relative to construction time."""

    def __init__(self):
        self.t0_wall = time.time()
        self.t0 = time.perf_counter()
        self.marks = {}
        self.cpu0 = time.process_time()

    def mark(self, name):
        # First mark wins: "first_model_call_start" must stay the first one.
        if name not in self.marks:
            self.marks[name] = round(time.perf_counter() - self.t0, 4)

    def elapsed(self):
        return time.perf_counter() - self.t0

    def to_dict(self):
        return {
            "t0_wall": self.t0_wall,
            "marks_s": dict(self.marks),
            "total_s": round(self.elapsed(), 4),
            "cpu_s": round(time.process_time() - self.cpu0, 4),
        }
