"use client";

import { useEffect } from "react";
import { KioskView } from "@/components/app/kiosk-view";
import { Toaster } from "@/components/livekit/toaster";

export function App() {
  useEffect(() => {
    console.log("[App] NLU mode — running local ChromaDB voice pipeline");
  }, []);

  return (
    <>
      <main className="grid h-svh grid-cols-1 place-content-center">
        <KioskView />
      </main>
      <Toaster />
    </>
  );
}
