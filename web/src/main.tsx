import {StrictMode} from 'react';
import {createRoot} from 'react-dom/client';
import {ToolSchemasProvider} from './store/ToolSchemasProvider';
import {AppRouter} from './AppRouter';
import './index.css';

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <ToolSchemasProvider>
      <AppRouter />
    </ToolSchemasProvider>
  </StrictMode>,
);
