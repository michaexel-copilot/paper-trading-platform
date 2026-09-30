# user-accounts Specification

## Purpose

Lets people create an account and sign in so that each person's simulated portfolios, orders and trades are private to them.

## Requirements

### Requirement: Account registration
The system SHALL let a visitor create an account with an email address and a password. Email addresses MUST be unique, compared case-insensitively. Passwords MUST be at least 10 characters long.

#### Scenario: Successful registration
- **WHEN** a visitor submits an unused email address and a password of at least 10 characters
- **THEN** an account is created and the visitor is signed in

#### Scenario: Email already registered
- **WHEN** a visitor submits an email address that differs only in letter case from an existing account's
- **THEN** no account is created and the visitor is told the email address is already in use

#### Scenario: Password too short
- **WHEN** a visitor submits a password shorter than 10 characters
- **THEN** no account is created and the visitor is told the minimum length

### Requirement: Sign in and sign out
The system SHALL let a registered user sign in with their email address and password and sign out again. A failed sign-in MUST NOT reveal whether the email address is registered.

#### Scenario: Successful sign-in
- **WHEN** a user submits their registered email address and correct password
- **THEN** a session is started and the user can reach their portfolios

#### Scenario: Wrong credentials
- **WHEN** a sign-in is attempted with an unknown email address or a wrong password
- **THEN** sign-in is refused with the same message in both cases

#### Scenario: Sign out
- **WHEN** a signed-in user signs out
- **THEN** the session is ended and further requests with it are refused as unauthenticated

### Requirement: Sign-in attempt limiting
The system SHALL refuse further sign-in attempts for an email address for 15 minutes after 5 consecutive failed attempts for that address.

#### Scenario: Too many failed attempts
- **WHEN** a sixth sign-in is attempted for an email address within 15 minutes of 5 consecutive failures
- **THEN** the attempt is refused without checking the password and the response states when sign-in can be retried

### Requirement: Session expiry
A session SHALL expire after 30 days without use. The session credential MUST NOT be readable by scripts running in the browser.

#### Scenario: Expired session
- **WHEN** a request arrives with a session that has not been used for more than 30 days
- **THEN** the request is refused as unauthenticated

### Requirement: Password storage
The system MUST store passwords only as salted hashes produced by a memory-hard password hashing function, and MUST NOT return a password or its hash in any response or write it to a log.

#### Scenario: Account data returned
- **WHEN** a signed-in user requests their own account details
- **THEN** the response contains the email address and creation date and contains neither the password nor its hash

### Requirement: Data isolation between users
The system SHALL restrict every portfolio, order, trade and fee setting to the user who owns it. Requests without a valid session MUST be refused for everything except registration, sign-in and the health check.

#### Scenario: Unauthenticated request
- **WHEN** a request for portfolios arrives without a valid session
- **THEN** it is refused as unauthenticated

#### Scenario: Another user's portfolio
- **WHEN** a signed-in user requests, changes or trades in a portfolio owned by a different user
- **THEN** the system responds as if that portfolio does not exist
