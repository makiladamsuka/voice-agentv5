import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { bodyLocalToWorld, bodyMmToWorld, mapBaseYawDeg, robotMapYawRad } from './yaw_map_pose.mjs';

const view = document.getElementById('view3d');
const vizLoading = document.getElementById('viz-loading');

function hideVizLoading() {
  if (vizLoading) vizLoading.style.display = 'none';
}

function showVizError(msg) {
  if (!vizLoading) return;
  vizLoading.style.display = 'flex';
  vizLoading.style.color = '#fca5a5';
  vizLoading.textContent = msg;
}

if (!view) {
  showVizError('3D view container missing');
  window.updateScene3d = () => {};
} else {
const hudLeft = document.getElementById('hud-left');
const hudRight = document.getElementById('hud-right');
const hudOrient = document.getElementById('hud-orient');
const readout = document.getElementById('object-readout');

const HUMAN_COL = 0x38bdf8;
const OBSTACLE_COL = 0xf97316;
const UNCERTAIN_COL = 0x6b7280;
const ROBOT_COL = 0xc8d6e5;
const TORSO_COL = 0x141414;
const HEAD_COL = 0xf472b6;
const HEADING_COL = 0x111111;
const FRONT_MARK_COL = 0xffffff;
const HOME_COL = 0x94a3b8;
const KIND_COL = { human: HUMAN_COL, obstacle: OBSTACLE_COL, uncertain: UNCERTAIN_COL };
const ZONE_ANGLE = { L: -45, C: 0, R: 45 };
const deg = d => d * Math.PI / 180;
const TRAIL_MAX = 28;
const ROBOT_RADIUS = 0.275;
const BASE_THICKNESS = 0.07;
const TORSO_BELLY_R = 0.148;
const TORSO_BELLY_SY = 0.9;
const TORSO_BELLY_SX = 1.1;
const TORSO_BELLY_SZ = 1.14;
const TORSO_H = 2 * TORSO_BELLY_R * TORSO_BELLY_SY;
const HEAD_W = 0.40;
const HEAD_H = 0.15;
const HEAD_D = 0.30;
const HEAD_CORNER = 0.034;
const SENSOR_MOUNT_Z = ROBOT_RADIUS;

function roundedBoxGeometry(width, depth, height, radius) {
  const hw = width / 2;
  const hd = depth / 2;
  const r = Math.min(radius, hw * 0.45, hd * 0.45);
  const shape = new THREE.Shape();
  shape.moveTo(-hw + r, -hd);
  shape.lineTo(hw - r, -hd);
  shape.quadraticCurveTo(hw, -hd, hw, -hd + r);
  shape.lineTo(hw, hd - r);
  shape.quadraticCurveTo(hw, hd, hw - r, hd);
  shape.lineTo(-hw + r, hd);
  shape.quadraticCurveTo(-hw, hd, -hw, hd - r);
  shape.lineTo(-hw, -hd + r);
  shape.quadraticCurveTo(-hw, -hd, -hw + r, -hd);
  const geo = new THREE.ExtrudeGeometry(shape, {
    depth: height,
    bevelEnabled: true,
    bevelThickness: Math.min(r, height * 0.38),
    bevelSize: r * 0.88,
    bevelSegments: 3,
    curveSegments: 6,
  });
  geo.rotateX(-Math.PI / 2);
  geo.translate(0, height / 2, 0);
  return geo;
}

// ── Renderer ──
const scene = new THREE.Scene();
scene.background = new THREE.Color(0x0a0c10);

const camera = new THREE.OrthographicCamera(-1, 1, 1, -1, 0.1, 30);
camera.position.set(0, 3.9, 0.78);
camera.lookAt(0, 0.26, 0.55);

const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false });
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
renderer.domElement.style.position = 'relative';
renderer.domElement.style.zIndex = '1';
view.appendChild(renderer.domElement);
hideVizLoading();

const controls = new OrbitControls(camera, renderer.domElement);
controls.target.set(0, 0.26, 0.55);
controls.enableDamping = true;
controls.dampingFactor = 0.08;
controls.maxPolarAngle = Math.PI * 0.2;
controls.minPolarAngle = Math.PI * 0.05;
controls.enableRotate = true;
controls.rotateSpeed = 0.5;

// Fixed world — HOME markers stay put; robot rotates with IMU base yaw from HOME
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

