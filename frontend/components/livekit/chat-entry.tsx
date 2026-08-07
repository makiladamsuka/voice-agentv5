import * as React from "react";
import { cn } from "@/lib/utils";

export interface ChatEntryProps extends React.HTMLAttributes<HTMLLIElement> {
  /** The locale to use for the timestamp. */
  locale: string;
  /** The timestamp of the message. */
  timestamp: number;
  /** The message to display. */
  message: string;
  /** The origin of the message. */
  messageOrigin: "local" | "remote";
  /** The sender's name. */
  name?: string;
  /** Whether the message has been edited. */
  hasBeenEdited?: boolean;
}

export const ChatEntry = ({
  name,
  locale,
  timestamp,
  message,
  messageOrigin,
  hasBeenEdited = false,
  className,
  ...props
}: ChatEntryProps) => {
  const time = new Date(timestamp);
  const title = time.toLocaleTimeString(locale, { timeStyle: "full" });

  return (
    <li
      title={title}
      data-lk-message-origin={messageOrigin}
      className={cn("group flex w-full flex-col gap-2", className)}
      {...props}
    >
      <header
        className={cn(
          "text-on-surface/60 flex items-center gap-2 text-[15px] font-medium tracking-wide px-1",
          messageOrigin === "local" ? "flex-row-reverse" : "text-left",
        )}
      >
        {name && <strong className="text-sm">{name}</strong>}
        <span className="opacity-50 transition-opacity ease-linear group-hover:opacity-100 uppercase">
          {hasBeenEdited && "*"}
          {time.toLocaleTimeString(locale, { timeStyle: "short" })}
        </span>
      </header>
      <div
        className={cn(
          "text-[22px] leading-relaxed px-6 py-4 max-w-[85%] w-fit transition-all",
          messageOrigin === "local"
            ? "ml-auto bg-primary text-white rounded-[28px] rounded-tr-sm border-none"
            : "mr-auto bg-surface-container-highest text-on-surface rounded-[28px] rounded-tl-sm border-none",
        )}
      >
        {message}
      </div>
    </li>
  );
};
