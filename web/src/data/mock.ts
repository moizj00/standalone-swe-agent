import { User, Task, DailyProgress, Contribution } from '../types';

export const mockUsers: User[] = [
  { id: 'u1', name: 'Alice Chen', role: 'Admin', avatar: 'https://api.dicebear.com/7.x/avataaars/svg?seed=Alice' },
  { id: 'u2', name: 'Bob Smith', role: 'Developer', avatar: 'https://api.dicebear.com/7.x/avataaars/svg?seed=Bob' },
  { id: 'u3', name: 'Charlie Davis', role: 'Developer', avatar: 'https://api.dicebear.com/7.x/avataaars/svg?seed=Charlie' },
  { id: 'u4', name: 'Diana Prince', role: 'Manager', avatar: 'https://api.dicebear.com/7.x/avataaars/svg?seed=Diana' },
];

export const mockTasks: Task[] = [
  { id: 't1', title: 'Setup Local LLM Tooling', status: 'In Progress', assigneeId: 'u2', dueDate: '2026-06-20', priority: 'High', progress: 45 },
  { id: 't2', title: 'Implement RBAC Middleware', status: 'Done', assigneeId: 'u1', dueDate: '2026-06-15', priority: 'High', progress: 100 },
  { id: 't3', title: 'Design Analytics Dashboard', status: 'Todo', assigneeId: 'u4', dueDate: '2026-06-22', priority: 'Medium', progress: 0 },
  { id: 't4', title: 'LangChain Agent Integration', status: 'Review', assigneeId: 'u3', dueDate: '2026-06-19', priority: 'High', progress: 90 },
  { id: 't5', title: 'Dark Mode Support', status: 'Done', assigneeId: 'u2', dueDate: '2026-06-18', priority: 'Low', progress: 100 },
];

export const mockProgress: DailyProgress[] = [
  { date: 'Mon', completed: 4, added: 6, expected: 5 },
  { date: 'Tue', completed: 7, added: 2, expected: 6 },
  { date: 'Wed', completed: 5, added: 4, expected: 7 },
  { date: 'Thu', completed: 9, added: 1, expected: 8 },
  { date: 'Fri', completed: 12, added: 3, expected: 10 },
  { date: 'Sat', completed: 14, added: 0, expected: 12 },
  { date: 'Sun', completed: 16, added: 2, expected: 14 },
];

export const mockContributions: Contribution[] = Array.from({ length: 30 }).map((_, i) => {
  const d = new Date();
  d.setDate(d.getDate() - (29 - i));
  return {
    date: d.toISOString().split('T')[0],
    count: Math.floor(Math.random() * 10),
  };
});
