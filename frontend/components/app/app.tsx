"use client";

import { useEffect } from "react";
import { CleanKioskView } from "@/components/app/clean-kiosk-view";
import { Toaster } from "@/components/livekit/toaster";

export function App() {
  useEffect(() => {
    console.log("[App] Exhibition Clean Kiosk mode active");
  }, []);

  return (
    <>
      <main className="w-screen h-screen overflow-hidden">
        <CleanKioskView />
      </main>
      <Toaster />
    </>
  );
}
