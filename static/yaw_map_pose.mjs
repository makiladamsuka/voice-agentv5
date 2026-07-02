/** Shared fixed-world map pose: HOME cone at +Z, robot rotates from HOME. */

export function degToRad(d) {
  return (Number(d) || 0) * Math.PI / 180;
}

/**
 * Degrees from HOME for map robot heading.
 * Approach publishes viz_base_yaw_deg (encoder); yaw test uses IMU only.
 */
export function mapBaseYawDeg(data) {
  const imuYaw = Number(data.from_home_imu_deg ?? data.body_yaw_deg ?? 0);
  const vizYaw = data.viz_base_yaw_deg;
  if (vizYaw !== undefined && vizYaw !== null && Number.isFinite(Number(vizYaw))) {
    return Number(vizYaw);
  }
  return imuYaw;
}

/** Robot map heading in radians for robot.rotation.y. */
export function robotMapYawRad(data) {
  const baseSign = Number(data.base_yaw_sign ?? -1);
  return -degToRad(mapBaseYawDeg(data)) * baseSign;
}

export function bodyLocalToWorld(x, z, yawRad) {
  const c = Math.cos(yawRad);
  const s = Math.sin(yawRad);
  return {
    x: x * c + z * s,
    z: -x * s + z * c,
  };
}

export function bodyMmToWorld(xMm, zMm, yawRad) {
  return bodyLocalToWorld(xMm / 1000, zMm / 1000, yawRad);
}
