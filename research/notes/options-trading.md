---
title: Options Trading
id: options-trading
tags:
- alpaca-options
created: '2026-06-17T20:04:35.418379Z'
updated: '2026-06-17T20:28:22.830881Z'
source: https://docs.alpaca.markets/docs/options-trading
source_domain: docs.alpaca.markets
fetched_at: '2026-06-17T20:04:35.418107Z'
fetch_provider: builtin
status: review
type: note
tier: ground_truth
content_type: docs
deprecated: false
summary: 'For AI agents: visit https://docs.alpaca.markets/llms.txt for an index of
  all pages formatted in Markdown and endpoin...'
---

Options Trading
For AI agents: visit https://docs.alpaca.markets/llms.txt for an index of all pages formatted in Markdown and endpoints in OpenAPI.
🎉
Options trading is now available on Live!
Share your feedback on
Options API for Trading API here
OpenAPI Spec
You can find our Open API docs here:
<https://docs.alpaca.markets/reference>
.
Enablement
In the Paper environment, options trading capability will be enabled by default - there's nothing you need to do!
Note, in production there will be a more robust experience to request options trading.
For those who do not wish to have options trading enabled, you can disable options trading by navigating to your Trading Dashboard; Account > Configure.
The
Trading Account
model was updated to reflect the account's options approved and trading levels.
The
Account Configuration
model was updated to show the maximum options trading level, and allow a user to downgrade to a lower level.  Note, a different API will be provided for requesting a level upgrade for live account.
Trading Levels
Alpaca supports the below options trading levels.
Level
Supported Trades
Validation
0
Options trading is disabled
NA
1
Sell a covered call
Sell cash-secured put
User must own sufficient underlying shares
User must have sufficient options buying power
2
Level 1
Buy a call
Buy a put
User must have sufficient options buying power
3
Level 1,2
Buy a call spread
Buy a put spread
User must have sufficient options buying power
Option Contracts
Assets API Update
The
Assets
endpoint has an existing
attributes
query parameter.  Symbols which have option contracts will have an attribute called
options_enabled
.
Querying for symbols with the
options_enabled
attribute allows users to identify the universe of symbols with corresponding option contracts.
Fetch Contracts
To obtain contract details, a new endpoint was introduced:
/v2/options/contracts?underlying_symbols=
. The endpoint supports fetching a single contract such as
/v2/options/contracts/{symbol_or_id}
The default params are:
expiration_date_lte: Next weekend
limit: 100
Example: if
/v2/options/contracts
is called on Thursday, the response will include Thursday and Friday data.  If called on a Saturday, the response will include Saturday, Sunday, Monday, Tuesday, Wednesday, Thursday, and Friday.
Here is an example of a response object:
JSON
{
    "option_contracts": [
        {
            "id": "6e58f870-fe73-4583-81e4-b9a37892c36f",
            "symbol": "AAPL240119C00100000",
            "name": "AAPL Jan 19 2024 100 Call",
            "status": "active",
            "tradable": true,
            "expiration_date": "2024-01-19",
            "root_symbol": "AAPL",
            "underlying_symbol": "AAPL",
            "underlying_asset_id": "b0b6dd9d-8b9b-48a9-ba46-b9d54906e415",
            "type": "call",
            "style": "american",
            "strike_price": "100",
            "size": "100",
            "open_interest": "6168",
            "open_interest_date": "2024-01-12",
            "close_price": "85.81",
            "close_price_date": "2024-01-12"
        },
...
	],
   "page_token": "MTAw",
   "limit": 100
}
More detailed information regarding this endpoint can be found on the OpenAPI spec
here
.
Market Data
Alpaca offers both
real-time
and
historical
option data.
Placing an Order
Placing an order for an option contract uses the same
Orders API
that is used for equities and crypto asset classes.
Given the same Orders API endpoint is being used, Alpaca has implemented a series of validations to ensure the options order does not include attributes relevant to other asset classes.  Some of these validations include:
Ensuring
qty
is a whole number
Notional
must not be populated
time_in_force
must be
day
or
gtc
extended_hours
must be
false
or not populated
type
must be
market
,
limit
,
stop
or ,
stop_limit
(
stop
and
stop_limit
are only available for single-leg orders)
Examples of valid order payloads can be found as a child page
here
.
Options Positions
Good news - the existing
Positions API
model will work with options contracts.  There is not expected to be a change to this model.
As an additive feature, we aim to support fetching positions of a certain asset class; whether that be options, equities, or crypto.
Account Activities
The existing schema for trade activities (TAs) are applicable for the options asset class.  For example, the
FILL
activity is applicable to options and maintains it's current shape for options.
There are new non-trade activities (NTAs) entry types for options, however the schema stays the same.  These NTAs reflect exercise, assignment, and expiry.  More details can be found on a child page
here
and on the OpenAPI spec
here
.
🚧
On PAPER NTAs are synced at the start of the following day. While your balance and positions are updated instantly, NTAs on PAPER will be visible in the Activities endpoint only the next day
Exercise and DNE Instructions
Exercise
Contract holders may submit exercise instructions to Alpaca.  Alpaca will process instructions and work with our clearing partner accordingly.
All available held shares of this option contract will be exercised. By default, Alpaca will automatically exercise in-the-money (ITM) contracts at expiry.
Exercise requests will be processed immediately once received. Exercise requests submitted between market close and midnight will be rejected to avoid any confusion about when the exercise will settle.
Endpoint:
POST /v2/positions/{symbol_or_contract_id}/exercise
(no body)
More details in our OpenAPI Spec
here
.
Do Not Exercise
To submit a Do-not-exercise (DNE) instruction, please contact our support team.
Expiration
In the event no instruction is provided on an ITM contract, the Alpaca system will exercise the contract as long as it is ITM by at least $0.01 USD.
Alpaca Operations has tooling and processes in place to identify accounts which pose a buying power risk with ITM contracts.
In the event the account does not have sufficient buying power to exercise an ITM position, Alpaca will sell-out the position within 1 hour before expiry.
Assignment
Options assignments are not delivered through websocket events. To check for assignment activity (non-trade activity, or NTA events), you’ll need to poll the REST API endpoints. Websocket support for NTAs is not currently available.
FAQ
Please see our full FAQs here:
https://alpaca.markets/support/tag/options
Copy Page