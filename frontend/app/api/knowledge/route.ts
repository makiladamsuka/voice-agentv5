import { NextRequest, NextResponse } from "next/server";
import fs from "fs";
import path from "path";
import { execSync } from "child_process";

const backendDir = process.env.BACKEND_DIR
  ? path.resolve(process.env.BACKEND_DIR)
  : path.join(process.cwd(), "..");

const KB_PATH = path.join(backendDir, "voice", "knowledge_base.yaml");
const COMPILER_PATH = path.join(
  backendDir,
  "voice",
  "compiler",
  "knowledge_compiler.py",
);
const BACKEND_INTERNAL_URL =
  process.env.BACKEND_INTERNAL_URL || "http://127.0.0.1:8765";

// ── GET: read knowledge_base.yaml as raw text ──────────────────────────────

export async function GET() {
  try {
    if (!fs.existsSync(KB_PATH)) {
      return NextResponse.json({ yaml: "" });
    }
    const yaml = fs.readFileSync(KB_PATH, "utf-8");
    return NextResponse.json({ yaml });
  } catch (err: any) {
    return NextResponse.json(
      { error: err.message || "Failed to read knowledge base" },
      { status: 500 },
    );
  }
}

// ── POST: save YAML, run compiler, hot-reload NLU ──────────────────────────

export async function POST(req: NextRequest) {
  try {
    const body = await req.json();
    const { yaml } = body as { yaml: string };

    if (typeof yaml !== "string") {
      return NextResponse.json(
        { error: "Missing 'yaml' field in request body" },
        { status: 400 },
      );
    }

    // Save the YAML file
    fs.writeFileSync(KB_PATH, yaml, "utf-8");

    // Run the compiler
    let compilerOutput = "";
    try {
      compilerOutput = execSync(
        `python "${COMPILER_PATH}"`,
        { cwd: backendDir, timeout: 15000 },
      ).toString();
    } catch (compErr: any) {
      console.error("Compiler error:", compErr.stderr?.toString());
      return NextResponse.json(
        {
          error: "Compiler failed — check YAML syntax.",
          detail: compErr.stderr?.toString() || compErr.message,
        },
        { status: 422 },
      );
    }

    // Hot-reload the NLU runtime
    try {
      await fetch(`${BACKEND_INTERNAL_URL}/reload-nlu`, {
        method: "POST",
        signal: AbortSignal.timeout(3000),
      });
    } catch {
      // Non-fatal — robot will pick up changes on next restart
    }

    return NextResponse.json({
      success: true,
      message: "Knowledge base saved and compiled successfully.",
      compilerOutput,
    });
  } catch (err: any) {
    console.error("Knowledge API error:", err);
    return NextResponse.json(
      { error: err.message || "Server Error" },
      { status: 500 },
    );
  }
}
