---
title: This Blog is Systematic
id: this-blog-is-systematic
tags:
- minimum-trades statistics carver
created: '2026-06-17T20:14:14.112878Z'
updated: '2026-06-17T20:28:23.139189Z'
source: https://qoppac.blogspot.com/
source_domain: qoppac.blogspot.com
fetched_at: '2026-06-17T20:14:14.112713Z'
fetch_provider: builtin
status: review
type: note
tier: commentary
content_type: blog
deprecated: false
summary: Honey I shrunk the weights (instead of the inputs!)
---

This Blog is Systematic
Home
Systematic investment
Systematic trading
Misc other posts
Code
My Books
Talks and media
Resources
Wednesday, 17 June 2026
Honey I shrunk the weights (instead of the inputs!)
TLDR: This is a post about something that doesn't work. So don't read if you only care about cherry picked delightful backtests.
This is my fifth post in a rapid fire intense series on portfolio optimisation. In my
last post
I looked at the optimal amount of shrinkage to use with real data, when running a bayesian methodology for mean variance optimisation. I found two things. Firstly, the optimal shrinkage was different for different sizes of in and out of sample periods. Secondly, that there was mostly a great deal of uncertainty about what the optimium was, with fairly flat surfaces and insigificant t-statistics abounding. I also found that random based methods (monte carlo and bootstrapping) don't work as well as the best shrinkage methods (and in some cases, do worse than the poorest methods). That's three things, but the latter point isn't relevant to this post.
Hence, shrinkage of 0.5 on SR and 0.75 for correlations seemed reasonable; but the truth is we don't really know for sure.
Now I am a big fan of the work of Resolve asset managment. And one thing they are fond of doing is if two or more things seem to work equally well, just taking an average of them (for example they do this here with
CTA replication
). And I also know intuitively that taking an average of portfolio weights is better than taking an average of inputs. Therefore might we not do better by taking an average of the weights produced by different shrinkage methods?
For example, if we averaged the weights produced by naive mean variance (NMV - zero shrinkage) and equal weights (full shrinkage on both inputs), then we're basically shrinking the weights.
This leaves us with two open questions (apart from the obvious question, which is how long I will continue flogging this subject to death):
What are we averaging?
What averaging weights should we use?
For the second part I'm going to keep things simple and just use
equal weights
. For the first part, consider this grid of shrinkage options. This is a subset of what we have seen before:
0.00  0.50  1.00
0
A       B     C
0.5
D       E     F
1.0
G       H     J
Each row is a different SR shrinkage. Each column is another level of correlation shrinkage. There are 9 options of shrinkage. Some have special names. A is no shrinkage; naive mean variance. B is closest to the optimal shrinkage from the
EPO paper
I have referenced before. E is not that different from the empirical option I selected in the previous post. J is full shrinkage; equal weights.
Now if I said to me "Rob, you can only choose
two
options from this list", I would select:
A and J
or perhaps, C and G
If allowed three options, I'd throw in E, so:
A, E,J
C, E, G
With four options I would hit the corners:
A,C,G,J
Finally with five options I would hit the corners and the centre:
A,C,G,J, E
With everything equally weighted in all of the above. This gives me six different permutations. These in turn can be compared to each of the individual shrinkage options (since we have to calculate them anyway...), so we're comparing 15 possibles.
I'm going to use exactly the same set up as the previous post; randomly chosen portfolios of nine trading rules for a random instrument; varying the size of the in sample and out of sample periods.
Note: yes the title is an allusion to
this paper.
One year in sample, one year out of sample
SR median  SR
0.05
T statistic
A
0.029
-
1.531        0.104
B
0.014
-
1.549        0.745
C
0.008
-
1.598        0.025
D
0.031
-
1.527        0.507
E
0.038
-
1.573
NaN
F
0.014
-
1.572        0.028
G
0.008
-
1.729        0.004
H
0.023
-
1.704        0.036
J
0.012
-
1.694        0.019
AJ
0.013
-
1.584        0.031
CG
0.003
-
1.668        0.001
AEJ
0.006
-
1.603        0.020
CEG
0.002
-
1.591        0.001
ACGJ
0.001
-
1.610        0.001
ACEGJ
0.005
-
1.595        0.001
0.00  0.50  1.00
0
A       B    C
0.5
D       E    F
1.0
G       H    J
Hopefully the format of this table makes sense. The first two columns are median SR across all the random portfolios, and the 5% point of the distribution of random portfolios. You can see that option E is best at the median point (0.5 shrinkage on both), but D is slightly better at the 5% point. The final column is the result of a paired t-statistic comparing the optimal choice (which has NaN in this column) and the choice on the appropriate line. A number below 0.05 means the optimum is significantly better at a 5% critical value, i.e. there is a 95% or more chance it isn't just pure luck.  One benefit of doing this optimisation is that there are fewer options, plus no random methods; so it's very quick. Hence I can get more reasonable t-statistics here (I have 4,000 values in my sample).
But you can still see that E isn't significantly better than D, and nor is A or B. C, F, H and J are insignificant at a 5% level, but not a 1% level. All the values in the top left quadrant are fine.
You will remember from the
last post
that the optimum was shrinkage of 0.25 SR, 0.6 correlations but also that there almost no statistical difference between sensible shrinkage results. Option E is closest to that previous optimum.
Sadly none of the new 'combo' options are any good.
One year in sample, five years out of sample
SR median  SR
0.05
T statistic
A
0.148
-
0.628          0.0
B
0.157
-
0.603          0.0
C
0.164
-
0.578          0.0
D
0.140
-
0.623          0.0
E
0.156
-
0.600          0.0
F
0.161
-
0.582          0.0
G
0.155
-
0.594          0.0
H
0.170
-
0.567          0.0
J
0.193
-
0.491
NaN
AJ
0.173
-
0.549          0.0
CG
0.176
-
0.539          0.0
AEJ
0.170
-
0.571          0.0
CEG
0.175
-
0.561          0.0
ACGJ
0.178
-
0.541          0.0
ACEGJ
0.179
-
0.542          0.0
0.00  0.50  1.00
0
A       B    C
0.5
D       E    F
1.0
G       H    J
Some amazing significance there - basically equal weights is better than everything by some margin. This is exactly the result from before. And the combos don't perform as well.
Five years in sample, one year out of sample
SR median  SR
0.05
T statistic
A
0.051
-
1.742        0.276
B
0.057
-
1.769
NaN
C
0.052
-
1.763        0.147
D
0.048
-
1.738        0.825
E
0.049
-
1.757        0.883
F
0.043
-
1.754        0.062
G
0.010
-
1.842        0.005
H
0.026
-
1.767        0.011
J
0.004
-
1.803        0.008
AJ
0.025
-
1.759        0.031
CG
0.019
-
1.789        0.013
AEJ
0.042
-
1.757        0.355
CEG
0.034
-
1.749        0.043
ACGJ
0.016
-
1.747        0.081
ACEGJ
0.037
-
1.745        0.047
0.00  0.50  1.00
0
A       B    C
0.5
D       E    F
1.0
G       H    J
Again, the newer combo methods aren't much cop although AEJ is a little better than J.
Five years in sample, five years out of sample
SR median  SR
0.05
T statistic
A
0.159
-
0.666        0.000
B
0.166
-
0.648        0.365
C
0.165
-
0.642        0.233
D
0.157
-
0.666        0.000
E
0.173
-
0.660
NaN
F
0.168
-
0.647        0.259
G
0.128
-
0.676        0.000
H
0.141
-
0.667        0.000
J
0.144
-
0.660        0.000
AJ
0.162
-
0.659        0.051
CG
0.157
-
0.653        0.001
AEJ
0.172
-
0.667        0.086
CEG
0.162
-
0.654        0.013
ACGJ
0.161
-
0.655        0.030
ACEGJ
0.163
-
0.658        0.054
0.00  0.50  1.00
0
A       B    C
0.5
D       E    F
1.0
G       H    J
Again the middle ground of E is the best; we're also seeing more extreme shrinkage (the bottom row) do very badly as does mean variance. None of the combos do as well.
Ten years in sample, one year out of sample
SR median  SR
0.05
T statistic
A         -
0.016
-
1.850        0.166
B         -
0.025
-
1.827        0.269
C         -
0.015
-
1.775        0.223
D         -
0.007
-
1.853
NaN
E         -
0.028
-
1.815        0.991
F         -
0.017
-
1.795        0.654
G         -
0.076
-
1.894        0.000
H         -
0.082
-
1.883        0.268
J         -
0.097
-
1.856        0.000
AJ        -
0.074
-
1.846        0.095
CG        -
0.069
-
1.858        0.000
AEJ       -
0.057
-
1.843        0.622
CEG       -
0.058
-
1.844        0.001
ACGJ      -
0.069
-
1.857        0.006
ACEGJ     -
0.058
-
1.840        0.004
0.00  0.50  1.00
0
A       B    C
0.5
D       E    F
1.0
G       H    J
I struggled to get statistical significance for this set before; I have some now, but basically again somewhere in the region of D and E is best. Combo methods do not win though again AEJ isn't significantly worse.
Ten years in sample, five years out of sample
SR median  SR
0.05
T statistic
A
0.089
-
0.825        0.962
B
0.095
-
0.865        0.578
C
0.099
-
0.848        0.083
D
0.086
-
0.842        0.581
E
0.095
-
0.852        0.431
F
0.101
-
0.848
NaN
G
0.072
-
0.923        0.000
H
0.076
-
0.930        0.000
J
0.089
-
0.939        0.000
AJ
0.082
-
0.889        0.000
CG
0.082
-
0.908        0.000
AEJ
0.089
-
0.874        0.000
CEG
0.091
-
0.907        0.000
ACGJ
0.085
-
0.896        0.000
ACEGJ
0.088
-
0.892        0.000
0.00  0.50  1.00
0
A       B    C
0.5
D       E    F
1.0
G       H    J
'Somewhere in the middle row' isn't a song from Wizard of OverFitting; but roughly where you want to be once again. The combo results are a dismal failure.
Summary
Someone once told me "I love your blog and your books because you talk about failures as well as successes". Well whoever that was - you'll have loved this one!
Posted by
Rob Carver
at
17:07
No comments:
Email This
BlogThis!
Share to X
Share to Facebook
Share to Pinterest
Labels:
asset allocation
,
Portfolio optimization
,
shrinkage
Monday, 15 June 2026
FIFA* World Cup  (*Fitting and Forecasting Actual data) Portfolio Optimisation competition with real returns
This is my fourth post in my summer 2026 mini series on portfolio optimisation.
It will very much follow the format of (also with a sports alluding title) blog
post number two
, so it might be worth rereading that. A reminder if you can't be bothered, I used random data to compare some optimisation methods:
monte carlo (random, parameteric)
bootstrapping (random, non parametric)
double shrinkage (shrinking SR towards average SR, and correlations to zero). This encompasses some other methods including:
NMV naive mean variance (no shrinkage on anything)
EW equal weights (both full shrinkage)
MD maximum diversification (no shrinkage correlation, full shrinkage on SR)
EPO (we just shrink the correlation matrix to some degree)
I found that MC/Bootstrap were the best, and didn't require any pesky estimation of the shrinkage meta-parameter. But they are SLOW. I worked out you'd need quite a few iterations to get the weights to converge, so each optimisation took quite a while. Should you wish to estimate that meta-parameter I found that for random data with a nice stable distribution that you didn't need much shrinkage. A little bit on the Sharpe Ratio was the most optimal; a little more wouldn't harm things much, but a lot was bad.
However as we know from
post three
, real data is not as nice as random data, and is much harder to forecast. It has a habit of doing annoying things, like changing it's distribution when you're not looking. So we're expecting that we will need, for example, more shrinkage to reflect this.
The real data we will be using will many different runs, each consisting of 9 randomly selected trading rules, chosen for a single randomly chosed instrument. Because we know from
post one
that fitting
within
instruments is the way to go. Although I currently have 40 trading rules in my actual portofolio, I am sticking with nine now for speed and intuition. Plus the results shouldn't be too different with more components - that is something I will be looking at later in the series. I'm sampling with replacement so it's feasible - but very unlikely- I'll get the same instrument/rule set more than once.
As per my previous posts I'm also going to compare the results for different lengths of data. In the random data post I could generate as much data as I want; that's tricky here when the absolute longest history I have for any instrument is just over 50 years and many are much less than that. So I'm going to use in sample lengths of 1 year, 5 years and 10 years; and out of sample lengths of 1 year and 5 years. If an instrument doesn't have sufficient data for a given pairing I won't use it; eg for 10 years/5 years I would need 15 years which will be tricky for many instruemnts whilst for 1 year/1 year I would just need 2 years obviously. If it has more data than required, then on a given random run I'll randomly select the required 2 to 15 year long period.
First some speed statistics. We already know that shrinkage will be darn quick, but as I'm using different data lengths from the prior post it's probably worth repeating the stats for montecarlo and bootstrap:
1 year in sample       5 years in sample      10 years in sample
BS          9.2                     20.6                       33.3
MC          5.1                      6.6                        8.0
Remember from the previous post that convergence is quicker with Monte Carlo than with Bootstrap, hence the substantially longer time taken to do BS which needs twice as many iterations; as well as the slight difference in implementation per iteration which explains the even worse performance of BS at longer iterations.
Results
One year in sample, One year out of sample
Let's begin with the median results. For the moment I'm going to present two data frames. The first is just Sharpe Ratios. Here is the one for an insample and out of sample period of just one year:
0.00   0.20   0.40   0.60   0.70   0.75   0.80   0.90   1.00
0
0.056  0.057  0.039  0.054  0.047  0.049  0.046  0.055  0.032
0.25
0.061  0.045  0.054  0.063  0.057  0.049  0.044  0.042  0.037
0.5
0.059  0.057  0.048  0.047  0.044  0.046  0.041  0.046  0.044
0.75
0.049  0.041  0.062  0.058  0.041  0.026  0.029  0.047  0.033
0.8
0.030  0.041  0.061  0.054  0.041  0.025  0.026  0.026  0.032
0.85
0.016  0.038  0.050  0.035  0.030  0.029  0.023  0.025  0.036
0.9
-0.002  0.022  0.043  0.030  0.041  0.038  0.029  0.024  0.052
0.95
0.015  0.022  0.045  0.043  0.049  0.043  0.049  0.034  0.056
1.0
0.014  0.003  0.038  0.060  0.056  0.058  0.043  0.032  0.004
MC   -0.000 -0.000 -0.000 -0.000 -0.000 -0.000 -0.000 -0.000 -0.000
BS    0.018  0.018  0.018  0.018  0.018  0.018  0.018  0.018  0.018
This will look very familiar if you looked at the previous post on random data, but there are a couple of extra rows. From the top then each column shows a different degree of correlation shrinkage. On the left 0.0 is no shrinkage where we used the estimated data. 1.0 is full shrinkage, where all correlations are set to zero. Apart from the diagonals. Obviously. Each row then is a different degree of SR shrinkage, from the top row where we use no shrinkage, down to the row labelled 1.0 where we fully shrink all SR to the average SR across assets.
The bottom two rows are the results for Monte Carlo and Bootstrapping. There is no shrinkage here, so for consistency I've just copied the single value for each across all columns.
Some elements of interest in the main part of the table, the top left corner (0.0, 0.0) is naive mean variance with no shrinkage, the top right (0.0, 1.0) is full correlation shrinkage, the bottom left (1.0, 0.0) is full SR shrinkage, and the bottom right (1.0, 1.0) is full shrinkage on both which leads to equal weights. The EPO empirical optimal is (0, 0.75).
The optimum value here has some shrinkage: 0.25 on SR and 0.60 on correlations.
Compare and contrast that with the results for random data. The optimal shrinkage was barely nothing: 0.25 SR, 0 correlations or thereabouts. It isn't surprising we need more shrinkage in general. Remember from the previous post in this series on random data:
Essentially random data sets a
lower bound
on robustness calibration. For example, suppose we determine that the correct shrinkage for the vector of expected SR on a Bayesian portfolio optimisation using random data is 0.1 (which means we average using 90% of the estimated SR, and 10% of the prior SR). Then it's likely the correct shrinkage on real data will be higher than 0.1.
However the amount of optimal SR versus correlation shrinkage might seem surprising. Quoting now
from post three
in this series, on forecasting statistical parameters with real data:
In simple terms, we are a little bit worse than forecasting Sharpe Ratios in real data one year ahead than we would be with random data, but a LOT worse with correlations. Partly this is because we are pretty terrible at forecasting SR one year ahead anyway even with a stable underlying distribution; we don't do much worse with real data. However it does seem that correlations are far more unstable in reality than in randomly generated data.... If we recall from the prior post that the optimal shrinkage is zero on correlations with random data; we can now see why with actual data we'd probably want to opt for some correlation shrinkage; purely because the sampling error is much larger in practice. That is the empirical finding of the EPO paper. It does feel a bit weird since up to now my gut feeling has been that we have to shrink means a lot because they are much harder to forecast and because they have an outsized effect on portfolio weights compared to differences in correlation. Whilst the latter is still true it seems the former is not.
There are two different effects here remember: predicability of each estimate compared to random data (where correlation is worse), and more about their outright predictability (where SR is worse), and the different effects each has on MV optimisation (small differences in SR affect the outcome more).
Another surprise might be the relatively poor performance of MC and BS. Remember that the only difference between them is the assumption of joint Gaussian returns in one case and not in the other.  In the random data round each method was the best performing. Both however are making an implicit assumption that there is a stable distribution (parameteric in one case, not in the other), and that any variance in outcome over the out of sample period will be the same as would be expected from the sampling distribution of each parameter. Which is exactly what happens with random data. But we know
from post three
that the parameter estimates we're making have a wider distribution with real data; and this is especially true for correlations. Hence, the MC/BS methods are too optimistic about predictability and their weights are suboptimal compared to those produced by high shrinkage optimisations.
Note: I have ideas to fix that, which may or may not in a subsequent blog post. Briefly they involve playing with the MC parameter inputs to reflect the higher RMSE of real versus random data.
Now let's run a paired t-test comparision of that optimum median value against all other values. Here are the p=values from doing those tests:
0.00  0.20  0.40  0.60  0.70  0.75  0.80  0.90  1.00
0
0.91  0.64  0.39  0.63  0.86  0.83  0.90  0.56  0.22
0.25
0.84  0.83  0.99   NaN  0.27  0.24  0.64  0.75  0.37
0.5
0.62  0.37  0.34  0.20  0.07  0.19  0.17  0.54  0.79
0.75
0.76  0.81  0.70  0.70  0.79  0.93  0.85  0.99  0.93
0.8
0.81  0.95  0.81  0.84  0.95  1.00  0.99  0.67  0.97
0.85
0.99  0.97  0.72  0.67  0.76  0.69  0.84  0.60  0.99
0.9
0.92  0.68  0.84  0.85  0.66  0.64  0.74  0.71  0.91
0.95
0.73  0.64  0.91  0.73  0.72  0.60  0.56  0.66  0.94
1.0
0.71  0.68  0.87  0.46  0.41  0.42  0.34  0.42  0.47
MC    0.05  0.05  0.05  0.05  0.05  0.05  0.05  0.05  0.05
BS    0.06  0.06  0.06  0.06  0.06  0.06  0.06  0.06  0.06
We can see that the optimum value itself is NaN since the p-value is undefined. We can also see that statistically, there isn't much difference in how shrinkage is used. MC and BS are definitely worse
Now, how do things change if we are more pessimistic? As before I'm going to look at the 5% distributional point of outcomes from my multiple random results. If I do this, the optimal shrinkage is 0.8 on correlations, but a massive 1.0 on SR. At the 25% point it's 0.75 on SR but 1.0 on correlations. We want more shrinkage for sure!
Now let's think about a nice graphical way of showing these values. I'll start with a heatmap of the median SR:
Now I'm going to do something familar to the students on my course. I'm going to replace every value that is statistically insigificant from the optimal median with the optimal media value. Here I will use a 90% critical value:
Here the result looks like a really shit piece of modern art. Since almost all shrinkage values are not significantly different from the optimal; except weirdly correlation shrinkage 0.7 and SR shrinkage 0.5 (which is adjacent to the optimum); it's just a sea of blue. But we can see the MC/BS methods are inferior.
One year in sample, Five years out of sample
It isn't obvious but I used the same procedure for this plot which shows SR, with all values that can't be distinguished from the optimum in the same colour as that optimum. But every single value other than the optimum, which is full shrinkage or equal weights, is inferior to that optimum.
Five years in sample, One years out of sample
A very interesting picture here. There's clearly a shrinkage area that doesn't work. Note that the results overall are quite poor.
Five years in sample, Five years out of sample
A little clearer here. Modest shrinkage would work well, but then so would random data. Just don't shrink the SR too much.
Ten years in sample, One years out of sample
Importantly here the critical value is 80%, not 90%. With 90% the whole plot goes one colour. Pretty much any amount of shrinkage works. Again the SR results are very poor.
Ten years in sample, Five years out of sample
Summary of results
Well that was messy. I'd conclude that shrinkage of SR 0.5 and correlation 0.75 (the EPO value) is in the optimum region in almost all time periods. That's a reversal of what my original intuition suggested and I've used before, with more shrinkage on the SR. I've explained at length why my intuition was wrong. The random methods (MC/BS) are also inferior in many cases, as well as being slow.
The exception is one year / five years where you need full shrinkage (equal weights). Using 0.5/0.75 isn't so bad however. Although it's significantly worse, the actual loss in SR is small. Still it does seem logical to use more shrinkage with more data; and we can see from the one year/five year plot that we're better off shrinking SR more. So here is my heuristic rule of thumb:
Five or more years of data: SR shrinkage 0.5, correlation 0.75
Four to five years of data: SR shrinkage 0.6, correlation 0.75
Three to four years of data: SR shrinkage 0.7, correlation 0.80
Two to three years of data: SR shrinkage 0.8, correlation 0.85
One to two years of data: SR shrinkage 0.9, correlation 0.90
One or less than one year of data: SR shrinkage 1.0, correlation 1.0 (equal weights)
These results are very domain specific. In particular, I'm mostly dealing with holding periods in the weeks and months. A faster trading system would be able to compress the periods above. But the main lesson is that it's very hard to state categoricially what the exact amount of shrinkage should be. The surface is mostly too noisy. So don't sweat it. Use a vaguely okay value and you'll do vaguely ok.
Posted by
Rob Carver
at
12:24
No comments:
Email This
BlogThis!
Share to X
Share to Facebook
Share to Pinterest
Labels:
correlation
,
fitting
,
Portfolio optimization
,
Sharpe ratio
,
Statistics
,
Uncertainty
Monday, 8 June 2026
Forecasting statistical estimates when data gets real
This is my third post in a series about optimisation and fitting. In my
previous post
I used random data to calibrate and evaluate many portfolio optimisation techniques. It's worth quoting in full from that post:
Random data is not real data: Well duh. But why is this important? Because random data is drawn from a fixed and well behaved distribution. This means the optimiser only has to discover / estimate the parameters of that distribution as more data is revealed to it. But real data doesn't have a fixed and known distribution. It doesn't actually have any distribution at all. We just model it hoping it does.
To summarise then, random data from a fixed distribution differs from real data in three important ways:
There is no distribution! We just assume there is one.
The distribution (which doesn't exist) is not known, and thus it's likely the distribution we assume is the wrong one. This is especially true for modelling underlying financial price returns with joint Gaussian models.
The distribution (which again, doesn't exist) isn't fixed, but can change over time.
And it has one thing in common:
The unknown parameters of the distribution are unknown and have to be learned over time.
In this post I'm going to explore this learning process for two key statistical estimates: correlations and Sharpe Ratios. What I am interested in is how much wider of the mark our estimates for these two things are likely to be for real data vs random data. This obviously has important implications for optimisation.
Let's look at a plot.
This is for random data generated by a process with a true SR of 1. It shows the evolution of the SR and it's statistical distribution as it is re-estimated each year. There is a burn in year which is missing, and then in the first year we can see our estimate of the SR using all available data so far (in orange), and the SR for the current year (in blue). You can see that the orange line is lagged by a year as it is purely out of sample and always a year behind. I've then used the orange line to estimate the theoretical sampling distribution of the Sharpe Ratio for a one year period, and constructed a 1.96x confidence interval (so about 95%) around the orange line which are the green and red lines.
Note: The theoretical standard deviation of the sampling distribution of the Sharpe Ratio, assuming i.i.d. returns, is sqrt[(1+0.5SR^2)/N] where N is the number of periods.
Broadly speaking if our estimates are correct then we'd hope to see around 1/20 of the blue points outside the red and green lines, and around 19/20 on the inside. There are 40 years of data here and we go outside the range twice, which is roughly what we'd expect.
Another way of measuring this is to look at our error term, normalised by our standard deviation. This will be equal to:
[(SR estimate this year N) - (SR estimate years 0...N-1)]/(SR sampling std dev error 0... N-1)
If I take the square of this, average of all years and then square root I get the normalised root mean squared error. This comes out at 0.998 for all the data above.
The blue line in this plot shows the absolute value of the error term for each year. The orange line shows the RMSE. You can see this gradually declining over time and settling in at around 0.85
Here are the same two plots for a correlation pair estimate:
Again the RMSE tends to end up around 0.86
Incidentally, we can also do these plots for longer periods. Here is the RMSE evolution for a SR estimate looking ahead over the next 5 years:
The RMSE here is a little higher - around 1.0
Now, let's look at some
real data
. I'm going to use the p&l from trading the US10 year bond with a 16,64 day EWMAC. Let's begin by trying to forecast the SR one year ahead:
Even without calculating the error we can see that there are more boundary breakages than before with random data. Here is the error:
Notice that it is higher than before (around 1.25; or about sqrt(2) times bigger than the random data RMSE) and doesn't slowly converge as it did with random data, instead it stays roughly constant (ignoring the initial period of luck at the start).
We get a similar picture for 5 years:
What about correlations? Let's look at the correlation between this slow momentum on 10 year US bonds, and the carry rule on the same instrument:
Wow, that's noisy. The RMSE will be off the charts. What about over 5 years?
Ouch. If we look at the correlation between two variations of the same trading rule, EWMAC64,256 and EWMAC32,128 - which are naturally highly correlated - then it's not much better:
Again the RMSE would be in double digits.
Those might be flukes, so let's look at lots of random results. I'm going to pick an instrument and trading rule randomly, and measure it's final RMSE number. I will then generate some random returns of the same length from the same SR distribution (by measuring the full sample SR for the relevant instrument/rule pairing); and measure that's RMSE. I will then select another rule from the same instrument, get the correlation of the two p&l streams, and generate some more random returns with the given expected correlation. Next and finally I will measure the correlation RMSE for the two sets of real returns, and the two sets of random returns.
If I consider the ratio [RMSE real data / RMSE random data] (both for next one year); then the median of this over a few thousand randomly selected trading strategy components is 1.06 for Sharpe Ratios, and for correlations around 5.6.
In simple terms,
we are a little bit worse than forecasting Sharpe Ratios in real data one year ahead than we would be with random data, but a LOT
worse with correlations.
Partly this is because we are pretty terrible at forecasting SR one year ahead anyway even with a stable underlying distribution; we don't do much worse with real data. However it does seem that correlations are far more unstable in reality than in randomly generated data. Note that these are correlations for trading strategy component returns. In some cases they are mathematically related (eg EWMAC of different speeds) and could be derived with some assumptions, a pencil, and a napkin. They are certainly more stable than the returns of the underlying instruments themselves (think about the changing correlation of stocks and bonds in different inflation environments).
(Note: These numbers are about the same for five years ahead and also ten years ahead)
If we recall from the prior post that the optimal shrinkage is zero on correlations with random data; we can now see why with actual data we'd probably want to opt for some correlation shrinkage; purely because the sampling error is much larger in practice. That is the empirical finding of the
EPO paper
. It does feel a bit weird since up to now my gut feeling has been that we have to shrink means a lot because they are much harder to forecast and because they have an outsized effect on portfolio weights compared to differences in correlation. Whilst the latter is still true it seems the former is not.
Food for though. Anyway the next step is to repeat the 'Ultimate Fitting Championships' battle, but this time with real data.
Posted by
Rob Carver
at
15:44
No comments:
Email This
BlogThis!
Share to X
Share to Facebook
Share to Pinterest
Labels:
correlation
,
fitting
,
forecasts
,
Portfolio optimization
,
prediction
,
Random data
,
Sharpe ratio
,
Uncertainty
Newer Posts
Older Posts
Home
Subscribe to:
Posts (Atom)