import { useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { runEvents } from './runStream';
import type { ApiFetch } from './runStream';
import { createOperationId } from './operationId';
import Markdown from 'react-markdown';
import remarkGfm from 'remark-gfm';

type Conversation = { id: string; title: string };
type Item = { id: string; role: string; content: string; run_id?: string; status?: string; error?: string; event_seq?: number };
type History = { conversation: Conversation; items: Item[]; older_before: number | null };
type Submission = { text: string; operationId: string; conversationId: string | null };
const terminal = (status?: string) => ['succeeded', 'failed', 'canceled', 'needs_reconciliation'].includes(status ?? '');
const merge = (older: Item[], newer: Item[]) => [...new Map([...older, ...newer].map(item => [item.id, item])).values()];

export default function ChatPanel({ apiFetch, onFinished }: { apiFetch: ApiFetch; onFinished: () => void }) {
  const [selected, setSelected] = useState(() => new URLSearchParams(location.search).get('conversation') ?? '');
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [items, setItems] = useState<Item[]>([]);
  const [older, setOlder] = useState<number | null>(null);
  const [draft, setDraft] = useState('');
  const [pending, setPending] = useState<Submission | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [loading, setLoading] = useState(false);
  const [running, setRunning] = useState<{ id: string; status: string } | null>(null);
  const [error, setError] = useState('');
  const [reload, setReload] = useState(0);
  const view = useRef<AbortController | null>(null);
  const navigation = useRef(0);

  useEffect(() => {
    const controller = new AbortController();
    void apiFetch('/api/v1/conversations', { signal: controller.signal }).then(async response => {
      if (!response.ok) throw new Error('Saved conversations unavailable.');
      const list = await response.json();
      if (!controller.signal.aborted) setConversations(list);
    }).catch(() => { if (!controller.signal.aborted) setError('Saved conversations unavailable.'); });
    return () => controller.abort();
  }, [apiFetch, reload]);

  useEffect(() => {
    const controller = new AbortController();
    view.current = controller;
    const signal = controller.signal;
    const url = new URL(location.href);
    if (selected) url.searchParams.set('conversation', selected); else url.searchParams.delete('conversation');
    history.replaceState(null, '', url);
    setItems([]); setRunning(null); setOlder(null); setError('');
    if (!selected) return () => controller.abort();
    setLoading(true);
    const load = async (): Promise<History> => {
      const response = await apiFetch(`/api/v1/conversations/${encodeURIComponent(selected)}/history`, { signal });
      if (!response.ok) throw new Error('Conversation unavailable for this account.');
      return response.json();
    };
    const update = (id: string, content: string, status: string, failure?: string) => {
      if (signal.aborted) return;
      setItems(previous => previous.map(item => item.run_id === id ? { ...item, content, status, error: failure } : item));
      setRunning(terminal(status) ? null : { id, status });
    };
    void (async () => {
      let saved = await load();
      while (!signal.aborted) {
        setItems(saved.items); setOlder(saved.older_before); setLoading(false);
        const active = saved.items.find(item => item.run_id && !terminal(item.status));
        if (!active?.run_id) { setRunning(null); return; }
        const id = active.run_id;
        let cursor = active.event_seq ?? 0, text = active.content, status = active.status ?? 'queued';
        let finished = false;
        setRunning({ id, status });
        for (let attempt = 0; attempt < 4 && !finished && !signal.aborted; attempt++) {
          try {
            for await (const event of runEvents(apiFetch, id, cursor, signal)) {
              if (signal.aborted) return;
              cursor = event.id;
              if (event.type === 'token' && typeof event.data.token === 'string') text += event.data.token;
              if (event.type === 'state' && typeof event.data.status === 'string') status = event.data.status;
              update(id, text, status);
              if (event.type === 'done') { finished = true; break; }
            }
            const response = await apiFetch(`/api/v1/runs/${encodeURIComponent(id)}`, { signal });
            if (!response.ok) throw new Error('Run unavailable.');
            const snapshot = await response.json();
            finished = terminal(snapshot.status);
            update(id, snapshot.output, snapshot.status, snapshot.error);
            if (!finished) throw new Error('Stream interrupted.');
          } catch {
            if (signal.aborted) return;
            if (attempt === 3) { setError('Connection lost. The run remains saved; reconnect to inspect it.'); return; }
            await new Promise(resolve => setTimeout(resolve, 250 * (attempt + 1)));
          }
        }
        if (signal.aborted) return;
        onFinished();
        saved = await load();
      }
    })().catch(failure => {
      if (!signal.aborted) setError(failure instanceof Error ? failure.message : 'Chat unavailable.');
    }).finally(() => { if (!signal.aborted) setLoading(false); });
    return () => controller.abort(); // Disconnecting this view never cancels server work.
  }, [selected, reload, apiFetch, onFinished]);

  function select(id: string) {
    navigation.current++;
    view.current?.abort();
    setSelected(id); setPending(null); setDraft(''); setLoading(false);
    setReload(value => value + 1);
  }

  async function submit(event?: FormEvent, retry?: Submission) {
    event?.preventDefault();
    if (submitting || running || (!retry && !draft.trim())) return;
    const epoch = navigation.current;
    const operation = retry ?? { text: draft.trim(), operationId: createOperationId(), conversationId: selected || null };
    setPending(operation); setSubmitting(true); setError('');
    try {
      const response = await apiFetch('/api/v1/runs', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: operation.text, conversation_id: operation.conversationId, client_operation_id: operation.operationId }) });
      if (!response.ok) throw new Error(`Run submission unavailable (${response.status}).`);
      const run = await response.json();
      if (epoch === navigation.current) { setSelected(run.conversation_id); setDraft(''); setPending(null); setReload(value => value + 1); }
    } catch {
      if (epoch === navigation.current) setError('Submission status is uncertain. Retry with the same request ID, or inspect saved conversations.');
    } finally { setSubmitting(false); }
  }

  return <section aria-label="Assistant chat" className="flex flex-1 min-h-0">
    <aside aria-label="Saved conversations" className="hidden md:block w-56 shrink-0 overflow-y-auto border-r border-neutral-800 bg-neutral-900/20 p-4 space-y-3">
      <h2 className="text-xs font-semibold uppercase tracking-wider text-neutral-500">Conversations</h2>
      <button className="block w-full rounded-xl border border-neutral-700 px-3 py-2.5 text-left text-sm font-medium hover:bg-neutral-800" onClick={() => select('')}>+ New conversation</button>
      <button className="block px-2 text-xs text-neutral-400 hover:text-neutral-100" onClick={() => setReload(value => value + 1)}>Refresh saved chats</button>
      {conversations.map(conversation => <button key={conversation.id} aria-current={selected === conversation.id ? 'page' : undefined}
        className="block w-full text-left p-2 rounded hover:bg-neutral-800 break-words" onClick={() => select(conversation.id)}>{conversation.title}</button>)}
    </aside>
    <div className="flex flex-1 flex-col min-w-0">
      <div className="md:hidden flex items-center gap-2 border-b border-neutral-800 px-4 py-3">
        <label className="sr-only" htmlFor="mobile-conversations">Saved conversations</label>
        <select id="mobile-conversations" value={selected} onChange={event => select(event.target.value)} className="min-w-0 flex-1 rounded-lg border border-neutral-700 bg-neutral-900 px-3 py-2 text-sm">
          <option value="">New conversation</option>
          {conversations.map(conversation => <option key={conversation.id} value={conversation.id}>{conversation.title}</option>)}
        </select>
        <button className="rounded-lg bg-neutral-800 px-3 py-2 text-sm" onClick={() => select('')}>New</button>
      </div>
      <div data-testid="chat-messages" className="flex-1 overflow-y-auto p-5 space-y-4">
        {older !== null && <button onClick={async () => {
          const controller = view.current;
          if (!controller || !selected) return;
          try {
            const response = await apiFetch(`/api/v1/conversations/${encodeURIComponent(selected)}/history?before=${older}`, { signal: controller.signal });
            if (!response.ok) throw new Error();
            const page: History = await response.json();
            if (!controller.signal.aborted) { setItems(current => merge(page.items, current)); setOlder(page.older_before); }
          } catch { if (!controller.signal.aborted) setError('Older messages unavailable.'); }
        }}>Load older messages</button>}
        {!selected && <div className="mx-auto flex h-full max-w-xl flex-col justify-center pb-12 md:text-center"><p className="text-xs font-semibold uppercase tracking-[0.2em] text-violet-400">Your space to think</p><h2 className="mt-3 text-2xl font-semibold tracking-tight text-neutral-100 sm:text-3xl">What can I help you with?</h2><p className="mt-3 text-sm leading-6 text-neutral-400">Make a plan, work through an idea, or pick up where you left off.</p></div>}
        {loading && <p role="status">Loading saved conversation…</p>}
        {items.map(item => <article key={item.id} data-message-id={item.id} data-run-id={item.run_id}
          className={`p-3 rounded-xl whitespace-pre-wrap break-words ${item.role === 'user' ? 'bg-purple-900/60' : 'bg-neutral-900'}`}>
          <strong>{item.role === 'user' ? 'You' : 'Archer'}</strong>
          {item.role === 'user' ? <p>{item.content}</p> : <div className="chat-answer">
            <Markdown remarkPlugins={[remarkGfm]} skipHtml disallowedElements={['img']} urlTransform={url => /^https?:\/\//i.test(url) ? url : ''}
              components={{a: ({node: _node, ...props}) => <a {...props} target="_blank" rel="noopener noreferrer" />}}>
              {item.content || (item.run_id && !terminal(item.status) ? 'Working locally…' : '')}
            </Markdown>
          </div>}
          {item.run_id && <small>{item.status}{item.error ? `: ${item.error}` : ''}</small>}
        </article>)}
      </div>
      <div className="p-4 border-t border-neutral-800 space-y-2">
        <div className="mx-auto max-w-3xl space-y-2">
        <p role="alert">{error}</p>
        <p role="status" data-testid="run-status" data-run-id={running?.id}>{running?.status ?? (submitting ? 'Submitting…' : 'Ready')}</p>
        {running && <button className="mr-3 p-2 rounded bg-neutral-800" onClick={async () => {
          try {
            const response = await apiFetch(`/api/v1/runs/${encodeURIComponent(running.id)}/cancel`, { method: 'POST' });
            if (!response.ok) throw new Error();
            const snapshot = await response.json(); setRunning({ id: running.id, status: snapshot.status });
          } catch { setError('Cancellation was not confirmed. Reconnect to inspect the saved run.'); }
        }}>Cancel run</button>}
        {selected && <button className="p-2 rounded bg-neutral-800" onClick={() => setReload(value => value + 1)}>Reconnect saved run</button>}
        {pending && !submitting && <button onClick={() => void submit(undefined, pending)}>Retry submission</button>}
        <form onSubmit={event => void submit(event)} className="flex gap-2">
          <input aria-label="Message Archer" value={draft} onChange={event => setDraft(event.target.value)} disabled={Boolean(pending)}
            placeholder="Ask Archer about plans, ideas, research, or everyday tasks" className="flex-1 min-w-0 bg-neutral-900 p-3 rounded border border-neutral-700" />
          <button type="submit" disabled={submitting || loading || Boolean(running) || Boolean(pending) || !draft.trim()} className="bg-purple-700 p-3 rounded disabled:opacity-40">Send</button>
        </form>
        </div>
      </div>
    </div>
  </section>;
}
