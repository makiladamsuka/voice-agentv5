import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';

const view = document.getElementById('view3d');
const vizLoading = document.getElementById('viz-loading');
const hudLeft = document.getElementById('hud-left');
const hudRight = document.getElementById('hud-right');
const readout = document.getElementById('object-readout');

const HUMAN_COL = 0x38bdf8;
const OBSTACLE_COL = 0xf97316;
const UNCERTAIN_COL = 0x6b7280;
const ROBOT_COL = 0xc8d6e5;
const KIND_COL = { human: HUMAN_COL, obstacle: OBSTACLE_COL, uncertain: UNCERTAIN_COL };
const ZONE_ANGLE = { L: -45, C: 0, R: 45 };
const deg = d => d * Math.PI / 180;
const MAP_R = 2.2;
const TRAIL_MAX = 28;
const LERP = 0.12;

// ── Renderer ──
const scene = new THREE.Scene();
scene.background = new THREE.Color(0x080a0f);
scene.fog = new THREE.FogExp2(0x080a0f, 0.18);

const camera = new THREE.OrthographicCamera(-1, 1, 1, -1, 0.1, 30);
camera.position.set(0.1, 3.6, 0.3);
camera.lookAt(0, 0, 0.4);

const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false });
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
view.appendChild(renderer.domElement);
if (vizLoading) vizLoading.style.display = 'none';

const controls = new OrbitControls(camera, renderer.domElement);
controls.target.set(0, 0, 0.4);
controls.enableDamping = true;
controls.dampingFactor = 0.08;
controls.maxPolarAngle = Math.PI * 0.2;
controls.minPolarAngle = Math.PI * 0.05;
controls.enableRotate = true;
controls.rotateSpeed = 0.5;

// ── Floor grid ──
const gridHelper = new THREE.PolarGridHelper(MAP_R, 8, 6, 64, 0x1a1f2e, 0x1a1f2e);
gridHelper.position.y = 0.001;
scene.add(gridHelper);

// ── Range rings with glow ──
for (const r of [0.5, 1.0, 1.5, 2.0]) {
  const ring = new THREE.Mesh(
    new THREE.RingGeometry(r - 0.005, r + 0.005, 96),
    new THREE.MeshBasicMaterial({
      color: 0x38bdf8, side: THREE.DoubleSide,
      transparent: true, opacity: r === 1.0 ? 0.18 : 0.08
    })
  );
  ring.rotation.x = -Math.PI / 2;
  ring.position.y = 0.003;
  scene.add(ring);
}

// ── Range labels ──
function makeTextSprite(text, x, z, col = '#4a5f7a') {
  const c = document.createElement('canvas');
  c.width = 128; c.height = 40;
  const ctx = c.getContext('2d');
  ctx.font = 'bold 22px system-ui, sans-serif';
  ctx.textAlign = 'center';
  ctx.fillStyle = col;
  ctx.fillText(text, 64, 28);
  const tex = new THREE.CanvasTexture(c);
  const sp = new THREE.Sprite(new THREE.SpriteMaterial({ map: tex, transparent: true, opacity: 0.7 }));
  sp.scale.set(0.28, 0.09, 1);
  sp.position.set(x, 0.02, z);
  return sp;
}
for (const r of [0.5, 1.0, 1.5, 2.0]) {
  scene.add(makeTextSprite(`${r * 1000}`, 0.18, r));
}

// ── Radar sweep line ──
const sweepGeo = new THREE.BufferGeometry().setFromPoints([
  new THREE.Vector3(0, 0.006, 0),
  new THREE.Vector3(0, 0.006, MAP_R)
]);
const sweepLine = new THREE.Line(sweepGeo,
  new THREE.LineBasicMaterial({ color: HUMAN_COL, transparent: true, opacity: 0.35 })
);
scene.add(sweepLine);

// Sweep trail (fading arc)
const sweepTrailGeo = new THREE.RingGeometry(0.02, MAP_R, 64, 1, 0, deg(30));
const sweepTrail = new THREE.Mesh(sweepTrailGeo,
  new THREE.MeshBasicMaterial({
    color: HUMAN_COL, transparent: true, opacity: 0.04, side: THREE.DoubleSide
  })
);
sweepTrail.rotation.x = -Math.PI / 2;
sweepTrail.position.y = 0.005;
scene.add(sweepTrail);

// ── Robot body (sleek top-down) ──
const robot = new THREE.Group();
scene.add(robot);

