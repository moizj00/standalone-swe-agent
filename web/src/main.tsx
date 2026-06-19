import {StrictMode} from 'react';
import {createRoot} from 'react-dom/client';
import App from './App.tsx';
import {ToolSchemasProvider} from './store/ToolSchemasProvider';
import './index.css';

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <ToolSchemasProvider>
      <App />
    </ToolSchemasProvider>
  </StrictMode>,
);
