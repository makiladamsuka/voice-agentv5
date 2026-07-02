"use client";

import type { AppConfig } from "@/app-config";
import { KioskView } from "@/components/app/kiosk-view";

interface ViewControllerProps {
  appConfig: AppConfig;
}

export function ViewController({ appConfig }: ViewControllerProps) {
  return <KioskView />;
}
