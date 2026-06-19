/**
 * React bindings for the tool-schema slice: a Context provider that owns the
 * reducer state, persists it to localStorage, and exposes a typed action API via
 * the `useToolSchemas()` hook.
 */
import React, { createContext, useContext, useEffect, useMemo, useReducer } from 'react';
import {
  CustomToolSchema,
  DraftSchema,
  initialToolSchemasState,
  loadSchemas,
  makeId,
  saveSchemas,
  toolSchemasReducer,
  ToolSchemasState,
} from './toolSchemas';

export interface ToolSchemasApi {
  schemas: CustomToolSchema[];
  addSchema: (draft: DraftSchema) => string; // returns the new id
  updateSchema: (id: string, draft: DraftSchema) => void;
  removeSchema: (id: string) => void;
  duplicateSchema: (id: string) => void;
  clearSchemas: () => void;
  replaceAll: (schemas: CustomToolSchema[]) => void;
}

const ToolSchemasContext = createContext<ToolSchemasApi | null>(null);

export function ToolSchemasProvider({ children }: { children: React.ReactNode }) {
  const [state, dispatch] = useReducer(
    toolSchemasReducer,
    initialToolSchemasState,
    // lazy init from localStorage so a refresh keeps the user's tools
    (init): ToolSchemasState => ({ ...init, schemas: loadSchemas() }),
  );

  useEffect(() => {
    saveSchemas(state.schemas);
  }, [state.schemas]);

  const api = useMemo<ToolSchemasApi>(() => ({
    schemas: state.schemas,
    addSchema: (draft) => {
      const now = Date.now();
      const id = makeId();
      dispatch({ type: 'add', schema: { ...draft, id, createdAt: now, updatedAt: now } });
      return id;
    },
    updateSchema: (id, draft) => dispatch({ type: 'update', id, patch: draft, now: Date.now() }),
    removeSchema: (id) => dispatch({ type: 'remove', id }),
    duplicateSchema: (id) => dispatch({ type: 'duplicate', id, newId: makeId(), now: Date.now() }),
    clearSchemas: () => dispatch({ type: 'clear' }),
    replaceAll: (schemas) => dispatch({ type: 'replaceAll', schemas }),
  }), [state.schemas]);

  return <ToolSchemasContext.Provider value={api}>{children}</ToolSchemasContext.Provider>;
}

export function useToolSchemas(): ToolSchemasApi {
  const ctx = useContext(ToolSchemasContext);
  if (!ctx) {
    throw new Error('useToolSchemas must be used within a <ToolSchemasProvider>');
  }
  return ctx;
}
