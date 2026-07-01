import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';

const view = document.getElementById('view3d');
const ROBOT_COL = 0xc8d6e5;
const HEAD_COL = 0xf472b6;
const HEADING_COL = 0x111111;
const HOME_COL = 0x94a3b8;
const ROBOT_RADIUS = 0.275;
const BASE_THICKNESS = 0.07;
const HEAD_RADIUS = 0.15;
const HEAD_THICKNESS = 0.045;
const deg = (d) => (d * Math.PI) / 180;

const scene = new THREE.Scene();
scene.background = new THREE.Color(0x0a0c10);

const camera = new THREE.OrthographicCamera(-1, 1, 1, -1, 0.1, 30);
camera.position.set(0, 3.6, 0.62);
camera.lookAt(0, 0.05, 0.55);

const renderer = new THREE.WebGLRenderer({ antialias: true });
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
view.appendChild(renderer.domElement);

const controls = new OrbitControls(camera, renderer.domElement);
controls.target.set(0, 0.05, 0.55);
controls.enableDamping = true;
controls.maxPolarAngle = Math.PI * 0.22;
controls.minPolarAngle = Math.PI * 0.05;

// Fixed world reference — HOME markers stay put
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

// Grey base = pan-compensated IMU base yaw; pink head = mechanical pan from HOME
const robot = new THREE.Group();
scene.add(robot);

const body = new THREE.Mesh(
  new THREE.CylinderGeometry(ROBOT_RADIUS, ROBOT_RADIUS, BASE_THICKNESS, 64),
  new THREE.MeshBasicMaterial({ color: ROBOT_COL })
);
body.position.y = BASE_THICKNESS / 2;
robot.add(body);

const headingStrip = new THREE.Mesh(
  new THREE.BoxGeometry(0.05, 0.02, 0.24),
  new THREE.MeshBasicMaterial({ color: HEADING_COL })
);
headingStrip.position.set(0, BASE_THICKNESS + 0.01, ROBOT_RADIUS * 0.42);
robot.add(headingStrip);

const outline = new THREE.Mesh(
  new THREE.RingGeometry(ROBOT_RADIUS - 0.01, ROBOT_RADIUS + 0.01, 64),
  new THREE.MeshBasicMaterial({ color: 0x334155, transparent: true, opacity: 0.5, side: THREE.DoubleSide })
);
outline.rotation.x = -Math.PI / 2;
outline.position.y = BASE_THICKNESS + 0.006;
robot.add(outline);

const head = new THREE.Group();
head.position.y = BASE_THICKNESS;
robot.add(head);

const headBody = new THREE.Mesh(
  new THREE.CylinderGeometry(HEAD_RADIUS, HEAD_RADIUS, HEAD_THICKNESS, 48),
  new THREE.MeshBasicMaterial({ color: HEAD_COL })
);
headBody.position.y = HEAD_THICKNESS / 2;
head.add(headBody);

const headStrip = new THREE.Mesh(
  new THREE.BoxGeometry(0.035, 0.016, 0.12),
  new THREE.MeshBasicMaterial({ color: HEADING_COL })
);
headStrip.position.set(0, HEAD_THICKNESS + 0.008, HEAD_RADIUS * 0.42);
head.add(headStrip);

const headOutline = new THREE.Mesh(
  new THREE.RingGeometry(HEAD_RADIUS - 0.008, HEAD_RADIUS + 0.008, 48),
  new THREE.MeshBasicMaterial({ color: 0xbe185d, transparent: true, opacity: 0.45, side: THREE.DoubleSide })
);
headOutline.rotation.x = -Math.PI / 2;
headOutline.position.y = HEAD_THICKNESS + 0.004;
head.add(headOutline);

let lastMaxYaw = 0;
let latest = null;

function updateYawScene(data) {
  latest = data;
  const baseSign = Number(data.base_yaw_sign ?? -1);
  const panSign = Number(data.pan_yaw_sign ?? data.base_yaw_sign ?? -1);
  const baseYaw = Number(data.from_home_imu_deg ?? data.map_yaw_deg ?? 0);
  const headPan = Number(data.pan_from_home_deg ?? 0);
  const maxYaw = Number(data.max_yaw_deg ?? 120);

  mapGroup.rotation.y = 0;
  robot.rotation.y = deg(baseYaw) * baseSign;
  head.rotation.y = deg(headPan) * panSign;

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
