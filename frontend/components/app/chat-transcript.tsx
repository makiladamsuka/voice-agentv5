"use client";

import { ChatEntry } from "@/components/livekit/chat-entry";

interface ChatTranscriptProps {
  hidden?: boolean;
  messages?: any[];
  transcriptions?: any[];
  className?: string;
  stagingText?: string;
  isLoading?: boolean;
}

export function ChatTranscript({
  hidden = false,
  messages = [],
  transcriptions = [],
  className,
  stagingText = "",
  isLoading = false,
  ...props
}: ChatTranscriptProps & React.HTMLAttributes<HTMLDivElement>) {
  // Combine messages — support LiveKit shape AND NLU adapter shape
  // LiveKit: { message|text, from.isLocal }
  // NLU:     { content, role: "user"|"assistant" }
  const rawItems = messages
    .map((m: any) => {
      const text = m.message || m.text || m.content || "";
      const isLocal =
        m.from?.isLocal === true ||
        m.role === "user" ||
        m.participantIdentity === "user";
      return {
        id: m.id || String(m.timestamp),
        timestamp: m.timestamp ?? Date.now(),
        message: text,
        isLocal: Boolean(isLocal),
        isFinal: true,
      };
    })
    .sort((a: any, b: any) => a.timestamp - b.timestamp);

  // Deduplicate progressive transcriptions and instant messages
  const combinedItems = rawItems.reduce((acc: any[], current: any) => {
    if (!current.message || current.message.trim() === "") return acc;

    const existingIndex = acc.findIndex(
      (item) =>
        item.isLocal === current.isLocal &&
        (item.message.includes(current.message) ||
          current.message.includes(item.message)) &&
        Math.abs(item.timestamp - current.timestamp) < 10000, // within 10 seconds
    );

    if (existingIndex >= 0) {
      // Keep the longer (more complete) message
      if (current.message.length > acc[existingIndex].message.length) {
        acc[existingIndex] = { ...current, id: acc[existingIndex].id }; // preserve original ID to avoid React re-mounting
      }
    } else {
      acc.push(current);
    }
    return acc;
  }, []);

  // Re-sort after deduplication since replacing an item can alter its timestamp
  combinedItems.sort((a, b) => a.timestamp - b.timestamp);

  if (hidden) return null;

  return (
    <div className={`flex flex-col gap-4 pb-4 ${className || ""}`} {...props}>
      {combinedItems.map((item) => {
        if (!item.message) return null;

        // Hide the item from chat if it's currently being spoken in the staging area!
        if (
          stagingText &&
          (item.message.includes(stagingText) ||
            stagingText.includes(item.message))
        ) {
          return null;
        }

        const locale = navigator?.language ?? "en-US";
        const messageOrigin = item.isLocal ? "local" : "remote";

        return (
          <ChatEntry
            key={item.id}
            locale={locale}
            timestamp={item.timestamp}
            message={item.message}
            messageOrigin={messageOrigin}
            hasBeenEdited={false}
          />
        );
      })}
    </div>
  );
}
