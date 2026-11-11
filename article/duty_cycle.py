import json, sys

def duty_cycle(intervals, window_s):
    """Fraction of window_s covered by at least one busy interval."""
    busy, end = 0.0, float("-inf")
    for s, e in sorted(intervals):
        if e <= end:
            continue
        busy += e - max(s, end)
        end = e
    return busy / window_s

# One JSON object per handled event: {"start": <unix s>, "end": <unix s>}
# Usage: python3 duty_cycle.py events.jsonl <window_start_unix> <window_end_unix>
if __name__ == "__main__":
    rows = [json.loads(line) for line in open(sys.argv[1])]
    window_start, window_end = float(sys.argv[2]), float(sys.argv[3])
    window = window_end - window_start
    clipped = []
    for r in rows:
        s, e = max(r["start"], window_start), min(r["end"], window_end)
        if e > s:
            clipped.append((s, e))
    print(f"events: {len(rows)}  duty cycle: {duty_cycle(clipped, window):.2%}")
