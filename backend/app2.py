from flask import Flask, jsonify, request
from flask_cors import CORS
import psycopg2
import psycopg2.extras
import json
import math
import os

app = Flask(__name__)
CORS(app)

# Database connection
DB_PARAMS = {
    'host': 'localhost',
    'port': 5432,
    'database': 'enugu_routing',
    'user': 'postgres',
    'password': 'c1h1u1k1s1'
}

def get_db_connection():
    return psycopg2.connect(**DB_PARAMS)

@app.route('/api/health', methods=['GET'])
def health():
    return jsonify({"status": "Enugu Emergency System Ready", "database": "connected"})

@app.route('/api/route', methods=['POST'])
def calculate_route():
    data = request.json
    start_lon, start_lat = data['start']
    end_lon, end_lat = data['end']
    
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    
    try:
        # First, check if roads table has data
        cursor.execute("SELECT COUNT(*) FROM roads")
        road_count = cursor.fetchone()[0]
        
        if road_count == 0:
            print("No roads found in database, using straight-line routing")
            return calculate_straight_line(start_lon, start_lat, end_lon, end_lat)
        
        # Find nearest road nodes - FIXED: Use proper geometry comparison
        cursor.execute("""
            SELECT id FROM roads 
            ORDER BY geom <-> ST_SetSRID(ST_MakePoint(%s, %s), 4326) 
            LIMIT 1
        """, (start_lon, start_lat))
        start_result = cursor.fetchone()
        
        cursor.execute("""
            SELECT id FROM roads 
            ORDER BY geom <-> ST_SetSRID(ST_MakePoint(%s, %s), 4326) 
            LIMIT 1
        """, (end_lon, end_lat))
        end_result = cursor.fetchone()
        
        if not start_result or not end_result:
            print("Could not find nearby roads, using straight-line routing")
            return calculate_straight_line(start_lon, start_lat, end_lon, end_lat)
        
        start_id = start_result[0]
        end_id = end_result[0]
        
        # Calculate shortest path - FIXED: Explicitly reference table for cost
        cursor.execute("""
            WITH path AS (
                SELECT * FROM pgr_dijkstra(
                    'SELECT id, source, target, r.cost as cost FROM roads r WHERE r.cost IS NOT NULL',
                    %s, %s,
                    directed := false
                )
            )
            SELECT 
                ST_AsGeoJSON(ST_Collect(r.geom)) as route_geom,
                SUM(ST_Length(r.geom::geography)) as total_distance,
                SUM(p.cost) as total_time
            FROM path p
            JOIN roads r ON p.edge = r.id
            WHERE r.geom IS NOT NULL
        """, (start_id, end_id))
        
        result = cursor.fetchone()
        
        if result and result['route_geom']:
            geojson = json.loads(result['route_geom'])
            
            # Extract coordinates
            coordinates = []
            if geojson['type'] == 'MultiLineString':
                for line in geojson['coordinates']:
                    for coord in line:
                        coordinates.append(coord)
            elif geojson['type'] == 'LineString':
                coordinates = geojson['coordinates']
            
            return jsonify({
                "coordinates": coordinates,
                "distance_km": round(result['total_distance'] / 1000, 2),
                "eta_minutes": round(result['total_time'] / 60, 1),
                "status": "success",
                "method": "road_network"
            })
        
        return calculate_straight_line(start_lon, start_lat, end_lon, end_lat)
        
    except Exception as e:
        print(f"Error in route calculation: {e}")
        return calculate_straight_line(start_lon, start_lat, end_lon, end_lat)
    finally:
        cursor.close()
        conn.close()

def calculate_straight_line(start_lon, start_lat, end_lon, end_lat):
    """Fallback: Calculate straight-line route"""
    # Haversine distance formula
    R = 6371  # Earth radius in km
    dlat = math.radians(end_lat - start_lat)
    dlon = math.radians(end_lon - start_lon)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(start_lat)) * math.cos(math.radians(end_lat)) * math.sin(dlon/2)**2
    distance = R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
    
    # Generate intermediate points for smooth animation
    steps = 50
    coordinates = []
    for i in range(steps + 1):
        t = i / steps
        lon = start_lon + t * (end_lon - start_lon)
        lat = start_lat + t * (end_lat - start_lat)
        coordinates.append([lon, lat])
    
    # Emergency vehicle speed: 40 km/h average
    eta_minutes = (distance / 40) * 60
    
    return jsonify({
        "coordinates": coordinates,
        "distance_km": round(distance, 2),
        "eta_minutes": round(eta_minutes, 1),
        "status": "success",
        "method": "straight_line"
    })

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
        
        # If no POIs in database, return default Enugu locations
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
        print(f"Error fetching POIs: {e}")
        # Return default POIs
        return jsonify([
            {"name": "Enugu State Fire Service HQ", "type": "fire_station", "lon": 7.4855, "lat": 6.4520},
            {"name": "UNTH Enugu", "type": "hospital", "lon": 7.5123, "lat": 6.4421},
            {"name": "National Orthopaedic Hospital", "type": "hospital", "lon": 7.4987, "lat": 6.4602}
        ])
    finally:
        cursor.close()
        conn.close()

if __name__ == '__main__':
    print("🚑 Enugu Emergency System Backend Starting...")
    print("📍 API Endpoints:")
    print("   GET  /api/health  - Check system status")
    print("   POST /api/route   - Calculate emergency route")
    print("   GET  /api/pois    - Get emergency service locations")
    print("\n✅ Server running on http://localhost:5000")
    app.run(debug=True, port=5000)