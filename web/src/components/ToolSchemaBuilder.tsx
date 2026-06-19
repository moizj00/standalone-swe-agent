import React, { useMemo, useState } from 'react';
import {
  Wrench, Plus, Trash2, Copy, Check, Save, X, AlertCircle, Files, FileJson,
} from 'lucide-react';
import { Card, CardContent, CardHeader, CardTitle } from './ui/card';
import { cn } from '../utils';
import { useToolSchemas } from '../store/ToolSchemasProvider';
import {
  DraftSchema, HTTP_METHODS, HttpMethod, PARAM_LOCATIONS, PARAM_TYPES, ParamLocation,
  ParamType, ToolParameter, emptyDraft, emptyHttp, emptyParameter, exportDeclarations,
  makeId, toFunctionDeclaration, validateDraft,
} from '../store/toolSchemas';

/**
 * Define custom API tool schemas (name, description, typed parameters) for the
 * LLM agent. State lives in the tool-schema slice (localStorage-backed); this
 * component is a thin editor over that slice. Schemas serialize to the same
 * Gemini `functionDeclaration` shape the agent's /api/tools speaks, so they can
 * be copied straight into a tools registry.
 */
export const ToolSchemaBuilder = () => {
  const { schemas, addSchema, updateSchema, removeSchema, duplicateSchema, clearSchemas } = useToolSchemas();

  const [draft, setDraft] = useState<DraftSchema | null>(null);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [copied, setCopied] = useState<string | null>(null);

  const errors = useMemo(
    () => (draft ? validateDraft(draft, schemas, editingId) : []),
    [draft, schemas, editingId],
  );

  const preview = useMemo(
    () => (draft ? JSON.stringify(toFunctionDeclaration(draft), null, 2) : ''),
    [draft],
  );

  // -- draft lifecycle ------------------------------------------------------

  const startNew = () => {
    setEditingId(null);
    setDraft({ ...emptyDraft(), parameters: [emptyParameter()] });
  };

  const startEdit = (id: string) => {
    const s = schemas.find(x => x.id === id);
    if (!s) return;
    setEditingId(id);
    setDraft({
      name: s.name,
      description: s.description,
      parameters: s.parameters.map(p => ({ ...p })),
      // preserve endpoint config — otherwise saving an edit would drop it and the
      // tool would silently become non-callable
      http: s.http
        ? {
            ...s.http,
            headers: s.http.headers.map(h => ({ ...h })),
            auth: s.http.auth ? { ...s.http.auth } : undefined,
          }
        : undefined,
    });
  };

  const cancel = () => { setDraft(null); setEditingId(null); };

  const save = () => {
    if (!draft || errors.length) return;
    if (editingId) updateSchema(editingId, draft);
    else addSchema(draft);
    cancel();
  };

  // -- parameter editing ----------------------------------------------------

  const patchDraft = (patch: Partial<DraftSchema>) =>
    setDraft(d => (d ? { ...d, ...patch } : d));

  const addParam = () =>
    setDraft(d => (d ? { ...d, parameters: [...d.parameters, emptyParameter()] } : d));

  const updateParam = (pid: string, patch: Partial<ToolParameter>) =>
    setDraft(d => (d ? { ...d, parameters: d.parameters.map(p => (p.id === pid ? { ...p, ...patch } : p)) } : d));

  const removeParam = (pid: string) =>
    setDraft(d => (d ? { ...d, parameters: d.parameters.filter(p => p.id !== pid) } : d));

  // -- endpoint editing -----------------------------------------------------

  const setHttp = (patch: Partial<NonNullable<DraftSchema['http']>>) =>
    setDraft(d => (d ? { ...d, http: { ...(d.http ?? emptyHttp()), ...patch } } : d));

  const toggleHttp = (on: boolean) =>
    setDraft(d => (d ? { ...d, http: on ? (d.http ?? emptyHttp()) : undefined } : d));

  const addHeader = () =>
    setDraft(d => (d && d.http ? { ...d, http: { ...d.http, headers: [...d.http.headers, { id: makeId(), key: '', value: '' }] } } : d));

  const updateHeader = (hid: string, patch: Partial<{ key: string; value: string }>) =>
    setDraft(d => (d && d.http ? { ...d, http: { ...d.http, headers: d.http.headers.map(h => (h.id === hid ? { ...h, ...patch } : h)) } } : d));

  const removeHeader = (hid: string) =>
    setDraft(d => (d && d.http ? { ...d, http: { ...d.http, headers: d.http.headers.filter(h => h.id !== hid) } } : d));

  // -- clipboard ------------------------------------------------------------

  const copy = (key: string, text: string) => {
    navigator.clipboard?.writeText(text).then(
      () => { setCopied(key); setTimeout(() => setCopied(c => (c === key ? null : c)), 1500); },
      () => {},
    );
  };

  const copyAll = () => copy('all', JSON.stringify(exportDeclarations(schemas), null, 2));

  // -- render ---------------------------------------------------------------

  return (
    <div className="space-y-6">
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div>
          <h2 className="text-2xl font-bold flex items-center gap-2">
            <Wrench className="h-6 w-6 text-indigo-600" /> Tool Schema Builder
          </h2>
          <p className="text-sm text-slate-500 dark:text-slate-400 mt-1 max-w-2xl">
            Define custom API tool schemas the agent can call — name, description, and typed
            parameters. Saved locally in your browser and exportable as Gemini-style function
            declarations.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={copyAll}
            disabled={schemas.length === 0}
            className="flex items-center gap-2 px-3 py-2 text-sm rounded-lg border border-slate-200 dark:border-slate-700 hover:bg-slate-50 dark:hover:bg-slate-800 disabled:opacity-40 disabled:cursor-not-allowed"
          >
            {copied === 'all' ? <Check className="h-4 w-4 text-emerald-500" /> : <FileJson className="h-4 w-4" />}
            Export all
          </button>
          <button
            onClick={startNew}
            className="flex items-center gap-2 px-3 py-2 text-sm rounded-lg bg-indigo-600 text-white hover:bg-indigo-700"
          >
            <Plus className="h-4 w-4" /> New tool
          </button>
        </div>
      </div>

      <div className="grid lg:grid-cols-[340px_1fr] gap-6 items-start">
        {/* List */}
        <Card className="bg-white dark:bg-slate-900">
          <CardHeader className="pb-2">
            <CardTitle className="text-sm flex items-center justify-between text-slate-500">
              <span>Custom tools ({schemas.length})</span>
              {schemas.length > 0 && (
                <button
                  onClick={() => { if (confirm('Delete all custom tools?')) { clearSchemas(); cancel(); } }}
                  className="text-xs text-rose-500 hover:text-rose-600 font-medium"
                >
                  Clear all
                </button>
              )}
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-2">
            {schemas.length === 0 && (
              <p className="text-sm text-slate-400 italic py-6 text-center">
                No custom tools yet. Click <span className="font-semibold">New tool</span> to define one.
              </p>
            )}
            {schemas.map(s => (
              <div
                key={s.id}
                className={cn(
                  'group rounded-lg border px-3 py-2 cursor-pointer transition-colors',
                  editingId === s.id
                    ? 'border-indigo-400 bg-indigo-50/60 dark:bg-indigo-950/40'
                    : 'border-slate-200 dark:border-slate-800 hover:bg-slate-50 dark:hover:bg-slate-800/50',
                )}
                onClick={() => startEdit(s.id)}
              >
                <div className="flex items-center justify-between gap-2">
                  <code className="text-sm font-semibold text-indigo-700 dark:text-indigo-300 truncate">{s.name}()</code>
                  <div className="flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
                    <button
                      title="Duplicate"
                      onClick={e => { e.stopPropagation(); duplicateSchema(s.id); }}
                      className="p-1 rounded hover:bg-slate-200 dark:hover:bg-slate-700"
                    >
                      <Files className="h-3.5 w-3.5 text-slate-500" />
                    </button>
                    <button
                      title="Delete"
                      onClick={e => { e.stopPropagation(); removeSchema(s.id); if (editingId === s.id) cancel(); }}
                      className="p-1 rounded hover:bg-rose-100 dark:hover:bg-rose-900/40"
                    >
                      <Trash2 className="h-3.5 w-3.5 text-rose-500" />
                    </button>
                  </div>
                </div>
                <p className="text-xs text-slate-500 dark:text-slate-400 truncate mt-0.5">
                  {s.description || <span className="italic">no description</span>}
                </p>
                <p className="text-[11px] text-slate-400 mt-1">{s.parameters.length} parameter{s.parameters.length === 1 ? '' : 's'}</p>
              </div>
            ))}
          </CardContent>
        </Card>

        {/* Editor */}
        {!draft ? (
          <Card className="bg-white dark:bg-slate-900">
            <CardContent className="py-16 text-center text-slate-400">
              <Wrench className="h-8 w-8 mx-auto mb-3 opacity-40" />
              <p className="text-sm">Select a tool to edit, or create a new one.</p>
            </CardContent>
          </Card>
        ) : (
          <Card className="bg-white dark:bg-slate-900">
            <CardHeader className="pb-3 flex-row items-center justify-between">
              <CardTitle className="text-base">{editingId ? 'Edit tool' : 'New tool'}</CardTitle>
              <button onClick={cancel} className="p-1.5 rounded-lg hover:bg-slate-100 dark:hover:bg-slate-800" title="Close">
                <X className="h-4 w-4 text-slate-500" />
              </button>
            </CardHeader>
            <CardContent className="space-y-5">
              {/* name + description */}
              <div className="space-y-1">
                <label className="text-xs font-semibold text-slate-500 uppercase tracking-wide">Name</label>
                <input
                  value={draft.name}
                  onChange={e => patchDraft({ name: e.target.value })}
                  placeholder="get_weather"
                  className="w-full font-mono text-sm rounded-lg border border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-800/60 px-3 py-2 outline-none focus:border-indigo-500"
                />
              </div>
              <div className="space-y-1">
                <label className="text-xs font-semibold text-slate-500 uppercase tracking-wide">Description</label>
                <textarea
                  value={draft.description}
                  onChange={e => patchDraft({ description: e.target.value })}
                  rows={2}
                  placeholder="What the tool does and when the model should call it."
                  className="w-full text-sm rounded-lg border border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-800/60 px-3 py-2 outline-none focus:border-indigo-500 resize-y"
                />
              </div>

              {/* parameters */}
              <div className="space-y-2">
                <div className="flex items-center justify-between">
                  <label className="text-xs font-semibold text-slate-500 uppercase tracking-wide">Parameters</label>
                  <button onClick={addParam} className="flex items-center gap-1 text-xs text-indigo-600 hover:text-indigo-700 font-medium">
                    <Plus className="h-3.5 w-3.5" /> Add parameter
                  </button>
                </div>
                {draft.parameters.length === 0 && (
                  <p className="text-xs text-slate-400 italic">No parameters (the tool takes no arguments).</p>
                )}
                <div className="space-y-2">
                  {draft.parameters.map(p => (
                    <div key={p.id} className="grid grid-cols-[1fr_110px_auto] gap-2 items-start rounded-lg border border-slate-200 dark:border-slate-800 p-2">
                      <div className="space-y-2">
                        <input
                          value={p.name}
                          onChange={e => updateParam(p.id, { name: e.target.value })}
                          placeholder="param_name"
                          className="w-full font-mono text-xs rounded border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 px-2 py-1.5 outline-none focus:border-indigo-500"
                        />
                        <input
                          value={p.description}
                          onChange={e => updateParam(p.id, { description: e.target.value })}
                          placeholder="description"
                          className="w-full text-xs rounded border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 px-2 py-1.5 outline-none focus:border-indigo-500"
                        />
                      </div>
                      <div className="space-y-2">
                        <select
                          value={p.type}
                          onChange={e => updateParam(p.id, { type: e.target.value as ParamType })}
                          className="w-full text-xs rounded border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 px-2 py-1.5 outline-none focus:border-indigo-500"
                        >
                          {PARAM_TYPES.map(t => <option key={t} value={t}>{t}</option>)}
                        </select>
                        {draft.http && (
                          <select
                            value={p.location ?? 'query'}
                            onChange={e => updateParam(p.id, { location: e.target.value as ParamLocation })}
                            title="Where this argument goes in the HTTP request"
                            className="w-full text-xs rounded border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 px-2 py-1.5 outline-none focus:border-indigo-500"
                          >
                            {PARAM_LOCATIONS.map(l => <option key={l} value={l}>in {l}</option>)}
                          </select>
                        )}
                      </div>
                      <div className="flex flex-col items-center gap-1.5 pt-0.5">
                        <label className="flex items-center gap-1 text-[11px] text-slate-500 cursor-pointer select-none" title="Required">
                          <input type="checkbox" checked={p.required} onChange={e => updateParam(p.id, { required: e.target.checked })} />
                          req
                        </label>
                        <button onClick={() => removeParam(p.id)} className="p-1 rounded hover:bg-rose-100 dark:hover:bg-rose-900/40" title="Remove parameter">
                          <Trash2 className="h-3.5 w-3.5 text-rose-500" />
                        </button>
                      </div>
                    </div>
                  ))}
                </div>
              </div>

              {/* endpoint */}
              <div className="space-y-2">
                <div className="flex items-center justify-between">
                  <label className="text-xs font-semibold text-slate-500 uppercase tracking-wide">Endpoint</label>
                  <label className="flex items-center gap-1.5 text-xs text-slate-500 cursor-pointer select-none">
                    <input type="checkbox" checked={!!draft.http} onChange={e => toggleHttp(e.target.checked)} />
                    callable HTTP tool
                  </label>
                </div>
                {!draft.http ? (
                  <p className="text-xs text-slate-400 italic">
                    No endpoint — the agent will know this tool exists but calling it returns "not configured".
                  </p>
                ) : (
                  <div className="space-y-2 rounded-lg border border-slate-200 dark:border-slate-800 p-3">
                    <div className="flex gap-2">
                      <select
                        value={draft.http.method}
                        onChange={e => setHttp({ method: e.target.value as HttpMethod })}
                        className="text-xs font-mono rounded border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 px-2 py-1.5 outline-none focus:border-indigo-500"
                      >
                        {HTTP_METHODS.map(m => <option key={m} value={m}>{m}</option>)}
                      </select>
                      <input
                        value={draft.http.url}
                        onChange={e => setHttp({ url: e.target.value })}
                        placeholder="https://api.example.com/resource/{id}"
                        className="flex-1 font-mono text-xs rounded border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 px-2 py-1.5 outline-none focus:border-indigo-500"
                      />
                    </div>

                    <div className="space-y-1.5">
                      <div className="flex items-center justify-between">
                        <span className="text-[11px] text-slate-500">Headers</span>
                        <button onClick={addHeader} className="flex items-center gap-1 text-[11px] text-indigo-600 hover:text-indigo-700 font-medium">
                          <Plus className="h-3 w-3" /> header
                        </button>
                      </div>
                      {draft.http.headers.map(h => (
                        <div key={h.id} className="grid grid-cols-[1fr_1fr_auto] gap-2 items-center">
                          <input value={h.key} onChange={e => updateHeader(h.id, { key: e.target.value })} placeholder="Header-Name" className="font-mono text-xs rounded border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 px-2 py-1 outline-none focus:border-indigo-500" />
                          <input value={h.value} onChange={e => updateHeader(h.id, { value: e.target.value })} placeholder="value" className="text-xs rounded border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 px-2 py-1 outline-none focus:border-indigo-500" />
                          <button onClick={() => removeHeader(h.id)} className="p-1 rounded hover:bg-rose-100 dark:hover:bg-rose-900/40" title="Remove header">
                            <Trash2 className="h-3.5 w-3.5 text-rose-500" />
                          </button>
                        </div>
                      ))}
                    </div>

                    <div className="flex flex-wrap gap-2 items-center">
                      <select
                        value={draft.http.auth?.type ?? 'none'}
                        onChange={e => setHttp({ auth: { ...(draft.http?.auth ?? { type: 'none' }), type: e.target.value as 'none' | 'bearer' | 'header' } })}
                        className="text-xs rounded border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 px-2 py-1.5 outline-none focus:border-indigo-500"
                      >
                        <option value="none">No auth</option>
                        <option value="bearer">Bearer token</option>
                        <option value="header">API-key header</option>
                      </select>
                      {draft.http.auth?.type === 'bearer' && (
                        <input value={draft.http.auth.token ?? ''} onChange={e => setHttp({ auth: { ...draft.http!.auth!, token: e.target.value } })} placeholder="token" className="flex-1 min-w-[120px] text-xs rounded border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 px-2 py-1.5 outline-none focus:border-indigo-500" />
                      )}
                      {draft.http.auth?.type === 'header' && (
                        <>
                          <input value={draft.http.auth.key ?? ''} onChange={e => setHttp({ auth: { ...draft.http!.auth!, key: e.target.value } })} placeholder="Header name" className="text-xs rounded border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 px-2 py-1.5 outline-none focus:border-indigo-500" />
                          <input value={draft.http.auth.value ?? ''} onChange={e => setHttp({ auth: { ...draft.http!.auth!, value: e.target.value } })} placeholder="value" className="flex-1 min-w-[100px] text-xs rounded border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 px-2 py-1.5 outline-none focus:border-indigo-500" />
                        </>
                      )}
                    </div>

                    <p className="text-[11px] font-mono text-slate-400 truncate">{draft.http.method} {draft.http.url || '…'}</p>
                  </div>
                )}
              </div>

              {/* validation */}
              {errors.length > 0 && (
                <div className="rounded-lg border border-amber-300 dark:border-amber-800/60 bg-amber-50 dark:bg-amber-950/30 p-3 space-y-1">
                  {errors.map((err, i) => (
                    <p key={i} className="text-xs text-amber-700 dark:text-amber-400 flex items-center gap-1.5">
                      <AlertCircle className="h-3.5 w-3.5 shrink-0" /> {err}
                    </p>
                  ))}
                </div>
              )}

              {/* live JSON preview */}
              <div className="space-y-1">
                <div className="flex items-center justify-between">
                  <label className="text-xs font-semibold text-slate-500 uppercase tracking-wide">Function declaration</label>
                  <button onClick={() => copy('preview', preview)} className="flex items-center gap-1 text-xs text-slate-500 hover:text-slate-700 dark:hover:text-slate-300">
                    {copied === 'preview' ? <Check className="h-3.5 w-3.5 text-emerald-500" /> : <Copy className="h-3.5 w-3.5" />} copy
                  </button>
                </div>
                <pre className="text-[11px] font-mono bg-slate-950 text-emerald-300 rounded-lg p-3 overflow-x-auto max-h-64">{preview}</pre>
              </div>

              {/* actions */}
              <div className="flex items-center justify-end gap-2 pt-1">
                <button onClick={cancel} className="px-4 py-2 text-sm rounded-lg border border-slate-200 dark:border-slate-700 hover:bg-slate-50 dark:hover:bg-slate-800">
                  Cancel
                </button>
                <button
                  onClick={save}
                  disabled={errors.length > 0}
                  className="flex items-center gap-2 px-4 py-2 text-sm rounded-lg bg-indigo-600 text-white hover:bg-indigo-700 disabled:opacity-40 disabled:cursor-not-allowed"
                >
                  <Save className="h-4 w-4" /> {editingId ? 'Save changes' : 'Create tool'}
                </button>
              </div>
            </CardContent>
          </Card>
        )}
      </div>
    </div>
  );
};
