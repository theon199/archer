import ChatPanel from './ChatPanel';
import ToolPermissions from './ToolPermissions';
import { createOperationId } from './operationId';
import React, { useState, useEffect, useRef, useCallback } from 'react';
import {
  Bot,
  Cpu,
  BookOpen,
  Brain,
  Terminal,
  Trash2,
  Sparkles,
} from 'lucide-react';

interface Note {
  id: string;
  title: string;
  content: string;
  tags: string[];
  created_at: string;
  revision: number;
}

interface Memory {
  id: string;
  memory_type: string;
  content: string;
  evidence: string;
  origin: string;
  validity: number;
  revision: number;
  value: string | null;
  allowed_values: string[];
  sources: { source_message_id: string | null; source_kind: string; quote: string; value: string }[];
}

interface JournalEntry {
  id: string;
  tool_name: string;
  arguments: Record<string, unknown>;
  permission_decision: string;
  outcome?: unknown;
  error?: string;
  timestamp: string;
}

export default function App() {
  const [token, setToken] = useState('');
  const [error, setError] = useState('');
  const logout = useCallback(() => setToken(''), []);
  if (token) return <AuthenticatedApp token={token} onLogout={logout} />;
  return <main className="min-h-dvh bg-neutral-950 text-neutral-100 flex items-center justify-center p-5">
    <div className="w-full max-w-sm rounded-3xl border border-neutral-800 bg-neutral-900 p-7 shadow-2xl sm:p-9">
    <div className="mb-8 flex items-center gap-3"><div className="flex h-11 w-11 items-center justify-center rounded-2xl bg-violet-600"><Sparkles className="h-5 w-5" /></div><span className="font-semibold tracking-wide">ARCHER</span></div>
    <h1 className="text-2xl font-semibold tracking-tight">Welcome back</h1>
    <p className="mt-2 text-sm leading-6 text-neutral-400">Sign in to your private workspace.</p>
    <form className="mt-7 space-y-4" onSubmit={async (event) => {
      event.preventDefault();
      const form = new FormData(event.currentTarget);
      try {
        const response = await fetch('/api/auth/login', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ username: form.get('username'), password: form.get('password') }),
        });
        if (!response.ok) { setError(response.status === 429 ? 'Too many attempts. Retry in five minutes.' : 'Sign in failed.'); return; }
        setToken((await response.json()).access_token);
        setError('');
      } catch { setError('Archer is unavailable.'); }
    }}>
      <label className="block text-sm font-medium text-neutral-300">Username <input className="mt-2 w-full rounded-xl border border-neutral-700 bg-neutral-950 px-4 py-3 text-neutral-100 outline-none focus:border-violet-400" name="username" autoComplete="username" required /></label>
      <label className="block text-sm font-medium text-neutral-300">Password <input className="mt-2 w-full rounded-xl border border-neutral-700 bg-neutral-950 px-4 py-3 text-neutral-100 outline-none focus:border-violet-400" name="password" type="password" autoComplete="current-password" required /></label>
      <button className="w-full rounded-xl bg-violet-600 px-4 py-3 text-sm font-semibold text-white hover:bg-violet-500 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-violet-300" type="submit">Sign in</button><p className="text-sm text-red-300" role="alert">{error}</p>
    </form>
    </div>
  </main>;
}

