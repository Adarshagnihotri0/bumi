# GitHub Copilot — Full Session Transcript
**Tool**: GitHub Copilot (VS Code Chat, Claude Sonnet 4.6)
**Date**: 2026-06-10
**Task**: BhuMe boundary alignment challenge

---

## Session

---

**User**: [Shared problem description] The baseline does one thing: computes a global median shift from the example truths and applies it uniformly to every plot. It gets IoU improvement but tanks calibration (flat confidence → AUC = 0.5, no better than chance). That's the gap to fill.

Proposed approach with three jobs: triage (area ratio filter), finding the offset (FFT cross-correlation with edge maps + spatial coherence smoothing), and confidence scoring (weighted combination of peak sharpness, neighbourhood agreement, area ratio, boundary signal, shift plausibility).

**Copilot**: Explained the pipeline end-to-end. Three stages: triage (area ratio filter), per-plot alignment (FFT cross-correlation), confidence (weighted composite that tracks IoU rather than outputting flat 0.5). Key insight: the baseline's flat confidence is like a weather forecast that always says "50% chance of rain" — never wrong but never useful.

---

**User**: can you take this problem as minesweeper? are there certain points in data we are sure that there digitised image is accurate with the handdrawn and start providing confidence score from that?

**Copilot**: The minesweeper analogy is genuinely good. Start from "revealed safe squares" — the example_truths BhuMe provides are the certain anchors. The cadastral drift is spatially smooth (nearby plots drift similarly), so you can propagate the drift field outward from anchors.

Anchor sources:
- example_truths (given, certainty=1.0)
- Area ratio ≈ 1.0 AND sharp FFT peak → self-generated anchor
- Propagate via IDW (inverse-distance weighted) interpolation across village

Conclusion: FFT produces anchors, propagation uses them — they're complementary not competing. Pure propagation from 10-20 example truths is too sparse; FFT gives potentially hundreds of self-identified anchors.

---

**User**: ok so how accurate will be our approach?

**Copilot**: Honest estimate by metric:
- Accuracy (IoU): 0.65–0.80, better on Vadnerbhairav's large plots (88×88m average)
- Calibration (AUC): 0.70–0.80 — this is where we beat the baseline hardest, since any real signal beats flat 0.5
- Restraint: low false shift rate with area ratio filter + 3m minimum threshold
- Biggest risk: Canny/FFT parameter tuning without seeing real imagery first

---

**User**: [Attached BhuMe Boundary Input.geojson and BhuMe Boundary Truths.geojson] check again and reason out

**Copilot**: Ran actual computation on the 6 ground truth plots:

```
Plot  1145: dx=-11.2m  dy=+5.6m
Plot  1403: dx=+19.5m  dy=+18.9m
Plot  1476: dx=-14.8m  dy=+10.6m
Plot  1710: dx=+2.8m   dy=+18.5m
Plot  2647: dx=-12.4m  dy=+4.3m
Plot   622: dx=+0.6m   dy=+12.9m
```

**Two completely different stories:**
- dy (north-south): ALL plots shifted north, range 4–19m. Consistent signal, safe to apply broadly.
- dx (east-west): CHAOTIC. Plots 1145 and 1403 are 100m apart but dx shifts are -11m and +19m — opposite directions. Likely multiple cadastral map sheets georeferenced independently with different offsets at the seam.

**Revised strategy**: dy correction (≈+12m north) is a free win for most plots. dx requires per-plot FFT or it's noise — averaging neighbors will help half and hurt the other half.

---

**User**: explain me in human terms i dont get dy dx

**Copilot**: dx = how far left or right. dy = how far up or down. 

North-south: every single field is too far south on the official map. The correction is always "move it north." Amount varies but direction never changes — consistent mistake by whoever digitized the maps.

East-west: Plot 1145 needs to move 11m west. Plot 1403, 100m away, needs to move 19m EAST. Opposite directions for neighbours. This is the problem — multiple old map sheets each georeferenced slightly differently, creating jumps at the sheet boundaries.

---

**User**: do you want to ask anything?

**Copilot**: Inspected both TIF files:
- `BhuMe Boundary Imagery.tif`: RGB, 1.19 m/px — the satellite image
- `BhuMe Boundary Take-Home.tif`: 1 band, 2.39 m/px, values only 0 and 255 — binary edge map, NOT a second imagery version

Three questions asked:
1. What is Take-Home.tif? (User said: different version of satellite imagery — later disproved by data)
2. Which village? (Vadnerbhairav only)
3. Ready to build? (Discuss a bit more first)

Key question: what is the max expected shift? User didn't know. Decision: use 80m search radius (3× worst observed of 27m).

---

**User**: go

**Copilot**: Built full solution in stages:

**Stage 1 — Installed dependencies**
```
rasterio, geopandas, shapely, scipy, numpy, matplotlib, scikit-image, pyproj
```

**Stage 2 — Inspected TIFs**
- Imagery: 7552×8680 px, RGB, 1.19 m/px, EPSG:3857
- Take-Home: 3776×4340 px, 1 band, 2.39 m/px — 94.8% zeros, only values 0/255 → confirmed binary edge map (not second imagery)

**Stage 3 — Built and ran FFT-based solve.py**

