/** No-op in NLU mode — LiveKit session/agent hooks are not used. */
export function useAgentErrors() {
  // LiveKit agent error monitoring removed. NLU errors are handled in useNluVoice.ts.
}
