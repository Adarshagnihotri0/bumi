"""
BhuMe Boundary Alignment Solver

Corrects cadastral map boundaries by snapping to satellite-derived edges.

Pipeline:
  1. Triage      — Skip plots with missing area data; compute area plausibility signal
  2. ICP         — Snap boundary vertices to nearest satellite edge; compute shift
  3. Validation  — Re-check shifted geometry at tight radius; measure actual alignment
  4. Evidence    — Combine 4 independent signals into h1_score
  5. Anchors     — Tiered trust network (L1=truths, L2=strong imagery+area)
  6. Restraint   — Flag if shift too small or confidence too low
  7. Output      — Write corrected positions with confidence scores

Key Design Decisions:
  • Position is ALWAYS from imagery (ICP), never from area or neighbourhood
  • Neighbourhood confidence contributes only to h1_score, not position
  • Area mismatch = both H1 and H2 evidence; measured as continuous signal
  • Post-shift tight validation: boundary must sit ON edges, not just within 80m
  • L2 anchors: promote on imagery+area alone; no bootstrap deadlock on 6 L1 truths
"""

from __future__ import annotations

import json
import math
import warnings
from pathlib import Path

import numpy as np
import rasterio
import rasterio.features
import rasterio.windows
import rasterio.transform
from pyproj import Transformer
from shapely.geometry import shape, mapping
from shapely.affinity import translate
from shapely.ops import transform as shp_transform
from scipy.spatial import cKDTree

warnings.filterwarnings("ignore")


# ════════════════════════════════════════════════════════════════════════════════════
# Configuration
# ════════════════════════════════════════════════════════════════════════════════════

# File paths (data/ folder structure)
DATA_DIR = Path("/Users/adarshagnihotri/land")
INPUT_GEOJSON = DATA_DIR / "data" / "BhuMe Boundary Input.geojson"
TRUTHS_GEOJSON = DATA_DIR / "data" / "BhuMe Boundary Truths.geojson"
IMAGERY_TIF = DATA_DIR / "data" / "BhuMe Boundary Imagery.tif"
BOUNDARY_TIF = DATA_DIR / "data" / "BhuMe Boundary Take-Home.tif"
OUTPUT_GEOJSON = DATA_DIR / "predictions.geojson"

# ICP search radius (metres)
PAD_M = 80.0

# Tight validation radius after shift found (metres)
TIGHT_RADIUS_M = 8.0

# Minimum shift to apply (metres); smaller shifts flag as uncertain
MIN_SHIFT_M = 1.5

# Minimum confidence threshold; below this → flag instead of correct
MIN_CONFIDENCE = 0.25

# Area ratio plausibility band
AREA_RATIO_LO = 0.70  # Outside this range = likely H2 (geometry wrong)
AREA_RATIO_HI = 1.35

# L2 anchor promotion thresholds (stricter than main algorithm)
L2_INLIER_MIN = 0.70    # Strong imagery signal required
L2_RATIO_LO = 0.85      # Tighter area ratio for promoting to anchor
L2_RATIO_HI = 1.15
L2_TRUST = 0.80         # Trust weight (L1 truths = 1.0)

# Second pass reassessment: upgrade flagged → corrected if confidence >= this threshold
# Set BELOW MIN_CONFIDENCE (0.25) so pass 2 is genuinely more lenient; enriched anchor
# network (6 → 385) justifies accepting borderline cases we couldn't trust in pass 1.
REASSESSMENT_THRESHOLD = 0.20

# Second pass: use a lower shift threshold since we have much better anchor coverage
REASSESSMENT_MIN_SHIFT_M = 1.0  # vs 1.5m in pass 1

# Neighbour agreement search radius (metres).
# 600m ≈ 25–30 × typical plot width in this dataset (~20m), giving a neighbourhood
# large enough to hold 10–30 anchors on average while staying sub-village scale.
AGREEMENT_RADIUS_M = 600.0


# ════════════════════════════════════════════════════════════════════════════════════
# Coordinate Transformation Helpers
# ════════════════════════════════════════════════════════════════════════════════════

def make_transformer(src_crs: str, dst_crs: str) -> Transformer:
    """Create a coordinate system transformer."""
    return Transformer.from_crs(src_crs, dst_crs, always_xy=True)


