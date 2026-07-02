import { useEffect } from "react";
import { useAgent, useSessionContext } from "@livekit/components-react";

export function useAgentErrors() {
  const agent = useAgent();
  const { isConnected, end } = useSessionContext();

  useEffect(() => {
    if (isConnected && agent.state === "failed") {


      end();
    }
  }, [agent, isConnected, end]);
}
