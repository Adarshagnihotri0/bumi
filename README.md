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

## Approach

### 1. Triage
Compute `area_ratio = map_area_sqm / (recorded_area_sqm + pot_kharaba_ha * 10,000)`.
This becomes a continuous plausibility signal (`area_ratio_score`) used in confidence.

### 2. ICP snap-to-nearest-edge
`Take-Home.tif` is a binary satellite-derived edge map. For each parcel:

1. Load all edge pixels within 80m of the official boundary
2. Build a KD-tree
3. Densify boundary points, then snap each to nearest edge within 80m
4. Take median displacement across valid points -> `(dx, dy)` in metres
5. Re-validate shifted geometry with a tight 8m check

Position is always from imagery (ICP), never from neighbours.

### 3. Confidence model
`h1_score` combines independent evidence:

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

### 5. Anchor propagation and iterative reassessment

- L1 anchors: provided truths (trust=1.0)
- L2 anchors: strong imagery + area-consistent parcels (trust=0.8)
- Reassess flagged parcels iteratively, promote new anchors, repeat until convergence or max passes

This adds constraint propagation while preserving imagery-only geometry updates.

## Results (public example truths, Vadnerbhairav)

| Metric | Official | Ours |
|---|---|---|
| Median IoU | 0.612 | **0.794** |
| Improvement | - | **+0.211** |
| Accurate @ IoU≥0.5 | — | **100%** |
| Centroid error | — | **8.7m** |
| Calibration ρ | — | 0.40 (4 pts, not meaningful) |

Note: corrected/flagged totals depend on current thresholds and iterative pass settings.

## What I learned from the data

**dy is consistent, dx is not.** Southward drift is relatively stable, while east-west drift varies locally.

**FFT failed, ICP worked.** Cross-correlation did not produce stable peaks in this landscape; ICP on binary edge structure was more reliable.

## What I'd do next

1. **Regional displacement field fitting**: replace simple radial trend with a smooth local drift surface.
2. **Hypothesis scoring**: test multiple candidate shifts around ICP estimate and reward clear winners.
3. **Calibration pass**: tune confidence thresholds with held-out labels where available.
