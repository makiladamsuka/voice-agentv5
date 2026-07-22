"use client";

import { AnimatePresence, motion } from "motion/react";
import {
  useTranscriptions,
  useVoiceAssistant,
} from "@livekit/components-react";
import { cn } from "@/lib/utils";
import { useEffect, useState } from "react";

const MotionMessage = motion.create("div");
const MotionWord = motion.create("span");

interface AgentLiveTranscriptionProps {
  className?: string;
  chatOpen?: boolean;
}

export function AgentLiveTranscription({
  className,
  chatOpen = false,
}: AgentLiveTranscriptionProps) {
  const transcriptions = useTranscriptions();
  const { state: agentState } = useVoiceAssistant();
  const [previousText, setPreviousText] = useState("");

  // Get the latest transcription
  const currentTranscription = transcriptions.slice(-1)[0];
  const text = currentTranscription?.text || "";

  // Detect speaker based on agent state
  // If agent is speaking, show green dot. Otherwise (listening/thinking), show red dot for user speech
  const isAgentSpeaking = agentState === "speaking";
  const isUser = !isAgentSpeaking && text.length > 0;

  // Split text into words for animation
  const words = text.split(" ").filter(Boolean);
  const previousWords = previousText.split(" ").filter(Boolean);

  useEffect(() => {
    if (text !== previousText) {
      setPreviousText(text);
    }
  }, [text, previousText]);

  return (
    <AnimatePresence>
      {text.length > 0 && (
        <MotionMessage
          initial={{ opacity: 0, y: chatOpen ? -20 : 0 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0, y: chatOpen ? -20 : 0 }}
          transition={{ duration: 0.2 }}
          className={cn(
            "pointer-events-none fixed z-50",
            chatOpen
              ? "left-4 right-4 top-4 md:left-6 md:right-6"
              : "left-1/2 top-1/2 max-w-4xl -translate-x-1/2 -translate-y-1/2",
            className,
          )}
        >
          <div className={cn("text-center", chatOpen && "text-left")}>
            <div
              className={cn(
                "mb-2 flex items-center gap-2",
                chatOpen ? "justify-start" : "justify-center",
              )}
            >
              <div
                className={cn(
                  "h-2 w-2 animate-pulse rounded-full",
                  isUser ? "bg-red-500" : "bg-green-500",
                )}
              />
            </div>
            <div
              className={cn(
                "font-medium text-gray-900 dark:text-gray-100",
                chatOpen ? "text-lg leading-snug" : "text-4xl leading-relaxed",
              )}
            >
              {words.map((word, index) => {
                const isNewWord =
                  index >= previousWords.length ||
                  word !== previousWords[index];

                return (
                  <MotionWord
                    key={`${word}-${index}`}
                    initial={
                      isNewWord
                        ? { scale: 1.05, opacity: 0.8 }
                        : { scale: 1, opacity: 1 }
                    }
                    animate={{ scale: 1, opacity: 1 }}
                    transition={{ duration: 0.15, ease: "easeOut" }}
                    className="inline-block mr-2"
                  >
                    {word}
                  </MotionWord>
                );
              })}
            </div>
          </div>
        </MotionMessage>
      )}
    </AnimatePresence>
  );
}
