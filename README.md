# Enugu Smart City – Flood Risk Integration Guide

## Your folder structure after adding these files

```
enugu_emergency_response/
├── backend/
│   ├── app.py                    ← unchanged
│   ├── flood_api.py              ← NEW  (port 5001)
│   └── requirements_flood.txt   ← NEW
├── frontend/
│   ├── index.html                ← add ONE line (see Step 4)
│   ├── main.js                   ← UPDATED (flood badge + window.enuguBuildings)
│   ├── flood_risk.js             ← NEW  (auto-attaches panel)
│   └── buildings_3dtiles/
│       ├── tileset.json
│       └── data/
├── data/
│   ├── raw/
│   │   ├── building_footprints.gpkg   ← your existing GPKG
│   │   └── dem_enugu.tif              ← your existing DEM
│   ├── processed/
│   └── flood_risk_results/            ← CREATED by flood_analysis.py
│       ├── hand.tif
│       ├── flood_5_year.tif
│       ├── flood_10_year.tif
│       ├── flood_25_year.tif
│       ├── flood_50_year.tif
│       ├── flood_100_year.tif
│       ├── flood_risk_buildings.geojson
│       └── summary.json
└── flood_analysis.py             ← NEW  (run once from project root)
```

---

## Step 1 — Install dependencies

```bash
cd enugu_emergency_response
pip install -r backend/requirements_flood.txt
```

---

## Step 2 — Check your data file names

`flood_analysis.py` expects:
```
data/raw/building_footprints.gpkg
data/raw/dem_enugu.tif
```

If your files have different names, edit the top of `flood_analysis.py`:
```python
DEM_PATH  = os.path.join(BASE_DIR, "data", "raw", "YOUR_DEM_NAME.tif")
BLDG_PATH = os.path.join(BASE_DIR, "data", "raw", "YOUR_GPKG_NAME.gpkg")
```

---

## Step 3 — Run the flood analysis (run ONCE, takes 2–10 min)

```bash
# From enugu_emergency_response/ root
python flood_analysis.py
```

You should see:
```
[1/5] Loading Digital Elevation Model …
      ✓  DEM loaded
[2/5] Extracting drainage network …
      ✓  Drainage cells : 4,821
[3/5] Calculating Height Above Nearest Drainage (HAND) …
      ✓  HAND range: 0.00 m – 48.3 m
[4/5] Generating flood inundation surfaces …
      5_year       1.23 km²  |  max depth 0.50 m
[5/5] Classifying building flood risk …
      High Risk              312  ( 6.2%)
      Medium-High Risk       481  ( 9.6%)
      ...
ANALYSIS COMPLETE ✓
```

---

## Step 4 — Add one line to index.html

Open `frontend/index.html`.
Find your existing `<script>` tags at the bottom.
Add this line **AFTER** your main.js line:

```html
<script src="main.js"></script>
<script src="flood_risk.js"></script>   ←  ADD THIS LINE
```

---

## Step 5 — Replace main.js

Copy the new `main.js` into `frontend/`.
The only changes from your original are:
1. `window.viewer = viewer;` (line after viewer init)
2. `window.enuguBuildings = enuguBuildings;` (inside loadBuildings)
3. `rows += FloodRisk.badge(props);` (inside showBuildingPanel)

If your original `main.js` is heavily customised, just add those 3 lines manually
instead of replacing the whole file.

---

## Step 6 — Start all servers

```bash
# Terminal 1 – emergency response API (unchanged)
cd backend
python app.py              # port 5000

# Terminal 2 – flood risk API (NEW)
cd backend
python flood_api.py        # port 5001

# Terminal 3 – frontend
cd frontend
python -m http.server 8080
```

---

## Step 7 — Verify it works

1. Open http://localhost:8080
2. Wait for 3D city to load
3. **Bottom-right corner** → "🌊 Flood Risk Overlay" panel appears
4. Click **Enable Flood Layer**
5. Buildings colour-code:
   - 🔴 Red = High Risk
   - 🟠 Orange = Medium-High
   - 🟡 Yellow = Medium / Low
   - ⚫ Grey = No Risk
6. Change return period dropdown → colours update instantly
7. Click a building → popup includes a flood risk badge row

---

## Troubleshooting

### Panel still not showing
```
F12 → Console → look for [FloodRisk] messages
```
- Is `flood_risk.js` in the same folder as `index.html`? ✓
- Did you add `<script src="flood_risk.js"></script>` to index.html? ✓
- Does `window.viewer` exist? The new main.js adds `window.viewer = viewer` — if
  using your old main.js, add that line manually right after the viewer is created.

### Buildings not colouring
- Check http://localhost:5001/api/flood/statistics — should return JSON
- If 5001 isn't responding, flood_api.py isn't running
- If GeoJSON has 0 features, flood_analysis.py didn't complete — rerun it

### flood_analysis.py crashes reading GPKG
```bash
python -c "import fiona; print(fiona.listlayers('data/raw/building_footprints.gpkg'))"
```
If it prints multiple layers, open `flood_analysis.py` and set `layer="your_layer_name"`
explicitly on the `gpd.read_file(...)` line.

### Flood surfaces not generating (all depths show 0.0)
Increase `HAND_CALIBRATION` in flood_analysis.py from `0.45` to `0.65` and rerun.
This means the drainage extraction found channels but your buildings sit well above them.

### CORS errors in browser
Ensure `flask-cors` is installed: `pip install flask-cors`

---

## Calibration tips

The `HAND_CALIBRATION = 0.45` constant in `flood_analysis.py` controls how aggressively
areas are flagged as flood-prone.

Compare output against Enugu's 2022 flood records:
- Too few buildings flagged → increase to 0.55–0.65
- Too many flagged → decrease to 0.30–0.35

Re-run `flood_analysis.py` after changing it — the API and frontend pick up
the new results automatically on next restart.
