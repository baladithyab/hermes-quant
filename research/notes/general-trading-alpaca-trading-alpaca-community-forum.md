---
title: General Trading - Alpaca Trading - Alpaca Community Forum
id: general-trading-alpaca-trading-alpaca-community-forum
tags:
- paper trading edge validation
created: '2026-06-17T20:13:34.436140Z'
updated: '2026-06-17T20:28:23.115587Z'
source: https://alpaca.markets/learn/paper-trading/
source_domain: forum.alpaca.markets
fetched_at: '2026-06-17T20:13:34.435963Z'
fetch_provider: builtin
status: review
type: note
tier: unknown
content_type: unknown
deprecated: false
summary: General Trading - Alpaca Trading - Alpaca Community Forum
---

General Trading - Alpaca Trading - Alpaca Community Forum
New to Alpaca? Create your Trading API account today!
Sign Up Today
General Trading
Alpaca Trading
Etienne_Brunet
August 13, 2020, 10:28am
1
Q - What is a pattern day trader (PDT)?
You will be considered a pattern day trader if you “day trade” 4 or more times within 5 business days and your day trading activities are greater than 6 percent of your total trading activity for that same 5 day period. A “day trade” is defined as buying then selling or selling short, then buying the same security on the same day. Just buying a security, without selling it later that same day, would not be considered a day trade.
Q - What is the minimum equity requirement for a pattern day trader?
If you are designated as a pattern day trader, a $25,000 minimum equity requirement must be deposited in the account prior to any day-trading activities and maintained in your account at all times. If the account falls below the $25,000 requirement, you will not be permitted to day trade until you deposit cash in the account to restore the account to the $25,000 minimum equity level.
Pattern Day Trader (PDT) in Alpaca Accounts
Currently (May 2020), all Alpaca Accounts are Margin accounts and that we currently do not support cash accounts. Further, if trading were to be done in a cash account, the rules require that s/he trade on settled cash only (effectively meaning that if s/he purchased a stock and sold it today, s/he could only use the cash from the sale after the original purchase had settled at T+2).
For more information about Pattern Day Trading, please visit:
http://www.finra.org/investors/day-trading-margin-requirements-know-rules
Q - Can I trade outside of market hours?
Alpaca supports the following extended trading hours*:
Pre-market:
9:00am - 9:30am
After-hours:
4:00pm - 6:00pm
More information regarding extended hours trading is available in our documentation
here
.
NOTE:
Trading outside of market hours may increase trading risks. For additional information, refer to Alpaca’s
Extended Hours Trading Risk
.
Q - What type of risks are unique to automated trading?
Mechanical failures
The theory behind automated trading makes it seem simple: Set up the software, program the rules and watch it trade. In reality, however, automated trading is a sophisticated method of trading, yet not infallible. Alpaca initially will only support algorithms that run on your own computer, thus your trading system will reside on your computer – and not a server. What that means is that if an internet connection is lost, an order might not be sent to the market. There is also the potential for a power loss, computer crash, or some other system quirk that could stop your algorithm from running or cause an anomaly.
Monitoring
Although it would be great to turn on the computer and leave for the day / week, automated trading systems do require monitoring or an alerting system. This is due to the potential for mechanical failures, such as connectivity issues, power losses or computer crashes, and to system quirks as mentioned above. It is also possible for an automated trading system to experience anomalies that could result in errant orders, missing orders, or duplicate orders. If the system is monitored and/or has an alerting system, these events can be identified and resolved quickly.
Trading experience
Your level of trading experience with automated trading systems is important in deciding how you should choose your overall trading strategy. Highly complex strategies with many variables make it more difficult to determine whether the trades that will execute are designed to be profitable. Starting with simple automation strategies will allow you to develop experience and learn methods of trading that work best for you.
Over-optimization
Though not specific to automated trading systems, traders who employ backtesting techniques can create systems that look great on paper and perform terribly in a live market. Over-optimization refers to excessive curve-fitting that produces a trading plan that is unreliable in live trading. It is possible, for example, to tweak a strategy to achieve exceptional results on the historical data on which it was tested. Traders sometimes incorrectly assume that a trading plan should have close to 100% profitable trades or should never experience a drawdown to be a viable plan. As such, parameters can be adjusted to create a “near perfect” plan – that completely fails as soon as it is applied to a live market.
Programming discrepancies
There could be a discrepancy between the “theoretical trades” generated by the strategy and the order entry platform component that turns them into real trades. Most traders should expect a learning curve when developing automated trading systems, and it is generally a good idea to start with small trade sizes or conduct “paper trading” while the process is being refined.
No High Frequency Trading
Alpaca’s platform is NOT a high frequency trading platform. While an automated trading strategy can sent trades to the market at a high frequency, Alpaca does not support the necessary speed of either market data flow or trade execution speed necessary for a high frequency trading program to function as intended. Automated trading strategies that have an over-reliance on the speed of market data and speed of execution will not be able to compete effectively with traders who have state of the art equipment and very short high speed connections to the market, in particular when the connection you are using to the internet is via a residential internet service provider.
Reliance on risk-reducing orders or strategies
With automated trading, substituting manual market monitoring with the placing of certain orders (e.g. ‘stop-loss’ orders or ‘stop-limit’ orders) which are intended to limit losses to certain amounts may not be effective because market conditions may make it impossible to execute such orders. At times, it is also difficult or impossible to liquidate a position without incurring substantial losses and Alpaca’s platform does not provide a readily available manual intervention process.
Q – What are Conditional Orders?
Conditional orders, also known as Bracket Orders, are those which will be sent to the market only if specific conditions and criteria are met.
You can trade three types of conditional orders with Alpaca:
OCO (One-Cancels-the-Other)
OTO (One-Triggers-the-Other)
OTOCO (One-Triggers-a-One-Cancels-the-Other)
Visit our Docs on
Orders
and
OCO & OTO launch announcement
to learn more about Conditional Orders.
Q – What risks are associated with Conditional Orders?
Conditional orders may have increased risk as a result of their reliance on trigger processing, market data, and other internal and external systems. Such orders are not sent to the market until specified conditions are met. During that time, issues such as system outages with downstream technologies or third parties may occur. Conditional orders triggering near the market close may fail to execute that day. Furthermore, our executing partner may impose controls on conditional orders to limit erroneous trades triggering downstream orders.
Alpaca Securities may not always be made aware of such changes to external controls immediately, which may lead to some conditional orders not being executed. As such, it is important to monitor conditional orders for reasonability. Conditional orders are “Not Held” orders whose execution instructions are on a best efforts basis upon being triggered. Furthermore, conditional orders may be subject to the increased risks of stop orders and market orders outlined above.
Given the increased potential risk of using conditional orders, the client agrees that Alpaca Securities cannot be held responsible for losses, damages, or missed opportunity costs associated with market data problems, systems issues, and user error, among other factors. By using conditional orders, the client understands and accepts the risks outlined above. Alpaca Securities encourages leveraging the use of Paper accounts to become more comfortable with the intricacies associated with these orders.
Q - What is Paper Trading?
When you run your algorithm with the real-time market, there are many things that can happen that you may not see in backtesting.  Orders may not be filled, prices may spike, or your network may get disconnected and retry may be needed. During the software development process, it is important to test your algorithm to catch these things in advance. AlpacaDB’s Paper Trading Platform provides a real-time simulation environment where you can test your algorithm so you can catch these issues in advance.
In paper trading, orders aren’t routed to the real exchanges. Instead, the system simulates the order filling based on the real-time quotes. It calculates the account balance and performance numbers with virtual money.
Please note that while Alpaca’s Paper Trading Platform makes best effort to simulate real market behavior, active markets can cause results to vary.
To learn more about paper trading and creating an account, please refer to the
Alpaca’s Paper Trading Document
.
Brokerage services are provided by Alpaca Securities LLC (“Alpaca”), member FINRA/SIPC, a wholly-owned subsidiary of AlpacaDB, Inc.
Q - Why can’t I place an order?
If you’re having issues placing an order, please ensure that your API keys are up to date. You can see your current API key on your dashboard, and you can generate a new one there, if the key you’re using is no longer valid. Make sure that your trading program is connected to the right API endpoint - paper and live trading use different URLs and key pairs, and must be generated separately by changing the paper/live account toggle.
If you’re seeing an error about “insufficient quantity” when trying to place a sell order or about having insufficient funds when placing a buy order, it is possible that your trading program is submitting orders too quickly. Orders do not always fill immediately, and your program should not assume that shares have been bought or sold instantly upon order submission. It is likely you will need to wait a moment for an order to be filled. Please ensure your program verifies the status of previous executions before placing new orders that depend on their outcome.
For assistance with debugging issues like this, we invite you to join Alpaca’s
Slack Community
, as well as our
community forum
.
1 Like
Riodda74
December 20, 2021,  1:56pm
4
I have a question about pattern day trading:
Let’s say that on Monday I buy a certain asset, then on tuesday I make another buy of the same asset to increase the position size, then still tuesday I sell the whole (monday buy + Tusday buy) position, this operation counts for pattern day trade ?
darj
September 6, 2023,  1:45pm
6
Yes, the scenario you described would count as a pattern day trade. Pattern day trading (PDT) is a designation used by the U.S. Securities and Exchange Commission (SEC) and Financial Industry Regulatory Authority (FINRA) to define traders who execute four or more day trades within a rolling five-business-day period in a margin account.
In your example:
On Monday, you bought a certain asset.
On Tuesday, you made another buy of the same asset, effectively increasing your position size.
Still on Tuesday, you sold the entire position (both the Monday and Tuesday buys).
Since you bought and sold the same asset on the same trading day (Tuesday), it would count as a day trade. As a result, if you repeated this pattern three more times within the same five-business-day period, you would be classified as a pattern day trader, subject to specific regulations.
Pattern day traders are required to maintain a minimum account equity of $25,000 in a margin account. If the equity falls below this threshold, the trader will be restricted from day trading until they meet the requirement. It’s essential to be aware of these rules to avoid potential violations and penalties associated with
pattern day trading
regulations.
Related topics
Topic
Replies
Views
Activity
Erro: pattern_day_trader': False still errors trade denied due to pattern day trading protection
Alpaca Trading
8
1035
January 25, 2022
APIError: trade denied due to pattern day trading protection
Alpaca Trading
13
7552
September 30, 2024
What counts as a day trade?
Alpaca Trading
8
3859
May 6, 2022
Understanding Day Trade Count and Pattern Day Trading with <$25K
Getting Started with Alpaca
0
877
August 1, 2022
I have $30K yet pattern day trading protection is occuring
Alpaca Account Troubleshooting
5
2952
December 17, 2021