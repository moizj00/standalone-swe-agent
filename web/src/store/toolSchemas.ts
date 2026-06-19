/**
 * Tool-schema state slice (pure logic).
 *
 * Holds user-defined custom tool schemas for the LLM agent. This module is
 * framework-agnostic: types, a pure reducer, validation, (de)serialization, and
 * conversion to the Gemini/AI-Studio `functionDeclaration` shape the rest of the
 * app (and the agent's /api/tools) already speaks. React bindings live in
 * ./ToolSchemasProvider.tsx.
 */

export type ParamType =
  | 'STRING'
  | 'NUMBER'
  | 'INTEGER'
  | 'BOOLEAN'
  | 'OBJECT'
  | 'ARRAY';

export const PARAM_TYPES: ParamType[] = [
  'STRING', 'NUMBER', 'INTEGER', 'BOOLEAN', 'OBJECT', 'ARRAY',
];

export type HttpMethod = 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE';
export const HTTP_METHODS: HttpMethod[] = ['GET', 'POST', 'PUT', 'PATCH', 'DELETE'];

/** Where a parameter is placed in the outgoing HTTP request. */
export type ParamLocation = 'query' | 'path' | 'body' | 'header';
export const PARAM_LOCATIONS: ParamLocation[] = ['query', 'path', 'body', 'header'];

export interface HeaderRow { id: string; key: string; value: string; }

export interface HttpConfig {
  method: HttpMethod;
  url: string;                 // may contain {param} path placeholders
  headers: HeaderRow[];
  auth?: { type: 'none' | 'bearer' | 'header'; token?: string; key?: string; value?: string };
}

/** One parameter row in the editor. `id` is a stable key for React lists. */
export interface ToolParameter {
  id: string;
  name: string;
  type: ParamType;
  description: string;
  required: boolean;
  location?: ParamLocation;    // HTTP request placement (defaults by method server-side)
}

/** The editable form shape (no identity/timestamps). */
export interface DraftSchema {
  name: string;
  description: string;
  parameters: ToolParameter[];
  http?: HttpConfig;           // optional endpoint that makes the tool callable
}

/** A persisted custom tool schema. */
export interface CustomToolSchema extends DraftSchema {
  id: string;
  createdAt: number;
  updatedAt: number;
}

export interface ToolSchemasState {
  schemas: CustomToolSchema[];
}

export const initialToolSchemasState: ToolSchemasState = { schemas: [] };

// --------------------------------------------------------------------------- actions

export type ToolSchemasAction =
  | { type: 'add'; schema: CustomToolSchema }
  | { type: 'update'; id: string; patch: DraftSchema; now: number }
  | { type: 'remove'; id: string }
  | { type: 'duplicate'; id: string; newId: string; now: number }
  | { type: 'clear' }
  | { type: 'replaceAll'; schemas: CustomToolSchema[] };

// --------------------------------------------------------------------------- reducer (pure)

export function toolSchemasReducer(
  state: ToolSchemasState,
  action: ToolSchemasAction,
): ToolSchemasState {
  switch (action.type) {
    case 'add':
      return { schemas: [...state.schemas, action.schema] };

    case 'update':
      return {
        schemas: state.schemas.map(s =>
          s.id === action.id
            ? { ...s, ...action.patch, id: s.id, createdAt: s.createdAt, updatedAt: action.now }
            : s),
      };

    case 'remove':
      return { schemas: state.schemas.filter(s => s.id !== action.id) };

    case 'duplicate': {
      const src = state.schemas.find(s => s.id === action.id);
      if (!src) return state;
      const copy: CustomToolSchema = {
        ...src,
        id: action.newId,
        name: uniqueName(`${src.name}_copy`, state.schemas),
        createdAt: action.now,
        updatedAt: action.now,
        parameters: src.parameters.map(p => ({ ...p })),
      };
      const idx = state.schemas.findIndex(s => s.id === action.id);
      const next = [...state.schemas];
      next.splice(idx + 1, 0, copy);
      return { schemas: next };
    }

    case 'clear':
      return { schemas: [] };

    case 'replaceAll':
      return { schemas: action.schemas };

    default:
      return state;
  }
}

// --------------------------------------------------------------------------- helpers

