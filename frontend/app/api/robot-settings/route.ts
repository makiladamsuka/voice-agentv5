import { NextResponse } from "next/server";
import fs from "fs";
import path from "path";

const configPath = path.resolve(process.cwd(), "../config.yaml");

function readBoolSetting(content: string, sectionHeader: string, keyName: string, defaultValue: boolean): boolean {
  const secIdx = content.indexOf(sectionHeader);
  if (secIdx === -1) return defaultValue;
  const section = content.substring(secIdx);
  // Match key within section up to next top-level key (line starting with no space followed by letter and :)
  const nextSectionMatch = section.substring(1).search(/\n[a-z_]+:/);
  const sectionBody = nextSectionMatch !== -1 ? section.substring(0, nextSectionMatch + 1) : section;
  const regex = new RegExp(`${keyName}:\\s*(true|false)`, "i");
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

  const regex = new RegExp(`(${keyName}:\\s*)(true|false)`, "i");
  if (regex.test(sectionBody)) {
    const updatedBody = sectionBody.replace(regex, `$1${value ? "true" : "false"}`);
    return before + updatedBody + after;
  }
  return content;
}

export async function GET() {
  try {
    if (!fs.existsSync(configPath)) {
      return NextResponse.json({
        faceGreeting: true,
        talkGestures: true,
        baseHomeSpin: true,
        faceTracking: true,
      });
    }
    const yaml = fs.readFileSync(configPath, "utf8");
    const faceGreeting = readBoolSetting(yaml, "face_greeting:", "enabled", true);
    const talkGestures = readBoolSetting(yaml, "talk_gesture:", "enabled", true);
    const baseHomeSpin = readBoolSetting(yaml, "base:", "spin_home_enabled", true);
    const faceTracking = readBoolSetting(yaml, "camera:", "roi_tracking_enabled", true);

    return NextResponse.json({
      faceGreeting,
      talkGestures,
      baseHomeSpin,
      faceTracking,
    });
  } catch (error) {
    return NextResponse.json(
      {
        faceGreeting: true,
        talkGestures: true,
        baseHomeSpin: true,
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
    let yaml = fs.readFileSync(configPath, "utf8");

    if (typeof body.faceGreeting === "boolean") {
      yaml = updateBoolSetting(yaml, "face_greeting:", "enabled", body.faceGreeting);
    }
    if (typeof body.talkGestures === "boolean") {
      yaml = updateBoolSetting(yaml, "talk_gesture:", "enabled", body.talkGestures);
    }
    if (typeof body.baseHomeSpin === "boolean") {
      yaml = updateBoolSetting(yaml, "base:", "spin_home_enabled", body.baseHomeSpin);
      yaml = updateBoolSetting(yaml, "base:", "home_on_shutdown", body.baseHomeSpin);
      yaml = updateBoolSetting(yaml, "proximity:", "clear_return_enabled", body.baseHomeSpin);
    }
    if (typeof body.faceTracking === "boolean") {
      yaml = updateBoolSetting(yaml, "camera:", "roi_tracking_enabled", body.faceTracking);
    }

    fs.writeFileSync(configPath, yaml, "utf8");
    return NextResponse.json({ success: true, updated: body });
  } catch (error) {
    return NextResponse.json({ success: false, error: String(error) }, { status: 500 });
  }
}
