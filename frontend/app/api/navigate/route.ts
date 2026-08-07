import { NextResponse } from "next/server";
import { execFile } from "child_process";
import path from "path";

export async function GET(request: Request) {
  const { searchParams } = new URL(request.url);
  const destination = searchParams.get("destination");
  const origin = searchParams.get("origin") || "You are here";

  if (!destination) {
    return NextResponse.json({ error: "destination query param required" }, { status: 400 });
  }

  const backendDir = process.env.BACKEND_DIR || path.join(process.cwd(), "..");

  // Inline Python script — imports Wayfinder and dumps the result as JSON
  const script = `
import sys, json, os

# Suppress stdout to prevent Wayfinder prints from breaking JSON parsing
original_stdout = sys.stdout
sys.stdout = open(os.devnull, 'w', encoding='utf-8')

sys.path.insert(0, ${JSON.stringify(backendDir)})
from voice.wayfinding import Wayfinder
wf = Wayfinder(${JSON.stringify(path.join(backendDir, "data"))})
result = wf.find_path(${JSON.stringify(destination)}, ${JSON.stringify(origin)})

# Restore stdout
sys.stdout = original_stdout

# strip waypoints to trim payload size
if result and "nodes" in result:
    result["nodes"] = [n for n in result["nodes"] if n.get("type") != "waypoint"]
print(json.dumps(result))
`.trim();

  // Try Linux venv paths first, then absolute system paths, then fallbacks
  const pythonCandidates = [
    path.join(backendDir, "venv", "bin", "python3"),
    path.join(backendDir, "venv", "bin", "python"),
    path.join(backendDir, "venv_310", "bin", "python3"),
    path.join(backendDir, "venv_312", "bin", "python3"),
    // Windows fallbacks
    path.join(backendDir, "venv_310", "Scripts", "python.exe"),
    path.join(backendDir, "venv_312", "Scripts", "python.exe"),
    path.join(backendDir, "venv",     "Scripts", "python.exe"),
    // Absolute system paths (Linux)
    "/usr/bin/python3",
    "/usr/local/bin/python3",
    "/usr/bin/python",
    // Bare names (may not work without shell PATH)
    "python3",
    "python",
  ];

  const errors: string[] = [];

  for (const python of pythonCandidates) {
    try {
      const result = await new Promise<string>((resolve, reject) => {
        execFile(python, ["-c", script], { timeout: 20_000, env: { ...process.env, PYTHONIOENCODING: "utf-8" } }, (err, stdout, stderr) => {
          if (err) return reject(new Error(`[${python}] ${stderr || err.message}`));
          if (!stdout.trim()) return reject(new Error(`[${python}] empty output`));
          resolve(stdout.trim());
        });
      });
      const data = JSON.parse(result);
      return NextResponse.json(data);
    } catch (e: any) {
      errors.push(e?.message || String(e));
      continue;
    }
  }

  console.error("Navigate API: all Python candidates failed:\n", errors.join("\n"));
  return NextResponse.json(
    { error: "Navigation failed. Could not run Python wayfinder.", details: errors.slice(-3) },
    { status: 500 },
  );
}
