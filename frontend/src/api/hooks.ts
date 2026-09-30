import { useQuery } from '@tanstack/react-query'
import { api } from './client'
import type {
  Asset,
  FeeProfile,
  History,
  Order,
  PortfolioDetail,
  PortfolioSummary,
  Quote,
  SearchResult,
  Snapshot,
  Trade,
} from './types'

// Crypto quotes change every few seconds; everything else is cached for a minute upstream.
export const CRYPTO_POLL_MS = 5_000
export const DEFAULT_POLL_MS = 30_000

export function pollInterval(assetClasses: Iterable<string>): number {
  for (const assetClass of assetClasses) {
    if (assetClass === 'crypto') return CRYPTO_POLL_MS
  }
  return DEFAULT_POLL_MS
}

export function useAssets(assetClass?: string) {
  return useQuery({
    queryKey: ['assets', assetClass ?? 'all'],
    queryFn: () => api<Asset[]>(assetClass ? `/api/assets?asset_class=${assetClass}` : '/api/assets'),
    staleTime: 60_000,
  })
}

export function useAsset(assetId: number) {
  return useQuery({
    queryKey: ['asset', assetId],
    queryFn: () => api<Asset>(`/api/assets/${assetId}`),
    staleTime: 60_000,
  })
}

/** Quotes for the assets on screen only; polling stops while the tab is hidden. */
export function useQuotes(assetIds: number[], intervalMs: number) {
  const ids = [...assetIds].sort((a, b) => a - b).join(',')
  return useQuery({
    queryKey: ['quotes', ids],
    queryFn: () => api<Record<string, Quote | null>>(`/api/market/quotes?ids=${ids}`),
    enabled: ids.length > 0,
    refetchInterval: intervalMs,
  })
}

export function useHistory(assetId: number, resolution: '1d' | '1h', start: string) {
  return useQuery({
    queryKey: ['history', assetId, resolution, start],
    queryFn: () =>
      api<History>(
        `/api/market/history/${assetId}?resolution=${resolution}&start=${encodeURIComponent(start)}`,
      ),
    staleTime: 10 * 60_000,
    retry: false,
  })
}

export function useSearch(query: string) {
  return useQuery({
    queryKey: ['search', query],
    queryFn: () => api<SearchResult[]>(`/api/assets/search?q=${encodeURIComponent(query)}`),
    enabled: query.length > 0,
    staleTime: 60_000,
  })
}

export function usePortfolios() {
  return useQuery({
    queryKey: ['portfolios'],
    queryFn: () => api<PortfolioSummary[]>('/api/portfolios'),
    refetchInterval: DEFAULT_POLL_MS,
  })
}

/** Polls at the pace of the portfolio's own assets unless an interval is given. */
export function usePortfolio(portfolioId: number | undefined, intervalMs?: number) {
  return useQuery({
    queryKey: ['portfolio', portfolioId],
    queryFn: () => api<PortfolioDetail>(`/api/portfolios/${portfolioId}`),
    enabled: portfolioId !== undefined,
    refetchInterval: (query) =>
      intervalMs ?? pollInterval(query.state.data?.tracked.map((t) => t.asset.asset_class) ?? []),
  })
}

export function useOrders(portfolioId: number) {
  return useQuery({
    queryKey: ['portfolio', portfolioId, 'orders'],
    queryFn: () => api<Order[]>(`/api/portfolios/${portfolioId}/orders`),
    refetchInterval: DEFAULT_POLL_MS,
  })
}

export function useTrades(portfolioId: number) {
  return useQuery({
    queryKey: ['portfolio', portfolioId, 'trades'],
    queryFn: () => api<Trade[]>(`/api/portfolios/${portfolioId}/trades`),
    refetchInterval: DEFAULT_POLL_MS,
  })
}

export function useValueHistory(portfolioId: number) {
  return useQuery({
    queryKey: ['portfolio', portfolioId, 'history'],
    queryFn: () => api<Snapshot[]>(`/api/portfolios/${portfolioId}/history`),
    refetchInterval: DEFAULT_POLL_MS,
  })
}

export function useFeeProfiles() {
  return useQuery({
    queryKey: ['fee-profiles'],
    queryFn: () => api<FeeProfile[]>('/api/fee-profiles'),
    staleTime: Infinity,
  })
}

export function usePortfolioFeeProfiles(portfolioId: number) {
  return useQuery({
    queryKey: ['portfolio', portfolioId, 'fee-profiles'],
    queryFn: () => api<Record<string, FeeProfile>>(`/api/portfolios/${portfolioId}/fee-profiles`),
  })
}
