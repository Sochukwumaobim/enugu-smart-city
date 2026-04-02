"""
traffic_api.py  -  Enugu Smart City
SUMO 3D Traffic Backend
========================
Place at:  enugu_emergency_response\\backend\\traffic_api.py
Run:       python backend\\traffic_api.py   (port 5002)

KEY DESIGN DECISIONS
---------------------
1. sumolib reads enugu.net.xml ONCE at startup → cached as _road_geojson
   The /api/traffic/routes endpoint returns this cache instantly (<5ms).
   Reading 135MB on every request was causing 30-60s timeouts.

2. SUMO runs in a background daemon thread via TraCI.
   The Flask thread only reads the shared vehicle buffer — never blocks.

3. Vehicle positions are returned as plain WGS84 lon/lat with NO altitude.
   CesiumJS clamps them to ground using CLAMP_TO_GROUND heightReference.
   Setting explicit altitude in Enugu (~300m above ellipsoid) was causing
   vehicles to float in the air.
"""

import os, math, random, time, threading, json
from datetime import datetime
from flask import Flask, jsonify, request
from flask_cors import CORS

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": "*"}})

# ════════════════════════════════════════════════════════════════════
# CONFIG
# ════════════════════════════════════════════════════════════════════
USE_SUMO  = True
SUMO_DIR  = r"C:\Users\Hp\Desktop\GIS-NG\enugu_emergency_response\data\sumo"
SUMO_CFG  = os.path.join(SUMO_DIR, "enugu.sumocfg")
NET_FILE  = os.path.join(SUMO_DIR, "enugu.net.xml")

MAX_VEHICLES  = 300
STEP_LENGTH   = 0.5    # seconds per SUMO step
SUMO_HZ       = 10     # steps per second target

# Enugu bounding box (WGS84) — discard any vehicle outside this
BBOX = (7.35, 6.25, 7.70, 6.65)   # west, south, east, north

# ════════════════════════════════════════════════════════════════════
# SHARED STATE
# ════════════════════════════════════════════════════════════════════
_lock          = threading.Lock()
_vehicles_buf  = []
_step_count    = 0
_sumo_source   = 'starting'
_sumo_stats    = {'total': 0, 'avg_speed': 0.0, 'by_type': {}}
_road_geojson  = []    # pre-built at startup, served instantly
_net_obj       = None  # sumolib net object, reused by SUMO thread

# ════════════════════════════════════════════════════════════════════
# NETWORK PRE-LOADER  (runs once at startup, before Flask starts)
# ════════════════════════════════════════════════════════════════════
def preload_network():
    """
    Reads enugu.net.xml once with sumolib, converts all drivable edge
    shapes to WGS84, caches result in _road_geojson.
    Also stores the net object so the SUMO thread can reuse it for
    convertXY2LonLat without re-reading the file.
    """
    global _road_geojson, _net_obj
    if not os.path.exists(NET_FILE):
        print(f"[Network] NET_FILE not found: {NET_FILE}")
        return

    print(f"[Network] Reading {NET_FILE}  (this takes ~10-30s for 135MB)...")
    t0 = time.time()
    try:
        import sumolib
        net = sumolib.net.readNet(NET_FILE, withInternal=False)
        _net_obj = net
        print(f"[Network] Loaded in {time.time()-t0:.1f}s")
    except Exception as e:
        print(f"[Network] Failed: {e}")
        return

    edges = []
    skipped = 0
    for edge in net.getEdges():
        try:
            # Only passenger/bus roads
            if not (edge.allows("passenger") or edge.allows("bus")):
                skipped += 1
                continue
            shape = edge.getShape()
            if len(shape) < 2:
                continue
            coords = []
            for x, y in shape:
                lon, lat = net.convertXY2LonLat(x, y)
                if BBOX[0] < lon < BBOX[2] and BBOX[1] < lat < BBOX[3]:
                    coords.append([round(lon, 6), round(lat, 6)])
            if len(coords) >= 2:
                edges.append({
                    'id':        edge.getID(),
                    'name':      edge.getName() or '',
                    'coords':    coords,
                    'speed_kmh': round((edge.getSpeed() or 13.9) * 3.6, 1),
                })
        except Exception:
            continue

    _road_geojson = edges
    print(f"[Network] {len(edges)} drivable edges cached  ({skipped} non-drivable skipped)")


