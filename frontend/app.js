/**
 * frontend/app.js
 * Modular Vanilla JavaScript controller for ChargeGrid P2P.
 *
 * Implements:
 * 1. No Initial Login Wall: Map, commercial EV stations, P2P host pins, and Smart Calculator
 *    load immediately for everyone without showing host phone numbers.
 * 2. Triggered Authentication (Gmail + OTP): Prompts login ONLY when clicking:
 *    - "Contact for Charge"
 *    - "Save Address"
 *    - "History"
 *    Then automatically resumes the user's triggered action upon OTP verification.
 * 3. Smart Calculator: `Total Cost = (Units Needed * Local Area Unit Cost) + 200 Rupees`
 * 4. Negotiation: One-tap price boost (`+₹50`, `+₹100`, `+₹200` or custom) when a provider refuses.
 * 5. Top-Right Account Dropdown: "History", "Logout", and "Delete Account" (with strict warning).
 */

(function () {
  'use strict';

  const state = {
    theme: 'dark',
    authenticated: false,
    user: null,
    centerLat: 30.7418,
    centerLng: 76.7825,
    providers: [],
    commercialStations: [],
    calculation: {
      car_model: 'Tata Nexon EV',
      battery_capacity_kwh: 30,
      current_battery_pct: 15,
      desired_battery_pct: 65,
      units_needed_kwh: 15.0,
      unit_cost_inr: 8.0,
      service_fee_inr: 200.0,
      total_cost_inr: 320.0,
      estimated_duration_minutes: 300
    },
    pendingActionAfterAuth: null,
    activeContactSession: null
  };

  let mainMap = null;
  let userPin = null;
  let p2pLayer = null;
  let commercialLayer = null;

  let providerMap = null;
  let providerPin = null;

  function $(id) {
    return document.getElementById(id);
  }

  function escapeHtml(value) {
    return String(value ?? '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function showToast(message) {
    const el = $('toast-banner');
    if (!el) return;
    el.textContent = message;
    el.classList.remove('hidden');
    window.clearTimeout(el._timer);
    el._timer = window.setTimeout(() => el.classList.add('hidden'), 6500);
  }

  function setError(id, msg) {
    const el = $(id);
    if (!el) return;
    if (!msg) {
      el.textContent = '';
      el.classList.add('hidden');
    } else {
      el.textContent = msg;
      el.classList.remove('hidden');
    }
  }

  async function apiRequest(path, options = {}) {
    const res = await fetch(path, {
      method: options.method || 'GET',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: options.body ? JSON.stringify(options.body) : undefined
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      throw new Error(data.detail || data.error || `Request failed (${res.status})`);
    }
    return data;
  }

  // ==========================================================================
  // 1. Dark / Light Theme Toggle
  // ==========================================================================
  function initTheme() {
    let saved = 'dark';
    try {
      saved = window.localStorage.getItem('chargegrid_theme') || 'dark';
    } catch (_e) {}
    applyTheme(saved === 'light' ? 'light' : 'dark');
  }

  function applyTheme(theme) {
    state.theme = theme;
    document.documentElement.setAttribute('data-theme', theme);
    const isDark = theme === 'dark';
    $('theme-icon').textContent = isDark ? '☀️' : '🌙';
    $('theme-text').textContent = isDark ? 'Light Mode' : 'Dark Mode';
    try {
      window.localStorage.setItem('chargegrid_theme', theme);
    } catch (_e) {}
  }

  // ==========================================================================
  // 2. Smart Calculator: Total Cost = (Units Needed * Local Area Unit Cost) + 200
  // ==========================================================================
  function runSmartCalculator() {
    const carModel = $('calc-car-model').value.trim();
    const cap = Number($('calc-battery-cap').value);
    const cur = Number($('calc-current-pct').value);
    const des = Number($('calc-desired-pct').value);
    const rate = Number($('calc-unit-cost').value);
    const kw = Number($('calc-socket-speed').value || 3.0);

    if (!carModel || carModel.length < 2) {
      setError('calc-validation-error', 'Please enter your Car Model.');
      return null;
    }
    if (!Number.isFinite(cap) || cap <= 0 || cap > 250) {
      setError('calc-validation-error', 'Battery Capacity must be between 1 and 250 kWh.');
      return null;
    }
    if (!Number.isFinite(cur) || cur < 0 || cur >= 100) {
      setError('calc-validation-error', 'Current Battery % must be between 0% and 99%.');
      return null;
    }
    if (!Number.isFinite(des) || des <= 0 || des > 100) {
      setError('calc-validation-error', 'Desired Charge % must be between 1% and 100%.');
      return null;
    }
    if (des <= cur) {
      setError(
        'calc-validation-error',
        'Desired Charge % must be strictly higher than Current Battery %.'
      );
      return null;
    }
    if (!Number.isFinite(rate) || rate <= 0 || rate > 100) {
      setError('calc-validation-error', 'Local Area Unit Cost must be between ₹1 and ₹100.');
      return null;
    }

    setError('calc-validation-error', null);

    const unitsNeeded = Math.round(cap * (des - cur)) / 100;
    const energyCharge = Math.round(unitsNeeded * rate * 100) / 100;
    const totalCost = Math.round((energyCharge + 200) * 100) / 100;
    const estMinutes = Math.max(5, Math.round((unitsNeeded / kw) * 60));

    state.calculation = {
      car_model: carModel,
      battery_capacity_kwh: cap,
      current_battery_pct: cur,
      desired_battery_pct: des,
      units_needed_kwh: unitsNeeded,
      unit_cost_inr: rate,
      service_fee_inr: 200,
      total_cost_inr: totalCost,
      estimated_duration_minutes: estMinutes
    };

    const kmPerKwh = state.selectedKmPerKwh || 6.8;
    const estRangeKm = Math.round(unitsNeeded * kmPerKwh);
    state.calculation.estimated_range_km = estRangeKm;

    $('out-units-needed').textContent = `${unitsNeeded.toFixed(2)} kWh`;
    if ($('out-range-added')) {
      $('out-range-added').textContent = `+ ~${estRangeKm} km`;
    }
    $('out-unit-cost').textContent = `₹${rate.toFixed(2)} / Unit`;
    $('out-total-cost').textContent = `₹${totalCost.toFixed(2)}`;
    $('out-formula-text').textContent = `Total Cost = (${unitsNeeded.toFixed(
      2
    )} Units × ₹${rate.toFixed(2)}) + ₹200 = ₹${totalCost.toFixed(2)}`;
    $('out-est-time').textContent = `⏱️ ~${estMinutes} mins`;

    if ($('battery-bar-current')) {
      $('gauge-current-label').textContent = `Current: ${cur}%`;
      $('gauge-desired-label').textContent = `Target: ${des}%`;
      $('gauge-gain-label').textContent = `+${(des - cur).toFixed(0)}% (+${unitsNeeded.toFixed(1)} kWh · ~${estRangeKm} km)`;
      $('battery-bar-current').style.width = `${Math.min(100, Math.max(0, cur))}%`;
      $('battery-bar-added').style.left = `${Math.min(100, Math.max(0, cur))}%`;
      $('battery-bar-added').style.width = `${Math.min(100 - cur, Math.max(0, des - cur))}%`;
    }

    return state.calculation;
  }

  // ==========================================================================
  // 3. Open Map Initialization (Leaflet + OpenStreetMap)
  // ==========================================================================
  function initMaps() {
    if (typeof window.L === 'undefined') return;

    mainMap = window.L.map('ev-map').setView([state.centerLat, state.centerLng], 12);
    window.L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
      maxZoom: 19,
      attribution: '&copy; OpenStreetMap contributors'
    }).addTo(mainMap);

    p2pLayer = window.L.layerGroup().addTo(mainMap);
    commercialLayer = window.L.layerGroup().addTo(mainMap);

    state.radarCircle = window.L.circle([state.centerLat, state.centerLng], {
      radius: 4500,
      color: '#10b981',
      weight: 1.5,
      fillColor: '#10b981',
      fillOpacity: 0.07
    }).addTo(mainMap);

    userPin = window.L.circleMarker([state.centerLat, state.centerLng], {
      radius: 9,
      fillColor: '#38bdf8',
      color: '#ffffff',
      weight: 3,
      fillOpacity: 0.95
    })
      .addTo(mainMap)
      .bindPopup('<strong>Your EV Location (4.5 km Emergency Radar)</strong>');

    mainMap.on('click', (e) => {
      updateSearchCenter(e.latlng.lat, e.latlng.lng);
    });

    // Provider location picker map
    providerMap = window.L.map('provider-map').setView([state.centerLat, state.centerLng], 13);
    window.L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
      maxZoom: 19,
      attribution: '&copy; OpenStreetMap contributors'
    }).addTo(providerMap);

    providerPin = window.L.marker([state.centerLat, state.centerLng]).addTo(providerMap);
    providerMap.on('click', (e) => {
      const lat = Number(e.latlng.lat.toFixed(5));
      const lng = Number(e.latlng.lng.toFixed(5));
      $('prov-lat').value = lat;
      $('prov-lng').value = lng;
      providerPin.setLatLng([lat, lng]);
    });
  }

  function updateSearchCenter(lat, lng, cityLabel) {
    state.centerLat = Number(lat.toFixed(4));
    state.centerLng = Number(lng.toFixed(4));
    $('map-coord-readout').textContent = cityLabel
      ? `Center: ${state.centerLat}, ${state.centerLng} (${cityLabel})`
      : `Center: ${state.centerLat}, ${state.centerLng}`;
    if (userPin) userPin.setLatLng([state.centerLat, state.centerLng]);
    if (state.radarCircle) state.radarCircle.setLatLng([state.centerLat, state.centerLng]);
    if (mainMap) mainMap.panTo([state.centerLat, state.centerLng]);
    loadPublicProviders();
    loadCommercialStations();
  }

  // ==========================================================================
  // 4. Load Public P2P Hosts (No Phone Exposed) & Commercial EV Stations
  // ==========================================================================
  async function loadPublicProviders() {
    try {
      const data = await apiRequest(
        `/api/providers/public?lat=${encodeURIComponent(state.centerLat)}&lng=${encodeURIComponent(
          state.centerLng
        )}`
      );
      state.providers = data.providers || [];
      renderHostsAndStations();
    } catch (err) {
      showToast(err.message);
    }
  }

