import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { robotMapYawRad } from './yaw_map_pose.mjs';

const view = document.getElementById('view3d');
const ROBOT_COL = 0xc8d6e5;
const TORSO_COL = 0x141414;
const HEAD_COL = 0xf472b6;
const HEADING_COL = 0x111111;
const FRONT_MARK_COL = 0xffffff;
const HOME_COL = 0x94a3b8;
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
const deg = (d) => (d * Math.PI) / 180;

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

const scene = new THREE.Scene();
scene.background = new THREE.Color(0x0a0c10);

const camera = new THREE.OrthographicCamera(-1, 1, 1, -1, 0.1, 30);
camera.position.set(0, 3.9, 0.78);
camera.lookAt(0, 0.26, 0.55);

const renderer = new THREE.WebGLRenderer({ antialias: true });
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
view.appendChild(renderer.domElement);

const controls = new OrbitControls(camera, renderer.domElement);
controls.target.set(0, 0.26, 0.55);
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

// Base disc → black rounded torso → head (pan + pitch)
const robot = new THREE.Group();
scene.add(robot);

const baseDisc = new THREE.Mesh(
  new THREE.CylinderGeometry(ROBOT_RADIUS, ROBOT_RADIUS, BASE_THICKNESS, 64),
  new THREE.MeshBasicMaterial({ color: ROBOT_COL })
);
baseDisc.position.y = BASE_THICKNESS / 2;
robot.add(baseDisc);

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

// Collar where neck meets belly
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

// Eyes on the front face — placed from mesh bounds so they sit outside the bevel
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

let lastMaxYaw = 0;
let latest = null;

function updateYawScene(data) {
  latest = data;
  const panSign = Number(data.pan_yaw_sign ?? data.base_yaw_sign ?? -1);
  const tiltSign = Number(data.tilt_sign ?? 1);
  const headPan = Number(data.pan_cmd_from_home_deg ?? data.pan_from_home_deg ?? 0);
  const headPitch = Number(data.pitch_from_home_deg ?? 0);
  const maxYaw = Number(data.max_yaw_deg ?? 120);

  mapGroup.rotation.y = 0;
  robot.rotation.y = robotMapYawRad(data);
  panNode.rotation.y = deg(headPan) * panSign;
  tiltNode.rotation.x = deg(headPitch) * tiltSign;

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
