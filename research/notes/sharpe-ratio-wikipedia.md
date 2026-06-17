---
title: Sharpe ratio - Wikipedia
id: sharpe-ratio-wikipedia
tags:
- sharpe-ratio statistics
created: '2026-06-17T20:06:32.059751Z'
updated: '2026-06-17T20:28:22.944310Z'
source: https://en.wikipedia.org/wiki/Sharpe_ratio
source_domain: en.wikipedia.org
fetched_at: '2026-06-17T20:06:32.059560Z'
fetch_provider: builtin
status: review
type: note
tier: institutional
content_type: article
deprecated: false
summary: From Wikipedia, the free encyclopedia
---

Sharpe ratio - Wikipedia
Jump to content
From Wikipedia, the free encyclopedia
Formula for measuring financial risk
In finance, the
Sharpe ratio
(also known as the
Sharpe index
, the
Sharpe measure
, and the
reward-to-variability ratio
) measures the performance of an investment such as a
security
or
portfolio
compared to a
risk-free asset
, after adjusting for its
risk
. It is defined as the difference between the returns of the investment and the
risk-free return
, divided by the
standard deviation
of the investment returns. It represents the additional amount of return that an investor receives per unit of increase in risk.
It was named after
William F. Sharpe
,
[
1
]
who developed it in 1966.
Definition
[
edit
]
Since its revision by the original author, William Sharpe, in 1994,
[
2
]
the
ex-ante
Sharpe ratio is defined as:
S
a
=
E
[
R
a
−
R
b
]
σ
a
=
E
[
R
a
−
R
b
]
v
a
r
[
R
a
−
R
b
]
,
{\displaystyle S_{a}={\frac {E[R_{a}-R_{b}]}{\sigma _{a}}}={\frac {E[R_{a}-R_{b}]}{\sqrt {\mathrm {var} [R_{a}-R_{b}]}}},}
where
R
a
{\displaystyle R_{a}}
is the asset return,
R
b
{\displaystyle R_{b}}
is the
risk-free return
(such as a
U.S. Treasury security
).
E
[
R
a
−
R
b
]
{\displaystyle E[R_{a}-R_{b}]}
is the
expected value
of the excess of the asset return over the benchmark return, and
σ
a
{\displaystyle {\sigma _{a}}}
is the
standard deviation
of the asset excess return. The
t-statistic
will equal the Sharpe Ratio times the square root of T (the number of returns used for the calculation).
The
ex-post
Sharpe ratio uses the same equation as the one above but with realized returns of the asset and benchmark rather than expected returns; see the second example below.
The
information ratio
is a generalization of the Sharpe ratio that uses as benchmark some other, typically risky index rather than using risk-free returns.
Use in finance
[
edit
]
The Sharpe ratio seeks to characterize how well the return of an asset compensates the investor for the risk taken.  When comparing two assets, the one with a higher Sharpe ratio appears to provide better return for the same risk, which is usually attractive to investors.
[
3
]
However, financial assets are often
not normally distributed
, so that standard deviation does not capture all aspects of risk.
Ponzi schemes
, for example, will have a high empirical Sharpe ratio until they fail. Similarly, a fund that sells low-strike
put options
will have a high empirical Sharpe ratio until one of those puts is exercised, creating a large loss. In both cases, the empirical standard deviation before failure gives no real indication of the size of the risk being run.
[
4
]
Even in less extreme cases, a reliable empirical estimate of Sharpe ratio still requires the collection of return data over sufficient period for all aspects of the strategy returns to be observed. For example, data must be taken over decades if the algorithm sells an insurance that involves a high liability payout once every 5–10 years, and a
high-frequency trading
algorithm may only require a week of data if each trade occurs every 50 milliseconds, with care taken toward risk from unexpected but rare results that such testing did not capture (see
flash crash
).
Additionally, when examining the investment performance of assets with smoothing of returns (such as
with-profits
funds), the Sharpe ratio should be derived from the performance of the underlying assets rather than the fund returns (Such a model would invalidate the aforementioned Ponzi scheme, as desired).
Sharpe ratios, along with
Treynor ratios
and
Jensen's alphas
, are often used to rank the performance of portfolio or
mutual fund
managers.
Berkshire Hathaway
had a Sharpe ratio of 0.79 for the period 1976 to 2017, higher than any other stock or mutual fund with a history of more than 30 years. The U.S. stock market had a Sharpe ratio of 0.49 for the same period.
[
5
]
Tests
[
edit
]
Several statistical tests of the Sharpe ratio have been proposed. These include those proposed by Jobson & Korkie
[
6
]
and Gibbons, Ross & Shanken.
[
7
]
History
[
edit
]
In 1952, Andrew D. Roy suggested maximizing the ratio
(
m
−
d
) ÷
σ
, where
m
is expected gross return,
d
is some "disaster level" (a.k.a., minimum acceptable return, or MAR) and
σ
is standard deviation of returns.
[
8
]
This ratio is just the Sharpe ratio, only using minimum acceptable return instead of the risk-free rate in the numerator, and using standard deviation of returns instead of standard deviation of excess returns in the denominator. Roy's ratio is also related to the
Sortino ratio
, which also uses MAR in the numerator, but uses a different standard deviation (semi/downside deviation) in the denominator.
In 1966,
William F. Sharpe
developed what is now known as the Sharpe ratio.
[
1
]
Sharpe originally called it the "reward-to-variability" ratio before it began being called the Sharpe ratio by later academics and financial operators. The definition was:
S
=
E
[
R
−
R
f
]
v
a
r
[
R
]
.
{\displaystyle S={\frac {E[R-R_{f}]}{\sqrt {\mathrm {var} [R]}}}.}
Sharpe's 1994 revision acknowledged that the basis of comparison should be an applicable benchmark, which changes with time. After this revision, the definition is:
S
=
E
[
R
−
R
b
]
v
a
r
[
R
−
R
b
]
.
{\displaystyle S={\frac {E[R-R_{b}]}{\sqrt {\mathrm {var} [R-R_{b}]}}}.}
Note, if
R
f
{\displaystyle R_{f}}
is a constant risk-free return throughout the period,
v
a
r
[
R
−
R
f
]
=
v
a
r
[
R
]
.
{\displaystyle {\sqrt {\mathrm {var} [R-R_{f}]}}={\sqrt {\mathrm {var} [R]}}.}
The (original) Sharpe ratio has often been challenged with regard to its appropriateness as a fund performance measure during periods of declining markets.
[
9
]
Examples
[
edit
]
Example 1
Suppose the asset has an expected return of 15% in excess of the risk free rate. We typically do not know if the asset will have this return. We estimate the risk of the asset, defined as standard deviation of the asset's
excess return
, as 10%. The risk-free return is constant. Then the Sharpe ratio using the old definition is
R
a
−
R
f
σ
a
=
0.15
0.10
=
1.5
{\displaystyle {\frac {R_{a}-R_{f}}{\sigma _{a}}}={\frac {0.15}{0.10}}=1.5}
.
Example 2
An investor has a portfolio with an expected return of 12% and a standard deviation of 10%. The rate of interest is 5%, and is risk-free.
The Sharpe ratio is:
0.12
−
0.05
0.1
=
0.7
{\displaystyle {\frac {0.12-0.05}{0.1}}=0.7}
.
Strengths and weaknesses
[
edit
]
A negative Sharpe ratio means the portfolio has underperformed its benchmark. All other things being equal, an investor typically prefers a higher positive Sharpe ratio as it has either higher returns or lower
volatility
. However, a negative Sharpe ratio can be made higher by either increasing returns (a good thing) or increasing volatility (a bad thing). Thus, for negative values the Sharpe ratio does not correspond well to typical investor
utility functions
.
The Sharpe ratio is convenient because it can be calculated purely from any observed series of returns without need for additional information surrounding the source of profitability. However, this makes it vulnerable to manipulation if opportunities exist for smoothing or discretionary pricing of illiquid assets. Statistics such as the
bias ratio
and
first order autocorrelation
are sometimes used to indicate the potential presence of these problems.
While the
Treynor ratio
considers only the
systematic risk
of a portfolio, the Sharpe ratio considers both systematic and
idiosyncratic risks
. Which one is more relevant will depend on the portfolio context.
The returns measured can be of any frequency (i.e. daily, weekly, monthly or annually), as long as they are
normally distributed
, as the returns can always be annualized. Herein lies the underlying weakness of the ratio – asset returns are not normally distributed. Abnormalities like
kurtosis
,
fatter tails
and higher peaks, or
skewness
on the
distribution
can be problematic for the ratio, as standard deviation does not have the same effectiveness when these problems exist.
[
10
]
For Brownian motion with i.i.d. increments, Sharpe ratio
μ
/
σ
{\displaystyle \mu /\sigma }
is a dimensional quantity with units
1
/
T
{\displaystyle 1/{\sqrt {T}}}
(where
T
{\displaystyle T}
is the horizon length), because over horizon
T
{\displaystyle T}
the expected excess return is
μ
T
{\displaystyle \mu T}
while the standard deviation is
σ
T
{\displaystyle \sigma {\sqrt {T}}}
; consequently the horizon-
T
{\displaystyle T}
Sharpe ratio is
S
T
=
(
μ
/
σ
)
T
{\displaystyle S_{T}=(\mu /\sigma ){\sqrt {T}}}
.
[
11
]
Kelly criterion
is a dimensionless quantity, and, indeed, Kelly fraction
μ
/
σ
2
{\displaystyle \mu /\sigma ^{2}}
is the numerical fraction of wealth suggested for the investment.
In some settings, the
Kelly criterion
can be used to convert the Sharpe ratio into a rate of return. The Kelly criterion gives the ideal size of the investment, which when adjusted by the period and expected rate of return per unit, gives a rate of return.
[
12
]
The accuracy of Sharpe ratio estimators hinges on the statistical properties of returns, and these properties can vary considerably among strategies, portfolios, and over time.
[
11
]
Drawback as fund selection criteria
[
edit
]
Bailey and López de Prado (2012)
[
13
]
show that Sharpe ratios tend to be overstated in the case of hedge funds with short track records. These authors propose a
deflated Sharpe ratio
that takes into account the asymmetry and fat-tails of the returns' distribution, sample length, and selection bias. With regards to the selection of portfolio managers on the basis of their Sharpe ratios, these authors have proposed a
Sharpe ratio indifference curve
.
[
14
]
This curve illustrates the fact that it is efficient to hire portfolio managers with low and even negative Sharpe ratios, as long as their correlation to the other portfolio managers is sufficiently low.
Goetzmann, Ingersoll, Spiegel, and Welch (2002) determined that the best strategy to maximize a portfolio's Sharpe ratio, when both securities and options contracts on these securities are available for investment, is a portfolio of selling one
out-of-the-money
call and selling one out-of-the-money put. This portfolio generates an immediate positive payoff, has a large probability of generating modestly high returns, and has a small probability of generating huge losses. Shah (2014) observed that such a portfolio is not suitable for many investors, but fund sponsors who select fund managers primarily based on the Sharpe ratio will give incentives for fund managers to adopt such a strategy.
[
15
]
In recent years, many financial websites have promoted the idea that a Sharpe Ratio "greater than 1 is considered acceptable; a ratio higher than 2.0 is considered very good; and a ratio above 3.0 is excellent."  While it is unclear where this rubric originated online, it makes little sense since the magnitude of the Sharpe ratio is sensitive to the time period over which the underlying returns are measured.  This is because the nominator of the ratio (returns) scales in proportion to time; while the denominator of the ratio (standard deviation) scales in proportion to the square root of time.  Most diversified indexes of equities, bonds, mortgages or commodities have annualized Sharpe ratios below 1, which suggests that a Sharpe ratio consistently above 2.0 or 3.0 is unrealistic.
[
citation needed
]
See also
[
edit
]
Calmar ratio
Capital asset pricing model
Coefficient of variation
Hansen–Jagannathan bound
List of financial performance measures
Modern portfolio theory
Omega ratio
Risk adjusted return on capital
Roy's safety-first criterion
Signal-to-noise ratio
Sterling ratio
Upside potential ratio
V2 ratio
Z score
References
[
edit
]
^
a
b
Sharpe, W. F. (1966). "Mutual Fund Performance".
Journal of Business
.
39
(S1):
119–
138.
doi
:
10.1086/294846
.
^
Sharpe, William F. (1994).
"The Sharpe Ratio"
.
The Journal of Portfolio Management
.
21
(1):
49–
58.
doi
:
10.3905/jpm.1994.409501
.
S2CID
55394403
. Retrieved
12 June
2012
.
^
Gatfaoui, Hayette. "Sharpe Ratios and Their Fundamental Components: An Empirical Study".
IESEG School of Management
.
^
Agarwal, Vikas; Naik, Narayan Y. (2004).
"Risks and Portfolio Decisions Involving Hedge Funds"
.
The Review of Financial Studies
.
17
(1):
63–
98.
doi
:
10.1093/rfs/hhg044
.
ISSN
0893-9454
.
JSTOR
1262669
.
^
Frazzini, Andrea; Kabiller, David; Pedersen, Lasse Heje (1 September 2018).
"Buffett's Alpha"
.
Financial Analysts Journal
.
doi
:
10.2469/faj.v74.n4.3
.
hdl
:
10398/5c1cd30d-a404-44ae-9578-7710cec23ea4
.
ISSN
0015-198X
.
^
Jobson JD; Korkie B (September 1981). "Performance hypothesis testing with the Sharpe and Treynor measures".
The Journal of Finance
.
36
(4):
888–
908.
doi
:
10.1111/j.1540-6261.1981.tb04891.x
.
JSTOR
2327554
.
^
Gibbons M; Ross S; Shanken J (September 1989). "A test of the efficiency of a given portfolio".
Econometrica
.
57
(5):
1121–
1152.
CiteSeerX
10.1.1.557.1995
.
doi
:
10.2307/1913625
.
JSTOR
1913625
.
^
Roy, Arthur D. (July 1952). "Safety First and the Holding of Assets".
Econometrica
.
20
(3):
431–
450.
doi
:
10.2307/1907413
.
JSTOR
1907413
.
^
Scholz, Hendrik (2007). "Refinements to the Sharpe ratio: Comparing alternatives for bear markets".
Journal of Asset Management
.
7
(5):
347–
357.
doi
:
10.1057/palgrave.jam.2250040
.
S2CID
154908707
.
^
"Understanding The Sharpe Ratio"
. Retrieved
14 March
2011
.
^
a
b
Lo, Andrew W. (July–August 2002). "The Statistics of Sharpe Ratios".
Financial Analysts Journal
.
58
(4).
^
Wilmott, Paul (2007).
Paul Wilmott introduces Quantitative Finance
(Second ed.). Wiley. pp.
429
–432.
ISBN
978-0-470-31958-1
.
^
Bailey, D. and M. López de Prado (2012): "The Sharpe Ratio Efficient Frontier", Journal of Risk, 15(2), pp.3–44. Available at
https://ssrn.com/abstract=1821643
^
Bailey, D. and M. Lopez de Prado (2013): "The Strategy Approval Decision: A Sharpe Ratio Indifference Curve approach", Algorithmic Finance 2(1), pp. 99–109 Available at
https://ssrn.com/abstract=2003638
^
Shah, Sunit N. (2014),
The Principal-Agent Problem in Finance
, CFA Institute, p. 14
Further reading
[
edit
]
Lo, Andrew W. "The statistics of Sharpe ratios." Financial analysts journal 58.4 (2002): 36–52
https://doi.org/10.2469/faj.v58.n4.2453
Bacon
Practical Portfolio Performance Measurement and Attribution 2nd Ed
: Wiley, 2008.
ISBN
978-0-470-05928-9
Bruce J. Feibel.
Investment Performance Measurement
. New York: Wiley, 2003.
ISBN
0-471-26849-6
Steven E. Pav.
The Sharpe Ratio: Statistics and Applications
. CRC Press, 2022.
ISBN
978-1-032-01930-7
Goetzmann, William; Ingersoll, Jonathan; Spiegel, Matthew; Welch, Ivo (2002),
Sharpening Sharpe Ratios
(PDF)
, National Bureau of Economic Research
.
Shah, Sunit N. (2014),
The Principal-Agent Problem in Finance
, CFA Institute
External links
[
edit
]
The Sharpe ratio
Generalized Sharpe Ratio
All Hail the Sharpe Ratio
– Uses and abuses of the Sharpe Ratio
"A Comparison of Different Measures of Risk-adjusted Return"
. September 2013.
What is a good Sharpe Ratio?
– Some example calculations of Sharpe ratios
Sharpe ratio in MS excel
– Risk adjusted return calculations
Calculating and Interpreting Sharpe Ratios online
– Cloud calculator
v
t
e
Financial risk
and
financial risk management
Categories
Credit risk
Consumer credit risk
Sovereign credit risk
Settlement risk
Default risk
Concentration risk
Credit derivative
Securitization
Market risk
Commodity risk
(e.g.
Volume risk
,
Basis risk
,
Shape risk
,
Holding period risk
,
Price area risk
)
Equity risk
Valuation risk
FX risk
Margining risk
Interest rate risk
Inflation risk
Volatility risk
Liquidity risk
(e.g.
Refinancing risk
,
Deposit risk
)
Operational risk
Operational risk management
Business risks
Model risk
Reputational risk
Country risk
Political risk
Legal risk
Supply chain risk
Other
Execution risk
Profit risk
Systemic risk
Non-financial risk
Modeling
Arbitrage pricing theory
Black–Scholes model
Replicating portfolio
Cash flow matching
Conditional Value-at-Risk (CVaR)
Copula
Drawdown
First-hitting-time model
Interest rate immunization
Market portfolio
Modern portfolio theory
Omega ratio
RAROC
Risk-free rate
Risk parity
Sharpe ratio
Sortino ratio
Survival analysis
(
Proportional hazards model
)
Tracking error
Value-at-Risk (VaR)
and extensions (
Profit at risk
,
Margin at risk
,
Liquidity at risk
,
Cash flow at risk
,
Earnings at risk
)
Basic concepts
Asset allocation
Asset and liability management
Asset pricing
Bad debt
Capital asset
Capital structure
Corporate finance
Cost of capital
Diversification
Economic bubble
Enterprise value
ESG
Exchange-traded fund
Expected return
Financial
adviser
analysis
analyst
asset
betting
crime
engineering
law
risk
social work
Fundamental analysis
Growth investing
Hazard
Hedge
Investment management
Risk
Risk pool
Risk of ruin
Systematic risk
Mathematical finance
Moral hazard
Risk–return spectrum
Speculation
Speculative attack
Statistical finance
Strategic financial management
Stress test (financial)
Structured finance
Structured product
Systemic risk
Toxic asset
Financial economics
Investment management
Mathematical finance
v
t
e
Financial ratios
Buffett indicator
Cyclically adjusted price-to-earnings
(CAPE)
Capitalization rate
(Cap Rate)
Cash return on capital invested
(CROCI)
Debt-to-equity
(D/E)
Dividend cover
Dividend payout
Earnings yield
(E/P)
Enterprise value/EBITDA
(EV/EBITDA)
Enterprise value/gross cash invested
(EV/GCI)
Enterprise value/sales
(EV/Sales)
Loan-to-value
(LTV)
Omega
Operating margin
Price-to-book
(P/B)
Present value of growth opportunities
(PVGO)
Price/cash flow
(P/CF)
Price-earnings
(P/E)
Price-earnings to growth
(PEG)
Price-sales
(P/S)
Profit margin
Return on assets
(ROA)
Return on net assets
(RONA)
Return on capital
(ROC)
Return on capital employed
(ROCE)
Return on equity
(ROE)
Return on tangible equity
(ROTE)
Risk-adjusted return on capital
(RAROC)
Risk return
(RRR)
Sharpe
Short interest
(SIR)
Sortino
Sustainable growth
(SGR)
Treynor
Retrieved from "
https://en.wikipedia.org/w/index.php?title=Sharpe_ratio&oldid=1359875983
"
Categories
:
Financial ratios
Statistical ratios
Portfolio theories
Yield (finance)
Hidden categories:
Articles with short description
Short description is different from Wikidata
Use dmy dates from December 2024
All articles with unsourced statements
Articles with unsourced statements from July 2025
Search
Search
Sharpe ratio
20 languages
Add topic