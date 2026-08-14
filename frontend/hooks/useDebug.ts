import * as React from "react";

/** No-op in NLU mode — LiveKit room context no longer exists. */
export const useDebugMode = (
  _options: { logLevel?: string; enabled?: boolean } = {},
) => {
  // No LiveKit room to expose. Console logging is handled natively.
};
