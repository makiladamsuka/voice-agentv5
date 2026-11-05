import { NextResponse } from "next/server";
import fs from "fs";
import path from "path";

function getConfigPaths(): string[] {
  const cwd = process.cwd();
  const rootDir = cwd.endsWith("frontend") ? path.resolve(cwd, "..") : cwd;
  const envConfig = process.env.CONFIG_PATH ? path.resolve(rootDir, process.env.CONFIG_PATH) : null;
  const kioskConfig = path.resolve(rootDir, "config.kiosk.yaml");
  const yamlConfig = path.resolve(rootDir, "config.yaml");

  const candidates = [envConfig, kioskConfig, yamlConfig].filter(Boolean) as string[];
  const existing = candidates.filter((p) => fs.existsSync(p));
  return Array.from(new Set(existing));
}

function readBoolSetting(content: string, sectionHeader: string, keyName: string, defaultValue: boolean): boolean {
  const secIdx = content.indexOf(sectionHeader);
  if (secIdx === -1) return defaultValue;
  const section = content.substring(secIdx);
  // Match key within section up to next top-level key (line starting with no space followed by letter and :)
  const nextSectionMatch = section.substring(1).search(/\n[a-z_]+:/);
  const sectionBody = nextSectionMatch !== -1 ? section.substring(0, nextSectionMatch + 1) : section;
  const regex = new RegExp(`^[ \t]*${keyName}:[ \t]*(true|false)`, "im");
  const match = sectionBody.match(regex);
  if (match) {
    return match[1].toLowerCase() === "true";
  }
  return defaultValue;
}

function updateBoolSetting(content: string, sectionHeader: string, keyName: string, value: boolean): string {
  const secIdx = content.indexOf(sectionHeader);
  if (secIdx === -1) return content;
  const before = content.substring(0, secIdx);
  const section = content.substring(secIdx);
  const nextSectionMatch = section.substring(1).search(/\n[a-z_]+:/);
  const sectionBody = nextSectionMatch !== -1 ? section.substring(0, nextSectionMatch + 1) : section;
  const after = nextSectionMatch !== -1 ? section.substring(nextSectionMatch + 1) : "";

  const regex = new RegExp(`(^[ \t]*${keyName}:[ \t]*)(true|false)`, "im");
  if (regex.test(sectionBody)) {
    const updatedBody = sectionBody.replace(regex, `$1${value ? "true" : "false"}`);
    return before + updatedBody + after;
  } else {
    // Key not found in section: insert it right below section header line
    const headerEnd = sectionBody.indexOf("\n");
    if (headerEnd !== -1) {
      const updatedBody =
        sectionBody.substring(0, headerEnd + 1) +
        `  ${keyName}: ${value ? "true" : "false"}\n` +
        sectionBody.substring(headerEnd + 1);
      return before + updatedBody + after;
    }
  }
  return content;
}

export async function GET() {
  try {
    const paths = getConfigPaths();
    if (paths.length === 0) {
      return NextResponse.json({
        faceGreeting: true,
        talkGestures: true,
        baseHomeSpin: true,
        baseRotation: true,
        faceTracking: true,
      });
    }

    // Read primary config file
    const primaryPath = paths[0];
    const yaml = fs.readFileSync(primaryPath, "utf8");

    const faceGreeting = readBoolSetting(yaml, "face_greeting:", "enabled", true);
    const talkGestures = readBoolSetting(yaml, "talk_gesture:", "enabled", true);
    // Base is considered on only if master base.enabled is true AND spin_home_enabled is not false
    const baseEnabled = readBoolSetting(yaml, "base:", "enabled", true);
    const spinHomeEnabled = readBoolSetting(yaml, "base:", "spin_home_enabled", true);
    const baseRotation = baseEnabled && spinHomeEnabled;
    const faceTracking = readBoolSetting(yaml, "camera:", "roi_tracking_enabled", true);

    return NextResponse.json({
      faceGreeting,
      talkGestures,
      baseHomeSpin: baseRotation,
      baseRotation,
      faceTracking,
    });
  } catch (error) {
    return NextResponse.json(
      {
        faceGreeting: true,
        talkGestures: true,
        baseHomeSpin: true,
        baseRotation: true,
        faceTracking: true,
        error: String(error),
      },
      { status: 500 }
    );
  }
}

export async function POST(req: Request) {
  try {
    const body = await req.json();
    const paths = getConfigPaths();

    if (paths.length === 0) {
      return NextResponse.json({ success: false, error: "No config files found" }, { status: 404 });
    }

    for (const p of paths) {
      let yaml = fs.readFileSync(p, "utf8");

      if (typeof body.faceGreeting === "boolean") {
        yaml = updateBoolSetting(yaml, "face_greeting:", "enabled", body.faceGreeting);
      }
      if (typeof body.talkGestures === "boolean") {
        yaml = updateBoolSetting(yaml, "talk_gesture:", "enabled", body.talkGestures);
      }
      const baseVal =
        typeof body.baseHomeSpin === "boolean"
          ? body.baseHomeSpin
          : typeof body.baseRotation === "boolean"
          ? body.baseRotation
          : undefined;

      if (typeof baseVal === "boolean") {
        yaml = updateBoolSetting(yaml, "base:", "enabled", baseVal);
        yaml = updateBoolSetting(yaml, "base:", "spin_home_enabled", baseVal);
        yaml = updateBoolSetting(yaml, "base:", "home_on_shutdown", baseVal);
        yaml = updateBoolSetting(yaml, "proximity:", "clear_return_enabled", baseVal);
        yaml = updateBoolSetting(yaml, "proximity:", "tof_turn_enabled", baseVal);
      }
      if (typeof body.faceTracking === "boolean") {
        yaml = updateBoolSetting(yaml, "camera:", "roi_tracking_enabled", body.faceTracking);
      }

      fs.writeFileSync(p, yaml, "utf8");
    }

    return NextResponse.json({ success: true, updated: body, modifiedFiles: paths });
  } catch (error) {
    return NextResponse.json({ success: false, error: String(error) }, { status: 500 });
  }
}
