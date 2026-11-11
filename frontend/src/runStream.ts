/** Durable SSE reader: handles fragmented frames and replays by sequence ID. */
export type ApiFetch = (url: string, options?: RequestInit) => Promise<Response>;
export type RunEvent = { id: number; type: string; data: Record<string, unknown> };

export async function* runEvents(fetcher: ApiFetch, runId: string, after: number, signal?: AbortSignal): AsyncGenerator<RunEvent> {
  const response = await fetcher(`/api/v1/runs/${encodeURIComponent(runId)}/events`, {
    headers: { 'Last-Event-ID': String(after) }, signal,
  });
  if (!response.ok || !response.body) throw new Error('Run event stream unavailable');
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  try {
    while (true) {
      const chunk = await reader.read();
      buffer += decoder.decode(chunk.value, { stream: !chunk.done });
      let boundary: RegExpExecArray | null;
      while ((boundary = /\r?\n\r?\n/.exec(buffer))) {
        const frame = buffer.slice(0, boundary.index);
        buffer = buffer.slice(boundary.index + boundary[0].length);
        const lines = frame.split(/\r?\n/);
        const type = lines.find(line => line.startsWith('event:'))?.slice(6).trim() ?? 'message';
        if (type === 'auth_expired') throw new Error('Session expired. Sign in again.');
        const id = Number(lines.find(line => line.startsWith('id:'))?.slice(3).trim());
        const data = lines.filter(line => line.startsWith('data:')).map(line => line.slice(5).trimStart()).join('\n');
        if (Number.isInteger(id) && id > after && data) {
          after = id;
          yield { id, type, data: JSON.parse(data) };
        }
      }
      if (chunk.done) return;
    }
  } finally {
    await reader.cancel().catch(() => undefined);
    reader.releaseLock();
  }
}
