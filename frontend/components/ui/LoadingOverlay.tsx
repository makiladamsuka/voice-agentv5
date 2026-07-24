import React from 'react';

export default function LoadingOverlay({ label = 'Loading…' }: { label?: string }) {
  return (
    <div
      role="status"
      aria-live="polite"
      className="fixed inset-0 flex items-center justify-center bg-neutral-950/85 z-50"
    >
      <div className="flex flex-col items-center gap-4 text-white">
        <svg
          className="animate-spin h-12 w-12"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
        >
          <circle cx="12" cy="12" r="10" strokeWidth="4" />
        </svg>
        <p className="text-lg font-medium">{label}</p>
      </div>
    </div>
  );
}
