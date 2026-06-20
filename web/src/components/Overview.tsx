import React, { useState } from 'react';
import { Card, CardContent } from './ui/card';
import { mockTasks } from '../data/mock';
import { Clock, AlertCircle, Filter } from 'lucide-react';
import { User } from '../types';

export const Overview = ({ user, searchQuery = '' }: { user: User, searchQuery?: string }) => {
  const allStatuses = ['Todo', 'In Progress', 'Review', 'Done'];
  const [statusFilter, setStatusFilter] = useState<string>('All');

  const filteredTasks = mockTasks.filter(task => {
    const matchesSearch = task.title.toLowerCase().includes(searchQuery.toLowerCase());
    const matchesStatus = statusFilter === 'All' || task.status === statusFilter;
    return matchesSearch && matchesStatus;
  });

  const statusesToRender = statusFilter === 'All' ? allStatuses : [statusFilter];

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-2xl font-bold tracking-tight">Welcome back, {user.name}</h2>
          <p className="text-slate-500 dark:text-slate-400">Here's a look at your team's progress today.</p>
        </div>
        <div className="flex items-center gap-2">
           <Filter className="h-4 w-4 text-slate-500" />
           <select 
             className="bg-white dark:bg-slate-900 border border-slate-200 dark:border-slate-800 rounded-md text-sm p-2 outline-none focus:ring-2 focus:ring-indigo-500"
             value={statusFilter}
             onChange={e => setStatusFilter(e.target.value)}
           >
             <option value="All">All Statuses</option>
             {allStatuses.map(s => <option key={s} value={s}>{s}</option>)}
           </select>
        </div>
      </div>

      <div className={`grid grid-cols-1 md:grid-cols-2 ${statusFilter === 'All' ? 'lg:grid-cols-4' : 'lg:grid-cols-1 max-w-xl'} gap-6`}>
        {statusesToRender.map(status => (
          <div key={status} className="flex flex-col gap-4">
            <h3 className="font-semibold text-sm uppercase tracking-wider text-slate-500 flex items-center justify-between">
               {status}
               <span className="bg-slate-200 dark:bg-slate-800 text-slate-700 dark:text-slate-300 py-0.5 px-2 rounded-full text-xs">
                 {filteredTasks.filter(t => t.status === status).length}
               </span>
            </h3>
            <div className="space-y-3">
              {filteredTasks.filter(t => t.status === status).map(task => (
                <Card key={task.id} className="cursor-pointer hover:shadow-md transition-shadow">
                  <CardContent className="p-4 space-y-3">
                    <div className="flex items-start justify-between">
                      <p className="font-medium text-sm leading-snug">{task.title}</p>
                    </div>
                    <div className="flex items-center justify-between text-xs text-slate-500">
                      <span className={`inline-flex items-center gap-1 font-medium ${
                         task.priority === 'High' ? 'text-rose-500' :
                         task.priority === 'Medium' ? 'text-amber-500' : 'text-emerald-500'
                      }`}>
                        <AlertCircle className="h-3 w-3" /> {task.priority}
                      </span>
                      <span className="flex items-center gap-1">
                        <Clock className="h-3 w-3" /> {new Date(task.dueDate).toLocaleDateString()}
                      </span>
                    </div>
                    <div className="mt-2 space-y-1">
                      <div className="flex items-center justify-between text-xs text-slate-500">
                        <span>Progress</span>
                        <span>{task.progress}%</span>
                      </div>
                      <div className="h-1.5 w-full bg-slate-100 dark:bg-slate-800 rounded-full overflow-hidden">
                         <div 
                           className="h-full bg-indigo-500 rounded-full" 
                           style={{ width: `${task.progress}%` }}
                         />
                      </div>
                    </div>
                  </CardContent>
                </Card>
              ))}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
