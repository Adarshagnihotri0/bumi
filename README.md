# BhuMe Boundary Alignment — Submission

## Files

| File | Description |
|---|---|
| `solve.py` | The method — run this to produce `predictions.geojson` from a village bundle |
| `predictions.geojson` | Predictions for Vadnerbhairav (2,457 plots) |
| `transcripts/copilot-session.md` | Full AI coding session transcript |

## How to run

```bash
# Install dependencies
python3 -m pip install rasterio geopandas shapely scipy numpy pyproj scikit-image

# Run — expects these files in the same directory:
#   BhuMe Boundary Input.geojson
#   BhuMe Boundary Imagery.tif
#   BhuMe Boundary Take-Home.tif
python3 solve.py
# writes predictions.geojson
```

## Approach

### 1. Triage
Compute `area_ratio = map_area_sqm / (recorded_area_sqm + pot_kharaba_ha × 10,000)`. Plots outside 0.70–1.35 are flagged immediately — their geometry disagrees with the land record and shifting won't fix them.

### 2. ICP snap-to-nearest-edge
`Take-Home.tif` is a binary satellite-derived edge map that aligns with true field boundaries (confirmed: 2–4× more overlap with example truths than with official polygons). For each placement candidate:

1. Load all edge pixels within 80m of the official boundary
2. Build a KD-tree
3. For each boundary vertex, snap to the nearest edge pixel within 80m
4. Take the **median displacement** across all vertices → `(dx, dy)` in metres
5. Apply translation to the geometry

The inlier fraction (how many vertices found a match) becomes the primary confidence signal.

### 3. Confidence
Weighted composite per plot:

| Signal | Weight |
|---|---|
| ICP inlier fraction | 0.45 |
| Shift agrees with neighbourhood drift (IDW surface) | 0.30 |
| Neighbourhood anchors are consistent | 0.15 |
| Area ratio proximity to 1.0 | 0.10 |

Plots are flagged (not corrected) if `confidence < 0.25` or `shift < 1.5m`.

### 4. Anchor propagation (minesweeper)
Example truths seed the anchor pool. Any plot with `inlier_frac ≥ 0.5` and `confidence ≥ 0.5` is added as an anchor. An IDW surface over all anchors gives the expected drift at every point — used both to fill in plots with no edge signal and to penalise ICP matches that disagree with their neighbourhood.

## Results (public example truths, Vadnerbhairav)

| Metric | Official | Ours |
|---|---|---|
| Median IoU | 0.612 | **0.794** |
| Improvement | — | **+0.211** |
| Accurate @ IoU≥0.5 | — | **100%** |
| Centroid error | — | **8.7m** |
| Calibration ρ | — | 0.40 (4 pts, not meaningful) |

1,482 plots corrected · 975 flagged

## What I learned from the data

**dy is consistent, dx is not.** Every plot drifts ~12m southward — safe to apply everywhere. East-west drift flips sign between adjacent plots, suggesting the village was digitised from multiple old cadastral sheets each georeferenced independently. A single global shift can't fix dx; it needs per-plot ICP.

**FFT failed, ICP worked.** Cross-correlation of a boundary ring against satellite edge texture produces no clean peak — agricultural fields have too many competing edges. ICP directly exploits the binary satellite edge map and sidesteps this entirely.

## What I'd do next

1. **Multi-scale ICP**: coarse 80m snap to find the basin, then refine with 15m snap to land precisely on the boundary rather than a nearby road/canal
2. **Sheet boundary detection**: cluster plots by dx shift to identify cadastral map sheet seams; do per-sheet IDW rather than village-wide
3. **Confidence calibration on hidden set**: check whether inlier_frac is genuinely predictive; if not, add local edge density as a prior on whether ICP is possible in that patch
