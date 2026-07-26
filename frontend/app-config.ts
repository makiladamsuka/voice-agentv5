export interface AppConfig {
  pageTitle: string;
  pageDescription: string;
  companyName: string;

  supportsChatInput: boolean;
  supportsVideoInput: boolean;
  supportsScreenShare: boolean;
  isPreConnectBufferEnabled: boolean;

  logo: string;
  startButtonText: string;
  accent?: string;
  logoDark?: string;
  accentDark?: string;

  // agent dispatch configuration
  agentName?: string;
}

export const APP_CONFIG_DEFAULTS: AppConfig = {
  companyName: "NEma Kiosk",
  pageTitle: "NEma Campus Kiosk",
  pageDescription: "NEma Campus Kiosk & Local NLU Voice Assistant",

  supportsChatInput: true,
  supportsVideoInput: false,
  supportsScreenShare: false,
  isPreConnectBufferEnabled: false,

  logo: "/lk-logo.svg",
  accent: "#002cf2",
  logoDark: "/lk-logo-dark.svg",
  accentDark: "#1fd5f9",
  startButtonText: "Start Interaction",

  agentName:
    process.env.AGENT_NAME ??
    process.env.LIVEKIT_AGENT_NAME ??
    "campus-greeting-agent",
};
