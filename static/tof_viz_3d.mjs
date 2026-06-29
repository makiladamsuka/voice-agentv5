import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';

const view = document.getElementById('view3d');
const vizLoading = document.getElementById('viz-loading');
const hudLeft = document.getElementById('hud-left');
const hudRight = document.getElementById('hud-right');
const hudOrient = document.getElementById('hud-orient');
const readout = document.getElementById('object-readout');

const HUMAN_COL = 0x38bdf8;
const OBSTACLE_COL = 0xf97316;
const UNCERTAIN_COL = 0x6b7280;
const ROBOT_COL = 0xc8d6e5;
const KIND_COL = { human: HUMAN_COL, obstacle: OBSTACLE_COL, uncertain: UNCERTAIN_COL };
const ZONE_ANGLE = { L: -45, C: 0, R: 45 };
const deg = d => d * Math.PI / 180;
const TRAIL_MAX = 28;
const LERP = 0.12;
// Robot base 550 mm diameter, centered at origin; sensors on front face (+Z)
const ROBOT_RADIUS = 0.275;
const SENSOR_MOUNT_Z = ROBOT_RADIUS; // front edge

// ── Renderer ──
const scene = new THREE.Scene();
scene.background = new THREE.Color(0x0a0c10);

const camera = new THREE.OrthographicCamera(-1, 1, 1, -1, 0.1, 30);
camera.position.set(0, 3.6, 0.55);
camera.lookAt(0, 0, 0.55);

const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false });
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
view.appendChild(renderer.domElement);
if (vizLoading) vizLoading.style.display = 'none';

const controls = new OrbitControls(camera, renderer.domElement);
controls.target.set(0, 0, 0.55);
controls.enableDamping = true;
controls.dampingFactor = 0.08;
controls.maxPolarAngle = Math.PI * 0.2;
controls.minPolarAngle = Math.PI * 0.05;
controls.enableRotate = true;
controls.rotateSpeed = 0.5;

// World map spins under the robot (heading-up: robot nose = screen forward)
const mapGroup = new THREE.Group();
scene.add(mapGroup);

// Plain floor (rotates with world / front reference)
const floor = new THREE.Mesh(
  new THREE.PlaneGeometry(4.5, 4.5),
  new THREE.MeshBasicMaterial({ color: 0x0e1117 })
);
floor.rotation.x = -Math.PI / 2;
floor.position.y = 0;
mapGroup.add(floor);

// Startup forward marker (+Z world) — grey cone; rotates opposite body yaw
const frontMarker = new THREE.Mesh(
  new THREE.ConeGeometry(0.07, 0.12, 3),
  new THREE.MeshBasicMaterial({ color: 0x94a3b8, transparent: true, opacity: 0.55 })
);
frontMarker.rotation.x = -Math.PI / 2;
frontMarker.position.set(0, 0.018, 1.05);
mapGroup.add(frontMarker);
const frontRing = new THREE.Mesh(
  new THREE.RingGeometry(0.09, 0.11, 24),
  new THREE.MeshBasicMaterial({ color: 0x64748b, transparent: true, opacity: 0.35, side: THREE.DoubleSide })
);
frontRing.rotation.x = -Math.PI / 2;
frontRing.position.set(0, 0.017, 1.05);
mapGroup.add(frontRing);

// ── Robot body (550 mm circle, always points screen-forward) ──
const robot = new THREE.Group();
scene.add(robot);

const body = new THREE.Mesh(
  new THREE.CircleGeometry(ROBOT_RADIUS, 64),
  new THREE.MeshBasicMaterial({ color: ROBOT_COL })
);
body.rotation.x = -Math.PI / 2;
body.position.y = 0.012;
robot.add(body);

// Center accent line (forward axis)
const accentLine = new THREE.Mesh(
  new THREE.PlaneGeometry(0.04, ROBOT_RADIUS * 1.7),
  new THREE.MeshBasicMaterial({ color: HUMAN_COL, transparent: true, opacity: 0.5, side: THREE.DoubleSide })
);
accentLine.rotation.x = -Math.PI / 2;
accentLine.position.set(0, 0.014, 0);
robot.add(accentLine);

// Nose indicator at front edge
const nose = new THREE.Mesh(
  new THREE.ConeGeometry(0.055, 0.09, 3),
  new THREE.MeshBasicMaterial({ color: HUMAN_COL, transparent: true, opacity: 0.85 })
);
nose.rotation.x = -Math.PI / 2;
nose.position.set(0, 0.015, SENSOR_MOUNT_Z + 0.04);
robot.add(nose);

