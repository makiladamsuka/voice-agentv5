import { headers } from "next/headers";
import { App } from "@/components/app/app";
import { getAppConfig } from "@/lib/utils";

export default async function VoicePage() {
  const hdrs = await headers();
  const appConfig = await getAppConfig(hdrs);

  return <App appConfig={appConfig} viewMode="voice-lite" />;
}
