import React, { useState, useEffect, useRef } from 'react';
import {
  Bot,
  Cpu,
  Send,
  BookOpen,
  Brain,
  Terminal,
  Trash2,
  Sparkles,
} from 'lucide-react';

interface Message {
  role: 'user' | 'assistant';
  content: string;
}

interface Note {
  id: string;
  title: string;
  content: string;
  tags: string[];
  created_at: string;
}

interface Memory {
  id: string;
  memory_type: string;
  content: string;
  evidence: string;
  origin: string;
  validity: number;
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
  const [activeTab, setActiveTab] = useState<'chat' | 'notes' | 'memories' | 'journal'>('chat');
  const [messages, setMessages] = useState<Message[]>([
    {
      role: 'assistant',
      content:
        'Hello Sri! I am Archer, your local-first AI assistant running on this NVIDIA DGX Spark. How can I assist your workflows today?',
    },
  ]);
  const [input, setInput] = useState('');
  const [isStreaming, setIsStreaming] = useState(false);
  const [provider, setProvider] = useState('local-ollama (gemma4:26b)');
  const [systemOnline, setSystemOnline] = useState(false);

  // Notes & Memory state
  const [notes, setNotes] = useState<Note[]>([]);
  const [memories, setMemories] = useState<Memory[]>([]);
  const [journalEntries, setJournalEntries] = useState<JournalEntry[]>([]);

  // Modals / forms
  const [newNoteTitle, setNewNoteTitle] = useState('');
  const [newNoteContent, setNewNoteContent] = useState('');

