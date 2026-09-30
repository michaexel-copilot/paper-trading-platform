## Purpose

Supplies real market prices for every asset in the catalog from interchangeable external sources, and tells the rest of the system how fresh and how trustworthy each price is.

## ADDED Requirements

### Requirement: Current quotes
The system SHALL provide a current quote for any available asset, containing the last price, the bid and ask when the source publishes them, the quote currency, the source that supplied it and the time the price was observed at the source.

#### Scenario: Quote with bid and ask
- **WHEN** a quote is requested for an asset whose source publishes an order book top
- **THEN** the quote contains last, bid and ask prices, the source name and the observation time

#### Scenario: Quote without bid and ask
- **WHEN** the source publishes only a last price for the asset
- **THEN** the quote contains the last price, has no bid or ask, and is marked as last-price-only

### Requirement: Pluggable sources with a uniform interface
The system SHALL obtain market data through source adapters that expose the same operations — list instruments, fetch a quote, fetch price history — so that a source can be added or replaced without changing portfolio or trading behaviour. The first version MUST include an OKX adapter for crypto and a Yahoo Finance adapter for stocks, ETFs, commodities and forex.

#### Scenario: Crypto priced from OKX
- **WHEN** a quote is requested for a crypto asset listed on OKX
- **THEN** the quote's source is OKX

#### Scenario: Stock priced from Yahoo
- **WHEN** a quote is requested for a seeded stock
- **THEN** the quote's source is Yahoo Finance

### Requirement: Source fallback
Each asset class SHALL have an ordered chain of sources. The system SHALL use the first source in the chain that lists the asset, and SHALL fall through to the next source when the current one does not list the asset or fails to answer. The chain MUST be changeable through configuration without a code change.

#### Scenario: Asset not listed on the first source
- **WHEN** a crypto asset is not listed on OKX and is listed on the next source in the crypto chain
- **THEN** quotes for it come from that next source and name it as the source

#### Scenario: First source temporarily failing
- **WHEN** the source normally used for an asset returns errors or times out and a later source in the chain lists the asset
- **THEN** the quote is served from the later source and names it as the source

#### Scenario: Every source failing
- **WHEN** no source in the chain can supply a quote for an asset
- **THEN** the request fails with a data-unavailable error and no price is invented or estimated

### Requirement: Quote freshness
The system SHALL classify each quote as fresh or stale by comparing its observation time to the current time against a per-asset-class limit: 30 seconds for crypto and 20 minutes for every other class. A quote for an asset whose market is closed is stale regardless of age.

#### Scenario: Fresh crypto quote
- **WHEN** a crypto quote was observed 5 seconds ago
- **THEN** it is classified as fresh

#### Scenario: Delayed stock quote during market hours
- **WHEN** a stock quote was observed 15 minutes ago while its exchange is open
- **THEN** it is classified as fresh and is labelled as delayed

#### Scenario: Old quote
- **WHEN** a quote is older than its asset class's limit
- **THEN** it is classified as stale

### Requirement: Market status
The system SHALL report for each asset whether its market is currently open and, when closed, the time it next opens. Crypto is always open. Stocks and ETFs follow their listing exchange's sessions and holidays. Forex is open from Sunday 22:00 UTC to Friday 22:00 UTC. Commodities follow the sessions of the exchange their price comes from.

#### Scenario: Stock on a weekend
- **WHEN** the market status of a US-listed stock is requested on a Saturday
- **THEN** the market is reported closed with the next opening time of its exchange

#### Scenario: Crypto at any time
- **WHEN** the market status of a crypto asset is requested
- **THEN** the market is reported open

#### Scenario: Exchange holiday
- **WHEN** the market status of a stock is requested on a weekday that is a holiday for its exchange
- **THEN** the market is reported closed

### Requirement: Price history
The system SHALL provide historical open, high, low, close and volume bars for an asset at daily resolution for at least the past five years where the source has them, and at hourly resolution for at least the past 30 days.

#### Scenario: Daily history
- **WHEN** one year of daily history is requested for a seeded asset
- **THEN** bars are returned in ascending time order with one bar per trading day

#### Scenario: Range before the asset existed
- **WHEN** history is requested for a period before the asset's first available bar
- **THEN** only the bars that exist are returned and the response is not an error

### Requirement: Currency conversion rates
The system SHALL provide a conversion rate between any asset's quote currency and any supported portfolio base currency, sourced from the same market-data sources and subject to the same freshness rules as forex quotes. Prices quoted in USDT SHALL be treated as USD at a rate of one to one.

#### Scenario: USD asset in a EUR portfolio
- **WHEN** a rate from USD to EUR is requested
- **THEN** a rate derived from the current EUR/USD quote is returned with its source and observation time

#### Scenario: Same currency
- **WHEN** a rate is requested between a currency and itself
- **THEN** the rate is exactly 1

### Requirement: Limited load on sources
The system SHALL respect each source's published or configured request limits and SHALL serve repeated requests for the same asset from a recent cached answer instead of contacting the source again, as long as the cached answer is no older than 5 seconds for crypto and 60 seconds for other classes.

#### Scenario: Many viewers of one asset
- **WHEN** many quote requests for the same stock arrive within 60 seconds
- **THEN** the source is contacted at most once and every request receives the same observation time
