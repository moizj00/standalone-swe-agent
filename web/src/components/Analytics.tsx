import React from 'react';
import { Card, CardContent, CardHeader, CardTitle } from './ui/card';
import { AreaChart, Area, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer } from 'recharts';
import { mockProgress, mockContributions } from '../data/mock';
import { Download } from 'lucide-react';

const getHeatmapColor = (count: number) => {
  if (count === 0) return 'bg-slate-100 dark:bg-slate-800';
  if (count < 3) return 'bg-emerald-200 dark:bg-emerald-900/40 text-emerald-900 dark:text-emerald-100';
  if (count < 6) return 'bg-emerald-400 dark:bg-emerald-700/60 text-emerald-950 dark:text-emerald-50';
  if (count < 9) return 'bg-emerald-500 dark:bg-emerald-600 text-white';
  return 'bg-emerald-600 dark:bg-emerald-500 text-white';
};

export const Analytics = () => {
  const handleDownloadCSV = () => {
    if (!mockProgress.length) return;
    const header = Object.keys(mockProgress[0]).join(',');
    const rows = mockProgress.map(row => Object.values(row).join(','));
    const csvContent = [header, ...rows].join('\n');
    
    const blob = new Blob([csvContent], { type: 'text/csv;charset=utf-8;' });
    const link = document.createElement('a');
    const url = URL.createObjectURL(blob);
    link.setAttribute('href', url);
    link.setAttribute('download', 'project_metrics.csv');
    link.style.visibility = 'hidden';
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
  };

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-2xl font-bold tracking-tight">Performance Analytics</h2>
          <p className="text-slate-500 dark:text-slate-400">Visualize key metrics and overall project health.</p>
        </div>
        <button 
          onClick={handleDownloadCSV}
          className="flex items-center gap-2 bg-white dark:bg-slate-900 border border-slate-200 dark:border-slate-800 hover:bg-slate-50 dark:hover:bg-slate-800 transition-colors px-4 py-2 rounded-lg text-sm font-medium"
        >
          <Download className="h-4 w-4" /> Download CSV
        </button>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium text-slate-500">Total Completion Rate</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="text-3xl font-bold">68.5%</div>
            <p className="text-xs text-emerald-500 font-medium mt-1">+2.4% from last week</p>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium text-slate-500">Budget Spent</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="text-3xl font-bold">$45,200</div>
            <p className="text-xs text-rose-500 font-medium mt-1">12% over expected timeline</p>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium text-slate-500">Active Tasks</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="text-3xl font-bold">24</div>
            <p className="text-xs text-emerald-500 font-medium mt-1">8 closing this week</p>
          </CardContent>
        </Card>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <Card>
          <CardHeader>
            <CardTitle>Milestone Progress vs Expected</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="h-[300px] w-full mt-4">
              <ResponsiveContainer width="100%" height="100%">
                <AreaChart data={mockProgress}>
                  <defs>
                    <linearGradient id="colorCompleted" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="5%" stopColor="#3b82f6" stopOpacity={0.3}/>
                      <stop offset="95%" stopColor="#3b82f6" stopOpacity={0}/>
                    </linearGradient>
                    <linearGradient id="colorExpected" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="5%" stopColor="#94a3b8" stopOpacity={0.3}/>
                      <stop offset="95%" stopColor="#94a3b8" stopOpacity={0}/>
                    </linearGradient>
                  </defs>
                  <CartesianGrid strokeDasharray="3 3" vertical={false} stroke="#e2e8f0" />
                  <XAxis dataKey="date" axisLine={false} tickLine={false} tick={{ fill: '#64748b', fontSize: 12 }} />
                  <YAxis axisLine={false} tickLine={false} tick={{ fill: '#64748b', fontSize: 12 }} />
                  <Tooltip contentStyle={{ borderRadius: '8px', border: 'none', boxShadow: '0 4px 6px -1px rgb(0 0 0 / 0.1)' }} />
                  <Area type="monotone" dataKey="expected" stroke="#94a3b8" fillOpacity={1} fill="url(#colorExpected)" />
                  <Area type="monotone" dataKey="completed" stroke="#3b82f6" fillOpacity={1} fill="url(#colorCompleted)" />
                </AreaChart>
              </ResponsiveContainer>
            </div>
          </CardContent>
        </Card>
        
        <Card>
          <CardHeader>
            <CardTitle>Team Contribution Volume (30 Days)</CardTitle>
          </CardHeader>
          <CardContent>
             <div className="flex flex-wrap gap-2 mt-4">
               {mockContributions.map((contrib, i) => (
                 <div 
                   key={i} 
                   className={`h-8 w-8 rounded flex items-center justify-center text-xs font-medium cursor-help transition-colors ${getHeatmapColor(contrib.count)}`}
                   title={`${contrib.date}: ${contrib.count} contributions`}
                 >
                   {contrib.count > 0 ? contrib.count : ''}
                 </div>
               ))}
             </div>
             <div className="mt-6 flex items-center gap-2 text-xs text-slate-500">
                <span>Less</span>
                <div className="h-3 w-3 bg-slate-100 dark:bg-slate-800 rounded-sm"></div>
                <div className="h-3 w-3 bg-emerald-200 dark:bg-emerald-900/40 rounded-sm"></div>
                <div className="h-3 w-3 bg-emerald-400 dark:bg-emerald-700/60 rounded-sm"></div>
                <div className="h-3 w-3 bg-emerald-500 dark:bg-emerald-600 rounded-sm"></div>
                <div className="h-3 w-3 bg-emerald-600 dark:bg-emerald-500 rounded-sm"></div>
                <span>More</span>
             </div>
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
