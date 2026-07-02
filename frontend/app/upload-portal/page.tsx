"use client";

import React, { useState, useRef } from "react";
import { UploadCloud, CheckCircle2, AlertCircle, Loader2 } from "lucide-react";

export default function UploadPortalPage() {
  const [file, setFile] = useState<File | null>(null);
  const [preview, setPreview] = useState<string | null>(null);
  const [status, setStatus] = useState<
    "idle" | "uploading" | "success" | "error"
  >("idle");
  const [category, setCategory] = useState<"events" | "competitions" | "posts">(
    "posts",
  );
  const [errorMessage, setErrorMessage] = useState("");
  const fileInputRef = useRef<HTMLInputElement>(null);

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (e.target.files && e.target.files.length > 0) {
      const selectedFile = e.target.files[0];
      if (!selectedFile.type.startsWith("image/")) {
        setStatus("error");
        setErrorMessage("Please select an image file (JPG, PNG).");
        return;
      }
      setFile(selectedFile);
      setPreview(URL.createObjectURL(selectedFile));
      setStatus("idle");
    }
  };

  const handleUpload = async () => {
    if (!file) return;

    setStatus("uploading");
    const formData = new FormData();
    formData.append("poster", file);
    formData.append("category", category);

    try {
      const res = await fetch("/api/upload-poster", {
        method: "POST",
        body: formData,
      });

      if (!res.ok) {
        const data = await res.json();
        throw new Error(data.error || "Upload failed");
      }

      setStatus("success");
    } catch (err: any) {
      console.error(err);
      setStatus("error");
      setErrorMessage(err.message || "Something went wrong.");
    }
  };

  return (
    <div className="min-h-screen bg-surface-container flex flex-col items-center justify-center p-6 text-on-surface">
      <div className="max-w-md w-full bg-surface rounded-3xl shadow-lg p-8 space-y-6">
        <div className="text-center">
          <h1 className="text-2xl font-bold mb-2">Upload Event Poster</h1>
          <p className="text-on-surface-variant text-sm">
            Upload an image of the event poster. Our AI will extract the details
            and add them to the kiosk.
          </p>
        </div>

        {status === "success" ? (
          <div className="flex flex-col items-center justify-center py-8 space-y-4 text-center">
            <CheckCircle2 className="w-16 h-16 text-green-500" />
            <h2 className="text-xl font-bold">Upload Successful!</h2>
            <p className="text-on-surface-variant">
              The poster is being processed. The voice agent will be updated
              shortly!
            </p>
            <button
              onClick={() => {
                setFile(null);
                setPreview(null);
                setStatus("idle");
              }}
              className="mt-4 px-6 py-2 bg-primary text-on-primary rounded-full font-medium"
            >
              Upload Another
            </button>
          </div>
        ) : (
          <div className="space-y-6">
            <input
              type="file"
              accept="image/*"
              className="hidden"
              ref={fileInputRef}
              onChange={handleFileChange}
            />

            {!file ? (
              <div
                onClick={() => fileInputRef.current?.click()}
                className="border-2 border-dashed border-primary/40 rounded-2xl p-8 flex flex-col items-center justify-center cursor-pointer hover:bg-primary/5 transition-colors"
              >
                <UploadCloud className="w-12 h-12 text-primary mb-3" />
                <p className="font-medium">Tap to select an image</p>
                <p className="text-xs text-on-surface-variant mt-1">
                  Supports JPG, PNG
                </p>
              </div>
            ) : (
              <div className="space-y-4">
                <div className="relative rounded-2xl overflow-hidden shadow-md">
                  {/* eslint-disable-next-line @next/next/no-img-element */}
                  <img
                    src={preview!}
                    alt="Preview"
                    className="w-full h-auto max-h-[300px] object-cover"
                  />
                  <button
                    onClick={() => {
                      setFile(null);
                      setPreview(null);
                    }}
                    className="absolute top-2 right-2 bg-black/60 text-white rounded-full px-3 py-1 text-xs font-bold"
                  >
                    Change
                  </button>
                </div>

                <div className="flex gap-2 p-1 bg-surface-variant/50 rounded-xl mt-4">
                  {(["events", "competitions", "posts"] as const).map((cat) => (
                    <button
                      key={cat}
                      onClick={() => setCategory(cat)}
                      className={`flex-1 py-3 px-2 text-sm font-bold rounded-lg capitalize transition-colors ${
                        category === cat
                          ? "bg-blue-600 text-white shadow-md border border-blue-500"
                          : "text-on-surface hover:bg-surface-variant bg-transparent border border-outline-variant/50"
                      }`}
                    >
                      {cat.slice(0, -1)}
                    </button>
                  ))}
                </div>
              </div>
            )}

            {status === "error" && (
              <div className="bg-red-500/10 text-red-600 p-3 rounded-lg flex items-start gap-2 text-sm">
                <AlertCircle className="w-5 h-5 shrink-0" />
                <p>{errorMessage}</p>
              </div>
            )}

            <button
              onClick={handleUpload}
              disabled={!file || status === "uploading"}
              className={`w-full py-3.5 rounded-full font-bold flex items-center justify-center gap-2 transition-all ${
                !file || status === "uploading"
                  ? "bg-surface-variant text-on-surface-variant cursor-not-allowed"
                  : "bg-primary text-on-primary shadow-md hover:shadow-lg hover:scale-[1.02]"
              }`}
            >
              {status === "uploading" ? (
                <>
                  <Loader2 className="w-5 h-5 animate-spin" />
                  Uploading...
                </>
              ) : (
                "Upload Poster"
              )}
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
