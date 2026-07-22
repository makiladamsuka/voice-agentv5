import { NextRequest, NextResponse } from "next/server";
import fs from "fs";
import path from "path";

export async function POST(req: NextRequest) {
  try {
    const formData = await req.formData();
    const file = formData.get("poster") as File | null;
    const category = (formData.get("category") as string) || "posts";

    if (!file) {
      return NextResponse.json(
        { error: "No poster file provided." },
        { status: 400 },
      );
    }

    const buffer = Buffer.from(await file.arrayBuffer());
    // Use a unique name to avoid collisions
    const fileName = `${Date.now()}_${file.name.replace(/[^a-zA-Z0-9.]/g, "_")}`;

    // Determine the absolute path to backend/assets/{category}
    const backendDir = process.env.BACKEND_DIR
      ? path.resolve(process.env.BACKEND_DIR)
      : path.join(process.cwd(), "..");
    const targetDir = path.join(backendDir, "assets", category);

    // Ensure the directory exists
    if (!fs.existsSync(targetDir)) {
      fs.mkdirSync(targetDir, { recursive: true });
    }

    const filePath = path.join(targetDir, fileName);
    fs.writeFileSync(filePath, buffer);

    // Call the python image server to trigger re-index
    try {
      const backendUrl =
        process.env.BACKEND_INTERNAL_URL || "http://127.0.0.1:8080";
      const response = await fetch(`${backendUrl}/trigger-index`, {
        method: "POST",
      });
      if (!response.ok) {
        console.error(
          "Failed to trigger index in backend:",
          response.statusText,
        );
      } else {
        console.log("Successfully triggered event re-index.");
      }
    } catch (e) {
      console.error("Error reaching backend to trigger index:", e);
    }

    return NextResponse.json({
      success: true,
      message: "Poster uploaded successfully.",
      fileName,
    });
  } catch (error: any) {
    console.error("Error uploading poster:", error);
    return NextResponse.json(
      { error: error.message || "Server Error" },
      { status: 500 },
    );
  }
}
