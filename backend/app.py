from flask import Flask, jsonify, request
from flask_cors import CORS
import psycopg2
import psycopg2.extras
import requests
import math

app = Flask(__name__)
CORS(app)

# ============================================
# DATABASE CONFIG (for POIs only)
# ============================================

DB_PARAMS = {
    'host': 'localhost',
    'port': 5432,
    'database': 'enugu_routing',
    'user': 'postgres',
    'password': 'c1h1u1k1s1'
}

def get_db_connection():
    return psycopg2.connect(**DB_PARAMS)

# ============================================
# HEALTH CHECK
# ============================================

@app.route('/api/health', methods=['GET'])
def health():
    return jsonify({"status": "🚑 Enugu Emergency System (OSRM) Ready"})

# ============================================
# 🚀 OSRM ROUTING FUNCTION (MAIN FIX)
# ============================================

def calculate_route_osrm(start_lon, start_lat, end_lon, end_lat):
    try:
        print(f"\n📍 OSRM Routing from ({start_lon},{start_lat}) → ({end_lon},{end_lat})")

        url = f"http://router.project-osrm.org/route/v1/driving/{start_lon},{start_lat};{end_lon},{end_lat}?overview=full&geometries=geojson"

        response = requests.get(url)
        data = response.json()

        # Debug response
        print("📡 OSRM response code:", data.get("code"))

        if data.get("code") != "Ok":
            print("❌ OSRM failed → fallback to straight line")
            return calculate_straight_line(start_lon, start_lat, end_lon, end_lat)

        route = data["routes"][0]

        coordinates = route["geometry"]["coordinates"]
        distance_km = route["distance"] / 1000
        eta_minutes = route["duration"] / 60

        print(f"✅ OSRM route: {distance_km:.2f} km, {eta_minutes:.1f} min")

        return jsonify({
            "coordinates": coordinates,
            "distance_km": round(distance_km, 2),
            "eta_minutes": round(eta_minutes, 1),
            "status": "success",
            "method": "osrm"
        })

    except Exception as e:
        print("❌ OSRM error:", e)
        return calculate_straight_line(start_lon, start_lat, end_lon, end_lat)

# ============================================
# API ROUTE ENDPOINT
# ============================================

@app.route('/api/route', methods=['POST'])
def route():
    data = request.json

    start_lon, start_lat = data['start']
    end_lon, end_lat = data['end']

    return calculate_route_osrm(start_lon, start_lat, end_lon, end_lat)

# ============================================
# FALLBACK (STRAIGHT LINE)
# ============================================

def calculate_straight_line(start_lon, start_lat, end_lon, end_lat):
    R = 6371

    dlat = math.radians(end_lat - start_lat)
    dlon = math.radians(end_lon - start_lon)

    a = math.sin(dlat/2)**2 + math.cos(math.radians(start_lat)) * math.cos(math.radians(end_lat)) * math.sin(dlon/2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))

    distance = R * c

    steps = 50
    coordinates = []

    for i in range(steps + 1):
        t = i / steps
        lon = start_lon + t * (end_lon - start_lon)
        lat = start_lat + t * (end_lat - start_lat)
        coordinates.append([lon, lat])

    eta_minutes = (distance / 40) * 60

    print(f"📏 Straight-line fallback: {distance:.2f} km")

    return jsonify({
        "coordinates": coordinates,
        "distance_km": round(distance, 2),
        "eta_minutes": round(eta_minutes, 1),
        "status": "success",
        "method": "straight_line"
    })

# ============================================
# POIs (UNCHANGED)
# ============================================

@app.route('/api/pois', methods=['GET'])
def get_pois():
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

    try:
        cursor.execute("""
            SELECT name, type, ST_X(geom) as lon, ST_Y(geom) as lat 
            FROM emergency_pois
            WHERE geom IS NOT NULL
        """)

        pois = [dict(row) for row in cursor.fetchall()]

        if not pois:
            pois = [
                {"name": "Enugu State Fire Service HQ", "type": "fire_station", "lon": 7.4855, "lat": 6.4520},
                {"name": "UNTH Enugu", "type": "hospital", "lon": 7.5123, "lat": 6.4421},
                {"name": "National Orthopaedic Hospital", "type": "hospital", "lon": 7.4987, "lat": 6.4602},
                {"name": "Enugu Central Fire Station", "type": "fire_station", "lon": 7.4789, "lat": 6.4450},
                {"name": "Police Zone 13 HQ", "type": "police", "lon": 7.4892, "lat": 6.4485}
            ]

        return jsonify(pois)

    except Exception as e:
        print("❌ POI error:", e)
        return jsonify([])

    finally:
        cursor.close()
        conn.close()

# ============================================
# RUN SERVER
# ============================================

if __name__ == '__main__':
    print("🚑 Enugu Emergency System (OSRM) starting...")
    app.run(debug=True, port=5000)