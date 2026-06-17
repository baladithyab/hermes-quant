---
title: How Not To Run an A/B Test – Evan Miller
id: how-not-to-run-an-ab-test-evan-miller
tags:
- multiple-comparisons thin-sample adversarial statistics
created: '2026-06-17T20:13:46.716785Z'
updated: '2026-06-17T20:28:23.127639Z'
source: https://www.evanmiller.org/how-not-to-run-an-ab-test.html
source_domain: www.evanmiller.org
fetched_at: '2026-06-17T20:13:46.716604Z'
fetch_provider: builtin
status: review
type: note
tier: unknown
content_type: unknown
deprecated: false
summary: How Not To Run an A/B Test – Evan Miller
---

How Not To Run an A/B Test – Evan Miller
How Not To Run an A/B Test
By
Evan Miller
April 18, 2010
Translations:
Russian
Spanish
Uzbek
If you run A/B tests on your website and regularly check ongoing experiments for significant results, you might be falling prey to what statisticians call
repeated significance testing errors
. As a result, even though your dashboard says a result is statistically significant, there’s a good chance that it’s actually insignificant. This note explains why.
Background
When an A/B testing dashboard says there is a “95% chance of beating original” or “90% probability of statistical significance,” it’s asking the following question: Assuming there is no underlying difference between A and B, how often will we see a difference like we do in the data just by chance? The answer to that question is called the significance level, and “statistically significant results” mean that the significance level is low, e.g. 5% or 1%. Dashboards usually take the complement of this (e.g. 95% or 99%) and report it as a “chance of beating the original” or something like that.
However, the significance calculation makes a critical assumption that you have probably violated without even realizing it:
that the sample size was fixed in advance
. If instead of deciding ahead of time, “this experiment will collect exactly 1,000 observations,” you say, “we’ll run it until we see a significant difference,”
all the reported significance levels become meaningless
. This result is completely counterintuitive and all the A/B testing packages out there ignore it, but I’ll try to explain the source of the problem with a simple example.
Example
Suppose you analyze an experiment after 200 and 500 observations. There are four things that could happen:
Scenario 1
Scenario 2
Scenario 3
Scenario 4
After 200 observations
Insignificant
Insignificant
Significant!
Significant!
After 500 observations
Insignificant
Significant!
Insignificant
Significant!
End of experiment
Insignificant
Significant!
Insignificant
Significant!
Assuming treatments A and B are the same and the significance level is 5%, then at the end of the experiment, we’ll have a significant result 5% of the time.
But suppose we stop the experiment as soon as there is a significant result. Now look at the four things that could happen:
Scenario 1
Scenario 2
Scenario 3
Scenario 4
After 200 observations
Insignificant
Insignificant
Significant!
Significant!
After 500 observations
Insignificant
Significant!
trial stopped
trial stopped
End of experiment
Insignificant
Significant!
Significant!
Significant!
The first row is the same as before, and the reported significance levels after 200 observations are perfectly fine. But now look at the third row.
At the end of the experiment, assuming A and B are actually the same, we’ve increased the ratio of significant relative to insignificant results. Therefore, the reported significance level – the “percent of the time the observed difference is due to chance” – will be wrong.
How big of a problem is this?
Suppose your conversion rate is 50% and you want to test to see if a new logo gives you a conversion rate of more than 50% (or less). You stop the experiment as soon as there is 5% significance, or you call off the experiment after 150 observations. Now suppose your new logo actually does nothing. What percent of the time will your experiment wrongly find a significant result? No more than five percent, right? Maybe six percent, in light of the preceding analysis?
Try 26.1% –
more than five times what you probably thought the significance level was
. This is sort of a worst-case scenario, since we’re running a significance test after every observation, but it’s not unheard-of. At least one A/B testing framework out there actually provides code for automatically stopping experiments after there is a significant result. That sounds like a neat trick until you realize it’s a statistical abomination.
Repeated significance testing always increases the rate of false positives, that is, you’ll think many insignificant results are significant (but not the other way around). The problem will be present if you ever find yourself “peeking” at the data and stopping an experiment that seems to be giving a significant result. The more you peek, the more your significance levels will be off. For example, if you peek at an ongoing experiment ten times, then what you think is 1% significance is actually just 5% significance. Here are other reported significance values you need to see just to get an actual significance of 5%:
You peeked…
To get 5% actual significance you need…
1 time
2.9% reported significance
2 times
2.2% reported significance
3 times
1.8% reported significance
5 times
1.4% reported significance
10 times
1.0% reported significance
Decide for yourself how big a problem you have, but if you run your business by constantly checking the results of ongoing A/B tests and making quick decisions, then this table should give you goosebumps.
What can be done?
If you run experiments:
the best way to avoid repeated significance testing errors is to not test significance repeatedly. Decide on a sample size in advance and wait until the experiment is over before you start believing the “chance of beating original” figures that the A/B testing software gives you. “Peeking” at the data is OK as long as you can restrain yourself from stopping an experiment before it has run its course. I know this goes against something in human nature, so perhaps the best advice is: no peeking!
Since you are going to fix the sample size in advance, what sample size should you use? This formula is a good rule of thumb:
\[
n = 16 \frac{\sigma^2}{\delta^2}
\]
Where \(\delta\) is the minimum effect you wish to detect and \(\sigma^2\) is the sample variance you expect. Of course you might not know the variance, but if it’s just a binomial proportion you’re calculating (e.g. a percent conversion rate) the variance is given by:
\[
\sigma^2 = p \times (1 - p)
\]
Committing to a sample size completely mitigates the problem described here.
UPDATE, May 2013:
You can see this formula in action with my new interactive
Sample Size Calculator
. Enter the effect size you wish to detect, set the power and significance levels, and you'll get an easy-to-read number telling you the sample size you need.
END OF UPDATE
If you write A/B testing software:
Don’t report significance levels until an experiment is over, and stop using significance levels to decide whether an experiment should stop or continue. Instead of reporting significance of ongoing experiments, report how large of an effect can be detected given the current sample size. That can be calculated with:
\[
\delta = (t_{\alpha/2} + t_\beta)\sigma \sqrt{2/n}
\]
Where the two \(t\)’s are the t-statistics for a given significance level \(\alpha/2\) and power \((1-\beta)\).
Painful as it sounds, you may even consider excluding the “current estimate” of the treatment effect until the experiment is over. If that information is used to stop experiments, then your reported significance levels are garbage.
If you really want to do this stuff right:
Fixing a sample size in advance can be frustrating. What if your change is a runaway hit, shouldn’t you deploy it immediately? This problem has haunted the medical world for a long time, since medical researchers often want to stop clinical trials as soon as a new treatment looks effective, but they also need to make valid statistical inferences on their data. Here are a couple of approaches used in medical experiment design that someone really ought to adapt to the web:
Sequential experiment design
: Sequential experiment design lets you set up checkpoints in advance where you will decide whether or not to continue the experiment, and it gives you the correct significance levels.
Learn more:
“Simple Sequential A/B Testing”
Bayesian experiment design
: With Bayesian experiment design you can stop your experiment at any time and make perfectly valid inferences. Given the real-time nature of web experiments, Bayesian design seems like the way forward.
Learn more:
“Bayesian A/B Testing”
Conclusion
Although they seem powerful and convenient, dashboard views of ongoing A/B experiments invite misuse. Any time they are used in conjunction with a manual or automatic “stopping rule,” the resulting significance tests are simply invalid. Until sequential or Bayesian experiment designs are implemented in software, anyone running web experiments should only run experiments where the sample size has been fixed in advance, and stick to that sample size with near-religious discipline.
Further reading
Repeated Significance Tests
P. Armitage, C. K. McPherson, and B. C. Rowe. “Significance Tests on Accumulating Data,”
Journal of the Royal Statistical Society.
Series A (General), Vol. 132, No. 2 (1969), pp. 235-244
Optimal Sample Sizes
John A. List, Sally Sadoff, and Mathis Wagner. “So you want to run an experiment, now what? Some Simple Rules of Thumb for Optimal Experimental Design.” NBER Working Paper No. 15701
Wheeler, Robert E. “Portable Power,”
Technometrics
, Vol. 16, No. 2 (May, 1974), pp. 193-201
Sequential Experiment Design
Pocock, Stuart J. “Group Sequential Methods in the Design and Analysis of Clinical Trials,”
Biometrika
, Vol. 64, No. 2 (Aug., 1977), pp. 191-199
Pocock, Stuart J. “Interim Analyses for Randomized Clinical Trials: The Group Sequential Approach,”
Biometrics
, Vol. 38, No. 1 (Mar., 1982), pp. 153-162
Bayesian Experiment Design
Berry, Donald A. “Bayesian Statistics and the Efficiency and Ethics of Clinical Trials,”
Statistical Science
, Vol. 19, No. 1 (Feb., 2004), pp. 175-187
You’re reading
evanmiller.org
, a random collection of math, tech, and musings. If you liked this you might also enjoy:
The Low Base Rate Problem
Simple Sequential A/B Testing
You Can’t Spell CUPED Without Frisch-Waugh-Lovell
Get new articles as they’re published, via
LinkedIn
,
Twitter
, or
RSS
.
Want to look for statistical patterns in your MySQL, PostgreSQL, or SQLite database? My desktop statistics software
Wizard
can help you analyze
more data in less time
and
communicate discoveries visually
without spending days struggling with pointless command syntax. Check it out!
Wizard
Statistics the Mac way
Back to Evan Miller’s home page
–
Subscribe to RSS
–
LinkedIn
–
Twitter