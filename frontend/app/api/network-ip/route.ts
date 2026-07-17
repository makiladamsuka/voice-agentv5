import { NextResponse } from "next/server";
import os from "os";

export async function GET() {
  const interfaces = os.networkInterfaces();
  let localIp = "127.0.0.1";

  for (const name of Object.keys(interfaces)) {
    const iface = interfaces[name];
    if (!iface) continue;

    for (const alias of iface) {
      if (alias.family === "IPv4" && !alias.internal) {
        localIp = alias.address;
        break;
      }
    }
  }

  return NextResponse.json({ ip: localIp });
}
