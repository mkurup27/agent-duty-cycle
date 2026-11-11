"""Monthly harness cost vs. duty cycle (DO list prices we verified 9 Nov 2026)."""
HOURS = 730

# (a) always-on: s5-2vcpu-4gb-30gb, v5 per-resource rates
DROPLET_HR = 2 * 0.0158219 + 4 * 0.0040411 + 30 * 0.000137      # $/hour

# (b) stateless: Functions at 512 MB, plus always-on state backends
FN_GIB_S = 0.0000185
FN_MEM_GIB = 0.5
BACKENDS = 15.00 + 10 * 0.215 + 5.00   # Postgres node + 10 GiB storage + Spaces

# (c) pause/resume: mars-2vcpu-4gb, billed only while awake
HR_CPU, HR_MEM, VCPU, MEM_GB = 0.044, 0.0095, 2, 4
SESSION_STORAGE = 1 * 0.05             # 1 GiB volume, $/month

def always_on(duty):
    return DROPLET_HR * HOURS

def stateless(duty, free_tier=False):
    gib_s = FN_MEM_GIB * duty * HOURS * 3600
    if free_tier:
        gib_s = max(0, gib_s - 90_000)
    return BACKENDS + gib_s * FN_GIB_S

def pause_resume(duty, cpu_util=0.11):
    # duty here is billed-awake fraction, not event-busy; equal only with immediate pause
    awake_h = duty * HOURS
    per_hr = VCPU * cpu_util * HR_CPU + MEM_GB * HR_MEM
    return SESSION_STORAGE + awake_h * per_hr

if __name__ == "__main__":
    print(f"{'duty':>5} {'always-on':>10} {'stateless':>10} {'pause/resume':>13}")
    for d in (0.02, 0.10, 0.30, 0.50, 1.00):
        print(f"{d:>5.0%} {always_on(d):>10.2f} {stateless(d):>10.2f} {pause_resume(d):>13.2f}")