const NAME_RE = /^[a-zA-Z_][a-zA-Z0-9_]*$/;

/** Make `base` unique within `schemas` by appending an incrementing suffix. */
export function uniqueName(base: string, schemas: CustomToolSchema[]): string {
  const taken = new Set(schemas.map(s => s.name));
  if (!taken.has(base)) return base;
  let n = 2;
  while (taken.has(`${base}${n}`)) n += 1;
  return `${base}${n}`;
}

export function emptyDraft(): DraftSchema {
  return { name: '', description: '', parameters: [] };
}

export function emptyParameter(): ToolParameter {
  // location intentionally left unset: an unset location is omitted from the
  // payload so the backend applies its method default (body for POST/PUT/PATCH,
  // query for GET/DELETE). The editor shows that default in the dropdown.
  return { id: makeId(), name: '', type: 'STRING', description: '', required: false };
}

/** The HTTP location the backend will default a parameter to for `method`. */
export function defaultParamLocation(method?: HttpMethod): ParamLocation {
  return method === 'POST' || method === 'PUT' || method === 'PATCH' ? 'body' : 'query';
}

export function emptyHttp(): HttpConfig {
  return { method: 'GET', url: '', headers: [], auth: { type: 'none' } };
}

const PLACEHOLDER_RE = /\{([a-zA-Z_][a-zA-Z0-9_]*)\}/g;

/** Path placeholders referenced in a URL template, e.g. {id}. */
export function urlPlaceholders(url: string): string[] {
  return [...url.matchAll(PLACEHOLDER_RE)].map(m => m[1]);
}

/** Best-effort check for a LITERAL private/loopback/link-local host the server's
 * SSRF guard would reject (the browser can't resolve DNS, so this catches only
 * literal hosts — enough to stop the obvious http://127.0.0.1 / metadata cases
 * that would otherwise 400 every chat). */
export function isBlockedLiteralHost(url: string): boolean {
  let host: string;
  try {
    host = new URL(url).hostname.toLowerCase().replace(/^\[|\]$/g, '');
  } catch {
    return false;  // unparseable (e.g. has placeholders) — other rules cover it
  }
  if (!host) return false;
  if (host === 'localhost' || host.endsWith('.localhost')) return true;
  if (host === '0.0.0.0' || host === '::1' || host === '::') return true;
  const m = host.match(/^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/);
  if (m) {
    const [a, b] = m.slice(1).map(Number);
    if ([a, ...m.slice(1).map(Number)].some(x => x > 255)) return false;
    if (a === 0 || a === 127 || a === 10) return true;          // this-host / loopback / private
    if (a === 192 && b === 168) return true;                    // private
    if (a === 169 && b === 254) return true;                    // link-local / cloud metadata
    if (a === 172 && b >= 16 && b <= 31) return true;           // private
    if (a === 100 && b >= 64 && b <= 127) return true;          // CGNAT / shared
  }
  if (/^f[cd][0-9a-f]{2}:/.test(host) || /^fe80:/.test(host)) return true;  // IPv6 ULA / link-local
  return false;
}

/**
 * Validate a draft against the existing set. `editingId` excludes the schema
 * currently being edited from the uniqueness check. Returns human-readable
 * error strings (empty array = valid).
 */
