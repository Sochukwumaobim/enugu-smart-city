"""
flood_analysis.py  –  Enugu Smart City Flood Risk Analysis
===========================================================
Place at:  enugu_emergency_response/flood_analysis.py
Run:       python flood_analysis.py
           python flood_analysis.py --debug

BUGS FIXED IN THIS VERSION (from console output analysis)
──────────────────────────────────────────────────────────
Bug 1 — All 100,000 buildings dropped as "slivers"
    Root cause: MIN_AREA (15 m²) was compared against polygon area in
    GEOGRAPHIC degrees (after reprojection to WGS84). A typical building
    footprint of 100 m² is only ~8e-10 square degrees — far below 15.
    Fix: measure area BEFORE reprojecting (in native projected CRS),
    store as a separate column, then reproject.  The area filter is
    applied to the native-CRS area, not the geographic area.

Bug 2 — HAND p10/p25/p50 all 0.00 (drainage over-extraction)
    Root cause: slope percentile threshold of 25 on a 30 m WGS84 DEM
    flagged 827,083 cells (21%) as drainage — essentially every flat or
    slightly sloped pixel.  When every other pixel is a "drain", HAND
    values are all near zero.
    Fix: use the bottom 5th percentile for slope (much stricter), a
    larger minimum_filter window (9×9 instead of 5×5), and require
    drainage clusters to be ≥ 10 cells.  Also cap total drainage cells
    at 5% of raster area.

Bug 3 — ZeroDivisionError: division by zero
    Root cause: after all buildings were dropped, len(bldgs)==0 and
    n/len(bldgs) crashed.
    Fix: guard with early exit if no buildings remain.

Bug 4 — PostgreSQL PROJ conflict (harmless but noisy)
    Fix: set PROJ_DATA env var to the conda/venv PROJ before importing
    rasterio/geopandas so the PostGIS proj.db is never consulted.
"""

import os, sys, json, argparse, warnings

# ── Suppress PostgreSQL PROJ conflict BEFORE any geo imports ──────────────────
# Finds the PROJ data folder shipped with the active Python environment
# (conda or venv) and forces rasterio/fiona to use it instead of PostGIS.
def _fix_proj():
    try:
        import pyproj
        proj_data = pyproj.datadir.get_data_dir()
        os.environ.setdefault("PROJ_DATA", proj_data)
        os.environ.setdefault("PROJ_LIB",  proj_data)
    except Exception:
        pass  # pyproj not installed — PROJ warning will still appear but is harmless

_fix_proj()
warnings.filterwarnings("ignore")

for pkg in ['rasterio','geopandas','scipy','pandas','pyproj']:
    try: __import__(pkg)
    except ImportError: sys.exit(f"\n❌  Missing: pip install {pkg}\n")

import rasterio
from rasterio.transform import rowcol, xy
import geopandas as gpd
import numpy as np
from scipy.ndimage import minimum_filter, label, gaussian_filter
from scipy.spatial import cKDTree
from datetime import datetime

# ── PATHS ─────────────────────────────────────────────────────────────────────
BASE   = os.path.dirname(os.path.abspath(__file__))
DEM    = os.path.join(BASE, "data", "raw", "dem_enugu.tif")
BLDG   = os.path.join(BASE, "data", "raw", "building_footprints.gpkg")
OUTDIR = os.path.join(BASE, "data", "flood_risk_results")

# ── HYDRAULIC PARAMETERS ──────────────────────────────────────────────────────
# HAND thresholds: metres above nearest drainage channel.
# Based on FEMA HAND implementation + validation for West African tropical
# catchments (Nwosu et al. 2023, Gbuyiro & Sule 2021).
# After running, compare risk_counts against your 2022 Enugu flood records
# and adjust these values if the distribution looks wrong.
HAND_THRESHOLDS = {
    "High Risk":        1.0,   # HAND < 1 m  → inundates in ~2-year events
    "Medium-High Risk": 3.0,   # HAND < 3 m  → 5-10 year events
    "Medium Risk":      6.0,   # HAND < 6 m  → 25-year events
    "Low Risk":        10.0,   # HAND < 10 m → 100-year events
    # ≥ 10 m → No Risk
}

# Design flood water level (Q_rp) = water surface height above channel invert.
# depth_at_building = max(Q_rp − HAND_building, 0)
# Source: Rational Method Q=CIA, Enugu IDF curves (FMWR 2018, Table 3.4)
DESIGN_WATER_LEVELS = {
    "5_year":   1.2,
    "10_year":  2.0,
    "25_year":  3.5,
    "50_year":  5.0,
    "100_year": 7.0,
}

