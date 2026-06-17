---
title: 'Investopedia: Average True Range ATR'
id: investopedia-average-true-range-atr
tags:
- atr-stop
created: '2026-06-17T15:12:59.881338Z'
updated: '2026-06-17T20:28:23.286110Z'
source: https://www.investopedia.com/terms/a/atr.asp
source_domain: www.investopedia.com
fetched_at: '2026-06-17T15:12:59.881102Z'
fetch_provider: builtin
status: evergreen
type: note
tier: unknown
content_type: unknown
deprecated: false
summary: 'Investopedia ATR primer: Wilder''s 14-day Average True Range measures volatility;
  basis for volatility-adaptive (Chandelier 3xATR) stops that scale to each instrument''s
  normal range.'
---

Average True Range (ATR) Formula, What It Means, and How to Use It
​
Top Stories
States Without State Taxes on Retirement Income
Is the Stock Market Open on Juneteenth?
How the Working Americans' Tax Cut Act Could Erase Taxes for the Middle Class
How Many Americans Wait for Full Retirement Age to Claim Social Security?
Table of Contents
Expand
Table of Contents
What Is the Average True Range (ATR)?
The ATR Formula
Calculating the ATR
What Does the ATR Tell You?
How to Use the ATR
Limitations
FAQs
The Bottom Line
Investopedia / Jiaqi Zhou
Close
Definition
Average true range is a technical analysis indicator that demonstrates market volatility.
Key Takeaways
The ATR is typically derived from the 14-day simple moving average of a series of true range indicators.
The ATR was initially developed for use in commodities markets but has since been applied to all types of securities.
The ATR shows investors the average range of prices for an investment over a specified period.
Consider our list of the
best online brokers
if you'd like to put this information to use.
Get personalized, AI-powered answers built on 27+ years of trusted expertise.
ASK
What Is the Average True Range (ATR)?
The average true range (ATR) is a technical analysis indicator introduced by market technician J. Welles Wilder Jr. in his book "New Concepts in Technical Trading Systems." It measures market volatility by decomposing the entire range of an asset price for that period.
The true range indicator is taken as the greatest of the following:
The current high less the current low
The absolute value of the current high less the previous close and
The absolute value of the current low less the previous close
The ATR is then a moving average of the true ranges, generally using 14 days.
Traders can use shorter periods than 14 days to generate more trading signals. Longer periods have a higher probability of generating fewer trading signals.
The Average True Range (ATR) Formula
The formula to calculate ATR for an investment with a previous ATR calculation is:
Previous ATR
(
n
−
1
)
+
TR
n
where:
n
=
Number of periods
TR
=
True range
\begin{aligned}&\frac{ \text{Previous ATR} ( n - 1 ) + \text{TR} }{ n } \\&\textbf{where:} \\&n = \text{Number of periods} \\&\text{TR} = \text{True range} \\\end{aligned}
​
n
Previous ATR
(
n
−
1
)
+
TR
​
where:
n
=
Number of periods
TR
=
True range
​
If there is not a previous ATR calculated, you must use:
(
1
n
)
∑
i
n
TR
i
where:
TR
i
=
Particular true range, such as first day’s TR,
then second, then third
n
=
Number of periods
\begin{aligned}&\Big ( \frac{ 1 }{ n } \Big ) \sum_{i}^{n} \text{TR}_i \\&\textbf{where:} \\&\text{TR}_i = \text{Particular true range, such as first day's TR,} \\&\text{then second, then third} \\&n = \text{Number of periods} \\\end{aligned}
​
(
n
1
​
)
i
∑
n
​
TR
i
​
where:
TR
i
​
=
Particular true range, such as first day’s TR,
then second, then third
n
=
Number of periods
​
Fast Fact
The capital sigma symbol (Σ) represents the summation of all the terms for
n
periods starting at
i
, or the period specified. If there is no number following
i
, it is assumed that the starting point is the first period. You may see
i=1
, noting to start summing at the first term.
You must first use the following formula to calculate the true range:
TR
=
Max
[
(
H
−
L
)
,
∣
H
−
C
p
∣
,
∣
L
−
C
p
∣
]
where:
H
=
Today’s high
L
=
Today’s low
C
p
=
Yesterday’s closing price
Max
=
Highest value of the three terms
so
that:
(
H
−
L
)
=
Today’s high minus the low
∣
H
−
C
p
∣
=
Absolute value of today’s high minus
yesterday’s closing price
∣
L
−
C
p
∣
=
Absolute value of today’s low minus
yesterday’s closing price
\begin{aligned}&\text{ TR } = \text{ Max } [ ( \text{H} - \text{L} ), | \text{H} - \text{C}_p |, | \text{L} - \text{C}_p | ] \\&\textbf{where:} \\&\text{H} = \text{Today's high} \\&\text{L} = \text{Today's low} \\&\text{C}_p = \text{Yesterday's closing price} \\&\text{Max} = \text{Highest value of the three terms} \\&\textbf{so that:} \\&( \text{H} - \text{L} ) = \text{Today's high minus the low} \\&| \text{H} - \text{C}_p | = \text{Absolute value of today's high minus} \\&\text{yesterday's closing price} \\&| \text{L} - \text{C}_p | = \text{Absolute value of today's low minus} \\&\text{yesterday's closing price} \\\end{aligned}
​
TR
=
Max
[(
H
−
L
)
,
∣
H
−
C
p
​
∣
,
∣
L
−
C
p
​
∣
]
where:
H
=
Today’s high
L
=
Today’s low
C
p
​
=
Yesterday’s closing price
Max
=
Highest value of the three terms
so that:
(
H
−
L
)
=
Today’s high minus the low
∣
H
−
C
p
​
∣
=
Absolute value of today’s high minus
yesterday’s closing price
∣
L
−
C
p
​
∣
=
Absolute value of today’s low minus
yesterday’s closing price
​
How to Calculate the ATR
The first step in calculating ATR is to find a series of true range values for a security. The price range of an asset for a given trading day is its high minus its low. To find an asset's true range value, you first determine the three terms from the formula.
Suppose that XYZ's stock had a trading high today of $21.95 and a low of $20.22. It closed yesterday at $21.51. Using the three terms, we use the highest result:
(
H
−
L
)
=
$
21.95
−
$
20.22
=
$
1.73
( \text{H} - \text{L}) = \$21.95 - \$20.22 = \$1.73
(
H
−
L
)
=
$21.95
−
$20.22
=
$1.73
∣
(
H
−
C
p
)
∣
=
∣
$
21.95
−
$
21.51
∣
=
$
0.44
| ( \text{H} - \text{C}_p ) | = | \$21.95 - \$21.51 | = \$0.44
∣
(
H
−
C
p
​
)
∣
=
∣$21.95
−
$21.51∣
=
$0.44
∣
(
L
−
C
p
)
∣
=
∣
$
20.22
−
$
21.51
∣
=
$
1.29
| ( \text{L} - \text{C}_p ) | = | \$20.22 - \$21.51 | = \$1.29
∣
(
L
−
C
p
​
)
∣
=
∣$20.22
−
$21.51∣
=
$1.29
The number you'd use would be $1.73 because it is the highest value.
Because you don't have a previous ATR, you must use the ATR formula:
(
1
n
)
∑
i
n
TR
i
\begin{aligned}\Big ( \frac{ 1 }{ n } \Big ) \sum_{i}^{n} \text{TR}_i\end{aligned}
(
n
1
​
)
i
∑
n
​
TR
i
​
​
Using 14 days as the number of periods, you'd calculate the TR for each of the 14 days. Assume the following prices from the table.
Daily Values
High
Low
Yesterday's Close
Day 1
$ 21.95
$ 20.22
$ 21.51
Day 2
$ 22.25
$ 21.10
$ 21.61
Day 3
$ 21.50
$ 20.34
$ 20.83
Day 4
$ 23.25
$ 22.13
$ 22.65
Day 5
$ 23.03
$ 21.87
$ 22.41
Day 6
$ 23.34
$ 22.18
$ 22.67
Day 7
$ 23.66
$ 22.57
$ 23.05
Day 8
$ 23.97
$ 22.80
$ 23.31
Day 9
$ 24.29
$ 23.15
$ 23.68
Day 10
$ 24.60
$ 23.45
$ 23.97
Day 11
$ 24.92
$ 23.76
$ 24.31
Day 12
$ 25.23
$ 24.09
$ 24.60
Day 13
$ 25.55
$ 24.39
$ 24.89
Day 14
$ 25.86
$ 24.69
$ 25.20
You'd use these prices to calculate the TR for each day.
Trading Range
H-L
H-C
p
L-C
p
Day 1
$ 1.73
$ 0.44
$ (1.29)
Day 2
$ 1.15
$ 0.64
$ (0.51)
Day 3
$ 1.16
$ 0.67
$ (0.49)
Day 4
$ 1.12
$ 0.60
$ (0.52)
Day 5
$ 1.15
$ 0.61
$ (0.54)
Day 6
$ 1.16
$ 0.67
$ (0.49)
Day 7
$ 1.09
$ 0.61
$ (0.48)
Day 8
$ 1.17
$ 0.66
$ (0.51)
Day 9
$ 1.14
$ 0.61
$ (0.53)
Day 10
$ 1.15
$ 0.63
$ (0.52)
Day 11
$ 1.16
$ 0.61
$ (0.55)
Day 12
$ 1.14
$ 0.63
$ (0.51)
Day 13
$ 1.16
$ 0.66
$ (0.50)
Day 14
$ 1.17
$ 0.66
$ (0.51)
You find that the highest values for each day are from the (H - L) column, so you'd add up all the results from the (H - L) column and multiply the result by 1/n, per the formula.
$
1.73
+
$
1.15
+
$
1.16
+
$
1.12
+
$
1.15
+
$
1.16
+
$
1.09
+
$
1.17
+
$
1.14
+
$
1.15
+
$
1.16
+
$
1.14
+
$
1.16
+
$
1.17
=
$
16.65
\begin{aligned}\$1.73 &+ \$1.15 + \$1.16 + \$1.12 + \$1.15 + \$1.16 + \$1.09 \\&+ \$1.17 + \$1.14 + \$1.15 + \$1.16 + \$1.14 + \$1.16 \\&+ \$1.17 = \$16.65 \\\end{aligned}
$1.73
​
+
$1.15
+
$1.16
+
$1.12
+
$1.15
+
$1.16
+
$1.09
+
$1.17
+
$1.14
+
$1.15
+
$1.16
+
$1.14
+
$1.16
+
$1.17
=
$16.65
​
1
n
(
$
16.65
)
=
1
14
(
$
16.65
)
\begin{aligned}\frac{ 1 }{ n } (\$16.65) = \frac{ 1 }{ 14 } (\$16.65)\end{aligned}
n
1
​
(
$16.65
)
=
14
1
​
(
$16.65
)
​
0.714
×
$
16.65
=
$
1.18
\begin{aligned}0.714 \times \$16.65 = \$1.18\end{aligned}
0.714
×
$16.65
=
$1.18
​
The average volatility for this asset is therefore $1.18.
Now that you have the ATR for the previous period, you can use it to determine the ATR for the current period using the following formula:
Previous ATR
(
n
−
1
)
+
TR
n
\begin{aligned}\frac{ \text{Previous ATR} ( n - 1 ) + \text{TR} }{ n }\end{aligned}
n
Previous ATR
(
n
−
1
)
+
TR
​
​
This formula is much simpler because you only have to calculate the TR for one day. Assuming on Day 15, the asset has a high of $25.55, a low of $24.37, and closed the previous day at $24.87; its TR works out to $1.18:
$
1.18
(
14
−
1
)
+
$
1.18
14
\begin{aligned}\frac{ \$1.18 ( 14 - 1 ) + \$1.18 }{ 14 }\end{aligned}
14
$1.18
(
14
−
1
)
+
$1.18
​
​
$
1.18
(
13
)
+
$
1.18
14
\begin{aligned}\frac{ \$1.18 ( 13 ) + \$1.18 }{ 14 }\end{aligned}
14
$1.18
(
13
)
+
$1.18
​
​
$
15.34
+
$
1.18
14
\begin{aligned}\frac{ \$15.34 + \$1.18 }{ 14 }\end{aligned}
14
$15.34
+
$1.18
​
​
$
16.52
14
=
$
1.18
\begin{aligned}\frac{ \$16.52 }{ 14 } = \$1.18\end{aligned}
14
$16.52
​
=
$1.18
​
The stock closed the day again with an average volatility (ATR) of $1.18.
Example Calculation of the ATR.
Image by Sabrina Jiang © Investopedia 2020
What Does the ATR Tell You?
Wilder originally developed the ATR for
commodities
, although the indicator can also be used for stocks and indices as well.
A stock experiencing a high level of volatility has a higher ATR, and a lower ATR indicates lower volatility for the period evaluated.
The ATR may be used by market technicians to enter and exit trades and is a useful tool to add to a trading system. It was created to allow traders to more accurately measure the daily volatility of an asset by using simple calculations. The indicator does not indicate the price direction. It is primarily used to measure volatility caused by gaps and limit up or down moves. The ATR is relatively simple to calculate and only needs historical price data.
The ATR is commonly used as an exit method that can be applied regardless of how the entry decision is made. One popular technique is known as the "chandelier exit," developed by Chuck LeBeau. The chandelier exit places a
trailing stop
under the highest high the stock has reached since you entered the trade. The distance between the highest high and the stop level is defined as some multiple multiplied by the ATR.
The Chandelier Exit.
Image by Sabrina Jiang © Investopedia 2020
The ATR can also give a trader an indication of what size trade to use in the
derivatives
markets. It is possible to use the ATR approach to position sizing that will account for an individual trader's willingness to accept risk and the volatility of the underlying market.
Example of How to Use the ATR
Assume the first value of a five-day ATR is calculated at 1.41, and the sixth day has a true range of 1.09. The sequential ATR value could be estimated by multiplying the previous value of the ATR by the number of days less one and then adding the true range for the current period to the product.
Next, divide the sum by the selected timeframe. The second value of the ATR is estimated to be 1.35, or (1.41 * (5 - 1) + (1.09)) / 5. The formula could then be repeated over the entire period.
The ATR doesn't tell us in which direction the breakout will occur, but it can be added to the
closing price
, and the trader can buy whenever the next day's price trades above that value. This idea is shown below. Trading signals occur relatively infrequently, but they usually indicate significant breakout points. The logic behind these signals is that whenever a price closes more than an ATR above the most recent close, a change in volatility has occurred.
Example of Using the ATR.
Image by Sabrina Jiang © Investopedia 2020
Limitations of the ATR
There are two main limitations to using the ATR indicator. The first is that ATR is a subjective measure. It's open to interpretation. No single ATR value will tell you with any certainty whether a trend is about to reverse. ATR readings should always be compared against earlier readings to get a feel of a trend's strength or weakness.
Second, ATR only measures volatility and not the direction of an asset's price.
This can sometimes result in mixed signals, particularly when markets are experiencing pivots or when trends are at turning points. A sudden increase in the ATR following a large move counter to the prevailing trend may lead some traders to think that the ATR is confirming the old trend. However, this may not be the case.
How Do You Use ATR in Trading?
Average true range is used to evaluate an investment's price volatility. It is used in conjunction with other indicators and tools to enter and exit trades or decide whether to purchase an asset.
How Do You Read ATR Values?
An average true range value is the average price range of an investment over a period. So, if the ATR for an asset is $1.18, its price has an average range of movement of $1.18 per trading day.
What Is a Good Average True Range?
A good ATR depends on the asset. If it generally has an ATR of close to $1.18, it is performing in a way that can be interpreted as normal. If the same asset suddenly has an ATR of more than $1.18, it might indicate that further investigation is required. If it has a much lower ATR, you should determine why it is happening before taking action.
The Bottom Line
The average true range is an indicator of the price volatility of an asset. It is best used to determine how much an investment's price has been moving in the period being evaluated, rather than as an indication of a trend. Calculating an investment's ATR is relatively straightforward, only requiring you to use price data for the period you're investigating.
Disclosure: This article is not intended to provide investment advice. Investing in securities entails varying degrees of risk and can result in partial or total loss of principal. The trading strategies discussed in this article are complex and should not be undertaken by novice investors. Readers seeking to engage in such trading strategies should seek extensive education on the topic.
Article Sources
Investopedia requires writers to use primary sources to support their work. These include white papers, government data, original reporting, and interviews with industry experts. We also reference original research from other reputable publishers where appropriate. You can learn more about the standards we follow in producing accurate, unbiased content in our
editorial policy.
Wilder, Jr., J. Welles. “
New Concepts in Technical Trading Systems
.”
Trend Research
, 1978.
TradingView. "
Average True Range (ATR)
."
Clenow, Andreas . “
Following the Trend: Diversified Managed Futures Trading, 2nd Edition
,”
John Wiley & Sons
, 2023, Pages 54–56, 76–79.
StockCharts. "
Chandelier Exit
."
Take the Next Step to Invest
Advertiser Disclosure
×
The offers that appear in this table are from partnerships from which Investopedia receives compensation. This compensation may impact how and where listings appear. Investopedia does not include all offers available in the marketplace.
Get personalized, AI-powered answers built on 27+ years of trusted expertise.
ASK
Read more
Trading
Technical Analysis
Technical Analysis Basic Education
Partner Links
Take the Next Step to Invest
Advertiser Disclosure
×
The offers that appear in this table are from partnerships from which Investopedia receives compensation. This compensation may impact how and where listings appear. Investopedia does not include all offers available in the marketplace.
Related Articles
Using Volume Rate of Change to Confirm Trends
Master Key Stock Chart Patterns: Spot Trends and Signals
RSI Indicator: Buy and Sell Signals
Spotting Fakeouts: Key Strategies in Technical Analysis
Mastering Range-Bound Trading: Strategy, Definition, and Techniques
Price by Volume (PBV) Charts: Definition, Uses, and Key Examples
Support and Resistance Basics
Trading Ranges Explained: Strategies and Key Occurrences
Mastering Price Movements with Volume Price Trend (VPT) Analysis
Chaikin Oscillator Explained: Formula, Usage & Market Trends
Understanding Resistance in Trading: Key Concepts and Strategies
How to Use the Chande Momentum Oscillator: A Comprehensive Guide
Understanding the Head and Shoulders Pattern in Technical Analysis
Technical Analysis Strategies for Gold Miner ETFs
Mastering the Hikkake Pattern: Predict Market Reversals
Impulse Wave Patterns Explained: Definition, Rules, and Trading Strategies
Newsletter Sign Up
Newsletter Sign Up
By clicking “Accept All Cookies”, you agree to the storing of cookies on your device to enhance site navigation, analyze site usage, and assist in our marketing efforts.
Cookies Settings
Accept All Cookies