def to_image_crs(geom_4326, img_crs: str):
    """Transform geometry from WGS84 (lon/lat) to image CRS."""
    tf = make_transformer("EPSG:4326", img_crs)
    return shp_transform(lambda xs, ys, z=None: tf.transform(xs, ys), geom_4326)


def get_centroid_lonlat(geom_4326):
    """Get centroid as (lon, lat)."""
    c = geom_4326.centroid
    return c.x, c.y


def haversine_metres(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    """Distance between two points in metres."""
    dlat = (lat2 - lat1) * 111320
    dlon = (lon2 - lon1) * 111320 * math.cos(math.radians((lat1 + lat2) / 2))
    return math.sqrt(dlat**2 + dlon**2)


def apply_shift_metres_to_lonlat(geom_4326, dx_m: float, dy_m: float, ref_lat: float):
    """Apply a shift in metres to a geometry in lon/lat coordinates."""
    dlon = dx_m / (111320 * math.cos(math.radians(ref_lat)))
    dlat = dy_m / 111320
    return translate(geom_4326, dlon, dlat)


# ════════════════════════════════════════════════════════════════════════════════════
# Raster Operations
# ════════════════════════════════════════════════════════════════════════════════════

def clip(val: float, lo: float, hi: float) -> float:
    """Clamp value between lo and hi."""
    return max(lo, min(hi, val))


def read_raster_patch(raster_src, geom_img_crs, pad_m: float):
    """
    Read a raster patch around a geometry with padding.
    
    Returns:
        (array, window_transform) or (None, None) if patch too small
    """
    minx, miny, maxx, maxy = geom_img_crs.bounds
    left = clip(minx - pad_m, raster_src.bounds.left, raster_src.bounds.right)
    bottom = clip(miny - pad_m, raster_src.bounds.bottom, raster_src.bounds.top)
    right = clip(maxx + pad_m, raster_src.bounds.left, raster_src.bounds.right)
    top = clip(maxy + pad_m, raster_src.bounds.bottom, raster_src.bounds.top)

    window = rasterio.windows.from_bounds(left, bottom, right, top, raster_src.transform)
    window = window.round_offsets().round_lengths()
    
    # Ensure minimum viable size
    if window.width < 10 or window.height < 10:
        return None, None
    
    data = raster_src.read(window=window)
    window_transform = raster_src.window_transform(window)
    return data, window_transform


def extract_edge_pixels(edge_data: np.ndarray, window_transform) -> tuple:
    """
    Extract all edge pixels (value==255) and build KD-tree for fast lookup.
    
    Returns:
        (edge_coords, kd_tree, edge_count) or (None, None, 0) if no edges
    """
    rows, cols = np.where(edge_data == 255)
    edge_count = int(len(rows))
    
    if len(rows) == 0:
        return None, None, 0
    
    # Convert pixel coordinates to geospatial coordinates
    xs, ys = rasterio.transform.xy(window_transform, rows, cols)
    coords = np.column_stack([xs, ys])
    
    return coords, cKDTree(coords), edge_count


# ════════════════════════════════════════════════════════════════════════════════════
# geometry helpers
# ════════════════════════════════════════════════════════════════════════════════════

def _transformer(src_crs, dst_crs):
    return Transformer.from_crs(src_crs, dst_crs, always_xy=True)


def _to_img_crs(geom_4326, img_crs):
    tf = _transformer("EPSG:4326", img_crs)
    return shp_transform(lambda xs, ys, z=None: tf.transform(xs, ys), geom_4326)


def _centroid_lonlat(geom_4326):
    c = geom_4326.centroid
    return c.x, c.y


def _dist_m(lon1, lat1, lon2, lat2):
    dlat = (lat2 - lat1) * 111320
    dlon = (lon2 - lon1) * 111320 * math.cos(math.radians((lat1 + lat2) / 2))
    return math.sqrt(dlat**2 + dlon**2)


def _apply_shift_lonlat(geom_4326, dx_m, dy_m, ref_lat):
    """Translate geometry in metres (UTM-equivalent) → lon/lat."""
    dlon = dx_m / (111320 * math.cos(math.radians(ref_lat)))
    dlat = dy_m / 111320
    return translate(geom_4326, dlon, dlat)


def _densify_geom(geom_img_crs, spacing_m: float = 2.0) -> np.ndarray:
    """Return densified exterior sample points spaced ~spacing_m apart.

    Sparse cadastral polygons (4–8 vertices) give the ICP median very few
    samples.  Interpolating every 2 m yields 50–200 points per parcel,
    dramatically stabilising the shift estimate and reducing sensitivity
    to individual vertex outliers.
    """
    def _ring_pts(ring):
        length = ring.length
        if length == 0:
            return np.array(ring.coords)
        n = max(int(length / spacing_m), len(ring.coords))
        distances = np.linspace(0, length, n, endpoint=False)
        pts = [ring.interpolate(d) for d in distances]
        return np.array([(p.x, p.y) for p in pts])

    if hasattr(geom_img_crs, 'exterior'):
        return _ring_pts(geom_img_crs.exterior)
    else:
        parts = [_ring_pts(part.exterior) for part in geom_img_crs.geoms]
        return np.vstack(parts)


# ════════════════════════════════════════════════════════════════════════════════════
# ICP (Iterative Closest Point) Core Algorithm
# ════════════════════════════════════════════════════════════════════════════════════

def _icp_shift(geom_img_crs, edge_kd, edge_coords, max_dist_m, res_m):
    """
    Snap each boundary vertex to nearest edge pixel.
    
    For each vertex:
      1. Query KD-tree for nearest edge within max_dist_m
      2. Compute displacement to that edge
      3. Take median displacement across valid vertices
    
    Returns:
        (dx_m, dy_m, inlier_fraction, failure_mode, displacement_std)
        
    failure_mode:
      'no_signal'  — Edge map empty in this region (heavy canopy/buildings)
      'no_match'   — Edges exist but boundary aligns with none (H2 evidence)
      'ambiguous'  — Vertex displacements widely scattered (multiple competing edges)
      'good'       — Clean, consistent snaps to nearby edges
    """
    if edge_kd is None:
        return 0.0, 0.0, 0.0, 'no_signal', 0.0

    verts = _densify_geom(geom_img_crs, spacing_m=2.0)
    if len(verts) == 0:
        return 0.0, 0.0, 0.0, 'no_signal', 0.0
    dists, idxs = edge_kd.query(verts, distance_upper_bound=max_dist_m)

    valid = dists < max_dist_m
    inlier_frac = float(valid.mean())

    if valid.sum() < 2:
        # edges exist (kd not None) but nothing near the boundary — H2 evidence
        return 0.0, 0.0, inlier_frac, 'no_match', 0.0

    matched_edge = edge_coords[idxs[valid]]
    matched_vert = verts[valid]
    disps = matched_edge - matched_vert

    dx_m = float(np.median(disps[:, 0]))
    dy_m = float(np.median(disps[:, 1]))

    # ambiguous: displacement vectors are widely scattered
    disp_mags = np.hypot(disps[:, 0], disps[:, 1])
    disp_std  = float(np.std(disp_mags))
    shift_mag = math.sqrt(dx_m**2 + dy_m**2)
    if disp_std > max(15.0, shift_mag * 1.5):
        failure_mode = 'ambiguous'
    else:
        failure_mode = 'good'

    return dx_m, dy_m, inlier_frac, failure_mode, float(disp_std)


# ════════════════════════════════════════════════════════════════════════════════════
# Confidence Signals: Neighbourhood Agreement & Imagery Quality
# ════════════════════════════════════════════════════════════════════════════════════

def compute_area_ratio_score(map_area: float, rec_area: float, pot_kharaba_ha: float) -> float:
    """
    Compute plausibility that map geometry matches recorded extent.
    
    Challenge spec: full parcel = recorded_area + pot_kharaba_area
    So area_ratio = map_area / (recorded_area + pot_kharaba_ha * 10000)
    
    Returns continuous score in [0, 1]:
      1.0     — Plausible H1 (0.90–1.10)
      0.3–0.9 — Suspicious zone (0.70–1.35)
      0.0     — Outside range or invalid data; likely H2 (geometry fundamentally wrong)
    """
    pot_kh_sqm = pot_kharaba_ha * 10000
    total_rec = rec_area + pot_kh_sqm

    if total_rec <= 0 or map_area <= 0:
        return 0.0  # Invalid data → treat as H2 evidence

    ratio = map_area / total_rec

    # Core plausibility zone (clearly H1)
    if 0.90 <= ratio <= 1.10:
        return 1.0

    # Suspicious zone with gradient (could be H1 or H2)
    if AREA_RATIO_LO <= ratio <= AREA_RATIO_HI:
        if ratio < 0.90:
            # Scale from 0.3 (at 0.70) to 1.0 (at 0.90)
            return 0.3 + 0.7 * (ratio - AREA_RATIO_LO) / (0.90 - AREA_RATIO_LO)
        else:
            # Scale from 1.0 (at 1.10) to 0.3 (at 1.35)
            return 0.3 + 0.7 * (AREA_RATIO_HI - ratio) / (AREA_RATIO_HI - 1.10)

    # Outside band = H2 evidence
    return 0.0


def _zscore_agreement(dx_m, dy_m, query_lon, query_lat,
                      anchor_lons, anchor_lats, anchor_dxs, anchor_dys,
                      anchor_trusts=None, radius_m=AGREEMENT_RADIUS_M):
    """Continuous agreement score: how unusual is (dx, dy) vs nearby anchors?

    Returns a value in [0, 1]:
      1.0 = shift is exactly what nearby anchors predict
      0.5 = no anchors nearby (neutral — don't penalise, don't reward)
      0.0 = shift is a major outlier vs neighbours

    anchor_trusts weights each anchor's contribution (L1=1.0, L2=0.8).
    Weighted median and weighted variance are used so high-trust anchors
    (ground-truth L1) have stronger pull on the reference displacement field.
    """
    trusts = anchor_trusts if anchor_trusts is not None else [1.0] * len(anchor_lons)
    nearby_dx, nearby_dy, nearby_w = [], [], []
    for alon, alat, adx, ady, atr in zip(anchor_lons, anchor_lats, anchor_dxs, anchor_dys, trusts):
        if _dist_m(query_lon, query_lat, alon, alat) <= radius_m:
            nearby_dx.append(adx)
            nearby_dy.append(ady)
            nearby_w.append(atr)

    if len(nearby_dx) < 3:
        return 0.5  # neutral — network too sparse here yet

    # Trust-weighted median: sort by value, find where cumulative weight crosses 50%
    def _wmedian(vals, wts):
        order = np.argsort(vals)
        sv = np.array(vals)[order]
        sw = np.array(wts)[order]
        cum = np.cumsum(sw)
        idx = np.searchsorted(cum, cum[-1] * 0.5)
        return float(sv[min(idx, len(sv) - 1)])

    med_dx = _wmedian(nearby_dx, nearby_w)
    med_dy = _wmedian(nearby_dy, nearby_w)

    # Trust-weighted standard deviation
    w = np.array(nearby_w)
    dx_arr = np.array(nearby_dx)
    dy_arr = np.array(nearby_dy)
    std_dx = float(np.sqrt(np.sum(w * (dx_arr - med_dx) ** 2) / np.sum(w))) + 1e-3
    std_dy = float(np.sqrt(np.sum(w * (dy_arr - med_dy) ** 2) / np.sum(w))) + 1e-3

    z = math.sqrt(((dx_m - med_dx) / std_dx) ** 2 + ((dy_m - med_dy) / std_dy) ** 2)
    # z=0 → score=1.0;  z=2 → score≈0.37;  z=4 → score≈0.02
    return float(math.exp(-z / 2.0))


# ════════════════════════════════════════════════════════════════════════════════════
# H1 Score: Joint Evidence of Misplacement (Not Geometry Error)
# ════════════════════════════════════════════════════════════════════════════════════

def compute_h1_score(icp_quality: float, agreement: float, imagery_signal: float,
                    area_ratio_score: float, failure_mode: str) -> float:
    """
    Bayesian combination of 4 independent confidence signals:
      • icp_quality (35%) — Does boundary sit on edges after correction?
      • agreement (25%)   — Does shift match neighbourhood drift?
      • imagery_signal (20%) — Is there usable edge data in this region?
      • area_ratio (20%)  — Does map size match record extent?
    
    Penalties for failure modes:
      • no_match  ×0.30  — Boundary aligns with no edges (H2)
      • ambiguous ×0.60  — Multiple competing edges (uncertain)
    
    Position is ALWAYS from imagery. These signals affect confidence only.
    """
    h1_score = (
        0.35 * icp_quality +    # ICP quality
        0.25 * agreement +      # Neighbourhood agreement
        0.20 * imagery_signal + # Edge density
        0.20 * area_ratio_score # Area plausibility
    )

    # Apply failure mode penalties
    if failure_mode == 'no_match':
        h1_score *= 0.30  # Strong H2 evidence
    elif failure_mode == 'ambiguous':
        h1_score *= 0.60  # Uncertainty

    return float(np.clip(h1_score, 0.0, 1.0))


# ════════════════════════════════════════════════════════════════════════════════════
# Re-assessment Helper (for second pass)
# ════════════════════════════════════════════════════════════════════════════════════

def reassess_plot(pn, geom, props, img_crs, edge_src, 
                  anchor_lons, anchor_lats, anchor_dxs, anchor_dys, anchor_trusts):
    """
    Re-evaluate a single plot using current anchor pool.
    
    Returns: (h1_score, dx_m, dy_m, inlier_frac, failure_mode, can_upgrade)
    can_upgrade = True if h1_score >= REASSESSMENT_THRESHOLD
    """
    map_area = props.get("map_area_sqm") or 0
    rec_area = props.get("recorded_area_sqm") or 0
    pot_kh = props.get("pot_kharaba_ha") or 0
    
    geom_img = _to_img_crs(geom, img_crs)
    clon, clat = geom.centroid.x, geom.centroid.y
    
    try:
        edge_data, edge_win_tf = read_raster_patch(edge_src, geom_img, PAD_M)
        if edge_data is None:
            return 0.0, 0.0, 0.0, 0.0, 'no_signal', False
        
        edge_data = edge_data[0]
        edge_coords, edge_kd, edge_count = extract_edge_pixels(edge_data, edge_win_tf)
        
        dx_m, dy_m, inlier_frac, failure_mode, disp_std = _icp_shift(
            geom_img, edge_kd, edge_coords, max_dist_m=PAD_M, res_m=edge_src.res[0]
        )
        
        # Post-shift tight validation
        if failure_mode in ('good', 'ambiguous') and (abs(dx_m) + abs(dy_m)) > 0:
            shifted_geom_4326 = _apply_shift_lonlat(geom, dx_m, dy_m, clat)
            shifted_geom_img = _to_img_crs(shifted_geom_4326, img_crs)
            _, _, tight_inlier, _, _ = _icp_shift(
                shifted_geom_img, edge_kd, edge_coords, max_dist_m=TIGHT_RADIUS_M, res_m=edge_src.res[0]
            )
            inlier_frac = tight_inlier
        
        imagery_signal = min(1.0, edge_count / 80.0)
        area_ratio_score = compute_area_ratio_score(map_area, rec_area, pot_kh)
        agreement = _zscore_agreement(
            dx_m, dy_m, clon, clat,
            anchor_lons, anchor_lats, anchor_dxs, anchor_dys, anchor_trusts
        )
        
        icp_quality = inlier_frac if failure_mode != 'no_signal' else 0.0
        h1_score = compute_h1_score(icp_quality, agreement, imagery_signal, area_ratio_score, failure_mode)
        
        # Check if passes reassessment threshold
        can_upgrade = h1_score >= REASSESSMENT_THRESHOLD
        
        return h1_score, dx_m, dy_m, inlier_frac, failure_mode, can_upgrade
    
    except Exception:
        return 0.0, 0.0, 0.0, 0.0, 'no_signal', False


# ════════════════════════════════════════════════════════════════════════════════════
# Main Processing Pipeline
# ════════════════════════════════════════════════════════════════════════════════════

def main():
    """Load data, process all plots, output predictions."""
    
    print("Loading data...")
    with open(INPUT_GEOJSON) as f:
        inputs = json.load(f)
    with open(TRUTHS_GEOJSON) as f:
        truths = json.load(f)

    truth_idx = {str(feat["properties"]["plot_number"]): feat for feat in truths["features"]}

    img_src = rasterio.open(IMAGERY_TIF)
    edge_src = rasterio.open(BOUNDARY_TIF)
    img_crs = str(img_src.crs)

    res_m = img_src.res[0]         # ~1.19 m/px
    edge_res_m = edge_src.res[0]   # ~2.39 m/px

    print(f"Imagery:  {img_src.width}x{img_src.height} px @ {res_m:.2f} m/px")
    print(f"Edges:    {edge_src.width}x{edge_src.height} px @ {edge_res_m:.2f} m/px")
    print(f"Plots:    {len(inputs['features'])}")
    print()

    # ── Seed L1 anchors from provided truths ──────────────────────────────────
    anchor_lons, anchor_lats = [], []
    anchor_dxs, anchor_dys = [], []
    anchor_trusts = []

    for feat in truths["features"]:
        pn = str(feat["properties"]["plot_number"])
        for inp in inputs["features"]:
            if str(inp["properties"]["plot_number"]) == pn:
                t_geom = shape(feat["geometry"])
                o_geom = shape(inp["geometry"])
                tlon, tlat = t_geom.centroid.x, t_geom.centroid.y
                olon, olat = o_geom.centroid.x, o_geom.centroid.y
                dx = (tlon - olon) * 111320 * math.cos(math.radians(tlat))
                dy = (tlat - olat) * 111320
                anchor_lons.append(olon)
                anchor_lats.append(olat)
                anchor_dxs.append(dx)
                anchor_dys.append(dy)
                anchor_trusts.append(1.0)
                break

    print(f"L1 anchors (ground truth): {len(anchor_lons)}")

    # ── Process every plot ────────────────────────────────────────────────────
    results = []
    l2_queue = []  # (clon, clat, dx_m, dy_m) — promote after first pass

    total = len(inputs["features"])
    for i, feat in enumerate(inputs["features"]):
        if i % 200 == 0:
            print(f"  [{i:4d}/{total}] processing...")

        pn = str(feat["properties"]["plot_number"])
        geom = shape(feat["geometry"])
        props = feat["properties"]

        clon, clat = geom.centroid.x, geom.centroid.y

        # ── Triage: Area Ratio ────────────────────────────────────────────────
        map_area = props.get("map_area_sqm") or 0
        rec_area = props.get("recorded_area_sqm") or 0
        pot_kh = props.get("pot_kharaba_ha") or 0

        area_ratio = map_area / (rec_area + pot_kh * 10000) if (rec_area + pot_kh * 10000) > 0 else None

        if area_ratio is None:
            results.append({
                "plot_number": pn,
                "status": "flagged",
                "confidence": None,
                "method_note": "null recorded area",
                "geometry": feat["geometry"],
            })
            continue

        area_ratio_score = compute_area_ratio_score(map_area, rec_area, pot_kh)

        # ── ICP Alignment ─────────────────────────────────────────────────────
        geom_img = _to_img_crs(geom, img_crs)

        try:
            edge_data, edge_win_tf = read_raster_patch(edge_src, geom_img, PAD_M)
            if edge_data is None:
                raise ValueError("empty patch")

            edge_data = edge_data[0]
            edge_coords, edge_kd, edge_count = extract_edge_pixels(edge_data, edge_win_tf)

            # Initial ICP at wide radius (find shift)
            dx_m, dy_m, inlier_frac, failure_mode, disp_std = _icp_shift(
                geom_img, edge_kd, edge_coords, max_dist_m=PAD_M, res_m=edge_res_m
            )

            # Post-shift tight validation (measure actual alignment)
            if failure_mode in ('good', 'ambiguous') and (abs(dx_m) + abs(dy_m)) > 0:
                shifted_geom_4326 = _apply_shift_lonlat(geom, dx_m, dy_m, clat)
                shifted_geom_img = _to_img_crs(shifted_geom_4326, img_crs)
                _, _, tight_inlier, _, _ = _icp_shift(
                    shifted_geom_img, edge_kd, edge_coords, max_dist_m=TIGHT_RADIUS_M, res_m=edge_res_m
                )
                inlier_frac = tight_inlier  # Replace with tight validation result

            # Imagery signal: proxy for field visibility
            imagery_signal = min(1.0, edge_count / 80.0)

        except Exception as e:
            results.append({
                "plot_number": pn,
                "status": "flagged",
                "confidence": None,
                "method_note": f"icp_error: {e}",
                "geometry": feat["geometry"],
            })
            continue

        shift_m = math.sqrt(dx_m**2 + dy_m**2)

        # ── Confidence Signals ────────────────────────────────────────────────
        agreement = _zscore_agreement(
            dx_m, dy_m, clon, clat,
            anchor_lons, anchor_lats, anchor_dxs, anchor_dys, anchor_trusts
        )

        # ── H1 Score: Joint Evidence ──────────────────────────────────────────
        icp_quality = inlier_frac if failure_mode != 'no_signal' else 0.0
        h1_score = compute_h1_score(icp_quality, agreement, imagery_signal, area_ratio_score, failure_mode)

        # ── L2 Anchor Promotion (strong imagery + area) ────────────────────────
        if (failure_mode == 'good' and inlier_frac >= L2_INLIER_MIN
                and L2_RATIO_LO <= area_ratio <= L2_RATIO_HI):
            l2_queue.append((clon, clat, dx_m, dy_m))

        # ── Restraint: Flag if uncertain or minimal shift ─────────────────────
        if shift_m < MIN_SHIFT_M or h1_score < MIN_CONFIDENCE:
            flag_reason = (
                f"area_ratio={area_ratio:.2f}"
                if area_ratio_score == 0.0
                else f"shift={shift_m:.1f}m h1={h1_score:.2f} mode={failure_mode}"
            )
            results.append({
                "plot_number": pn,
                "status": "flagged",
                "confidence": None,
                "method_note": flag_reason,
                "geometry": feat["geometry"],
            })
            continue

        # ── Output: Corrected ─────────────────────────────────────────────────
        new_geom = _apply_shift_lonlat(geom, dx_m, dy_m, clat)
        results.append({
            "plot_number": pn,
            "status": "corrected",
            "confidence": round(h1_score, 3),
            "method_note": f"icp dx={dx_m:.1f}m dy={dy_m:.1f}m inlier={inlier_frac:.2f} mode={failure_mode} h1={h1_score:.2f}",
            "geometry": mapping(new_geom),
        })

    img_src.close()
    edge_src.close()

    # ── Commit L2 anchors (deduplicated) ──────────────────────────────────────
    seen = set()
    for alon, alat, adx, ady in l2_queue:
        # 100m grid cell: prevents cluster self-reinforcement where a
        # locally-correlated ICP mistake gets amplified via dense neighbours.
        # round(_, 3) ≈ 0.001° ≈ 111 m per cell.
        key = (round(alon, 3), round(alat, 3))
        if key not in seen:
            seen.add(key)
            anchor_lons.append(alon)
            anchor_lats.append(alat)
            anchor_dxs.append(adx)
            anchor_dys.append(ady)
            anchor_trusts.append(L2_TRUST)

    l1_count = sum(1 for t in anchor_trusts if t == 1.0)
    l2_count = sum(1 for t in anchor_trusts if t == L2_TRUST)
    print(f"\nDone. Anchor pool: {l1_count} L1 + {l2_count} L2 = {len(anchor_lons)} total")

    corrected = [r for r in results if r["status"] == "corrected"]
    flagged = [r for r in results if r["status"] == "flagged"]
    print(f"  corrected: {len(corrected)}")
    print(f"  flagged:   {len(flagged)}")
    if corrected:
        confs = [r["confidence"] for r in corrected]
        print(f"  confidence: min={min(confs):.2f}  median={sorted(confs)[len(confs)//2]:.2f}  max={max(confs):.2f}")

    # ── Second Pass: Re-assess flagged plots with enriched anchor pool ─────────
    print(f"\n[PASS 2] Re-assessing {len(flagged)} flagged plots with enriched anchor network...")
    
    # Analyze why plots are flagged
    small_shift_count = 0
    low_conf_count = 0
    for flag_result in flagged:
        note = flag_result.get("method_note", "")
        if "shift=" in note and ("h1=" in note):
            # Extract shift and h1 from note
            try:
                shift_str = note.split("shift=")[1].split("m")[0]
                shift_val = float(shift_str)
                h1_str = note.split("h1=")[1].split()[0]
                h1_val = float(h1_str)
                if shift_val < MIN_SHIFT_M:
                    small_shift_count += 1
                if h1_val < MIN_CONFIDENCE:
                    low_conf_count += 1
            except:
                pass
        elif "area_ratio=" in note:
            small_shift_count += 1  # flagged for area ratio = small decision
    
    print(f"  flagged reasons: ~{small_shift_count} small shift, ~{low_conf_count} low confidence")
    
    upgraded = 0
    upgraded_details = {"conf_improved": 0}
    confidence_deltas: list[float] = []
    
    for i, flag_result in enumerate(flagged):
        if i % 100 == 0 and i > 0:
            print(f"  [{i}/{len(flagged)}] re-assessing...")
        
        pn = flag_result["plot_number"]
        # Find original input geometry
        orig_feat = None
        for feat in inputs["features"]:
            if str(feat["properties"]["plot_number"]) == pn:
                orig_feat = feat
                break
        
        if orig_feat is None:
            continue
        
        geom = shape(orig_feat["geometry"])
        props = orig_feat["properties"]
        
        h1_score, dx_m, dy_m, inlier_frac, failure_mode, can_upgrade = reassess_plot(
            pn, geom, props, img_crs, edge_src,
            anchor_lons, anchor_lats, anchor_dxs, anchor_dys, anchor_trusts
        )
        
        shift_m = math.sqrt(dx_m**2 + dy_m**2)
        
        # Track confidence delta (pass2_h1 - pass1_h1) for all reassessed plots
        try:
            note = flag_result.get("method_note", "")
            p1_h1 = float(note.split("h1=")[1].split()[0])
            confidence_deltas.append(h1_score - p1_h1)
        except (IndexError, ValueError):
            pass
        
        # If confidence now meets threshold, upgrade from flagged → corrected
        # can_upgrade already encodes h1_score >= REASSESSMENT_THRESHOLD (0.20)
        if can_upgrade and shift_m >= REASSESSMENT_MIN_SHIFT_M:
            clon, clat = geom.centroid.x, geom.centroid.y
            new_geom = _apply_shift_lonlat(geom, dx_m, dy_m, clat)
            
            # Update the result from flagged → corrected
            old_note = flag_result.get("method_note", "")
            flag_result["status"] = "corrected"
            flag_result["confidence"] = round(h1_score, 3)
            flag_result["method_note"] = f"[PASS2] icp dx={dx_m:.1f}m dy={dy_m:.1f}m inlier={inlier_frac:.2f} mode={failure_mode} h1={h1_score:.2f} (from: {old_note})"
            flag_result["geometry"] = mapping(new_geom)
            upgraded += 1
            upgraded_details["conf_improved"] += 1
    
    print(f"  upgraded: {upgraded} plots from flagged → corrected")
    if upgraded > 0:
        print(f"    - confidence improved: {upgraded_details['conf_improved']}")
    
    # Confidence delta analysis: did pass 2 reveal new evidence or just confirm uncertainty?
    if confidence_deltas:
        deltas = sorted(confidence_deltas)
        n = len(deltas)
        median_d = deltas[n // 2]
        p95_d = deltas[int(n * 0.95)]
        max_d = deltas[-1]
        print(f"  confidence_delta (pass2 - pass1): median={median_d:+.3f}  p95={p95_d:+.3f}  max={max_d:+.3f}")
        if max_d < 0.05:
            print("  → Pass 2 confirmed uncertainty rather than revealing new evidence.")
        else:
            print(f"  → {sum(d > 0.05 for d in deltas)} plots gained meaningful confidence in pass 2.")
    
    # Update counts
    corrected = [r for r in results if r["status"] == "corrected"]
    flagged = [r for r in results if r["status"] == "flagged"]
    print(f"\nFinal counts after 2 passes:")
    print(f"  corrected: {len(corrected)}")
    print(f"  flagged:   {len(flagged)}")
    if corrected:
        confs = [r["confidence"] for r in corrected]
        print(f"  confidence: min={min(confs):.2f}  median={sorted(confs)[len(confs)//2]:.2f}  max={max(confs):.2f}")

    # ── Write output ──────────────────────────────────────────────────────────
    features = []
    for r in results:
        feat = {
            "type": "Feature",
            "geometry": r["geometry"],
            "properties": {
                "plot_number": r["plot_number"],
                "status": r["status"],
                "method_note": r.get("method_note", ""),
            }
        }
        if r["status"] == "corrected":
            feat["properties"]["confidence"] = r["confidence"]
        features.append(feat)

    out = {"type": "FeatureCollection", "features": features}
    with open(OUTPUT_GEOJSON, "w") as f:
        json.dump(out, f)

    print(f"\nWritten → {OUTPUT_GEOJSON}")


if __name__ == "__main__":
    main()
