## Purpose

Lets a user run one or more simulated portfolios, each with its own cash, tracked assets and positions, and shows what each portfolio is worth and how it has performed after costs.

## ADDED Requirements

### Requirement: Create a portfolio
The system SHALL let a signed-in user create a portfolio by giving a name, a base currency of EUR or USD, and a starting cash amount greater than zero. Portfolio names MUST be unique among that user's portfolios. The base currency and starting cash MUST NOT be changeable after creation.

#### Scenario: Successful creation
- **WHEN** a user creates a portfolio named "Momentum" with base currency EUR and starting cash 10000
- **THEN** the portfolio exists with 10000 EUR cash, no positions and a total value of 10000 EUR

#### Scenario: Duplicate name
- **WHEN** a user creates a portfolio with a name one of their own portfolios already has
- **THEN** creation is refused and the user is told the name is taken

#### Scenario: Invalid starting cash
- **WHEN** a user submits a starting cash amount of zero or less
- **THEN** creation is refused

### Requirement: Manage portfolios
The system SHALL let a user list their portfolios, rename a portfolio and delete a portfolio. Deleting a portfolio SHALL permanently remove its positions, orders, trades and history and MUST require explicit confirmation.

#### Scenario: Listing
- **WHEN** a user lists their portfolios
- **THEN** each portfolio is shown with its name, base currency, current total value and total return

#### Scenario: Deleting
- **WHEN** a user confirms deletion of a portfolio
- **THEN** the portfolio and everything in it are gone and its open orders can no longer fill

### Requirement: Tracked assets
The system SHALL let a user add catalog assets to a portfolio and remove them again. A tracked asset is shown in the portfolio with its current quote whether or not the portfolio holds a position in it. An asset MUST NOT be removable while the portfolio holds a position in it or has an open order for it. Trading an asset that is not yet tracked SHALL add it to the portfolio.

#### Scenario: Adding an asset
- **WHEN** a user adds a catalog asset to a portfolio
- **THEN** the asset appears in the portfolio with its current quote and a position of zero

#### Scenario: Removing an asset with a position
- **WHEN** a user tries to remove an asset the portfolio holds a position in
- **THEN** removal is refused and the user is told to close the position first

#### Scenario: Adding an unavailable asset
- **WHEN** a user adds an asset that is marked unavailable
- **THEN** the asset is refused

### Requirement: Cash balance
Each portfolio SHALL keep a cash balance in its base currency that changes only through fills. The system SHALL report available cash as the cash balance minus cash reserved for open buy orders. The cash balance MUST never be negative.

#### Scenario: Cash after a purchase
- **WHEN** a buy order fills for a total cost of 1000 plus a fee of 1, both in the base currency
- **THEN** the cash balance falls by 1001

#### Scenario: Cash reserved by an open order
- **WHEN** a portfolio with 5000 cash has an open limit buy order reserving 2000
- **THEN** the cash balance is 5000 and available cash is 3000

### Requirement: Positions
The system SHALL show, for each asset held, the quantity, the average cost per unit, the current price, the market value in the base currency and the unrealised profit or loss in the base currency and as a percentage. Average cost SHALL be computed by the average-cost method from buy fills converted to the base currency at each fill's conversion rate, and SHALL exclude fees.

#### Scenario: Two purchases at different prices
- **WHEN** a portfolio buys 1 unit at 100 and later 1 unit at 200 in its base currency
- **THEN** the position is 2 units with an average cost of 150

#### Scenario: Partial sale
- **WHEN** a portfolio holding 2 units at an average cost of 150 sells 1 unit at 180
- **THEN** the position is 1 unit at an average cost of 150 and realised profit rises by 30

#### Scenario: Position closed
- **WHEN** the full quantity of a position is sold
- **THEN** the position no longer appears among holdings and the asset remains tracked

### Requirement: Portfolio valuation
The system SHALL report a portfolio's total value as its cash balance plus the market value of every position at its latest quote, converted to the base currency at the current rate. When any position is valued with a stale quote, the valuation MUST be marked accordingly and MUST state the age of the oldest quote used.

#### Scenario: Mixed currencies
- **WHEN** a EUR portfolio holds a USD-quoted position
- **THEN** the position's market value is its quantity times its latest price converted to EUR at the current rate

#### Scenario: Market closed
- **WHEN** a portfolio is valued while one of its positions' markets is closed
- **THEN** the last available price is used for that position and the valuation is marked as containing stale prices

#### Scenario: No price available
- **WHEN** no quote at all can be obtained for a held asset
- **THEN** the position is valued at its most recently recorded price and is flagged as unpriced

### Requirement: Performance figures
The system SHALL report for each portfolio the total return as an amount and as a percentage of starting cash, the realised profit or loss, the unrealised profit or loss and the total fees paid, all in the base currency. Total return MUST be net of fees.

#### Scenario: Return after fees
- **WHEN** a portfolio started with 10000, has a total value of 10450 and has paid 50 in fees
- **THEN** the total return is 450 and 4.5 percent, and fees paid is shown as 50

### Requirement: Value history
The system SHALL record each portfolio's total value at least once per day and after every fill, and SHALL present the recorded values as a time series from the portfolio's creation.

#### Scenario: Chart after a week
- **WHEN** a user opens the value history of a portfolio created seven days ago
- **THEN** the series starts at the starting cash on the creation date and has at least one point for each day since

#### Scenario: Point recorded on fill
- **WHEN** an order fills
- **THEN** a new value point is recorded with the time of the fill
