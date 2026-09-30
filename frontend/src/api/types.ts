import type { components } from './schema'

type Schemas = components['schemas']

export type User = Schemas['UserOut']
export type Asset = Schemas['AssetOut']
export type Quote = Schemas['QuoteOut']
export type MarketStatus = Schemas['MarketStatusOut']
export type History = Schemas['HistoryOut']
export type SearchResult = Schemas['SearchResultOut']
export type FeeProfile = Schemas['FeeProfileOut']
export type PortfolioSummary = Schemas['PortfolioSummary']
export type PortfolioDetail = Schemas['PortfolioDetail']
export type Position = Schemas['PositionOut']
export type Snapshot = Schemas['SnapshotOut']
export type Order = Schemas['OrderOut']
export type OrderIn = Schemas['OrderIn']
export type PreviewIn = Schemas['PreviewIn']
export type Preview = Schemas['PreviewOut']
export type Trade = Schemas['TradeOut']

export const ASSET_CLASSES = ['crypto', 'stocks', 'etfs', 'commodities', 'forex'] as const
export type AssetClass = (typeof ASSET_CLASSES)[number]

export const CLASS_LABELS: Record<string, string> = {
  crypto: 'Crypto',
  stocks: 'Stocks',
  etfs: 'ETFs',
  commodities: 'Commodities',
  forex: 'Forex',
}
