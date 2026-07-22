"use client";

import { useTheme } from "next-themes";
import { MoonIcon, SunIcon } from "@phosphor-icons/react";
import { cn } from "@/lib/utils";
import { useEffect, useState } from "react";

interface ThemeToggleProps {
  className?: string;
}

export function ThemeToggle({ className }: ThemeToggleProps) {
  const { theme, setTheme, resolvedTheme } = useTheme();
  const [mounted, setMounted] = useState(false);

  useEffect(() => {
    setMounted(true);
  }, []);

  if (!mounted) return null;

  const isDark = resolvedTheme === "dark";

  return (
    <button
      type="button"
      onClick={() => setTheme(isDark ? "light" : "dark")}
      className={cn(
        "text-primary bg-surface-container hover:bg-surface-container-high transition-colors rounded-full p-2.5 flex items-center justify-center border border-outline-variant/30",
        className,
      )}
      aria-label="Toggle theme"
    >
      {isDark ? (
        <SunIcon size={24} weight="bold" />
      ) : (
        <MoonIcon size={24} weight="bold" />
      )}
    </button>
  );
}