// Startup HOME forward marker (+Z world) — grey cone; map rotation = IMU from HOME
const frontMarker = new THREE.Mesh(
  new THREE.ConeGeometry(0.08, 0.14, 3),
  new THREE.MeshBasicMaterial({ color: HOME_COL, transparent: true, opacity: 0.6 })
);
frontMarker.rotation.x = -Math.PI / 2;
frontMarker.position.set(0, 0.02, 1.1);
mapGroup.add(frontMarker);
const frontRing = new THREE.Mesh(
  new THREE.RingGeometry(0.1, 0.12, 32),
  new THREE.MeshBasicMaterial({ color: 0x64748b, transparent: true, opacity: 0.4, side: THREE.DoubleSide })
);
frontRing.rotation.x = -Math.PI / 2;
frontRing.position.set(0, 0.018, 1.1);
mapGroup.add(frontRing);

// ±max_yaw limit arc (rotates with world)
const limitGroup = new THREE.Group();
mapGroup.add(limitGroup);
let lastMaxYaw = 0;

function updateLimitArc(maxYawDeg) {
  limitGroup.clear();
  if (!maxYawDeg || maxYawDeg < 5) return;
  const r = 1.15;
  const segments = 48;
  const a0 = deg(-maxYawDeg);
  const a1 = deg(maxYawDeg);
  const pts = [];
  for (let i = 0; i <= segments; i++) {
    const t = a0 + ((a1 - a0) * i) / segments;
    pts.push(new THREE.Vector3(Math.sin(t) * r, 0.016, Math.cos(t) * r));
  }
  const geo = new THREE.BufferGeometry().setFromPoints(pts);
  limitGroup.add(new THREE.Line(geo, new THREE.LineBasicMaterial({ color: 0x475569, transparent: true, opacity: 0.35 })));
}

// ── Robot: base disc → belly → head (pan + pitch); ToF sensors on base front ──
const robot = new THREE.Group();
scene.add(robot);

const baseDisc = new THREE.Mesh(
  new THREE.CylinderGeometry(ROBOT_RADIUS, ROBOT_RADIUS, BASE_THICKNESS, 64),
  new THREE.MeshBasicMaterial({ color: ROBOT_COL })
);
baseDisc.position.y = BASE_THICKNESS / 2;
robot.add(baseDisc);

const baseStrip = new THREE.Mesh(
  new THREE.BoxGeometry(0.05, 0.02, 0.24),
  new THREE.MeshBasicMaterial({ color: HEADING_COL })
);
baseStrip.position.set(0, BASE_THICKNESS + 0.01, ROBOT_RADIUS * 0.42);
robot.add(baseStrip);

const baseOutline = new THREE.Mesh(
  new THREE.RingGeometry(ROBOT_RADIUS - 0.01, ROBOT_RADIUS + 0.01, 64),
  new THREE.MeshBasicMaterial({ color: 0x334155, transparent: true, opacity: 0.5, side: THREE.DoubleSide })
);
baseOutline.rotation.x = -Math.PI / 2;
baseOutline.position.y = BASE_THICKNESS + 0.006;
robot.add(baseOutline);

const torsoMount = new THREE.Group();
torsoMount.position.y = BASE_THICKNESS;
robot.add(torsoMount);

const torso = new THREE.Mesh(
  new THREE.SphereGeometry(TORSO_BELLY_R, 36, 28),
  new THREE.MeshBasicMaterial({ color: TORSO_COL })
);
torso.scale.set(TORSO_BELLY_SX, TORSO_BELLY_SY, TORSO_BELLY_SZ);
torso.position.y = TORSO_BELLY_R * TORSO_BELLY_SY;
torsoMount.add(torso);

const neckRing = new THREE.Mesh(
  new THREE.TorusGeometry(TORSO_BELLY_R * TORSO_BELLY_SX * 0.55, 0.009, 8, 32),
  new THREE.MeshBasicMaterial({ color: 0x2a2a2a })
);
neckRing.rotation.x = Math.PI / 2;
neckRing.position.y = TORSO_H - 0.012;
torsoMount.add(neckRing);

const headMount = new THREE.Group();
headMount.position.y = TORSO_H;
torsoMount.add(headMount);

const panNode = new THREE.Group();
headMount.add(panNode);

const tiltNode = new THREE.Group();
panNode.add(tiltNode);

const headBody = new THREE.Mesh(
  roundedBoxGeometry(HEAD_W, HEAD_D, HEAD_H, HEAD_CORNER),
  new THREE.MeshBasicMaterial({ color: HEAD_COL })
);
headBody.geometry.computeBoundingBox();
tiltNode.add(headBody);

const headBounds = headBody.geometry.boundingBox;
const frontZ = headBounds.max.z + 0.012;
const eyeY = headBounds.min.y + (headBounds.max.y - headBounds.min.y) * 0.58;
const EYE_WHITE_R = 0.032;
const EYE_PUPIL_R = 0.014;
const EYE_GAP = 0.1;

