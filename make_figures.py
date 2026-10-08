#!/usr/bin/env python3
"""Generate result figures for the document distillation pipeline.

Reads the real artefact produced by distill.py (out3/knowledge.json) and
renders:
  * results/distillation_results.png — knowledge breakdown + stage latency
"""
from __future__ import annotations

import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parent
RESULTS = ROOT / "results"
RESULTS.mkdir(exist_ok=True)

# ---------------------------------------------------------------- knowledge
knowledge = json.loads((ROOT / "out3" / "knowledge.json").read_text(encoding="utf-8"))

CATEGORIES = [
    ("Concepts", "concepts"),
    ("Parameters", "parameters"),
    ("Procedures", "procedures"),
    ("Constraints", "constraints"),
    ("FAQ", "faq"),
]
labels = [name for name, _ in CATEGORIES]
counts = [len(knowledge.get(key, [])) for _, key in CATEGORIES]
total = sum(counts)

COLORS = ["#2b4f9e", "#3d8bfd", "#7aa7e8", "#e8a33d", "#c0553a"]

fig, axes = plt.subplots(1, 2, figsize=(12.5, 4.8))

# --- 1. knowledge breakdown ------------------------------------------------
bars = axes[0].bar(labels, counts, color=COLORS)
axes[0].bar_label(bars, fontsize=11, padding=2)
axes[0].set_title(
    f"Structured knowledge extracted from one document\n"
    f"4,400 words → {total} items",
    fontsize=11,
)
axes[0].set_ylabel("Items")
axes[0].grid(axis="y", alpha=0.25)
axes[0].set_axisbelow(True)

# --- 2. latency: distillation vs scene rendering ---------------------------
stages = [
    "Distillation\n(one-off)",
    "Scene 1\nnewcomer Q&A",
    "Scene 2\ncustomer reply",
    "Scene 3\ntechnical proposal",
    "Scene 4\nexam paper",
]
times = [33.6, 19.9, 9.0, 70.8, 24.1]

b2 = axes[1].barh(stages[::-1], times[::-1], color="#2b4f9e")
axes[1].bar_label(b2, fmt="%.1f s", fontsize=10, padding=3)
axes[1].set_xlabel("Wall-clock time [s]")
axes[1].set_title(
    "One-off distillation vs per-scene rendering\n"
    "(rendering reuses the distilled artefact — no re-reading)",
    fontsize=11,
)
axes[1].grid(axis="x", alpha=0.25)
axes[1].set_axisbelow(True)

fig.tight_layout()
out = RESULTS / "distillation_results.png"
fig.savefig(out, dpi=150)
plt.close(fig)

print(f"✅ {out}")
print(f"   knowledge items: {dict(zip(labels, counts))}  (total {total})")