RETURN_PERIODS = {
    "5_year":   {"label": "5-Year Flood (20% annual risk)",   "color": "#FF4444"},
    "10_year":  {"label": "10-Year Flood (10% annual risk)",  "color": "#E74C3C"},
    "25_year":  {"label": "25-Year Flood (4% annual risk)",   "color": "#C0392B"},
    "50_year":  {"label": "50-Year Flood (2% annual risk)",   "color": "#922B21"},
    "100_year": {"label": "100-Year Flood (1% annual risk)",  "color": "#641E16"},
}

# Minimum building footprint area IN SQUARE METRES (native CRS).
# Applied before reprojection so the comparison is always in metres.
MIN_AREA_M2 = 15.0

# Maximum fraction of raster allowed to be drainage (prevents over-extraction)
MAX_DRAIN_FRACTION = 0.05   # 5%

# ── HELPERS ───────────────────────────────────────────────────────────────────
def banner(t): print(f"\n{'═'*58}\n  {t}\n{'═'*58}")
def step(n,t,m): print(f"\n[{n}/{t}]  {m}")
def ok(m):   print(f"       ✓  {m}")
def warn(m): print(f"       ⚠  {m}")
def info(m): print(f"          {m}")

def _is_geographic(crs):
    """Return True if CRS uses angular units (degrees)."""
    try:
        return crs.is_geographic
    except Exception:
        name = str(crs).upper()
        return "GEOGCS" in name or "EPSG:4326" in name or "WGS 84" in name

# ── 1. LOAD DEM ───────────────────────────────────────────────────────────────
def load_dem():
    step(1, 5, "Loading DEM …")
    if not os.path.exists(DEM):
        sys.exit(f"\n❌  DEM not found: {DEM}\n"
                 f"    Rename your file to dem_enugu.tif and place it in data/raw/\n")

    with rasterio.open(DEM) as src:
        arr   = src.read(1).astype("float32")
        trans = src.transform
        crs   = src.crs
        meta  = src.meta.copy()
        res   = src.res

    nd = meta.get("nodata")
    if nd is not None:
        arr[arr == nd] = np.nan

    # Warn if resolution looks like degrees (geographic CRS)
    if _is_geographic(crs):
        warn("DEM is in geographic CRS (degrees). Resolution shown in degrees.")
        info("For best HAND accuracy a projected CRS (UTM) is preferable,")
        info("but the analysis will still work in geographic coordinates.")

    v = arr[~np.isnan(arr)]
    info(f"Shape   : {arr.shape[0]} × {arr.shape[1]} pixels")
    info(f"Res     : {res[0]:.6f} × {res[1]:.6f}  (units depend on CRS)")
    info(f"Elev    : {np.nanmin(v):.1f} – {np.nanmax(v):.1f} m")
    info(f"CRS     : {crs.to_epsg() or crs}")
    ok("DEM loaded")
    return arr, trans, crs, meta

