import React from 'react';
import ReactDOM from 'react-dom/client';
import './styles.css';
import { AppShell } from './app_shell';

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <AppShell />
  </React.StrictMode>,
);