# ════════════════════════════════════════════════════════════════════
# SYNTHETIC ENGINE  (fallback when SUMO unavailable)
# ════════════════════════════════════════════════════════════════════
ROUTES = [
    {'id':'ogui_rd',    'name':'Ogui Road',              'speed_kmh':45,
     'type_weights':{'car':0.6,'bus':0.15,'motorcycle':0.2,'truck':0.05},
     'waypoints':[[7.4880,6.4380],[7.4920,6.4400],[7.4970,6.4420],
                  [7.5020,6.4440],[7.5080,6.4460],[7.5130,6.4480],[7.5220,6.4520]]},
    {'id':'agbani_rd',  'name':'Agbani Road',            'speed_kmh':50,
     'type_weights':{'car':0.55,'bus':0.1,'motorcycle':0.25,'truck':0.1},
     'waypoints':[[7.5050,6.4350],[7.5100,6.4320],[7.5150,6.4290],
                  [7.5200,6.4260],[7.5260,6.4230],[7.5310,6.4200]]},
    {'id':'zik_ave',    'name':'Zik Avenue',             'speed_kmh':40,
     'type_weights':{'car':0.65,'bus':0.12,'motorcycle':0.18,'truck':0.05},
     'waypoints':[[7.4980,6.4460],[7.5010,6.4470],[7.5050,6.4480],
                  [7.5090,6.4490],[7.5130,6.4505],[7.5170,6.4515]]},
    {'id':'presidential','name':'Presidential Road',     'speed_kmh':55,
     'type_weights':{'car':0.7,'bus':0.1,'motorcycle':0.12,'truck':0.08},
     'waypoints':[[7.4880,6.4530],[7.4920,6.4545],[7.4970,6.4560],
                  [7.5020,6.4575],[7.5070,6.4590],[7.5110,6.4600]]},
    {'id':'enutcha_exp','name':'Enugu-Onitsha Expressway','speed_kmh':80,
     'type_weights':{'car':0.5,'bus':0.2,'motorcycle':0.1,'truck':0.2},
     'waypoints':[[7.4620,6.4430],[7.4740,6.4445],[7.4800,6.4450],
                  [7.4860,6.4455],[7.4920,6.4458],[7.4970,6.4460]]},
    {'id':'gra_ring',   'name':'GRA Ring Road',          'speed_kmh':35,
     'type_weights':{'car':0.75,'bus':0.05,'motorcycle':0.15,'truck':0.05},
     'waypoints':[[7.5100,6.4610],[7.5140,6.4630],[7.5180,6.4650],
                  [7.5220,6.4670],[7.5260,6.4680],[7.5300,6.4670]]},
]

class SyntheticVehicle:
    _ctr = 0
    def __init__(self, route):
        SyntheticVehicle._ctr += 1
        self.id  = f"sv{SyntheticVehicle._ctr:04d}"
        self.route = route
        self.wps = route['waypoints']
        self.type = self._pick(route['type_weights'])
        self.speed = route['speed_kmh'] * random.uniform(0.75, 1.15)
        self.seg = random.randint(0, max(0, len(self.wps)-2))
        self.t   = random.random()
        self.fwd = random.choice([True, False])
        self.lon = self.lat = self.heading = 0.0
        self._refresh()

    @staticmethod
    def _pick(w):
        r, s = random.random(), 0
        for k, p in w.items():
            s += p
            if r <= s: return k
        return 'car'

    def step(self, dt):
        n = len(self.wps)-1
        if n < 1: return
        i = min(self.seg, n-1)
        dx = self.wps[i+1][0]-self.wps[i][0]
        dy = self.wps[i+1][1]-self.wps[i][1]
        slen = max(math.sqrt(dx*dx+dy*dy), 1e-9)
        frac = (self.speed/3.6/110_000*dt) / slen
        if self.fwd:
            self.t += frac
            if self.t >= 1.0:
                self.t=0.0; self.seg+=1
                if self.seg >= n: self.seg=n-1; self.fwd=False
        else:
            self.t -= frac
            if self.t <= 0.0:
                self.t=1.0; self.seg-=1
                if self.seg < 0: self.seg=0; self.fwd=True
        self._refresh()

    def _refresh(self):
        i = min(self.seg, len(self.wps)-2); t = max(0, min(1, self.t))
        self.lon = self.wps[i][0] + t*(self.wps[i+1][0]-self.wps[i][0])
        self.lat = self.wps[i][1] + t*(self.wps[i+1][1]-self.wps[i][1])
        dx = (self.wps[i+1][0]-self.wps[i][0])*math.cos(math.radians(self.lat))*110310
        dy = (self.wps[i+1][1]-self.wps[i][1])*111000
        if not self.fwd: dx,dy = -dx,-dy
        self.heading = (math.degrees(math.atan2(dx,dy))+360)%360

    def to_dict(self):
        return {'id':self.id,'lon':round(self.lon,7),'lat':round(self.lat,7),
                'heading':round(self.heading,1),'speed':round(self.speed,1),
                'type':self.type,'route':self.route['id']}