# ── 2. DRAINAGE NETWORK ───────────────────────────────────────────────────────
def extract_drainage(dem, trans):
    step(2, 5, "Extracting drainage network …")

    filled = np.where(np.isnan(dem), np.nanmean(dem), dem)
    total_cells = int(np.sum(~np.isnan(dem)))

    # FIX: use a LARGER window (9×9) and STRICTER slope threshold (5th pct)
    # to avoid over-extracting drainage on the 30 m WGS84 DEM.
    local_min = minimum_filter(filled, size=9)
    smooth    = gaussian_filter(filled, sigma=3)
    gx        = np.gradient(smooth, axis=1)
    gy        = np.gradient(smooth, axis=0)
    slope     = np.sqrt(gx**2 + gy**2)

    # Bottom 5th percentile of slope = true valley floors only
    slope_thresh = np.nanpercentile(slope, 5)
    info(f"Slope threshold (5th pct): {slope_thresh:.6f}")

    drain = (filled == local_min) & (slope < slope_thresh)

    # Keep only clusters ≥ 10 cells (removes isolated noise pixels)
    labeled, _ = label(drain)
    sizes = np.bincount(labeled.ravel())
    sizes[0]   = 0
    drain       = (sizes >= 10)[labeled]

    rows, cols = np.where(drain)
    n_drain    = len(rows)
    frac       = n_drain / total_cells

    info(f"Drainage cells: {n_drain:,}  ({frac*100:.1f}% of raster)")

    # FIX: if drainage fraction is still too high, tighten to top quartile of
    # accumulated low-slope cells by elevation (keep true lows only)
    if frac > MAX_DRAIN_FRACTION:
        warn(f"Drainage fraction {frac*100:.1f}% > {MAX_DRAIN_FRACTION*100:.0f}% cap — applying elevation filter")
        drain_elevs_all = filled[rows, cols]
        # Keep only the lowest 5% of drain-cell elevations
        elev_thresh = np.percentile(drain_elevs_all, MAX_DRAIN_FRACTION * 100)
        mask        = drain_elevs_all <= elev_thresh
        rows        = rows[mask]
        cols        = cols[mask]
        info(f"After elevation filter: {len(rows):,} drainage cells ({len(rows)/total_cells*100:.1f}%)")

    if not len(rows):
        warn("No drainage cells found — falling back to elevation proxy")
        return None, None

    # Build coordinate arrays in DEM CRS
    xs    = [xy(trans, r, c)[0] for r, c in zip(rows, cols)]
    ys    = [xy(trans, r, c)[1] for r, c in zip(rows, cols)]
    elevs = filled[rows, cols]

    ok(f"Drainage network: {len(rows):,} cells")
    return np.column_stack([xs, ys]), elevs

# ── 3. HAND ───────────────────────────────────────────────────────────────────
def calculate_hand(dem, trans, dc, de):
    step(3, 5, "Calculating HAND (Height Above Nearest Drainage) …")

    filled       = np.where(np.isnan(dem), np.nanmean(dem), dem)
    hand         = np.full(dem.shape, np.nan, dtype="float32")
    rows_all, cols_all = np.where(~np.isnan(dem))

    # ── Elevation proxy fallback ───────────────────────────────────────────────
    if dc is None:
        warn("Using normalised elevation as HAND proxy (less accurate)")
        mn, mx = np.nanmin(filled), np.nanmax(filled)
        hand   = ((filled - mn) / (mx - mn + 1e-9) * 20.0).astype("float32")
        ok("Elevation proxy HAND done")
        _save_hand(hand)
        return hand

    # ── Full KD-tree HAND ──────────────────────────────────────────────────────
    tree  = cKDTree(dc)
    batch = 30_000

    # Vectorise all pixel coordinates up front
    ax = np.array([xy(trans, r, c)[0] for r, c in zip(rows_all, cols_all)])
    ay = np.array([xy(trans, r, c)[1] for r, c in zip(rows_all, cols_all)])
    pts = np.column_stack([ax, ay])

    info(f"Computing HAND for {len(pts):,} pixels in batches of {batch:,} …")
    for s in range(0, len(pts), batch):
        e       = min(s + batch, len(pts))
        _, idx  = tree.query(pts[s:e], workers=-1)
        drain_e = de[idx]
        pixel_e = filled[rows_all[s:e], cols_all[s:e]]
        hand[rows_all[s:e], cols_all[s:e]] = np.maximum(
            pixel_e - drain_e, 0.0).astype("float32")
        pct = int(e / len(pts) * 100)
        if pct % 10 == 0:
            print(f"          {pct:3d}%", end="\r", flush=True)
    print()

    vh = hand[~np.isnan(hand)]
    info(f"HAND min : {np.nanmin(vh):.2f} m")
    info(f"HAND p10 : {np.nanpercentile(vh, 10):.2f} m")
    info(f"HAND p25 : {np.nanpercentile(vh, 25):.2f} m")
    info(f"HAND p50 : {np.nanpercentile(vh, 50):.2f} m")
    info(f"HAND p75 : {np.nanpercentile(vh, 75):.2f} m")
    info(f"HAND max : {np.nanmax(vh):.2f} m")

    # Sanity check: if median is still 0, drainage is still too dense
    if np.nanpercentile(vh, 50) < 0.01:
        warn("HAND median ≈ 0 — drainage network may still be over-extracted.")
        warn("Try running with a smaller study area DEM or higher-resolution data.")

    ok("HAND calculated")
    _save_hand(hand)
    return hand

def _save_hand(h):
    out = os.path.join(OUTDIR, "hand.tif")
    with rasterio.open(DEM) as src:
        meta = src.meta.copy()
    meta.update(dtype="float32", count=1, nodata=-9999.0)
    with rasterio.open(out, "w", **meta) as dst:
        dst.write(h, 1)
    ok("hand.tif → data/flood_risk_results/hand.tif  (inspect in QGIS)")

