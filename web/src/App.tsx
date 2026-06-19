import React, { useState, useEffect } from 'react';
import { LayoutDashboard, BarChart3, Moon, Sun, Code2, ListTodo, Shield, Search } from 'lucide-react';
import { cn } from './utils';
import { Overview } from './components/Overview';
import { Analytics } from './components/Analytics';
import { CodingMode } from './components/CodingMode';
import { mockUsers } from './data/mock';

function App() {
  const [view, setView] = useState<'overview' | 'analytics' | 'coding'>('overview');
  const [mode, setMode] = useState<'planning' | 'coding'>('planning');
  const [theme, setTheme] = useState<'light' | 'dark'>('light');
  const [currentUser] = useState(mockUsers[0]);
  const [searchQuery, setSearchQuery] = useState('');

  useEffect(() => {
    if (theme === 'dark') {
      document.documentElement.classList.add('dark');
    } else {
      document.documentElement.classList.remove('dark');
    }
  }, [theme]);

  const toggleTheme = () => setTheme(t => t === 'light' ? 'dark' : 'light');

  const renderContent = () => {
    if (mode === 'coding') {
      return <CodingMode />;
    }
    switch (view) {
      case 'overview': return <Overview user={currentUser} searchQuery={searchQuery} />;
      case 'analytics': return <Analytics />;
      case 'coding': return <CodingMode />;
      default: return <Overview user={currentUser} searchQuery={searchQuery} />;
    }
  };

  return (
    <div className="flex h-screen w-full bg-slate-50 dark:bg-slate-950 text-slate-900 dark:text-slate-50 font-sans">
      <aside className="w-64 border-r border-slate-200 dark:border-slate-800 bg-white dark:bg-slate-900 flex flex-col">
        <div className="p-6">
          <h1 className="text-xl font-bold flex items-center gap-2">
            <LayoutDashboard className="h-6 w-6 text-indigo-600" />
            ProjectBoard
          </h1>
        </div>

        <div className="px-4 py-2 mt-4 space-y-1 flex-1">
           <div className="text-xs font-semibold text-slate-400 uppercase tracking-wider mb-2 px-2">Modes</div>
           <button onClick={() => setMode('planning')} className={cn("w-full flex items-center gap-3 px-3 py-2 rounded-lg text-sm font-medium transition-colors", mode === 'planning' ? 'bg-indigo-50 text-indigo-700 dark:bg-indigo-900/50 dark:text-indigo-300' : 'text-slate-600 hover:bg-slate-100 dark:text-slate-400 dark:hover:bg-slate-800')}>
             <ListTodo className="h-4 w-4" /> Planning Mode
           </button>
           <button onClick={() => setMode('coding')} className={cn("w-full flex items-center gap-3 px-3 py-2 rounded-lg text-sm font-medium transition-colors", mode === 'coding' ? 'bg-indigo-50 text-indigo-700 dark:bg-indigo-900/50 dark:text-indigo-300' : 'text-slate-600 hover:bg-slate-100 dark:text-slate-400 dark:hover:bg-slate-800')}>
             <Code2 className="h-4 w-4" /> Coding Mode
           </button>

           {mode === 'planning' && (
             <>
               <div className="text-xs font-semibold text-slate-400 uppercase tracking-wider mb-2 mt-6 px-2">Navigation</div>
               <button onClick={() => setView('overview')} className={cn("w-full flex items-center gap-3 px-3 py-2 rounded-lg text-sm font-medium transition-colors", view === 'overview' ? 'bg-slate-100 text-slate-900 dark:bg-slate-800 dark:text-slate-50' : 'text-slate-600 hover:bg-slate-50 dark:text-slate-400 dark:hover:bg-slate-800/50')}>
                 <LayoutDashboard className="h-4 w-4" /> Overview
               </button>
               <button onClick={() => setView('analytics')} className={cn("w-full flex items-center gap-3 px-3 py-2 rounded-lg text-sm font-medium transition-colors", view === 'analytics' ? 'bg-slate-100 text-slate-900 dark:bg-slate-800 dark:text-slate-50' : 'text-slate-600 hover:bg-slate-50 dark:text-slate-400 dark:hover:bg-slate-800/50')}>
                 <BarChart3 className="h-4 w-4" /> Analytics
               </button>
             </>
           )}
        </div>

        <div className="p-4 border-t border-slate-200 dark:border-slate-800 space-y-4">
          <div className="flex items-center justify-between px-2">
            <span className="text-sm font-medium text-slate-600 dark:text-slate-400">Theme</span>
            <button onClick={toggleTheme} className="p-2 rounded-full hover:bg-slate-100 dark:hover:bg-slate-800 transition-colors">
              {theme === 'light' ? <Moon className="h-4 w-4" /> : <Sun className="h-4 w-4" />}
            </button>
          </div>

          <div className="flex items-center gap-3 px-2 py-2 rounded-lg bg-slate-50 dark:bg-slate-800/50 border border-slate-200 dark:border-slate-700">
            <img src={currentUser.avatar} alt="Avatar" className="w-8 h-8 rounded-full border border-slate-300 dark:border-slate-600 bg-white" />
            <div className="flex-1 overflow-hidden">
              <div className="text-sm font-medium truncate">{currentUser.name}</div>
              <div className="text-xs text-slate-500 font-mono flex items-center gap-1">
                <Shield className="h-3 w-3" />
                {currentUser.role}
              </div>
            </div>
          </div>
        </div>
      </aside>

      <main className="flex-1 flex flex-col h-screen overflow-hidden">
        <header className="h-16 border-b border-slate-200 dark:border-slate-800 bg-white dark:bg-slate-900 flex items-center px-8 justify-between shrink-0">
           <h2 className="text-lg font-semibold capitalize hidden md:block text-slate-800 dark:text-slate-100">{mode === 'coding' ? 'Coding Workspace' : view}</h2>
           <div className="flex-1 max-w-md ml-auto relative">
             <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-slate-400" />
             <input 
               type="text" 
               placeholder="Search tasks across dashboard..." 
               value={searchQuery}
               onChange={e => setSearchQuery(e.target.value)}
               className="w-full bg-slate-100 dark:bg-slate-800/50 border-transparent focus:bg-white dark:focus:bg-slate-900 focus:border-indigo-500 focus:ring-2 focus:ring-indigo-200 dark:focus:ring-indigo-900/50 rounded-lg pl-10 pr-4 py-2 text-sm transition-all outline-none"
             />
           </div>
        </header>

         <div className="flex-1 overflow-y-auto p-8">
           <div className="max-w-7xl mx-auto">
             {renderContent()}
           </div>
         </div>
      </main>
    </div>
  );
}

export default App;