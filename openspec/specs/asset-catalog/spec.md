# asset-catalog Specification

## Purpose

Defines which assets can be traded in a simulation, how they are grouped into asset classes, and the metadata needed to price and trade each one.

## Requirements

### Requirement: Asset classes
The system SHALL organise tradable assets into exactly these asset classes: crypto, stocks, ETFs, commodities and forex. Every asset MUST belong to exactly one class.

#### Scenario: Listing asset classes
- **WHEN** a signed-in user requests the asset classes
- **THEN** the five classes are returned, each with the number of assets in it

### Requirement: Seeded top-10 lists
The system SHALL ship with a curated list of 10 assets for each asset class, available to every user without any setup. Stablecoins MUST NOT appear in the crypto list.

#### Scenario: Fresh installation
- **WHEN** the platform is started for the first time and a user opens an asset class
- **THEN** that class shows its 10 seeded assets

#### Scenario: Seed applied twice
- **WHEN** the seed is applied to a catalog that already contains the seeded assets
- **THEN** no duplicate assets are created and existing assets keep their identity

### Requirement: Asset metadata
Each asset SHALL expose its display name, symbol, asset class, quote currency, smallest tradable quantity step, minimum order size where the venue defines one, the trading-hours calendar it follows, and the data source currently used to price it.

#### Scenario: Viewing a crypto asset
- **WHEN** a user views Bitcoin
- **THEN** the asset shows a quote currency, a fractional quantity step and minimum order size taken from the exchange, and a calendar that is always open

#### Scenario: Viewing a stock
- **WHEN** a user views a US-listed stock
- **THEN** the asset shows USD as quote currency, a quantity step of one whole share, and the calendar of its listing exchange

### Requirement: Seed availability check
The system SHALL check each seeded asset against the market-data sources and mark an asset unavailable when no source can price it. Unavailable assets MUST remain visible, MUST be labelled as unavailable and MUST NOT be tradable.

#### Scenario: Seeded asset not on the preferred source
- **WHEN** a seeded crypto asset is not listed on the preferred exchange but is listed on a fallback source
- **THEN** the asset is available and shows the fallback source as its data source

#### Scenario: Seeded asset on no source
- **WHEN** no configured source can price a seeded asset
- **THEN** the asset is shown as unavailable and an attempt to trade it is refused

### Requirement: Asset search
The system SHALL let a user search by symbol or name for assets beyond the seeded lists and add a found asset to the catalog, provided at least one source can price it. Assets added this way are available to all users.

#### Scenario: Adding an asset found by search
- **WHEN** a user searches for a symbol that a source lists and adds the result
- **THEN** the asset joins the catalog in the asset class the source reports for it and can be added to portfolios

#### Scenario: No match
- **WHEN** a user searches for text that matches nothing on any source
- **THEN** an empty result is returned and the catalog is unchanged

#### Scenario: Instrument type not supported
- **WHEN** a search result is an instrument that fits none of the five asset classes, such as an option or a mutual fund
- **THEN** it cannot be added and the user is told the instrument type is not supported