class SyntheticEngine:
    def __init__(self):
        self.vehicles={}; self._lt=time.time(); self.step_n=0
        self._seed()

    def _target(self):
        h=datetime.now().hour
        if 7<=h<=9 or 17<=h<=19: return min(MAX_VEHICLES,180)
        if 10<=h<=16: return min(MAX_VEHICLES,100)
        if h>=20 or h<=5: return min(MAX_VEHICLES,20)
        return min(MAX_VEHICLES,60)

    def _seed(self):
        for r in ROUTES:
            for _ in range(max(2, int(self._target()*r['speed_kmh']/450))):
                v=SyntheticVehicle(r); self.vehicles[v.id]=v

    def step(self):
        now=time.time(); dt=min(now-self._lt,2.0); self._lt=now; self.step_n+=1
        for v in self.vehicles.values(): v.step(dt)
        tgt=self._target()
        if len(self.vehicles)<tgt:
            v=SyntheticVehicle(random.choice(ROUTES)); self.vehicles[v.id]=v
        elif len(self.vehicles)>tgt+5:
            del self.vehicles[next(iter(self.vehicles))]
        return [v.to_dict() for v in self.vehicles.values()]

synth_engine = SyntheticEngine()

# ════════════════════════════════════════════════════════════════════
# SUMO BACKGROUND THREAD
# ════════════════════════════════════════════════════════════════════
def _vtype(tid):
    t=tid.lower()
    if 'bus' in t or 'coach' in t: return 'bus'
    if 'truck' in t or 'heavy' in t: return 'truck'
    if 'moto' in t or ('bike' in t and 'cycle' not in t): return 'motorcycle'
    if 'bicycle' in t or 'cycle' in t: return 'bicycle'
    return 'car'

def sumo_thread():
    global _step_count, _sumo_source, _sumo_stats, _vehicles_buf
    try:
        import traci
    except ImportError:
        print("[SUMO] traci not installed — pip install traci")
        _sumo_source='synthetic'; return

    # Reuse the pre-loaded net object for coordinate conversion
    net = _net_obj
    if net is None:
        print("[SUMO] Network not loaded — synthetic fallback")
        _sumo_source='synthetic'; return

    print("[SUMO] Starting SUMO headless via TraCI...")
    try:
        traci.start([
            'sumo', '-c', SUMO_CFG,
            '--no-step-log',      'true',
            '--no-warnings',      'true',
            '--step-length',      str(STEP_LENGTH),
            '--collision.action', 'remove',
            '--time-to-teleport', '60',
            '--max-depart-delay', '60',
        ])
        _sumo_source='sumo'
        print("[SUMO] TraCI connected ✓")
    except Exception as e:
        print(f"[SUMO] Start failed: {e}")
        _sumo_source='synthetic'; return

    interval  = 1.0/SUMO_HZ
    tick      = time.time()

    while True:
        try:
            traci.simulationStep()
            _step_count += 1

            vids = traci.vehicle.getIDList()
            vehs = []
            for vid in vids[:MAX_VEHICLES]:
                try:
                    x, y    = traci.vehicle.getPosition(vid)
                    lon, lat = net.convertXY2LonLat(x, y)
                    # Skip if outside Enugu
                    if not (BBOX[0]<lon<BBOX[2] and BBOX[1]<lat<BBOX[3]):
                        continue
                    angle = traci.vehicle.getAngle(vid)
                    speed = traci.vehicle.getSpeed(vid)*3.6
                    vtype = traci.vehicle.getTypeID(vid)
                    vehs.append({
                        'id':      vid,
                        'lon':     round(lon,7),
                        'lat':     round(lat,7),
                        'heading': round(angle,1),
                        'speed':   round(speed,1),
                        'type':    _vtype(vtype),
                    })
                except Exception:
                    continue

            total   = len(vehs)
            avg_spd = sum(v['speed'] for v in vehs)/max(total,1)
            by_type = {}
            for v in vehs:
                by_type[v['type']] = by_type.get(v['type'],0)+1

            with _lock:
                _vehicles_buf = vehs
                _sumo_stats   = {'total':total,'avg_speed':round(avg_spd,1),'by_type':by_type}

            elapsed = time.time()-tick
            time.sleep(max(0, interval-elapsed))
            tick = time.time()

        except Exception as e:
            estr = str(e)
            if 'FatalTraCI' in estr or 'connection' in estr.lower():
                print("[SUMO] Simulation ended — restarting...")
                try:
                    traci.close()
                    time.sleep(2)
                    traci.start([
                        'sumo','-c',SUMO_CFG,
                        '--no-step-log','true','--no-warnings','true',
                        '--step-length',str(STEP_LENGTH),
                        '--collision.action','remove',
                        '--time-to-teleport','60',
                    ])
                    _step_count=0
                    print("[SUMO] Restarted ✓")
                except Exception as re:
                    print(f"[SUMO] Restart failed: {re}"); break
            else:
                print(f"[SUMO] Step error: {e}")
                time.sleep(0.1)

