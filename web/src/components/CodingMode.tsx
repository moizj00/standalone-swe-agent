import React, { useState, useRef, useEffect } from 'react';
import { 
  Cpu, Database, Network, Send, Terminal, Settings, Copy, 
  RotateCcw, Plus, Trash2, Check, AlertCircle, RefreshCw, 
  BookOpen, ExternalLink, Code, FileCode
} from 'lucide-react';
import { Card, CardContent, CardHeader, CardTitle } from './ui/card';
import Markdown from 'react-markdown';
import { cn } from '../utils';
import { useToolSchemas } from '../store/ToolSchemasProvider';
import { callableSchemas } from '../store/toolSchemas';

type Role = 'user' | 'model';

interface ToolEvent {
  name: string;
  args?: any;
  result?: string;
}

interface Message {
  role: Role;
  parts: { text: string }[];
  tools?: ToolEvent[];
}

interface PropertyDetail {
  type: string;
  description: string;
}

interface ToolSchema {
  name: string;
  description: string;
  parameters: {
    type: 'OBJECT';
    properties: Record<string, PropertyDetail>;
    required?: string[];
  };
}

export const CodingMode = () => {
  const [activeTab, setActiveTab] = useState<'terminal' | 'tools' | 'vscode'>('terminal');
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState('');
  const [loading, setLoading] = useState(false);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const { schemas: customSchemas } = useToolSchemas();
  const endOfMessagesRef = useRef<HTMLDivElement>(null);

  // Tool Schema States
  const [toolsList, setToolsList] = useState<ToolSchema[]>([]);
  const [selectedTool, setSelectedTool] = useState<ToolSchema | null>(null);
  const [isAddingTool, setIsAddingTool] = useState(false);
  const [newToolName, setNewToolName] = useState('');
  const [newToolDesc, setNewToolDesc] = useState('');
  const [newToolParams, setNewToolParams] = useState<string>(
    '{\n  "path": {\n    "type": "STRING",\n    "description": "Path to target file or folder"\n  }\n}'
  );
  const [newToolRequired, setNewToolRequired] = useState<string>('path');
  const [schemaEditorContent, setSchemaEditorContent] = useState<string>('');
  const [schemaError, setSchemaError] = useState<string | null>(null);

  // VS Code states
  const [vsCodeStatus, setVsCodeStatus] = useState<any>(null);
  const [isInitializingVsCode, setIsInitializingVsCode] = useState(false);
  const [copiedFile, setCopiedFile] = useState<string | null>(null);
  const [activeVscodeFile, setActiveVscodeFile] = useState<'settings' | 'tasks' | 'extensions'>('settings');

  const fetchTools = async () => {
    try {
      const response = await fetch('/api/tools');
      if (response.ok) {
        const data = await response.json();
        setToolsList(data);
        if (data.length > 0) {
          setSelectedTool(data[0]);
        }
      }
    } catch (err) {
      console.error("Failed to load tools from backend:", err);
    }
  };

  const fetchVsCodeStatus = async () => {
    try {
      const response = await fetch('/api/vscode/status');
      if (response.ok) {
        const data = await response.json();
        setVsCodeStatus(data);
      }
    } catch (err) {
      console.error("Failed to fetch VS Code integration status:", err);
    }
  };

  useEffect(() => {
    fetchTools();
    fetchVsCodeStatus();
  }, []);

  useEffect(() => {
    endOfMessagesRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages, loading]);

  useEffect(() => {
    if (selectedTool) {
      setSchemaEditorContent(JSON.stringify(selectedTool, null, 2));
      setSchemaError(null);
    } else {
      setSchemaEditorContent('');
    }
  }, [selectedTool]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!input.trim() || loading) return;

    const userMessage: Message = { role: 'user', parts: [{ text: input }] };
    const newMessages = [...messages, userMessage];

    setMessages(newMessages);
    setInput('');
    setLoading(true);

    // Live state for the in-flight assistant turn, rebuilt into the trailing
    // model message on every Server-Sent Event. Tokens are committed per step so
    // multi-step reasoning renders as separate blocks instead of one run-on blob.
    const committed: string[] = [];   // completed steps' streamed text
    let stepBuf = '';                 // current step's streamed tokens
    let finalText: string | null = null;
    const liveTools: ToolEvent[] = [];

    const streamedSoFar = () => [...committed, stepBuf].filter(Boolean).join('\n\n');
    const render = () =>
      setMessages([...newMessages, {
        role: 'model',
        parts: [{ text: finalText ?? streamedSoFar() }],
        tools: [...liveTools],
      }]);

    const apply = (ev: any) => {
      switch (ev.type) {
        case 'session':
          setSessionId(ev.session_id);
          break;
        case 'step':
          // a new step began: commit the previous step's text as its own block
          if (stepBuf) { committed.push(stepBuf); stepBuf = ''; }
          break;
        case 'token':
          stepBuf += ev.text || '';
          render();
          break;
        case 'assistant':
          // emitted only when tokens were NOT streamed; use it as this step's text
          if (!stepBuf) { stepBuf = ev.content || ''; render(); }
          break;
        case 'tool_call':
          liveTools.push({ name: ev.name, args: ev.arguments });
          render();
          break;
        case 'tool_result': {
          // attach to the most recent matching tool that has no result yet
          const slot = [...liveTools].reverse().find(t => t.name === ev.name && t.result === undefined);
          if (slot) slot.result = ev.content;
          render();
          break;
        }
        case 'final':
          // Authoritative final answer; fall back to streamed text if final is empty.
          finalText = ev.text || streamedSoFar();
          render();
          break;
        case 'error':
          finalText = `[SYSTEM ERROR]: ${ev.message || 'agent error'}`;
          render();
          break;
      }
    };

    try {
      const response = await fetch('/api/chat/stream', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          messages: newMessages,
          session_id: sessionId ?? undefined,
          custom_tools: callableSchemas(customSchemas),
        }),
      });

      if (!response.ok || !response.body) {
        let msg = `Agent returned ${response.status}`;
        try { msg = (await response.json()).error || msg; } catch {}
        throw new Error(msg);
      }

      // Parse the SSE stream: events are separated by a blank line, payload on
      // "data: " lines.
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const chunks = buffer.split('\n\n');
        buffer = chunks.pop() || '';
        for (const chunk of chunks) {
          const line = chunk.split('\n').find(l => l.startsWith('data: '));
          if (!line) continue;
          try { apply(JSON.parse(line.slice(6))); } catch { /* ignore keep-alive */ }
        }
      }
    } catch (error: any) {
      console.error(error);
      liveText = `[SYSTEM ERROR]: ${error.message || 'Failed to connect to agent.'}`;
      render();
    } finally {
      setLoading(false);
    }
  };

  const handleSaveTools = async (listToSave: ToolSchema[]) => {
    try {
      const response = await fetch('/api/tools', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(listToSave)
      });
      if (response.ok) {
        setToolsList(listToSave);
      } else {
        const data = await response.json();
        alert(data.error || "Failed to save tools on backend.");
      }
    } catch (err: any) {
      alert("Error saving tools: " + err.message);
    }
  };

  const handleCreateTool = () => {
    if (!newToolName.trim() || !newToolDesc.trim()) {
      alert("Schema Name and Description are required.");
      return;
    }

    try {
      let parsedParams = {};
      try {
        parsedParams = JSON.parse(newToolParams);
      } catch (e) {
        alert("Parameters structure must be a valid JSON Object Map matching standard Tooling schemas (e.g. Type STRING, NUMBER).");
        return;
      }

      const formattedName = newToolName.trim().toLowerCase().replace(/\s+/g, '_');
      
      const exists = toolsList.some(t => t.name === formattedName);
      if (exists) {
        alert(`A tool named '${formattedName}' already exists in the workspace.`);
        return;
      }

      const requiredList = newToolRequired
        .split(',')
        .map(s => s.trim())
        .filter(Boolean);

      const newSchema: ToolSchema = {
        name: formattedName,
        description: newToolDesc.trim(),
        parameters: {
          type: 'OBJECT',
          properties: parsedParams,
          required: requiredList.length > 0 ? requiredList : undefined
        }
      };

      const updated = [...toolsList, newSchema];
      handleSaveTools(updated);
      setSelectedTool(newSchema);
      setIsAddingTool(false);

      // Reset fields
      setNewToolName('');
      setNewToolDesc('');
      setNewToolParams('{\n  "path": {\n    "type": "STRING",\n    "description": "Path to target file or folder"\n  }\n}');
      setNewToolRequired('path');
    } catch (err: any) {
      alert("Error generating tool: " + err.message);
    }
  };

  const handleUpdateSchemaText = () => {
    if (!selectedTool) return;
    try {
      const parsed = JSON.parse(schemaEditorContent);
      if (!parsed.name || !parsed.description || !parsed.parameters) {
        setSchemaError("Validation Error: Schema must include 'name', 'description' and 'parameters' fields.");
        return;
      }
      
      const updatedList = toolsList.map(t => t.name === selectedTool.name ? parsed : t);
      handleSaveTools(updatedList);
      setSelectedTool(parsed);
      setSchemaError(null);
      alert("Tool Schema schema successfully verified and persisted to target configuration.");
    } catch (e: any) {
      setSchemaError(`JSON Parse Error: ${e.message}`);
    }
  };

  const handleDeleteTool = (name: string) => {
    if (window.confirm(`Are you sure you want to remove '${name}' from active tool schemas?`)) {
      const updated = toolsList.filter(t => t.name !== name);
      handleSaveTools(updated);
      if (selectedTool?.name === name) {
        setSelectedTool(updated[0] || null);
      }
    }
  };

  const handleResetToDefaults = async () => {
    if (window.confirm("This will restore default developer file system schemas and status checks. Continue?")) {
      try {
        const response = await fetch('/api/tools', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify([])
        });
        if (response.ok) {
          fetchTools();
          alert("Default tool schemas restored successfully.");
        }
      } catch (err: any) {
        alert("Error resetting list: " + err.message);
      }
    }
  };

  const handleInitVsCode = async () => {
    setIsInitializingVsCode(true);
    try {
      const response = await fetch('/api/vscode/init', { method: 'POST' });
      const data = await response.json();
      if (response.ok) {
        fetchVsCodeStatus();
        alert(data.message || "VS Code integration successfully configured in .vscode folder!");
      } else {
        alert(data.error || "Failed to initialize folder structure.");
      }
    } catch (err: any) {
      alert("Error linking VS Code: " + err.message);
    } finally {
      setIsInitializingVsCode(false);
    }
  };

  const triggerCopy = (filename: string, text: string) => {
    navigator.clipboard.writeText(text);
    setCopiedFile(filename);
    setTimeout(() => setCopiedFile(null), 2500);
  };

  const renderTerminalTab = () => {
    return (
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6 flex-1 min-h-0">
        <Card className="lg:col-span-2 bg-slate-950 text-emerald-400 border-slate-800 font-mono shadow-xl relative overflow-hidden flex flex-col h-[550px]">
           {/* Top bar */}
           <div className="absolute top-0 left-0 w-full h-8 bg-slate-900 border-b border-slate-800 flex items-center px-4 justify-between shrink-0 z-10">
             <div className="flex items-center gap-2">
               <div className="w-3 h-3 rounded-full bg-rose-500" />
               <div className="w-3 h-3 rounded-full bg-amber-500" />
               <div className="w-3 h-3 rounded-full bg-emerald-500" />
               <span className="text-xs text-slate-500 ml-4">bash — ai-agent-terminal</span>
             </div>
             <span className="text-xs text-slate-600 font-sans font-medium">Port: 3000 online</span>
           </div>
           
           <CardContent className="p-6 pt-12 space-y-4 text-sm flex-1 overflow-y-auto w-full">
             <div className="space-y-2 opacity-80 border-b border-slate-900 pb-3">
               <p className="flex items-center gap-2 mt-2"><Cpu className="h-4 w-4 text-indigo-400 animate-pulse"/> Connecting to local SWE agent (Ollama)…</p>
               <p className="text-slate-500 pl-6 text-xs">Model bounds established with {toolsList.length} real tools.</p>
               <p className="flex items-center gap-2 mt-2"><Network className="h-4 w-4 text-emerald-400"/> System operational status bound.</p>
             </div>

             {/* Armed tool tags */}
             <div className="flex flex-wrap items-center gap-2 border-b border-slate-900 pb-4">
               <span className="text-xs text-slate-500 uppercase font-sans tracking-wide">Dynamic Toolsets:</span>
               {toolsList.map(tool => (
                 <span key={tool.name} className="text-xs font-mono bg-slate-900 text-indigo-300 font-semibold px-2 py-0.5 rounded border border-indigo-950 flex items-center gap-1 hover:bg-slate-800 cursor-help" title={tool.description}>
                   {tool.name}()
                 </span>
               ))}
               {toolsList.length === 0 && (
                 <span className="text-xs font-serif italic text-amber-500">No tools loaded (Standard Conversational Mode)</span>
               )}
             </div>

             {messages.length === 0 && (
               <div className="p-4 bg-slate-900/40 rounded border border-slate-900 text-slate-400 space-y-2 font-sans mt-2">
                 <p className="font-semibold text-slate-300 flex items-center gap-2">
                   <BookOpen className="h-4 w-4 text-indigo-400" />
                   AI Workspace Terminal Guide
                 </p>
                 <p className="text-xs leading-relaxed">
                   Ask the AI engineer to explore the project structure, compile the code, create new files, or run statuses. The agent will automatically call your <strong>custom Tool Schemas</strong> registered next door in the Registry.
                 </p>
                 <div className="text-xs bg-slate-950 font-mono p-2 rounded text-indigo-400">
                   Try: "Show me files in the root folder and list files"
                 </div>
               </div>
             )}

             {messages.map((msg, idx) => (
               <div key={idx} className={`pt-4 border-t border-slate-900/50 ${msg.role === 'user' ? 'text-slate-300' : 'text-emerald-400'}`}>
                 <div className="flex items-start gap-3 w-full">
                   <span className={msg.role === 'user' ? "text-rose-400 mt-0.5" : "text-emerald-500 mt-0.5"}>
                     {msg.role === 'user' ? '❯' : '🤖'}
                   </span>
                   <div className="flex-1 w-full overflow-hidden">
                     {msg.role === 'user' ? (
                       <p className="font-semibold whitespace-pre-wrap">{msg.parts[0].text}</p>
                     ) : (
                       <div className="space-y-2">
                         {msg.tools && msg.tools.length > 0 && (
                           <div className="space-y-1.5">
                             {msg.tools.map((t, i) => (
                               <div key={i} className="text-xs font-mono bg-slate-900/70 border border-slate-800 rounded px-2 py-1">
                                 <div className="flex items-center gap-2 text-indigo-300">
                                   <Code className="h-3 w-3 shrink-0" />
                                   <span className="font-semibold">{t.name}</span>
                                   <span className="text-slate-500 truncate">{t.args ? JSON.stringify(t.args) : ''}</span>
                                 </div>
                                 {t.result !== undefined && (
                                   <pre className="mt-1 text-[11px] text-slate-400 whitespace-pre-wrap break-words max-h-40 overflow-y-auto">{t.result.length > 600 ? t.result.slice(0, 600) + ' …' : t.result}</pre>
                                 )}
                               </div>
                             ))}
                           </div>
                         )}
                         {msg.parts[0].text && (
                           <div className="markdown-body prose prose-invert prose-emerald max-w-none text-sm break-words">
                             <Markdown>{msg.parts[0].text}</Markdown>
                           </div>
                         )}
                       </div>
                     )}
                   </div>
                 </div>
               </div>
             ))}
             
             {loading && (
               <div className="pt-4 border-t border-slate-900/50 text-slate-500 flex flex-col items-start gap-2">
                 <div className="flex gap-2 items-center">
                    <span className="text-emerald-500">🤖</span> 
                    <span className="animate-pulse">Agent is thinking, reviewing schemas, and planning workspace steps...</span>
                 </div>
               </div>
             )}
             
             <div ref={endOfMessagesRef} />
           </CardContent>

           <div className="p-4 bg-slate-900 border-t border-slate-800 shrink-0">
             <form onSubmit={handleSubmit} className="flex items-center gap-3">
               <span className="text-rose-400">❯</span>
               <input 
                 type="text" 
                 value={input}
                 onChange={e => setInput(e.target.value)}
                 disabled={loading}
                 className="bg-transparent border-none outline-none text-slate-100 flex-1 placeholder:text-slate-600 focus:ring-0 disabled:opacity-50" 
                 placeholder="Instruct the agent (e.g., 'create a readme', 'run build')..." 
               />
               <button type="submit" disabled={!input.trim() || loading} className="text-slate-500 hover:text-emerald-400 disabled:opacity-50 disabled:cursor-not-allowed transition-colors">
                 <Send className="h-4 w-4" />
               </button>
             </form>
           </div>
        </Card>

        <div className="flex flex-col gap-6">
           <Card className="bg-white dark:bg-slate-900 shadow-md">
             <CardHeader className="pb-3">
               <CardTitle className="text-sm flex items-center gap-2 text-slate-500">
                 <Database className="h-4 w-4" />
                 Environment State
               </CardTitle>
             </CardHeader>
             <CardContent>
                <div className="space-y-4 text-sm font-sans">
                  <div className="flex justify-between items-center">
                    <span className="font-medium text-slate-700 dark:text-slate-300">Agent Backend</span>
                    <span className="text-emerald-500 bg-emerald-50 dark:bg-emerald-950/40 px-2 py-0.5 rounded text-xs border border-emerald-500/10">Ollama (local)</span>
                  </div>
                  <div className="flex justify-between items-center">
                    <span className="font-medium text-slate-700 dark:text-slate-300">Model Engine</span>
                    <span className="text-indigo-500 bg-indigo-50 dark:bg-indigo-950/40 px-2 py-0.5 rounded text-xs font-mono border border-indigo-500/10">qwen2.5-coder-tools</span>
                  </div>
                  <div className="flex justify-between items-center">
                    <span className="font-medium text-slate-700 dark:text-slate-300">Session</span>
                    <span className="text-amber-500 bg-amber-50 dark:bg-amber-950/40 px-2 py-0.5 rounded text-xs font-mono border border-amber-500/10">{sessionId ? sessionId.slice(0, 12) : 'new'}</span>
                  </div>
                  <div className="flex justify-between items-center border-t border-slate-100 dark:border-slate-800 pt-3">
                    <span className="font-medium text-slate-700 dark:text-slate-300">VS Code Configs</span>
                    <span className={vsCodeStatus?.isInitialized ? "text-emerald-500 font-semibold" : "text-slate-400 font-serif italic"}>
                      {vsCodeStatus?.isInitialized ? "Active" : "Unconnected"}
                    </span>
                  </div>
                </div>
             </CardContent>
           </Card>

           <Card className="bg-white dark:bg-slate-900 shadow-md flex-1">
             <CardHeader className="pb-2">
               <CardTitle className="text-sm flex items-center gap-2 text-teal-600 dark:text-teal-400">
                 <BookOpen className="h-4 w-4" />
                 Integrated Developer Actions
               </CardTitle>
             </CardHeader>
             <CardContent className="space-y-3 text-xs text-slate-500 pt-2 font-sans">
                <p>Because the applet runs in an active sandbox workspace, actions taken in this terminal directly impact real assets:</p>
                <div className="bg-slate-50 dark:bg-slate-950 rounded p-3 border border-slate-100 dark:border-slate-850 font-mono text-[11px] text-slate-600 dark:text-slate-300 space-y-1">
                  <div>CWD: <span className="text-slate-500">{vsCodeStatus?.env?.cwd || "/app/applet"}</span></div>
                  <div>OS: <span className="text-slate-500">{vsCodeStatus?.env?.os || "linux"}</span></div>
                  <div>PORT: <span className="text-indigo-400">3000 (Forwarded Proxy)</span></div>
                </div>
                <div className="border-t border-slate-100 dark:border-slate-850 pt-2 text-indigo-600 dark:text-indigo-400 font-semibold hover:underline cursor-pointer flex items-center gap-1" onClick={() => setActiveTab('vscode')}>
                  View setup rules in VS Code tab &rarr;
                </div>
             </CardContent>
           </Card>
        </div>
      </div>
    );
  };

  const renderToolsTab = () => {
    return (
      <div className="space-y-6">
        <div className="flex flex-col md:flex-row justify-between items-start md:items-center gap-4">
          <div>
            <h3 className="text-lg font-bold tracking-tight">Gemini Tool Schema Registry</h3>
            <p className="text-sm text-slate-500 dark:text-slate-400">Develop, register, and inspect function declarations that Gemini can execute natively.</p>
          </div>
          <div className="flex gap-3">
            <button onClick={handleResetToDefaults} className="flex items-center gap-2 text-xs font-semibold px-3 py-2 border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 rounded-lg hover:bg-slate-50 dark:hover:bg-slate-800 transition-colors">
              <RotateCcw className="h-3.5 w-3.5" /> Restore Defaults
            </button>
            <button onClick={() => setIsAddingTool(!isAddingTool)} className="flex items-center gap-2 text-xs font-semibold px-3 py-2 bg-indigo-600 text-white hover:bg-indigo-700 rounded-lg transition-colors">
              <Plus className="h-3.5 w-3.5" /> Register New Tool
            </button>
          </div>
        </div>

        {isAddingTool && (
          <Card className="bg-slate-50 dark:bg-slate-900/50 border-indigo-200 dark:border-indigo-900 shadow">
            <CardHeader className="pb-3 border-b border-slate-150 dark:border-slate-800/80">
              <CardTitle className="text-sm flex items-center gap-2 text-indigo-600 dark:text-indigo-400">
                <Plus className="h-4 w-4" /> Create Custom Tool Schema
              </CardTitle>
            </CardHeader>
            <CardContent className="p-6 space-y-4">
              <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                <div className="space-y-1">
                  <label className="text-xs font-bold text-slate-600 dark:text-slate-400 uppercase">Schema (Function) Name</label>
                  <input 
                    type="text" 
                    value={newToolName}
                    onChange={e => setNewToolName(e.target.value)}
                    placeholder="e.g. check_git_history"
                    className="w-full bg-white dark:bg-slate-900 border border-slate-200 dark:border-slate-850 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500"
                  />
                  <p className="text-[10px] text-slate-400 font-mono">Will be normalized to lowercase with underscores.</p>
                </div>
                <div className="space-y-1">
                  <label className="text-xs font-bold text-slate-600 dark:text-slate-400 uppercase">Required Arguments</label>
                  <input 
                    type="text" 
                    value={newToolRequired}
                    onChange={e => setNewToolRequired(e.target.value)}
                    placeholder="e.g. path, limit"
                    className="w-full bg-white dark:bg-slate-900 border border-slate-200 dark:border-slate-850 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500"
                  />
                  <p className="text-[10px] text-slate-400">Comma-separated field names that Gemini MUST supply.</p>
                </div>
              </div>

              <div className="space-y-1">
                <label className="text-xs font-bold text-slate-600 dark:text-slate-400 uppercase">Function Purpose / Instructions for LLM</label>
                <input 
                  type="text" 
                  value={newToolDesc}
                  onChange={e => setNewToolDesc(e.target.value)}
                  placeholder="Describes exactly what this tool does, and guides Gemini on when to invoke it."
                  className="w-full bg-white dark:bg-slate-900 border border-slate-200 dark:border-slate-850 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500"
                />
              </div>

              <div className="space-y-1">
                <label className="text-xs font-bold text-slate-600 dark:text-slate-400 uppercase flex justify-between">
                  <span>Parameter Structure JSON schema map</span>
                  <span className="text-[10px] text-indigo-400 lowercase font-mono">Type.OBJECT property details</span>
                </label>
                <textarea 
                  value={newToolParams}
                  onChange={e => setNewToolParams(e.target.value)}
                  rows={4}
                  className="w-full bg-white dark:bg-slate-950 font-mono border border-slate-200 dark:border-slate-850 rounded-lg px-3 py-2 text-xs focus:outline-none focus:ring-2 focus:ring-indigo-500"
                />
              </div>

              <div className="flex justify-end gap-3 pt-2">
                <button onClick={() => setIsAddingTool(false)} className="px-3 py-1.5 text-xs font-medium border border-slate-200 dark:border-slate-700 rounded-lg hover:bg-slate-50 dark:hover:bg-slate-800 transition-colors">
                  Cancel
                </button>
                <button onClick={handleCreateTool} className="px-4 py-1.5 text-xs font-medium bg-indigo-600 text-white hover:bg-indigo-700 rounded-lg transition-colors">
                  Save to Registry
                </button>
              </div>
            </CardContent>
          </Card>
        )}

        <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
          {/* Left panel - schemas list */}
          <div className="space-y-3">
            <div className="text-xs font-bold text-slate-400 uppercase tracking-wider px-1">Registered Schemas</div>
            <div className="space-y-2 max-h-[480px] overflow-y-auto pr-2">
              {toolsList.map(t => (
                <div 
                  key={t.name}
                  onClick={() => setSelectedTool(t)}
                  className={cn(
                    "p-3 rounded-lg border transition-all cursor-pointer flex justify-between items-center group shadow-sm bg-white dark:bg-slate-900/60",
                    selectedTool?.name === t.name 
                      ? "border-indigo-600 bg-indigo-50/40 dark:border-indigo-500 dark:bg-indigo-950/20" 
                      : "border-slate-200 hover:border-slate-300 dark:border-slate-800 dark:hover:border-slate-700"
                  )}
                >
                  <div className="flex-1 overflow-hidden pr-2">
                    <div className="font-mono text-sm font-semibold truncate text-slate-850 dark:text-slate-100 flex items-center gap-1.5">
                      <span className="w-1.5 h-1.5 rounded-full bg-indigo-500" />
                      {t.name}()
                    </div>
                    <div className="text-xs text-slate-400 dark:text-slate-505 truncate mt-0.5">{t.description}</div>
                  </div>
                  <button 
                    onClick={(e) => {
                      e.stopPropagation();
                      handleDeleteTool(t.name);
                    }} 
                    className="text-slate-400 hover:text-rose-500 p-1 opacity-0 group-hover:opacity-100 transition-all focus:opacity-100"
                    title="Delete tool schema"
                  >
                    <Trash2 className="h-4 w-4" />
                  </button>
                </div>
              ))}
              {toolsList.length === 0 && (
                <div className="p-4 border border-dashed border-slate-200 dark:border-slate-800 rounded-lg text-center text-slate-400 text-xs italic">
                  No Tool Schemas loaded. Click Register to define custom agent behaviors!
                </div>
              )}
            </div>
          </div>

          {/* Right panel - detail sheet */}
          <div className="md:col-span-2 space-y-3">
            <div className="text-xs font-bold text-slate-400 uppercase tracking-wider px-1">JSON Schema Definition (Gemini Native)</div>
            
            {selectedTool ? (
              <Card className="bg-white dark:bg-slate-900 shadow border-slate-200 dark:border-slate-800 flex flex-col h-[480px]">
                <CardHeader className="py-3.5 border-b border-slate-100 dark:border-slate-800/80 flex flex-row items-center justify-between">
                  <div>
                    <CardTitle className="font-mono text-sm font-bold flex items-center gap-2 text-indigo-600 dark:text-indigo-400">
                      <FileCode className="h-4 w-4" />
                      {selectedTool.name}
                    </CardTitle>
                    <p className="text-[10px] text-slate-400 mt-0.5 font-sans font-medium">Auto-synced with workspace core configurations.</p>
                  </div>
                  <div className="flex gap-2">
                    <button 
                      onClick={handleUpdateSchemaText}
                      className="text-xs font-bold px-3 py-1 border border-indigo-500/20 text-indigo-600 dark:text-indigo-400 bg-indigo-50/50 dark:bg-indigo-950/20 rounded hover:bg-indigo-100 transition-colors"
                    >
                      Verify & Apply Changes
                    </button>
                  </div>
                </CardHeader>
                <CardContent className="p-4 flex-1 flex flex-col min-h-0 bg-slate-950">
                  <span className="text-[10px] text-indigo-400 uppercase font-mono font-semibold tracking-wider mb-2 shrink-0">Editable JSON Declaration</span>
                  <div className="flex-grow min-y-0 h-full">
                    <textarea 
                      value={schemaEditorContent}
                      onChange={e => setSchemaEditorContent(e.target.value)}
                      className="w-full h-full bg-transparent text-emerald-300 font-mono text-xs border-none focus:ring-0 p-0 outline-none resize-none"
                    />
                  </div>
                  {schemaError && (
                    <div className="mt-2 p-2 bg-rose-950/60 border border-rose-900 text-rose-300 text-xs rounded-md flex items-center gap-2 font-sans">
                      <AlertCircle className="h-4 w-4 flex-shrink-0" />
                      <span>{schemaError}</span>
                    </div>
                  )}
                </CardContent>
              </Card>
            ) : (
              <div className="p-8 text-center border rounded-lg bg-slate-50 dark:bg-slate-900/40 text-slate-400 italic text-sm border-slate-200 dark:border-slate-800">
                Select a schema from the registry menu to view parameters details or edit schema files.
              </div>
            )}
          </div>
        </div>
      </div>
    );
  };

  const renderVscodeTab = () => {
    const settingsJson = vsCodeStatus ? `{
  "workbench.colorCustomizations": {
    "activityBar.background": "#1e1b4b",
    "titleBar.activeBackground": "#1e1b4b",
    "titleBar.activeForeground": "#f8fafc"
  },
  "editor.fontFamily": "'JetBrains Mono', 'Fira Code', monospace",
  "editor.fontSize": 14,
  "editor.lineHeight": 22,
  "editor.tabSize": 2,
  "editor.insertSpaces": true,
  "editor.formatOnSave": true,
  "files.exclude": {
    "**/.git": true,
    "**/node_modules": true,
    "**/dist": true
  }
}` : 'Run VS Code Initializer to generate configurator...';

    const tasksJson = vsCodeStatus ? `{
  "version": "2.0.0",
  "tasks": [
    {
      "label": "AI Studio: Start Development Server",
      "type": "shell",
      "command": "npm run dev",
      "group": "active",
      "presentation": {
        "reveal": "always",
        "panel": "new"
      }
    },
    {
      "label": "AI Studio: Compile & Build Application",
      "type": "shell",
      "command": "npm run build",
      "group": {
        "kind": "build",
        "isDefault": true
      }
    }
  ]
}` : 'Run VS Code Initializer to generate tasks...';

    const extensionsJson = vsCodeStatus ? `{
  "recommendations": [
    "bradlc.vscode-tailwindcss",
    "dbaeumer.vscode-eslint",
    "esbenp.prettier-vscode",
    "google.gemini-vscode"
  ]
}` : 'Run VS Code Initializer to generate extensions...';

    let activeCodeBlock = settingsJson;
    let activeFileName = ".vscode/settings.json";
    if (activeVscodeFile === 'tasks') {
      activeCodeBlock = tasksJson;
      activeFileName = ".vscode/tasks.json";
    } else if (activeVscodeFile === 'extensions') {
      activeCodeBlock = extensionsJson;
      activeFileName = ".vscode/extensions.json";
    }

    return (
      <div className="space-y-6">
        <div>
          <h3 className="text-lg font-bold tracking-tight">VS Code Workspace Integration</h3>
          <p className="text-sm text-slate-500 dark:text-slate-400">Configure, monitor, and sync with your local IDE so the web environment communicates fully with VS Code.</p>
        </div>

        <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
          <div className="lg:col-span-2 space-y-6">
            <Card className="bg-white dark:bg-slate-900 border-slate-200 dark:border-slate-850 shadow-md">
              <CardHeader className="border-b border-slate-100 dark:border-slate-800/80 flex flex-row justify-between items-center py-4">
                <CardTitle className="text-sm font-semibold flex items-center gap-2">
                  <FileCode className="h-4.5 w-4.5 text-indigo-500" />
                  Generated Workspace Config files
                </CardTitle>
                <div className="flex border border-slate-200 dark:border-slate-700 rounded-lg overflow-hidden text-xs">
                  <button onClick={() => setActiveVscodeFile('settings')} className={cn("px-3 py-1.5 transition-colors", activeVscodeFile === 'settings' ? "bg-indigo-600 text-white font-medium" : "bg-white text-slate-600 hover:bg-slate-50 dark:bg-slate-900 dark:text-slate-400")}>settings.json</button>
                  <button onClick={() => setActiveVscodeFile('tasks')} className={cn("px-3 py-1.5 transition-colors", activeVscodeFile === 'tasks' ? "bg-indigo-600 text-white font-medium" : "bg-white text-slate-600 hover:bg-slate-50 dark:bg-slate-900 dark:text-slate-400")}>tasks.json</button>
                  <button onClick={() => setActiveVscodeFile('extensions')} className={cn("px-3 py-1.5 transition-colors", activeVscodeFile === 'extensions' ? "bg-indigo-600 text-white font-medium" : "bg-white text-slate-600 hover:bg-slate-50 dark:bg-slate-900 dark:text-slate-400")}>extensions.json</button>
                </div>
              </CardHeader>
              <CardContent className="p-0 bg-slate-950 relative h-[320px] flex flex-col">
                <div className="bg-slate-900 text-[11px] text-slate-500 px-4 py-2 flex justify-between items-center select-none font-mono">
                  <span>{activeFileName}</span>
                  <button onClick={() => triggerCopy(activeFileName, activeCodeBlock)} className="flex items-center gap-1 hover:text-white transition-colors">
                    {copiedFile === activeFileName ? <Check className="h-3.5 w-3.5 text-emerald-400" /> : <Copy className="h-3.5 w-3.5" />}
                    <span>{copiedFile === activeFileName ? "Copied!" : "Copy"}</span>
                  </button>
                </div>
                <div className="p-4 flex-grow overflow-y-auto font-mono text-xs text-emerald-300">
                  <pre className="whitespace-pre">{activeCodeBlock}</pre>
                </div>
              </CardContent>
            </Card>

            <div className="bg-slate-100 dark:bg-slate-900 p-4 rounded-lg border border-slate-200 dark:border-slate-800 space-y-3 font-sans">
              <h4 className="font-bold text-slate-800 dark:text-slate-100 flex items-center gap-2">
                <BookOpen className="h-4.5 w-4.5 text-indigo-500" />
                How the application connects to the environment
              </h4>
              <p className="text-xs text-slate-500 leading-relaxed">
                By clicking <strong>Initialize VS Code Workspaces</strong>, ProjectBoard drops real configurations inside the filesystem. Because a standard container shell runs the dev server inside port <code className="bg-slate-200 dark:bg-slate-800 px-1 py-0.5 rounded font-mono text-rose-500">3000</code>, VS Code or Remote attaches can safely bind port forwards.
              </p>
              <div className="p-3 bg-white dark:bg-slate-950 rounded-lg border border-slate-200/60 dark:border-slate-850/80 space-y-1.5 text-xs text-slate-600 dark:text-slate-300">
                <p>To hook up with your local desktop:</p>
                <ol className="list-decimal pl-4 space-y-1 mt-1 font-sans text-xs">
                  <li>Attach VS Code using the <strong>Dev Containers</strong> extension, pointing directly to this environment.</li>
                  <li>VS Code will read your newly generated task schemas (<code className="font-mono text-[11px]">tasks.json</code>) to spin up compilation, server restarts, and lint systems natively!</li>
                  <li>The local model will coordinate seamlessly using the active Gemini key mapped in the sandbox.</li>
                </ol>
              </div>
            </div>
          </div>

          <div className="space-y-6">
            <Card className="bg-white dark:bg-slate-900 shadow-md">
              <CardHeader>
                <CardTitle className="text-sm font-semibold">Workspace Checkpoints</CardTitle>
              </CardHeader>
              <CardContent className="space-y-4 pt-2 font-sans text-xs text-slate-600 dark:text-slate-300">
                <div className="flex gap-3 items-start">
                  <div className={cn("w-5 h-5 rounded-full flex items-center justify-center shrink-0 border text-[10px] font-bold", vsCodeStatus?.files?.settings ? "bg-emerald-50 border-emerald-500 text-emerald-600" : "bg-slate-50 border-slate-300 text-slate-500")}>
                    {vsCodeStatus?.files?.settings ? "✓" : "1"}
                  </div>
                  <div>
                    <div className="font-semibold text-slate-800 dark:text-slate-100">Settings Config Created</div>
                    <div className="text-[10px] text-slate-500 font-mono mt-0.5">.vscode/settings.json</div>
                  </div>
                </div>

                <div className="flex gap-3 items-start">
                  <div className={cn("w-5 h-5 rounded-full flex items-center justify-center shrink-0 border text-[10px] font-bold", vsCodeStatus?.files?.tasks ? "bg-emerald-50 border-emerald-500 text-emerald-600" : "bg-slate-50 border-slate-300 text-slate-500")}>
                    {vsCodeStatus?.files?.tasks ? "✓" : "2"}
                  </div>
                  <div>
                    <div className="font-semibold text-slate-800 dark:text-slate-100">Tasks Config Synthesized</div>
                    <div className="text-[10px] text-slate-500 font-mono mt-0.5">.vscode/tasks.json</div>
                  </div>
                </div>

                <div className="flex gap-3 items-start">
                  <div className={cn("w-5 h-5 rounded-full flex items-center justify-center shrink-0 border text-[10px] font-bold", vsCodeStatus?.files?.extensions ? "bg-emerald-50 border-emerald-500 text-emerald-600" : "bg-slate-50 border-slate-300 text-slate-500")}>
                    {vsCodeStatus?.files?.extensions ? "✓" : "3"}
                  </div>
                  <div>
                    <div className="font-semibold text-slate-800 dark:text-slate-100">Recommended Extensions File</div>
                    <div className="text-[10px] text-slate-500 font-mono mt-0.5">.vscode/extensions.json</div>
                  </div>
                </div>

                <div className="flex gap-3 items-start">
                  <div className="w-5 h-5 rounded-full bg-emerald-50 border-emerald-500 text-emerald-600 flex items-center justify-center shrink-0 border text-[10px] font-bold">✓</div>
                  <div>
                    <div className="font-semibold text-slate-800 dark:text-slate-100">External Reverse Proxy Hub</div>
                    <div className="text-[10px] text-slate-500 font-mono mt-0.5">Port 3000 mapping validated</div>
                  </div>
                </div>

                <button 
                  onClick={handleInitVsCode}
                  disabled={isInitializingVsCode}
                  className="w-full bg-indigo-600 text-white font-medium py-2 rounded-lg text-xs hover:bg-indigo-700 disabled:opacity-50 transition-colors flex items-center justify-center gap-2 mt-4"
                >
                  <RefreshCw className={cn("h-3.5 w-3.5", isInitializingVsCode && "animate-spin")} />
                  {vsCodeStatus?.isInitialized ? "Re-initialize VS Code Workspace" : "Initialize VS Code Workspace"}
                </button>
              </CardContent>
            </Card>

            <Card className="bg-slate-50 dark:bg-slate-900/40 border-slate-200 dark:border-slate-800 flex flex-col p-4 space-y-3 shadow-inner">
              <div className="flex items-center gap-2 text-indigo-600 dark:text-indigo-400 font-bold text-xs uppercase tracking-wide">
                <Terminal className="h-4 w-4" />
                Connection Link
              </div>
              <p className="text-[11px] text-slate-500 leading-relaxed font-sans">
                You can invoke VS Code open protocol on your local workstation pointing directly to this workspace payload:
              </p>
              <div className="bg-slate-100 dark:bg-slate-950 rounded p-2 border border-slate-200 dark:border-slate-850 font-mono text-[10px] flex justify-between items-center text-slate-650 dark:text-slate-300">
                <span className="truncate mr-2">vscode://file/app/applet</span>
                <button 
                  onClick={() => triggerCopy('link', "vscode://file/app/applet")} 
                  className="text-indigo-500 hover:text-indigo-400 font-sans text-xs underline"
                >
                  Copy
                </button>
              </div>
            </Card>
          </div>
        </div>
      </div>
    );
  };

  return (
    <div className="space-y-6 flex flex-col h-full max-h-[calc(100vh-8rem)]">
      <div className="flex flex-col md:flex-row justify-between items-start md:items-end gap-2 pr-1">
        <div>
           <h2 className="text-2xl font-bold tracking-tight text-slate-900 dark:text-slate-100">Coding Workspace Control Center</h2>
           <p className="text-sm text-slate-500 dark:text-slate-400">Integrated bash terminal with dynamic Gemini Tool Schema planner and IDE adapters.</p>
        </div>
      </div>

      <div className="flex border-b border-slate-200 dark:border-slate-800 shrink-0 gap-6">
        <button 
          onClick={() => setActiveTab('terminal')} 
          className={cn(
            "pb-3 text-sm font-medium border-b-2 px-1 transition-colors flex items-center gap-2",
            activeTab === 'terminal' 
              ? "border-indigo-600 text-indigo-600 dark:border-indigo-400 dark:text-indigo-400 font-semibold" 
              : "border-transparent text-slate-500 hover:text-slate-700 dark:text-slate-400 dark:hover:text-slate-200"
          )}
        >
          <Terminal className="h-4 w-4" />
          Terminal Agent
        </button>
        <button 
          onClick={() => setActiveTab('tools')} 
          className={cn(
            "pb-3 text-sm font-medium border-b-2 px-1 transition-colors flex items-center gap-2",
            activeTab === 'tools' 
              ? "border-indigo-600 text-indigo-600 dark:border-indigo-400 dark:text-indigo-400 font-semibold" 
              : "border-transparent text-slate-500 hover:text-slate-700 dark:text-slate-400 dark:hover:text-slate-200"
          )}
        >
          <Settings className="h-4 w-4" />
          Tool Schema Registry {toolsList.length > 0 && `(${toolsList.length})`}
        </button>
        <button 
          onClick={() => setActiveTab('vscode')} 
          className={cn(
            "pb-3 text-sm font-medium border-b-2 px-1 transition-colors flex items-center gap-2",
            activeTab === 'vscode' 
              ? "border-indigo-600 text-indigo-600 dark:border-indigo-400 dark:text-indigo-400 font-semibold" 
              : "border-transparent text-slate-500 hover:text-slate-700 dark:text-slate-400 dark:hover:text-slate-200"
          )}
        >
          <Code className="h-4 w-4" />
          VS Code Integration
        </button>
      </div>

      <div className="flex-grow min-h-0">
        {activeTab === 'terminal' && renderTerminalTab()}
        {activeTab === 'tools' && renderToolsTab()}
        {activeTab === 'vscode' && renderVscodeTab()}
      </div>
    </div>
  );
};