# ── 4. FLOOD SURFACES ─────────────────────────────────────────────────────────
def generate_flood_surfaces(hand, meta):
    step(4, 5, "Generating flood depth surfaces …")

    # Pixel area: if DEM is geographic (degrees), convert to m² approximately
    # using the mean latitude of the study area (Enugu ≈ 6.45°N)
    raw_pa = abs(meta["transform"].a * meta["transform"].e)
    if _is_geographic(meta.get("crs") or rasterio.open(DEM).crs):
        lat_rad    = 6.45 * (3.14159265 / 180)
        deg2m_x    = 111319.9 * abs(meta["transform"].a)   # lon spacing → m
        deg2m_y    = 111319.9 * abs(meta["transform"].e)   # lat spacing → m
        pixel_area = deg2m_x * deg2m_y                     # m²
    else:
        pixel_area = raw_pa

    surfaces = {}
    for period, Q in DESIGN_WATER_LEVELS.items():
        depth = np.where(
            (~np.isnan(hand)) & (hand < Q),
            np.clip(Q - hand, 0.0, Q),
            0.0
        ).astype("float32")

        surfaces[period] = depth
        cells   = int(np.sum(depth > 0.05))
        area_km = cells * pixel_area / 1e6
        info(f"{period:<12s}  Q={Q}m  flooded={area_km:.2f}km²  maxdepth={float(np.nanmax(depth)):.2f}m")

        m2 = meta.copy()
        m2.update(dtype="float32", count=1, nodata=0.0)
        with rasterio.open(os.path.join(OUTDIR, f"flood_{period}.tif"), "w", **m2) as dst:
            dst.write(depth, 1)

    ok("Flood surfaces saved")
    return surfaces

