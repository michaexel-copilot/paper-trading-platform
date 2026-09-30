import { useMutation } from '@tanstack/react-query'
import { useState, type FormEvent } from 'react'
import { Navigate, useLocation, useNavigate } from 'react-router'
import { api, ApiError, errorMessage } from '../api/client'
import type { User } from '../api/types'
import { useSession, useUser } from '../auth'
import { formatDateTime } from '../lib/format'

const MIN_PASSWORD_LENGTH = 10

export default function LoginPage() {
  const [mode, setMode] = useState<'login' | 'register'>('login')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const { data: user } = useUser()
  const session = useSession()
  const navigate = useNavigate()
  const location = useLocation()
  const target = (location.state as { from?: string } | null)?.from ?? '/portfolios'

  const submit = useMutation({
    mutationFn: () =>
      api<User>(`/api/auth/${mode}`, { method: 'POST', body: { email, password } }),
    onSuccess: (signedIn) => {
      session.signedIn(signedIn)
      navigate(target, { replace: true })
    },
  })

  if (user) return <Navigate to={target} replace />

  function onSubmit(event: FormEvent) {
    event.preventDefault()
    submit.mutate()
  }

  function switchMode(next: 'login' | 'register') {
    setMode(next)
    submit.reset()
  }

  const tooShort = mode === 'register' && password.length > 0 && password.length < MIN_PASSWORD_LENGTH
  const error = submit.error
  const retryAt =
    error instanceof ApiError && error.code === 'locked_out'
      ? formatDateTime(String(error.detail.retry_at))
      : null

  return (
    <div className="auth">
      <h1>Paper Trading</h1>
      <p className="muted">Simulate portfolios with real prices and real fees.</p>
      <div className="tabs">
        <button className={mode === 'login' ? 'active' : ''} onClick={() => switchMode('login')}>
          Sign in
        </button>
        <button
          className={mode === 'register' ? 'active' : ''}
          onClick={() => switchMode('register')}
        >
          Create account
        </button>
      </div>
      <form onSubmit={onSubmit} className="stack">
        <label>
          Email
          <input
            type="email"
            name="email"
            autoComplete="email"
            required
            value={email}
            onChange={(e) => setEmail(e.target.value)}
          />
        </label>
        <label>
          Password
          <input
            type="password"
            name="password"
            autoComplete={mode === 'login' ? 'current-password' : 'new-password'}
            required
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </label>
        {mode === 'register' && (
          <p className={tooShort ? 'error' : 'muted'}>
            At least {MIN_PASSWORD_LENGTH} characters.
          </p>
        )}
        {error && (
          <p className="error" role="alert">
            {errorMessage(error)}
            {retryAt && ` You can try again at ${retryAt}.`}
          </p>
        )}
        <button type="submit" className="primary" disabled={submit.isPending || tooShort}>
          {mode === 'login' ? 'Sign in' : 'Create account'}
        </button>
      </form>
    </div>
  )
}
