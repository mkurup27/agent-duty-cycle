"""Redraw the charts used in the article.

  python3 article/charts.py

Needs matplotlib. Writes into article/images/:
  agent-lifecycle-architectures.png
  idle-cost-breakdown.png
  duty-cycle-cost-curves.png
"""

import os
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from duty_cost import always_on, pause_resume, stateless

OUT = os.path.join(HERE, "images") + os.sep
os.makedirs(OUT, exist_ok=True)

C = {
    "a": "#0068F8",
    "b": "#F0A020",
    "b_dark": "#C07810",
    "c": "#10A068",
    "c100": "#8840A8",
    "ink": "#282828",
    "mute": "#686868",
    "grid": "#E0E0E8",
}
plt.rcParams.update({
    "font.family": "sans-serif",
    "font.sans-serif": ["Helvetica Neue", "Helvetica", "Arial", "DejaVu Sans"],
    "font.size": 11,
})


def cost_curves():
    d = [i / 1000 for i in range(0, 1001, 5)]
    x = [v * 100 for v in d]
    fig, ax = plt.subplots(figsize=(10, 6), dpi=150)
    ax.plot(x, [always_on(v) for v in d], color=C["a"], lw=2.5,
            label="(a) Always-on Droplet")
    ax.plot(x, [stateless(v) for v in d], color=C["b"], lw=2.5,
            label="(b) Stateless Functions + Postgres/Spaces")
    ax.plot(x, [pause_resume(v) for v in d], color=C["c"], lw=2.5,
            label="(c) Pause/resume, 11% CPU while awake (measured)")
    ax.plot(x, [pause_resume(v, 1.0) for v in d], color=C["c100"], lw=2, ls="--",
            label="(c) Pause/resume, 100% CPU while awake")
    for xv, lab, xt in ((64.8, "64.8%", (70, 8)), (41.2, "41.2%", (20, 50))):
        ax.axvline(xv, color=C["mute"], lw=0.8, ls=":")
        ax.annotate(
            f"crosses always-on\nat {lab}", xy=(xv, 37.9), xytext=xt, fontsize=9,
            color=C["mute"], arrowprops=dict(arrowstyle="-", color=C["mute"], lw=0.8),
        )
    for xv in (2, 30):
        ax.axvspan(xv - 0.4, xv + 0.4, color="#E5E7EB", zorder=0)
    ax.text(2.8, 88, "2% and 30%:\nmeasured profiles", fontsize=8.5, color=C["mute"])
    ax.set_xlim(0, 100)
    ax.set_ylim(0, 95)
    ax.set_xlabel("Duty cycle (% of wall-clock time the agent is working)")
    ax.set_ylabel("Harness cost, USD per 730-hour month")
    ax.set_title(
        "Monthly harness cost vs. duty cycle (DO list prices, 9 Nov 2026)",
        loc="left", fontsize=13, color=C["ink"], pad=12,
    )
    ax.spines[["top", "right"]].set_visible(False)
    ax.grid(axis="y", color=C["grid"])
    ax.legend(loc="upper left", bbox_to_anchor=(0, 0.93), frameon=False, fontsize=9.5)
    fig.text(
        0.01, 0.01,
        "Model: duty_cost.py. Excludes model tokens. "
        "Pause/resume assumes memory billed on the full 4 GB allocation.",
        fontsize=8, color=C["mute"],
    )
    fig.tight_layout(rect=(0, 0.03, 1, 1))
    fig.savefig(OUT + "duty-cycle-cost-curves.png")
    plt.close(fig)


