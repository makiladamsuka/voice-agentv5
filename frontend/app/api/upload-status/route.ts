import { NextResponse } from "next/server";
import fs from "fs";
import path from "path";

export const dynamic = "force-dynamic";

export async function GET() {
  try {
    const backendDir = process.env.BACKEND_DIR
      ? path.resolve(process.env.BACKEND_DIR)
      : path.join(process.cwd(), "..");
    const assetsDir = path.join(backendDir, "assets");
    const categories = ["events", "competitions", "posts"];
    let latestTime = 0;
    let latestFileUrl = "";
    let latestCategory = "";

    const allFiles: any[] = [];

    const extractedEventsPath = path.join(
      backendDir,
      "voice",
      "event_db",
      "extracted_events.json",
    );
    let extractedEvents: any[] = [];
    if (fs.existsSync(extractedEventsPath)) {
      try {
        extractedEvents = JSON.parse(
          fs.readFileSync(extractedEventsPath, "utf8"),
        );
      } catch (e) {}
    }

    for (const category of categories) {
      const categoryDir = path.join(assetsDir, category);
      if (!fs.existsSync(categoryDir)) continue;

      const files = fs.readdirSync(categoryDir);
      for (const file of files) {
        if (!file.match(/\.(jpg|jpeg|png|webp)$/i)) continue;
        const stats = fs.statSync(path.join(categoryDir, file));
        const fileUrl = `/api/image?path=${category}/${file}`;

        const extractedData = extractedEvents.find(
          (e: any) => e.source_file === file,
        );

        allFiles.push({
          url: fileUrl,
          category,
          mtimeMs: stats.mtimeMs,
          name: file,
          extracted: extractedData || null,
        });

        if (stats.mtimeMs > latestTime) {
          latestTime = stats.mtimeMs;
          // Return the relative path, the frontend will append the correct hostname and port 8080
          latestFileUrl = fileUrl;
          latestCategory = category;
        }
      }
    }

    allFiles.sort((a, b) => b.mtimeMs - a.mtimeMs);

    return NextResponse.json({
      lastUpload: latestTime,
      latestFileUrl,
      latestCategory,
      allFiles,
    });
  } catch (error) {
    return NextResponse.json({ lastUpload: 0 });
  }
}
