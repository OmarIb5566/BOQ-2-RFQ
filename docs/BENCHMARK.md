# Benchmark: the app's packages vs. packages made by hand

`bench/benchmark.py` runs the full engine on historical projects for which people cut the work packages by hand, and
compares. The project files stay on the PC (they are not in the repository); only numbers are written to
`bench/results/` (also not committed).

## Method

* **Ground truth**: Project 1 (`ETS-A-ARCH-BOQ.xls` + `ETS-A-Civil-BOQ.xlsx`; 5 hand-made package BOQs) and Project 2
  (`BOQ All QTY.xlsx`; 4 hand-made package BOQs). 9 packages, 51 hand-packaged items, of which 30 could be matched to the full BOQ.
* Each hand-made item is matched to the full BOQ by its text (+ quantity); otherwise by the same item number with ≥ 35 %
  of the words in common, otherwise by ≥ 60 % of the words in common. The hand-made packages were often cut from a
  slightly different revision of the BOQ (other thicknesses, quantities), so some items cannot be matched.
* **together**: share of a hand-made package's items that the app put in one work package.
  **purity**: share of that app package's items that belong to the hand-made package.
* The special cases are evaluated "all approved" and "all denied" (the human decisions were not recorded).
* Every run in a scratch folder, AI cache off (`force`), on this PC (RTX 4090).

## Results — 23 November 2026

| Configuration | together (denied) | purity (denied) | together (approved) | purity (approved) | time P1 / P2 |
|---|---|---|---|---|---|
| keywords only (no AI) | 0.889 | 0.749 | 0.870 | 0.718 | 2 s / 1 s |
| **gemma4:e4b (default)** | 0.889 | 0.749 | 0.870 | 0.712 | 86 s / 13 s |
| qwen3:14b | 0.889 | 0.749 | 0.870 | 0.712 | 108 s / 26 s |

**Decision: the default stays gemma4:e4b.** qwen3:14b gave the same packages and took longer; nothing justifies a
change. The second-opinion check only flags items (it never changes packages), so it cannot change these numbers;
it stays off by default.

## What the numbers do and do not say

* The sample is **small** (9 packages, 30 matched items) and mostly items that the keyword pre-screen already places
  correctly, so it does not separate the AI models. A larger set of hand-made packages is needed for that.
* The misses come from the company flowchart, not from the model: e.g. people put metal doors and aluminium windows in
  one package, while the flowchart has *Metal Doors Only* and *Aluminium Doors & Windows* as separate packages; the
  "RC concrete" and "Earthwork" packages could only partly be matched (other BOQ revision), so their purity is low.
* Re-run after any change to the models, the prompts or the flowchart rules:
  `python bench/benchmark.py --config keywords --config baseline --config qwen3-14b`.

## Speed (measured)

On Windows, calling the AI engine through `localhost` cost ~2.1 s per request (IPv6 tried first); through `127.0.0.1`
with a kept-alive connection ~0.08 s. A small 11-item BOQ: 12.5 s → 3.8 s with identical results. The app now always
uses the IP address.