# ════════════════════════════════════════════════════════════════════
# FLASK ENDPOINTS
# ════════════════════════════════════════════════════════════════════

@app.route('/api/traffic/health')
def health():
    with _lock: n=len(_vehicles_buf)
    return jsonify({'status':'ok','mode':_sumo_source,'vehicles':n,
                    'step':_step_count,'roads':len(_road_geojson),
                    'timestamp':datetime.now().isoformat()})

@app.route('/api/traffic/step')
def step():
    with _lock:
        vehs  = list(_vehicles_buf)
        stats = dict(_sumo_stats)
    if not vehs and _sumo_source in ('starting','synthetic'):
        vehs  = synth_engine.step()
        total = len(vehs)
        by_t  = {}
        for v in vehs: by_t[v['type']]=by_t.get(v['type'],0)+1
        stats = {'total':total,'avg_speed':round(sum(v['speed'] for v in vehs)/max(total,1),1),'by_type':by_t}
    return jsonify({'step':_step_count,'source':_sumo_source,'vehicles':vehs,'stats':stats})

@app.route('/api/traffic/routes')
def routes():
    """
    Returns pre-cached road network geometry (capped at 5,000 edges).
    Full 40k edges causes multi-hundred MB JSON + browser memory crash.
    5,000 edges drawn as a single GroundPolylinePrimitive covers the
    whole Enugu network visually at normal viewing distances.
    """
    MAX_EDGES = 5000
    if _road_geojson:
        data = _road_geojson
        if len(data) > MAX_EDGES:
            step = max(1, len(data) // MAX_EDGES)
            data = data[::step][:MAX_EDGES]
        return jsonify(data)
    # Fallback to hardcoded
    return jsonify([{'id':r['id'],'name':r['name'],
                     'coords':r['waypoints'],'speed_kmh':r['speed_kmh']}
                    for r in ROUTES])

@app.route('/api/traffic/flood-adjust', methods=['POST'])
def flood_adjust():
    wl=float((request.json or {}).get('water_level',0))
    if _sumo_source=='synthetic' and wl>0:
        for v in synth_engine.vehicles.values():
            v.speed=max(5,v.route['speed_kmh']*random.uniform(0.4,0.7))
    return jsonify({'ok':True})

# ════════════════════════════════════════════════════════════════════
# MAIN
# ════════════════════════════════════════════════════════════════════
if __name__=='__main__':
    print()
    print("="*62)
    print("  Enugu Smart City — 3D Traffic API   http://localhost:5002")
    print("="*62)

    # Step 1: Pre-load network (blocking, runs before Flask starts)
    preload_network()

    # Step 2: Start SUMO thread if files exist
    files_ok = os.path.exists(SUMO_CFG) and os.path.exists(NET_FILE)
    if USE_SUMO and files_ok:
        t=threading.Thread(target=sumo_thread,daemon=True)
        t.start()
        print("[SUMO] Background thread started — vehicles appear in ~5-10s")
    else:
        _sumo_source='synthetic'
        if not files_ok:
            print("[SUMO] Files missing — run setup_sumo_enugu.bat first")
        print(f"[Synthetic] {len(synth_engine.vehicles)} vehicles spawned")

    print()
    print("  GET  /api/traffic/health")
    print("  GET  /api/traffic/step    <- vehicle positions every 500ms")
    print("  GET  /api/traffic/routes  <- road network (instant from cache)")
    print("="*62)
    print()

    app.run(host='0.0.0.0',port=5002,debug=False,threaded=True)