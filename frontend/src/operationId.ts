// getRandomValues is available on private HTTP-over-Tailscale origins, where
// randomUUID may be unavailable because the browser does not consider them secure.
export function createOperationId(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  return Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join('');
}
