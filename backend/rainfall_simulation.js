/**
 * rainfall_simulation.js
 * Enugu Smart City — Real-Time Rainfall → Flood Simulation Module
 * ================================================================
 * Add to index.html AFTER main.js:
 *   <script src="rainfall_simulation.js"></script>
 *
 * SCIENTIFIC BASIS
 * ─────────────────
 * Uses the Rational Method (Q = CiA) — the standard for Nigerian urban
 * drainage design (FMWR 2018, Manual of Standards and Specifications):
 *
 *   Q   = peak runoff (m³/s)
 *   C   = runoff coefficient (depends on land use)
 *   i   = rainfall intensity (mm/hr) from the slider
 *   A   = catchment area
 *
 * Water level above channel = Q_accumulated / channel_capacity
 * Buildings with HAND < water_level become flooded.
 *
 * The simulation runs in "smart city real-time" — 1 second of wall-clock
 * time represents 5 minutes of rainfall accumulation, so a 60-minute
 * storm plays out in 12 seconds (adjustable with speed control).
 *
 * INTEGRATION WITH EXISTING SYSTEM
 * ──────────────────────────────────
 * Reads floodBuildingRisks[] from main.js (already loaded).
 * Adds its own CesiumJS entities tagged with name='rainfall_*' for
 * easy cleanup. Does NOT modify any existing entities.
 */

