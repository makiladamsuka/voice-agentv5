"use client";

import type { AppConfig } from "@/app-config";
import { KioskView } from "@/components/app/kiosk-view";
import { VoiceLiteView } from "@/components/app/voice-lite-view";

export type AppViewMode = "kiosk" | "voice-lite";

interface ViewControllerProps {
  appConfig: AppConfig;
  viewMode?: AppViewMode;
}

export function ViewController({
  appConfig: _appConfig,
  viewMode = "kiosk",
}: ViewControllerProps) {
  if (viewMode === "voice-lite") {
    return <VoiceLiteView />;
  }
  return <KioskView />;
}