// Sensor origin marker (where ToF beams start)
const sensorOrigin = new THREE.Mesh(
  new THREE.RingGeometry(0.04, 0.055, 20),
  new THREE.MeshBasicMaterial({ color: HUMAN_COL, transparent: true, opacity: 0.35, side: THREE.DoubleSide })
);
sensorOrigin.rotation.x = -Math.PI / 2;
sensorOrigin.position.set(0, 0.016, SENSOR_MOUNT_Z);
robot.add(sensorOrigin);

// Robot outline ring
const robotGlow = new THREE.Mesh(
  new THREE.RingGeometry(ROBOT_RADIUS - 0.01, ROBOT_RADIUS + 0.01, 64),
  new THREE.MeshBasicMaterial({ color: 0x334155, transparent: true, opacity: 0.45, side: THREE.DoubleSide })
);
robotGlow.rotation.x = -Math.PI / 2;
robotGlow.position.set(0, 0.004, 0);
robot.add(robotGlow);

// ── FOV wedges (from front sensor plane) ──
const fovGroup = new THREE.Group();
fovGroup.position.set(0, 0, SENSOR_MOUNT_Z);
robot.add(fovGroup);

for (const [key, angle] of Object.entries(ZONE_ANGLE)) {
  const g = new THREE.Group();
  g.rotation.y = deg(angle);
  const wedge = new THREE.Mesh(
    new THREE.CircleGeometry(1.9, 48, -deg(11), deg(22)),
    new THREE.MeshBasicMaterial({ color: 0x334155, transparent: true, opacity: 0.04, side: THREE.DoubleSide })
  );
  wedge.rotation.x = -Math.PI / 2;
  wedge.position.set(0, 0.003, 0.02);
  g.add(wedge);
  for (const sign of [-1, 1]) {
    const edgeAngle = sign * 11;
    const pts = [
      new THREE.Vector3(0, 0.004, 0),
      new THREE.Vector3(Math.sin(deg(edgeAngle)) * 1.9, 0.004, Math.cos(deg(edgeAngle)) * 1.9),
    ];
    const line = new THREE.Line(
      new THREE.BufferGeometry().setFromPoints(pts),
      new THREE.LineBasicMaterial({ color: 0x334155, transparent: true, opacity: 0.12 })
    );
    g.add(line);
  }
  // Zone label at beam tip
  const tip = new THREE.Mesh(
    new THREE.SphereGeometry(0.018, 8, 8),
    new THREE.MeshBasicMaterial({ color: key === 'C' ? 0xa855f7 : (key === 'L' ? 0x3b82f6 : 0x22c55e), transparent: true, opacity: 0.5 })
  );
  tip.position.set(0, 0.02, 0.35);
  g.add(tip);
  fovGroup.add(g);
}

// ── Detected objects (body frame — locked to ToF beams) ──
const entityGroup = new THREE.Group();
robot.add(entityGroup);

// Bearing line from sensor plane toward fused hit
const bearingGeo = new THREE.BufferGeometry().setFromPoints([
  new THREE.Vector3(0, 0.02, SENSOR_MOUNT_Z),
  new THREE.Vector3(0, 0.02, SENSOR_MOUNT_Z + 0.5),
]);
const bearingLine = new THREE.Line(
  bearingGeo,
  new THREE.LineBasicMaterial({ color: HUMAN_COL, transparent: true, opacity: 0.55 })
);
bearingLine.visible = false;
robot.add(bearingLine);

