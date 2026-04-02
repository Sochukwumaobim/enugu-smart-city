"""
flood_api.py
Enugu Smart City – Flood Risk REST API
Place at:  enugu_emergency_response/backend/flood_api.py
Run with:  python flood_api.py   (port 5001)

FIXED vs previous version:
  - Added /api/flood/risk  →  returns a flat JSON array of building objects
    with lon, lat, flood_risk, flood_depth, address, building_type, elevation.
    This is what main.js expects when it calls:
        fetch(`${FLOOD_API}/risk`)
        floodBuildingRisks = await riskResponse.json()
  - Added /api/flood/risk?lon=&lat=  →  point query used by the building
    click handler in main.js to display per-building flood info in the popup.
  - /api/flood/statistics now returns both risk_counts AND risk_distribution
    so old and new code both work.
"""

import os
import json
import base64
import math
from io import BytesIO
from datetime import datetime

from flask import Flask, jsonify, request, send_file
from flask_cors import CORS

# ── Optional heavy deps ───────────────────────────────────────────────────────
try:
    import rasterio
    RASTERIO_OK = True
except ImportError:
    RASTERIO_OK = False
    print("⚠  rasterio not available – raster PNG endpoints disabled")

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.colors import LinearSegmentedColormap
    MPL_OK = True
except ImportError:
    MPL_OK = False
    print("⚠  matplotlib not available – PNG rendering disabled")

# ─────────────────────────────────────────────────────────────────────────────
# PATHS
# flood_api.py is in backend/;  data is at ../data/flood_risk_results/
# ─────────────────────────────────────────────────────────────────────────────
HERE     = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "..", "data", "flood_risk_results")

# ─────────────────────────────────────────────────────────────────────────────
# METADATA
# ─────────────────────────────────────────────────────────────────────────────
RETURN_PERIODS = {
    "5_year":   {"depth": 0.5,  "label": "5-Year Flood (20% annual risk)",   "color": "#FF4444"},
    "10_year":  {"depth": 1.0,  "label": "10-Year Flood (10% annual risk)",  "color": "#E74C3C"},
    "25_year":  {"depth": 1.5,  "label": "25-Year Flood (4% annual risk)",   "color": "#C0392B"},
    "50_year":  {"depth": 2.0,  "label": "50-Year Flood (2% annual risk)",   "color": "#922B21"},
    "100_year": {"depth": 2.5,  "label": "100-Year Flood (1% annual risk)",  "color": "#641E16"},
}

RISK_COLORS = {
    "High Risk":        "#FF4444",
    "Medium-High Risk": "#FF6B35",
    "Medium Risk":      "#FFA500",
    "Low Risk":         "#FFD700",
    "No Risk":          "#95A5A6",
}

# ─────────────────────────────────────────────────────────────────────────────
# FLASK APP
# ─────────────────────────────────────────────────────────────────────────────
app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": "*"}})


