"use client";

import { useEffect } from "react";

/** Boost LiveKit agent audio above HTMLMediaElement max (1.0) via Web Audio gain. */
const OUTPUT_GAIN = 1.25;

export function KioskAudioBoost() {
  useEffect(() => {
    const wired = new WeakSet<HTMLAudioElement>();
    let ctx: AudioContext | null = null;
    let gainNode: GainNode | null = null;

    const wire = (el: HTMLAudioElement) => {
      if (wired.has(el)) return;
      wired.add(el);
      try {
        if (!ctx) {
          ctx = new AudioContext();
          gainNode = ctx.createGain();
          gainNode.gain.value = OUTPUT_GAIN;
          gainNode.connect(ctx.destination);
        }
        el.volume = 1;
        const src = ctx.createMediaElementSource(el);
        src.connect(gainNode!);
        void ctx.resume();
      } catch {
        el.volume = 1;
      }
    };

    const scan = () => {
      document.querySelectorAll("audio").forEach((node) => wire(node));
    };

    scan();
    const observer = new MutationObserver(scan);
    observer.observe(document.body, { childList: true, subtree: true });
    const timer = setInterval(scan, 1500);

    return () => {
      observer.disconnect();
      clearInterval(timer);
      void ctx?.close();
    };
  }, []);

  return null;
}