const evStationCache = new Map();

  const INSTANT_HIGHWAY_STATIONS = [
    { id: 'comm-1', station_name: 'Tata Power EZ Charge — Sector 17 Plaza', operator: 'Tata Power', address: 'Sector 17-C, Chandigarh', lat: 30.7398, lng: 76.7845, connectors: 'CCS2 DC Fast · Type-2 AC', power_kw: 50, tariff_inr_per_kwh: 18.5, hours: '24/7 Open' },
    { id: 'comm-2', station_name: 'Statiq Fast Charging Hub — Phase 7 Mohali', operator: 'Statiq Network', address: 'Phase 7 Market, Mohali, Punjab', lat: 30.7135, lng: 76.7254, connectors: 'CCS2 Dual-Gun (60 kW DC)', power_kw: 60, tariff_inr_per_kwh: 19.0, hours: '24/7 Open' },
    { id: 'comm-3', station_name: 'Statiq Express Charger — Ferozepur Road', operator: 'Statiq Network', address: 'Ferozepur Road, Ludhiana, Punjab', lat: 30.8895, lng: 75.8312, connectors: 'CCS2 (60 kW DC) · Type-2 AC', power_kw: 60, tariff_inr_per_kwh: 19.5, hours: '24/7 Open' },
    { id: 'ev-jal-1', station_name: 'Jio-bp pulse EV Station — NH-44 Jalandhar', operator: 'Jio-bp pulse', address: 'GT Road Highway Complex, Jalandhar', lat: 31.2982, lng: 75.6415, connectors: 'CCS2 (120 kW Ultra-Fast DC)', power_kw: 120, tariff_inr_per_kwh: 21.0, hours: '24/7 Open' },
    { id: 'ev-asr-1', station_name: 'BPCL e-Drive Fast Charger — Ranjit Ave Amritsar', operator: 'BPCL e-Drive', address: 'Ranjit Avenue, Amritsar, Punjab', lat: 31.6512, lng: 74.8648, connectors: 'CCS2 (30 kW DC) · Type-2 AC', power_kw: 30, tariff_inr_per_kwh: 17.5, hours: '24/7 Open' },
    { id: 'ev-pat-1', station_name: 'Universal EV Fast Point — Leela Bhawan Patiala', operator: 'Punjab EV Grid', address: 'Leela Bhawan Market, Patiala', lat: 30.3398, lng: 76.3869, connectors: 'CCS2 (30 kW DC) · Type-2 AC', power_kw: 30, tariff_inr_per_kwh: 16.5, hours: '06:00 AM – 11:00 PM' },
    { id: 'ev-amb-1', station_name: 'Tata Power Highway Charger — Ambala NH-44', operator: 'Tata Power', address: 'NH-44 GT Road, Ambala', lat: 30.3782, lng: 76.7767, connectors: 'CCS2 (60 kW DC)', power_kw: 60, tariff_inr_per_kwh: 18.5, hours: '24/7 Open' }
  ];

  async function loadCommercialStations() {
    const lat = state.centerLat;
    const lng = state.centerLng;
    const cacheKey = `${lat.toFixed(2)},${lng.toFixed(2)}`;

    const toRad = (d) => (d * Math.PI) / 180;
    const calcDist = (lat2, lng2) => {
      const a = Math.sin(toRad(lat2 - lat) / 2) ** 2 + Math.cos(toRad(lat)) * Math.cos(toRad(lat2)) * Math.sin(toRad(lng2 - lng) / 2) ** 2;
      return Math.round(6371 * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a)) * 100) / 100;
    };

    if (evStationCache.has(cacheKey)) {
      state.commercialStations = evStationCache.get(cacheKey);
      $('osm-station-status').textContent = `Showing ${state.commercialStations.length} EV station(s) within 150 km`;
      renderCommercialStationsAndMarkers();
      return;
    }

    const instantList = INSTANT_HIGHWAY_STATIONS
      .map((s) => ({ ...s, distance_km: calcDist(s.lat, s.lng) }))
      .filter((s) => s.distance_km <= 150)
      .sort((a, b) => a.distance_km - b.distance_km);

    state.commercialStations = instantList;
    renderCommercialStationsAndMarkers();
    $('osm-station-status').textContent = `Showing ${instantList.length} station(s) · Scanning live 150 km radar...`;

    const sBox = (lat - 1.35).toFixed(3);
    const wBox = (lng - 1.55).toFixed(3);
    const nBox = (lat + 1.35).toFixed(3);
    const eBox = (lng + 1.55).toFixed(3);
    const fastBboxQuery = `[out:json][timeout:6];node["amenity"="charging_station"](${sBox},${wBox},${nBox},${eBox});out 40;`;

    try {
      const res = await fetch(`https://overpass-api.de/api/interpreter?data=${encodeURIComponent(fastBboxQuery)}`);
      if (!res.ok) throw new Error('Overpass busy');
      const osmData = await res.json();
      const elements = Array.isArray(osmData.elements) ? osmData.elements : [];

      const liveStations = elements
        .map((el) => {
          if (!Number.isFinite(el.lat) || !Number.isFinite(el.lon)) return null;
          const dist = calcDist(el.lat, el.lon);
          if (dist > 150) return null;
          const t = el.tags || {};
          return {
            id: `osm-${el.id}`,
            station_name: t.name || t.operator || t.brand || 'Public EV Charging Station',
            operator: t.operator || t.brand || 'OpenStreetMap Live Grid',
            address: [t['addr:street'], t['addr:city']].filter(Boolean).join(', ') || `GPS: ${el.lat.toFixed(4)}, ${el.lon.toFixed(4)}`,
            lat: el.lat,
            lng: el.lon,
            connectors: t['socket:ccs'] ? 'CCS2 DC Fast' : t['socket:type2'] ? 'Type-2 AC' : 'CCS2 / Type-2 Charger',
            power_kw: 50,
            tariff_inr_per_kwh: 18.0,
            hours: t.opening_hours || '24/7 Open',
            distance_km: dist
          };
        })
        .filter(Boolean);

      const merged = [...liveStations, ...instantList].sort((a, b) => a.distance_km - b.distance_km);
      state.commercialStations = merged;
      evStationCache.set(cacheKey, merged);
      $('osm-station-status').textContent = `Showing ${merged.length} EV station(s) within 150 km`;
      renderCommercialStationsAndMarkers();
    } catch (_e) {
      evStationCache.set(cacheKey, instantList);
      $('osm-station-status').textContent = `Showing ${instantList.length} EV station(s) within 150 km`;
    }
  }
        return;
      }
    } catch (_e) {}

    // Fallback if no OSM stations are mapped in a rural village
    try {
      const data = await apiRequest(`/api/ev-stations/nearby?lat=${lat}&lng=${lng}`);
      state.commercialStations = data.stations || [];
    } catch (_e) {
      state.commercialStations = [];
    }
    renderCommercialStationsAndMarkers();
  }

  function renderCommercialStationsAndMarkers() {
    const listEl = $('ev-stations-list-container');
    const connectorFilter = $('select-station-connector') ? $('select-station-connector').value : 'all';
    const filtered = state.commercialStations.filter((s) => {
      if (connectorFilter === 'all') return true;
      return String(s.connectors || '').toLowerCase().includes(connectorFilter.toLowerCase());
    });

    if ($('station-count-badge')) {
      $('station-count-badge').textContent = String(filtered.length);
    }

    const calc = runSmartCalculator() || state.calculation;

    if (listEl) {
      if (filtered.length === 0) {
        listEl.innerHTML = '<p class="helper-text">No EV stations match the selected connector filter.</p>';
      } else {
        listEl.innerHTML = filtered
          .map((s) => {
            const estFastMins = Math.max(8, Math.round((calc.units_needed_kwh / (s.power_kw || 50)) * 60));
            const estStationCost = Math.round(calc.units_needed_kwh * (s.tariff_inr_per_kwh || 18) * 100) / 100;
            const navUrl = `https://www.openstreetmap.org/directions?engine=fossgis_osrm_car&route=${state.centerLat}%2C${state.centerLng}%3B${s.lat}%2C${s.lng}`;
            return `
              <article class="host-card" role="listitem">
                <div class="host-card-top">
                  <div>
                    <h3 class="host-name">${escapeHtml(s.station_name)}</h3>
                    <div class="host-meta">
                      <span class="badge badge-amber">⚡ ${escapeHtml(s.operator)}</span>
                      <span>📍 <strong>${Number(s.distance_km).toFixed(1)} km</strong> away</span>
                      <span>⚡ ${Number(s.power_kw).toFixed(0)} kW Fast Charge (~${estFastMins} mins)</span>
                    </div>
                  </div>
                  <span class="mono" style="font-weight:700;">₹${Number(s.tariff_inr_per_kwh).toFixed(2)}/kWh</span>
                </div>
                <div class="host-meta">
                  <span>🔌 ${escapeHtml(s.connectors)}</span>
                  <span>🕒 ${escapeHtml(s.hours)}</span>
                </div>
                <div class="host-meta">
                  <span>${escapeHtml(s.address)}</span>
                </div>
                <div class="host-card-footer">
                  <span class="mono" style="font-size:0.82rem;">
                    Est. Energy Cost: <strong>₹${estStationCost.toFixed(2)}</strong>
                  </span>
                  <div class="btn-group">
                    <button
                      type="button"
                      class="btn btn-secondary btn-xs btn-focus-station"
                      data-station-id="${escapeHtml(s.id)}"
                    >
                      🎯 Locate on Map
                    </button>
                    <a
                      href="${navUrl}"
                      target="_blank"
                      rel="noopener noreferrer"
                      class="btn btn-primary btn-xs"
                    >
                      🧭 Navigate
                    </a>
                  </div>
                </div>
              </article>
            `;
          })
          .join('');
      }
    }

    if (!mainMap || !commercialLayer || typeof window.L === 'undefined') return;
    commercialLayer.clearLayers();
    filtered.forEach((s) => {
      const m = window.L.circleMarker([s.lat, s.lng], {
        radius: 9,
        fillColor: '#f59e0b',
        color: '#ffffff',
        weight: 2,
        fillOpacity: 0.94
      });
      m.bindPopup(
        `<strong>${escapeHtml(s.station_name)}</strong><br/>
         <span class="badge badge-amber">⚡ Public EV Station (${escapeHtml(s.operator)})</span><br/>
         Connectors: ${escapeHtml(s.connectors)}<br/>
         Output: <strong>${Number(s.power_kw).toFixed(0)} kW</strong> · Rate: ₹${Number(s.tariff_inr_per_kwh).toFixed(2)}/kWh<br/>
         Distance: ${Number(s.distance_km).toFixed(1)} km`
      );
      s._marker = m;
      commercialLayer.addLayer(m);
    });
  }

  function renderNearestRescueBanner() {
    const strip = $('nearest-rescue-strip');
    if (!strip) return;
    const closestHost = state.providers.find((p) => p.is_available) || state.providers[0];
    const closestStation = state.commercialStations[0];
    if (!closestHost && !closestStation) {
      strip.innerHTML = '<span>Scanning nearby emergency charging options...</span>';
      return;
    }
    const calc = state.calculation;
    const hostEst = closestHost
      ? Math.round((calc.units_needed_kwh * Number(closestHost.unit_cost_inr) + 200) * 100) / 100
      : 0;
    strip.innerHTML = `
      <div>
        <strong>⚡ Best Roadside Match:</strong>
        ${
          closestHost
            ? `P2P Host <strong>${escapeHtml(closestHost.host_name)}</strong> (${Number(
                closestHost.distance_km
              ).toFixed(1)} km · Est. <strong>₹${hostEst.toFixed(0)}</strong>)`
            : ''
        }
        ${
          closestStation
            ? ` · Fast Charger: <strong>${escapeHtml(
                closestStation.station_name.split('—')[0]
              )}</strong> (${Number(closestStation.distance_km).toFixed(1)} km)`
            : ''
        }
      </div>
      ${
        closestHost
          ? `<button type="button" id="btn-instant-rescue" class="btn btn-primary btn-xs" data-provider-id="${closestHost.id}">
               🚀 Instant Rescue Contact
             </button>`
          : ''
      }
    `;
  }

  function renderHostsAndStations() {
    const listEl = $('hosts-list-container');
    $('host-count-badge').textContent = String(state.providers.length);
    renderCommercialStationsAndMarkers();
    renderNearestRescueBanner();

    if (p2pLayer) p2pLayer.clearLayers();

    const calc = runSmartCalculator() || state.calculation;

    listEl.innerHTML = state.providers
      .map((p) => {
        const hostTotal =
          Math.round((calc.units_needed_kwh * Number(p.unit_cost_inr) + 200) * 100) / 100;
        const propBadge =
          p.property_type === 'House'
            ? '🏠 House'
            : p.property_type === 'Shop'
            ? '🏪 Shop'
            : '🅿️ Parking Area';

        return `
          <article class="host-card" role="listitem">
            <div class="host-card-top">
              <div>
                <h3 class="host-name">${escapeHtml(p.host_name)}</h3>
                <div class="host-meta">
                  <span class="badge badge-emerald">${propBadge}</span>
                  <span>📍 ${Number(p.distance_km).toFixed(1)} km</span>
                  <span>🔌 ${escapeHtml(p.socket_type)}</span>
                </div>
              </div>
              <span class="mono" style="font-weight:700;">₹${Number(p.unit_cost_inr).toFixed(2)}/Unit</span>
            </div>
            <div class="host-meta">
              <span>${escapeHtml(p.address_label)}</span>
              <span>🔒 Phone hidden until contact</span>
            </div>
            <div class="host-card-footer">
              <span class="mono" style="font-size:0.82rem;">
                Est. Total: <strong>₹${hostTotal.toFixed(2)}</strong>
              </span>
              <button
                type="button"
                class="btn btn-primary btn-xs btn-contact-for-charge"
                data-provider-id="${p.id}"
              >
                📞 Contact for Charge
              </button>
            </div>
          </article>
        `;
      })
      .join('');

    if (mainMap && p2pLayer && typeof window.L !== 'undefined') {
      state.providers.forEach((p) => {
        const m = window.L.circleMarker([p.lat, p.lng], {
          radius: 9,
          fillColor: '#10b981',
          color: '#ffffff',
          weight: 2,
          fillOpacity: 0.92
        });
        m.bindPopup(`
          <strong>${escapeHtml(p.host_name)}</strong><br/>
          Type: ${escapeHtml(p.property_type)}<br/>
          Rate: ₹${Number(p.unit_cost_inr).toFixed(2)}/kWh + ₹200 fee<br/>
          <em>Phone hidden — click "Contact for Charge" in the sidebar</em>
        `);
        p2pLayer.addLayer(m);
      });
    }
  }

  // ==========================================================================
  // 5. Triggered Authentication Engine (Gmail + OTP)
  // ==========================================================================
  function requireTriggeredAuth(reasonLabel, callbackAfterAuth) {
    if (state.authenticated && state.user) {
      callbackAfterAuth();
      return;
    }
    state.pendingActionAfterAuth = callbackAfterAuth;
    $('auth-trigger-reason').textContent = `Login Triggered by: "${reasonLabel}"`;
    setError('auth-modal-error', null);
    $('modal-auth').classList.remove('hidden');
    $('auth-email-input').focus();
  }

  async function checkSessionOnLoad() {
    try {
      const data = await apiRequest('/api/auth/session');
      state.authenticated = Boolean(data.authenticated);
      state.user = data.user || null;
      updateAccountIndicator();
      if (state.user && state.user.provider_listing) {
        fillProviderForm(state.user.provider_listing);
      }
    } catch (_e) {}
  }

  function updateAccountIndicator() {
    const dot = $('auth-indicator-dot');
    const label = $('account-btn-label');
    const status = $('dropdown-user-status');

    if (state.authenticated && state.user) {
      dot.className = 'dot-online';
      label.textContent = state.user.email.split('@')[0];
      status.textContent = state.user.email;
    } else {
      dot.className = 'dot-offline';
      label.textContent = 'Account';
      status.textContent = 'Guest (Browsing Open Map)';
    }
  }

  function fillProviderForm(listing) {
    if (!listing) return;
    $('prov-host-name').value = listing.host_name || '';
    $('prov-property-type').value = listing.property_type || 'House';
    $('prov-phone').value = listing.phone_number || '';
    $('prov-address').value = listing.address_label || '';
    $('prov-lat').value = listing.lat;
    $('prov-lng').value = listing.lng;
    $('prov-unit-cost').value = listing.unit_cost_inr;
    $('prov-socket').value = listing.socket_type || '15A Domestic (3.3 kW)';
    $('prov-available').checked = Boolean(listing.is_available);
    $('provider-saved-badge').textContent = 'Saved & Active';
    $('provider-saved-badge').className = 'badge badge-emerald';
  }

  // ==========================================================================
  // 6. Execute Triggered Flows: "Contact for Charge", "Save Address", "History"
  // ==========================================================================
  async function executeContactForCharge(providerId) {
    const calc = runSmartCalculator();
    if (!calc) {
      showToast('Please enter valid Car Model and Battery Percentages first.');
      return;
    }
    try {
      const res = await apiRequest('/api/charging/contact', {
        method: 'POST',
        body: {
          provider_id: providerId,
          car_model: calc.car_model,
          battery_capacity_kwh: calc.battery_capacity_kwh,
          current_battery_pct: calc.current_battery_pct,
          desired_battery_pct: calc.desired_battery_pct
        }
      });

      state.activeContactSession = res;
      renderContactModal(res);
      $('boost-feedback-msg').classList.add('hidden');
      $('modal-contact-charge').classList.remove('hidden');
    } catch (err) {
      showToast(err.message);
    }
  }

  function renderContactModal(sessionData) {
    const p = sessionData.provider;
    const c = sessionData.calculation;
    const offered = Number(sessionData.offered_cost_inr);
    const waText = encodeURIComponent(
      `Hi ${p.host_name}, I found your ${p.property_type} charger on ChargeGrid P2P. I need ~${c.units_needed_kwh.toFixed(
        1
      )} kWh for my ${state.calculation.car_model} and am offering ₹${offered.toFixed(
        0
      )}. Are you available for an emergency charge?`
    );

    $('contact-host-details').innerHTML = `
      <div><strong>Provider:</strong> ${escapeHtml(p.host_name)} <span class="badge badge-emerald">${escapeHtml(
        p.property_type
      )}</span></div>
      <div><strong>Location:</strong> ${escapeHtml(p.address_label)}</div>
      <div style="margin-top:8px;padding:10px;border-radius:8px;background:var(--emerald-tint);border:1px solid var(--emerald);">
        <strong>📞 Unlocked 10-Digit Mobile Number:</strong>
        <a href="tel:${escapeHtml(p.phone_number)}" class="mono" style="font-size:1.1rem;font-weight:700;margin-left:6px;">
          ${escapeHtml(p.phone_number)}
        </a>
        <div class="contact-action-grid">
          <a href="tel:${escapeHtml(p.phone_number)}" class="btn btn-primary btn-xs">📞 Call Host Now</a>
          <a href="https://wa.me/91${escapeHtml(p.phone_number)}?text=${waText}" target="_blank" rel="noopener noreferrer" class="btn btn-secondary btn-xs">💬 WhatsApp Host</a>
          <button type="button" id="btn-copy-host-phone" class="btn btn-outline btn-xs" data-phone="${escapeHtml(p.phone_number)}">📋 Copy Number</button>
        </div>
      </div>
      <div style="margin-top:8px;">
        <strong>Smart Calculator Breakdown:</strong>
        (${c.units_needed_kwh.toFixed(2)} Units × ₹${c.unit_cost_inr.toFixed(2)}) + ₹200 =
        <strong>₹${c.total_cost_inr.toFixed(2)}</strong>
      </div>
      <div>
        <strong>Current Active Offer to Host:</strong>
        <span class="mono total-price">₹${offered.toFixed(2)}</span>
      </div>
    `;

    $('input-boost-amount').value = Math.round(offered + 50);
  }

  async function executeSaveProviderAddress() {
    setError('prov-error-alert', null);
    const phoneRaw = $('prov-phone').value.trim();
    if (!/^[6-9]\d{9}$/.test(phoneRaw)) {
      setError(
        'prov-error-alert',
        'Please enter a strictly validated 10-digit Indian mobile number (starting with 6–9, no spaces or +91).'
      );
      return;
    }

    try {
      const res = await apiRequest('/api/providers/save', {
        method: 'POST',
        body: {
          host_name: $('prov-host-name').value.trim(),
          property_type: $('prov-property-type').value,
          phone_number: phoneRaw,
          address_label: $('prov-address').value.trim(),
          lat: Number($('prov-lat').value),
          lng: Number($('prov-lng').value),
          unit_cost_inr: Number($('prov-unit-cost').value),
          socket_type: $('prov-socket').value,
          is_available: $('prov-available').checked
        }
      });

      fillProviderForm(res.listing);
      await loadPublicProviders();
      showToast('Provider address saved! Your pin is live on the map (phone number remains private).');
    } catch (err) {
      setError('prov-error-alert', err.message);
    }
  }

  async function executeOpenHistory() {
    try {
      const res = await apiRequest('/api/history');
      const custList = $('history-customer-list');
      const provList = $('history-provider-list');

      if (!res.as_customer || res.as_customer.length === 0) {
        custList.innerHTML = '<p class="helper-text">No customer charging sessions initiated yet.</p>';
      } else {
        custList.innerHTML = res.as_customer
          .map(
            (item) => `
            <div class="host-card">
              <strong>${escapeHtml(item.host_name)} (${escapeHtml(item.property_type)})</strong>
              <div class="host-meta">
                <span>🚗 ${escapeHtml(item.car_model)} (${item.units_needed_kwh} kWh)</span>
                <span>📞 ${escapeHtml(item.phone_number)}</span>
              </div>
              <div class="mono" style="font-size:0.8rem;">
                Calculated: ₹${Number(item.calculated_cost_inr).toFixed(2)} ·
                <strong>Offered: ₹${Number(item.offered_cost_inr).toFixed(2)}</strong>
                (${escapeHtml(item.status)})
              </div>
            </div>
          `
          )
          .join('');
      }

      if (!res.as_provider || res.as_provider.length === 0) {
        provList.innerHTML = '<p class="helper-text">No incoming customer requests for your host pin yet.</p>';
      } else {
        provList.innerHTML = res.as_provider
          .map(
            (item) => `
            <div class="host-card">
              <strong>Customer: ${escapeHtml(item.customer_email)}</strong>
              <div class="host-meta">
                <span>🚗 ${escapeHtml(item.car_model)} (${item.units_needed_kwh} kWh)</span>
                <span>Status: ${escapeHtml(item.status)}</span>
              </div>
              <div class="mono" style="font-size:0.8rem;">
                Offered Amount: <strong>₹${Number(item.offered_cost_inr).toFixed(2)}</strong>
              </div>
            </div>
          `
          )
          .join('');
      }

      $('modal-history').classList.remove('hidden');
    } catch (err) {
      showToast(err.message);
    }
  }

  // ==========================================================================
  // 7. Wire DOM Event Listeners
  // ==========================================================================
  function bindEvents() {
    $('btn-theme-toggle').addEventListener('click', () => {
      applyTheme(state.theme === 'dark' ? 'light' : 'dark');
    });

    function switchDirectoryTab(mode) {
      const isStations = mode === 'stations';
      $('panel-dir-p2p').classList.toggle('hidden', isStations);
      $('panel-dir-stations').classList.toggle('hidden', !isStations);
      $('btn-dir-p2p').className = isStations ? 'btn btn-outline btn-xs' : 'btn btn-primary btn-xs';
      $('btn-dir-p2p').setAttribute('aria-selected', String(!isStations));
      $('btn-dir-stations').className = isStations ? 'btn btn-primary btn-xs' : 'btn btn-outline btn-xs';
      $('btn-dir-stations').setAttribute('aria-selected', String(isStations));
    }

    function applyMapLayerFilter(filterType) {
      ['filter-map-all', 'filter-map-p2p', 'filter-map-stations'].forEach((id) => {
        $(id).classList.toggle('active', id === `filter-map-${filterType}`);
      });
      if (!mainMap) return;
      if (filterType === 'all' || filterType === 'p2p') {
        if (p2pLayer && !mainMap.hasLayer(p2pLayer)) mainMap.addLayer(p2pLayer);
      } else if (p2pLayer && mainMap.hasLayer(p2pLayer)) {
        mainMap.removeLayer(p2pLayer);
      }
      if (filterType === 'all' || filterType === 'stations') {
        if (commercialLayer && !mainMap.hasLayer(commercialLayer)) mainMap.addLayer(commercialLayer);
      } else if (commercialLayer && mainMap.hasLayer(commercialLayer)) {
        mainMap.removeLayer(commercialLayer);
      }
    }

    // Role Tabs (Customer <-> Nearby EV Stations <-> Provider)
    $('tab-customer-mode').addEventListener('click', () => {
      $('tab-customer-mode').classList.add('active');
      $('tab-customer-mode').setAttribute('aria-selected', 'true');
      $('tab-ev-stations-mode').classList.remove('active');
      $('tab-ev-stations-mode').setAttribute('aria-selected', 'false');
      $('tab-provider-mode').classList.remove('active');
      $('tab-provider-mode').setAttribute('aria-selected', 'false');
      $('section-customer-view').classList.remove('hidden');
      $('section-provider-view').classList.add('hidden');
      switchDirectoryTab('p2p');
      applyMapLayerFilter('all');
      window.setTimeout(() => mainMap && mainMap.invalidateSize(), 60);
    });

    $('tab-ev-stations-mode').addEventListener('click', () => {
      $('tab-ev-stations-mode').classList.add('active');
      $('tab-ev-stations-mode').setAttribute('aria-selected', 'true');
      $('tab-customer-mode').classList.remove('active');
      $('tab-customer-mode').setAttribute('aria-selected', 'false');
      $('tab-provider-mode').classList.remove('active');
      $('tab-provider-mode').setAttribute('aria-selected', 'false');
      $('section-customer-view').classList.remove('hidden');
      $('section-provider-view').classList.add('hidden');
      switchDirectoryTab('stations');
      applyMapLayerFilter('stations');
      $('directory-card').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
      window.setTimeout(() => mainMap && mainMap.invalidateSize(), 60);
      showToast('Showing Nearby Public & Commercial EV Charging Stations.');
    });

    $('tab-provider-mode').addEventListener('click', () => {
      $('tab-provider-mode').classList.add('active');
      $('tab-provider-mode').setAttribute('aria-selected', 'true');
      $('tab-customer-mode').classList.remove('active');
      $('tab-customer-mode').setAttribute('aria-selected', 'false');
      $('tab-ev-stations-mode').classList.remove('active');
      $('tab-ev-stations-mode').setAttribute('aria-selected', 'false');
      $('section-provider-view').classList.remove('hidden');
      $('section-customer-view').classList.add('hidden');
      window.setTimeout(() => providerMap && providerMap.invalidateSize(), 60);
    });

    $('btn-dir-p2p').addEventListener('click', () => switchDirectoryTab('p2p'));
    $('btn-dir-stations').addEventListener('click', () => switchDirectoryTab('stations'));

    $('filter-map-all').addEventListener('click', () => applyMapLayerFilter('all'));
    $('filter-map-p2p').addEventListener('click', () => {
      applyMapLayerFilter('p2p');
      switchDirectoryTab('p2p');
    });
    $('filter-map-stations').addEventListener('click', () => {
      applyMapLayerFilter('stations');
      switchDirectoryTab('stations');
    });

    $('select-station-connector').addEventListener('change', () => {
      renderCommercialStationsAndMarkers();
    });

    $('ev-stations-list-container').addEventListener('click', (e) => {
      const btn = e.target.closest('.btn-focus-station');
      if (!btn) return;
      const stationId = btn.getAttribute('data-station-id');
      const st = state.commercialStations.find((x) => x.id === stationId);
      if (st && mainMap) {
        applyMapLayerFilter('all');
        mainMap.setView([st.lat, st.lng], 14);
        if (st._marker) st._marker.openPopup();
        $('ev-map').scrollIntoView({ behavior: 'smooth', block: 'center' });
      }
    });

    // Smart Calculator live updates
    [
      'calc-car-model',
      'calc-battery-cap',
      'calc-current-pct',
      'calc-desired-pct',
      'calc-unit-cost',
      'calc-socket-speed'
    ].forEach((id) => {
      $(id).addEventListener('input', () => {
        runSmartCalculator();
        renderHostsAndStations();
      });
    });

    // GPS & Search on Customer Map
    $('btn-locate-me').addEventListener('click', () => {
      if (!navigator.geolocation) {
        showToast('Geolocation unavailable in this browser.');
        return;
      }
      navigator.geolocation.getCurrentPosition(
        (pos) => updateSearchCenter(pos.coords.latitude, pos.coords.longitude),
        () => showToast('GPS permission denied — click anywhere on the map or use the search bar.')
      );
    });

    $('btn-refresh-pins').addEventListener('click', () => {
      loadPublicProviders();
      loadCommercialStations();
    });

    $('form-map-search').addEventListener('submit', async (e) => {
      e.preventDefault();
      const q = $('input-map-search').value.trim();
      if (!q) return;
      try {
        const res = await fetch(
          `https://nominatim.openstreetmap.org/search?format=json&limit=1&q=${encodeURIComponent(q)}`
        );
        const items = await res.json();
        if (Array.isArray(items) && items.length > 0) {
          updateSearchCenter(Number(items[0].lat), Number(items[0].lon));
          showToast(`Centered on ${items[0].display_name.split(',')[0]}`);
        } else {
          showToast('Location not found. Click directly on the map to move your pin.');
        }
      } catch (_e) {
        showToast('Geocoding service busy. Click directly on the map instead.');
      }
    });

    // Provider GPS helper
    $('btn-provider-gps').addEventListener('click', () => {
      if (!navigator.geolocation) return;
      navigator.geolocation.getCurrentPosition((pos) => {
        const lat = Number(pos.coords.latitude.toFixed(5));
        const lng = Number(pos.coords.longitude.toFixed(5));
        $('prov-lat').value = lat;
        $('prov-lng').value = lng;
        if (providerMap && providerPin) {
          providerMap.setView([lat, lng], 14);
          providerPin.setLatLng([lat, lng]);
        }
      });
    });

    // Trigger 1: "Contact for Charge" button
    $('hosts-list-container').addEventListener('click', (e) => {
      const btn = e.target.closest('.btn-contact-for-charge');
      if (!btn) return;
      const providerId = Number(btn.getAttribute('data-provider-id'));
      requireTriggeredAuth('Contact for Charge', () => executeContactForCharge(providerId));
    });

    // Trigger 2: "Save Address" button in Provider Flow
    $('form-provider-save').addEventListener('submit', (e) => {
      e.preventDefault();
      const phoneClean = $('prov-phone').value.trim();
      if (!/^[6-9]\d{9}$/.test(phoneClean)) {
        setError(
          'prov-error-alert',
          'Mobile number must be strictly 10 digits (starting with 6–9, no +91).'
        );
        return;
      }
      setError('prov-error-alert', null);
      requireTriggeredAuth('Save Address', () => executeSaveProviderAddress());
    });

    // Top-Right Account Dropdown
    $('btn-account-dropdown').addEventListener('click', () => {
      const menu = $('account-dropdown-menu');
      const hidden = menu.classList.toggle('hidden');
      $('btn-account-dropdown').setAttribute('aria-expanded', String(!hidden));
    });

    document.addEventListener('click', (e) => {
      const wrap = $('account-menu-wrap');
      if (wrap && !wrap.contains(e.target)) {
        $('account-dropdown-menu').classList.add('hidden');
      }
    });

    // Direct "Login / Sign Up" option in Top-Right Account Menu
  if ($('menu-btn-login')) {
    $('menu-btn-login').addEventListener('click', () => {
      $('account-dropdown-menu').classList.add('hidden');
      if (state.authenticated && state.user) {
        showToast(`You are already logged in as ${state.user.email}.`);
        return;
      }
      requireTriggeredAuth('Account Login', () => {
        showToast('Successfully logged in!');
      });
    });
  }

  // Trigger 3: "History" in Top-Right Account Menu
  $('menu-btn-history').addEventListener('click', () => {
    $('account-dropdown-menu').classList.add('hidden');
    requireTriggeredAuth('History', () => executeOpenHistory());
  });

    // Account Menu: "Logout"
    $('menu-btn-logout').addEventListener('click', async () => {
      $('account-dropdown-menu').classList.add('hidden');
      if (!state.authenticated) {
        showToast('You are currently browsing as a guest.');
        return;
      }
      try {
        await apiRequest('/api/auth/logout', { method: 'POST' });
        state.authenticated = false;
        state.user = null;
        updateAccountIndicator();
        showToast('Logged out.');
      } catch (err) {
        showToast(err.message);
      }
    });

    // Account Menu: "Delete Account" (Shows strict warning modal)
    $('menu-btn-delete-account').addEventListener('click', () => {
      $('account-dropdown-menu').classList.add('hidden');
      if (!state.authenticated) {
        requireTriggeredAuth('Delete Account', () => {
          $('modal-delete-warning').classList.remove('hidden');
        });
        return;
      }
      $('modal-delete-warning').classList.remove('hidden');
    });

    $('btn-close-delete-modal').addEventListener('click', () =>
      $('modal-delete-warning').classList.add('hidden')
    );
    $('btn-cancel-delete-account').addEventListener('click', () =>
      $('modal-delete-warning').classList.add('hidden')
    );

    $('btn-ok-delete-account').addEventListener('click', async () => {
      try {
        await apiRequest('/api/auth/account', { method: 'DELETE' });
        $('modal-delete-warning').classList.add('hidden');
        state.authenticated = false;
        state.user = null;
        updateAccountIndicator();
        await loadPublicProviders();
        showToast('This account and all data have been permanently deleted.');
      } catch (err) {
        showToast(err.message);
      }
    });

    // Triggered Auth Modal: Send OTP & Verify OTP
    $('btn-close-auth-modal').addEventListener('click', () => $('modal-auth').classList.add('hidden'));
    $('btn-cancel-auth-modal').addEventListener('click', () => $('modal-auth').classList.add('hidden'));

    $('form-request-otp').addEventListener('submit', async (e) => {
      e.preventDefault();
      setError('auth-modal-error', null);
      const email = $('auth-email-input').value.trim();
      try {
        const res = await apiRequest('/api/auth/request-otp', {
          method: 'POST',
          body: { email }
        });
        const banner = $('dev-otp-preview-banner');
        if (res.dev_otp_preview) {
          banner.innerHTML = `<strong>[DEV OTP PREVIEW]:</strong> Your 6-digit verification code for <strong>${escapeHtml(
            email
          )}</strong> is <strong class="mono">${escapeHtml(res.dev_otp_preview)}</strong>.`;
          banner.classList.remove('hidden');
          $('auth-otp-input').value = res.dev_otp_preview;
        } else {
          banner.classList.add('hidden');
        }
        showToast(res.message);
      } catch (err) {
        setError('auth-modal-error', err.message);
      }
    });

    $('form-verify-otp').addEventListener('submit', async (e) => {
      e.preventDefault();
      setError('auth-modal-error', null);
      const email = $('auth-email-input').value.trim();
      const otp = $('auth-otp-input').value.trim();
      try {
        const res = await apiRequest('/api/auth/verify-otp', {
          method: 'POST',
          body: { email, otp }
        });
        state.authenticated = true;
        state.user = res.user;
        updateAccountIndicator();
        if (state.user && state.user.provider_listing) {
          fillProviderForm(state.user.provider_listing);
        }
        $('modal-auth').classList.add('hidden');
        $('dev-otp-preview-banner').classList.add('hidden');
        $('auth-otp-input').value = '';
        showToast(`Verified as ${email}!`);

        if (typeof state.pendingActionAfterAuth === 'function') {
          const action = state.pendingActionAfterAuth;
          state.pendingActionAfterAuth = null;
          action();
        }
      } catch (err) {
        setError('auth-modal-error', err.message);
      }
    });

    // Contact Modal & Price Boost Negotiation
    $('btn-close-contact-modal').addEventListener('click', () =>
      $('modal-contact-charge').classList.add('hidden')
    );
    $('btn-done-contact-modal').addEventListener('click', () =>
      $('modal-contact-charge').classList.add('hidden')
    );

    document.querySelectorAll('.btn-quick-boost').forEach((btn) => {
      btn.addEventListener('click', () => {
        if (!state.activeContactSession) return;
        const inc = Number(btn.getAttribute('data-boost') || 50);
        const base = Number(state.activeContactSession.offered_cost_inr);
        $('input-boost-amount').value = Math.round(base + inc);
      });
    });

    $('form-boost-offer').addEventListener('submit', async (e) => {
      e.preventDefault();
      if (!state.activeContactSession) return;
      const newOffer = Number($('input-boost-amount').value);
      try {
        const res = await apiRequest(
          `/api/charging/${state.activeContactSession.request_id}/boost`,
          {
            method: 'PATCH',
            body: { new_offered_cost_inr: newOffer }
          }
        );
        state.activeContactSession.offered_cost_inr = res.record.offered_cost_inr;
        renderContactModal(state.activeContactSession);
        const fb = $('boost-feedback-msg');
        fb.textContent = `Incentivized offer increased to ₹${Number(
          res.record.offered_cost_inr
        ).toFixed(2)}! Share this updated amount with ${res.record.host_name}.`;
        fb.classList.remove('hidden');
      } catch (err) {
        showToast(err.message);
      }
    });


    // Quick City Hub Chips (Chandigarh, Mohali, Ludhiana, Jalandhar, Amritsar, Patiala)
    document.querySelectorAll('.hub-chip').forEach((chip) => {
      chip.addEventListener('click', () => {
        document.querySelectorAll('.hub-chip').forEach((c) => c.classList.remove('active'));
        chip.classList.add('active');
        const lat = Number(chip.getAttribute('data-lat'));
        const lng = Number(chip.getAttribute('data-lng'));
        const city = chip.getAttribute('data-city');
        updateSearchCenter(lat, lng, `${city}, Punjab`);
        showToast(`Switched radar center to ${city}.`);
      });
    });

    // Popular EV Model Preset Chips
    document.querySelectorAll('.ev-preset-chip').forEach((chip) => {
      chip.addEventListener('click', () => {
        document.querySelectorAll('.ev-preset-chip').forEach((c) => c.classList.remove('active'));
        chip.classList.add('active');
        $('calc-car-model').value = chip.getAttribute('data-model');
        $('calc-battery-cap').value = chip.getAttribute('data-cap');
        state.selectedKmPerKwh = Number(chip.getAttribute('data-eff') || 6.8);
        runSmartCalculator();
        renderHostsAndStations();
      });
    });

    // Nearest Rescue Strip 1-Click Instant Contact
    if ($('nearest-rescue-strip')) {
      $('nearest-rescue-strip').addEventListener('click', (e) => {
        const btn = e.target.closest('#btn-instant-rescue');
        if (!btn) return;
        const pid = Number(btn.getAttribute('data-provider-id'));
        requireTriggeredAuth('Contact for Charge', () => executeContactForCharge(pid));
      });
    }

    // Copy Host Phone Number inside Contact Modal
    $('contact-host-details').addEventListener('click', (e) => {
      const copyBtn = e.target.closest('#btn-copy-host-phone');
      if (!copyBtn) return;
      const phone = copyBtn.getAttribute('data-phone');
      const tempInput = document.createElement('input');
      tempInput.value = phone;
      document.body.appendChild(tempInput);
      tempInput.select();
      try {
        document.execCommand('copy');
        copyBtn.textContent = '✅ Copied!';
      } catch (_err) {}
      document.body.removeChild(tempInput);
    });

        // History Modal Close
    $('btn-close-history-modal').addEventListener('click', () =>
      $('modal-history').classList.add('hidden')
    );
    $('btn-done-history-modal').addEventListener('click', () =>
      $('modal-history').classList.add('hidden')
    );
  }

  document.addEventListener('DOMContentLoaded', () => {
    initTheme();
    runSmartCalculator();
    initMaps();
    bindEvents();
    checkSessionOnLoad();
    loadPublicProviders();
    loadCommercialStations();
  });
})();
