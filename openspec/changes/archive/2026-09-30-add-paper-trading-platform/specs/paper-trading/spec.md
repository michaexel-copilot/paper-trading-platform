## Purpose

Lets a user buy and sell assets inside a portfolio as a simulation, with orders that fill at real market prices under the conditions a real order would face.

## ADDED Requirements

### Requirement: Place an order
The system SHALL let a user place a buy or sell order in one of their portfolios for an available catalog asset, as a market, limit or stop order, stating a quantity. Limit orders MUST state a limit price and stop orders a stop price. The quantity MUST be positive, a multiple of the asset's quantity step and not below the asset's minimum order size.

#### Scenario: Valid market order
- **WHEN** a user places a market buy for a valid quantity of an asset whose market is open
- **THEN** the order is accepted

#### Scenario: Quantity not on the step
- **WHEN** a user orders 1.5 units of an asset whose quantity step is 1
- **THEN** the order is refused and the user is told the permitted step

#### Scenario: Below minimum size
- **WHEN** a user orders less than the asset's minimum order size
- **THEN** the order is refused and the user is told the minimum

#### Scenario: Limit order without a price
- **WHEN** a user places a limit order without a limit price
- **THEN** the order is refused

### Requirement: Long-only, fully funded trading
The system MUST refuse a buy order whose estimated cost including the fee exceeds the portfolio's available cash, and MUST refuse a sell order whose quantity exceeds the quantity held minus the quantity already committed to open sell orders. Short selling and borrowing are not possible.

#### Scenario: Insufficient cash
- **WHEN** a user places a buy order costing more than the available cash once the fee is included
- **THEN** the order is refused and the shortfall is stated

#### Scenario: Selling more than held
- **WHEN** a user holding 2 units with an open sell order for 1 unit places another sell order for 2 units
- **THEN** the order is refused because only 1 unit is uncommitted

### Requirement: Market order execution
A market order SHALL fill immediately and in full at the current quote: a buy at the ask and a sell at the bid. When the quote is last-price-only, the fill price SHALL be the last price adjusted by the assumed spread of the applicable fee profile. A market order MUST be refused when the asset's market is closed or no fresh quote is available, and the refusal MUST state the reason and, for a closed market, the next opening time.

#### Scenario: Buy at the ask
- **WHEN** a market buy is placed while the quote shows bid 99 and ask 101
- **THEN** the order fills at 101

#### Scenario: Sell at the bid
- **WHEN** a market sell is placed while the quote shows bid 99 and ask 101
- **THEN** the order fills at 99

#### Scenario: Market closed
- **WHEN** a market order for a stock is placed on a Saturday
- **THEN** the order is refused and the response gives the exchange's next opening time

#### Scenario: No fresh quote
- **WHEN** a market order is placed while the only quote obtainable is stale
- **THEN** the order is refused and nothing is filled

### Requirement: Limit order execution
A limit order that can be filled at the current quote when it is placed SHALL fill immediately at that quote as a price-taking order. Otherwise it SHALL stay open and SHALL fill in full at its limit price as a price-making order once a fresh quote, observed while the market is open, shows the ask at or below the limit for a buy or the bid at or above the limit for a sell. Limit orders may be placed while the market is closed.

#### Scenario: Immediately fillable limit buy
- **WHEN** a limit buy at 105 is placed while the ask is 101
- **THEN** the order fills at once at 101 and is charged the taker rate

#### Scenario: Resting limit buy fills later
- **WHEN** a limit buy at 95 is open and a fresh quote shows the ask at 94
- **THEN** the order fills at 95 and is charged the maker rate

#### Scenario: Price reached only while closed
- **WHEN** the only quotes at or beyond the limit price were observed while the market was closed
- **THEN** the order remains open

### Requirement: Stop order execution
A stop order SHALL stay open until a fresh quote, observed while the market is open, shows the last price at or below the stop price for a sell or at or above the stop price for a buy. It SHALL then fill in full as a market order at the quote that triggered it, which may be worse than the stop price.