const bodyShape = new THREE.Shape();
const bw = 0.22, bd = 0.28, cr = 0.1;
bodyShape.moveTo(-bw + cr, -bd);
bodyShape.lineTo(bw - cr, -bd);
bodyShape.quadraticCurveTo(bw, -bd, bw, -bd + cr);
bodyShape.lineTo(bw, bd - cr);
bodyShape.quadraticCurveTo(bw, bd, bw - cr, bd);
bodyShape.lineTo(-bw + cr, bd);
bodyShape.quadraticCurveTo(-bw, bd, -bw, bd - cr);
bodyShape.lineTo(-bw, -bd + cr);
bodyShape.quadraticCurveTo(-bw, -bd, -bw + cr, -bd);

const body = new THREE.Mesh(
  new THREE.ShapeGeometry(bodyShape),
  new THREE.MeshBasicMaterial({ color: ROBOT_COL })
);
body.rotation.x = -Math.PI / 2;
body.position.y = 0.012;
robot.add(body);

// Center accent line
const accentLine = new THREE.Mesh(
  new THREE.PlaneGeometry(0.04, 0.48),
  new THREE.MeshBasicMaterial({ color: HUMAN_COL, transparent: true, opacity: 0.6, side: THREE.DoubleSide })
);
accentLine.rotation.x = -Math.PI / 2;
accentLine.position.set(0, 0.014, 0.02);
robot.add(accentLine);

// Nose indicator
const nose = new THREE.Mesh(
  new THREE.ConeGeometry(0.06, 0.1, 3),
  new THREE.MeshBasicMaterial({ color: HUMAN_COL, transparent: true, opacity: 0.8 })
);
nose.rotation.x = -Math.PI / 2;
nose.position.set(0, 0.013, 0.34);
robot.add(nose);

// Robot glow ring
const robotGlow = new THREE.Mesh(
  new THREE.RingGeometry(0.34, 0.38, 32),
  new THREE.MeshBasicMaterial({ color: HUMAN_COL, transparent: true, opacity: 0.08, side: THREE.DoubleSide })
);
robotGlow.rotation.x = -Math.PI / 2;
robotGlow.position.y = 0.004;
robot.add(robotGlow);

// ── FOV wedges ──
for (const [, angle] of Object.entries(ZONE_ANGLE)) {
  const g = new THREE.Group();
  g.rotation.y = deg(angle);
  const wedge = new THREE.Mesh(
    new THREE.CircleGeometry(1.9, 48, -deg(11), deg(22)),
    new THREE.MeshBasicMaterial({ color: HUMAN_COL, transparent: true, opacity: 0.025, side: THREE.DoubleSide })
  );
  wedge.rotation.x = -Math.PI / 2;
  wedge.position.set(0, 0.003, 0.15);
  g.add(wedge);
  // Edge line
  for (const sign of [-1, 1]) {
    const edgeAngle = deg(angle + sign * 11);
    const pts = [
      new THREE.Vector3(Math.sin(edgeAngle) * 0.15, 0.004, Math.cos(edgeAngle) * 0.15),
      new THREE.Vector3(Math.sin(edgeAngle) * 1.9, 0.004, Math.cos(edgeAngle) * 1.9),
    ];
    const line = new THREE.Line(
      new THREE.BufferGeometry().setFromPoints(pts),
      new THREE.LineBasicMaterial({ color: HUMAN_COL, transparent: true, opacity: 0.06 })
    );
    scene.add(line);
  }
  robot.add(g);
}

// ── Entity factories ──
const entityGroup = new THREE.Group();
scene.add(entityGroup);

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

// ── State ──
let mainEntity = null;
const hitMarkers = {};
const trailDots = [];
const trailHistory = [];
const targetPos = new THREE.Vector3();
const currentPos = new THREE.Vector3();
let hasTarget = false;
let latest3d = null;
let sweepAngle = 0;