(function (global) {
    'use strict';

    // ── CONFIG ────────────────────────────────────────────────────────────────
    const SIM = {
        // Time compression: 1 wall-clock second = SIM_MINUTES_PER_SECOND
        // of storm time
        MINUTES_PER_TICK:  5,       // storm minutes per simulation tick
        TICK_MS:         500,       // wall-clock ms per tick (2 ticks/sec)

        // Rational Method constants for Enugu urban mix
        // (weighted average of residential + commercial + road)
        RUNOFF_COEFF: 0.65,         // C value (0=permeable, 1=impermeable)

        // Channel capacity: water level (m) above channel that triggers
        // flooding at a given HAND threshold
        // Derived from Manning's equation for typical Enugu drains
        BASE_CHANNEL_CAPACITY: 0.8, // m of water before overflow begins

        // Enugu IDF curve parameters (Gumbel distribution fit)
        // intensity (mm/hr) = a / (t + b)^n  for t in minutes
        IDF_A: 1800,
        IDF_B: 15,
        IDF_N: 0.85,
    };

    // Rainfall scenarios (mm/hr at peak intensity)
    const SCENARIOS = {
        dry:          { label: '☀ Dry Season',        intensity:   0, color: '#95A5A6' },
        light:        { label: '🌦 Light Rain',        intensity:  15, color: '#74B9FF' },
        moderate:     { label: '🌧 Moderate',          intensity:  40, color: '#0984E3' },
        heavy:        { label: '⛈ Heavy Storm',       intensity:  80, color: '#6C5CE7' },
        extreme:      { label: '🌀 Extreme Event',     intensity: 130, color: '#D63031' },
        custom:       { label: '🎛 Custom',            intensity:   0, color: '#FDCB6E' },
    };

    // Risk level HAND thresholds (must match flood_analysis.py)
    const HAND_THRESH = {
        'High Risk':        1.0,
        'Medium-High Risk': 3.0,
        'Medium Risk':      6.0,
        'Low Risk':        10.0,
    };

    // Category colours matching RISK_PALETTE in main.js
    const FLOOD_COLORS = {
        'High Risk':        new Cesium.Color(0.86, 0.08, 0.08, 0.90),
        'Medium-High Risk': new Cesium.Color(1.00, 0.47, 0.00, 0.85),
        'Medium Risk':      new Cesium.Color(1.00, 0.84, 0.00, 0.78),
        'Low Risk':         new Cesium.Color(0.12, 0.71, 0.24, 0.70),
    };

    // ── STATE ─────────────────────────────────────────────────────────────────
    let simRunning       = false;
    let simTick          = 0;          // elapsed ticks
    let simInterval      = null;
    let currentIntensity = 0;          // mm/hr
    let currentScenario  = 'dry';
    let accumulatedMm    = 0;          // total rainfall accumulated this storm (mm)
    let waterLevel       = 0;          // simulated water level above channel (m)
    let floodedEntities  = [];         // Cesium entities added by simulation
    let rainParticles    = [];         // DOM rain particle elements
    let alertsIssued     = new Set();  // prevent duplicate alerts

    // ── WAIT FOR MAIN.JS ──────────────────────────────────────────────────────
    function waitReady(cb) {
        if (window.viewer && typeof window.floodBuildingRisks !== 'undefined') {
            return cb();
        }
        setTimeout(() => waitReady(cb), 400);
    }

    // ── PANEL HTML ────────────────────────────────────────────────────────────
    function buildPanel() {
        if (document.getElementById('rainfall-panel')) return;

        const panel = document.createElement('div');
        panel.id    = 'rainfall-panel';
        panel.innerHTML = `
            <div class="rp-header">
                <span>🌧 Rainfall Simulation</span>
                <button id="rp-collapse" title="Collapse">−</button>
            </div>

            <div id="rp-body">
                <!-- Scenario presets -->
                <div class="rp-section-label">Scenario</div>
                <div class="rp-scenarios">
                    ${Object.entries(SCENARIOS).filter(([k])=>k!=='custom').map(([k,v])=>`
                    <button class="rp-scenario-btn" data-scenario="${k}"
                        style="border-color:${v.color}">${v.label}</button>`).join('')}
                </div>

                <!-- Custom intensity slider -->
                <div class="rp-section-label" style="margin-top:10px">
                    Intensity: <span id="rp-intensity-val">0</span> mm/hr
                </div>
                <input id="rp-intensity" type="range" min="0" max="150" value="0" class="rp-slider">

                <!-- Storm duration -->
                <div class="rp-section-label" style="margin-top:8px">
                    Storm Duration: <span id="rp-duration-val">60</span> min
                </div>
                <input id="rp-duration" type="range" min="10" max="180" value="60" step="10" class="rp-slider">

                <!-- Sim speed -->
                <div class="rp-section-label" style="margin-top:8px">
                    Sim Speed: <span id="rp-speed-val">5</span>× real-time
                </div>
                <input id="rp-speed" type="range" min="1" max="20" value="5" class="rp-slider">

                <!-- Controls -->
                <div style="display:flex;gap:8px;margin-top:12px">
                    <button id="rp-start" class="rp-btn primary">▶ Run Simulation</button>
                    <button id="rp-reset" class="rp-btn">↺ Reset</button>
                </div>

                <!-- Live metrics -->
                <div class="rp-metrics" id="rp-metrics" style="display:none">
                    <div class="rp-metric">
                        <span class="rp-metric-val" id="rp-m-time">0</span>
                        <span class="rp-metric-lbl">Storm min</span>
                    </div>
                    <div class="rp-metric">
                        <span class="rp-metric-val" id="rp-m-accum">0</span>
                        <span class="rp-metric-lbl">mm total</span>
                    </div>
                    <div class="rp-metric">
                        <span class="rp-metric-val" id="rp-m-wl">0.00</span>
                        <span class="rp-metric-lbl">m water</span>
                    </div>
                    <div class="rp-metric">
                        <span class="rp-metric-val" id="rp-m-flooded" style="color:#DC1414">0</span>
                        <span class="rp-metric-lbl">flooded</span>
                    </div>
                </div>

                <!-- Progress bar -->
                <div class="rp-progress-wrap" id="rp-progress-wrap" style="display:none">
                    <div class="rp-progress-bar">
                        <div id="rp-progress-fill" style="width:0%;background:#4682DC"></div>
                    </div>
                    <div id="rp-status-text" style="font-size:10px;text-align:center;margin-top:4px;opacity:.6">—</div>
                </div>

                <!-- Alerts -->
                <div id="rp-alerts"></div>
            </div>
        `;
        document.body.appendChild(panel);
        injectCSS();
        attachEvents();
    }

    function injectCSS() {
        if (document.getElementById('rp-css')) return;
        const s = document.createElement('style'); s.id = 'rp-css';
        s.textContent = `
            /* ── Rainfall simulation panel ──────────── */
            #rainfall-panel {
                position: absolute;
                bottom: 140px;
                right: 16px;
                width: 285px;
                background: linear-gradient(160deg, rgba(5,8,20,.97), rgba(10,16,35,.97));
                border: 1px solid rgba(70,130,220,.35);
                border-left: 4px solid #4682DC;
                border-radius: 14px;
                padding: 0;
                color: #dde4f0;
                font: 12px/1.4 'Segoe UI', system-ui, sans-serif;
                z-index: 500;
                box-shadow: 0 10px 40px rgba(0,0,0,.65);
                backdrop-filter: blur(16px);
                overflow: hidden;
            }
            .rp-header {
                display: flex; justify-content: space-between; align-items: center;
                padding: 11px 14px; font-weight: 700; font-size: 12px;
                letter-spacing: .06em; text-transform: uppercase; color: #74B9FF;
                background: rgba(70,130,220,.1);
                border-bottom: 1px solid rgba(70,130,220,.2);
            }
            .rp-header button {
                background: none; border: none; color: #74B9FF; font-size: 16px;
                cursor: pointer; line-height: 1; padding: 0 2px;
            }
            #rp-body { padding: 12px 14px 14px; }
            #rp-body.collapsed { display: none; }
            .rp-section-label {
                font-size: 10px; font-weight: 700; letter-spacing: .07em;
                text-transform: uppercase; opacity: .55; margin-bottom: 5px;
            }
            .rp-scenarios {
                display: flex; flex-wrap: wrap; gap: 5px;
            }
            .rp-scenario-btn {
                flex: 1; min-width: 80px; padding: 5px 4px; font-size: 10px;
                background: rgba(255,255,255,.04); border: 1px solid rgba(255,255,255,.15);
                color: #dde4f0; border-radius: 6px; cursor: pointer; transition: all .15s;
                text-align: center; white-space: nowrap;
            }
            .rp-scenario-btn:hover { background: rgba(70,130,220,.2); }
            .rp-scenario-btn.active { background: rgba(70,130,220,.3); border-color: #4682DC; color: #fff; }
            .rp-slider {
                width: 100%; height: 4px; -webkit-appearance: none; appearance: none;
                background: rgba(255,255,255,.12); border-radius: 2px; outline: none; cursor: pointer;
            }
            .rp-slider::-webkit-slider-thumb {
                -webkit-appearance: none; width: 14px; height: 14px;
                border-radius: 50%; background: #4682DC; cursor: pointer;
                border: 2px solid rgba(255,255,255,.4);
            }
            .rp-btn {
                flex: 1; padding: 8px; border: 1px solid rgba(255,255,255,.15);
                background: rgba(255,255,255,.05); color: #dde4f0; border-radius: 8px;
                font-size: 11px; font-weight: 600; cursor: pointer; transition: all .15s;
            }
            .rp-btn:hover { background: rgba(70,130,220,.2); }
            .rp-btn.primary {
                background: linear-gradient(90deg,#1a3a6b,#2d5aa8);
                border-color: rgba(70,130,220,.5); color: #fff;
            }
            .rp-btn.primary:hover { opacity: .88; }
            .rp-btn.running {
                background: linear-gradient(90deg,#6b1a1a,#a82d2d) !important;
                border-color: rgba(220,20,20,.5) !important;
            }
            .rp-metrics {
                display: grid; grid-template-columns: repeat(4,1fr);
                gap: 5px; margin-top: 12px; background: rgba(0,0,0,.3);
                border-radius: 8px; padding: 8px 5px;
            }
            .rp-metric { text-align: center; }
            .rp-metric-val { display: block; font-size: 16px; font-weight: 800; color: #74B9FF; line-height: 1; }
            .rp-metric-lbl { display: block; font-size: 8px; opacity: .5; margin-top: 2px; text-transform: uppercase; letter-spacing: .05em; }
            .rp-progress-wrap { margin-top: 10px; }
            .rp-progress-bar {
                height: 5px; background: rgba(255,255,255,.1); border-radius: 3px; overflow: hidden;
            }
            #rp-progress-fill { height: 100%; border-radius: 3px; transition: width .4s, background .5s; }
            /* Flood alerts */
            .rp-alert {
                margin-top: 6px; padding: 6px 8px; border-radius: 7px;
                font-size: 10px; font-weight: 600; border-left: 3px solid;
                animation: rp-fadein .3s ease;
            }
            @keyframes rp-fadein { from{opacity:0;transform:translateY(4px)} to{opacity:1;transform:none} }
            .rp-alert.high  { background:rgba(220,20,20,.15); border-color:#DC1414; color:#FF7B7B; }
            .rp-alert.warn  { background:rgba(255,120,0,.12); border-color:#FF7800; color:#FFB07B; }
            .rp-alert.info  { background:rgba(70,130,220,.12); border-color:#4682DC; color:#74B9FF; }
            /* Rain particles overlay */
            #rp-rain-overlay {
                position:fixed; top:0; left:0; width:100%; height:100%;
                pointer-events:none; z-index:9998; overflow:hidden;
            }
            .rp-drop {
                position:absolute; width:1.5px; border-radius:2px;
                background:linear-gradient(to bottom,rgba(100,160,255,0),rgba(100,160,255,.7));
                animation:rp-fall linear infinite;
            }
            @keyframes rp-fall {
                0%  { transform:translateY(-20px); opacity:0; }
                10% { opacity:1; }
                90% { opacity:.8; }
                100%{ transform:translateY(110vh); opacity:0; }
            }
        `;
        document.head.appendChild(s);
    }

    function attachEvents() {
        // Collapse toggle
        document.getElementById('rp-collapse').onclick = () => {
            const body = document.getElementById('rp-body');
            body.classList.toggle('collapsed');
            document.getElementById('rp-collapse').textContent =
                body.classList.contains('collapsed') ? '+' : '−';
        };

        // Scenario buttons
        document.querySelectorAll('.rp-scenario-btn').forEach(btn => {
            btn.addEventListener('click', () => {
                document.querySelectorAll('.rp-scenario-btn').forEach(b=>b.classList.remove('active'));
                btn.classList.add('active');
                currentScenario = btn.dataset.scenario;
                const s = SCENARIOS[currentScenario];
                currentIntensity = s.intensity;
                document.getElementById('rp-intensity').value = currentIntensity;
                document.getElementById('rp-intensity-val').textContent = currentIntensity;
            });
        });

        // Intensity slider
        document.getElementById('rp-intensity').addEventListener('input', e => {
            currentIntensity = parseFloat(e.target.value);
            document.getElementById('rp-intensity-val').textContent = currentIntensity;
            currentScenario = 'custom';
            document.querySelectorAll('.rp-scenario-btn').forEach(b=>b.classList.remove('active'));
        });

        // Duration slider
        document.getElementById('rp-duration').addEventListener('input', e => {
            document.getElementById('rp-duration-val').textContent = e.target.value;
        });

        // Speed slider
        document.getElementById('rp-speed').addEventListener('input', e => {
            document.getElementById('rp-speed-val').textContent = e.target.value;
            if (simRunning) {
                // Restart interval with new speed
                clearInterval(simInterval);
                const ms = Math.round(SIM.TICK_MS / parseFloat(e.target.value));
                simInterval = setInterval(tick, ms);
            }
        });

        // Start/stop
        document.getElementById('rp-start').addEventListener('click', () => {
            if (simRunning) stopSimulation();
            else startSimulation();
        });

        // Reset
        document.getElementById('rp-reset').addEventListener('click', resetSimulation);
    }

    // ── RAIN PARTICLE OVERLAY ─────────────────────────────────────────────────
    function startRainOverlay(intensity) {
        stopRainOverlay();
        if (intensity < 5) return;

        const overlay = document.createElement('div');
        overlay.id = 'rp-rain-overlay';
        document.body.appendChild(overlay);

        // Scale drop count with intensity (5 drops at 15mm/hr, 40 at 150mm/hr)
        const count = Math.round(5 + (intensity / 150) * 35);
        for (let i = 0; i < count; i++) {
            const drop = document.createElement('div');
            drop.className = 'rp-drop';
            const dur    = 0.4 + Math.random() * 0.6;   // 0.4–1.0s fall
            const height = 10 + (intensity / 150) * 25;  // 10–35px long
            drop.style.cssText = `
                left:${Math.random()*100}%;
                height:${height}px;
                animation-duration:${dur}s;
                animation-delay:${Math.random()*dur}s;
                opacity:${0.4 + (intensity/150)*0.5};
            `;
            overlay.appendChild(drop);
            rainParticles.push(drop);
        }
    }

    function stopRainOverlay() {
        const el = document.getElementById('rp-rain-overlay');
        if (el) el.remove();
        rainParticles = [];
    }

    // ── SIMULATION CORE ───────────────────────────────────────────────────────
    function startSimulation() {
        if (currentIntensity === 0) {
            addAlert('Set rainfall intensity before running.', 'info');
            return;
        }
        simRunning    = true;
        simTick       = 0;
        accumulatedMm = 0;
        waterLevel    = 0;
        alertsIssued  = new Set();

        document.getElementById('rp-start').textContent = '⏹ Stop';
        document.getElementById('rp-start').classList.add('running');
        document.getElementById('rp-metrics').style.display  = 'grid';
        document.getElementById('rp-progress-wrap').style.display = 'block';
        document.getElementById('rp-alerts').innerHTML = '';

        clearFloodSimulation();
        startRainOverlay(currentIntensity);

        const speed = parseFloat(document.getElementById('rp-speed').value);
        const ms    = Math.round(SIM.TICK_MS / speed);
        simInterval = setInterval(tick, ms);
        addAlert(`Storm started: ${currentIntensity} mm/hr — ${document.getElementById('rp-duration').value} min storm`, 'info');
    }

    function tick() {
        const durationMin = parseFloat(document.getElementById('rp-duration').value);
        const elapsedMin  = simTick * SIM.MINUTES_PER_TICK;

        if (elapsedMin >= durationMin) {
            // Storm ends — drainage recession phase
            drainRecession();
            return;
        }

        simTick++;

        // Rational Method: accumulate rainfall
        // intensity can vary over time using IDF curve shape (peaks then decays)
        const progress     = elapsedMin / durationMin;
        const peakFactor   = Math.sin(progress * Math.PI);   // bell curve 0→1→0
        const activeIntens = currentIntensity * (0.3 + 0.7 * peakFactor);
        const mmThisTick   = (activeIntens / 60) * SIM.MINUTES_PER_TICK;
        accumulatedMm     += mmThisTick;

        // Runoff volume → water level above channel (simplified Manning's)
        const runoff    = accumulatedMm * SIM.RUNOFF_COEFF;
        waterLevel      = Math.max(0, runoff / 40 - SIM.BASE_CHANNEL_CAPACITY);

        updateFloodVisualization(waterLevel, elapsedMin, durationMin);
        updateMetrics(elapsedMin, durationMin);
        issueAlerts(waterLevel, elapsedMin);
    }

    function drainRecession() {
        // Simulate drainage after storm ends (exponential decay)
        clearInterval(simInterval);
        addAlert('Storm ended — drainage underway…', 'info');

        let drainTick = 0;
        const initialWL = waterLevel;
        simInterval = setInterval(() => {
            drainTick++;
            waterLevel = initialWL * Math.exp(-drainTick * 0.15);
            if (waterLevel < 0.01) {
                waterLevel = 0;
                clearInterval(simInterval);
                simInterval = null;
                simRunning  = false;
                document.getElementById('rp-start').textContent = '▶ Run Simulation';
                document.getElementById('rp-start').classList.remove('running');
                stopRainOverlay();
                clearFloodSimulation();
                addAlert('✅ Drainage complete — flood zones cleared', 'info');
                setStatusText('Simulation complete');
                return;
            }
            updateFloodVisualization(waterLevel, null, null);
            document.getElementById('rp-m-wl').textContent = waterLevel.toFixed(2);
        }, 600);
    }

    function stopSimulation() {
        clearInterval(simInterval); simInterval = null;
        simRunning = false;
        document.getElementById('rp-start').textContent = '▶ Run Simulation';
        document.getElementById('rp-start').classList.remove('running');
        stopRainOverlay();
        addAlert('Simulation paused', 'info');
    }

    function resetSimulation() {
        stopSimulation();
        clearFloodSimulation();
        stopRainOverlay();
        simTick = 0; accumulatedMm = 0; waterLevel = 0;
        alertsIssued = new Set();
        document.getElementById('rp-metrics').style.display      = 'none';
        document.getElementById('rp-progress-wrap').style.display = 'none';
        document.getElementById('rp-alerts').innerHTML = '';
        document.getElementById('rp-progress-fill').style.width   = '0%';
        setStatusText('—');
    }

    // ── FLOOD VISUALISATION ───────────────────────────────────────────────────
    function updateFloodVisualization(wl, elapsedMin, durationMin) {
        clearFloodSimulation();

        if (!window.floodBuildingRisks || !window.floodBuildingRisks.length) return;
        if (wl <= 0) return;

        // Determine which buildings are currently flooded given water level
        // A building floods when waterLevel > HAND_value
        // (its HAND is the metres it sits above the nearest drain)
        const currentlyFlooded = window.floodBuildingRisks.filter(b => {
            const h = typeof b.hand_m === 'number' ? b.hand_m : 99;
            return h < wl;
        });

        // Group by risk class and draw one canvas per class
        const groups = {};
        currentlyFlooded.forEach(b => {
            const cls = b.flood_risk || 'High Risk';
            if (!groups[cls]) groups[cls] = [];
            groups[cls].push(b);
        });

        const bounds = window.enuguBounds || { west:7.45, east:7.55, south:6.40, north:6.50 };
        const pad    = 0.004;
        const rect   = Cesium.Rectangle.fromDegrees(
            bounds.west-pad, bounds.south-pad, bounds.east+pad, bounds.north+pad);

        Object.entries(groups).forEach(([cls, buildings], i) => {
            if (!buildings.length) return;
            const cesColor = FLOOD_COLORS[cls] || new Cesium.Color(0.86,0.08,0.08,0.85);

            // Build canvas for this flooded class
            const canvas = buildSimCanvas(buildings, cls, wl);
            const entity = window.viewer.entities.add({
                name: `rainfall_flood_${cls}`,
                rectangle: {
                    coordinates: rect,
                    material:    new Cesium.ImageMaterialProperty({
                        image:       canvas,
                        transparent: true,
                        color:       new Cesium.Color(1,1,1,1),
                    }),
                    height:             60 + i * 2,
                    classificationType: Cesium.ClassificationType.TERRAIN,
                    zIndex:             100 + i,
                }
            });
            floodedEntities.push(entity);
        });

        // Update flooded count metric
        document.getElementById('rp-m-flooded').textContent = currentlyFlooded.length.toLocaleString();
    }

    function buildSimCanvas(buildings, riskClass, waterLevel) {
        const W = 1024, H = 768;
        const canvas = document.createElement('canvas');
        canvas.width = W; canvas.height = H;
        const ctx = canvas.getContext('2d');
        ctx.clearRect(0,0,W,H);

        const bounds   = window.enuguBounds || { west:7.45, east:7.55, south:6.40, north:6.50 };
        const lonRange = bounds.east  - bounds.west;
        const latRange = bounds.north - bounds.south;

        // Water depth determines circle size and opacity
        const maxWL = 7.0;  // m

        buildings.forEach(b => {
            const depth = Math.max(0, waterLevel - (b.hand_m || 0));
            if (depth <= 0) return;

            const px = ((b.lon - bounds.west)  / lonRange) * W;
            const py = (1 - (b.lat - bounds.south) / latRange) * H;
            const r  = 8 + (depth / maxWL) * 20;
            const a  = Math.min(0.92, 0.5 + (depth / maxWL) * 0.42);

            // Animated "ripple" effect via slightly randomised radius jitter
            const jitter = 1 + Math.sin(Date.now()/500 + px) * 0.08;

            ctx.globalCompositeOperation = 'source-over';
            ctx.beginPath();
            ctx.arc(px, py, r * jitter, 0, 2*Math.PI);

            const pal = { 'High Risk':[220,20,20], 'Medium-High Risk':[255,120,0],
                          'Medium Risk':[255,215,0], 'Low Risk':[30,180,60] };
            const [cr,cg,cb] = pal[riskClass] || [70,130,220];
            ctx.fillStyle = `rgba(${cr},${cg},${cb},${a})`;
            ctx.fill();
        });

        return canvas;
    }

    function clearFloodSimulation() {
        if (!window.viewer) return;
        floodedEntities.forEach(e => window.viewer.entities.remove(e));
        floodedEntities = [];
    }

    // ── METRICS & STATUS ──────────────────────────────────────────────────────
    function updateMetrics(elapsedMin, durationMin) {
        document.getElementById('rp-m-time').textContent   = Math.round(elapsedMin);
        document.getElementById('rp-m-accum').textContent  = Math.round(accumulatedMm);
        document.getElementById('rp-m-wl').textContent     = waterLevel.toFixed(2);

        const pct = Math.min(100, (elapsedMin / durationMin) * 100);
        const fill = document.getElementById('rp-progress-fill');
        fill.style.width = pct + '%';
        // Colour the bar: blue → orange → red as storm progresses
        fill.style.background = pct < 40 ? '#4682DC' : pct < 75 ? '#FF7800' : '#DC1414';

        setStatusText(`${Math.round(elapsedMin)}/${durationMin} min · ${Math.round(accumulatedMm)} mm accumulated`);
    }

    function setStatusText(t) {
        const el = document.getElementById('rp-status-text');
        if (el) el.textContent = t;
    }

    // ── ALERTS ────────────────────────────────────────────────────────────────
    function issueAlerts(wl, elapsedMin) {
        // Issue contextual alerts as water level crosses thresholds
        if (wl >= HAND_THRESH['High Risk'] && !alertsIssued.has('high')) {
            alertsIssued.add('high');
            addAlert(`🚨 High Risk areas inundating — water level ${wl.toFixed(2)} m above drainage`, 'high');
            if (window.showNotification) window.showNotification('🚨 FLOOD WARNING', 'High risk zones now flooding', 'error');
        }
        if (wl >= HAND_THRESH['Medium-High Risk'] && !alertsIssued.has('mh')) {
            alertsIssued.add('mh');
            addAlert(`⚠ Medium-High zones flooding — water level ${wl.toFixed(2)} m`, 'warn');
        }
        if (wl >= HAND_THRESH['Medium Risk'] && !alertsIssued.has('med')) {
            alertsIssued.add('med');
            addAlert(`⚠ Medium risk zones flooded — emergency routes may be blocked`, 'warn');
        }
        if (elapsedMin > 30 && wl > 0 && !alertsIssued.has('routes')) {
            alertsIssued.add('routes');
            addAlert('ℹ Check emergency dispatch routes for flood interference', 'info');
        }
    }

    function addAlert(msg, type) {
        const alerts = document.getElementById('rp-alerts');
        if (!alerts) return;
        const div = document.createElement('div');
        div.className = `rp-alert ${type}`;
        div.textContent = msg;
        alerts.appendChild(div);
        // Keep only last 5 alerts
        while (alerts.children.length > 5) alerts.removeChild(alerts.firstChild);
        alerts.scrollTop = alerts.scrollHeight;
    }

    // ── INIT ──────────────────────────────────────────────────────────────────
    function init() {
        buildPanel();
        console.log('[RainfallSim] ✓ Panel ready');
    }

    waitReady(init);

})(window);