  const messagesEndRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    fetchHealth();
    fetchNotes();
    fetchMemories();
    fetchJournal();
  }, []);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  const fetchHealth = async () => {
    try {
      const res = await fetch('/api/health');
      if (res.ok) {
        const data = await res.json();
        setSystemOnline(true);
        setProvider(data.active_provider);
      }
    } catch {
      setSystemOnline(false);
    }
  };

  const fetchNotes = async () => {
    try {
      const res = await fetch('/api/notes');
      if (res.ok) setNotes(await res.json());
    } catch (e) {
      console.error(e);
    }
  };

  const fetchMemories = async () => {
    try {
      const res = await fetch('/api/memories');
      if (res.ok) setMemories(await res.json());
    } catch (e) {
      console.error(e);
    }
  };

  const fetchJournal = async () => {
    try {
      const res = await fetch('/api/journal');
      if (res.ok) setJournalEntries(await res.json());
    } catch (e) {
      console.error(e);
    }
  };

  const handleSendMessage = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!input.trim() || isStreaming) return;

    const userText = input.trim();
    setInput('');
    setMessages((prev) => [...prev, { role: 'user', content: userText }]);
    setIsStreaming(true);

    try {
      const response = await fetch('/api/chat/stream', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: userText }),
      });

      if (!response.body) return;
      const reader = response.body.getReader();
      const decoder = new TextDecoder();

      setMessages((prev) => [...prev, { role: 'assistant', content: '' }]);

      let assistantResponse = '';

      while (true) {
        const { value, done } = await reader.read();
        if (done) break;

        const chunk = decoder.decode(value);
        const lines = chunk.split('\n');

        for (const line of lines) {
          if (line.startsWith('data: ')) {
            const raw = line.slice(6);
            if (raw === '[DONE]') continue;
            try {
              const data = JSON.parse(raw);
              if (data.token) {
                assistantResponse += data.token;
                setMessages((prev) => {
                  const updated = [...prev];
                  updated[updated.length - 1] = {
                    role: 'assistant',
                    content: assistantResponse,
                  };
                  return updated;
                });
              } else if (data.content) {
                // Learned memory event
                fetchMemories();
              }
            } catch {
              // ignore plain text or sse heartbeats
            }
          }
        }
      }
    } catch (err) {
      console.error('Chat error:', err);
    } finally {
      setIsStreaming(false);
      fetchJournal();
    }
  };

  const handleCreateNote = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!newNoteTitle.trim()) return;
    try {
      await fetch('/api/notes', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title: newNoteTitle, content: newNoteContent }),
      });
      setNewNoteTitle('');
      setNewNoteContent('');
      fetchNotes();
      fetchJournal();
    } catch (err) {
      console.error(err);
    }
  };

  const handleDeleteNote = async (id: string) => {
    try {
      await fetch(`/api/notes/${id}`, { method: 'DELETE' });
      fetchNotes();
      fetchJournal();
    } catch (err) {
      console.error(err);
    }
  };

  const handleDeleteMemory = async (id: string) => {
    try {
      await fetch(`/api/memories/${id}`, { method: 'DELETE' });
      fetchMemories();
    } catch (err) {
      console.error(err);
    }
  };

  return (
    <div className="flex h-screen w-screen bg-neutral-950 text-neutral-100 font-sans">
      {/* Sidebar */}
      <aside className="w-64 border-r border-neutral-800 bg-neutral-900/60 p-4 flex flex-col justify-between">
        <div>
          <div className="flex items-center space-x-3 mb-6 px-2">
            <div className="h-9 w-9 rounded-xl bg-gradient-to-tr from-purple-600 to-indigo-500 flex items-center justify-center shadow-lg shadow-purple-500/20">
              <Sparkles className="h-5 w-5 text-white" />
            </div>
            <div>
              <h1 className="font-bold text-lg leading-tight tracking-wide text-neutral-50">ARCHER</h1>
              <p className="text-xs text-neutral-400 font-mono">v1.0 (Safety Core)</p>
            </div>
          </div>

          {/* Navigation */}
          <nav className="space-y-1">
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
              onClick={() => setActiveTab('notes')}
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
              onClick={() => setActiveTab('memories')}
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
              onClick={() => setActiveTab('journal')}
              className={`w-full flex items-center space-x-3 px-3 py-2.5 rounded-lg text-sm font-medium transition ${
                activeTab === 'journal'
                  ? 'bg-purple-600 text-white shadow-md'
                  : 'text-neutral-400 hover:text-white hover:bg-neutral-800/60'
              }`}
            >
              <Terminal className="h-4 w-4" />
              <span>Action Journal</span>
            </button>
          </nav>
        </div>

        {/* System status footer */}
        <div className="bg-neutral-900 border border-neutral-800 rounded-xl p-3 text-xs space-y-2">
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
      <main className="flex-1 flex flex-col h-full overflow-hidden bg-neutral-950">
        {/* Top Header */}
        <header className="h-14 border-b border-neutral-800 px-6 flex items-center justify-between bg-neutral-900/30 backdrop-blur">
          <div className="flex items-center space-x-2">
            <span className="text-sm text-neutral-400">Workspace:</span>
            <span className="text-sm font-semibold text-neutral-200">Archer V1 / Sricharan</span>
          </div>
          <div className="flex items-center space-x-3 text-xs">
            <span className="px-2.5 py-1 rounded-full bg-neutral-800 text-neutral-300 border border-neutral-700">
              Plugins: Approved Files, Notes, Brave Web Search
            </span>
          </div>
        </header>

        {/* View switching */}
        {activeTab === 'chat' && (
          <div className="flex-1 flex flex-col h-[calc(100%-3.5rem)]">
            {/* Messages */}
            <div className="flex-1 overflow-y-auto p-6 space-y-4">
              {messages.map((msg, i) => (
                <div
                  key={i}
                  className={`flex ${msg.role === 'user' ? 'justify-end' : 'justify-start'}`}
                >
                  <div
                    className={`max-w-2xl px-4 py-3 rounded-2xl text-sm leading-relaxed ${
                      msg.role === 'user'
                        ? 'bg-purple-600 text-white rounded-br-none shadow-md shadow-purple-600/10'
                        : 'bg-neutral-900 text-neutral-200 border border-neutral-800 rounded-bl-none shadow'
                    }`}
                  >
                    {msg.content || (isStreaming && i === messages.length - 1 ? (
                      <span className="animate-pulse">Thinking on Spark...</span>
                    ) : null)}
                  </div>
                </div>
              ))}
              <div ref={messagesEndRef} />
            </div>

            {/* Input bar */}
            <div className="p-4 border-t border-neutral-800 bg-neutral-900/40">
              <form onSubmit={handleSendMessage} className="max-w-3xl mx-auto flex items-center space-x-2">
                <input
                  type="text"
                  value={input}
                  onChange={(e) => setInput(e.target.value)}
                  placeholder="Ask Archer (e.g. 'Search for recent breakthroughs in robotic autonomy' or 'Remember that I prefer...') "
                  className="flex-1 bg-neutral-900 border border-neutral-700 focus:border-purple-500 rounded-xl px-4 py-3 text-sm focus:outline-none placeholder-neutral-500 transition"
                />
                <button
                  type="submit"
                  disabled={isStreaming || !input.trim()}
                  className="bg-purple-600 hover:bg-purple-500 disabled:opacity-40 disabled:hover:bg-purple-600 text-white px-5 py-3 rounded-xl text-sm font-semibold flex items-center space-x-1.5 transition shadow"
                >
                  <span>Send</span>
                  <Send className="h-4 w-4" />
                </button>
              </form>
            </div>
          </div>
        )}

        {activeTab === 'notes' && (
          <div className="flex-1 p-6 overflow-y-auto space-y-6">
            <div className="max-w-4xl mx-auto space-y-6">
              <div className="flex items-center justify-between">
                <div>
                  <h2 className="text-xl font-bold text-neutral-100">Personal Notes</h2>
                  <p className="text-xs text-neutral-400">Private, persistent notes stored locally on DGX Spark.</p>
                </div>
              </div>

              {/* Create Note Card */}
              <form onSubmit={handleCreateNote} className="bg-neutral-900 border border-neutral-800 rounded-xl p-4 space-y-3">
                <input
                  type="text"
                  value={newNoteTitle}
                  onChange={(e) => setNewNoteTitle(e.target.value)}
                  placeholder="Note Title..."
                  className="w-full bg-neutral-950 border border-neutral-800 rounded-lg px-3 py-2 text-sm focus:outline-none focus:border-purple-500"
                />
                <textarea
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
                  <div key={note.id} className="bg-neutral-900 border border-neutral-800 rounded-xl p-4 flex flex-col justify-between">
                    <div>
                      <h3 className="font-semibold text-neutral-200 text-sm mb-1">{note.title}</h3>
                      <p className="text-xs text-neutral-400 whitespace-pre-wrap">{note.content}</p>
                    </div>
                    <div className="flex items-center justify-between mt-4 pt-3 border-t border-neutral-800/80 text-[11px] text-neutral-500">
                      <span>{new Date(note.created_at).toLocaleDateString()}</span>
                      <button
                        onClick={() => handleDeleteNote(note.id)}
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

              <div className="space-y-3">
                {memories.map((mem) => (
                  <div key={mem.id} className="bg-neutral-900 border border-neutral-800 rounded-xl p-4 flex items-start justify-between">
                    <div className="space-y-1">
                      <div className="flex items-center space-x-2">
                        <span className="px-2 py-0.5 rounded text-[10px] font-mono uppercase bg-purple-950 text-purple-400 border border-purple-800/50">
                          {mem.memory_type}
                        </span>
                        <span className="text-[11px] text-neutral-500">Origin: {mem.origin}</span>
                      </div>
                      <p className="text-sm text-neutral-200 font-medium">{mem.content}</p>
                      <p className="text-xs text-neutral-400 italic">Evidence: "{mem.evidence}"</p>
                    </div>
                    <button
                      onClick={() => handleDeleteMemory(mem.id)}
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