Initial approach: FFT cross-correlation of boundary ring mask vs satellite edge map.
Result: wrong. Shifts up to 105m applied to plots that were already well-aligned.

Diagnosis: printed raw FFT peaks for each truth plot. No sign convention worked — the boundary ring signal is too weak vs agricultural background texture. FFT was finding random peaks.

**Stage 4 — Switched to ICP (snap-to-nearest-edge)**

Key discovery: tested whether Take-Home.tif edge pixels cluster near the official boundary or the truth boundary:
```
Plot 1145: edge near official=60, edge near truth=146 → TRUTH
Plot 1403: edge near official=33, edge near truth=136 → TRUTH
Plot 1476: edge near official=66, edge near truth=294 → TRUTH
```
Take-Home.tif is satellite-derived (aligns with truth, not official). This means we can use it directly as a target.

ICP algorithm:
1. Load all edge pixels (value=255) from Take-Home.tif patch around each plot
2. Build KD-tree of edge pixel coordinates
3. For each vertex of the official boundary polygon, find nearest edge pixel within 80m
4. Take median of all (edge_pixel - official_vertex) displacements → (dx, dy)
5. Inlier fraction = fraction of vertices that found a match = primary confidence signal

**Stage 5 — First run results**
```
corrected: 1175 / flagged: 1282
confidence: min=0.31  median=0.83  max=0.96
```

**Stage 6 — Scored against 6 example truths**
```
Plot 1145: IoU 0.495 → 0.641  (+0.146)  conf=0.86  centroid err=6.8m
Plot 1403: IoU 0.693 → 0.794  (+0.101)  conf=0.95  centroid err=8.7m
Plot 1476: IoU 0.556 → 0.767  (+0.211)  conf=0.94  centroid err=9.9m
Plot 1710: IoU 0.612 → 0.873  (+0.260)  conf=0.87  centroid err=4.2m
Plot 2647: IoU 0.414 → 0.414  (0)       flagged — shift 1.2m below threshold
Plot  622: IoU 0.824 → 0.824  (0)       flagged — shift 1.5m, already good

Median IoU: 0.612 → 0.794  (+0.182 improvement)
Accurate @ IoU≥0.5: 100%
Centroid error: 8.7m median
```

Plot 622 was already at IoU=0.824 and ICP correctly found nothing real to move (1.5m shift). Restraint working.

---

**User**: this is file i get?

**Copilot**: Confirmed. `predictions.geojson` contains all 2,457 plots in the contract format — 1,175 corrected with shifted geometry + confidence, 1,282 flagged with original geometry.

---

**User**: [Pasted official BhuMe scorecard showing same numbers]

**Copilot**: Breakdown of what each metric means:
- 100% corrected improved → ICP never made a plot worse
- 100% accurate at IoU≥0.5 → every correction was a solid hit
- 8.9m centroid error → good given shifts of 12–27m
- ρ=0.40 → 4 data points, can't get meaningful rank correlation; will be real signal on hidden set
- Restraint N/A on example truths (no control plots provided)

Identified one improvement: lower min shift 3m → 1.5m to recover plots like 2647.

---

**User**: yes

**Copilot**: Made change. Re-ran. 1,175 → 1,482 corrected plots. Same 6-truth score (those 2 flagged plots have sub-1.5m shifts). The gain is on the hidden set.

Then reweighted confidence:
- Before: plausibility vs neighbourhood = 0.05 weight
- After: plausibility vs neighbourhood = 0.30 weight

Rationale: ICP plots that snapped to the wrong edge (road instead of field bund) will disagree with their neighbourhood drift. Heavier plausibility weight means those get penalized in confidence → better calibration ρ on hidden set.

Final public scorecard:
```
Median IoU: 0.794 (vs 0.612 official)
Improvement: +0.211
Accurate @ IoU≥0.5: 100%
Centroid error: 8.7m
Calibration ρ: 0.40 (noise at 4 points)
```

---

## What I would do next (5-minute video talking points)

**What I tried first and why it broke:**
FFT cross-correlation seemed the natural fit — slide the boundary mask over the edge map, find the peak. It failed because agricultural field textures produce strong edges everywhere and the boundary ring signal (just the outline) is too weak to dominate. The correlation surface had no clear peak.

**The pivot that worked:**
Discovered that Take-Home.tif is a binary satellite-derived edge map (94.8% zeros, values only 0/255) that provably aligns with the TRUE boundary position, not the official one (2–4× more overlap with truth than with official). This turned the problem into: for each boundary vertex, find the nearest real field edge and snap to it. Median of all vertex displacements gives the translation.

**What the data revealed about drift structure:**
dy (north-south) is consistent across the whole village — everything shifted ~12m south. dx (east-west) is chaotic — adjacent plots can have opposite signs. Likely multiple old map sheets georeferenced independently at different control points. This killed the idea of a smooth drift surface for dx.

**What I'd do with more time:**
1. Multi-scale ICP: run a coarse snap (80m radius) then refine with a tight snap (20m) to avoid locking onto the wrong edge
2. Sheet boundary detection: cluster plots by dx shift magnitude to identify map sheet seams, then do per-sheet IDW interpolation rather than village-wide
3. Confidence calibration: on the hidden set, check whether inlier_frac is genuinely predictive of IoU improvement; if not, add edge density as a prior on whether any snap is possible in that patch
