# fee-models Specification

## Purpose

Makes simulated results realistic by charging every fill the costs a real venue would charge, and makes those costs visible so a user can see what trading a strategy costs.

## Requirements

### Requirement: Fee profiles
The system SHALL provide named fee profiles, each describing how a fill is charged: a rate on the trade value that may differ for price-making and price-taking fills, a charge per unit, a fixed charge per order, a minimum and a maximum charge, and an assumed spread used when a quote has no bid and ask. Each profile SHALL state the venue or broker it represents, the asset classes it applies to, the published schedule it was taken from and the date that schedule was checked.

#### Scenario: Listing profiles
- **WHEN** a user lists the fee profiles for an asset class
- **THEN** each profile shows its name, its charges, its source reference and the date it was last checked

### Requirement: Built-in profiles
The system SHALL ship with at least one fee profile for every asset class, including the OKX spot schedule for crypto, a per-share commission broker profile and a flat-fee broker profile for stocks and ETFs, and a zero-commission profile. Each asset class SHALL have a default profile.

#### Scenario: Default for crypto
- **WHEN** a portfolio is created without choosing fee profiles
- **THEN** its crypto trades are charged according to the OKX spot profile

#### Scenario: Every class covered
- **WHEN** a portfolio is created without choosing fee profiles
- **THEN** each of the five asset classes has a profile assigned

### Requirement: Exchange-published crypto rates
For a crypto asset, the system SHALL charge the maker and taker rates the pricing exchange publishes for that market at its base tier, and SHALL fall back to the built-in profile's rates when the exchange publishes none.

#### Scenario: Exchange publishes rates
- **WHEN** a crypto market's exchange publishes a taker rate of 0.10 percent and a market buy of value 1000 fills
- **THEN** the fee is 1.00

#### Scenario: Exchange publishes no rates
- **WHEN** the pricing source publishes no fee rates for a crypto market
- **THEN** the built-in profile's rates are charged and the trade records that the fallback was used

### Requirement: Fee profile selection per portfolio
The system SHALL let a user choose, per portfolio and per asset class, which fee profile applies. A change SHALL apply only to fills that happen after it.

#### Scenario: Switching broker profile
- **WHEN** a user changes a portfolio's stock fee profile after two stock trades
- **THEN** the two existing trades keep their recorded fees and later stock fills use the new profile

### Requirement: Fee calculation
The system SHALL compute the fee of a fill as the rate times the trade value, plus the per-unit charge times the quantity, plus the fixed charge, then raised to the minimum and capped at the maximum where the profile defines them. The price-taking rate applies to market orders, triggered stop orders and limit orders that fill on placement; the price-making rate applies to limit orders that fill after resting. Fees SHALL be charged in cash in the portfolio's base currency, rounded half-up to two decimal places, and MUST never be negative.

#### Scenario: Percentage fee
- **WHEN** a fill of value 2000 is charged under a profile with a taker rate of 0.10 percent and no other charges
- **THEN** the fee is 2.00

#### Scenario: Minimum applies
- **WHEN** a fill of 10 shares is charged under a profile of 0.005 per share with a minimum of 1.00
- **THEN** the fee is 1.00

#### Scenario: Maximum applies
- **WHEN** a fill of 1000 shares worth 300 in total is charged under a profile of 0.005 per share with a maximum of 1 percent of trade value
- **THEN** the fee is 3.00

#### Scenario: Maker rate for a resting limit order
- **WHEN** a limit order fills after resting under a profile with a maker rate of 0.08 percent and a taker rate of 0.10 percent on a value of 1000
- **THEN** the fee is 0.80

#### Scenario: Fee in another currency
- **WHEN** a profile's charges are denominated in USD and the portfolio's base currency is EUR
- **THEN** the fee is computed in USD and converted to EUR at the fill's conversion rate before rounding

### Requirement: Assumed spread for last-price-only quotes
When a quote has no bid and ask, the system SHALL derive the fill price by moving the last price against the trader by half of the profile's assumed spread: upward for a buy and downward for a sell. When the quote has a bid and ask, the assumed spread MUST NOT be applied.

#### Scenario: Buy on a last-price-only quote
- **WHEN** a market buy fills on a last price of 100 under a profile with an assumed spread of 0.10 percent
- **THEN** the fill price is 100.05

#### Scenario: Real bid and ask present
- **WHEN** a market buy fills on a quote with an ask of 101 under a profile with an assumed spread
- **THEN** the fill price is 101

### Requirement: Fee preview
The system SHALL show the estimated fee, the estimated fill price and the estimated total cash effect of an order before the user confirms it, computed with the same rules as an actual fill.

#### Scenario: Preview matches fill
- **WHEN** a user previews a market order and confirms it while the quote is unchanged
- **THEN** the fee and total charged equal the previewed amounts

### Requirement: Fee reporting
Each trade SHALL record the fee charged and the name and rates of the profile applied. The system SHALL report the total fees paid per portfolio and per asset class.

#### Scenario: Fee totals
- **WHEN** a portfolio has paid fees of 2.00 on crypto trades and 3.00 on stock trades
- **THEN** its fee report shows 2.00 for crypto, 3.00 for stocks and 5.00 in total