def idle_cost_breakdown():
    """Tradeoff 1 measured points: stacked backends vs compute for stateless."""
    import numpy as np

    profiles = ["2% duty cycle", "30% duty cycle"]
    always = np.array([37.90, 37.90])
    backends = np.array([22.15, 22.15])
    compute = np.array([0.48, 8.03])
    pause = np.array([0.69, 11.30])
    totals_b = backends + compute

    x = np.arange(len(profiles))
    width = 0.26
    fig, ax = plt.subplots(figsize=(12, 7.5), dpi=150)
    fig.subplots_adjust(left=0.10, right=0.70, top=0.90, bottom=0.16)

    b_a = ax.bar(x - width, always, width, color=C["a"], zorder=3)
    b_be = ax.bar(x, backends, width, color=C["b_dark"], zorder=3)
    b_co = ax.bar(x, compute, width, bottom=backends, color=C["b"], zorder=3)
    b_c = ax.bar(x + width, pause, width, color=C["c"], zorder=3)

    ax.set_ylabel("List-price harness cost, USD per 730-hour month")
    ax.set_title("Tradeoff 1: monthly harness cost at the measured duty cycles")
    ax.set_xticks(x)
    ax.set_xticklabels(profiles)
    ax.set_ylim(0, 46)
    ax.set_xlim(-0.55, 1.55)
    ax.yaxis.grid(True, color=C["grid"], linewidth=1, zorder=0)
    ax.set_axisbelow(True)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)

    label_bbox = dict(boxstyle="round,pad=0.2", facecolor="white", edgecolor="none", alpha=0.95)
    for i, tot in enumerate(always):
        ax.text(x[i] - width, tot + 1.6, f"${tot:.2f}", ha="center", va="bottom",
                fontsize=11, fontweight="bold", color=C["a"], clip_on=False, bbox=label_bbox)
    for i, tot in enumerate(totals_b):
        ax.text(x[i], tot + 1.6, f"${tot:.2f}", ha="center", va="bottom",
                fontsize=11, fontweight="bold", color=C["b_dark"], clip_on=False, bbox=label_bbox)
    for i, tot in enumerate(pause):
        ax.text(x[i] + width, tot + 1.6, f"${tot:.2f}", ha="center", va="bottom",
                fontsize=11, fontweight="bold", color=C["c"], clip_on=False, bbox=label_bbox)

    ax.legend(
        [b_a, b_be, b_co, b_c],
        [
            "(a) Always-on Droplet",
            "(b) Backends — Postgres/Spaces ($22.15)",
            "(b) Functions compute ($0.48 / $8.03)",
            "(c) Pause/resume harness",
        ],
        loc="center left",
        bbox_to_anchor=(1.04, 0.55),
        frameon=False,
        labelspacing=2.0,
        handlelength=1.6,
        borderaxespad=0.0,
    )
    fig.text(
        0.08, 0.035,
        "Measured usage x DO list prices. Stateless Functions compute is gross (pre-allowance). "
        "Pause/resume excludes $0.05/GiB-month SESSION_STORAGE. Model tokens excluded.",
        fontsize=8.5, color=C["mute"], ha="left", va="bottom",
    )
    fig.savefig(OUT + "idle-cost-breakdown.png", dpi=150, facecolor="white", pad_inches=0.15)
    plt.close(fig)


def lifecycle():
    fig, ax = plt.subplots(figsize=(12, 5.6), dpi=150)
    ax.set_xlim(0, 12)
    ax.set_ylim(0, 6.0)
    ax.axis("off")
    rows = [
        ("(a) Always-on", C["a"], [
            "Process already\nrunning", "Handle event",
            "Wait in memory\n(billed every second)",
        ]),
        ("(b) Event-triggered stateless", C["b"], [
            "Start container", "Load state from\nPostgres + Spaces", "Handle event",
            "Write state back", "Container exits\n(backends still billed)",
        ]),
        ("(c) Pause/resume", C["c"], [
            "Resume session", "Handle event",
            "Pause: memory + disk\nfrozen (storage billed)",
        ]),
    ]
    ax.add_patch(FancyBboxPatch(
        (0.15, 2.25), 1.3, 0.9, boxstyle="round,pad=0.05", fc="#F3F4F6", ec=C["ink"], lw=1.2,
    ))
    ax.text(0.8, 2.7, "Event\narrives", ha="center", va="center",
            fontsize=10, color=C["ink"], weight="bold")
    ys = [4.45, 2.7, 0.95]
    for (title, col, steps), y in zip(rows, ys):
        ax.text(1.95, y + 0.72, title, fontsize=10.5, weight="bold", color=col)
        ax.annotate(
            "", xy=(1.95, y), xytext=(1.5, 2.7),
            arrowprops=dict(arrowstyle="-|>", color=C["mute"], lw=1.2, connectionstyle="arc3,rad=0"),
        )
        w, gap, x0 = 1.75, 0.25, 1.95
        for i, s in enumerate(steps):
            x = x0 + i * (w + gap)
            last = i == len(steps) - 1
            ax.add_patch(FancyBboxPatch(
                (x, y - 0.4), w, 0.8, boxstyle="round,pad=0.04",
                fc=col if not last else "white", ec=col, lw=1.5, alpha=1 if last else 0.15,
            ))
            ax.add_patch(FancyBboxPatch(
                (x, y - 0.4), w, 0.8, boxstyle="round,pad=0.04", fc="none", ec=col, lw=1.5,
            ))
            ax.text(x + w / 2, y, s, ha="center", va="center", fontsize=8.8, color=C["ink"])
            if not last:
                ax.annotate(
                    "", xy=(x + w + gap, y), xytext=(x + w, y),
                    arrowprops=dict(arrowstyle="-|>", color=col, lw=1.3),
                )
    ax.text(0.15, 5.75, "Three ways to keep an agent alive between events",
            fontsize=13, color=C["ink"])
    fig.savefig(OUT + "agent-lifecycle-architectures.png", bbox_inches="tight", facecolor="white")
    plt.close(fig)


if __name__ == "__main__":
    cost_curves()
    idle_cost_breakdown()
    lifecycle()
    print("wrote", OUT)