function addEye(x) {
  const white = new THREE.Mesh(
    new THREE.CircleGeometry(EYE_WHITE_R, 20),
    new THREE.MeshBasicMaterial({ color: FRONT_MARK_COL, side: THREE.DoubleSide, depthTest: false })
  );
  white.position.set(x, eyeY, frontZ);
  white.renderOrder = 2;
  tiltNode.add(white);

  const pupil = new THREE.Mesh(
    new THREE.CircleGeometry(EYE_PUPIL_R, 16),
    new THREE.MeshBasicMaterial({ color: HEADING_COL, side: THREE.DoubleSide, depthTest: false })
  );
  pupil.position.set(x, eyeY, frontZ + 0.003);
  pupil.renderOrder = 3;
  tiltNode.add(pupil);
}

addEye(-EYE_GAP / 2);
addEye(EYE_GAP / 2);

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

// ── Detected objects (world frame — body mm rotated by current base yaw) ──
const entityGroup = new THREE.Group();
mapGroup.add(entityGroup);

// Bearing line from sensor plane toward fused hit (world coords)
const bearingLine = new THREE.Line(
  new THREE.BufferGeometry().setFromPoints([
    new THREE.Vector3(0, 0.02, SENSOR_MOUNT_Z),
    new THREE.Vector3(0, 0.02, SENSOR_MOUNT_Z + 0.5),
  ]),
  new THREE.LineBasicMaterial({ color: HUMAN_COL, transparent: true, opacity: 0.55 })
);
bearingLine.visible = false;
mapGroup.add(bearingLine);

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
let robotYawRad = 0;

function fmtDeg(v) {
  const n = Math.round(Number(v) || 0);
  return `${n >= 0 ? '+' : ''}${n}°`;
}

function updateRobotPose(data) {
  const panSign = Number(data.pan_yaw_sign ?? data.base_yaw_sign ?? -1);
  const tiltSign = Number(data.tilt_sign ?? 1);
  const headPan = Number(data.pan_cmd_from_home_deg ?? data.pan_from_home_deg ?? data.head_yaw_on_body_deg ?? 0);
  const headPitch = Number(data.pitch_from_home_deg ?? 0);
  const maxYaw = Number(data.max_yaw_deg ?? 120);

  mapGroup.rotation.y = 0;
  // Encoder-closed-loop bearing spins — map heading tracks viz_base_yaw_deg (enc from HOME).
  robotYawRad = robotMapYawRad(data);
  robot.rotation.y = robotYawRad;
  panNode.rotation.y = deg(headPan) * panSign;
  tiltNode.rotation.x = deg(headPitch) * tiltSign;

  if (Math.abs(maxYaw - lastMaxYaw) > 0.5) {
    lastMaxYaw = maxYaw;
    updateLimitArc(maxYaw);
  }
  return robotYawRad;
}