function makePerson() {
  const g = new THREE.Group();
  // Ground glow ring
  const glow = new THREE.Mesh(
    new THREE.RingGeometry(0.12, 0.22, 32),
    new THREE.MeshBasicMaterial({ color: HUMAN_COL, transparent: true, opacity: 0.2, side: THREE.DoubleSide })
  );
  glow.rotation.x = -Math.PI / 2;
  glow.position.y = 0.004;
  glow.name = 'glow';
  g.add(glow);
  // Inner pulse ring
  const pulse = new THREE.Mesh(
    new THREE.RingGeometry(0.05, 0.08, 24),
    new THREE.MeshBasicMaterial({ color: HUMAN_COL, transparent: true, opacity: 0.35, side: THREE.DoubleSide })
  );
  pulse.rotation.x = -Math.PI / 2;
  pulse.position.y = 0.005;
  pulse.name = 'pulse';
  g.add(pulse);
  // Body capsule
  const mat = new THREE.MeshBasicMaterial({ color: HUMAN_COL, transparent: true, opacity: 0.9 });
  const torso = new THREE.Mesh(new THREE.CapsuleGeometry(0.055, 0.16, 6, 12), mat);
  torso.rotation.x = Math.PI / 2;
  torso.position.y = 0.1;
  g.add(torso);
  // Head
  const head = new THREE.Mesh(new THREE.SphereGeometry(0.045, 14, 14), mat);
  head.position.y = 0.22;
  g.add(head);
  // Vertical beam
  const beam = new THREE.Mesh(
    new THREE.CylinderGeometry(0.003, 0.003, 0.35, 4),
    new THREE.MeshBasicMaterial({ color: HUMAN_COL, transparent: true, opacity: 0.15 })
  );
  beam.position.y = 0.17;
  g.add(beam);
  g.userData.kind = 'human';
  return g;
}

function makeObstacle() {
  const g = new THREE.Group();
  // Base shadow
  const shadow = new THREE.Mesh(
    new THREE.PlaneGeometry(0.32, 0.28),
    new THREE.MeshBasicMaterial({ color: OBSTACLE_COL, transparent: true, opacity: 0.06, side: THREE.DoubleSide })
  );
  shadow.rotation.x = -Math.PI / 2;
  shadow.position.y = 0.003;
  g.add(shadow);
  // Main block
  const block = new THREE.Mesh(
    new THREE.BoxGeometry(0.24, 0.1, 0.2),
    new THREE.MeshBasicMaterial({ color: OBSTACLE_COL, transparent: true, opacity: 0.7 })
  );
  block.position.y = 0.05;
  g.add(block);
  // Wireframe overlay
  const wire = new THREE.LineSegments(
    new THREE.EdgesGeometry(new THREE.BoxGeometry(0.25, 0.11, 0.21)),
    new THREE.LineBasicMaterial({ color: OBSTACLE_COL, transparent: true, opacity: 0.9 })
  );
  wire.position.y = 0.05;
  g.add(wire);
  // Corner markers
  for (const sx of [-1, 1]) {
    for (const sz of [-1, 1]) {
      const corner = new THREE.Mesh(
        new THREE.BoxGeometry(0.02, 0.005, 0.02),
        new THREE.MeshBasicMaterial({ color: OBSTACLE_COL, opacity: 0.5, transparent: true })
      );
      corner.position.set(sx * 0.14, 0.003, sz * 0.12);
      g.add(corner);
    }
  }
  g.userData.kind = 'obstacle';
  return g;
}

function makeUncertain() {
  const g = new THREE.Group();
  const ring = new THREE.Mesh(
    new THREE.RingGeometry(0.08, 0.12, 6),
    new THREE.MeshBasicMaterial({ color: UNCERTAIN_COL, transparent: true, opacity: 0.4, side: THREE.DoubleSide })
  );
  ring.rotation.x = -Math.PI / 2;
  ring.position.y = 0.008;
  ring.name = 'ring';
  g.add(ring);
  const dot = new THREE.Mesh(
    new THREE.SphereGeometry(0.03, 8, 8),
    new THREE.MeshBasicMaterial({ color: UNCERTAIN_COL, transparent: true, opacity: 0.5 })
  );
  dot.position.y = 0.04;
  g.add(dot);
  g.userData.kind = 'uncertain';
  return g;
}

function getEntity(kind) {
  if (kind === 'human') return makePerson();
  if (kind === 'obstacle') return makeObstacle();
  return makeUncertain();
}

// ── State ──
let mainEntity = null;
const trackEntities = {};
const hitMarkers = {};
const trailDots = [];
const trailHistory = [];
const targetPos = new THREE.Vector3();
const currentPos = new THREE.Vector3();
let hasTarget = false;
let latest3d = null;
let mapYawRad = 0;
let targetMapYawRad = 0;

function bodyMmToLocal(xMm, zMm) {
  return { x: xMm / 1000, z: zMm / 1000 };
}

function fmtDeg(v) {
  const n = Math.round(Number(v) || 0);
  return `${n >= 0 ? '+' : ''}${n}°`;
}

