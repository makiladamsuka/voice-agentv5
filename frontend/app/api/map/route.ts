import { NextResponse } from "next/server";
import fs from "fs/promises";
import path from "path";

export async function POST(request: Request) {
  try {
    const data = await request.json();
    const floor = new URL(request.url).searchParams.get("floor") || "default";

    // Determine path dynamically based on project structure
    const backendDir =
      process.env.BACKEND_DIR || path.join(process.cwd(), "..");
    const dataDir = path.join(backendDir, "data");

    // Create data directory if it doesn't exist
    try {
      await fs.access(dataDir);
    } catch {
      await fs.mkdir(dataDir, { recursive: true });
    }

    const filePath = path.join(dataDir, `map_graph_${floor}.json`);
    await fs.writeFile(filePath, JSON.stringify(data, null, 2));

    return NextResponse.json({ success: true });
  } catch (error) {
    console.error("Error saving map data:", error);
    return NextResponse.json(
      { error: "Failed to save map data" },
      { status: 500 },
    );
  }
}

export async function GET(request: Request) {
  try {
    const floor = new URL(request.url).searchParams.get("floor") || "default";
    const backendDir =
      process.env.BACKEND_DIR || path.join(process.cwd(), "..");
    const filePath = path.join(backendDir, "data", `map_graph_${floor}.json`);

    const fileContents = await fs.readFile(filePath, "utf-8");
    return NextResponse.json(JSON.parse(fileContents));
  } catch (error) {
    // Return empty graph if no file exists
    return NextResponse.json({
      nodes: [],
      edges: [],
      buildings: {
        building_1: {
          position: [-6, 0, 0],
          size: [10, 10],
          color: "#ffffff",
          name: "Building 1",
        },
        building_2: {
          position: [6, 0, -2],
          size: [10, 10],
          color: "#a5f3fc",
          name: "Building 2",
        },
      },
    });
  }
}