# ─────────────────────────────────────────────────────────────────────────────
# DATA SERVICE
# ─────────────────────────────────────────────────────────────────────────────
class FloodDataService:

    def __init__(self):
        self.geojson        = {"type": "FeatureCollection", "features": []}
        self.summary        = self._empty_summary()
        self.flood_arrays   = {}
        self.raster_meta    = None
        self.raster_bounds  = None
        self._flat_cache    = None   # cached flat array for /risk endpoint
        self._load_all()

    # ── Loaders ───────────────────────────────────────────────────────────────
    def _load_all(self):
        print("\n🌊  FloodDataService loading …")
        self._load_summary()
        self._load_geojson()
        self._load_rasters()
        n = len(self.geojson["features"])
        print(f"   Ready  –  {n:,} buildings  |  {len(self.flood_arrays)} rasters\n")

    def _load_summary(self):
        p = os.path.join(DATA_DIR, "summary.json")
        if os.path.exists(p):
            with open(p) as f:
                self.summary = json.load(f)
            print(f"   ✓  summary.json  ({self.summary['total_buildings']:,} buildings)")
        else:
            print("   ⚠  summary.json not found – run flood_analysis.py first")

    def _load_geojson(self):
        p = os.path.join(DATA_DIR, "flood_risk_buildings.geojson")
        if os.path.exists(p):
            with open(p) as f:
                self.geojson = json.load(f)
            n = len(self.geojson["features"])
            print(f"   ✓  GeoJSON  ({n:,} features)")
            self._flat_cache = None   # invalidate cache
        else:
            print("   ⚠  flood_risk_buildings.geojson not found – run flood_analysis.py first")

    def _load_rasters(self):
        if not RASTERIO_OK:
            return
        for period in RETURN_PERIODS:
            p = os.path.join(DATA_DIR, f"flood_{period}.tif")
            if os.path.exists(p):
                with rasterio.open(p) as src:
                    self.flood_arrays[period] = src.read(1)
                    if self.raster_meta is None:
                        self.raster_meta   = src.meta
                        self.raster_bounds = src.bounds
                print(f"   ✓  flood_{period}.tif")

    # ── Flat building array (for /api/flood/risk) ─────────────────────────────
    def flat_buildings(self):
        """
        Returns a list of plain dicts — one per building — with the fields
        that main.js needs:
            lon, lat, flood_risk, flood_depth, address, building_type, elevation
        Derived from the GeoJSON FeatureCollection produced by flood_analysis.py.
        """
        if self._flat_cache is not None:
            return self._flat_cache

        result = []
        for feat in self.geojson.get("features", []):
            props = feat.get("properties", {}) or {}
            geom  = feat.get("geometry",   {}) or {}

            # Extract centroid lon/lat from geometry
            lon, lat = None, None
            if geom.get("type") == "Point":
                lon, lat = geom["coordinates"][0], geom["coordinates"][1]
            elif geom.get("type") in ("Polygon", "MultiPolygon"):
                coords = geom.get("coordinates", [])
                try:
                    if geom["type"] == "Polygon":
                        ring = coords[0]
                    else:
                        ring = coords[0][0]
                    xs = [c[0] for c in ring]
                    ys = [c[1] for c in ring]
                    lon = sum(xs) / len(xs)
                    lat = sum(ys) / len(ys)
                except Exception:
                    pass

            if lon is None or lat is None:
                continue

            # Best available depth: use max_flood_depth if present, else 5-year
            depth = (props.get("max_flood_depth")
                     or props.get("flood_depth_5_year")
                     or 0.0)
            try:
                depth = float(depth)
            except (TypeError, ValueError):
                depth = 0.0

            # Address — try common column names from ENGIS data
            address = (props.get("ADDRESS")
                       or props.get("address")
                       or props.get("BLD_CODE")
                       or props.get("osm_id")
                       or None)

            result.append({
                "lon":           round(lon, 6),
                "lat":           round(lat, 6),
                "flood_risk":    props.get("flood_risk", "No Risk"),
                "flood_depth":   round(depth, 2),
                "address":       address,
                "building_type": (props.get("BLD_TYPE")
                                  or props.get("building_type")
                                  or props.get("use_type")
                                  or "Residential"),
                "hand_m":        round(float(props["hand_m"]), 3) if props.get("hand_m") is not None else None,
                "flood_depth":   round(float(props.get("flood_depth") or props.get("flood_depth_5_year") or 0), 2),
                "elevation":     props.get("elevation") or props.get("hand_m"),
            })

        self._flat_cache = result
        return result

    # ── Point query (for building click popup) ────────────────────────────────
    def nearest_building_risk(self, lon, lat, max_dist_deg=0.001):
        """
        Return the flood risk of the building nearest to (lon, lat).
        max_dist_deg ≈ 100m at Enugu's latitude.
        """
        best      = None
        best_dist = max_dist_deg

        for b in self.flat_buildings():
            d = math.sqrt((b["lon"] - lon) ** 2 + (b["lat"] - lat) ** 2)
            if d < best_dist:
                best_dist = d
                best      = b

        if best:
            return {
                "flood_risk":        best["flood_risk"],
                "flood_depth":       best.get("flood_depth", 0),
                "hand_m":            best.get("hand_m"),
                "elevation":         best.get("elevation"),
                "river_distance_km": None,
            }
        return {"flood_risk": "No Risk", "flood_depth": 0, "hand_m": None, "elevation": None}

    # ── Statistics ─────────────────────────────────────────────────────────────
    def statistics(self):
        rc    = self.summary.get("risk_counts", {})
        total = self.summary.get("total_buildings", 0)
        affected = sum(v for k, v in rc.items() if k != "No Risk")

        # Build risk_distribution in the shape some older code might expect
        risk_distribution = {
            k: {"count": v, "percentage": round(v / total * 100, 1) if total else 0}
            for k, v in rc.items()
        }

        return {
            "total_buildings":    total,
            "affected_buildings": affected,
            "risk_counts":        rc,               # used by new main.js
            "risk_distribution":  risk_distribution,  # kept for backward compat
            "risk_percentages": {
                k: round(v / total * 100, 1) if total else 0
                for k, v in rc.items()
            },
            "risk_colors": RISK_COLORS,
            "generated":   self.summary.get("generated"),
        }

    # ── GeoJSON buildings ──────────────────────────────────────────────────────
    def buildings_geojson(self, period=None):
        if not period:
            return self.geojson
        depth_key = f"flood_depth_{period}"
        feats = [
            f for f in self.geojson["features"]
            if (f.get("properties") or {}).get(depth_key, 0) > 0.05
        ]
        return {"type": "FeatureCollection", "features": feats}

    # ── Flood surface PNG ──────────────────────────────────────────────────────
    def flood_surface_png(self, period):
        if not MPL_OK or period not in self.flood_arrays:
            return None, None
        arr    = self.flood_arrays[period]
        params = RETURN_PERIODS[period]
        cmap   = LinearSegmentedColormap.from_list(
            "flood", ["#FFFFFF00", f"{params['color']}80", params["color"]], N=256
        )
        fig, ax = plt.subplots(figsize=(8, 6), dpi=100)
        im = ax.imshow(arr, cmap=cmap, vmin=0, vmax=params["depth"], interpolation="bilinear")
        ax.axis("off")
        plt.colorbar(im, ax=ax, label="Flood depth (m)", shrink=0.5)
        plt.title(params["label"], fontsize=9, pad=4)
        plt.tight_layout(pad=0)
        buf = BytesIO()
        plt.savefig(buf, format="png", bbox_inches="tight", transparent=True)
        plt.close(fig)
        buf.seek(0)
        b64    = base64.b64encode(buf.read()).decode()
        bounds = None
        if self.raster_bounds:
            b      = self.raster_bounds
            bounds = {"west": b.left, "south": b.bottom, "east": b.right, "north": b.top}
        return b64, bounds

    @staticmethod
    def _empty_summary():
        return {"generated": None, "total_buildings": 0, "risk_counts": {k: 0 for k in RISK_COLORS}}


