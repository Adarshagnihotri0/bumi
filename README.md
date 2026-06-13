# BhuMe Boundary Alignment - Submission

## Files

| File | Description |
|---|---|
| `solve.py` | Main solver: imagery-driven ICP + confidence-based acceptance |
| `predictions.geojson` | Predictions for Vadnerbhairav (2,457 plots) |
| `transcripts/copilot-session.md` | Full AI coding session transcript |

## How to run

```bash
# Install dependencies
python3 -m pip install rasterio geopandas shapely scipy numpy pyproj scikit-image

# Run (paths are configured at the top of solve.py)
python3 solve.py
# writes predictions.geojson
```

## Design philosophy

The solver separates parcels into two conceptual categories:

- H1: geometry is structurally correct but translated.
- H2: geometry itself is wrong (shape/topology/merge-split errors), so translation alone is insufficient.

Only H1-like parcels are corrected. The rest of the pipeline estimates whether a parcel behaves like H1 before allowing a translation to be applied.

## Conservative correction strategy

The solver intentionally favors precision over recall.

- Corrections are applied only when multiple signals agree.
- Uncertain parcels are flagged instead of force-moved.

This minimizes the risk of damaging H2 parcels that require different algorithms.

## Approach

### 1. Triage
Before correction, the solver tests whether a parcel is a plausible H1 (translation-only) candidate.

`area_ratio = map_area_sqm / (recorded_area_sqm + pot_kharaba_ha * 10,000)`

Rather than acting as a hard filter, this becomes a continuous confidence signal (`area_ratio_score`). Large mismatches are treated as evidence that the parcel may be H2, where translation alone is unlikely to succeed.

### 2. ICP snap-to-nearest-edge
`Take-Home.tif` is a binary satellite-derived edge map. For each parcel:

1. Load all edge pixels within 80m of the official boundary
2. Build a KD-tree
3. Densify boundary points, then snap each to nearest edge within 80m
4. Take median displacement across valid points -> `(dx, dy)` in metres
5. Re-validate shifted geometry with a tight 8m check

Each boundary sample votes for where the parcel should move. The median displacement is used (instead of mean) for robustness to outliers from noisy edges, vegetation, buildings, or local mismatches.

The initial 80m radius is for finding displacement. After shifting, the 8m validation checks whether the boundary actually sits on nearby edges, instead of merely matching some edge somewhere in the original wide search region.

Position is always from imagery (ICP), never from neighbours or area signals.

### 3. Confidence model
`h1_score` combines four approximately independent pieces of evidence:

| Signal | Weight |
|---|---|
| ICP quality (tight inlier fraction) | 0.35 |
| Neighbour agreement | 0.25 |
| Imagery signal (edge density) | 0.20 |
| Area plausibility | 0.20 |

Failure penalties:

- `no_match`: x0.30 (strong H2 evidence)
- `ambiguous`: x0.60

Restraint rule:

- Flag if `h1_score < 0.25` or `shift < 1.5m`

### 4. Topology-aware agreement
Neighbour evidence blends two signals:

- Radius-based agreement from nearby anchors (within 600m)
- Topology-based agreement from parcel adjacency (shared-boundary graph)

Adjacency is built with an STRtree and filtered by minimum shared-boundary length.
Topology agreement allows parcels that physically share boundaries to reinforce one another, even when they are not the closest parcels by Euclidean distance.

Neighbour evidence never changes geometry. It only affects confidence.

### 5. Anchor propagation and iterative reassessment

- L1 anchors: provided truths (trust=1.0)
- L2 anchors: strong imagery + area-consistent parcels (trust=0.8)
- Reassess flagged parcels iteratively, promote new anchors, repeat until convergence or max passes

Only high-confidence parcels are promoted to anchors, limiting error propagation.

Additional anchors improve neighbourhood evidence in later passes, but never replace or alter imagery-derived displacement.

This adds constraint propagation while preserving imagery-only geometry updates.

## Results (public example truths, Vadnerbhairav)

Evaluation subset summary:

- 5 corrected
- 1 flagged
- 6 total truths

| Metric | Official | Ours |
|---|---|---|
| Median IoU | 0.612 | **0.749** |
| Improvement | - | **+0.128** |
| Plots improved vs official | - | **100%** |
| Accurate @ IoU>=0.5 | - | **80%** |
| Median centroid error | - | **11.0m** |
| Calibration AUC (6 public truths; not statistically meaningful) | - | **1.000** |
| Restraint | Hidden-set only | N/A on public truths |

Metric definitions:

- Median IoU: typical overlap with truth (shared / combined area), range [0, 1]
- Improvement: IoU gain over the official baseline
- Accurate @ IoU>=0.5: share of corrected plots with solid overlap
- Centroid error: distance between prediction and truth centroids
- Calibration AUC: whether confidence ranks better fixes above weaker ones (0.5 chance, 1.0 perfect)
- Restraint: whether already-correct plots were left untouched (graded on hidden set)

Important caveat:

These public truths are only a handful of examples, so treat these numbers as a rough directional check, not a grade. Calibration especially needs more samples to be meaningful. The final score is determined on a larger hidden evaluation set.

## What I learned from the data

**dy is consistent, dx is not.** Southward drift appears more systematic across the region, while east-west displacement varies locally. This supports neighbourhood reasoning over a single global correction.

**FFT failed, ICP worked.** FFT-style cross-correlation assumes a dominant global translation signal. In this landscape, heterogeneous boundaries and local variation weaken that assumption. ICP on binary edge geometry produced more stable displacement estimates.

## What I'd do next

1. **Regional displacement field fitting**: replace simple radial trend with a smooth local drift surface.
2. **Hypothesis scoring**: test multiple candidate shifts around ICP estimate and reward clear winners.
3. **Data-driven confidence learning**: learn weights/thresholds from a larger labelled set rather than relying only on manual coefficients.