export function validateDraft(
  draft: DraftSchema,
  schemas: CustomToolSchema[],
  editingId: string | null,
  builtinNames: Set<string> = new Set(),
): string[] {
  const errors: string[] = [];
  const name = draft.name.trim();

  if (!name) {
    errors.push('Tool name is required.');
  } else if (!NAME_RE.test(name)) {
    errors.push('Tool name must start with a letter/underscore and contain only letters, digits, and underscores.');
  } else if (schemas.some(s => s.name.trim() === name && s.id !== editingId)) {
    // compare TRIMMED — the payload trims names, so "foo " and "foo" collide server-side
    errors.push(`A tool named "${name}" already exists.`);
  } else if (builtinNames.has(name)) {
    // mirror the server: a custom tool can't shadow a built-in agent tool
    errors.push(`A tool named "${name}" shadows a built-in agent tool; choose another.`);
  }

  if (!draft.description.trim()) {
    errors.push('Description is required (it tells the model when to call the tool).');
  }

  const seen = new Set<string>();
  draft.parameters.forEach((p, i) => {
    const pname = p.name.trim();
    if (!pname) {
      errors.push(`Parameter #${i + 1} needs a name.`);
      return;
    }
    if (!NAME_RE.test(pname)) {
      errors.push(`Parameter "${pname}" has an invalid name.`);
    }
    if (seen.has(pname)) {
      errors.push(`Duplicate parameter name "${pname}".`);
    }
    seen.add(pname);
  });

  if (draft.http && (draft.http.url.trim() || draft.http.method !== 'GET')) {
    const url = draft.http.url.trim();
    if (!url) {
      errors.push('Endpoint URL is required when an endpoint is configured.');
    } else if (!/^https?:\/\//i.test(url)) {
      errors.push('Endpoint URL must start with http:// or https://.');
    } else if (isBlockedLiteralHost(url)) {
      errors.push('Endpoint host is a private/loopback/link-local address the agent will refuse.');
    } else if (/\/\/[^/?#]*@/.test(url)) {
      errors.push('Endpoint URL must not embed credentials (user:pass@host); use the auth field.');
    }
    // mirror the server: placeholders are not allowed in the scheme/authority
    const authority = url.match(/^[a-zA-Z][a-zA-Z0-9+.-]*:\/\/([^/?#]*)/);
    if (authority && authority[1].includes('{')) {
      errors.push('URL placeholders are only allowed in the path/query, not the host.');
    }
    const declared = new Set(draft.parameters.map(p => p.name.trim()).filter(Boolean));
    const placeholders = new Set(urlPlaceholders(url));
    for (const ph of placeholders) {
      if (!declared.has(ph)) errors.push(`URL placeholder "{${ph}}" has no matching parameter.`);
    }
    // mirror the server's path-direction rule: a 'path' param needs a placeholder
    draft.parameters.forEach(p => {
      const pn = p.name.trim();
      if (pn && p.location === 'path' && !placeholders.has(pn)) {
        errors.push(`Parameter "${pn}" is set to 'path' but the URL has no "{${pn}}" placeholder.`);
      }
    });
    // mirror the server: a 'header' param can't collide with a configured/auth header
    const reservedHeaders = new Set<string>();
    draft.http.headers.forEach(h => { if (h.key.trim()) reservedHeaders.add(h.key.trim().toLowerCase()); });
    if (draft.http.auth?.type === 'bearer') reservedHeaders.add('authorization');
    if (draft.http.auth?.type === 'header' && draft.http.auth.key?.trim()) {
      reservedHeaders.add(draft.http.auth.key.trim().toLowerCase());
    }
    draft.parameters.forEach(p => {
      const pn = p.name.trim();
      if (pn && p.location === 'header' && reservedHeaders.has(pn.toLowerCase())) {
        errors.push(`Parameter "${pn}" (header) collides with a configured/auth header.`);
      }
    });
    draft.http.headers.forEach(h => {
      if (h.value.trim() && !h.key.trim()) errors.push('A header has a value but no name.');
    });
  }

  return errors;
}

/** The Gemini/AI-Studio functionDeclaration shape (UPPERCASE types). */
export interface FunctionDeclaration {
  name: string;
  description: string;
  parameters: {
    type: 'OBJECT';
    properties: Record<string, { type: ParamType; description: string }>;
    required?: string[];
  };
}

export function toFunctionDeclaration(draft: DraftSchema): FunctionDeclaration {
  const properties: Record<string, { type: ParamType; description: string }> = {};
  const required: string[] = [];
  for (const p of draft.parameters) {
    const pname = p.name.trim();
    if (!pname) continue;
    properties[pname] = { type: p.type, description: p.description.trim() };
    if (p.required) required.push(pname);
  }
  const decl: FunctionDeclaration = {
    name: draft.name.trim(),
    description: draft.description.trim(),
    parameters: { type: 'OBJECT', properties },
  };
  if (required.length) decl.parameters.required = required;
  return decl;
}

export function exportDeclarations(schemas: CustomToolSchema[]): FunctionDeclaration[] {
  return schemas.map(toFunctionDeclaration);
}

// --------------------------------------------------------------------------- agent payload

const JSONSCHEMA_TYPE: Record<ParamType, string> = {
  STRING: 'string', NUMBER: 'number', INTEGER: 'integer',
  BOOLEAN: 'boolean', OBJECT: 'object', ARRAY: 'array',
};

/**
 * The shape sent to the agent server in the chat request `custom_tools` array
 * (consumed by swe_agent/tools/custom.py). Parameters use lowercase JSON-schema
 * types (what Ollama expects); `http` is included only when an endpoint URL is set.
 */
export interface CustomToolPayload {
  name: string;
  description: string;
  parameters: {
    type: 'object';
    properties: Record<string, { type: string; description: string }>;
    required?: string[];
  };
  http?: {
    method: HttpMethod;
    url: string;
    headers: Record<string, string>;
    param_location: Record<string, ParamLocation>;
    auth?: { type: string; token?: string; key?: string; value?: string };
  };
}

export function toCustomToolPayload(schema: DraftSchema): CustomToolPayload {
  const properties: Record<string, { type: string; description: string }> = {};
  const required: string[] = [];
  const paramLocation: Record<string, ParamLocation> = {};
  for (const p of schema.parameters) {
    const pname = p.name.trim();
    if (!pname) continue;
    properties[pname] = { type: JSONSCHEMA_TYPE[p.type] ?? 'string', description: p.description.trim() };
    if (p.required) required.push(pname);
    if (p.location) paramLocation[pname] = p.location;
  }
  const payload: CustomToolPayload = {
    name: schema.name.trim(),
    description: schema.description.trim(),
    parameters: { type: 'object', properties },
  };
  if (required.length) payload.parameters.required = required;

  if (schema.http && schema.http.url.trim()) {
    const headers: Record<string, string> = {};
    for (const h of schema.http.headers) {
      if (h.key.trim()) headers[h.key.trim()] = h.value;
    }
    const auth = schema.http.auth && schema.http.auth.type !== 'none' ? schema.http.auth : undefined;
    payload.http = {
      method: schema.http.method,
      url: schema.http.url.trim(),
      headers,
      param_location: paramLocation,
      ...(auth ? { auth } : {}),
    };
  }
  return payload;
}

/** Tools with a configured endpoint — the ones the agent can actually call. */
export function callableSchemas(schemas: CustomToolSchema[]): CustomToolPayload[] {
  return schemas.filter(s => s.http && s.http.url.trim()).map(toCustomToolPayload);
}

// --------------------------------------------------------------------------- ids + persistence

export function makeId(): string {
  const c: any = typeof crypto !== 'undefined' ? crypto : undefined;
  if (c && typeof c.randomUUID === 'function') return c.randomUUID();
  return `id_${Date.now().toString(36)}_${Math.floor(Math.random() * 1e9).toString(36)}`;
}

export const STORAGE_KEY = 'swe-agent.tool-schemas.v1';

/** Coerce one persisted entry into a well-formed schema, or null if unsalvageable.
 * Later code assumes `http.url`/`http.headers` exist, so a partial/corrupt `http`
 * block is dropped (the tool degrades to declarations-only) rather than crashing
 * the Tool Builder / Coding Mode. */
function _normalizeLoaded(s: any): CustomToolSchema | null {
  if (!s || typeof s.id !== 'string' || typeof s.name !== 'string' || !Array.isArray(s.parameters)) {
    return null;
  }
  let http = s.http;
  if (http !== undefined) {
    if (!http || typeof http !== 'object' || typeof http.url !== 'string') {
      http = undefined;  // unusable endpoint -> declarations-only
    } else {
      http = { ...http, headers: Array.isArray(http.headers) ? http.headers : [] };
    }
  }
  return { ...s, http };
}

export function loadSchemas(): CustomToolSchema[] {
  if (typeof localStorage === 'undefined') return [];
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.map(_normalizeLoaded).filter((s): s is CustomToolSchema => s !== null);
  } catch {
    return [];
  }
}

export function saveSchemas(schemas: CustomToolSchema[]): void {
  if (typeof localStorage === 'undefined') return;
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(schemas));
  } catch {
    /* quota / disabled storage — non-fatal */
  }
}