function getEntity(kind) {
  if (kind === 'human') return makePerson();
  if (kind === 'obstacle') return makeObstacle();
  return makeUncertain();
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
  const hits = data.hits || [];
  const fused = data.fused;
  const t = performance.now() * 0.001;

  // HUD
  if (hudLeft) {
    const age = ((Date.now() / 1000) - (data.last_ts || 0));
    hudLeft.innerHTML = `SCAN <span class="val">${age < 2 ? 'ACTIVE' : 'STALE'}</span>`;
  }
  if (hudRight) {
    hudRight.innerHTML = `OBJECTS <span class="val">${hits.length}</span>`;
  }

  // Per-zone hit dots
  for (const key of ['L', 'C', 'R']) {
    const hit = hits.find(h => h.zone === key);
    if (!hit) {
      if (hitMarkers[key]) hitMarkers[key].visible = false;
      continue;
    }
    if (!hitMarkers[key]) {
      // Small diamond marker
      const geo = new THREE.OctahedronGeometry(0.03, 0);
      const mat = new THREE.MeshBasicMaterial({ color: 0x555555, transparent: true, opacity: 0.6 });
      hitMarkers[key] = new THREE.Mesh(geo, mat);
      entityGroup.add(hitMarkers[key]);
    }
    const col = KIND_COL[hit.kind] || UNCERTAIN_COL;
    hitMarkers[key].material.color.setHex(col);
    hitMarkers[key].position.set(hit.x_mm / 1000, 0.025, hit.z_mm / 1000);
    hitMarkers[key].rotation.y = t * 2;
    hitMarkers[key].visible = true;
  }

  // Main fused entity
  if (fused) {
    const kind = fused.kind || 'uncertain';
    targetPos.set(fused.x_mm / 1000, 0, fused.z_mm / 1000);

    if (!mainEntity || mainEntity.userData.kind !== kind) {
      if (mainEntity) entityGroup.remove(mainEntity);
      mainEntity = getEntity(kind);
      const label = kind === 'human' ? 'PERSON' : kind === 'obstacle' ? 'OBSTACLE' : '?';
      const lCol = kind === 'human' ? '#38bdf8' : kind === 'obstacle' ? '#f97316' : '#9ca3af';
      mainEntity.add(makeLabelSprite(label, lCol));
      entityGroup.add(mainEntity);
      currentPos.copy(targetPos);
      hasTarget = true;
    }
    hasTarget = true;

    // Smooth lerp
    currentPos.lerp(targetPos, LERP);
    mainEntity.position.copy(currentPos);
    mainEntity.visible = true;

    // Animate human
    if (kind === 'human') {
      const breathe = 1 + 0.06 * Math.sin(t * 3);
      mainEntity.scale.setScalar(breathe);
      const glow = mainEntity.getObjectByName('glow');
      if (glow) {
        glow.scale.setScalar(1 + 0.3 * Math.sin(t * 4));
        glow.material.opacity = 0.12 + 0.1 * Math.sin(t * 4);
      }
      const pulse = mainEntity.getObjectByName('pulse');
      if (pulse) {
        pulse.scale.setScalar(1 + 0.5 * Math.abs(Math.sin(t * 5)));
        pulse.material.opacity = 0.35 * (0.5 + 0.5 * Math.sin(t * 5));
      }
    } else {
      mainEntity.scale.setScalar(1);
    }

    // Uncertain spin
    if (kind === 'uncertain') {
      const ring = mainEntity.getObjectByName('ring');
      if (ring) ring.rotation.z = t * 1.5;
    }

    // Trail (only for humans)
    trailHistory.unshift(new THREE.Vector3(currentPos.x, 0.008, currentPos.z));
    if (trailHistory.length > TRAIL_MAX) trailHistory.length = TRAIL_MAX;
    while (trailDots.length < trailHistory.length) {
      const d = new THREE.Mesh(
        new THREE.CircleGeometry(0.015, 8),
        new THREE.MeshBasicMaterial({ color: HUMAN_COL, transparent: true, opacity: 0, side: THREE.DoubleSide })
      );
      d.rotation.x = -Math.PI / 2;
      entityGroup.add(d);
      trailDots.push(d);
    }
    trailDots.forEach((d, i) => {
      const p = trailHistory[i];
      if (p && kind === 'human') {
        d.position.copy(p);
        d.material.opacity = 0.4 * (1 - i / TRAIL_MAX);
        d.material.color.setHex(HUMAN_COL);
        d.scale.setScalar(1 - i * 0.02);
      } else {
        d.material.opacity = 0;
      }
    });

    // Readout
    const dist = Math.round(Math.hypot(fused.x_mm, fused.z_mm));
    const bearing = Math.round(Math.atan2(fused.x_mm, fused.z_mm) * 180 / Math.PI);
    const tag = kind === 'human' ? 'PERSON' : kind === 'obstacle' ? 'OBSTACLE' : 'UNCERTAIN';
    const cls = kind === 'human' ? 'readout-human' : kind === 'obstacle' ? 'readout-obstacle' : 'readout-uncertain';
    readout.className = 'object-readout ' + cls;
    readout.innerHTML = `<strong>${tag}</strong> · ${dist} mm · ${bearing > 0 ? '+' : ''}${bearing}° · ${Math.round(fused.confidence * 100)}% — ${fused.reason}`;
  } else {
    if (mainEntity) { entityGroup.remove(mainEntity); mainEntity = null; }
    hasTarget = false;
    trailHistory.length = 0;
    trailDots.forEach(d => { d.material.opacity = 0; });
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
  const t = performance.now() * 0.001;

  // Radar sweep
  sweepAngle += 0.012;
  sweepLine.rotation.y = sweepAngle;
  sweepTrail.rotation.z = -sweepAngle + deg(15);
  sweepLine.material.opacity = 0.2 + 0.15 * Math.sin(t * 2);

  controls.update();
  if (latest3d) updateScene3d(latest3d);
  renderer.render(scene, camera);
}
animate();