function updateRobotPose(data) {
  const baseSign = Number(data.base_yaw_sign ?? -1);
  const bodyYaw = Number(
    data.front_offset_deg ?? data.encoder_yaw_deg ?? data.body_yaw_deg ?? 0
  );
  // Spin world under robot so nose stays screen-forward; front marker shows true +Z.
  targetMapYawRad = -deg(bodyYaw) * baseSign;
  mapYawRad += (targetMapYawRad - mapYawRad) * LERP;
  mapGroup.rotation.y = mapYawRad;
  robot.rotation.y = 0;
}

// ── Label sprites ──
function makeLabelSprite(text, hexCol) {
  const c = document.createElement('canvas');
  c.width = 320; c.height = 80;
  const ctx = c.getContext('2d');
  ctx.fillStyle = 'rgba(0,0,0,0.6)';
  const pad = 10;
  ctx.beginPath();
  ctx.roundRect(pad, 14, 300, 52, 8);
  ctx.fill();
  ctx.fillStyle = hexCol;
  ctx.font = 'bold 30px system-ui, sans-serif';
  ctx.textAlign = 'center';
  ctx.fillText(text, 160, 50);
  const tex = new THREE.CanvasTexture(c);
  const sp = new THREE.Sprite(new THREE.SpriteMaterial({ map: tex, transparent: true }));
  sp.scale.set(0.45, 0.12, 1);
  sp.position.y = 0.32;
  return sp;
}

