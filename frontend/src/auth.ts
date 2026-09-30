import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect } from 'react'
import { api, ApiError, UNAUTHORIZED_EVENT } from './api/client'
import type { User } from './api/types'

const ME = ['me']

/** The signed-in user, or null when nobody is signed in. */
export function useUser() {
  const queryClient = useQueryClient()
  const query = useQuery({
    queryKey: ME,
    queryFn: async () => {
      try {
        return await api<User>('/api/auth/me')
      } catch (error) {
        if (error instanceof ApiError && error.status === 401) return null
        throw error
      }
    },
    staleTime: Infinity,
    retry: false,
  })

  // Any request that comes back 401 means the session is gone.
  useEffect(() => {
    const onUnauthorized = () => queryClient.setQueryData(ME, null)
    window.addEventListener(UNAUTHORIZED_EVENT, onUnauthorized)
    return () => window.removeEventListener(UNAUTHORIZED_EVENT, onUnauthorized)
  }, [queryClient])

  return query
}

export function useSession() {
  const queryClient = useQueryClient()
  return {
    signedIn(user: User) {
      queryClient.clear()
      queryClient.setQueryData(ME, user)
    },
    async signOut() {
      await api('/api/auth/logout', { method: 'POST' })
      queryClient.clear()
      queryClient.setQueryData(ME, null)
    },
  }
}
