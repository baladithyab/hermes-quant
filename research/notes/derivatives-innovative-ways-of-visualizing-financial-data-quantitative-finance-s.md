---
title: derivatives - Innovative ways of visualizing financial data - Quantitative
  Finance Stack Exchange
id: derivatives-innovative-ways-of-visualizing-financial-data-quantitative-finance-s
tags:
- minimum-track-record statistics
created: '2026-06-17T20:05:57.707712Z'
updated: '2026-06-17T20:28:22.911643Z'
source: https://quant.stackexchange.com/questions/1365/what-is-the-minimum-length-of-track-record-for-a-sharpe-ratio
source_domain: quant.stackexchange.com
fetched_at: '2026-06-17T20:05:57.707521Z'
fetch_provider: builtin
status: review
type: note
tier: practitioner
content_type: forum
deprecated: false
summary: derivatives - Innovative ways of visualizing financial data - Quantitative
  Finance Stack Exchange
---

derivatives - Innovative ways of visualizing financial data - Quantitative Finance Stack Exchange
Stack Internal
Knowledge at work
Bring the best of human thought and AI automation together at your work.
Explore Stack Internal
Innovative ways of visualizing financial data
Ask Question
Asked
15 years, 4 months ago
Modified
3 years, 6 months ago
Viewed
23k times
98
$\begingroup$
Finance is drowning in a deluge of data. Humans are not very good at comprehending large amounts of data. One way out may be visualization.
Traditional ways of visualizing patterns, complexities and contexts are of course charts and for derivatives e.g. payoff diagrams, a more modern approach are
heat maps
.
My question:
Do you know of any innovative (or experimental) ways of visualizing financial and/or derivatives data?
data
derivatives
visualization
Share
Improve this question
Follow
asked
Feb 18, 2011 at 10:43
vonjd
28.2k
11
11 gold badges
107
107 silver badges
173
173 bronze badges
$\endgroup$
6
2
$\begingroup$
Do you accept to extend your question also to "audio visualization"?
$\endgroup$
Beer4All
–
Beer4All
2011-12-08 13:13:28 +00:00
Commented
Dec 8, 2011 at 13:13
$\begingroup$
@Beer4All: What do you mean by extending it to that topic? What has audio to do with finance?
$\endgroup$
vonjd
–
vonjd
2011-12-09 16:15:37 +00:00
Commented
Dec 9, 2011 at 16:15
3
$\begingroup$
@vonjd -- Victor Neiderhoffer, developed a means of "playing" audio of market moves for his trading desk.  I have a colleague, that creates audio files of fractal market structures.  He can play an entire day's tick by tick volatility.  It just provides a different way to experience the data.  Such things, like the things this great question has evoked from the community, may can shift the way we think about things (notice a theme in my various comments across the sight?) and keep us fresh.
$\endgroup$
Jagra
–
Jagra
2013-07-16 16:21:49 +00:00
Commented
Jul 16, 2013 at 16:21
$\begingroup$
@Jagra: If you made an answer out of this comment I would definitely vote it up! Thank you for sharing.
$\endgroup$
vonjd
–
vonjd
2013-07-16 16:39:27 +00:00
Commented
Jul 16, 2013 at 16:39
1
$\begingroup$
live.bionictrader.io/bionic-trader
<-- 3D live market viz.
$\endgroup$
P i
–
P i
2020-02-14 08:36:34 +00:00
Commented
Feb 14, 2020 at 8:36
|
Show
1
more comment
14 Answers
14
Sorted by:
Reset to default
Highest score (default)
Date modified (newest first)
Date created (oldest first)
66
$\begingroup$
Visualization should lead to truth and understanding.  As such, I find that simple visualizations tend to be the best.  My favorite visualization for showing relationships is the
scatterplot
.  Once you start to even introduce a line plot, you are implying continuities between data that may not exist.  And trying to introduce more advanced visualizations like network diagrams (
ex
) or complicated pie charts (
ex
) can lead to more confusion than understanding if misapplied.
A few thoughts:
I think that you have already mentioned a few good ones.  Heatmaps are good because they allow you to show three (or more) dimensional data without the added issues that arise when trying to create a 3D visualization.  Payoff diagrams are simple but they accomplish their goal efficiently as a result.
The
FinViz website
has a few nice examples of visualizations, including a simple
bar chart
,
candlesticks
, and
heatmap
.
People often don't consider that it is possible to include more dimensions in a typical plot by changing the width, size, color, or intensity of a shape.  This is a much better idea than trying to plot more than 2 dimensions spatially.
The fourth real dimension is time, and time plays a very important role in financial data.  One popular way to incorporate this as another dimension in a visualization is through
video
.  A great example is
gapminder
, the software created by Hans Rosling, which made for some very compelling
TED talks
about global poverty.  This was acquired by google and is now available as part of
their web toolkit
(also mentioned by
Ben Hoffstein
).
Visualization techniques from other fields are still very appropriate in finance, and the best starting point is
Edward Tufte
, especially
"The Visual Display of Quantitative Information"
and
"Envisioning Information"
.  You also can get a benefit from learning a visualization language.  I recommend any of these three (in order of complexity):
R with
ggplot2
, (
plotly
now provide an easy way to make ggplot graphes interactive)
Protovis
Processing
These each have a learning curve, but once you learn how to use them they all allow for
exploratory data analysis
in a way that can't be achieved with other tools.
There are also many great and innovative commercial tools.  To mention a few that are all used by banks and hedge funds:
Panopticon
does an amazing job with real-time visualization.
Tableau
,
Spotfire
, and
Qlikview
all allow for interactive visualization of data using in-memory databases.
Share
Improve this answer
Follow
edited
Dec 6, 2022 at 9:28
Community
Bot
1
answered
Feb 18, 2011 at 15:01
Shane
9,405
4
4 gold badges
55
55 silver badges
57
57 bronze badges
$\endgroup$
7
$\begingroup$
Shane,great resources you list. Do you know the kind of technology that can be used to integrate these visualization into a webpage?
$\endgroup$
Andy Nguyen
–
Andy Nguyen
2011-02-19 00:07:45 +00:00
Commented
Feb 19, 2011 at 0:07
$\begingroup$
@Andy Which are you talking about specifically?  Protovis is a javascript library, so that's straight forward.  Processing is Java so that can be used from the web in any number of different ways.  R doesn't have a specific web framework yet, but there are solutions (such as calling it from a foreign language interface).  The commercial tools all have server versions.
$\endgroup$
Shane
–
Shane
2011-02-19 00:26:31 +00:00
Commented
Feb 19, 2011 at 0:26
$\begingroup$
Shane, i was asking along those line. Which tools use which web technology and requiring which data format. For example, I'm personally interested in exposing data we have on XML, JSON format and create an interactive visualization of them and basically put anywhere on a website.
$\endgroup$
Andy Nguyen
–
Andy Nguyen
2011-02-19 00:41:24 +00:00
Commented
Feb 19, 2011 at 0:41
$\begingroup$
@Andy For interactive web visualization, I find either (a) you use one of the commercial tools (which I believe can all read XML/JSON data) or (b) expect to put in a fair amount of effort.  Another option is the google visualization toolkit mentioned below.  Using either Protovis or Processing can result in amazing stuff, but they are both very low level, so expect to get your hands dirty.  ggplot isn't really intended for that kind of usage (IMO).  One of the vendors might provide it to you for free with some advertising (try Tableau first:
tableausoftware.com/products/digital
).
$\endgroup$
Shane
–
Shane
2011-02-19 01:10:22 +00:00
Commented
Feb 19, 2011 at 1:10
$\begingroup$
@shane, thanks. I have been dabbling in with Exhibit frame work (see
simile-widgets.org
) and currently use Datapress from the MIT team (we basically the biggest client of their tool). You can see it here (
quantnet.com/program-selector
). I always look for ways to present data in a more attractive way and at the same time provides easy way to manage for the owner.
$\endgroup$
Andy Nguyen
–
Andy Nguyen
2011-02-19 05:34:32 +00:00
Commented
Feb 19, 2011 at 5:34
|
Show
2
more comments
26
$\begingroup$
The
Google Motion Chart
is a particularly elegant visualization for 'replaying' time series data. There is also an
R package
to interface with it.
Share
Improve this answer
Follow
answered
Feb 18, 2011 at 23:05
Ben Hoffstein
361
2
2 silver badges
5
5 bronze badges
$\endgroup$
Add a comment
|
24
$\begingroup$
Nanex has an interesting way of showing the order-book:
The following images show CME's emMni future (S&P 500) depth of book and trades. The images are rainbow (ROYGBIV) color coded by the relative size at each depth level. Red indicates a lot of size, violet indicates size approaching 0. Note that a full minute before each event, the depth starts cooling rapidly. The volume of contracts traded is represented at the bottom of the chart.
More info here:
http://www.nanex.net/Research/EMini1/Emini1.html
Share
Improve this answer
Follow
answered
Jun 28, 2011 at 14:58
Meh
1,203
9
9 silver badges
9
9 bronze badges
$\endgroup$
Add a comment
|
13
$\begingroup$
Shane's advice is good.  I think it's worth adding the following two techniques not already mentioned:
Self-Organizing Maps
(SOMs)
Seriation
(pdf pertaining to R package
seriation
, but great intro to the topic).
They are not explicit visualize techniques,
per se
.  Instead, they are algos that transform underlying data in ways that aim to lead to greater/new insight on the underlying data.  Thus, in my mind, the above approaches share the common objective with xy plots, contour plots, scatter-plot matrices, heat maps, etc.
For strict quantitative visualization, Tufte, as mentioned above, a great place to start.  Personally, I've gotten more out of Wong's, "The Wall Street Journal Guide to Information Graphics" and Janert's "Data Analysis with Open Source Tools".  However, keep in mind that each have different audiences and objectives in mind.
I also believe Processing (mentioned by Shane) has a very bright future in finance - it's been used heavily by multimedia artists primarily because of its relative ease and great flexibility.
Share
Improve this answer
Follow
answered
Feb 19, 2011 at 16:22
ZAxisMapping
1,164
8
8 silver badges
11
11 bronze badges
$\endgroup$
Add a comment
|
11
$\begingroup$
Although quite simple connected scatterplots can give interesting new insights on how time series perform together:
http://steveharoz.com/research/connected_scatterplot/
As an example: Gold vs. S&P 500 from 1970 till today:
The green point marks 1970, the red point is today. Every point is a year, moving vertically upwards means rise in the S&P 500 without gold changing, moving horizontally to the right vice versa. The diagonal line would be perfect correlation.
On the given website are many example to play with and there is an accompanying paper which can be downloaded (pdf) free of charge.
Share
Improve this answer
Follow
answered
Nov 25, 2015 at 15:23
vonjd
28.2k
11
11 gold badges
107
107 silver badges
173
173 bronze badges
$\endgroup$
Add a comment
|
9
$\begingroup$
And then music...
Victor Neiderhoffer, in a 2001
interview
:
The market plays music all the time. The problem is you never know how the music of the market is going to end. But a good framework is that it will end on the tonic. Consonance to dissonance back to consonance. And whenever there's tremendous dissonance, strident moves in one direction, a good working hypothesis is that at the end, you'll find consonance again.
Mean reversion?
A 2007
New Yorker
article on Neiderhoffer discussed some more of his music and markets ideas:
In “The Education of a Speculator,” he devotes an entire chapter to this notion, comparing the market’s movements to some of his favorite pieces of classical music, and juxtaposing pages of sheet music with stock charts. “When the markets are moving in my favor in a nice, gentle way—never below my initial price—I often think of the ‘Trout Quintet,’ ” he writes. “Another frequent work I hear in the market is Haydn’s Symphony No. 94. . . . Right after lunch, or before a holiday, the markets have a tendency to meander up and down in a five-point range above and below the opening. The pattern is similar to the twinkling C-major fifths of Haydn’s symphony.”
At some point, Neiderhoffer developed a means of "playing" audio of market moves for his trading desk. (I'll try to find a link to the source, its been a while since I saw it.
More interestingly...
I have a colleague, that creates audio files of fractal market structures. He can play an entire day's tick by tick volatility. It just provides a different way to experience the data. Such things, like the things this great question has evoked from the community, may shift the way we think about things and keep us fresh.
If anyone wants to listen to some short clips, contact me by my email, posted in my profile, and I'll send a few.
Share
Improve this answer
Follow
answered
Jul 16, 2013 at 20:03
Jagra
591
5
5 silver badges
14
14 bronze badges
$\endgroup$
Add a comment
|
9
$\begingroup$
To me, coloring by data value is a great way to bring applications alive.
If traditional ways are not enough, probably taking 3D in use would be a way:
And of course 2D heatmap is a very handy for sure.
I'm developing data visualization software components with 3D technologies, so definitely all feedback and ideas are welcome :-)
Share
Improve this answer
Follow
answered
Jan 6, 2014 at 21:20
Pasi Tuomainen
233
2
2 silver badges
7
7 bronze badges
$\endgroup$
Add a comment
|
8
$\begingroup$
Here are a few recent examples:
https://stackoverflow.com/questions/4951193/find-largest-5-value-less-than-1-lowest-5-values
http://tables2graphs.com/doku.php?id=04_regression_coefficients#figure_6
http://tables2graphs.com/doku.php?id=03_descriptive_statistics#figure_5
http://chartporn.org/category/innovative/
Share
Improve this answer
Follow
edited
May 23, 2017 at 12:41
Community
Bot
1
answered
Feb 18, 2011 at 19:33
bill_080
3,874
1
1 gold badge
21
21 silver badges
20
20 bronze badges
$\endgroup$
Add a comment
|
6
$\begingroup$
There are many price driven financial data finsualization concepts are available such as candle stick stock charts. However, there is an advanced charting concept, Mano Stick which is supply & demand driven charting concept. Mano Stick is a multidimentional charting concept which is able to display price information along with volume information to show trend changes in early statge.
Visit
http://www.manostick.com/share-price-analysis.html
. And see mano Stick at
http://www.manostick.com/Images/MS-volume.png
"manostick"
Share
Improve this answer
Follow
answered
Jun 25, 2011 at 17:56
mano Siluvairajah
61
1
1 silver badge
1
1 bronze badge
$\endgroup$
1
6
$\begingroup$
You should point-out that you are affiliated with the service you're mentioning. You know, potential conflict of interest and all.
$\endgroup$
chrisaycock
–
chrisaycock
2011-06-26 04:40:03 +00:00
Commented
Jun 26, 2011 at 4:40
Add a comment
|
2
$\begingroup$
Great question, I love to visualize data! A visualization is really the most efficient way to display a large amount of information to be processed by the human brain IMO. Depending on what exactly you are trying to plot and visualize, I would suggest trying the javascript API for WebGL called Three.js. Examples of Three.js are here:
http://threejs.org/examples/
For example, I have created the following 3D Contour graphs with Three.js displaying a specific indicator (LPPL) for a time series here:
https://lpplmarketwatch.com/3d-contour-examples/
I have brainstormed ways, unsuccessfully, to visualize the millions of traders interacting in a network - buying and selling, shorting, etc... It would be cool if anyone had any ideas on how to visualize a network with millions of nodes interacting in a trading exchange with Three.js -- perhaps with Bitcoin for a practical example, since I imagine most trade information is confidential or expensive.
Share
Improve this answer
Follow
edited
Mar 16, 2015 at 13:32
answered
Mar 16, 2015 at 13:25
Solar Anamnesis
96
1
1 silver badge
5
5 bronze badges
$\endgroup$
4
$\begingroup$
+1: threejs is really impressive! I would encourage you to create a new question on your idea about visualizing interacting traders in a network. Perhaps somebody has already done something along those lines.
$\endgroup$
vonjd
–
vonjd
2015-03-16 13:53:57 +00:00
Commented
Mar 16, 2015 at 13:53
$\begingroup$
@Taylor, have you  tried reactive programming? It reacts in real time , scales well (multicore, multi processor), it is responsive to software and harware failures and can be used on arbitrary data events/data lakes
$\endgroup$
user7056
–
user7056
2015-04-21 07:41:26 +00:00
Commented
Apr 21, 2015 at 7:41
$\begingroup$
@user7056 No I have not tried. what did you have in mind?
$\endgroup$
Solar Anamnesis
–
Solar Anamnesis
2015-04-25 20:00:17 +00:00
Commented
Apr 25, 2015 at 20:00
$\begingroup$
@Taylor   There are some détails here:
class.coursera.org/reactive-002/lecture/3
$\endgroup$
user7056
–
user7056
2015-04-26 22:15:36 +00:00
Commented
Apr 26, 2015 at 22:15
Add a comment
|
1
$\begingroup$
download gnuplot
better then matlab , R and has almost every thing you will need
It can also do everything mentioned in the other posts, and even visualize data in real time, at no cost as its open source and offers output to almost any format you want even LaTex for your thesis.
Share
Improve this answer
Follow
edited
Dec 8, 2011 at 6:38
answered
Dec 8, 2011 at 6:11
pyCthon
2,284
2
2 gold badges
19
19 silver badges
40
40 bronze badges
$\endgroup$
2
3
$\begingroup$
Hello, and welcome to the site. You might notice that many of the answers here are pretty detailed. Your answer right now is a one-line opinion piece rather than a full response to the question. Consider this as a benchmark: What impression would this answer give your professor if he had asked this question in class?
$\endgroup$
chrisaycock
–
chrisaycock
2011-12-08 06:16:43 +00:00
Commented
Dec 8, 2011 at 6:16
1
$\begingroup$
glad you like it
$\endgroup$
pyCthon
–
pyCthon
2011-12-13 02:01:28 +00:00
Commented
Dec 13, 2011 at 2:01
Add a comment
|
1
$\begingroup$
BitListen
-- "Realtime Bitcoin transaction and trade visualizer" is pretty neat.
Share
Improve this answer
Follow
answered
Oct 24, 2013 at 0:27
zcopley
37
2
2 bronze badges
$\endgroup$
Add a comment
|
1
$\begingroup$
LOESS diagrams show the payoff structure of any investment vehicle in relation to a benchmark (i.e. "underlying"). Even complicated trading strategies of (hedge) funds or ETFs become accessible this way:
I published a blog post with some background information, fully documented R-code and many examples:
Financial X-Rays: Dissect any Price Series with a simple Payoff Diagram
Share
Improve this answer
Follow
answered
Jun 9, 2021 at 13:35
vonjd
28.2k
11
11 gold badges
107
107 silver badges
173
173 bronze badges
$\endgroup$
Add a comment
|
0
$\begingroup$
Chernoff faces are also a good and undervalued idea
http://en.wikipedia.org/wiki/Chernoff_face
. It is applicable to multivariate statistical data, which, I beleive, occur frequently in world of finance/
Share
Improve this answer
Follow
answered
Oct 24, 2013 at 5:02
Rustam
682
4
4 silver badges
11
11 bronze badges
$\endgroup$
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
data
derivatives
visualization
See similar questions with these tags.
Featured on Meta
Native Ads Coming To Comments
Related
11
How to "uncluster" a set of financial data?
8
New ways of communicating risk
96
Building Financial Data Time Series Database from scratch
2
Different ways to express a 2s10s steepener?
2
Downloading historical data of "Financial Account Data"
Hot Network Questions
Wheeled cart tips over instead of climbing over a bump
The nonlinear Schrödinger equation and the Hamilton-Jacobi equation
How to write a scene with sword training (2 people) supervised by a 3rd person?
Does the half-minotaur template truly add Str and Con (and remove Dex) via size changes?
Short story - probably by Greg Egan - laborers sing "Dehumanize Yourself"
Do Large creatures automatically have Reach?
How can I rotate a face to align with the axis?
What are various Buddhist responses to boredom?
Is this weakening of the definition of a group equivalent to the definition of a group?
What kind of hexagonal antenna is this?
Hat Game with 2 prisoners
Help identifying this square connector with 10 contacts?
Electrical Current Traveling Down a Curve?
Would a tidally locked planet have a higher or lower amount of photosynthesis compared to a planet like Earth?
Is it still best to stick with UTC times after the introduction of <chrono>?
How to sculpt heavy, compressed leather folds on a boot?
How to include section template defined heading prefix in TOC?
Predictors with a spike at zero or semi continuous variable
What is the point of making a timeline for your world?
Do FreeBSD jails allow running older version of the base system?
To ensure a smooth experience on REDMI Note 15 Pro+
Early-morning (pre-6 AM) accessible route from Caumartin to Gare du Nord with luggage
Homotopy spaces of relative maps
Significant figures: reliably know digits + 1 estimated digit?
more hot questions
Question feed
default