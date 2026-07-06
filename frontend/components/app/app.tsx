"use client";

import { useMemo } from "react";
import { TokenSource } from "livekit-client";
import {
  SessionProvider,
  StartAudio,
  useSession,
} from "@livekit/components-react";
import type { AppConfig } from "@/app-config";
import { VoiceAudioOutput } from "@/components/app/voice-audio-output";
import {
  ViewController,
  type AppViewMode,
} from "@/components/app/view-controller";
import { Toaster } from "@/components/livekit/toaster";
import { useAgentErrors } from "@/hooks/useAgentErrors";
import { useDebugMode } from "@/hooks/useDebug";
import { getSandboxTokenSource } from "@/lib/utils";

const IN_DEVELOPMENT = process.env.NODE_ENV !== "production";

function AppSetup() {
  useDebugMode({ enabled: IN_DEVELOPMENT });
  useAgentErrors();

  return null;
}

interface AppProps {
  appConfig: AppConfig;
  viewMode?: AppViewMode;
}

export function App({ appConfig, viewMode = "kiosk" }: AppProps) {
  const tokenSource = useMemo(() => {
    return typeof process.env.NEXT_PUBLIC_CONN_DETAILS_ENDPOINT === "string"
      ? getSandboxTokenSource(appConfig)
      : TokenSource.endpoint("/api/connection-details");
  }, [appConfig]);

  const session = useSession(
    tokenSource,
    appConfig.agentName ? { agentName: appConfig.agentName } : undefined,
  );

  return (
    <SessionProvider session={session}>
      <AppSetup />
      <main className="grid h-svh grid-cols-1 place-content-center">
        <ViewController appConfig={appConfig} viewMode={viewMode} />
      </main>
      <StartAudio label="Start Audio" />
      <VoiceAudioOutput />
      <Toaster />
    </SessionProvider>
  );
}
