import { NextResponse } from "next/server";
import fs from "fs";
import path from "path";

// config.yaml is in the project root, one level up from frontend
const configPath = path.resolve(process.cwd(), "../config.yaml");

export async function GET() {
  try {
    const yamlContent = fs.readFileSync(configPath, "utf8");
    const faceGreetingIndex = yamlContent.indexOf("face_greeting:");
    if (faceGreetingIndex !== -1) {
      const section = yamlContent.substring(faceGreetingIndex);
      const enabledMatch = section.match(/enabled:\s*(true|false)/);
      if (enabledMatch) {
        return NextResponse.json({ enabled: enabledMatch[1] === "true" });
      }
    }
    return NextResponse.json({ enabled: true }); // Default fallback
  } catch (error) {
    return NextResponse.json({ enabled: true });
  }
}

export async function POST(req: Request) {
  try {
    const { enabled } = await req.json();
    let yamlContent = fs.readFileSync(configPath, "utf8");
    const newVal = enabled ? "true" : "false";
    
    const faceGreetingIndex = yamlContent.indexOf("face_greeting:");
    if (faceGreetingIndex !== -1) {
      const section = yamlContent.substring(faceGreetingIndex);
      const enabledMatch = section.match(/enabled:\s*(true|false)/);
      if (enabledMatch) {
         const oldEnabledStr = enabledMatch[0];
         const newEnabledStr = `enabled: ${newVal}`;
         const before = yamlContent.substring(0, faceGreetingIndex);
         const after = section.replace(oldEnabledStr, newEnabledStr);
         yamlContent = before + after;
         fs.writeFileSync(configPath, yamlContent, "utf8");
      }
    }
    return NextResponse.json({ success: true, enabled });
  } catch (error) {
    return NextResponse.json({ success: false, error: String(error) }, { status: 500 });
  }
}