# ── Singleton ─────────────────────────────────────────────────────────────────
svc = FloodDataService()


# ─────────────────────────────────────────────────────────────────────────────
# ENDPOINTS
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/flood/health")
def health():
    data_ready = os.path.exists(os.path.join(DATA_DIR, "summary.json"))
    return jsonify({"status": "ok", "data_ready": data_ready,
                    "timestamp": datetime.now().isoformat()})


@app.route("/api/flood/periods")
def periods():
    return jsonify(RETURN_PERIODS)


@app.route("/api/flood/statistics")
def statistics():
    return jsonify(svc.statistics())


# ── /api/flood/risk  ──────────────────────────────────────────────────────────
@app.route("/api/flood/risk")
def risk():
    """
    Two modes:

    1. GET /api/flood/risk
       Returns a JSON ARRAY of building objects:
       [{ lon, lat, flood_risk, flood_depth, address, building_type, elevation }, ...]
       Used by main.js:  floodBuildingRisks = await riskResponse.json()

    2. GET /api/flood/risk?lon=7.4855&lat=6.4520
       Returns a single object for the nearest building at that coordinate.
       Used by the click handler:  fetch(`${FLOOD_API}/risk?lon=${lon}&lat=${lat}`)
    """
    lon_str = request.args.get("lon")
    lat_str = request.args.get("lat")

    if lon_str and lat_str:
        # Point query mode
        try:
            lon = float(lon_str)
            lat = float(lat_str)
        except ValueError:
            return jsonify({"error": "lon and lat must be numbers"}), 400
        return jsonify(svc.nearest_building_risk(lon, lat))

    # Array mode
    return jsonify(svc.flat_buildings())


@app.route("/api/flood/buildings")
def buildings():
    """GeoJSON FeatureCollection — optional ?period= filter."""
    period = request.args.get("period")
    return jsonify(svc.buildings_geojson(period))


@app.route("/api/flood/surface/<period>")
def surface(period):
    if period not in RETURN_PERIODS:
        return jsonify({"error": f"Unknown period. Use: {list(RETURN_PERIODS)}"}), 400
    png, bounds = svc.flood_surface_png(period)
    return jsonify({
        "period": period,
        "label":  RETURN_PERIODS[period]["label"],
        "color":  RETURN_PERIODS[period]["color"],
        "depth":  RETURN_PERIODS[period]["depth"],
        "image":  png,
        "bounds": bounds,
    })


@app.route("/api/flood/export")
def export():
    p = os.path.join(DATA_DIR, "flood_risk_buildings.geojson")
    if not os.path.exists(p):
        return jsonify({"error": "Run flood_analysis.py first"}), 404
    return send_file(p, mimetype="application/geo+json",
                     as_attachment=True, download_name="enugu_flood_risk.geojson")


# ─────────────────────────────────────────────────────────────────────────────
# RUN
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 55)
    print("  Enugu Flood Risk API  –  http://localhost:5001")
    print("=" * 55)
    print("  GET /api/flood/health")
    print("  GET /api/flood/periods")
    print("  GET /api/flood/statistics")
    print("  GET /api/flood/risk              ← flat array for heatmap")
    print("  GET /api/flood/risk?lon=&lat=    ← point query for click popup")
    print("  GET /api/flood/buildings[?period=5_year]")
    print("  GET /api/flood/surface/<period>")
    print("  GET /api/flood/export")
    print("=" * 55)
    app.run(host="0.0.0.0", port=5001, debug=True)
