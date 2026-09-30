import { Navigate, useLocation } from 'react-router'
import { useUser } from '../auth'

export default function RequireAuth({ children }: { children: React.ReactNode }) {
  const { data: user, isPending } = useUser()
  const location = useLocation()
  if (isPending) return <p className="muted pad">Loading…</p>
  if (!user) return <Navigate to="/login" replace state={{ from: location.pathname }} />
  return <>{children}</>
}
