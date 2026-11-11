import { useCallback, useEffect, useRef, useState } from 'react';
import type { ApiFetch } from './runStream';

type Capability = { name: string; version: string; description: string; actions: string[]; configuration_notice: string | null };
type Grant = { tool_name: string; action: string; tool_version: string; enabled: number; revision: number };
type Snapshot = { capabilities: Capability[]; grants: Grant[] };
const titles: Record<string, string> = {
  personal_notes: 'Personal notes', read_approved_file: 'Approved files',
  web_search: 'Web search', web_fetch: 'Public web documents',
};

export default function ToolPermissions({ apiFetch }: { apiFetch: ApiFetch }) {
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const [changing, setChanging] = useState(false);
  const alive = useRef(false);
  const pending = useRef(false);
  const generation = useRef(0);
  const request = useRef<AbortController | null>(null);
  const channel = useRef<BroadcastChannel | null>(null);

  const refresh = useCallback(async () => {
    const current = ++generation.current;
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    setLoading(true);
    try {
      const response = await apiFetch('/api/permissions', { signal: controller.signal });
      if (!response.ok) throw new Error(response.status === 403 ? 'Only the owner can manage tool permissions.' : 'Tool permissions could not be loaded.');
      const data: Snapshot = await response.json();
      if (!Array.isArray(data.capabilities) || !Array.isArray(data.grants)) throw new Error('Tool permission data is unavailable.');
      if (alive.current && current === generation.current) { setSnapshot(data); setError(''); }
    } catch (failure) {
      if (controller.signal.aborted) return;
      if (alive.current && current === generation.current) {
        setSnapshot(null);
        setError(failure instanceof Error ? failure.message : 'Tool permissions could not be loaded.');
      }
    } finally {
      if (alive.current && current === generation.current) setLoading(false);
    }
  }, [apiFetch]);

  useEffect(() => {
    alive.current = true;
    const update = () => { if (!pending.current && document.visibilityState === 'visible') void refresh(); };
    update();
    window.addEventListener('focus', update);
    document.addEventListener('visibilitychange', update);
    const timer = window.setInterval(update, 5000);
    if (typeof BroadcastChannel !== 'undefined') {
      channel.current = new BroadcastChannel('archer-tool-permissions');
      channel.current.onmessage = update;
    }
    return () => {
      alive.current = false; request.current?.abort();
      window.clearInterval(timer); window.removeEventListener('focus', update);
      document.removeEventListener('visibilitychange', update);
      channel.current?.close(); channel.current = null;
    };
  }, [refresh]);

  const change = async (tool: Capability, action: string, enabled: boolean) => {
    if (pending.current) return;
    pending.current = true; setChanging(true);
    generation.current++; request.current?.abort();
    let message = '';
    try {
      const response = await apiFetch(`/api/permissions/${encodeURIComponent(tool.name)}/${encodeURIComponent(action)}`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled, expected_tool_version: tool.version }),
      });
      if (!response.ok) message = response.status === 409 ? 'The tool version changed. Review the refreshed permissions before granting.' : 'Permission change was rejected. Review the refreshed state.';
    } catch {
      message = 'The change acknowledgement was lost. Review the refreshed server state before trying again.';
    } finally {
      channel.current?.postMessage('refresh');
      if (alive.current) {
        await refresh();
        if (alive.current && message) setError(message);
        if (alive.current) setChanging(false);
      }
      pending.current = false;
    }
  };

  return <section className="flex-1 overflow-y-auto p-6 space-y-4" aria-label="Owner tool permissions">
    <h2 className="text-xl font-bold">Tool permissions</h2>
    <p>Your selected tools are enabled by default. You can turn off any action here. Grants apply to your account and the tool version; configuration and safety checks still apply.</p>
    <p>Public web retrieval sends the requested URL to that website; web search sends your query to Tavily.</p>
    <button disabled={changing || loading} onClick={() => void refresh()}>Refresh permissions</button>
    <p role="status" aria-live="polite">{changing ? 'Saving and checking permissions…' : loading ? 'Checking permissions…' : ''}</p>
    <p role="alert">{error}</p>
    {snapshot?.capabilities.map(tool => <article key={tool.name} data-tool-name={tool.name} className="border border-neutral-700 rounded p-4 space-y-2">
      <h3 className="font-semibold">{titles[tool.name] ?? tool.name}</h3>
      <p>{tool.description}</p>
      {tool.configuration_notice && <p>{tool.configuration_notice}</p>}
      <details><summary>Reviewed tool version</summary><code className="break-all">{tool.version}</code></details>
      <fieldset aria-label={`${tool.name} permissions`} disabled={changing || loading}>
        {tool.actions.map(action => {
          const saved = snapshot.grants.find(grant => grant.tool_name === tool.name && grant.action === action);
          const stale = Boolean(saved?.enabled && saved.tool_version !== tool.version);
          const allowed = Boolean(saved?.enabled && !stale);
          return <div key={action} className="flex flex-wrap gap-3 py-1">
            <span>{action}</span>
            <span data-permission-status={`${tool.name}:${action}`}>{stale ? 'Review required: tool version changed' : allowed ? 'Allowed' : 'Not allowed'}</span>
            <button aria-label={`Grant ${tool.name} ${action}`} disabled={allowed} onClick={() => void change(tool, action, true)}>Allow {action}</button>
            <button aria-label={`Revoke ${tool.name} ${action}`} disabled={!saved?.enabled} onClick={() => void change(tool, action, false)}>Revoke {action}</button>
          </div>;
        })}
      </fieldset>
    </article>)}
  </section>;
}
