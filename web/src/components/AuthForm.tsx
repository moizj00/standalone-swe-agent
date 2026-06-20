import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { authClient } from '../lib/auth-client'

interface AuthFormProps {
  mode: 'sign-in' | 'sign-up'
}

export function AuthForm({ mode }: AuthFormProps) {
  const navigate = useNavigate()
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [name, setName] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setError('')
    setLoading(true)

    try {
      if (mode === 'sign-up') {
        await authClient.signUp.email({
          email,
          password,
          name: name || email.split('@')[0],
        })
      } else {
        await authClient.signIn.email({
          email,
          password,
        })
      }
      navigate('/')
    } catch (err: any) {
      setError(err.message || 'Authentication failed')
      console.error('[v0] Auth error:', err)
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="min-h-screen bg-background flex items-center justify-center p-4">
      <div className="w-full max-w-md">
        <div className="bg-card rounded-lg border border-border shadow-lg p-8">
          <h1 className="text-2xl font-bold text-foreground mb-2">
            {mode === 'sign-in' ? 'Sign In' : 'Sign Up'}
          </h1>
          <p className="text-muted-foreground mb-6">
            {mode === 'sign-in'
              ? 'Enter your credentials to access your account'
              : 'Create a new account to get started'}
          </p>

          <form onSubmit={handleSubmit} className="space-y-4">
            {mode === 'sign-up' && (
              <div>
                <label htmlFor="name" className="block text-sm font-medium text-foreground mb-1">
                  Name
                </label>
                <input
                  id="name"
                  type="text"
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  placeholder="Your name"
                  className="w-full px-4 py-2 bg-input border border-input rounded-md text-foreground placeholder-muted-foreground focus:outline-none focus:ring-2 focus:ring-primary"
                />
              </div>
            )}

            <div>
              <label htmlFor="email" className="block text-sm font-medium text-foreground mb-1">
                Email
              </label>
              <input
                id="email"
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="your@email.com"
                required
                className="w-full px-4 py-2 bg-input border border-input rounded-md text-foreground placeholder-muted-foreground focus:outline-none focus:ring-2 focus:ring-primary"
              />
            </div>

            <div>
              <label htmlFor="password" className="block text-sm font-medium text-foreground mb-1">
                Password
              </label>
              <input
                id="password"
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder="••••••••"
                required
                minLength={8}
                className="w-full px-4 py-2 bg-input border border-input rounded-md text-foreground placeholder-muted-foreground focus:outline-none focus:ring-2 focus:ring-primary"
              />
            </div>

            {error && (
              <div className="p-3 bg-destructive/10 border border-destructive/20 rounded-md">
                <p className="text-sm text-destructive">{error}</p>
              </div>
            )}

            <button
              type="submit"
              disabled={loading}
              className="w-full py-2 px-4 bg-primary text-primary-foreground rounded-md font-medium hover:bg-primary/90 disabled:opacity-50 disabled:cursor-not-allowed transition"
            >
              {loading ? 'Loading...' : mode === 'sign-in' ? 'Sign In' : 'Sign Up'}
            </button>

            <p className="text-center text-sm text-muted-foreground mt-4">
              {mode === 'sign-in' ? "Don't have an account? " : 'Already have an account? '}
              <a
                href={mode === 'sign-in' ? '/sign-up' : '/sign-in'}
                className="text-primary hover:underline"
              >
                {mode === 'sign-in' ? 'Sign up' : 'Sign in'}
              </a>
            </p>
          </form>
        </div>
      </div>
    </div>
  )
}
