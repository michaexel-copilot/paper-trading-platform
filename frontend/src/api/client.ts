/** Raised for every non-2xx answer. `code` is the API's stable error code. */
export class ApiError extends Error {
  status: number
  code: string
  detail: Record<string, unknown>

  constructor(status: number, code: string, message: string, detail: Record<string, unknown>) {
    super(message)
    this.status = status
    this.code = code
    this.detail = detail
  }
}

export const UNAUTHORIZED_EVENT = 'api:unauthorized'

async function toError(response: Response): Promise<ApiError> {
  let detail: unknown
  try {
    detail = (await response.json()).detail
  } catch {
    detail = undefined
  }
  if (detail && typeof detail === 'object' && !Array.isArray(detail)) {
    const fields = detail as Record<string, unknown>
    return new ApiError(
      response.status,
      String(fields.code ?? 'error'),
      String(fields.message ?? response.statusText),
      fields,
    )
  }
  if (Array.isArray(detail)) {
    // Request validation errors: one entry per invalid field.
    const message = detail.map((item) => `${item.loc?.slice(1).join('.')}: ${item.msg}`).join('; ')
    return new ApiError(response.status, 'invalid_request', message, {})
  }
  const message = typeof detail === 'string' ? detail : response.statusText
  return new ApiError(response.status, 'error', message, {})
}

/** JSON request with the session cookie. Signals a lost session to the app. */
export async function api<T>(path: string, init: { method?: string; body?: unknown } = {}): Promise<T> {
  const response = await fetch(path, {
    method: init.method ?? 'GET',
    credentials: 'same-origin',
    headers: init.body === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: init.body === undefined ? undefined : JSON.stringify(init.body),
  })
  if (!response.ok) {
    if (response.status === 401 && !path.startsWith('/api/auth/')) {
      window.dispatchEvent(new Event(UNAUTHORIZED_EVENT))
    }
    throw await toError(response)
  }
  if (response.status === 204) return undefined as T
  return (await response.json()) as T
}

export function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : 'Something went wrong.'
}