function AuthenticatedApp({ token, onLogout }: { token: string; onLogout: () => void }) {
  const apiFetch = useCallback(async (url: string, options: RequestInit = {}) => {
    const headers = new Headers(options.headers);
    headers.set('Authorization', `Bearer ${token}`);
    const response = await fetch(url, { ...options, headers });
    if (response.status === 401) onLogout();
    return response;
  }, [token, onLogout]);
  const [activeTab, setActiveTab] = useState<'chat' | 'notes' | 'memories' | 'journal' | 'tools'>('chat');
  const [provider, setProvider] = useState('local-ollama (gemma4:26b)');
  const [systemOnline, setSystemOnline] = useState(false);

  // Notes & Memory state
  const [notes, setNotes] = useState<Note[]>([]);
  const [memories, setMemories] = useState<Memory[]>([]);
  const [memoryError, setMemoryError] = useState('');
  const [learning, setLearning] = useState({ learning_enabled: true, retrieval_enabled: true });
  const [suppressedTopics, setSuppressedTopics] = useState<string[]>([]);
  const [learningEpoch, setLearningEpoch] = useState(0);
  const [learningBusy, setLearningBusy] = useState(false);
  const [journalEntries, setJournalEntries] = useState<JournalEntry[]>([]);

  // Modals / forms
  const [noteError, setNoteError] = useState('');
  const noteOperation = useRef<{ body: string; key: string } | null>(null);
  const [newNoteTitle, setNewNoteTitle] = useState('');
  const [newNoteContent, setNewNoteContent] = useState('');
  const [editingNote, setEditingNote] = useState<Note | null>(null);
  const [noteSaving, setNoteSaving] = useState(false);
  const editOperation = useRef<{ body: string; key: string } | null>(null);
  const [noteHistory, setNoteHistory] = useState<{ id: string; revisions: { revision: number; title: string; content: string; deleted_at: string | null }[] } | null>(null);

  const fetchHealth = useCallback(async () => {
    try {
      const res = await apiFetch('/api/health');
      if (res.ok) {
        const data = await res.json();
        setSystemOnline(true);
        setProvider(data.active_provider);
      }
    } catch {
      setSystemOnline(false);
    }
  }, [apiFetch]);

  const fetchNotes = useCallback(async () => {
    try {
      const res = await apiFetch('/api/notes');
      if (res.ok) { setNotes(await res.json()); setNoteError(''); }
      else { setNotes([]); setEditingNote(null); setNoteHistory(null); setNoteError('Notes access is unavailable. Review the permissions below.'); }
    } catch (e) {
      console.error(e);
    }
  }, [apiFetch]);

  const fetchMemories = useCallback(async () => {
    setLearningBusy(true);
    try {
      const res = await apiFetch('/api/memories');
      if (res.ok) setMemories(await res.json());
      const settings = await apiFetch('/api/memory/settings');
      if (settings.ok) {
        const data = await settings.json();
        setLearning({ learning_enabled: Boolean(data.learning_enabled), retrieval_enabled: Boolean(data.retrieval_enabled) });
        setLearningEpoch(data.epoch);
        setSuppressedTopics(data.suppressed_topics.map((topic: { subject_key: string }) => topic.subject_key));
      }
    } catch (e) {
      console.error(e);
      setMemoryError('Memories could not be refreshed. Saved settings may be out of date.');
    } finally { setLearningBusy(false); }
  }, [apiFetch]);

  const fetchJournal = useCallback(async () => {
    try {
      const res = await apiFetch('/api/journal');
      if (res.ok) setJournalEntries(await res.json());
    } catch (e) {
      console.error(e);
    }
  }, [apiFetch]);

  useEffect(() => {
    fetchHealth();
    fetchNotes();
    fetchMemories();
    fetchJournal();
  }, [fetchHealth, fetchNotes, fetchMemories, fetchJournal]);


  const handleCreateNote = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!newNoteTitle.trim()) return;
    try {
      const body = JSON.stringify({ title: newNoteTitle, content: newNoteContent });
      const operation = noteOperation.current?.body === body ? noteOperation.current : { body, key: createOperationId() };
      noteOperation.current = operation;
      const response = await apiFetch('/api/notes', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Idempotency-Key': operation.key },
        body,
      });
      if (!response.ok) { setNoteError(`Note was not saved (${response.status}).`); return; }
      noteOperation.current = null;
      setNoteError('');
      setNewNoteTitle('');
      setNewNoteContent('');
      fetchNotes();
      fetchJournal();
    } catch {
      setNoteError('Save status is uncertain. Retry the same note or refresh notes to inspect it.');
    }
  };

  const handleDeleteNote = async (id: string, revision: number) => {
    try {
      const response = await apiFetch(`/api/notes/${id}?expected_revision=${revision}`, { method: 'DELETE', headers: { 'Idempotency-Key': `delete:${id}:${revision}` } });
      if (!response.ok) { setNoteError(`Note was not deleted (${response.status}); refresh before retrying.`); return; }
      setNoteError('');
      fetchNotes();
      fetchJournal();
    } catch (err) {
      console.error(err);
    }
  };

  const handleDeleteMemory = async (id: string, revision: number) => {
    try {
      const response = await apiFetch(`/api/memories/${id}?expected_revision=${revision}`, { method: 'DELETE' });
      if (!response.ok) { setMemoryError(`Memory was not deleted (${response.status}); refresh before retrying.`); return; }
      setMemoryError('');
      fetchMemories();
    } catch (err) {
      console.error(err);
    }
  };

  return (
    <div className="flex h-dvh w-full flex-col md:flex-row bg-neutral-950 text-neutral-100 font-sans overflow-hidden">
      {/* Sidebar */}
      <aside className="w-full shrink-0 border-b md:w-64 md:border-b-0 md:border-r border-neutral-800 bg-neutral-900/60 px-3 py-2 md:p-4 flex flex-col justify-between">
        <div>
          <div className="flex items-center space-x-3 mb-2 md:mb-6 px-2">
            <div className="h-9 w-9 rounded-xl bg-gradient-to-tr from-purple-600 to-indigo-500 flex items-center justify-center shadow-lg shadow-purple-500/20">
              <Sparkles className="h-5 w-5 text-white" />
            </div>
            <div>
              <h1 className="font-bold text-lg leading-tight tracking-wide text-neutral-50">ARCHER</h1>
              <p className="text-xs text-neutral-400">Your private assistant</p>
            </div>
          </div>

          {/* Navigation */}
          <nav aria-label="Main navigation" className="flex gap-1 overflow-x-auto md:block md:space-y-1 [&>button]:w-auto [&>button]:shrink-0 md:[&>button]:w-full">
            <button
              onClick={() => setActiveTab('chat')}
              className={`w-full flex items-center space-x-3 px-3 py-2.5 rounded-lg text-sm font-medium transition ${
                activeTab === 'chat'
                  ? 'bg-purple-600 text-white shadow-md'
                  : 'text-neutral-400 hover:text-white hover:bg-neutral-800/60'
              }`}
            >
              <Bot className="h-4 w-4" />
              <span>Assistant Chat</span>
            </button>
            <button
              onClick={() => { setNotes([]); setEditingNote(null); setNoteHistory(null); setActiveTab('notes'); void fetchNotes(); }}
              className={`w-full flex items-center space-x-3 px-3 py-2.5 rounded-lg text-sm font-medium transition ${
                activeTab === 'notes'
                  ? 'bg-purple-600 text-white shadow-md'
                  : 'text-neutral-400 hover:text-white hover:bg-neutral-800/60'
              }`}
            >
              <BookOpen className="h-4 w-4" />
              <span>Personal Notes</span>
            </button>
            <button
              onClick={() => { setActiveTab('memories'); void fetchMemories(); }}
              className={`w-full flex items-center space-x-3 px-3 py-2.5 rounded-lg text-sm font-medium transition ${
                activeTab === 'memories'
                  ? 'bg-purple-600 text-white shadow-md'
                  : 'text-neutral-400 hover:text-white hover:bg-neutral-800/60'
              }`}
            >
              <Brain className="h-4 w-4" />
              <span>Learned Memory</span>
            </button>
            <button
              onClick={() => { setActiveTab('journal'); void fetchJournal(); }}
              className={`w-full flex items-center space-x-3 px-3 py-2.5 rounded-lg text-sm font-medium transition ${
                activeTab === 'journal'
                  ? 'bg-purple-600 text-white shadow-md'
                  : 'text-neutral-400 hover:text-white hover:bg-neutral-800/60'
              }`}
            >
              <Terminal className="h-4 w-4" />
              <span>Action Journal</span>
            </button>
            <button onClick={() => setActiveTab('tools')} className={`w-full px-3 py-2.5 rounded-lg text-left text-sm font-medium ${activeTab === 'tools' ? 'bg-purple-600 text-white' : 'text-neutral-400 hover:text-white hover:bg-neutral-800/60'}`}>
              Tools
            </button>
            <button className="px-3 py-2.5 text-left text-sm text-neutral-400 hover:text-white" onClick={async () => {
              await apiFetch('/api/auth/logout', { method: 'POST' }); onLogout();
            }}>Sign out</button>
          </nav>
        </div>

        {/* System status footer */}
        <div className="hidden md:block bg-neutral-900 border border-neutral-800 rounded-xl p-3 text-xs space-y-2">
          <div className="flex items-center justify-between">
            <span className="text-neutral-400 flex items-center space-x-1.5">
              <Cpu className="h-3.5 w-3.5 text-indigo-400" />
              <span>DGX Spark</span>
            </span>
            <span
              className={`inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-semibold ${
                systemOnline ? 'bg-emerald-950 text-emerald-400 border border-emerald-800/60' : 'bg-red-950 text-red-400'
              }`}
            >
              {systemOnline ? 'ONLINE' : 'CONNECTING'}
            </span>
          </div>
          <div className="text-[11px] text-neutral-400 truncate">
            Provider: <span className="text-neutral-200 font-mono">{provider}</span>
          </div>
        </div>
      </aside>

      {/* Main Content Area */}
      <main className="flex-1 min-h-0 min-w-0 flex flex-col overflow-hidden bg-neutral-950">
        {/* Top Header */}
        <header className="hidden md:flex h-14 shrink-0 border-b border-neutral-800 px-6 items-center justify-between bg-neutral-900/30 backdrop-blur">
          <div className="flex items-center space-x-2">
            <span className="text-sm text-neutral-400">Workspace:</span>
            <span className="text-sm font-semibold text-neutral-200">Archer V1 / Personal workspace</span>
          </div>
          <div className="flex items-center space-x-3 text-xs">
            <span className="px-2.5 py-1 rounded-full bg-neutral-800 text-neutral-300 border border-neutral-700">
              Plugins: Approved Files, Notes, Tavily Web Search
            </span>
          </div>
        </header>

        {/* View switching */}
        {activeTab === 'tools' && <ToolPermissions apiFetch={apiFetch} />}
        {activeTab === 'chat' && <ChatPanel apiFetch={apiFetch} onFinished={fetchJournal} />}

        {activeTab === 'notes' && (
          <div className="flex-1 p-6 overflow-y-auto space-y-6">
            <div className="max-w-4xl mx-auto space-y-6">
              <div className="flex items-center justify-between">
                <div>
                  <h2 className="text-xl font-bold text-neutral-100">Personal Notes</h2>
                  <p className="text-xs text-neutral-400">Private, persistent notes stored locally on DGX Spark.</p>
                </div>
              </div>

              <p role="alert">{noteError}</p>
              <button className="text-sm text-violet-300 hover:text-violet-200" onClick={() => { setEditingNote(null); setNoteHistory(null); void fetchNotes(); }}>Refresh notes</button>
              <details className="rounded-xl border border-neutral-800 bg-neutral-900/50 p-4 text-sm">
              <summary className="cursor-pointer font-medium text-neutral-300">Note permissions</summary>
              <fieldset className="mt-3 space-x-3">
                <legend>Owner permissions for notes (also applies to assistant actions)</legend>
                {(['list', 'get', 'history', 'create', 'update', 'delete'] as const).map(action =>
                  <span key={action} className="inline-flex gap-1 p-1">
                    <button className="px-2 py-1 rounded bg-neutral-800" onClick={async () => {
                      const result = await apiFetch(`/api/permissions/personal_notes/${action}`, { method: 'PUT',
                        headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ enabled: true }) });
                      if (!result.ok) setNoteError('Only a provisioned owner can grant note permissions.');
                      else { setNoteError(''); if (action === 'list') fetchNotes(); }
                    }}>Allow {action}</button>
                    <button className="px-2 py-1 rounded border border-neutral-700" aria-label={`Revoke notes ${action}`} onClick={async () => {
                      const result = await apiFetch(`/api/permissions/personal_notes/${action}`, { method: 'PUT',
                        headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ enabled: false }) });
                      if (!result.ok) setNoteError('Permission change failed.');
                      else {
                        if (action === 'list') { setNotes([]); setEditingNote(null); setNoteHistory(null); }
                        if (action === 'history') setNoteHistory(null);
                        if (action === 'update') setEditingNote(null);
                        setNoteError(`Notes ${action} permission revoked.`);
                      }
                    }}>Revoke</button>
                  </span>)}
              </fieldset>
              </details>
              {/* Create Note Card */}
              <form onSubmit={handleCreateNote} className="bg-neutral-900 border border-neutral-800 rounded-xl p-4 space-y-3">
                <input
                  aria-label="New note title"
                  type="text"
                  value={newNoteTitle}
                  onChange={(e) => setNewNoteTitle(e.target.value)}
                  placeholder="Note Title..."
                  className="w-full bg-neutral-950 border border-neutral-800 rounded-lg px-3 py-2 text-sm focus:outline-none focus:border-purple-500"
                />
                <textarea
                  aria-label="New note content"
                  value={newNoteContent}
                  onChange={(e) => setNewNoteContent(e.target.value)}
                  placeholder="Markdown content..."
                  rows={3}
                  className="w-full bg-neutral-950 border border-neutral-800 rounded-lg px-3 py-2 text-sm focus:outline-none focus:border-purple-500"
                />
                <button
                  type="submit"
                  className="bg-purple-600 hover:bg-purple-500 text-white px-4 py-2 rounded-lg text-xs font-semibold"
                >
                  Save Note
                </button>
              </form>

              {/* Notes Grid */}
              <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                {notes.map((note) => (
                  <div key={note.id} data-note-id={note.id} className="bg-neutral-900 border border-neutral-800 rounded-xl p-4 flex flex-col justify-between">
                    <div>
                      <h3 className="font-semibold text-neutral-200 text-sm mb-1">{note.title}</h3>
                      <p className="text-xs text-neutral-400 whitespace-pre-wrap">{note.content}</p>
                      <p>Revision {note.revision}</p>
                      <button className="mr-3 p-2" onClick={() => { editOperation.current = null; setEditingNote({ ...note }); }}>Edit note</button>
                      <button className="p-2" onClick={async () => {
                        try {
                          const response = await apiFetch(`/api/notes/${note.id}/history`);
                          if (!response.ok) { setNoteHistory(null); setNoteError(`Note history unavailable (${response.status}).`); return; }
                          const saved = await response.json(); setNoteHistory({ id: note.id, revisions: saved.revisions }); setNoteError('');
                        } catch { setNoteError('Note history unavailable.'); }
                      }}>View revisions</button>
                      {editingNote?.id === note.id && <form aria-label="Edit note" className="space-y-2" onSubmit={async event => {
                        event.preventDefault(); if (noteSaving) return;
                        const body = JSON.stringify({ title: editingNote.title, content: editingNote.content, expected_revision: editingNote.revision });
                        const operation = editOperation.current?.body === body ? editOperation.current : { body, key: createOperationId() };
                        editOperation.current = operation; setNoteSaving(true);
                        try {
                          const response = await apiFetch(`/api/notes/${note.id}`, { method: 'PUT', headers: { 'Content-Type': 'application/json', 'Idempotency-Key': operation.key }, body });
                          if (!response.ok) { setNoteError(`Note was not updated (${response.status}). Refresh before retrying.`); return; }
                          editOperation.current = null; setEditingNote(null); setNoteHistory(null); setNoteError(''); void fetchNotes(); void fetchJournal();
                        } catch { setNoteError('Update status is uncertain. Retry the same edit or refresh notes to inspect it.'); }
                        finally { setNoteSaving(false); }
                      }}>
                        <input aria-label="Edit note title" value={editingNote.title} disabled={noteSaving} onChange={event => setEditingNote({ ...editingNote, title: event.target.value })} className="w-full bg-neutral-950 p-2" />
                        <textarea aria-label="Edit note content" value={editingNote.content} disabled={noteSaving} onChange={event => setEditingNote({ ...editingNote, content: event.target.value })} className="w-full bg-neutral-950 p-2" />
                        <button type="submit" disabled={noteSaving}>Save changes</button>
                        <button type="button" disabled={noteSaving} className="ml-3" onClick={() => setEditingNote(null)}>Cancel edit</button>
                      </form>}
                      {noteHistory?.id === note.id && <section aria-label="Note revision history">
                        {noteHistory.revisions.map(revision => <article key={revision.revision} className="border-t border-neutral-700 mt-2 p-2">
                          <strong>Revision {revision.revision}{revision.deleted_at ? ' (deleted)' : ''}: {revision.title}</strong>
                          <p className="whitespace-pre-wrap">{revision.content}</p>
                        </article>)}
                      </section>}
                    </div>
                    <div className="flex items-center justify-between mt-4 pt-3 border-t border-neutral-800/80 text-[11px] text-neutral-500">
                      <span>{new Date(note.created_at).toLocaleDateString()}</span>
                      <button
                        aria-label={`Delete note ${note.title}`}
                        onClick={() => handleDeleteNote(note.id, note.revision)}
                        className="text-neutral-500 hover:text-red-400 transition"
                      >
                        <Trash2 className="h-3.5 w-3.5" />
                      </button>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          </div>
        )}

        {activeTab === 'memories' && (
          <div className="flex-1 p-6 overflow-y-auto space-y-6">
            <div className="max-w-4xl mx-auto space-y-4">
              <div>
                <h2 className="text-xl font-bold text-neutral-100">Learned Preferences & Memories</h2>
                <p className="text-xs text-neutral-400">
                  Inspect what Archer has learned about your workflow, review source evidence, and correct or delete items.
                </p>
              </div>

              <p role="alert">{memoryError}</p>
              <fieldset>
                <legend>Learning and personalization</legend>
                {(['learning_enabled', 'retrieval_enabled'] as const).map(key => <label key={key} className="block">
                  <input type="checkbox" disabled={learningBusy} checked={learning[key]} onChange={async event => {
                    const next = { ...learning, [key]: event.currentTarget.checked };
                    setLearning(next);
                    setLearningBusy(true);
                    try {
                      const response = await apiFetch('/api/memory/settings', { method: 'PUT',
                        headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ...next, expected_epoch: learningEpoch }) });
                      if (response.ok) {
                        const saved = await response.json();
                        setLearning(next); setLearningEpoch(saved.epoch); await fetchMemories();
                      } else { setMemoryError('Learning settings changed or could not be saved. Refresh and retry.'); await fetchMemories(); }
                    } catch { setMemoryError('Learning settings could not be saved.'); await fetchMemories(); }
                    finally { setLearningBusy(false); }
                  }} /> {key === 'learning_enabled' ? 'Learn supported preferences from my messages' : 'Use saved preferences in future answers'}
                </label>)}
                <p className="text-xs">Supported topics: response length, response format, units, and descriptive routine suggestions. Suggestions never schedule or execute.</p>
                {suppressedTopics.length > 0 && <p>Deleted topics remain suppressed: {suppressedTopics.join(', ')}. Historical chat remains saved.</p>}
                <button onClick={() => fetchMemories()}>Refresh memories</button>
              </fieldset>
              <div className="space-y-3">
                {memories.map((mem) => (
                  <div key={mem.id} data-memory-id={mem.id} className="bg-neutral-900 border border-neutral-800 rounded-xl p-4 flex items-start justify-between">
                    <div className="space-y-1">
                      <div className="flex items-center space-x-2">
                        <span className="px-2 py-0.5 rounded text-[10px] font-mono uppercase bg-purple-950 text-purple-400 border border-purple-800/50">
                          {mem.memory_type}
                        </span>
                        <span className="text-[11px] text-neutral-500">Origin: {mem.origin}</span>
                      </div>
                      <p className="text-sm text-neutral-200 font-medium">{mem.content}</p>
                      <details>
                        <summary>Evidence and corrections</summary>
                        {mem.sources.map((source, index) => <p key={index} className="text-xs text-neutral-400">
                          {source.source_kind}: {source.quote} {source.source_message_id && <span>Source message: {source.source_message_id}</span>}
                        </p>)}
                        {!mem.sources.length && <p>Legacy evidence is unverified and is not used for personalization.</p>}
                      </details>
                      {mem.allowed_values.length > 0 && <form onSubmit={async event => {
                        event.preventDefault();
                        const value = new FormData(event.currentTarget).get('value');
                        const response = await apiFetch(`/api/memories/${mem.id}`, { method: 'PUT',
                          headers: { 'Content-Type': 'application/json' },
                          body: JSON.stringify({ new_content: value, expected_revision: mem.revision }) });
                        if (!response.ok) setMemoryError(`Correction failed (${response.status}). Refresh and retry.`);
                        else { setMemoryError(''); fetchMemories(); }
                      }}>
                        <label>Correct preference <select key={mem.revision} name="value" defaultValue={mem.value ?? ''}>
                          {mem.allowed_values.map(value => <option key={value} value={value}>{value}</option>)}
                        </select></label><button type="submit">Save correction</button>
                      </form>}
                    </div>
                    <button
                      aria-label={`Delete memory ${mem.content}`}
                      onClick={() => handleDeleteMemory(mem.id, mem.revision)}
                      className="text-neutral-500 hover:text-red-400 transition p-1"
                    >
                      <Trash2 className="h-4 w-4" />
                    </button>
                  </div>
                ))}
              </div>
            </div>
          </div>
        )}

        {activeTab === 'journal' && (
          <div className="flex-1 p-6 overflow-y-auto space-y-6">
            <div className="max-w-4xl mx-auto space-y-4">
              <div>
                <h2 className="text-xl font-bold text-neutral-100">Action Journal & Audit Trail</h2>
                <p className="text-xs text-neutral-400">
                  Every tool call, permission decision, and execution outcome is logged with full transparency.
                </p>
              </div>

              <div className="space-y-2">
                {journalEntries.map((entry) => (
                  <div key={entry.id} className="bg-neutral-900 border border-neutral-800 rounded-xl p-3 text-xs space-y-1.5 font-mono">
                    <div className="flex items-center justify-between">
                      <span className="font-bold text-indigo-400">{entry.tool_name}</span>
                      <span className="text-neutral-500 text-[11px]">{entry.timestamp}</span>
                    </div>
                    <div className="text-neutral-400">
                      Arguments: <span className="text-neutral-300">{JSON.stringify(entry.arguments)}</span>
                    </div>
                    <div className="text-neutral-400">
                      Permission: <span className="text-emerald-400">{entry.permission_decision}</span>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          </div>
        )}
      </main>
    </div>
  );
}