#### Scenario: Stop-loss triggered
- **WHEN** a sell stop at 90 is open and a fresh quote shows last 89, bid 88.5
- **THEN** the order fills at 88.5 and is charged the taker rate

#### Scenario: Gap through the stop
- **WHEN** a sell stop at 90 is open and the first quote after the market opens shows bid 80
- **THEN** the order fills at 80

### Requirement: Cash and holdings reserved by open orders
An open buy order SHALL reserve its estimated cost including the fee from available cash, using the limit price for a limit order and the stop price for a stop order. An open sell order SHALL commit its quantity. Reservations SHALL be released when the order fills, is cancelled or is rejected. When an open buy order becomes fillable and its actual cost exceeds the cash balance, the order SHALL be rejected with the reason recorded instead of filling.

#### Scenario: Cancelling releases the reservation
- **WHEN** an open limit buy reserving 2000 is cancelled
- **THEN** available cash rises by 2000

#### Scenario: Stop buy costing more than expected
- **WHEN** a buy stop triggers at a price that makes the cost exceed the portfolio's cash balance
- **THEN** the order is rejected with reason insufficient cash and the cash balance is unchanged

### Requirement: Timely evaluation of open orders
The system SHALL evaluate every open order against a new quote at least every 10 seconds for crypto and at least every 60 seconds for other asset classes while the asset's market is open. Open orders MUST continue to be evaluated when no user is signed in.

#### Scenario: Fill while signed out
- **WHEN** a resting limit order's price is reached while its owner is signed out
- **THEN** the order fills and the owner sees the fill, with its time, on their next visit

### Requirement: Currency conversion on fills
When the asset's quote currency differs from the portfolio's base currency, the system SHALL convert the fill value and fee to the base currency at the conversion rate current at the time of the fill and SHALL record that rate on the trade.

#### Scenario: USD asset bought in a EUR portfolio
- **WHEN** a EUR portfolio buys 10 units at 100 USD while 1 USD converts to 0.90 EUR
- **THEN** cash falls by 900 EUR plus the fee and the trade records a rate of 0.90

### Requirement: Atomic fills
A fill SHALL change the order status, the cash balance, the position and the trade history together or not at all, and an order MUST NOT fill more than once.

#### Scenario: Failure during a fill
- **WHEN** recording a fill fails part-way
- **THEN** the order is still open, and cash, position and trade history are unchanged

#### Scenario: Repeated submission
- **WHEN** the same order submission is received twice, for example after a double click or a retried request
- **THEN** exactly one order exists

### Requirement: Order lifecycle and cancellation
Every order SHALL have exactly one status: open, filled, cancelled or rejected. A user SHALL be able to cancel an open order. Filled, cancelled and rejected orders MUST NOT change status again. Open orders stay open until filled, cancelled or rejected.

#### Scenario: Cancelling an open order
- **WHEN** a user cancels an open order
- **THEN** its status becomes cancelled and it can no longer fill

#### Scenario: Cancelling a filled order
- **WHEN** a user tries to cancel an order that has filled
- **THEN** the cancellation is refused and the order stays filled

### Requirement: Order and trade history
The system SHALL keep every order and every fill permanently for the life of the portfolio. Each trade record SHALL show the time, asset, side, quantity, fill price, the data source and observation time of the quote used, the conversion rate, the fee and the fee profile applied, and the resulting cash change. Trade records MUST NOT be editable. The user SHALL be able to download a portfolio's trades as a CSV file.

#### Scenario: Inspecting a trade
- **WHEN** a user opens a past trade
- **THEN** the price, its source and observation time, the conversion rate and the fee are all shown

#### Scenario: CSV export
- **WHEN** a user downloads the trades of a portfolio with three fills
- **THEN** the file contains a header row and three rows in time order