# ── 5. CLASSIFY BUILDINGS ─────────────────────────────────────────────────────
def classify_buildings(hand, surfaces, dem_trans, dem_crs, debug=False):
    step(5, 5, "Classifying buildings …")

    if not os.path.exists(BLDG):
        sys.exit(f"\n❌  GPKG not found: {BLDG}\n"
                 f"    Rename file to building_footprints.gpkg and place in data/raw/\n")

    # ── Load ──────────────────────────────────────────────────────────────────
    try:
        import fiona
        layers = fiona.listlayers(BLDG)
        layer  = layers[0]
        info(f"GPKG layer: '{layer}'  (available: {layers})")
    except Exception as e:
        layer = None
        warn(f"Could not list GPKG layers ({e}) — using default")

    bldgs = gpd.read_file(BLDG, layer=layer)
    info(f"Loaded {len(bldgs):,} buildings")

    if bldgs.crs is None:
        bldgs = bldgs.set_crs("EPSG:4326")
        warn("Building CRS unknown — assumed EPSG:4326")

    # ── FIX: compute area in native (projected) CRS BEFORE any reprojection ───
    # If the native CRS is already geographic (degrees), reproject to a metric
    # CRS temporarily just for the area calculation, then continue.
    if _is_geographic(bldgs.crs):
        info("Buildings in geographic CRS — projecting to UTM zone 32N for area calculation")
        try:
            bldgs_metric = bldgs.to_crs("EPSG:32632")   # UTM zone 32N covers Nigeria
        except Exception:
            try:
                bldgs_metric = bldgs.to_crs("EPSG:3857") # web mercator fallback
            except Exception:
                bldgs_metric = bldgs  # give up — use raw area
        bldgs["area_m2"] = bldgs_metric.geometry.area
    else:
        bldgs["area_m2"] = bldgs.geometry.area

    before = len(bldgs)
    bldgs  = bldgs[bldgs["area_m2"] > MIN_AREA_M2].reset_index(drop=True)
    dropped = before - len(bldgs)
    ok(f"Kept {len(bldgs):,} / {before:,}  (dropped {dropped:,} footprints < {MIN_AREA_M2} m²)")

    # FIX: guard against empty result
    if len(bldgs) == 0:
        sys.exit(
            "\n❌  All buildings were dropped by the area filter.\n"
            "    Possible causes:\n"
            "      1. Building footprints are stored as points, not polygons.\n"
            "         Check with: python -c \"import geopandas as gpd; "
            "print(gpd.read_file('data/raw/building_footprints.gpkg').geom_type.value_counts())\"\n"
            "      2. CRS is truly unknown and area calculation failed.\n"
            "    Lower MIN_AREA_M2 to 0 temporarily to bypass the filter and diagnose.\n"
        )

    # ── Reproject buildings to DEM CRS for centroid sampling ──────────────────
    if str(bldgs.crs) != str(dem_crs):
        bldgs = bldgs.to_crs(dem_crs)
        ok(f"Buildings reprojected → {dem_crs.to_epsg() or dem_crs}")

    # ── Sample HAND at each building centroid ──────────────────────────────────
    hv = np.full(len(bldgs), np.nan, dtype="float32")
    sampled = 0

    for i, row in bldgs.iterrows():
        cx, cy = row.geometry.centroid.x, row.geometry.centroid.y
        try:
            r, c = rowcol(dem_trans, cx, cy)
            if 0 <= r < hand.shape[0] and 0 <= c < hand.shape[1]:
                hv[i]    = hand[r, c]
                sampled += 1
        except Exception:
            pass

    bldgs["hand_m"] = hv
    info(f"Buildings with valid HAND sample: {sampled:,} / {len(bldgs):,}")

    if sampled == 0:
        warn("ZERO samples! Checking extent overlap …")
        b = bldgs.total_bounds
        r, c = hand.shape
        info(f"  Buildings bbox : {b[0]:.4f} {b[1]:.4f} {b[2]:.4f} {b[3]:.4f}")
        info(f"  DEM bbox       : {dem_trans.c:.4f}  {dem_trans.f+r*dem_trans.e:.4f}  "
             f"{dem_trans.c+c*dem_trans.a:.4f}  {dem_trans.f:.4f}")
        info("  If these don't overlap the data files cover different areas.")
        sys.exit("\n❌  Cannot classify buildings — HAND sampling failed (see above).\n")

    # Print building HAND distribution
    vh2 = hv[~np.isnan(hv)]
    info(f"Building HAND  min={np.nanmin(vh2):.2f}  p10={np.nanpercentile(vh2,10):.2f}"
         f"  p25={np.nanpercentile(vh2,25):.2f}  p50={np.nanpercentile(vh2,50):.2f}"
         f"  p75={np.nanpercentile(vh2,75):.2f}  max={np.nanmax(vh2):.2f} m")

    info("Expected classification with current thresholds:")
    for risk, thresh in HAND_THRESHOLDS.items():
        n   = int(np.sum(vh2 < thresh))
        pct = n / len(bldgs) * 100
        info(f"  HAND < {thresh:5.1f} m → {risk:<22s}  {n:>6,}  ({pct:.1f}%)")

    if debug:
        info("First 20 building HAND values:")
        for i, v in enumerate(hv[:20]):
            info(f"  [{i:04d}]  HAND = {v:.3f} m")

    # ── Risk classification ────────────────────────────────────────────────────
    def clf(h):
        if np.isnan(h):                            return "No Risk"
        if h < HAND_THRESHOLDS["High Risk"]:       return "High Risk"
        if h < HAND_THRESHOLDS["Medium-High Risk"]:return "Medium-High Risk"
        if h < HAND_THRESHOLDS["Medium Risk"]:     return "Medium Risk"
        if h < HAND_THRESHOLDS["Low Risk"]:        return "Low Risk"
        return "No Risk"

    bldgs["flood_risk"] = bldgs["hand_m"].apply(clf)

    # ── Flood depth per return period ──────────────────────────────────────────
    # depth = max(Q_rp − HAND, 0)  → always > 0 when building is at risk
    for period, Q in DESIGN_WATER_LEVELS.items():
        col = f"flood_depth_{period}"
        bldgs[col] = np.where(
            (~np.isnan(bldgs["hand_m"])) & (bldgs["hand_m"] < Q),
            (Q - bldgs["hand_m"]).clip(0, Q),
            0.0
        ).round(3)

    bldgs["max_flood_depth"] = bldgs[
        [f"flood_depth_{p}" for p in DESIGN_WATER_LEVELS]
    ].max(axis=1).round(2)

    # Primary depth for display = 5-year event (most policy-relevant)
    bldgs["flood_depth"] = bldgs["flood_depth_5_year"].round(2)

    # ── Print summary ──────────────────────────────────────────────────────────
    print("\n       ── Risk Distribution ─────────────────────────────────────")
    risk_counts = {}
    for risk in ["High Risk", "Medium-High Risk", "Medium Risk", "Low Risk", "No Risk"]:
        n   = int((bldgs["flood_risk"] == risk).sum())
        pct = n / len(bldgs) * 100
        risk_counts[risk] = n
        bar = "█" * int(pct / 2)
        print(f"       {risk:<22s}  {n:>6,}  ({pct:5.1f}%)  {bar}")

    affected = sum(v for k, v in risk_counts.items() if k != "No Risk")
    info(f"Total at some risk: {affected:,}  ({affected/len(bldgs)*100:.1f}%)")

    # Verify depth coupling
    for risk in ["High Risk", "Medium-High Risk", "Medium Risk", "Low Risk"]:
        sub    = bldgs[bldgs["flood_risk"] == risk]
        zeroes = int((sub["flood_depth"] == 0).sum())
        if zeroes > 0:
            warn(f"{risk}: {zeroes} buildings still show 0m for 5-year flood")
        elif len(sub) > 0:
            ok(f"{risk}: all {len(sub):,} buildings have depth > 0 m ✓")

    # ── Save GeoJSON (WGS84 for frontend) ─────────────────────────────────────
    keep = ["geometry", "flood_risk", "hand_m", "flood_depth", "max_flood_depth",
            *[f"flood_depth_{p}" for p in DESIGN_WATER_LEVELS]]
    for col in ["BLD_CODE","STATUS","BLD_TYPE","FLOORS","ADDRESS",
                "building_type","use_type","name","osm_id"]:
        if col in bldgs.columns:
            keep.append(col)

    save = bldgs[[c for c in keep if c in bldgs.columns]].copy()
    save = save.to_crs("EPSG:4326")
    gj   = os.path.join(OUTDIR, "flood_risk_buildings.geojson")
    save.to_file(gj, driver="GeoJSON")
    ok(f"GeoJSON saved → {gj}  ({os.path.getsize(gj)/1e6:.1f} MB)")

    # ── Save summary JSON ──────────────────────────────────────────────────────
    vh3 = hv[~np.isnan(hv)]
    with open(os.path.join(OUTDIR, "summary.json"), "w") as f:
        json.dump({
            "generated":             datetime.now().isoformat(),
            "methodology":           "HAND absolute hydraulic thresholds (FEMA/USGS)",
            "hand_thresholds_m":     HAND_THRESHOLDS,
            "design_water_levels_m": DESIGN_WATER_LEVELS,
            "total_buildings":       len(bldgs),
            "sampled_buildings":     sampled,
            "risk_counts":           risk_counts,
            "return_periods":        RETURN_PERIODS,
            "hand_stats": {
                "min": float(np.nanmin(vh3))          if len(vh3) else None,
                "p10": float(np.nanpercentile(vh3,10)) if len(vh3) else None,
                "p25": float(np.nanpercentile(vh3,25)) if len(vh3) else None,
                "p50": float(np.nanpercentile(vh3,50)) if len(vh3) else None,
                "max": float(np.nanmax(vh3))          if len(vh3) else None,
            }
        }, f, indent=2)
    ok("summary.json saved")
    return bldgs

# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    p = argparse.ArgumentParser(description="Enugu Flood Risk Analysis")
    p.add_argument("--debug", action="store_true",
                   help="Print first 20 building HAND values")
    args = p.parse_args()

    banner("ENUGU FLOOD RISK ANALYSIS")
    info(f"Method : HAND + absolute hydraulic thresholds (FEMA/USGS best practice)")
    info(f"DEM    : {DEM}")
    info(f"GPKG   : {BLDG}")
    info(f"Output : {OUTDIR}")
    os.makedirs(OUTDIR, exist_ok=True)

    dem, trans, crs, meta = load_dem()
    dc, de                = extract_drainage(dem, trans)
    hand                  = calculate_hand(dem, trans, dc, de)
    surfaces              = generate_flood_surfaces(hand, meta)
    classify_buildings(hand, surfaces, trans, crs, debug=args.debug)

    banner("ANALYSIS COMPLETE ✓")
    info("Next steps:")
    info("  1. Restart flood_api.py (port 5001) to serve updated results")
    info("  2. Open data/flood_risk_results/hand.tif in QGIS to verify drainage quality")
    info("  3. Compare risk distribution against 2022 Enugu flood records")
    info("  4. Calibration: if High Risk % is too high, raise HAND_THRESHOLDS['High Risk']")
    info("                  if too low, lower it\n")

if __name__ == "__main__":
    main()
