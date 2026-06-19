export type Role = 'Admin' | 'Manager' | 'Developer' | 'Viewer';
export type Status = 'Todo' | 'In Progress' | 'Review' | 'Done';

export interface User {
  id: string;
  name: string;
  role: Role;
  avatar: string;
}

export interface Task {
  id: string;
  title: string;
  status: Status;
  assigneeId: string;
  dueDate: string;
  priority: 'Low' | 'Medium' | 'High';
  description?: string;
  progress: number;
}

export interface DailyProgress {
  date: string;
  completed: number;
  added: number;
  expected: number;
}

export interface Contribution {
  date: string;
  count: number;
}