// ── Scene update ──
function updateScene3d(data) {
  latest3d = data;
  updateRobotPose(data);
  const hits = data.hits || [];
  const t = performance.now() * 0.001;
  const bodyYawDeg = Number(data.body_yaw_deg ?? 0);
  const encYaw = Number(data.encoder_yaw_deg ?? bodyYawDeg);
  const frontOff = Number(data.front_offset_deg ?? bodyYawDeg);
  const aimErr = data.aim_error_deg;
  const drift = Number(data.imu_drift_correction_deg ?? 0);
  const stationary = data.fusion_stationary;

  // HUD
  if (hudLeft) {
    const age = ((Date.now() / 1000) - (data.last_ts || 0));
    const headOnBody = Number(data.head_yaw_on_body_deg ?? 0);
    hudLeft.innerHTML = `SCAN <span class="val">${age < 2 ? 'ACTIVE' : 'STALE'}</span> · FWD <span class="val">${fmtDeg(frontOff)}</span>`;
    if (aimErr != null) {
      hudLeft.innerHTML += ` · AIM <span class="val">${fmtDeg(aimErr)}</span>`;
    }
    if (Math.abs(headOnBody) > 0.5) {
      hudLeft.innerHTML += ` · HEAD <span class="val">${fmtDeg(headOnBody)}</span>`;
    }
  }
  if (hudOrient) {
    const st = stationary ? 'still' : 'move';
    hudOrient.innerHTML = `BODY <span class="val">${fmtDeg(bodyYawDeg)}</span> · ENC <span class="val">${fmtDeg(encYaw)}</span> · DRIFT <span class="val">${fmtDeg(drift)}</span> · ${st}`;
  }
  if (hudRight) {
    const ntracks = (data.tracks || []).length;
    hudRight.innerHTML = `TRACKS <span class="val">${ntracks}</span> · HITS <span class="val">${hits.length}</span>`;
  }

  // Per-zone hit dots (on floor at projected position)
  for (const key of ['L', 'C', 'R']) {
    const hit = hits.find(h => h.zone === key);
    if (!hit) {
      if (hitMarkers[key]) hitMarkers[key].visible = false;
      continue;
    }
    if (!hitMarkers[key]) {
      const geo = new THREE.OctahedronGeometry(0.028, 0);
      const mat = new THREE.MeshBasicMaterial({ color: 0x555555, transparent: true, opacity: 0.7 });
      hitMarkers[key] = new THREE.Mesh(geo, mat);
      entityGroup.add(hitMarkers[key]);
    }
    const col = KIND_COL[hit.kind] || UNCERTAIN_COL;
    const local = bodyMmToLocal(hit.x_mm, hit.z_mm);
    hitMarkers[key].material.color.setHex(col);
    hitMarkers[key].position.set(local.x, 0.018, local.z);
    hitMarkers[key].rotation.y = t * 2;
    hitMarkers[key].visible = true;
  }

  // Per-track entities (multiple people / obstacles)
  const tracks = data.tracks || [];
  const primaryId = data.primary_target?.id;
  const liveIds = new Set();

  for (const track of tracks) {
    const id = String(track.id);
    liveIds.add(id);
    const kind = track.kind || 'uncertain';
    const local = bodyMmToLocal(track.x_mm, track.z_mm);
    const isPrimary = track.id === primaryId;

    if (!trackEntities[id] || trackEntities[id].userData.kind !== kind) {
      if (trackEntities[id]) entityGroup.remove(trackEntities[id]);
      trackEntities[id] = getEntity(kind);
      const label = isPrimary
        ? (kind === 'human' ? 'TARGET' : kind === 'obstacle' ? 'TARGET' : '?')
        : `#${id}`;
      const lCol = isPrimary
        ? (kind === 'human' ? '#38bdf8' : kind === 'obstacle' ? '#f97316' : '#9ca3af')
        : '#6b7280';
      trackEntities[id].add(makeLabelSprite(label, lCol));
      trackEntities[id].userData.trackId = id;
      entityGroup.add(trackEntities[id]);
    }

    const ent = trackEntities[id];
    ent.position.set(local.x, 0, local.z);
    ent.visible = true;
    ent.scale.setScalar(isPrimary ? 1.0 : 0.82);
    if (ent.material) ent.material.opacity = isPrimary ? 0.95 : 0.55;
  }

  for (const id of Object.keys(trackEntities)) {
    if (!liveIds.has(id)) {
      entityGroup.remove(trackEntities[id]);
      delete trackEntities[id];
    }
  }

  const fused = data.primary_target || data.fused;
  if (fused) {
    const kind = fused.kind || 'uncertain';
    const local = bodyMmToLocal(fused.x_mm, fused.z_mm);

    bearingLine.geometry.setFromPoints([
      new THREE.Vector3(0, 0.02, SENSOR_MOUNT_Z),
      new THREE.Vector3(local.x, 0.02, local.z),
    ]);
    bearingLine.material.color.setHex(KIND_COL[kind] || UNCERTAIN_COL);
    bearingLine.visible = true;

    if (mainEntity) {
      mainEntity.visible = false;
    }

    // Readout
    const dist = Math.round(fused.dist_mm || Math.hypot(fused.x_mm, fused.z_mm - SENSOR_MOUNT_Z * 1000));
    const bearing = Math.round(fused.bearing_deg ?? Math.atan2(fused.x_mm, fused.z_mm) * 180 / Math.PI);
    const tag = kind === 'human' ? 'PERSON' : kind === 'obstacle' ? 'OBSTACLE' : 'UNCERTAIN';
    const cls = kind === 'human' ? 'readout-human' : kind === 'obstacle' ? 'readout-obstacle' : 'readout-uncertain';
    const ntracks = tracks.length;
    readout.className = 'object-readout ' + cls;
    readout.innerHTML = `<strong>${tag}</strong> #${fused.id ?? '?'} · ${ntracks} track${ntracks === 1 ? '' : 's'} · ${dist} mm · aim ${fmtDeg(aimErr ?? bearing)} · fwd ${fmtDeg(frontOff)} · ${Math.round((fused.confidence || 0) * 100)}% — ${fused.reason || ''}`;
  } else {
    bearingLine.visible = false;
    if (mainEntity) mainEntity.visible = false;
    for (const id of Object.keys(trackEntities)) {
      entityGroup.remove(trackEntities[id]);
      delete trackEntities[id];
    }
    readout.className = 'object-readout';
    readout.textContent = 'Clear — no object in sensor field';
  }
}

window.updateScene3d = updateScene3d;

// ── Resize ──
function resize3d() {
  const w = view.clientWidth, h = view.clientHeight;
  if (!w || !h) return;
  const aspect = w / h;
  const frustum = 1.3;
  if (aspect > 1) {
    camera.left = -frustum * aspect; camera.right = frustum * aspect;
    camera.top = frustum; camera.bottom = -frustum;
  } else {
    camera.left = -frustum; camera.right = frustum;
    camera.top = frustum / aspect; camera.bottom = -frustum / aspect;
  }
  camera.updateProjectionMatrix();
  renderer.setSize(w, h, false);
}
window.addEventListener('resize', resize3d);
resize3d();

// ── Animate ──
function animate() {
  requestAnimationFrame(animate);
  controls.update();
  if (latest3d) updateScene3d(latest3d);
  renderer.render(scene, camera);
}
animate();
