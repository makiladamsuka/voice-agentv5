import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';

const view = document.getElementById('view3d');
const ROBOT_COL = 0xc8d6e5;
const ENC_COL = 0x38bdf8;
const IMU_COL = 0xfb923c;
const HOME_COL = 0x94a3b8;
const ROBOT_RADIUS = 0.275;
const deg = (d) => (d * Math.PI) / 180;
const LERP = 0.14;

const scene = new THREE.Scene();
scene.background = new THREE.Color(0x0a0c10);

const camera = new THREE.OrthographicCamera(-1, 1, 1, -1, 0.1, 30);
camera.position.set(0, 3.6, 0.55);
camera.lookAt(0, 0, 0.55);

const renderer = new THREE.WebGLRenderer({ antialias: true });
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
view.appendChild(renderer.domElement);

const controls = new OrbitControls(camera, renderer.domElement);
controls.target.set(0, 0, 0.55);
controls.enableDamping = true;
controls.maxPolarAngle = Math.PI * 0.22;
controls.minPolarAngle = Math.PI * 0.05;

// World spins under robot (heading-up)
const mapGroup = new THREE.Group();
scene.add(mapGroup);

const floor = new THREE.Mesh(
  new THREE.PlaneGeometry(4.5, 4.5),
  new THREE.MeshBasicMaterial({ color: 0x0e1117 })
);
floor.rotation.x = -Math.PI / 2;
mapGroup.add(floor);

// HOME forward marker (grey)
const homeMarker = new THREE.Mesh(
  new THREE.ConeGeometry(0.08, 0.14, 3),
  new THREE.MeshBasicMaterial({ color: HOME_COL, transparent: true, opacity: 0.6 })
);
homeMarker.rotation.x = -Math.PI / 2;
homeMarker.position.set(0, 0.02, 1.1);
mapGroup.add(homeMarker);

const homeRing = new THREE.Mesh(
  new THREE.RingGeometry(0.1, 0.12, 32),
  new THREE.MeshBasicMaterial({ color: 0x64748b, transparent: true, opacity: 0.4, side: THREE.DoubleSide })
);
homeRing.rotation.x = -Math.PI / 2;
homeRing.position.set(0, 0.018, 1.1);
mapGroup.add(homeRing);

// Limit arc at ±max_yaw
const limitGroup = new THREE.Group();
mapGroup.add(limitGroup);

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

// Robot fixed on screen (nose = current forward)
const robot = new THREE.Group();
scene.add(robot);

const body = new THREE.Mesh(
  new THREE.CircleGeometry(ROBOT_RADIUS, 64),
  new THREE.MeshBasicMaterial({ color: ROBOT_COL })
);
body.rotation.x = -Math.PI / 2;
body.position.y = 0.014;
robot.add(body);

const nose = new THREE.Mesh(
  new THREE.ConeGeometry(0.055, 0.1, 3),
  new THREE.MeshBasicMaterial({ color: ENC_COL, transparent: true, opacity: 0.9 })
);
nose.rotation.x = -Math.PI / 2;
nose.position.set(0, 0.016, ROBOT_RADIUS + 0.045);
robot.add(nose);

const outline = new THREE.Mesh(
  new THREE.RingGeometry(ROBOT_RADIUS - 0.01, ROBOT_RADIUS + 0.01, 64),
  new THREE.MeshBasicMaterial({ color: 0x334155, transparent: true, opacity: 0.5, side: THREE.DoubleSide })
);
outline.rotation.x = -Math.PI / 2;
outline.position.y = 0.012;
robot.add(outline);

// IMU direction tick (orange) on robot rim
const imuTick = new THREE.Mesh(
  new THREE.BoxGeometry(0.04, 0.02, 0.14),
  new THREE.MeshBasicMaterial({ color: IMU_COL, transparent: true, opacity: 0.85 })
);
imuTick.position.set(0, 0.02, ROBOT_RADIUS - 0.02);
robot.add(imuTick);

let mapYawRad = 0;
let targetMapYawRad = 0;
let lastMaxYaw = 0;
let latest = null;

function fmtDeg(v) {
  const n = Math.round(Number(v) || 0);
  return `${n >= 0 ? '+' : ''}${n}°`;
}

function updateYawScene(data) {
  latest = data;
  const sign = Number(data.base_yaw_sign ?? -1);
  const enc = Number(data.map_yaw_deg ?? data.from_home_enc_deg ?? 0);
  const imu = Number(data.from_home_imu_deg ?? 0);
  const maxYaw = Number(data.max_yaw_deg ?? 120);

  targetMapYawRad = -deg(enc) * sign;
  mapYawRad += (targetMapYawRad - mapYawRad) * LERP;
  mapGroup.rotation.y = mapYawRad;

  imuTick.rotation.y = deg(imu) * sign;
  imuTick.visible = Boolean(data.imu_online);

  if (Math.abs(maxYaw - lastMaxYaw) > 0.5) {
    lastMaxYaw = maxYaw;
    updateLimitArc(maxYaw);
  }
}

window.updateYawScene = updateYawScene;

function resize() {
  const w = view.clientWidth;
  const h = view.clientHeight;
  if (!w || !h) return;
  const aspect = w / h;
  const frustum = 1.35;
  if (aspect > 1) {
    camera.left = -frustum * aspect;
    camera.right = frustum * aspect;
    camera.top = frustum;
    camera.bottom = -frustum;
  } else {
    camera.left = -frustum;
    camera.right = frustum;
    camera.top = frustum / aspect;
    camera.bottom = -frustum / aspect;
  }
  camera.updateProjectionMatrix();
  renderer.setSize(w, h, false);
}
window.addEventListener('resize', resize);
resize();

function animate() {
  requestAnimationFrame(animate);
  controls.update();
  if (latest) updateYawScene(latest);
  renderer.render(scene, camera);
}
animate();