// ── Label sprites ──
function makeLabelSprite(text, hexCol) {
  const c = document.createElement('canvas');
  c.width = 320; c.height = 80;
  const ctx = c.getContext('2d');
  ctx.fillStyle = 'rgba(0,0,0,0.6)';
  const pad = 10;
  if (typeof ctx.roundRect === 'function') {
    ctx.beginPath();
    ctx.roundRect(pad, 14, 300, 52, 8);
    ctx.fill();
  } else {
    ctx.fillRect(pad, 14, 300, 52);
  }
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
  hideVizLoading();
  try {
  latest3d = data;
  const yawRad = updateRobotPose(data);
  const hits = data.hits || [];
  const t = performance.now() * 0.001;
  const bodyYawDeg = Number(data.from_home_imu_deg ?? data.body_yaw_deg ?? 0);
  const encYaw = Number(data.from_home_enc_deg ?? data.encoder_yaw_deg ?? 0);
  const mapYawDeg = mapBaseYawDeg(data);
  const frontOff = mapYawDeg;
  const disagreement = Number(data.disagreement_deg ?? (bodyYawDeg - encYaw));
  const aimErr = data.aim_error_deg;
  const drift = Number(data.imu_drift_correction_deg ?? 0);
  const stationary = data.fusion_stationary;
  const tickDelta = Number(data.encoder_count_delta ?? 0);
  const baseRotating = Boolean(data.base_rotating);
  const sensorWorld = bodyLocalToWorld(0, SENSOR_MOUNT_Z, yawRad);

  // HUD
  if (hudLeft) {
    const age = ((Date.now() / 1000) - (data.last_ts || 0));
    const approachPhase = String(data.approach_phase || 'idle');
    const clearRem = Number(data.clear_wait_remaining_sec || 0);
    let scan;
    if (approachPhase === 'homing') {
      scan = 'HOMING';
    } else if (approachPhase === 'clear_wait' && clearRem > 0.05) {
      scan = `CLEAR ${clearRem.toFixed(1)}s`;
    } else if (approachPhase === 'aiming' || baseRotating) {
      scan = 'AIMING';
    } else if (age < 2) {
      scan = approachPhase === 'tracking' ? 'TRACKING' : 'ACTIVE';
    } else {
      scan = 'STALE';
    }
    hudLeft.innerHTML = `SCAN <span class="val">${scan}</span> · MAP <span class="val">${fmtDeg(frontOff)}</span>`;
    if (aimErr != null) {
      hudLeft.innerHTML += ` · AIM <span class="val">${fmtDeg(aimErr)}</span>`;
    }
  }
  if (hudOrient) {
    const st = stationary ? 'still' : 'move';
    const headPan = Number(data.pan_cmd_from_home_deg ?? data.pan_from_home_deg ?? 0);
    const headPitch = Number(data.pitch_from_home_deg ?? 0);
    hudOrient.innerHTML =
      `<strong>FROM HOME</strong> enc <span class="val">${fmtDeg(encYaw)}</span>` +
      ` · imu <span class="val">${fmtDeg(bodyYawDeg)}</span>` +
      ` · head <span class="val">${fmtDeg(headPan)}/${fmtDeg(headPitch)}</span>` +
      ` · Δ <span class="val">${fmtDeg(disagreement)}</span>` +
      ` · ticksΔ <span class="val">${tickDelta >= 0 ? '+' : ''}${Math.round(tickDelta)}</span>` +
      ` · ${st}`;
  }
  if (hudRight) {
    const ntracks = (data.tracks || []).length;
    hudRight.innerHTML = `TRACKS <span class="val">${ntracks}</span> · HITS <span class="val">${hits.length}</span>`;
  }

  // Per-zone hit dots (world floor — body mm → world so targets stay put while base spins)
  for (const key of ['L', 'C', 'R']) {
    const hit = hits.find(h => h.zone === key);
    if (!hit || baseRotating) {
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
    const world = bodyMmToWorld(hit.x_mm, hit.z_mm, yawRad);
    hitMarkers[key].material.color.setHex(col);
    hitMarkers[key].position.set(world.x, 0.018, world.z);
    hitMarkers[key].rotation.y = t * 2;
    hitMarkers[key].visible = true;
  }

  // Per-track entities (world frame)
  const tracks = data.tracks || [];
  const primaryId = data.primary_target?.id;
  const liveIds = new Set();

  if (baseRotating) {
    for (const key of Object.keys(hitMarkers)) {
      if (hitMarkers[key]) hitMarkers[key].visible = false;
    }
    for (const id of Object.keys(trackEntities)) {
      entityGroup.remove(trackEntities[id]);
      delete trackEntities[id];
    }
    bearingLine.visible = false;
  } else for (const track of tracks) {
    const id = String(track.id);
    liveIds.add(id);
    const kind = track.kind || 'uncertain';
    const world = bodyMmToWorld(track.x_mm, track.z_mm, yawRad);
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
    ent.position.set(world.x, 0, world.z);
    ent.visible = true;
    ent.scale.setScalar(isPrimary ? 1.0 : 0.82);
    if (ent.material) ent.material.opacity = isPrimary ? 0.95 : 0.55;
  }

  if (!baseRotating) {
    for (const id of Object.keys(trackEntities)) {
      if (!liveIds.has(id)) {
        entityGroup.remove(trackEntities[id]);
        delete trackEntities[id];
      }
    }
  }

  const fused = data.primary_target || data.fused;
  if (fused && !baseRotating) {
    const kind = fused.kind || 'uncertain';
    const world = bodyMmToWorld(fused.x_mm, fused.z_mm, yawRad);

    bearingLine.geometry.setFromPoints([
      new THREE.Vector3(sensorWorld.x, 0.02, sensorWorld.z),
      new THREE.Vector3(world.x, 0.02, world.z),
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
  } else if (!baseRotating) {
    bearingLine.visible = false;
    if (mainEntity) mainEntity.visible = false;
    if (tracks.length === 0) {
      for (const id of Object.keys(trackEntities)) {
        entityGroup.remove(trackEntities[id]);
        delete trackEntities[id];
      }
      readout.className = 'object-readout';
      readout.textContent = 'Clear — no object in sensor field';
    }
  } else {
    bearingLine.visible = false;
  }
  } catch (err) {
    console.error('[tof_viz_3d] updateScene3d', err);
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

} // end view init
