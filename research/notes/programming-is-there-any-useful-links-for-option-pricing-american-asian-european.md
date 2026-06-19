---
title: programming - Is there any useful links for option pricing (american + asian
  + european) using R - Quantitative Finance Stack Exchange
id: programming-is-there-any-useful-links-for-option-pricing-american-asian-european
tags:
- minimum-track-record statistics
created: '2026-06-17T20:05:52.828765Z'
updated: '2026-06-17T20:28:22.906262Z'
source: https://quant.stackexchange.com/questions/34018/what-is-the-minimum-track-record-length-to-determine-if-a-strategy-has-edge
source_domain: quant.stackexchange.com
fetched_at: '2026-06-17T20:05:52.828590Z'
fetch_provider: builtin
status: review
type: note
tier: practitioner
content_type: forum
deprecated: false
summary: programming - Is there any useful links for option pricing (american + asian
  + european) using R - Quantitative Finan...
---

programming - Is there any useful links for option pricing (american + asian + european) using R - Quantitative Finance Stack Exchange
Stack Internal
Knowledge at work
Bring the best of human thought and AI automation together at your work.
Explore Stack Internal
Is there any useful links for option pricing (american + asian + european) using R
Ask Question
Asked
9 years, 1 month ago
Modified
9 years, 1 month ago
Viewed
387 times
3
$\begingroup$
I'm trying to evaluate option pricing mainly american, asian and european options in order to get a plot to measure option valuation in time.
Is there any useful references to do that using R ?
option-pricing
programming
american-options
european-options
asian-option
Share
Improve this question
Follow
asked
Apr 29, 2017 at 22:12
user27705
31
1
1 bronze badge
$\endgroup$
Add a comment
|
1 Answer
1
Sorted by:
Reset to default
Highest score (default)
Date modified (newest first)
Date created (oldest first)
4
$\begingroup$
Below is an example of how you could plot a "call" option value with
RQuantLib
:
library(RQuantLib)
library(ggplot2)
call_price <- sapply(seq(365,0,-1), function(x) AmericanOption("call", 100, 100, 0.2, 0.03, x/365, 0.4)$value)
qplot(day, call_price, data=data.frame(day=0:365, call_price=call_price), geom="line")
The code output:
Another useful package is
fOptions
There is also a book
"Option Pricing and Estimation of Financial Models with R"
Share
Improve this answer
Follow
edited
Apr 30, 2017 at 7:54
Bob Jansen
♦
8,764
8
8 gold badges
41
41 silver badges
61
61 bronze badges
answered
Apr 30, 2017 at 7:38
zer0hedge
1,724
1
1 gold badge
12
12 silver badges
27
27 bronze badges
$\endgroup$
2
$\begingroup$
Quite an improvement!
$\endgroup$
Bob Jansen
–
Bob Jansen
♦
2017-04-30 07:54:14 +00:00
Commented
Apr 30, 2017 at 7:54
$\begingroup$
@BobJansen I have to work hard to improve my reputation :-)
$\endgroup$
zer0hedge
–
zer0hedge
2017-04-30 07:57:38 +00:00
Commented
Apr 30, 2017 at 7:57
Add a comment
|
Your Answer
Draft saved
Draft discarded
Sign up or
log in
Sign up using Google
Sign up using Email and Password
Submit
Post as a guest
Name
Email
Required, but never shown
Post as a guest
Name
Email
Required, but never shown
Post Your Answer
Discard
By clicking “Post Your Answer”, you agree to our
terms of service
and acknowledge you have read our
privacy policy
.
Start asking to get answers
Find the answer to your question by asking.
Ask question
Explore related questions
option-pricing
programming
american-options
european-options
asian-option
See similar questions with these tags.
Featured on Meta
Native Ads Coming To Comments
Related
4
Pricing the European counterpart from American Options
2
Wrong pricing of Asian Option
1
Pricing American style Asian option
5
Monte Carlo for Asian Pricing
0
If most real options are American, why so much focus on European option pricing?
4
What are the downsides of using Kim's integral equation (1990) to determine the exercise boundary of an American option?
Hot Network Questions
Does Ontario offer a province-wide library card, like British Columbia’s One Card?
Hat Game with 2 prisoners
Electrical Current Traveling Down a Curve?
Can a planet orbit BETWEEN two stars?
What is the source of this Greek-Persian anecdote in de la Boetie's "Discourse on Voluntary Servitude"?
World Cup Group M puzzle
IPO stocks: does selling covered calls trigger the flipping rule?
What does 褃劲 mean?
With discoloration and blackness that can't be scrubbed off, is this pan safe to use?
The Great Escape
Statistics of a distribution on unitary matrices
How can I rotate a face to align with the axis?
Does Quine believe a phenomenalist conceptual scheme can be translated into a physicalist conceptual scheme?
To ensure a smooth experience on REDMI Note 15 Pro+
Applications of Egorov's theorem beyond the Bounded Convergence Theorem
Why would Australian Border Force issue Do Not Board for an outbound flight?
Can we observe and have we observed 'time-refraction'?
Best practice for GND copper planes with basic PCB design
How to actually calculate a universal object in a category
Homotopy spaces of relative maps
What kind of hexagonal antenna is this?
Does Number of Games Played Somewhat Correlate to Chess Rating?
How to include section template defined heading prefix in TOC?
Why band gap decreases as the anions are changed in ABX3 lattice?
more hot questions
Question feed
default