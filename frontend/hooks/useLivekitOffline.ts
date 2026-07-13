import { useState, useEffect, useCallback, useRef } from 'react';

// Mock Track to satisfy the type
export const Track = {
  Source: {
    Microphone: 'microphone'
  }
};

export function useLivekitOffline() {
  const [bbState, setBbState] = useState<any>({
    conv_state: "idle",
    user_text: "",
    agent_text: "",
    current_action: {}
  });

  // Poll blackboard state
  useEffect(() => {
    let mounted = true;
    const poll = async () => {
      try {
        let host = typeof window !== 'undefined' ? window.location.hostname : '127.0.0.1';
        if (host === 'localhost') host = '127.0.0.1';
        const res = await fetch(`http://${host}:8080/api/blackboard-state`);
        if (res.ok && mounted) {
          const data = await res.json();
          setBbState(data);
        }
      } catch (e) {
        // ignore
      }
      if (mounted) setTimeout(poll, 100);
    };
    poll();
    return () => { mounted = false; };
  }, []);

  return bbState;
}

export function useSessionContext() {
  const bb = useLivekitOffline();
  const [isConnected, setIsConnected] = useState(false);

  useEffect(() => {
    if (bb.conv_state === "thinking" || bb.conv_state === "speaking") {
      setIsConnected(true);
    } else {
      // Revert to standby/inactive after 8 seconds of inactivity
      const timer = setTimeout(() => {
        setIsConnected(false);
      }, 8000);
      return () => clearTimeout(timer);
    }
  }, [bb.conv_state]);

  const start = useCallback(async () => {
    setIsConnected(true);
  }, []);

  const end = useCallback(async () => {
    setIsConnected(false);
  }, []);

  return {
    isConnected,
    start,
    end,
  };
}

export function useSessionMessages(session: any) {
  const bb = useLivekitOffline();
  const [messages, setMessages] = useState<any[]>([]);

  useEffect(() => {
    const list = [];
    if (bb.user_text) {
      list.push({ 
        id: 'user-' + bb.user_text, 
        from: { isLocal: true }, 
        message: bb.user_text,
        timestamp: Date.now() - 500
      });
    }
    if (bb.agent_text) {
      list.push({ 
        id: 'agent-' + bb.agent_text, 
        from: { isLocal: false }, 
        message: bb.agent_text,
        timestamp: Date.now()
      });
    }
    setMessages(list);
  }, [bb.user_text, bb.agent_text]);

  return { messages };
}

export function useTranscriptions() {
  const bb = useLivekitOffline();
  if (bb.user_text) {
    return [{ text: bb.user_text }];
  }
  return [];
}

export function useTracks(sources: any[]) {
  // Return a dummy local mic track
  return [{ participant: { isLocal: true } }];
}

export function useTrackVolume(track: any) {
  const bb = useLivekitOffline();
  // Fake volume if speaking to trigger UI pulses
  if (bb.conv_state === "speaking") return 0.5;
  if (bb.conv_state === "listening") return 0.2;
  return 0;
}

export function useVoiceAssistant() {
  const bb = useLivekitOffline();
  return {
    audioTrack: {},
    state: bb.conv_state // "idle", "listening", "speaking", "thinking"
  };
}

export function useRoomContext() {
  return {
    localParticipant: {
      publishData: (data: Uint8Array) => {
        try {
          const payload = JSON.parse(new TextDecoder().decode(data));
          if (payload.type === "change_eye_color") {
            let host = typeof window !== 'undefined' ? window.location.hostname : '127.0.0.1';
            if (host === 'localhost') host = '127.0.0.1';
            fetch(`http://${host}:8080/api/eye-color`, {
              method: "POST",
              body: JSON.stringify({ theme: payload.color })
            });
          }
        } catch (e) {}
      }
    },
    on: (event: string, callback: any) => {},
    off: (event: string, callback: any) => {}
  };
}
