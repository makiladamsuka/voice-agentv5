"use client";

import { useEffect, useMemo } from "react";
import { LogLevel, TokenSource, setLogLevel } from "livekit-client";
import {
  SessionProvider,
  StartAudio,
  useSession,
} from "@livekit/components-react";
import type { AppConfig } from "@/app-config";
import { KioskView } from "@/components/app/kiosk-view";
import { VoiceAudioOutput } from "@/components/app/voice-audio-output";
import { Toaster } from "@/components/livekit/toaster";
import { useAgentErrors } from "@/hooks/useAgentErrors";
import { useDebugMode } from "@/hooks/useDebug";
import { getSandboxTokenSource } from "@/lib/utils";

const IN_DEVELOPMENT = process.env.NODE_ENV !== "production";

function AppSetup() {
  useDebugMode({ enabled: IN_DEVELOPMENT });
  useAgentErrors();

  useEffect(() => {
    const debug =
      process.env.NEXT_PUBLIC_LIVEKIT_DEBUG === "1" ||
      process.env.NEXT_PUBLIC_LIVEKIT_DEBUG === "true";
    if (debug) {
      setLogLevel(LogLevel.debug);
    }
  }, []);

  return null;
}

interface AppProps {
  appConfig: AppConfig;
}

export function App({ appConfig }: AppProps) {
  const tokenSource = useMemo(() => {
    return typeof process.env.NEXT_PUBLIC_CONN_DETAILS_ENDPOINT === "string"
      ? getSandboxTokenSource(appConfig)
      : TokenSource.endpoint("/api/connection-details");
  }, [appConfig]);

  // Pi kiosk agent can take well over the LiveKit default (20s) to join +
  // finish initializing while CPU is busy — avoid marking failed too early.
  const session = useSession(tokenSource, {
    ...(appConfig.agentName ? { agentName: appConfig.agentName } : {}),
    agentConnectTimeoutMilliseconds: 90_000,
  });

  return (
    <SessionProvider session={session}>
      <AppSetup />
      <main className="grid h-svh grid-cols-1 place-content-center">
        <KioskView />
      </main>
      <StartAudio label="Start Audio" />
      <VoiceAudioOutput />
      <Toaster />
    </SessionProvider>
  );
}
