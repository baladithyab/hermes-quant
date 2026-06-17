---
title: Collar (finance) - Wikipedia
id: collar-finance-wikipedia
tags:
- options-strategy
created: '2026-06-17T20:08:33.695460Z'
updated: '2026-06-17T20:28:23.019448Z'
source: https://en.wikipedia.org/wiki/Collar_(finance)
source_domain: en.wikipedia.org
fetched_at: '2026-06-17T20:08:33.695283Z'
fetch_provider: builtin
status: review
type: note
tier: institutional
content_type: article
deprecated: false
summary: Collar (finance) - Wikipedia
---

Collar (finance) - Wikipedia
Jump to content
From Wikipedia, the free encyclopedia
Stock options trading strategy
This article has multiple issues.
Please help
improve it
or discuss these issues on the
talk page
.
(
Learn how and when to remove these messages
)
This article
may be
confusing or unclear
to readers
.
Please help
clarify the article
. There might be a discussion about this on
the talk page
.
(
December 2013
)
(
Learn how and when to remove this message
)
This article
is written like a
personal reflection, personal essay, or argumentative essay
that states a Wikipedia editor's personal feelings or presents an original argument about a topic.
Please
help improve it
by rewriting it in an
encyclopedic style
.
(
December 2009
)
(
Learn how and when to remove this message
)
This article
needs additional citations for
verification
.
Please help
improve this article
by
adding citations to reliable sources
. Unsourced material may be challenged and removed.
Find sources:
"Collar" finance
–
news
·
newspapers
·
books
·
scholar
·
JSTOR
(
December 2009
)
(
Learn how and when to remove this message
)
(
Learn how and when to remove this message
)
In
finance
, a
collar
is an
option strategy
that limits the range of possible positive or negative returns on an
underlying
to a specific range.  A
collar
strategy is used as one of the ways to
hedge
against possible losses and it represents long put options financed with short call options.
[
1
]
The collar combines the strategies of the
protective put
and the
covered call
.
[
2
]
Equity collar
[
edit
]
Structure
[
edit
]
A collar is created by:
[
3
]
buying
the underlying asset
buying a
put option
at
strike price
, X (called the
floor
)
selling
a
call option
at strike price, X + a (called the
cap
).
These latter two are a short
risk reversal
position. So:
Underlying −
risk reversal
= Collar
The premium income from selling the call reduces the cost of purchasing the put. The amount saved depends on the strike price of the two options.
Most commonly, the two strikes are roughly equal distances from the current price. For example, an investor would insure against loss more than 20% in return for giving up gain more than 20%. In this case the cost of the two options should be roughly equal. In case the premiums are exactly equal, this may be called a zero-cost collar; the return is the same as if no collar was applied, provided that the ending price is between the two strikes.
On expiry the value (but not the profit) of the collar will be:
X if the price of the underlying is below X
the value of the underlying if the underlying is between X and X + a, inclusive
X + a, if the underlying is above X + a.
Example
[
edit
]
Consider an investor who owns 100 shares of a
stock
with a current
share
price of $5. An investor could construct a collar by buying one put with a strike price of $3 and selling one call with a strike price of $7. The collar would ensure that the gain on the portfolio will be no higher than $2 and the loss will be no worse than $2 (before deducting the net cost of the put option; i.e., the cost of the put option less what is received for selling the call option).
There are three possible scenarios when the
options expire
:
If the stock price is above the $7 strike price on the call he wrote, the person who bought the call from the investor will
exercise
the purchased call; the investor effectively sells the shares at the $7 strike price. This would lock in a $2 profit for the investor. He
only
makes a $2 profit (minus fees), no matter how high the share price goes. For example, if the stock price goes up to $11, the buyer of the call will exercise the option and the investor will sell the shares that he bought at $5 for $11, for a $6 profit, but must then pay out $11 – $7 = $4, making his profit only $2 ($6 − $4).  The premium paid for the put must then be subtracted from this $2 profit to calculate the total return on this investment.
If the stock price drops below the $3 strike price on the put then the investor may exercise the put and the person who sold it is forced to buy the investor's 100 shares at $3. The investor loses $2 on the stock but can lose
only
$2 (plus fees) no matter how low the price of the stock goes. For example, if the stock price falls to $1 then the investor exercises the put and has a $2 gain. The value of the investor's stock has fallen by $5 – $1 = $4. The call expires worthless (since the buyer does not exercise it) and the total net loss is $2 – $4 =  −$2.  The premium received for the call must then be added to reduce this $2 loss to calculate the total return on this investment.
If the stock price is between the two strike prices on the expiry date, both options expire unexercised and the investor is left with the 100 shares whose value is that stock price (×100), plus the cash gained from selling the call option, minus the price paid to buy the put option, minus fees.
One source of risk is counterparty risk. If the stock price expires below the $3 floor then the counterparty may default on the put contract, thus creating the potential for losses up to the full value of the stock (plus fees).
Interest Rate Collar
[
edit
]
Structure
[
edit
]
In an interest rate collar, the investor seeks to limit exposure to changing interest rates and at the same time lower its net premium obligations. Hence, the investor goes long on the cap (floor) that will save it money for a strike of X +(-) S1 but at the same time shorts a floor (cap) for a strike of X +(-) S2 so that the premium of one at least partially offsets the premium of the other. Here S1 is the maximum tolerable unfavorable change in payable interest rate and S2 is the maximum benefit of a favorable move in interest rates.
[
4
]
Example
[
edit
]
Consider an investor who has an obligation to pay floating 6 month LIBOR annually on a notional N and which (when invested) earns 6%. A rise in LIBOR above 6% will hurt said investor, while a drop will benefit him. Thus, it is desirable for him to purchase an interest rate cap which will pay him back in the case that the LIBOR rises above his level of comfort. Figuring that he is comfortable paying up to 7%, he buys an interest rate cap contract from a counterparty, where the counterparty will pay him the difference between the 6 month LIBOR and 7% when the LIBOR exceeds 7% for a premium of 0.08N. To offset this premium, he also sells an interest rate floor contract to a counterparty, where he will have to pay the difference between the 6 month LIBOR and 5% when the LIBOR falls below 5%. For this he receives a 0.075N premium, thus offsetting what he paid for the cap contract.
[
5
]
Now, he can face 3 scenarios:
Rising interest rates - he will pay a maximum of 7% on his original obligation. Anything over and above that will be offset by the payments he will receive under the cap agreement. Hence, the investor is not exposed to interest rate increases exceeding 1%.
Stationary interest rates - neither contract triggers, nothing happens
Falling interest rates - he will benefit from a fall in interest rates down to 5%. If they fall further, the investor will have to pay the difference under the floor agreement, while of course saving the same amount on the original obligation. Hence, the investor is not exposed to interest falls exceeding 1%.
Rationale
[
edit
]
In times of high
volatility
, or in
bear markets
, it can be useful to limit the
downside risk
to a portfolio. One obvious way to do this is to sell the stock. In the above example, if an investor just sold the stock, the investor would get $5. This may be fine, but it poses additional questions. Does the investor have an acceptable investment available to put the money from the sale into? What are the
transaction costs
associated with
liquidating
the portfolio? Would the investor rather just hold on to the stock? What are the tax consequences?
If it makes more sense to hold on to the stock (or other underlying asset), the investor can limit that downside risk that lies below the strike price on the put in exchange for giving up the upside above the strike price on the call. Another advantage is that the cost of setting up a collar is (usually) free or nearly free. The price received for selling the call is used to buy the put—one pays for the other.
Finally, using a collar strategy takes the
return
from the probable to the definite. That is, when an investor owns a stock (or another underlying asset) and has an
expected return
, that expected return is only the
mean
of the distribution of possible returns, weighted by their probability. The investor may get a higher or lower return. When an investor who owns a stock (or other underlying asset) uses a collar strategy, the investor knows that the return can be no higher than the return defined by strike price on the call, and no lower than the return that results from the strike price of the put.
Symmetric Collar
[
edit
]
A symmetric collar is one where the initial value of each leg is equal. The product has therefore no cost to enter.
Structured collar
[
edit
]
A
structured collar
describes an
interest rate
derivative
product consisting of a straightforward
cap
, and an enhanced
floor
. The enhancement consists of additions which increase the cost of the floor should it be breached, or other adjustments designed to increase its cost.
It can be contrasted with a symmetric collar, where the value of the cap and floor are equal. It attracted criticism as part of the Financial Conduct Authorities' review of mis-sold bank interest rate products.
[
6
]
References
[
edit
]
Hull, John
(2005).
Fundamentals of Futures and Options Markets
, 5th ed. Upper Saddle River, NJ: Prentice Hall.
ISBN
0-13-144565-0
.
^
Ordu, Umut; Schweizer, Denis (2015-06-01). "Executive compensation and informed trading in acquiring firms around merger announcements".
Journal of Banking & Finance
. Global Governance and Financial Stability.
55
:
260–
280.
doi
:
10.1016/j.jbankfin.2015.02.013
.
^
"How a Protective Collar Works"
. Investopedia
. Retrieved
September 2,
2022
.
^
"Statement 133 Implementation Issue No. E18"
. Retrieved
July 8,
2011
.
^
HM Revenues and Customs.
"CFM13350 - Understanding corporate finance: derivative contracts: interest rate collars: Using interest rate collars"
. Retrieved
July 8,
2011
.
^
"Interest Rate Collars"
. Investopedia
. Retrieved
July 8,
2011
.
^
"Archived copy"
(PDF)
. Archived from
the original
(PDF)
on 2013-03-19
. Retrieved
2016-12-23
.
{{
cite web
}}
:  CS1 maint: archived copy as title (
link
)
Szado, Edward, and
Thomas Schneeweis
.
"Loosening Your Collar: Alternative Implementations of QQQ Collars"
. Isenberg School of Management, CISDM. University of Massachusetts, Amherst. (Original Version: August 2009. Current Update: September 2009).
v
t
e
Derivatives market
Derivative (finance)
*
List of futures exchanges
Options
Terms
Delta neutral
Exercise
Expiration
Moneyness
Open interest
Pin risk
Risk-free interest rate
Strike price
Synthetic position
the Greeks
Volatility
Vanillas
American
Bond option
Call
Employee stock option
European
Fixed income
FX
Option styles
Put
Warrants
Exotics
Asian
Barrier
Basket
Binary
Callable bull/bear contract
Chooser
Cliquet
Compound
Forward start
Interest rate
Lookback
Mountain range
Rainbow
Spread
Swaption
Strategies
Backspread
Box spread
Butterfly
Calendar spread
Collar
Condor
Covered option
Credit spread
Debit spread
Diagonal spread
Fence
Intermarket spread
Iron butterfly
Iron condor
Jelly roll
Ladder
Naked option
Straddle
Strangle
Protective option
Ratio spread
Risk reversal
Vertical spread
(
Bear
,
Bull
)
Valuation
Valuation methods
Continuous-time stochastic processes:
• Arithmetic diffusion:
Bachelier
• Geometric diffusion:
Black
,
Black–Scholes
,
Garman–Kohlhagen
,
Margrabe
• Stochastic volatility:
Heston
• Jump processes:
Jump diffusion
Discrete-time processes:
•
Binomial
,
Trinomial
,
Lattices
Numerical methods:
•
Finite difference
,
MC Simulation
,
Real options
Model-free:•
Put–call parity
,
Vanna–Volga
Swaps
Amortising
Asset
Basis
Commodity
Conditional variance
Constant maturity
Correlation
Credit default
Currency
Dividend
Equity
Forex
Forward Rate Agreement
Inflation
Interest rate
Overnight indexed
Total return
Variance
Volatility
Year-on-year inflation-indexed
Zero Coupon
Zero-coupon inflation-indexed
Forwards
Futures
Contango
Spot contract
Normal backwardation
Commodities future
Currency future
Dividend future
Forward market
Forward price
Forwards pricing
Forward rate
Futures pricing
Interest rate future
Margin
Perpetual futures
Single-stock futures
Slippage
Stock market index future
Exotic derivatives
Energy derivative
Freight derivative
Inflation derivative
Property derivative
Weather derivative
Other derivatives
Collateralized debt obligation (CDO)
Constant proportion portfolio insurance
Contract for difference
Credit-linked note (CLN)
Credit default option
Credit derivative
Equity-linked note (ELN)
Equity derivative
Foreign exchange derivative
Fund derivative
Fund of funds
Interest rate derivative
Mortgage-backed security
Power reverse dual-currency note (PRDC)
Market issues
Consumer debt
Corporate debt
Government debt
Great Recession
Municipal debt
Tax policy
Business portal
Retrieved from "
https://en.wikipedia.org/w/index.php?title=Collar_(finance)&oldid=1275957400
"
Categories
:
Options (finance)
Investment
Financial risk management
Hidden categories:
CS1 maint: archived copy as title
Articles with short description
Short description matches Wikidata
Wikipedia articles needing clarification from December 2013
All Wikipedia articles needing clarification
Wikipedia articles with style issues from December 2009
All articles with style issues
Articles needing additional references from December 2009
All articles needing additional references
Articles with multiple maintenance issues
Search
Search
Collar (finance)
7 languages
Add topic