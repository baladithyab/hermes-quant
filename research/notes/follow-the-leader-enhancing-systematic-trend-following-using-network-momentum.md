---
title: 'Follow the Leader: Enhancing Systematic Trend-Following Using Network Momentum'
id: follow-the-leader-enhancing-systematic-trend-following-using-network-momentum
tags:
- exit-strategy
created: '2026-06-17T20:07:07.405968Z'
updated: '2026-06-17T20:28:22.975443Z'
source: https://arxiv.org/html/2501.07135
source_domain: arxiv.org
fetched_at: '2026-06-17T20:07:07.405767Z'
fetch_provider: builtin
status: review
type: note
tier: institutional
content_type: paper
deprecated: false
summary: 'Follow the Leader: Enhancing Systematic Trend-Following Using Network Momentum'
---

Follow the Leader: Enhancing Systematic Trend-Following Using Network Momentum
Follow the Leader: Enhancing Systematic Trend-Following Using Network Momentum
Linze Li
Imperial College London, UK
linze.li20@imperial.ac.uk
&William Ferreira
william.ferreira.14@alumni.ucl.ac.uk
Abstract
We present a systematic, trend-following strategy, applied to commodity futures markets, that combines univariate trend indicators with cross-sectional trend indicators that capture so-called
momentum spillover
, which can occur when there is a lead-lag relationship between the trending behaviour of different markets. Our strategy utilises two methods for detecting lead-lag relationships, with a method for computing
network momentum
, to produce a novel trend-following indicator. We use our new trend indicator to construct a portfolio whose performance we compare to a baseline model which uses only univariate indicators, and demonstrate statistically significant improvements in Sharpe ratio, skewness of returns, and downside performance, using synthetic bootstrapped data samples taken from time-series of actual prices.
1
Introduction
Trend-following is a well-known and popular strategy in finance
[
1
]
. The basic premise of trend-following is that price direction exhibits persistence: assets whose prices have been rising tend to continue to rise (and conversely assets with falling prices tend to continue to fall); therefore, an investor may realise profits by buying (resp. selling) assets whose price has recently risen (resp. fallen) in the expectation that the price trend will continue. One of the characteristic and desirable features of trend-following strategies is the positive skew in the distribution of returns: regular small losses are compensated by fewer, but much larger gains.
The persistence of market returns has been extensively studied from macroeconomic perspectives. For instance, industrial growth rates have a significant impact on momentum profits
[
2
]
; investor behaviours, such as delayed information reception and asynchronous response timings, support the slow information diffusion hypothesis
[
3
,
4
]
. Behavioural biases like conservatism may also encourage premature selling or prolonged holding of assets
[
5
]
. Such persistence in market returns continues until significant deviations from price fundamentals eventually trigger a market reversal
[
6
]
. The profitability of time series momentum strategies is demonstrated across various markets, showing that purchasing stocks that perform well in recent months and selling those that show poor returns results in higher profits
[
7
,
8
,
9
]
. This profitability has been rigorously validated through statistical experiments to confirm it is not due to random chance
[
10
,
1
]
.
Price momentum extends beyond individual markets.
[
11
]
observes that high equity returns in one year can predict high corporate bond returns the following year despite bonds lacking inherent momentum. This ‘cross-sectional momentum spillover’ is primarily due to the bond market’s delayed reaction to equity performance, known as the ‘lead-lag effect’. Previous literature has identified multiple drivers for the lead-lag effect, including factors such as company size
[
12
]
, institutional ownership levels
[
13
]
, analyst coverage
[
14
]
, and industry affiliation
[
15
,
16
]
. Numerous studies have explored systematic approaches to capturing the lead-lag effect. For instance,
[
17
]
utilised the difference in the cross-correlation function based on Pearson correlation, while
[
18
]
employed the signed normalised area under the curve of the cross-correlation function as an indicator. Further,
[
19
]
experimented with alternatives to Pearson correlation, such as Kendall rank correlation
[
20
]
, distance correlation
[
21
]
, and mutual information from discretised time series values
[
22
]
. For additional methods for detecting lead-lag effects, see
[
23
,
24
,
25
]
After computing lead-lag metrics pairwise, many studies suggest employing ranking algorithms to identify assets most likely to lead or follow
[
26
,
19
,
27
]
. Positions for the followers are established based on the average of the leaders’ performance. For example, if leaders exhibit a negative average return, followers are shorted in anticipation of a similar downward trend. To rank markets based on their lead-lag behaviour cross-sectionally, the literature employs methods such as RowSum Rank
[
26
,
28
,
19
]
, PageRank
[
18
,
29
]
, and machine learning approaches like the Learning-to-rank algorithm
[
30
,
31
]
. Another strategy involves clustering lead-lag metrics using algorithms such as clustering by industry sectors
[
4
,
23
]
, k-means
[
28
]
, and spectral and Hermitian clustering
[
19
,
26
]
. This data-driven methodology, prevalent in existing studies
[
29
,
18
]
, suggests establishing positions within lagging clusters based on the average performance of markets in the leading cluster. This approach is used to capitalise on trend-following opportunities or to construct opposite positions to counteract mean-reverting behaviours
[
26
]
.
To the best of our knowledge, quantitative research that measures the influence of leading markets on the performance of lagging markets is currently limited, especially when portfolios span multiple industries. It is pointed out in
[
32
]
that, although previous studies have identified momentum spillover across various sectors—such as equities and bonds
[
11
,
33
]
, equities and foreign currencies
[
34
]
, currency news and bonds in emerging markets
[
35
]
, and between crude oil indexes and equities
[
36
]
—the absence of firm-like economic and fundamental linkages in commodities markets complicates the identification of connections, such as those between orange juice and natural gas.
Moreover, while existing studies predominantly focus on statistically examining the momentum spillover effect and use it as a market selection mechanism for trading followers in exchanges, they fail to quantify and aggregate this momentum spillover into a new trading signal for portfolio construction. To bridge this gap, the existing literature suggests leveraging network theory. For instance,
[
37
]
uses edge centrality to quantify the importance of supplier-customer relationships, and
[
32
]
explores the ‘network momentum’ spillover across industries, aggregating momentum from commodities, equities, bonds, and currencies to create a novel trading signal. Specifically, the latter research employs ideas from graph learning, treating each market as a node within a graph. This approach uses time series momentum features, including moving average crossover signals and exponentially weighted returns, as signal processes for each node. They then solve a convex optimisation problem to approximate the weighted graph adjacency matrix. The edges of this graph elucidate the complex relationships across markets, with the magnitude of each edge reflecting the strength of similarity in momentum features between market pairs. Following this model, the time series momentum of other markets is weighted and used in a linear regression to predict the next day’s returns. Subsequently, a portfolio is constructed by assigning binary positions:
+
1
1
+1
+ 1
for a long position or
−
1
1
-1
- 1
for a short position, based on the sign of the predicted future returns
[
28
,
30
,
31
]
. However, such binary betting on positions based on momentum direction may not be optimal because this approach could lead to a discontinuous model, losing both convexity and positive skewness in returns
[
38
]
. Similarly,
[
32
]
notes that their strategy may not adequately address risk characteristics, potentially increasing exposure to significant downside movements.
Our work makes three contributions: first, it combines multiple lead-lag detection mechanisms as an ensemble model for price trend detection. Second, it uses the outputs of the ensemble model as a low-dimension feature space to learn a graph-adjacency matrix, to generate a network momentum indicator. Third, our network momentum indicator is used to construct a realistic portfolio for trend-following commodity futures that is statistically significantly superior to a baseline model that utilises only univariate trend indicators, when compared using bootstrapped price trajectories sampled from real historical price data.
The remainder of the paper is organised as follows. Chapter
2
presents the frameworks used to identify the lead-lag relationship. Chapter
3
introduces our approach to constructing the network momentum matrix. Chapter
4
explains the strategy setup. Chapter
5
explains the construction of our portfolio based on the momentum signals, and the performance analysis is reported in Chapter
6
. We conclude the paper with final thoughts and future directions in Chapter
7
, and provide auxiliary data in the Appendix.
2
Lead-lag matrix construction
We employ two recently proposed methods
to identify and quantify the lead-lag relationship between pairs of assets. The first method, based on the Lévy area of pairwise market returns
[
26
]
, identifies both linear and nonlinear relationships at a fixed lag length, for example a one day lag. The second model utilises the dynamic time warping (DTW) algorithm
[
39
]
on pairwise market returns, as presented in
[
28
,
40
]
. The DTW model relaxes the fixed lag assumption and adeptly handles non-synchronised time series of varying lengths. Building upon these foundations, we explore various advanced DTW algorithms to capture the co-movement between two markets’ returns. We then calculate pairwise lead-lag scores for all combinations of market pairs and use them to construct the lead-lag matrix, which
contains values indicating the direction of each market’s lead against another. The lead-lag matrix is skew-symmetric since its transpose equals its negative – this can be seen from if market
m
𝑚
m
italic_m
leads market
n
𝑛
n
italic_n
by a lag of
ℓ
ℓ
\ell
roman_ℓ
where
ℓ
ℓ
\ell
roman_ℓ
has the same unit as the time, then market
n
𝑛
n
italic_n
leads market
m
𝑚
m
italic_m
by a lag of
−
ℓ
ℓ
-\ell
- roman_ℓ
.
2.1
Signature based Lévy area
The first lead-lag model applied is based on the Lévy area enclosed by two time-series, which can be used to identify both linear and non-linear lead-lag relationships between them. To understand the concept of the Lévy area, it is necessary to introduce the concept of
path
; for a full discussion of paths the reader is referred to
[
41
,
42
]
. A
path
is a continuous function
X
𝑋
X
italic_X
on an interval
[
a
,
b
]
𝑎
𝑏
[a,b]
[ italic_a , italic_b ]
, such that
X
:
[
a
,
b
]
→
ℝ
n
:
𝑋
→
𝑎
𝑏
superscript
ℝ
𝑛
X:[a,b]\rightarrow\mathbb{R}^{n}
italic_X : [ italic_a , italic_b ] → blackboard_R start_POSTSUPERSCRIPT italic_n end_POSTSUPERSCRIPT
. Assuming that
[
a
,
b
]
𝑎
𝑏
[a,b]
[ italic_a , italic_b ]
represents an interval of time, we write
X
t
subscript
𝑋
𝑡
X_{t}
italic_X start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT
for
X
⁢
(
t
)
𝑋
𝑡
X(t)
italic_X ( italic_t )
, the value of
X
𝑋
X
italic_X
at time
t
∈
[
a
,
b
]
𝑡
𝑎
𝑏
t\in[a,b]
italic_t ∈ [ italic_a , italic_b ]
; when
n
=
2
𝑛
2
n=2
italic_n = 2
we write
X
t
=
{
X
t
1
,
X
t
2
}
subscript
𝑋
𝑡
subscript
superscript
𝑋
1
𝑡
subscript
superscript
𝑋
2
𝑡
X_{t}=\{X^{1}_{t},X^{2}_{t}\}
italic_X start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT = { italic_X start_POSTSUPERSCRIPT 1 end_POSTSUPERSCRIPT start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT , italic_X start_POSTSUPERSCRIPT 2 end_POSTSUPERSCRIPT start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT }
. We can view the time-series of returns of two assets as following a two-dimensional path, where
(
X
t
1
,
X
t
2
)
subscript
superscript
𝑋
1
𝑡
subscript
superscript
𝑋
2
𝑡
(X^{1}_{t},X^{2}_{t})
( italic_X start_POSTSUPERSCRIPT 1 end_POSTSUPERSCRIPT start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT , italic_X start_POSTSUPERSCRIPT 2 end_POSTSUPERSCRIPT start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT )
represents the returns at time
t
𝑡
t
italic_t
of asset 1 and asset 2 respectively. Let
S
⁢
(
X
)
a
,
t
i
,
j
𝑆
subscript
superscript
𝑋
𝑖
𝑗
𝑎
𝑡
S(X)^{i,j}_{a,t}
italic_S ( italic_X ) start_POSTSUPERSCRIPT italic_i , italic_j end_POSTSUPERSCRIPT start_POSTSUBSCRIPT italic_a , italic_t end_POSTSUBSCRIPT
1
1
1
S
⁢
(
X
)
i
,
j
𝑆
superscript
𝑋
𝑖
𝑗
S(X)^{i,j}
italic_S ( italic_X ) start_POSTSUPERSCRIPT italic_i , italic_j end_POSTSUPERSCRIPT
is the 2nd-level
signature
of the path
be defined as:
S
⁢
(
X
)
a
,
t
i
,
j
=
∫
a
<
r
<
s
<
t
𝑑
X
r
i
⁢
𝑑
X
s
j
𝑆
subscript
superscript
𝑋
𝑖
𝑗
𝑎
𝑡
subscript
𝑎
𝑟
𝑠
𝑡
differential-d
subscript
superscript
𝑋
𝑖
𝑟
differential-d
subscript
superscript
𝑋
𝑗
𝑠
S(X)^{i,j}_{a,t}=\int_{a<r<s<t}dX^{i}_{r}dX^{j}_{s}
italic_S ( italic_X ) start_POSTSUPERSCRIPT italic_i , italic_j end_POSTSUPERSCRIPT start_POSTSUBSCRIPT italic_a , italic_t end_POSTSUBSCRIPT = ∫ start_POSTSUBSCRIPT italic_a < italic_r < italic_s < italic_t end_POSTSUBSCRIPT italic_d italic_X start_POSTSUPERSCRIPT italic_i end_POSTSUPERSCRIPT start_POSTSUBSCRIPT italic_r end_POSTSUBSCRIPT italic_d italic_X start_POSTSUPERSCRIPT italic_j end_POSTSUPERSCRIPT start_POSTSUBSCRIPT italic_s end_POSTSUBSCRIPT
which can be used to define the Lévy area,
A
Lévy
superscript
𝐴
Lévy
A^{\text{L\'{e}vy}}
italic_A start_POSTSUPERSCRIPT Lévy end_POSTSUPERSCRIPT
, enclosed by path
{
X
t
1
,
X
t
2
}
subscript
superscript
𝑋
1
𝑡
subscript
superscript
𝑋
2
𝑡
\{X^{1}_{t},X^{2}_{t}\}
{ italic_X start_POSTSUPERSCRIPT 1 end_POSTSUPERSCRIPT start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT , italic_X start_POSTSUPERSCRIPT 2 end_POSTSUPERSCRIPT start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT }
:
A
1
,
2
Lévy
=
1
2
⁢
(
S
⁢
(
X
)
a
,
b
1
,
2
−
S
⁢
(
X
)
a
,
b
2
,
1
)
superscript
subscript
𝐴
1
2
Lévy
1
2
𝑆
subscript
superscript
𝑋
1
2
𝑎
𝑏
𝑆
subscript
superscript
𝑋
2
1
𝑎
𝑏
A_{1,2}^{\text{L\'{e}vy}}=\frac{1}{2}\left(S(X)^{1,2}_{a,b}-S(X)^{2,1}_{a,b}\right)
italic_A start_POSTSUBSCRIPT 1 , 2 end_POSTSUBSCRIPT start_POSTSUPERSCRIPT Lévy end_POSTSUPERSCRIPT = divide start_ARG 1 end_ARG start_ARG 2 end_ARG ( italic_S ( italic_X ) start_POSTSUPERSCRIPT 1 , 2 end_POSTSUPERSCRIPT start_POSTSUBSCRIPT italic_a , italic_b end_POSTSUBSCRIPT - italic_S ( italic_X ) start_POSTSUPERSCRIPT 2 , 1 end_POSTSUPERSCRIPT start_POSTSUBSCRIPT italic_a , italic_b end_POSTSUBSCRIPT )
If an increase (resp. decrease) in the
X
1
superscript
𝑋
1
X^{1}
italic_X start_POSTSUPERSCRIPT 1 end_POSTSUPERSCRIPT
is followed by an increase (resp. decrease) in the
X
2
superscript
𝑋
2
X^{2}
italic_X start_POSTSUPERSCRIPT 2 end_POSTSUPERSCRIPT
, then the Lévy area enclosed by the two series and the chord connecting the two ends, is positive. Conversely, if the movements of
X
1
superscript
𝑋
1
X^{1}
italic_X start_POSTSUPERSCRIPT 1 end_POSTSUPERSCRIPT
and
X
2
superscript
𝑋
2
X^{2}
italic_X start_POSTSUPERSCRIPT 2 end_POSTSUPERSCRIPT
are in opposite directions, then the Lévy area is negative. For discrete processes
X
t
1
superscript
subscript
𝑋
𝑡
1
X_{t}^{1}
italic_X start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT start_POSTSUPERSCRIPT 1 end_POSTSUPERSCRIPT
and
X
t
2
superscript
subscript
𝑋
𝑡
2
X_{t}^{2}
italic_X start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT start_POSTSUPERSCRIPT 2 end_POSTSUPERSCRIPT
, the Lévy area can be expressed as:
A
1
,
2
Lévy
:=
1
2
⁢
(
∑
a
<
s
<
b
(
−
X
s
i
⁢
X
s
−
1
j
+
X
s
j
⁢
X
s
−
1
i
)
+
X
a
i
⁢
(
X
a
j
−
X
b
j
)
+
X
a
j
⁢
(
X
b
i
−
X
a
i
)
)
.
assign
superscript
subscript
𝐴
1
2
Lévy
1
2
subscript
𝑎
𝑠
𝑏
superscript
subscript
𝑋
𝑠
𝑖
superscript
subscript
𝑋
𝑠
1
𝑗
superscript
subscript
𝑋
𝑠
𝑗
superscript
subscript
𝑋
𝑠
1
𝑖
superscript
subscript
𝑋
𝑎
𝑖
superscript
subscript
𝑋
𝑎
𝑗
superscript
subscript
𝑋
𝑏
𝑗
superscript
subscript
𝑋
𝑎
𝑗
superscript
subscript
𝑋
𝑏
𝑖
superscript
subscript
𝑋
𝑎
𝑖
A_{1,2}^{\text{L\'{e}vy}}:=\frac{1}{2}\left(\sum_{a<s<b}(-X_{s}^{i}X_{s-1}^{j}%
+X_{s}^{j}X_{s-1}^{i})+X_{a}^{i}(X_{a}^{j}-X_{b}^{j})+X_{a}^{j}(X_{b}^{i}-X_{a%
}^{i})\right).
italic_A start_POSTSUBSCRIPT 1 , 2 end_POSTSUBSCRIPT start_POSTSUPERSCRIPT Lévy end_POSTSUPERSCRIPT := divide start_ARG 1 end_ARG start_ARG 2 end_ARG ( ∑ start_POSTSUBSCRIPT italic_a < italic_s < italic_b end_POSTSUBSCRIPT ( - italic_X start_POSTSUBSCRIPT italic_s end_POSTSUBSCRIPT start_POSTSUPERSCRIPT italic_i end_POSTSUPERSCRIPT italic_X start_POSTSUBSCRIPT italic_s - 1 end_POSTSUBSCRIPT start_POSTSUPERSCRIPT italic_j end_POSTSUPERSCRIPT + italic_X start_POSTSUBSCRIPT italic_s end_POSTSUBSCRIPT start_POSTSUPERSCRIPT italic_j end_POSTSUPERSCRIPT italic_X start_POSTSUBSCRIPT italic_s - 1 end_POSTSUBSCRIPT start_POSTSUPERSCRIPT italic_i end_POSTSUPERSCRIPT ) + italic_X start_POSTSUBSCRIPT italic_a end_POSTSUBSCRIPT start_POSTSUPERSCRIPT italic_i end_POSTSUPERSCRIPT ( italic_X start_POSTSUBSCRIPT italic_a end_POSTSUBSCRIPT start_POSTSUPERSCRIPT italic_j end_POSTSUPERSCRIPT - italic_X start_POSTSUBSCRIPT italic_b end_POSTSUBSCRIPT start_POSTSUPERSCRIPT italic_j end_POSTSUPERSCRIPT ) + italic_X start_POSTSUBSCRIPT italic_a end_POSTSUBSCRIPT start_POSTSUPERSCRIPT italic_j end_POSTSUPERSCRIPT ( italic_X start_POSTSUBSCRIPT italic_b end_POSTSUBSCRIPT start_POSTSUPERSCRIPT italic_i end_POSTSUPERSCRIPT - italic_X start_POSTSUBSCRIPT italic_a end_POSTSUBSCRIPT start_POSTSUPERSCRIPT italic_i end_POSTSUPERSCRIPT ) ) .
(1)
Let
r
~
n
,
t
subscript
~
𝑟
𝑛
𝑡
\tilde{r}_{n,t}
over~ start_ARG italic_r end_ARG start_POSTSUBSCRIPT italic_n , italic_t end_POSTSUBSCRIPT
denote the
standardised
return of asset
n
𝑛
n
italic_n
at time
t
𝑡
t
italic_t
, i.e. the return on the asset normalised to have zero mean and unit variance, and suppose the functional form of the relationship between the standardised returns on asset
m
𝑚
m
italic_m
and asset
n
𝑛
n
italic_n
is given by:
r
~
n
,
t
=
β
ℓ
⁢
f
⁢
(
r
~
m
,
t
−
ℓ
)
+
ϵ
t
,
subscript
~
𝑟
𝑛
𝑡
subscript
𝛽
ℓ
𝑓
subscript
~
𝑟
𝑚
𝑡
ℓ
subscript
italic-ϵ
𝑡
\tilde{r}_{n,t}=\beta_{\ell}f(\tilde{r}_{m,t-\ell})+\epsilon_{t},
over~ start_ARG italic_r end_ARG start_POSTSUBSCRIPT italic_n , italic_t end_POSTSUBSCRIPT = italic_β start_POSTSUBSCRIPT roman_ℓ end_POSTSUBSCRIPT italic_f ( over~ start_ARG italic_r end_ARG start_POSTSUBSCRIPT italic_m , italic_t - roman_ℓ end_POSTSUBSCRIPT ) + italic_ϵ start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT ,
(2)
where
t
−
ℓ
𝑡
ℓ
t-\ell
italic_t - roman_ℓ
represents
ℓ
ℓ
\ell
roman_ℓ
time-steps prior to
t
𝑡
t
italic_t
, and
f
𝑓
f
italic_f
is any continuous function, then the following result
[
26
, Theorem 1, page 9]
links (
2
) to the Lévy area:
Theorem
.
[
26
, Theorem 1, page 9]
Assume
{
X
τ
i
}
τ
=
s
t
superscript
subscript
subscript
superscript
𝑋
𝑖
𝜏
𝜏
𝑠
𝑡
\{X^{i}_{\tau}\}_{\tau=s}^{t}
{ italic_X start_POSTSUPERSCRIPT italic_i end_POSTSUPERSCRIPT start_POSTSUBSCRIPT italic_τ end_POSTSUBSCRIPT } start_POSTSUBSCRIPT italic_τ = italic_s end_POSTSUBSCRIPT start_POSTSUPERSCRIPT italic_t end_POSTSUPERSCRIPT
and
{
X
τ
j
}
τ
=
s
t
superscript
subscript
subscript
superscript
𝑋
𝑗
𝜏
𝜏
𝑠
𝑡
\{X^{j}_{\tau}\}_{\tau=s}^{t}
{ italic_X start_POSTSUPERSCRIPT italic_j end_POSTSUPERSCRIPT start_POSTSUBSCRIPT italic_τ end_POSTSUBSCRIPT } start_POSTSUBSCRIPT italic_τ = italic_s end_POSTSUBSCRIPT start_POSTSUPERSCRIPT italic_t end_POSTSUPERSCRIPT
are two independent random processes with zero mean, unit variance, and symmetric distribution, and both satisfy (
2
) over a time interval [s,t]. Then, the sign of the Lévy area
A
i
,
j
Lévy
superscript
subscript
𝐴
𝑖
𝑗
Lévy
A_{i,j}^{\text{L\'{e}vy}}
italic_A start_POSTSUBSCRIPT italic_i , italic_j end_POSTSUBSCRIPT start_POSTSUPERSCRIPT Lévy end_POSTSUPERSCRIPT
between
X
τ
i
subscript
superscript
𝑋
𝑖
𝜏
X^{i}_{\tau}
italic_X start_POSTSUPERSCRIPT italic_i end_POSTSUPERSCRIPT start_POSTSUBSCRIPT italic_τ end_POSTSUBSCRIPT
and
X
τ
j
subscript
superscript
𝑋
𝑗
𝜏
X^{j}_{\tau}
italic_X start_POSTSUPERSCRIPT italic_j end_POSTSUPERSCRIPT start_POSTSUBSCRIPT italic_τ end_POSTSUBSCRIPT
is the same as the sign of
ℓ
ℓ
\ell
roman_ℓ
if and only if
ℓ
=
±
1
ℓ
plus-or-minus
1
\ell=\pm 1
roman_ℓ = ± 1
. In addition, if
ℓ
=
±
1
ℓ
plus-or-minus
1
\ell=\pm 1
roman_ℓ = ± 1
and the third derivative of the function
f
𝑓
f
italic_f
exists, there is a constant
K
𝐾
K
italic_K
such that for all pairs (
i
𝑖
i
italic_i
,
j
𝑗
j
italic_j
), we have
E
⁢
[
A
i
,
j
Lévy
−
K
⁢
β
ℓ
]
=
M
6
⁢
β
ℓ
⁢
E
⁢
[
∑
s
<
a
<
t
f
′′′
⁢
(
ξ
a
−
1
j
)
⁢
(
X
a
−
1
j
)
4
]
𝐸
delimited-[]
superscript
subscript
𝐴
𝑖
𝑗
Lévy
𝐾
subscript
𝛽
ℓ
𝑀
6
subscript
𝛽
ℓ
𝐸
delimited-[]
subscript
𝑠
𝑎
𝑡
superscript
𝑓
′′′
superscript
subscript
𝜉
𝑎
1
𝑗
superscript
superscript
subscript
𝑋
𝑎
1
𝑗
4
E[A_{i,j}^{\text{L\'{e}vy}}-K\beta_{\ell}]=\frac{M}{6}\beta_{\ell}E[\sum_{s<a<%
t}f^{{}^{\prime\prime\prime}}(\xi_{a-1}^{j})(X_{a-1}^{j})^{4}]
italic_E [ italic_A start_POSTSUBSCRIPT italic_i , italic_j end_POSTSUBSCRIPT start_POSTSUPERSCRIPT Lévy end_POSTSUPERSCRIPT - italic_K italic_β start_POSTSUBSCRIPT roman_ℓ end_POSTSUBSCRIPT ] = divide start_ARG italic_M end_ARG start_ARG 6 end_ARG italic_β start_POSTSUBSCRIPT roman_ℓ end_POSTSUBSCRIPT italic_E [ ∑ start_POSTSUBSCRIPT italic_s < italic_a < italic_t end_POSTSUBSCRIPT italic_f start_POSTSUPERSCRIPT start_FLOATSUPERSCRIPT ′ ′ ′ end_FLOATSUPERSCRIPT end_POSTSUPERSCRIPT ( italic_ξ start_POSTSUBSCRIPT italic_a - 1 end_POSTSUBSCRIPT start_POSTSUPERSCRIPT italic_j end_POSTSUPERSCRIPT ) ( italic_X start_POSTSUBSCRIPT italic_a - 1 end_POSTSUBSCRIPT start_POSTSUPERSCRIPT italic_j end_POSTSUPERSCRIPT ) start_POSTSUPERSCRIPT 4 end_POSTSUPERSCRIPT ]
(3)
for some constant
M
𝑀
M
italic_M
and
|
ξ
a
−
1
j
|
<
|
X
a
−
1
j
|
superscript
subscript
𝜉
𝑎
1
𝑗
superscript
subscript
𝑋
𝑎
1
𝑗
|\xi_{a-1}^{j}|<|X_{a-1}^{j}|
| italic_ξ start_POSTSUBSCRIPT italic_a - 1 end_POSTSUBSCRIPT start_POSTSUPERSCRIPT italic_j end_POSTSUPERSCRIPT | < | italic_X start_POSTSUBSCRIPT italic_a - 1 end_POSTSUBSCRIPT start_POSTSUPERSCRIPT italic_j end_POSTSUPERSCRIPT |
.
Thus, we may use the Lévy area to detect lead-lag relationships in asset returns.
2.2
Dynamic Time Warping
The second lead-lag model applied is based on dynamic time warping. Compared to the signature-based Lévy area model proposed in Section
2.1
, this model differs in two major respects: firstly, it does not assume a prefixed lag
ℓ
ℓ
\ell
roman_ℓ
between two market return series; instead, the alignment between the two series is dynamically determined by the algorithm. Secondly, this model can handle non-synchronised paired series, thus making the analysis of markets with different lengths of return series feasible. We contend that the variation in matched indices from the two paired series
can identify the leader and the follower within these pairs.
The classical dynamic time warping (DTW) model effectively identifies the lead-lag relationship as shown in
[
28
,
40
]
. Suppose
X
𝑋
X
italic_X
and
Y
𝑌
Y
italic_Y
are two time series with lengths
m
𝑚
m
italic_m
and
n
𝑛
n
italic_n
, respectively:
X
=
X
1
,
X
2
,
…
,
X
m
,
Y
=
Y
1
,
Y
2
,
…
,
Y
n
,
formulae-sequence
𝑋
subscript
𝑋
1
subscript
𝑋
2
…
subscript
𝑋
𝑚
𝑌
subscript
𝑌
1
subscript
𝑌
2
…
subscript
𝑌
𝑛
X=X_{1},X_{2},\ldots,X_{m},\quad Y=Y_{1},Y_{2},\ldots,Y_{n},
italic_X = italic_X start_POSTSUBSCRIPT 1 end_POSTSUBSCRIPT , italic_X start_POSTSUBSCRIPT 2 end_POSTSUBSCRIPT , … , italic_X start_POSTSUBSCRIPT italic_m end_POSTSUBSCRIPT , italic_Y = italic_Y start_POSTSUBSCRIPT 1 end_POSTSUBSCRIPT , italic_Y start_POSTSUBSCRIPT 2 end_POSTSUBSCRIPT , … , italic_Y start_POSTSUBSCRIPT italic_n end_POSTSUBSCRIPT ,
DTW creates a
warping path
,
𝒲
⁢
{
X
,
Y
}
𝒲
𝑋
𝑌
\mathcal{W}\{X,Y\}
caligraphic_W { italic_X , italic_Y }
defined by:
𝒲
⁢
{
X
,
Y
}
=
{
w
1
,
⋯
,
w
n
,
⋯
,
w
L
}
max
⁡
(
m
,
n
)
≤
L
≤
m
+
n
−
1
.
formulae-sequence
𝒲
𝑋
𝑌
subscript
𝑤
1
⋯
subscript
𝑤
𝑛
⋯
subscript
𝑤
𝐿
max
𝑚
𝑛
𝐿
𝑚
𝑛
1
\mathcal{W}\{X,Y\}=\{w_{1},\cdots,w_{n},\cdots,w_{L}\}\quad\operatorname{max}(%
m,n)\leq L\leq m+n-1.
caligraphic_W { italic_X , italic_Y } = { italic_w start_POSTSUBSCRIPT 1 end_POSTSUBSCRIPT , ⋯ , italic_w start_POSTSUBSCRIPT italic_n end_POSTSUBSCRIPT , ⋯ , italic_w start_POSTSUBSCRIPT italic_L end_POSTSUBSCRIPT } roman_max ( italic_m , italic_n ) ≤ italic_L ≤ italic_m + italic_n - 1 .
(4)
where each element
w
k
=
(
i
,
j
)
k
subscript
𝑤
𝑘
subscript
𝑖
𝑗
𝑘
w_{k}=(i,j)_{k}
italic_w start_POSTSUBSCRIPT italic_k end_POSTSUBSCRIPT = ( italic_i , italic_j ) start_POSTSUBSCRIPT italic_k end_POSTSUBSCRIPT
indicating that, at the
k
th
superscript
𝑘
th
k^{\text{th}}
italic_k start_POSTSUPERSCRIPT th end_POSTSUPERSCRIPT
step in the warping path,
X
i
subscript
𝑋
𝑖
X_{i}
italic_X start_POSTSUBSCRIPT italic_i end_POSTSUBSCRIPT
should be aligned with
Y
j
subscript
𝑌
𝑗
Y_{j}
italic_Y start_POSTSUBSCRIPT italic_j end_POSTSUBSCRIPT
. This warping path is constructed with three essential constraints:
•
Boundary conditions:
w
1
=
(
1
,
1
)
subscript
𝑤
1
1
1
w_{1}=(1,1)
italic_w start_POSTSUBSCRIPT 1 end_POSTSUBSCRIPT = ( 1 , 1 )
and
w
L
=
(
m
,
n
)
subscript
𝑤
𝐿
𝑚
𝑛
w_{L}=(m,n)
italic_w start_POSTSUBSCRIPT italic_L end_POSTSUBSCRIPT = ( italic_m , italic_n )
. This ensures that the first and last elements in
X
𝑋
X
italic_X
and
Y
𝑌
Y
italic_Y
are matched respectively. This boundary condition can, however, be partly relaxed. We refer interested readers to
[
43
]
for details.
•
Monotonicity condition: Given
w
l
=
(
i
l
,
j
l
)
subscript
𝑤
𝑙
subscript
𝑖
𝑙
subscript
𝑗
𝑙
w_{l}=(i_{l},j_{l})
italic_w start_POSTSUBSCRIPT italic_l end_POSTSUBSCRIPT = ( italic_i start_POSTSUBSCRIPT italic_l end_POSTSUBSCRIPT , italic_j start_POSTSUBSCRIPT italic_l end_POSTSUBSCRIPT )
for
l
∈
[
1
,
⋯
,
L
]
𝑙
1
⋯
𝐿
l\in[1,\cdots,L]
italic_l ∈ [ 1 , ⋯ , italic_L ]
,
i
1
≤
i
2
≤
⋯
≤
i
L
subscript
𝑖
1
subscript
𝑖
2
⋯
subscript
𝑖
𝐿
i_{1}\leq i_{2}\leq\cdots\leq i_{L}
italic_i start_POSTSUBSCRIPT 1 end_POSTSUBSCRIPT ≤ italic_i start_POSTSUBSCRIPT 2 end_POSTSUBSCRIPT ≤ ⋯ ≤ italic_i start_POSTSUBSCRIPT italic_L end_POSTSUBSCRIPT
and
j
1
≤
j
2
≤
⋯
≤
j
L
subscript
𝑗
1
subscript
𝑗
2
⋯
subscript
𝑗
𝐿
j_{1}\leq j_{2}\leq\cdots\leq j_{L}
italic_j start_POSTSUBSCRIPT 1 end_POSTSUBSCRIPT ≤ italic_j start_POSTSUBSCRIPT 2 end_POSTSUBSCRIPT ≤ ⋯ ≤ italic_j start_POSTSUBSCRIPT italic_L end_POSTSUBSCRIPT
. This condition enforces the mapping in chronological order.
•
Step size condition:
w
l
+
1
−
w
l
∈
{
(
1
,
0
)
,
(
0
,
1
)
,
(
1
,
1
)
}
⁢
for
⁢
l
∈
[
1
,
…
,
L
−
1
]
subscript
𝑤
𝑙
1
subscript
𝑤
𝑙
1
0
0
1
1
1
for
𝑙
1
…
𝐿
1
w_{l+1}-w_{l}\in\{(1,0),(0,1),(1,1)\}\text{ for }l\in[1,\ldots,L-1]
italic_w start_POSTSUBSCRIPT italic_l + 1 end_POSTSUBSCRIPT - italic_w start_POSTSUBSCRIPT italic_l end_POSTSUBSCRIPT ∈ { ( 1 , 0 ) , ( 0 , 1 ) , ( 1 , 1 ) } for italic_l ∈ [ 1 , … , italic_L - 1 ]
. This is sometimes called the continuity constraint. It allows only adjacent cell transitions within the warping path.
The cost of a warping path
𝒲
⁢
{
X
,
Y
}
𝒲
𝑋
𝑌
\mathcal{W}\{X,Y\}
caligraphic_W { italic_X , italic_Y }
is defined as
c
𝒲
⁢
(
X
,
Y
)
:=
∑
l
=
1
L
c
local
⁢
(
X
i
l
,
Y
j
l
)
,
assign
subscript
𝑐
𝒲
𝑋
𝑌
superscript
subscript
𝑙
1
𝐿
subscript
𝑐
local
subscript
𝑋
subscript
𝑖
𝑙
subscript
𝑌
subscript
𝑗
𝑙
c_{\mathcal{W}}(X,Y):=\sum_{l=1}^{L}c_{\text{local}}(X_{i_{l}},Y_{j_{l}}),
italic_c start_POSTSUBSCRIPT caligraphic_W end_POSTSUBSCRIPT ( italic_X , italic_Y ) := ∑ start_POSTSUBSCRIPT italic_l = 1 end_POSTSUBSCRIPT start_POSTSUPERSCRIPT italic_L end_POSTSUPERSCRIPT italic_c start_POSTSUBSCRIPT local end_POSTSUBSCRIPT ( italic_X start_POSTSUBSCRIPT italic_i start_POSTSUBSCRIPT italic_l end_POSTSUBSCRIPT end_POSTSUBSCRIPT , italic_Y start_POSTSUBSCRIPT italic_j start_POSTSUBSCRIPT italic_l end_POSTSUBSCRIPT end_POSTSUBSCRIPT ) ,
where
c
local
subscript
𝑐
local
c_{\text{local}}
italic_c start_POSTSUBSCRIPT local end_POSTSUBSCRIPT
is a local cost measure measuring the dissimilarity between two points. Typically, this is chosen as the Euclidean distance
[
28
,
40
]
.
Among the various warping paths, the optimal path
𝒲
∗
superscript
𝒲
\mathcal{W}^{*}
caligraphic_W start_POSTSUPERSCRIPT ∗ end_POSTSUPERSCRIPT
, which is also the best alignment, follows a path of minimal cost through
c
𝒲
⁢
(
X
,
Y
)
subscript
𝑐
𝒲
𝑋
𝑌
c_{\mathcal{W}}(X,Y)
italic_c start_POSTSUBSCRIPT caligraphic_W end_POSTSUBSCRIPT ( italic_X , italic_Y )
. The DTW distance between
X
𝑋
X
italic_X
and
Y
𝑌
Y
italic_Y
is then defined as
DTW
⁢
(
X
,
Y
)
DTW
X
Y
\displaystyle\operatorname{DTW(X,Y)}
roman_DTW ( roman_X , roman_Y )
:=
min
⁡
{
c
𝒲
⁢
(
X
,
Y
)
|
𝒲
⁢
is a warping path satisfying necessary constraints
}
.
assign
absent
min
conditional
subscript
𝑐
𝒲
𝑋
𝑌
𝒲
is a warping path satisfying necessary constraints
\displaystyle:=\operatorname{min}\{c_{\mathcal{W}}(X,Y)|\mathcal{W}~{}\text{is%
 a warping path satisfying necessary constraints}\}.
:= roman_min { italic_c start_POSTSUBSCRIPT caligraphic_W end_POSTSUBSCRIPT ( italic_X , italic_Y ) | caligraphic_W is a warping path satisfying necessary constraints } .
The optimal warping path can be computed by a dynamic programming algorithm
[
44
]
.
In
[
45
]
it mentions that DTW aligns two series solely on their coordinate values. It faces difficulties when the two time sequences have similar local shapes but differ in their values – a single point on one time series is mapped to several points on the other time series. This undesirable behaviour is referred to as ‘singularities’. According to
[
45
, page 2]
, this occurs because the algorithm may attempt to account for variability in the Y-axis by warping the X-axis. Although one could, and in fact should, as stated by
[
46
]
, always perform Z-normalisation to convert the time sequences to a common and comparable scale with a mean of 0 and a standard deviation of 1, this approach does not resolve the alignment issue. Consider the scenario where a point
X
i
subscript
𝑋
𝑖
X_{i}
italic_X start_POSTSUBSCRIPT italic_i end_POSTSUBSCRIPT
from sequence
X
𝑋
X
italic_X
has an identical value to
Y
j
subscript
𝑌
𝑗
Y_{j}
italic_Y start_POSTSUBSCRIPT italic_j end_POSTSUBSCRIPT
from sequence
Y
𝑌
Y
italic_Y
, yet the neighbourhood of
X
i
subscript
𝑋
𝑖
X_{i}
italic_X start_POSTSUBSCRIPT italic_i end_POSTSUBSCRIPT
is in a rising trend while the neighbourhood of
Y
j
subscript
𝑌
𝑗
Y_{j}
italic_Y start_POSTSUBSCRIPT italic_j end_POSTSUBSCRIPT
is in a falling trend. DTW may map these points onto one another to achieve minimal overall cost.
To address this problem,
[
45
]
modifies DTW, denoted as DDTW. Instead of finding the optimal warping based on the raw values of the sequences, we consider the estimated local derivative of the sequence. The derivative of points on sequence
X
𝑋
X
italic_X
is estimated by the following equations
D
X
⁢
[
X
n
]
=
(
X
n
−
X
n
−
1
)
+
(
(
X
n
+
1
−
X
n
−
1
)
/
2
)
2
⁢
, with
⁢
n
∈
[
2
,
⋯
,
m
−
1
]
,
subscript
𝐷
𝑋
delimited-[]
subscript
𝑋
𝑛
subscript
𝑋
𝑛
subscript
𝑋
𝑛
1
subscript
𝑋
𝑛
1
subscript
𝑋
𝑛
1
2
2
, with
𝑛
2
⋯
𝑚
1
\displaystyle D_{X}[X_{n}]=\frac{(X_{n}-X_{n-1})+((X_{n+1}-X_{n-1})/2)}{2}%
\text{, with }n\in[2,\cdots,m-1],
italic_D start_POSTSUBSCRIPT italic_X end_POSTSUBSCRIPT [ italic_X start_POSTSUBSCRIPT italic_n end_POSTSUBSCRIPT ] = divide start_ARG ( italic_X start_POSTSUBSCRIPT italic_n end_POSTSUBSCRIPT - italic_X start_POSTSUBSCRIPT italic_n - 1 end_POSTSUBSCRIPT ) + ( ( italic_X start_POSTSUBSCRIPT italic_n + 1 end_POSTSUBSCRIPT - italic_X start_POSTSUBSCRIPT italic_n - 1 end_POSTSUBSCRIPT ) / 2 ) end_ARG start_ARG 2 end_ARG , with italic_n ∈ [ 2 , ⋯ , italic_m - 1 ] ,
(5)
D
X
⁢
[
X
1
]
=
D
X
⁢
[
X
2
]
,
and
subscript
𝐷
𝑋
delimited-[]
subscript
𝑋
1
subscript
𝐷
𝑋
delimited-[]
subscript
𝑋
2
and
\displaystyle D_{X}[X_{1}]=D_{X}[X_{2}],\text{ and}
italic_D start_POSTSUBSCRIPT italic_X end_POSTSUBSCRIPT [ italic_X start_POSTSUBSCRIPT 1 end_POSTSUBSCRIPT ] = italic_D start_POSTSUBSCRIPT italic_X end_POSTSUBSCRIPT [ italic_X start_POSTSUBSCRIPT 2 end_POSTSUBSCRIPT ] , and
D
X
⁢
[
X
m
]
=
D
X
⁢
[
X
m
−
1
]
.
subscript
𝐷
𝑋
delimited-[]
subscript
𝑋
𝑚
subscript
𝐷
𝑋
delimited-[]
subscript
𝑋
𝑚
1
\displaystyle D_{X}[X_{m}]=D_{X}[X_{m}-1].
italic_D start_POSTSUBSCRIPT italic_X end_POSTSUBSCRIPT [ italic_X start_POSTSUBSCRIPT italic_m end_POSTSUBSCRIPT ] = italic_D start_POSTSUBSCRIPT italic_X end_POSTSUBSCRIPT [ italic_X start_POSTSUBSCRIPT italic_m end_POSTSUBSCRIPT - 1 ] .
This is effectively the mean value between the slope of the line from the left neighbour to the point and the slope of the line from the left neighbour to the right neighbour.
[
45
]
suggests that after replacing the original sequence with the estimated derivative, the following procedure is the same as the classical dynamic time warping algorithm.
While DDTW considers the slope of the time series, it only considers the slope within a local neighbourhood, failing to consider the global features. An improvement was proposed in
[
47
]
by dealing with multidimensional time series to account for both global features and local shapes, known as shape dynamic time warping (shapeDTW). The intuitive idea is to convert a one-dimensional time series
X
=
(
X
1
,
X
2
,
…
,
X
m
)
𝑋
subscript
𝑋
1
subscript
𝑋
2
…
subscript
𝑋
𝑚
X=(X_{1},X_{2},\ldots,X_{m})
italic_X = ( italic_X start_POSTSUBSCRIPT 1 end_POSTSUBSCRIPT , italic_X start_POSTSUBSCRIPT 2 end_POSTSUBSCRIPT , … , italic_X start_POSTSUBSCRIPT italic_m end_POSTSUBSCRIPT )
of length
m
𝑚
m
italic_m
into a multidimensional series
𝒟
=
(
d
1
,
⋯
,
d
m
)
∈
ℝ
m
×
l
𝒟
subscript
𝑑
1
⋯
subscript
𝑑
𝑚
superscript
ℝ
𝑚
𝑙
\mathcal{D}=(d_{1},\cdots,d_{m})\in\mathbb{R}^{m\times l}
caligraphic_D = ( italic_d start_POSTSUBSCRIPT 1 end_POSTSUBSCRIPT , ⋯ , italic_d start_POSTSUBSCRIPT italic_m end_POSTSUBSCRIPT ) ∈ blackboard_R start_POSTSUPERSCRIPT italic_m × italic_l end_POSTSUPERSCRIPT
, where each subsequence
d
i
subscript
𝑑
𝑖
d_{i}
italic_d start_POSTSUBSCRIPT italic_i end_POSTSUBSCRIPT
of length
l
𝑙
l
italic_l
embeds the information of the point
X
i
subscript
𝑋
𝑖
X_{i}
italic_X start_POSTSUBSCRIPT italic_i end_POSTSUBSCRIPT
. Each subsequence
d
i
subscript
𝑑
𝑖
d_{i}
italic_d start_POSTSUBSCRIPT italic_i end_POSTSUBSCRIPT
can be constructed in one of two ways: either by directly taking the
l
𝑙
l
italic_l
values centred around the temporal point
X
i
subscript
𝑋
𝑖
X_{i}
italic_X start_POSTSUBSCRIPT italic_i end_POSTSUBSCRIPT
to capture local shape information, or by applying the derivative operation (as in DDTW) to these values to emphasise local trends—this derivative-based approach is known as shapeDDTW. The dependent multidimensional DTW algorithm proposed in
[
48
]
is then applied to the two multidimensional series to calculate the distance cost and determine the optimal warping path.
Following the convention in
[
28
]
, dynamic time warping can be used to detect the lag between two paths,
X
𝑋
X
italic_X
and
Y
𝑌
Y
italic_Y
, by taking the mode of the differences between index pairs in the warping path
𝒲
⁢
{
X
,
Y
}
𝒲
𝑋
𝑌
\mathcal{W}\{X,Y\}
caligraphic_W { italic_X , italic_Y }
. Specifically, the lag is given by
Mode
⁡
(
Δ
⁢
𝒲
⁢
(
X
,
Y
)
)
Mode
Δ
𝒲
𝑋
𝑌
\operatorname{Mode}(\Delta\mathcal{W}(X,Y))
roman_Mode ( roman_Δ caligraphic_W ( italic_X , italic_Y ) )
, where
Δ
⁢
𝒲
⁢
{
X
,
Y
}
=
{
Δ
⁢
w
1
,
…
,
Δ
⁢
w
k
}
Δ
𝒲
𝑋
𝑌
Δ
subscript
𝑤
1
…
Δ
subscript
𝑤
𝑘
\Delta\mathcal{W}\{X,Y\}=\{\Delta w_{1},\ldots,\Delta w_{k}\}
roman_Δ caligraphic_W { italic_X , italic_Y } = { roman_Δ italic_w start_POSTSUBSCRIPT 1 end_POSTSUBSCRIPT , … , roman_Δ italic_w start_POSTSUBSCRIPT italic_k end_POSTSUBSCRIPT }
, and each
Δ
⁢
w
k
Δ
subscript
𝑤
𝑘
\Delta w_{k}
roman_Δ italic_w start_POSTSUBSCRIPT italic_k end_POSTSUBSCRIPT
represents the difference between the indices
i
𝑖
i
italic_i
and
j
𝑗
j
italic_j
in the
k
𝑘
k
italic_k
-th warping pair, i.e.,
Δ
⁢
w
k
=
j
−
i
Δ
subscript
𝑤
𝑘
𝑗
𝑖
\Delta w_{k}=j-i
roman_Δ italic_w start_POSTSUBSCRIPT italic_k end_POSTSUBSCRIPT = italic_j - italic_i
.
3
From cross-sectional to network
We introduce a method to convert the pairwise lead-lag relationship into a ‘network momentum’. This network aims to capture the interconnected cross-sectional momentum between paired markets. The skew-symmetric lead-lag matrix,
𝐕
∈
ℝ
M
×
M
𝐕
superscript
ℝ
𝑀
𝑀
\mathbf{V}\in\mathbb{R}^{M\times M}
bold_V ∈ blackboard_R start_POSTSUPERSCRIPT italic_M × italic_M end_POSTSUPERSCRIPT
, shows the directed lead-lag relationships in the market but has two limitations that prevent its direct use as network momentum.
First, the lead-lag matrix, obtained via dynamic time warping, contains only integer lags, i.e.,
𝐕
i
,
j
=
ℓ
i
,
j
∈
ℤ
subscript
𝐕
𝑖
𝑗
subscript
ℓ
𝑖
𝑗
ℤ
\mathbf{V}_{i,j}=\ell_{i,j}\in\mathbb{Z}
bold_V start_POSTSUBSCRIPT italic_i , italic_j end_POSTSUBSCRIPT = roman_ℓ start_POSTSUBSCRIPT italic_i , italic_j end_POSTSUBSCRIPT ∈ blackboard_Z
, which indicates that
r
~
i
,
t
−
ℓ
i
,
j
subscript
~
𝑟
𝑖
𝑡
subscript
ℓ
𝑖
𝑗
\tilde{r}_{i,t-\ell_{i,j}}
over~ start_ARG italic_r end_ARG start_POSTSUBSCRIPT italic_i , italic_t - roman_ℓ start_POSTSUBSCRIPT italic_i , italic_j end_POSTSUBSCRIPT end_POSTSUBSCRIPT
has some predictive power over
r
~
j
,
t
subscript
~
𝑟
𝑗
𝑡
\tilde{r}_{j,t}
over~ start_ARG italic_r end_ARG start_POSTSUBSCRIPT italic_j , italic_t end_POSTSUBSCRIPT
, but not its magnitude. While
[
26
]
suggests that the Lévy area can indicate the strength of a lead-lag relationship, both it and dynamic time warping face the second challenge:
𝐕
𝐕
\mathbf{V}
bold_V
is a dense matrix because every market
i
𝑖
i
italic_i
has a lead-lag relationship with every other market
j
𝑗
j
italic_j
.
An ideal network momentum matrix should be sparse, retaining only the most significant connections and reducing noise. To address this, we propose fitting a graph learning model to the lead-lag matrix to generate a sparse adjacency matrix. This adjacency matrix keeps only the important edges, replacing the integer lag from dynamic time warping with non-negative weights that quantify the connection strength. We refer to this adjacency matrix as the network momentum matrix. Our method contrasts with the approach proposed by
[
32
]
, where nodes in the graph are set with time-series momentum features, such as price information. In our case, each node represents a market and encodes its lead-lag relationships with other markets, reflecting the interconnected dynamics. We refer to this adjacency matrix as the network momentum matrix.
A non-parametric method to study the adjacency matrix is proposed in
[
49
, page 923]
, where they study the following convex optimisation problem to learn the adjacency matrix.
Definition 3.1
(Graph learning model)
.
[
49
, Page 923]
Given a smooth matrix
𝐗
∈
ℝ
N
×
p
𝐗
superscript
ℝ
𝑁
𝑝
\mathbf{X}\in\mathbb{R}^{N\times p}
bold_X ∈ blackboard_R start_POSTSUPERSCRIPT italic_N × italic_p end_POSTSUPERSCRIPT
on a graph
G
𝐺
G
italic_G
,
𝐃
∈
ℝ
+
N
×
N
𝐃
superscript
subscript
ℝ
𝑁
𝑁
\mathbf{D}\in\mathbb{R}_{+}^{N\times N}
bold_D ∈ blackboard_R start_POSTSUBSCRIPT + end_POSTSUBSCRIPT start_POSTSUPERSCRIPT italic_N × italic_N end_POSTSUPERSCRIPT
being the degree matrix of the graph, and sparsity parameters
α
>
0
𝛼
0
\alpha>0
italic_α > 0
and
β
≥
0
𝛽
0
\beta\geq 0
italic_β ≥ 0
, can be found by solving the following convex optimisation problem:
minimise
𝐀
tr
⁡
(
𝐗
T
⁢
(
𝐃
−
𝐀
)
⁢
𝐗
)
−
α
⁢
𝟏
T
⁢
log
⁡
(
𝐀𝟏
)
+
β
⁢
‖
𝐀
‖
F
2
subscript
minimise
𝐀
tr
superscript
𝐗
𝑇
𝐃
𝐀
𝐗
𝛼
superscript
1
𝑇
log
𝐀𝟏
𝛽
subscript
superscript
norm
𝐀
2
𝐹
\displaystyle\operatorname{minimise}_{\mathbf{A}}\quad\operatorname{tr}(%
\mathbf{X}^{T}(\mathbf{D}-\mathbf{A})\mathbf{X})-\alpha\mathbf{1}^{T}%
\operatorname{log}(\mathbf{A}\mathbf{1})+\beta\|\mathbf{A}\|^{2}_{F}
roman_minimise start_POSTSUBSCRIPT bold_A end_POSTSUBSCRIPT roman_tr ( bold_X start_POSTSUPERSCRIPT italic_T end_POSTSUPERSCRIPT ( bold_D - bold_A ) bold_X ) - italic_α bold_1 start_POSTSUPERSCRIPT italic_T end_POSTSUPERSCRIPT roman_log ( bold_A1 ) + italic_β ∥ bold_A ∥ start_POSTSUPERSCRIPT 2 end_POSTSUPERSCRIPT start_POSTSUBSCRIPT italic_F end_POSTSUBSCRIPT
such that
𝐀
i
,
j
=
𝐀
j
,
i
,
𝐀
i
,
j
≥
0
∀
i
≠
j
,
diag
⁡
(
𝐀
)
=
𝟎
,
formulae-sequence
such that
subscript
𝐀
𝑖
𝑗
subscript
𝐀
𝑗
𝑖
formulae-sequence
subscript
𝐀
𝑖
𝑗
0
formulae-sequence
for-all
𝑖
𝑗
diag
𝐀
0
\displaystyle\text{such that}\quad\mathbf{A}_{i,j}=\mathbf{A}_{j,i},\quad%
\mathbf{A}_{i,j}\geq 0\quad\forall i\neq j,\quad\operatorname{diag}(\mathbf{A}%
)=\mathbf{0},
such that bold_A start_POSTSUBSCRIPT italic_i , italic_j end_POSTSUBSCRIPT = bold_A start_POSTSUBSCRIPT italic_j , italic_i end_POSTSUBSCRIPT , bold_A start_POSTSUBSCRIPT italic_i , italic_j end_POSTSUBSCRIPT ≥ 0 ∀ italic_i ≠ italic_j , roman_diag ( bold_A ) = bold_0 ,
Here, a logarithmic penalty term is used to prevent isolated nodes so that every node has at least one connected neighbour, and the Frobenius norm is used to control sparsity, unlike the
ℓ
1
subscript
ℓ
1
\ell_{1}
roman_ℓ start_POSTSUBSCRIPT 1 end_POSTSUBSCRIPT
norm, it only penalises the edge with large magnitude without disproportionately affecting smaller edges. This model also allows us to control the sparsity of the graph by tuning the parameters
α
𝛼
\alpha
italic_α
and
β
𝛽
\beta
italic_β
, in general, as the parameters
α
𝛼
\alpha
italic_α
and
β
𝛽
\beta
italic_β
increase, a denser graph is obtained.
The lead-lag matrix changes each time we fit the algorithm to new price data, so we denote the lead-lag matrix obtained at trading time
t
𝑡
t
italic_t
as
𝐕
t
subscript
𝐕
𝑡
\mathbf{V}_{t}
bold_V start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT
. We replace the signal matrix
X
𝑋
X
italic_X
in the graph learning model defined in
3.1
with
𝐕
t
subscript
𝐕
𝑡
\mathbf{V}_{t}
bold_V start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT
to obtain an adjacency matrix with non-negative edge weights and no isolated markets. The edge values reflect the interconnected relationship of the leadingness of the markets.
It is suggested in
[
32
]
that to mitigate the effects of scale differences in constructing network momentum—arising from the variance in the number of connections some nodes have, with some connected to many other assets and others to only a few—a graph normalisation should be applied to the adjacency matrix
𝐀
t
subscript
𝐀
𝑡
\mathbf{A}_{t}
bold_A start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT
before using it to aggregate time series momentum. This normalisation is defined as follows:
𝐀
~
t
=
𝐃
t
−
1
/
2
⁢
𝐀
t
⁢
𝐃
t
−
1
/
2
subscript
~
𝐀
𝑡
superscript
subscript
𝐃
𝑡
1
2
subscript
𝐀
𝑡
superscript
subscript
𝐃
𝑡
1
2
\tilde{\mathbf{A}}_{t}=\mathbf{D}_{t}^{-1/2}\mathbf{A}_{t}\mathbf{D}_{t}^{-1/2}
over~ start_ARG bold_A end_ARG start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT = bold_D start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT start_POSTSUPERSCRIPT - 1 / 2 end_POSTSUPERSCRIPT bold_A start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT bold_D start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT start_POSTSUPERSCRIPT - 1 / 2 end_POSTSUPERSCRIPT
(6)
The empirical analysis in
[
32
]
suggests that combining
S
𝑆
S
italic_S
adjacency matrices obtained from different lead-lag matrices
𝐕
t
subscript
𝐕
𝑡
\mathbf{V}_{t}
bold_V start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT
based on historical price data with different lookback windows can improve performance. Therefore, we define the ensemble adjacency matrix
𝐀
¯
t
subscript
¯
𝐀
𝑡
\bar{\mathbf{A}}_{t}
over¯ start_ARG bold_A end_ARG start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT
as:
𝐀
¯
t
=
1
S
⁢
∑
s
=
1
S
𝐀
t
(
s
)
subscript
¯
𝐀
𝑡
1
𝑆
superscript
subscript
𝑠
1
𝑆
superscript
subscript
𝐀
𝑡
𝑠
\bar{\mathbf{A}}_{t}=\frac{1}{S}\sum_{s=1}^{S}\mathbf{A}_{t}^{(s)}
over¯ start_ARG bold_A end_ARG start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT = divide start_ARG 1 end_ARG start_ARG italic_S end_ARG ∑ start_POSTSUBSCRIPT italic_s = 1 end_POSTSUBSCRIPT start_POSTSUPERSCRIPT italic_S end_POSTSUPERSCRIPT bold_A start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT start_POSTSUPERSCRIPT ( italic_s ) end_POSTSUPERSCRIPT
(7)
and will compare the strategy performance between using and not using the ensemble mechanism.
We now summarise the algorithm for calculating the network momentum matrix in Table
1
. In practice, the optimisation problem in the graph learning model
3.1
is solved numerically with MOSEK and Python library CVXPY
[
50
]
.
Algorithm 1
Algorithm for Computing the Network Momentum Matrix Using Graph Learning
0:
Series of lead-lag matrices
{
𝐕
t
s
∈
ℝ
M
×
M
}
s
=
1
S
superscript
subscript
superscript
subscript
𝐕
𝑡
𝑠
superscript
ℝ
𝑀
𝑀
𝑠
1
𝑆
\{\mathbf{V}_{t}^{s}\in\mathbb{R}^{M\times M}\}_{s=1}^{S}
{ bold_V start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT start_POSTSUPERSCRIPT italic_s end_POSTSUPERSCRIPT ∈ blackboard_R start_POSTSUPERSCRIPT italic_M × italic_M end_POSTSUPERSCRIPT } start_POSTSUBSCRIPT italic_s = 1 end_POSTSUBSCRIPT start_POSTSUPERSCRIPT italic_S end_POSTSUPERSCRIPT
observed at trading time
t
𝑡
t
italic_t
, where
M
𝑀
M
italic_M
is the number of markets and
S
𝑆
S
italic_S
is the number of historical price data inputs,
S
≥
1
𝑆
1
S\geq 1
italic_S ≥ 1
.
0:
Hyperparameters
α
>
0
𝛼
0
\alpha>0
italic_α > 0
and
β
≥
0
𝛽
0
\beta\geq 0
italic_β ≥ 0
for sparsity control.
0:
Normalised network momentum matrix
𝐀
~
t
∈
ℝ
M
×
M
subscript
~
𝐀
𝑡
superscript
ℝ
𝑀
𝑀
\tilde{\mathbf{A}}_{t}\in\mathbb{R}^{M\times M}
over~ start_ARG bold_A end_ARG start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT ∈ blackboard_R start_POSTSUPERSCRIPT italic_M × italic_M end_POSTSUPERSCRIPT
.
1:
Initialise an ensemble adjacency matrix
𝐀
¯
t
subscript
¯
𝐀
𝑡
\bar{\mathbf{A}}_{t}
over¯ start_ARG bold_A end_ARG start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT
with zeros of shape
(
M
,
M
)
𝑀
𝑀
(M,M)
( italic_M , italic_M )
.
2:
for
s
=
1
𝑠
1
s=1
italic_s = 1
to
S
𝑆
S
italic_S
do
3:
Replace the signal matrix
X
𝑋
X
italic_X
in the graph learning model defined in
3.1
with
𝐕
t
s
superscript
subscript
𝐕
𝑡
𝑠
\mathbf{V}_{t}^{s}
bold_V start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT start_POSTSUPERSCRIPT italic_s end_POSTSUPERSCRIPT
to obtain the adjacency matrix
𝐀
t
s
superscript
subscript
𝐀
𝑡
𝑠
\mathbf{A}_{t}^{s}
bold_A start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT start_POSTSUPERSCRIPT italic_s end_POSTSUPERSCRIPT
.
4:
Update the ensemble adjacency matrix according to (
7
):
𝐀
¯
t
←
𝐀
¯
t
+
1
S
⁢
𝐀
t
s
←
subscript
¯
𝐀
𝑡
subscript
¯
𝐀
𝑡
1
𝑆
superscript
subscript
𝐀
𝑡
𝑠
\bar{\mathbf{A}}_{t}\leftarrow\bar{\mathbf{A}}_{t}+\frac{1}{S}\mathbf{A}_{t}^{s}
over¯ start_ARG bold_A end_ARG start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT ← over¯ start_ARG bold_A end_ARG start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT + divide start_ARG 1 end_ARG start_ARG italic_S end_ARG bold_A start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT start_POSTSUPERSCRIPT italic_s end_POSTSUPERSCRIPT
.
5:
end
for
6:
Normalise
𝐀
¯
t
subscript
¯
𝐀
𝑡
\bar{\mathbf{A}}_{t}
over¯ start_ARG bold_A end_ARG start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT
using the graph normalisation formula (
6
) to obtain
𝐀
~
t
subscript
~
𝐀
𝑡
\tilde{\mathbf{A}}_{t}
over~ start_ARG bold_A end_ARG start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT
.
7:
return
𝐀
~
t
subscript
~
𝐀
𝑡
\tilde{\mathbf{A}}_{t}
over~ start_ARG bold_A end_ARG start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT
Note: If
S
=
1
𝑆
1
S=1
italic_S = 1
, this algorithm is equivalent to not using the graph ensemble method, thereby directly applying normalisation to the single obtained adjacency matrix.
4
Methodology
4.1
Data
Our dataset contains the daily settlement price of 28 futures markets, ranging across sectors: agriculture, energy, metals and equity indices. The model training period spans from June 2002 to June 2024, with strategy performance evaluated on out-of-sample data from January 2005 to June 2024.
A complete list of the markets included in our portfolio can be found in Appendix
7.1
.
We are interested in comparing different variations of our models for detecting network momentum, to each other, and to a baseline model. To facilitate this comparison we use the stationary block bootstrap procedure
[
51
]
to generate a population set of price trajectories using the true market data. Stationary block bootstrapping preserves the auto- and cross-correlation of market returns, and so enables us to make statistical comparisons about the relative performance of the different models.
4.2
Set Up for Time Series Momentum Features
In this section, we present the construction of classical time series momentum features based on price information. For each market
m
𝑚
m
italic_m
and time index
t
=
1
,
…
,
T
𝑡
1
…
𝑇
t=1,\ldots,T
italic_t = 1 , … , italic_T
, we denote the price for market
m
𝑚
m
italic_m
at time
t
𝑡
t
italic_t
as
P
t
,
m
∈
ℝ
subscript
𝑃
𝑡
𝑚
ℝ
P_{t,m}\in\mathbb{R}
italic_P start_POSTSUBSCRIPT italic_t , italic_m end_POSTSUBSCRIPT ∈ blackboard_R
, and therefore the price time series for market
m
𝑚
m
italic_m
is denoted as
P
m
:=
(
P
1
,
m
,
P
2
,
m
,
…
,
P
T
,
m
)
∈
ℝ
T
.
assign
subscript
𝑃
𝑚
subscript
𝑃
1
𝑚
subscript
𝑃
2
𝑚
…
subscript
𝑃
𝑇
𝑚
superscript
ℝ
𝑇
P_{m}:=(P_{1,m},P_{2,m},\ldots,P_{T,m})\in\mathbb{R}^{T}.
italic_P start_POSTSUBSCRIPT italic_m end_POSTSUBSCRIPT := ( italic_P start_POSTSUBSCRIPT 1 , italic_m end_POSTSUBSCRIPT , italic_P start_POSTSUBSCRIPT 2 , italic_m end_POSTSUBSCRIPT , … , italic_P start_POSTSUBSCRIPT italic_T , italic_m end_POSTSUBSCRIPT ) ∈ blackboard_R start_POSTSUPERSCRIPT italic_T end_POSTSUPERSCRIPT .
Note that futures markets are made up of a continually expiring sequence of individual contracts, and so there is not a continuously observed uninterrupted price for a futures market. We construct a continuous price using individual contract prices, according the the
backward Panama canal method
[
52
]
, and a suitable choice of roll dates. The derived price series is known as the
backadjusted price
. We construct
𝐏
∈
ℝ
T
×
M
𝐏
superscript
ℝ
𝑇
𝑀
\mathbf{P}\in\mathbb{R}^{T\times M}
bold_P ∈ blackboard_R start_POSTSUPERSCRIPT italic_T × italic_M end_POSTSUPERSCRIPT
, representing a matrix of
M
𝑀
M
italic_M
market backadjusted prices across a time horizon of
T
𝑇
T
italic_T
, where each vector is a price time series for a market.
Definition 4.1
(Price delta)
.
Given a market
m
𝑚
m
italic_m
, we denote its price time series from time
t
=
1
𝑡
1
t=1
italic_t = 1
to
t
=
T
𝑡
𝑇
t=T
italic_t = italic_T
as
(
P
1
,
m
,
P
2
,
m
,
…
,
P
T
,
m
)
∈
ℝ
T
subscript
𝑃
1
𝑚
subscript
𝑃
2
𝑚
…
subscript
𝑃
𝑇
𝑚
superscript
ℝ
𝑇
(P_{1,m},P_{2,m},\ldots,P_{T,m})\in\mathbb{R}^{T}
( italic_P start_POSTSUBSCRIPT 1 , italic_m end_POSTSUBSCRIPT , italic_P start_POSTSUBSCRIPT 2 , italic_m end_POSTSUBSCRIPT , … , italic_P start_POSTSUBSCRIPT italic_T , italic_m end_POSTSUBSCRIPT ) ∈ blackboard_R start_POSTSUPERSCRIPT italic_T end_POSTSUPERSCRIPT
, the price delta for it at time
t
𝑡
t
italic_t
,
Δ
t
,
m
subscript
Δ
𝑡
𝑚
\Delta_{t,m}
roman_Δ start_POSTSUBSCRIPT italic_t , italic_m end_POSTSUBSCRIPT
, is defined as the first difference in its price series,
Δ
t
,
m
:=
P
t
,
m
−
P
t
−
1
,
m
.
assign
subscript
Δ
𝑡
𝑚
subscript
𝑃
𝑡
𝑚
subscript
𝑃
𝑡
1
𝑚
\Delta_{t,m}:=P_{t,m}-P_{t-1,m}.
roman_Δ start_POSTSUBSCRIPT italic_t , italic_m end_POSTSUBSCRIPT := italic_P start_POSTSUBSCRIPT italic_t , italic_m end_POSTSUBSCRIPT - italic_P start_POSTSUBSCRIPT italic_t - 1 , italic_m end_POSTSUBSCRIPT .
Then, the price delta time series for market
m
𝑚
m
italic_m
is denoted as
Δ
m
:=
(
Δ
1
,
m
,
…
,
Δ
T
,
m
)
,
assign
subscript
Δ
𝑚
subscript
Δ
1
𝑚
…
subscript
Δ
𝑇
𝑚
\Delta_{m}:=(\Delta_{1,m},\ldots,\Delta_{T,m}),
roman_Δ start_POSTSUBSCRIPT italic_m end_POSTSUBSCRIPT := ( roman_Δ start_POSTSUBSCRIPT 1 , italic_m end_POSTSUBSCRIPT , … , roman_Δ start_POSTSUBSCRIPT italic_T , italic_m end_POSTSUBSCRIPT ) ,
and the matrix of market price deltas is defined as
𝚫
∈
ℝ
T
×
M
𝚫
superscript
ℝ
𝑇
𝑀
\mathbf{\Delta}\in\mathbb{R}^{T\times M}
bold_Δ ∈ blackboard_R start_POSTSUPERSCRIPT italic_T × italic_M end_POSTSUPERSCRIPT
.
Considering that each market exhibits different levels of price volatility, we choose to normalise the price deltas of each market to have unit volatility. This step aligns with the extant literature
[
32
,
53
]
in their construction of time series momentum features.
Definition 4.2
(Volatility-scaled price delta)
.
Given a market
m
𝑚
m
italic_m
, denote its price delta time series from time
t
=
1
𝑡
1
t=1
italic_t = 1
to
t
=
T
𝑡
𝑇
t=T
italic_t = italic_T
as
(
Δ
1
,
m
,
…
,
Δ
T
,
m
)
subscript
Δ
1
𝑚
…
subscript
Δ
𝑇
𝑚
(\Delta_{1,m},\ldots,\Delta_{T,m})
( roman_Δ start_POSTSUBSCRIPT 1 , italic_m end_POSTSUBSCRIPT , … , roman_Δ start_POSTSUBSCRIPT italic_T , italic_m end_POSTSUBSCRIPT )
, let the exponential weighted moving standard deviation at time
t
𝑡
t
italic_t
over a span of 22 days denoted as
σ
t
,
m
22
superscript
subscript
𝜎
𝑡
𝑚
22
\sigma_{t,m}^{22}
italic_σ start_POSTSUBSCRIPT italic_t , italic_m end_POSTSUBSCRIPT start_POSTSUPERSCRIPT 22 end_POSTSUPERSCRIPT
. The volatility scaled price deltas for market
m
𝑚
m
italic_m
at time
t
𝑡
t
italic_t
is defined as
Δ
~
t
,
m
:=
Δ
t
,
m
σ
t
,
m
22
.
assign
subscript
~
Δ
𝑡
𝑚
subscript
Δ
𝑡
𝑚
superscript
subscript
𝜎
𝑡
𝑚
22
\tilde{\Delta}_{t,m}:=\frac{\Delta_{t,m}}{\sigma_{t,m}^{22}}.
over~ start_ARG roman_Δ end_ARG start_POSTSUBSCRIPT italic_t , italic_m end_POSTSUBSCRIPT := divide start_ARG roman_Δ start_POSTSUBSCRIPT italic_t , italic_m end_POSTSUBSCRIPT end_ARG start_ARG italic_σ start_POSTSUBSCRIPT italic_t , italic_m end_POSTSUBSCRIPT start_POSTSUPERSCRIPT 22 end_POSTSUPERSCRIPT end_ARG .
The time series of volatility-scaled price delta for market
m
𝑚
m
italic_m
is denoted as
Δ
~
m
=
(
Δ
~
1
,
m
,
…
,
Δ
~
T
,
m
)
,
subscript
~
Δ
𝑚
subscript
~
Δ
1
𝑚
…
subscript
~
Δ
𝑇
𝑚
\tilde{\Delta}_{m}=(\tilde{\Delta}_{1,m},\ldots,\tilde{\Delta}_{T,m}),
over~ start_ARG roman_Δ end_ARG start_POSTSUBSCRIPT italic_m end_POSTSUBSCRIPT = ( over~ start_ARG roman_Δ end_ARG start_POSTSUBSCRIPT 1 , italic_m end_POSTSUBSCRIPT , … , over~ start_ARG roman_Δ end_ARG start_POSTSUBSCRIPT italic_T , italic_m end_POSTSUBSCRIPT ) ,
and the matrix of all volatility-scaled market price deltas is defined as
𝚫
~
∈
ℝ
T
×
M
~
𝚫
superscript
ℝ
𝑇
𝑀
\mathbf{\tilde{\Delta}}\in\mathbb{R}^{T\times M}
over~ start_ARG bold_Δ end_ARG ∈ blackboard_R start_POSTSUPERSCRIPT italic_T × italic_M end_POSTSUPERSCRIPT
.
Definition 4.3
(Volatility-scaled price)
.
Given a market
m
𝑚
m
italic_m
, denote its volatility-scaled price delta time series from time
t
=
1
𝑡
1
t=1
italic_t = 1
to
t
=
T
𝑡
𝑇
t=T
italic_t = italic_T
as
(
Δ
~
1
,
m
,
…
,
Δ
~
T
,
m
)
subscript
~
Δ
1
𝑚
…
subscript
~
Δ
𝑇
𝑚
(\tilde{\Delta}_{1,m},\ldots,\tilde{\Delta}_{T,m})
( over~ start_ARG roman_Δ end_ARG start_POSTSUBSCRIPT 1 , italic_m end_POSTSUBSCRIPT , … , over~ start_ARG roman_Δ end_ARG start_POSTSUBSCRIPT italic_T , italic_m end_POSTSUBSCRIPT )
, the volatility-scaled price for market
m
𝑚
m
italic_m
at time
t
𝑡
t
italic_t
is defined as
P
~
t
,
m
:=
∑
i
=
0
t
Δ
~
i
,
m
.
assign
subscript
~
𝑃
𝑡
𝑚
superscript
subscript
𝑖
0
𝑡
subscript
~
Δ
𝑖
𝑚
\tilde{P}_{t,m}:=\sum_{i=0}^{t}\tilde{\Delta}_{i,m}.
over~ start_ARG italic_P end_ARG start_POSTSUBSCRIPT italic_t , italic_m end_POSTSUBSCRIPT := ∑ start_POSTSUBSCRIPT italic_i = 0 end_POSTSUBSCRIPT start_POSTSUPERSCRIPT italic_t end_POSTSUPERSCRIPT over~ start_ARG roman_Δ end_ARG start_POSTSUBSCRIPT italic_i , italic_m end_POSTSUBSCRIPT .
The time series of volatility-scaled price for market
m
𝑚
m
italic_m
is denoted as
P
~
:=
(
P
~
1
,
m
,
…
,
P
~
T
,
m
)
,
assign
~
𝑃
subscript
~
𝑃
1
𝑚
…
subscript
~
𝑃
𝑇
𝑚
\tilde{P}:=(\tilde{P}_{1,m},\ldots,\tilde{P}_{T,m}),
over~ start_ARG italic_P end_ARG := ( over~ start_ARG italic_P end_ARG start_POSTSUBSCRIPT 1 , italic_m end_POSTSUBSCRIPT , … , over~ start_ARG italic_P end_ARG start_POSTSUBSCRIPT italic_T , italic_m end_POSTSUBSCRIPT ) ,
and the matrix of all market price deltas is defined as
𝐏
~
∈
ℝ
T
×
M
~
𝐏
superscript
ℝ
𝑇
𝑀
\mathbf{\tilde{P}}\in\mathbb{R}^{T\times M}
over~ start_ARG bold_P end_ARG ∈ blackboard_R start_POSTSUPERSCRIPT italic_T × italic_M end_POSTSUPERSCRIPT
.
Now, we are ready to define the time series momentum features, which we also refer to as oscillators or individual momentum features interchangeably.
Definition 4.4
(Time series momentum features)
.
Given a volatility-scaled price time series for market
m
𝑚
m
italic_m
from time
t
=
1
𝑡
1
t=1
italic_t = 1
to
t
=
T
𝑡
𝑇
t=T
italic_t = italic_T
,
P
~
m
=
(
P
~
1
,
m
,
…
,
P
~
T
,
m
)
subscript
~
𝑃
𝑚
subscript
~
𝑃
1
𝑚
…
subscript
~
𝑃
𝑇
𝑚
\tilde{P}_{m}=(\tilde{P}_{1,m},\ldots,\tilde{P}_{T,m})
over~ start_ARG italic_P end_ARG start_POSTSUBSCRIPT italic_m end_POSTSUBSCRIPT = ( over~ start_ARG italic_P end_ARG start_POSTSUBSCRIPT 1 , italic_m end_POSTSUBSCRIPT , … , over~ start_ARG italic_P end_ARG start_POSTSUBSCRIPT italic_T , italic_m end_POSTSUBSCRIPT )
, and a speed parameter
k
∈
ℕ
+
𝑘
subscript
ℕ
k\in\mathbb{N}_{+}
italic_k ∈ blackboard_N start_POSTSUBSCRIPT + end_POSTSUBSCRIPT
, we define two smoothing factors as,
α
f
⁢
a
⁢
s
⁢
t
⁢
(
k
)
:=
1
2
k
,
α
s
⁢
l
⁢
o
⁢
w
⁢
(
k
)
:=
1
M
×
2
k
.
formulae-sequence
assign
subscript
𝛼
𝑓
𝑎
𝑠
𝑡
𝑘
1
superscript
2
𝑘
assign
subscript
𝛼
𝑠
𝑙
𝑜
𝑤
𝑘
1
𝑀
superscript
2
𝑘
\alpha_{fast}(k):=\frac{1}{2^{k}},\quad\alpha_{slow}(k):=\frac{1}{M\times 2^{k%
}}.
italic_α start_POSTSUBSCRIPT italic_f italic_a italic_s italic_t end_POSTSUBSCRIPT ( italic_k ) := divide start_ARG 1 end_ARG start_ARG 2 start_POSTSUPERSCRIPT italic_k end_POSTSUPERSCRIPT end_ARG , italic_α start_POSTSUBSCRIPT italic_s italic_l italic_o italic_w end_POSTSUBSCRIPT ( italic_k ) := divide start_ARG 1 end_ARG start_ARG italic_M × 2 start_POSTSUPERSCRIPT italic_k end_POSTSUPERSCRIPT end_ARG .
for some
M
>
1
𝑀
1
M>1
italic_M > 1
. Let
μ
⁢
(
P
,
α
)
𝜇
𝑃
𝛼
\mu(P,\alpha)
italic_μ ( italic_P , italic_α )
denote the exponential-weighted moving average of time-series
P
𝑃
P
italic_P
, with decay factor
α
𝛼
\alpha
italic_α
. Then the oscillator for market
m
𝑚
m
italic_m
and speed
k
𝑘
k
italic_k
, denoted
R
t
,
m
k
superscript
subscript
𝑅
𝑡
𝑚
𝑘
R_{t,m}^{k}
italic_R start_POSTSUBSCRIPT italic_t , italic_m end_POSTSUBSCRIPT start_POSTSUPERSCRIPT italic_k end_POSTSUPERSCRIPT
, is defined by:
R
t
,
m
k
:=
μ
⁢
(
P
~
m
,
α
f
⁢
a
⁢
s
⁢
t
⁢
(
k
)
)
−
μ
⁢
(
P
~
m
,
α
s
⁢
l
⁢
o
⁢
w
⁢
(
k
)
)
assign
superscript
subscript
𝑅
𝑡
𝑚
𝑘
𝜇
subscript
~
𝑃
𝑚
subscript
𝛼
𝑓
𝑎
𝑠
𝑡
𝑘
𝜇
subscript
~
𝑃
𝑚
subscript
𝛼
𝑠
𝑙
𝑜
𝑤
𝑘
R_{t,m}^{k}:=\mu(\tilde{P}_{m},\alpha_{fast}(k))-\mu(\tilde{P}_{m},\alpha_{%
slow}(k))
italic_R start_POSTSUBSCRIPT italic_t , italic_m end_POSTSUBSCRIPT start_POSTSUPERSCRIPT italic_k end_POSTSUPERSCRIPT := italic_μ ( over~ start_ARG italic_P end_ARG start_POSTSUBSCRIPT italic_m end_POSTSUBSCRIPT , italic_α start_POSTSUBSCRIPT italic_f italic_a italic_s italic_t end_POSTSUBSCRIPT ( italic_k ) ) - italic_μ ( over~ start_ARG italic_P end_ARG start_POSTSUBSCRIPT italic_m end_POSTSUBSCRIPT , italic_α start_POSTSUBSCRIPT italic_s italic_l italic_o italic_w end_POSTSUBSCRIPT ( italic_k ) )
The underlying intuition is that the crossover of exponentially weighted moving averages can provide insight into recent market trends. If the short-term average price crosses above the long-term average price from below, it indicates an expected increase in price, suggesting a potential upward trend and, conversely, a downward trend if it crosses below. Also, for smaller speed parameters
k
𝑘
k
italic_k
, the time series momentum feature contains information for short-term recent trends. Conversely, time series momentum features with larger speed parameter
k
𝑘
k
italic_k
contain long-term trend information. Throughout this paper, we choose
k
=
{
1
,
2
,
3
,
4
,
5
,
6
}
𝑘
1
2
3
4
5
6
k=\{1,2,3,4,5,6\}
italic_k = { 1 , 2 , 3 , 4 , 5 , 6 }
to create
6
6
6
6
time series momentum features at different speeds to identify the auto-correlation in each market, in line with existing literature
[
53
,
32
,
54
]
.
4.3
Set Up for Network Momentum Features
Now we introduce the setup for the network momentum features. At each trading day
t
𝑡
t
italic_t
, given a lookback window of
δ
𝛿
\delta
italic_δ
days, the first step of constructing network momentum features is to use one of the lead-lag detection methods (Lévy Area, or one- or multi-dimensional DTW) to construct a lead-lag matrix
𝐕
t
subscript
𝐕
𝑡
\mathbf{V}_{t}
bold_V start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT
by using the volatility-scaled prices deltas
𝚫
~
t
∈
ℝ
δ
×
M
subscript
~
𝚫
𝑡
superscript
ℝ
𝛿
𝑀
\mathbf{\tilde{\Delta}}_{t}\in\mathbb{R}^{\delta\times M}
over~ start_ARG bold_Δ end_ARG start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT ∈ blackboard_R start_POSTSUPERSCRIPT italic_δ × italic_M end_POSTSUPERSCRIPT
as input features. Each vector in
𝚫
~
t
subscript
~
𝚫
𝑡
\mathbf{\tilde{\Delta}}_{t}
over~ start_ARG bold_Δ end_ARG start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT
is the volatility-scaled price delta for a market across the past
δ
𝛿
\delta
italic_δ
days from day
t
𝑡
t
italic_t
. The second step is to apply the graph learning algorithm in Table
1
by using the lead-lag matrix
𝐕
t
subscript
𝐕
𝑡
\mathbf{V}_{t}
bold_V start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT
as an input feature to obtain the normalised adjacency matrix
𝐀
~
t
subscript
~
𝐀
𝑡
\mathbf{\tilde{A}}_{t}
over~ start_ARG bold_A end_ARG start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT
.
Definition 4.5
(Network momentum feature)
.
Given a time series momentum feature with speed
k
𝑘
k
italic_k
for market
m
𝑚
m
italic_m
at time
t
𝑡
t
italic_t
,
R
t
,
m
k
superscript
subscript
𝑅
𝑡
𝑚
𝑘
R_{t,m}^{k}
italic_R start_POSTSUBSCRIPT italic_t , italic_m end_POSTSUBSCRIPT start_POSTSUPERSCRIPT italic_k end_POSTSUPERSCRIPT
, and the normalised adjacency matrix
𝐀
~
t
subscript
~
𝐀
𝑡
\mathbf{\tilde{A}}_{t}
over~ start_ARG bold_A end_ARG start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT
from fitting a lead-lag detection model and the graph learning model to the volatility-scaled price deltas
𝚫
~
t
subscript
~
𝚫
𝑡
\mathbf{\tilde{\Delta}}_{t}
over~ start_ARG bold_Δ end_ARG start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT
, the network momentum feature with speed
k
𝑘
k
italic_k
for market
m
𝑚
m
italic_m
at time
t
𝑡
t
italic_t
is defined as
R
~
t
,
m
k
:=
∑
n
∈
𝒩
t
⁢
(
m
)
A
~
m
,
n
⁢
R
t
,
m
k
,
assign
superscript
subscript
~
𝑅
𝑡
𝑚
𝑘
subscript
𝑛
subscript
𝒩
𝑡
𝑚
subscript
~
𝐴
𝑚
𝑛
superscript
subscript
𝑅
𝑡
𝑚
𝑘
\tilde{R}_{t,m}^{k}:=\sum_{n\in\mathcal{N}_{t}(m)}\tilde{A}_{m,n}R_{t,m}^{k},
over~ start_ARG italic_R end_ARG start_POSTSUBSCRIPT italic_t , italic_m end_POSTSUBSCRIPT start_POSTSUPERSCRIPT italic_k end_POSTSUPERSCRIPT := ∑ start_POSTSUBSCRIPT italic_n ∈ caligraphic_N start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT ( italic_m ) end_POSTSUBSCRIPT over~ start_ARG italic_A end_ARG start_POSTSUBSCRIPT italic_m , italic_n end_POSTSUBSCRIPT italic_R start_POSTSUBSCRIPT italic_t , italic_m end_POSTSUBSCRIPT start_POSTSUPERSCRIPT italic_k end_POSTSUPERSCRIPT ,
where
𝒩
t
⁢
(
m
)
subscript
𝒩
𝑡
𝑚
\mathcal{N}_{t}(m)
caligraphic_N start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT ( italic_m )
denotes the set of markets connected to market
m
𝑚
m
italic_m
such that
A
~
m
,
n
≠
0
subscript
~
𝐴
𝑚
𝑛
0
\tilde{A}_{m,n}\neq 0
over~ start_ARG italic_A end_ARG start_POSTSUBSCRIPT italic_m , italic_n end_POSTSUBSCRIPT ≠ 0
and
R
t
,
n
k
superscript
subscript
𝑅
𝑡
𝑛
𝑘
R_{t,n}^{k}
italic_R start_POSTSUBSCRIPT italic_t , italic_n end_POSTSUBSCRIPT start_POSTSUPERSCRIPT italic_k end_POSTSUPERSCRIPT
is the time series momentum feature with the same speed for market
n
𝑛
n
italic_n
at time
t
𝑡
t
italic_t
.
Considering that the graph sparsity is significantly influenced by the two hyperparameters
α
𝛼
\alpha
italic_α
and
β
𝛽
\beta
italic_β
in the graph learning model
3.1
, which consequently affect the number of connections each market can establish, we conduct a discrete grid search over the combinations of:
α
=
{
0.001
,
0.01
,
0.1
,
1
,
10
,
100
}
⁢
,
β
=
{
0.001
,
0.01
,
0.1
,
1
,
10
,
100
}
,
formulae-sequence
𝛼
0.001
0.01
0.1
1
10
100
,
𝛽
0.001
0.01
0.1
1
10
100
\alpha=\{0.001,0.01,0.1,1,10,100\}\text{,}\quad\beta=\{0.001,0.01,0.1,1,10,100\},
italic_α = { 0.001 , 0.01 , 0.1 , 1 , 10 , 100 } , italic_β = { 0.001 , 0.01 , 0.1 , 1 , 10 , 100 } ,
on in-sample data to determine their optimal combination for achieving the highest net Sharpe ratio.
We choose
δ
=
132
𝛿
132
\delta=132
italic_δ = 132
, so the lead-lag matrix is constructed by considering each market’s past half year’s daily performances. In addition, to enhance the robustness of our model, we consider employing an ensemble method that fits multiple lead-lag matrices to
𝚫
~
t
subscript
~
𝚫
𝑡
\mathbf{\tilde{\Delta}}_{t}
over~ start_ARG bold_Δ end_ARG start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT
across a range of lookback windows. Specifically, we use the following series of lookback windows:
δ
=
{
22
,
44
,
66
,
88
,
110
,
132
}
.
𝛿
22
44
66
88
110
132
\delta=\{22,44,66,88,110,132\}.
italic_δ = { 22 , 44 , 66 , 88 , 110 , 132 } .
The multiple lead-lag matrices are summarised into a series, which serves as a new input to the graph learning algorithm detailed in Table
1
. According to
[
32
]
, employing an ensemble method helps reduce the variance of the learned edge weights, improving the strategy’s performance and reducing turnover.
5
Portfolio Construction
In this section we detail our portfolio construction methodology and baseline model. We then introduce our variations of network momentum and compare them to the baseline. The raw inputs to the portfolio are the moving-average crossover signals for each market, at various speeds, described in the previous section. The first step in the portfolio construction is to attenuate these raw signals by passing them through a
response function
which has the effect of exponentially squashing the signal towards zero in its left and right tails. If the raw signal sign is an indication of the strength of a trend (both upwards and downwards), then the response function serves the purpose of risk control, in the sense that it curtails the response to extreme trends, reflecting increasing uncertainty about the predictive power of the trend signal at its extremes. To this end, we choose a
reverting sigmoid
function
[
38
]
with the following functional form:
Definition 5.1
(Response function)
.
The response function
r
⁢
(
x
)
:
ℝ
→
ℝ
:
𝑟
𝑥
→
ℝ
ℝ
r(x):\mathbb{R}\rightarrow\mathbb{R}
italic_r ( italic_x ) : blackboard_R → blackboard_R
, parameterised by a positive constant
λ
>
0
𝜆
0
\lambda>0
italic_λ > 0
, is defined as follows:
r
⁢
(
x
)
:=
c
λ
⋅
x
⋅
e
−
λ
2
⁢
x
2
/
2
,
assign
𝑟
𝑥
⋅
subscript
𝑐
𝜆
𝑥
superscript
𝑒
superscript
𝜆
2
superscript
𝑥
2
2
r(x):=c_{\lambda}\cdot x\cdot e^{-\lambda^{2}x^{2}/2},
italic_r ( italic_x ) := italic_c start_POSTSUBSCRIPT italic_λ end_POSTSUBSCRIPT ⋅ italic_x ⋅ italic_e start_POSTSUPERSCRIPT - italic_λ start_POSTSUPERSCRIPT 2 end_POSTSUPERSCRIPT italic_x start_POSTSUPERSCRIPT 2 end_POSTSUPERSCRIPT / 2 end_POSTSUPERSCRIPT ,
In accordance with our desire to combine signals with unit variance, the response function includes the term
c
λ
subscript
𝑐
𝜆
c_{\lambda}
italic_c start_POSTSUBSCRIPT italic_λ end_POSTSUBSCRIPT
which acts as a normalisation constant. The response function peaks at
x
=
±
λ
−
1
𝑥
plus-or-minus
superscript
𝜆
1
x=\pm\lambda^{-1}
italic_x = ± italic_λ start_POSTSUPERSCRIPT - 1 end_POSTSUPERSCRIPT
, setting the bounds for the maximum response to the trend signal; we fix
λ
=
2
𝜆
2
\lambda=\sqrt{2}
italic_λ = square-root start_ARG 2 end_ARG
.
Definition 5.2
(Position signal)
.
Given a series of momentum features with different speeds for market
m
𝑚
m
italic_m
at time
t
𝑡
t
italic_t
,
(
R
t
,
m
1
,
…
,
R
t
,
m
K
)
superscript
subscript
𝑅
𝑡
𝑚
1
…
superscript
subscript
𝑅
𝑡
𝑚
𝐾
(R_{t,m}^{1},\ldots,R_{t,m}^{K})
( italic_R start_POSTSUBSCRIPT italic_t , italic_m end_POSTSUBSCRIPT start_POSTSUPERSCRIPT 1 end_POSTSUPERSCRIPT , … , italic_R start_POSTSUBSCRIPT italic_t , italic_m end_POSTSUBSCRIPT start_POSTSUPERSCRIPT italic_K end_POSTSUPERSCRIPT )
, the desired daily position, in lots, for market
m
𝑚
m
italic_m
at time
t
𝑡
t
italic_t
,
X
t
,
m
subscript
𝑋
𝑡
𝑚
X_{t,m}
italic_X start_POSTSUBSCRIPT italic_t , italic_m end_POSTSUBSCRIPT
is defined as:
X
t
,
m
:=
1
M
⁢
(
1
K
⁢
∑
k
=
1
K
r
⁢
(
R
t
,
m
k
)
)
⋅
(
F
t
,
m
⋅
E
t
,
m
⋅
σ
t
,
m
22
)
−
1
⋅
Γ
⋅
σ
tgt
252
,
assign
subscript
𝑋
𝑡
𝑚
⋅
1
𝑀
1
𝐾
superscript
subscript
𝑘
1
𝐾
𝑟
superscript
subscript
𝑅
𝑡
𝑚
𝑘
superscript
⋅
subscript
𝐹
𝑡
𝑚
subscript
𝐸
𝑡
𝑚
superscript
subscript
𝜎
𝑡
𝑚
22
1
Γ
subscript
𝜎
tgt
252
X_{t,m}:=\frac{1}{M}\left(\frac{1}{K}\sum_{k=1}^{K}r(R_{t,m}^{k})\right)\cdot(%
F_{t,m}\cdot E_{t,m}\cdot\sigma_{t,m}^{22})^{-1}\cdot\Gamma\cdot\frac{\sigma_{%
\text{tgt}}}{\sqrt{252}},
italic_X start_POSTSUBSCRIPT italic_t , italic_m end_POSTSUBSCRIPT := divide start_ARG 1 end_ARG start_ARG italic_M end_ARG ( divide start_ARG 1 end_ARG start_ARG italic_K end_ARG ∑ start_POSTSUBSCRIPT italic_k = 1 end_POSTSUBSCRIPT start_POSTSUPERSCRIPT italic_K end_POSTSUPERSCRIPT italic_r ( italic_R start_POSTSUBSCRIPT italic_t , italic_m end_POSTSUBSCRIPT start_POSTSUPERSCRIPT italic_k end_POSTSUPERSCRIPT ) ) ⋅ ( italic_F start_POSTSUBSCRIPT italic_t , italic_m end_POSTSUBSCRIPT ⋅ italic_E start_POSTSUBSCRIPT italic_t , italic_m end_POSTSUBSCRIPT ⋅ italic_σ start_POSTSUBSCRIPT italic_t , italic_m end_POSTSUBSCRIPT start_POSTSUPERSCRIPT 22 end_POSTSUPERSCRIPT ) start_POSTSUPERSCRIPT - 1 end_POSTSUPERSCRIPT ⋅ roman_Γ ⋅ divide start_ARG italic_σ start_POSTSUBSCRIPT tgt end_POSTSUBSCRIPT end_ARG start_ARG square-root start_ARG 252 end_ARG end_ARG ,
where
•
F
t
,
m
subscript
𝐹
𝑡
𝑚
F_{t,m}
italic_F start_POSTSUBSCRIPT italic_t , italic_m end_POSTSUBSCRIPT
denotes the
point value
of the futures contract: the local currency value of a 1 point move in the price of the contract,
•
E
t
,
m
subscript
𝐸
𝑡
𝑚
E_{t,m}
italic_E start_POSTSUBSCRIPT italic_t , italic_m end_POSTSUBSCRIPT
denotes the exchange rate between the currency in which market
m
𝑚
m
italic_m
trades and the USD on day
t
𝑡
t
italic_t
,
•
σ
t
,
m
22
superscript
subscript
𝜎
𝑡
𝑚
22
\sigma_{t,m}^{22}
italic_σ start_POSTSUBSCRIPT italic_t , italic_m end_POSTSUBSCRIPT start_POSTSUPERSCRIPT 22 end_POSTSUPERSCRIPT
is the exponential weighted
moving standard deviation of the price delta of market
m
𝑚
m
italic_m
at time t over a span of 22 days,
•
Γ
Γ
\Gamma
roman_Γ
denotes the
notional aum
allocated to the portfolio, in USD,
•
σ
tgt
subscript
𝜎
tgt
\sigma_{\text{tgt}}
italic_σ start_POSTSUBSCRIPT tgt end_POSTSUBSCRIPT
denotes the annual target portfolio volatility which we set at
10
%
percent
10
10\%
10 %
,
•
r
⁢
(
⋅
)
𝑟
⋅
r(\cdot)
italic_r ( ⋅ )
is the response function in Definition
5.1
.
In this construction, we take equal contributions from oscillators with different speeds and markets in our portfolio, accounted for by the scalars
1
M
1
𝑀
\frac{1}{M}
divide start_ARG 1 end_ARG start_ARG italic_M end_ARG
and
1
K
1
𝐾
\frac{1}{K}
divide start_ARG 1 end_ARG start_ARG italic_K end_ARG
respectively. The term
(
F
t
,
m
⋅
E
t
,
m
⋅
σ
t
,
m
22
)
−
1
superscript
⋅
subscript
𝐹
𝑡
𝑚
subscript
𝐸
𝑡
𝑚
superscript
subscript
𝜎
𝑡
𝑚
22
1
(F_{t,m}\cdot E_{t,m}\cdot\sigma_{t,m}^{22})^{-1}
( italic_F start_POSTSUBSCRIPT italic_t , italic_m end_POSTSUBSCRIPT ⋅ italic_E start_POSTSUBSCRIPT italic_t , italic_m end_POSTSUBSCRIPT ⋅ italic_σ start_POSTSUBSCRIPT italic_t , italic_m end_POSTSUBSCRIPT start_POSTSUPERSCRIPT 22 end_POSTSUPERSCRIPT ) start_POSTSUPERSCRIPT - 1 end_POSTSUPERSCRIPT
is the number of lots of market
m
𝑚
m
italic_m
on day
t
𝑡
t
italic_t
required to achieve 1 USD of risk. The last part in the above definition,
(
Γ
⋅
σ
tgt
252
)
⋅
Γ
subscript
𝜎
tgt
252
\left(\Gamma\cdot\frac{\sigma_{\text{tgt}}}{\sqrt{252}}\right)
( roman_Γ ⋅ divide start_ARG italic_σ start_POSTSUBSCRIPT tgt end_POSTSUBSCRIPT end_ARG start_ARG square-root start_ARG 252 end_ARG end_ARG )
, is used to scale our position to realise the daily USD risk amount that our portfolio is targeting. The desired daily position, as defined above, constitutes our baseline portfolio. In practice, the weights controlling the contribution of the oscillators, and the participation of each market in the portfolio, would be subject to selection via an optimisation process, where the objective function would attempt to maximise some desirable portfolio metric (typically the net Sharpe ratio). It is our intention to compare the relative merits of various network momentum indicators to a baseline trend-following portfolio, so we choose to work with an unoptimised (at least with respect to oscillator and market weights) portfolio. Given the baseline portfolio above, the portfolios constructed using the network momentum signals for each market
m
𝑚
m
italic_m
at time
t
𝑡
t
italic_t
,
X
~
t
,
m
subscript
~
𝑋
𝑡
𝑚
\tilde{X}_{t,m}
over~ start_ARG italic_X end_ARG start_POSTSUBSCRIPT italic_t , italic_m end_POSTSUBSCRIPT
, are defined similarly by replacing the momentum features in Definition
5.2
with the network momentum features in Definition
4.5
.
The daily gross USD pnl of market
m
𝑚
m
italic_m
generated by the position signal
X
t
,
m
subscript
𝑋
𝑡
𝑚
X_{t,m}
italic_X start_POSTSUBSCRIPT italic_t , italic_m end_POSTSUBSCRIPT
for time series momentum features is calculated as
r
t
+
2
,
m
:=
X
t
,
m
⋅
Δ
t
+
2
,
m
⋅
F
t
+
2
,
m
⋅
E
t
+
2
,
m
.
assign
subscript
𝑟
𝑡
2
𝑚
⋅
subscript
𝑋
𝑡
𝑚
subscript
Δ
𝑡
2
𝑚
subscript
𝐹
𝑡
2
𝑚
subscript
𝐸
𝑡
2
𝑚
r_{t+2,m}:=X_{t,m}\cdot\Delta_{t+2,m}\cdot F_{t+2,m}\cdot E_{t+2,m}.
italic_r start_POSTSUBSCRIPT italic_t + 2 , italic_m end_POSTSUBSCRIPT := italic_X start_POSTSUBSCRIPT italic_t , italic_m end_POSTSUBSCRIPT ⋅ roman_Δ start_POSTSUBSCRIPT italic_t + 2 , italic_m end_POSTSUBSCRIPT ⋅ italic_F start_POSTSUBSCRIPT italic_t + 2 , italic_m end_POSTSUBSCRIPT ⋅ italic_E start_POSTSUBSCRIPT italic_t + 2 , italic_m end_POSTSUBSCRIPT .
and the daily gross USD pnl for network momentum features
r
~
t
+
2
,
m
subscript
~
𝑟
𝑡
2
𝑚
\tilde{r}_{t+2,m}
over~ start_ARG italic_r end_ARG start_POSTSUBSCRIPT italic_t + 2 , italic_m end_POSTSUBSCRIPT
is calculated similarly by replacing
X
t
,
m
subscript
𝑋
𝑡
𝑚
X_{t,m}
italic_X start_POSTSUBSCRIPT italic_t , italic_m end_POSTSUBSCRIPT
with
X
~
t
,
m
subscript
~
𝑋
𝑡
𝑚
\tilde{X}_{t,m}
over~ start_ARG italic_X end_ARG start_POSTSUBSCRIPT italic_t , italic_m end_POSTSUBSCRIPT
. We take a conservative approach to pnl calculation, assuming that a signal/position generated at time
t
𝑡
t
italic_t
, including information up to time
t
𝑡
t
italic_t
, is established via a trade at time
t
+
1
𝑡
1
t+1
italic_t + 1
, and then pnl is earned on the position at time
t
+
2
𝑡
2
t+2
italic_t + 2
. This is in contrast to examples in the literature that assume pnl from a signal at time
t
𝑡
t
italic_t
is earned in full on the position at time
t
+
1
𝑡
1
t+1
italic_t + 1
, but this is unrealistic, unless the position is established at or close to the opening price on day
t
+
1
𝑡
1
t+1
italic_t + 1
, and may also be compromised by market synchronicity issues. To calculate the net USD pnl, we need to incorporate transaction costs, which typically are a market specific cost to establishing each new daily desired position. We choose market specific transaction costs based on half the average bid-ask spread in the market, denoted
s
m
subscript
𝑠
𝑚
s_{m}
italic_s start_POSTSUBSCRIPT italic_m end_POSTSUBSCRIPT
, over some representative historical period. The transaction cost for establishing the position
X
t
,
m
subscript
𝑋
𝑡
𝑚
X_{t,m}
italic_X start_POSTSUBSCRIPT italic_t , italic_m end_POSTSUBSCRIPT
on day
t
+
1
𝑡
1
t+1
italic_t + 1
is then calculated as:
c
t
+
1
,
m
:=
|
X
t
+
1
,
m
−
X
t
,
m
|
⋅
s
m
2
⋅
F
t
+
1
,
m
⋅
E
t
+
1
,
m
.
assign
subscript
𝑐
𝑡
1
𝑚
⋅
subscript
𝑋
𝑡
1
𝑚
subscript
𝑋
𝑡
𝑚
subscript
𝑠
𝑚
2
subscript
𝐹
𝑡
1
𝑚
subscript
𝐸
𝑡
1
𝑚
c_{t+1,m}:=\left|X_{t+1,m}-X_{t,m}\right|\cdot\frac{s_{m}}{2}\cdot F_{t+1,m}%
\cdot E_{t+1,m}.
italic_c start_POSTSUBSCRIPT italic_t + 1 , italic_m end_POSTSUBSCRIPT := | italic_X start_POSTSUBSCRIPT italic_t + 1 , italic_m end_POSTSUBSCRIPT - italic_X start_POSTSUBSCRIPT italic_t , italic_m end_POSTSUBSCRIPT | ⋅ divide start_ARG italic_s start_POSTSUBSCRIPT italic_m end_POSTSUBSCRIPT end_ARG start_ARG 2 end_ARG ⋅ italic_F start_POSTSUBSCRIPT italic_t + 1 , italic_m end_POSTSUBSCRIPT ⋅ italic_E start_POSTSUBSCRIPT italic_t + 1 , italic_m end_POSTSUBSCRIPT .
Here we estimate the cost for executing the trade by half of the spread
s
t
,
m
subscript
𝑠
𝑡
𝑚
s_{t,m}
italic_s start_POSTSUBSCRIPT italic_t , italic_m end_POSTSUBSCRIPT
, and this execution happens during day
t
+
1
𝑡
1
t+1
italic_t + 1
. We denote the transaction cost for network momentum features as
c
~
t
+
1
,
m
subscript
~
𝑐
𝑡
1
𝑚
\tilde{c}_{t+1,m}
over~ start_ARG italic_c end_ARG start_POSTSUBSCRIPT italic_t + 1 , italic_m end_POSTSUBSCRIPT
by replacing
X
t
,
m
subscript
𝑋
𝑡
𝑚
X_{t,m}
italic_X start_POSTSUBSCRIPT italic_t , italic_m end_POSTSUBSCRIPT
with
X
~
t
,
m
subscript
~
𝑋
𝑡
𝑚
\tilde{X}_{t,m}
over~ start_ARG italic_X end_ARG start_POSTSUBSCRIPT italic_t , italic_m end_POSTSUBSCRIPT
.
Finally, we can calculate the net daily USD pnl for market
m
𝑚
m
italic_m
on time
t
𝑡
t
italic_t
by
r
t
,
m
′
=
r
t
,
m
−
c
t
,
m
.
subscript
superscript
𝑟
′
𝑡
𝑚
subscript
𝑟
𝑡
𝑚
subscript
𝑐
𝑡
𝑚
r^{\prime}_{t,m}=r_{t,m}-c_{t,m}.
italic_r start_POSTSUPERSCRIPT ′ end_POSTSUPERSCRIPT start_POSTSUBSCRIPT italic_t , italic_m end_POSTSUBSCRIPT = italic_r start_POSTSUBSCRIPT italic_t , italic_m end_POSTSUBSCRIPT - italic_c start_POSTSUBSCRIPT italic_t , italic_m end_POSTSUBSCRIPT .
This formulation means that the net pnl generated by market
m
𝑚
m
italic_m
on day
t
𝑡
t
italic_t
consists of the gross return realised from position
X
t
−
2
,
m
subscript
𝑋
𝑡
2
𝑚
X_{t-2,m}
italic_X start_POSTSUBSCRIPT italic_t - 2 , italic_m end_POSTSUBSCRIPT
combined with the transaction cost incurred for establishing the new position generated by
X
t
−
1
,
m
subscript
𝑋
𝑡
1
𝑚
X_{t-1,m}
italic_X start_POSTSUBSCRIPT italic_t - 1 , italic_m end_POSTSUBSCRIPT
. The net pnl for network momentum features is denoted as
r
~
t
,
m
′
subscript
superscript
~
𝑟
′
𝑡
𝑚
\tilde{r}^{\prime}_{t,m}
over~ start_ARG italic_r end_ARG start_POSTSUPERSCRIPT ′ end_POSTSUPERSCRIPT start_POSTSUBSCRIPT italic_t , italic_m end_POSTSUBSCRIPT
by replacing
X
t
,
m
subscript
𝑋
𝑡
𝑚
X_{t,m}
italic_X start_POSTSUBSCRIPT italic_t , italic_m end_POSTSUBSCRIPT
with
X
~
t
,
m
subscript
~
𝑋
𝑡
𝑚
\tilde{X}_{t,m}
over~ start_ARG italic_X end_ARG start_POSTSUBSCRIPT italic_t , italic_m end_POSTSUBSCRIPT
. We can transform the daily pnl into a return by dividing through by the notional aum,
Γ
Γ
\Gamma
roman_Γ
.
We utilise a uniform methodology for portfolio construction and returns calculation across a series of candidate network momentum models, each employing different lead-lag detection algorithms. The models and their configurations are defined as follows:
1.
MACD
uses the time series momentum signal
R
m
k
superscript
subscript
𝑅
𝑚
𝑘
R_{m}^{k}
italic_R start_POSTSUBSCRIPT italic_m end_POSTSUBSCRIPT start_POSTSUPERSCRIPT italic_k end_POSTSUPERSCRIPT
, for
k
𝑘
k
italic_k
from 1 to 6, as defined in Definition
4.4
, to calculate the position signal in Definition
5.2
; this is our baseline model portfolio.
2.
NMM-DTW
and
NMM-DTW-E
:
•
At each training time
t
𝑡
t
italic_t
, NMM-DTW constructs the lead-lag matrix
𝐕
t
subscript
𝐕
𝑡
\mathbf{V}_{t}
bold_V start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT
using the classical dynamic time warping algorithm with a
δ
=
132
𝛿
132
\delta=132
italic_δ = 132
lookback window. This matrix inputs into the graph learning model and computes the normalised adjacency matrix
𝐀
~
t
subscript
~
𝐀
𝑡
\mathbf{\tilde{A}}_{t}
over~ start_ARG bold_A end_ARG start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT
following the algorithm in Table
1
. The network momentum features derived from
𝐀
~
t
subscript
~
𝐀
𝑡
\mathbf{\tilde{A}}_{t}
over~ start_ARG bold_A end_ARG start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT
following Definition
4.5
are then used to calculate the position signal (Definition
5.2
).
•
NMM-DTW-E employs ensemble methods by fitting the DTW algorithm to varying lookback windows
δ
=
{
22
,
44
,
66
,
88
,
110
,
132
}
𝛿
22
44
66
88
110
132
\delta=\{22,44,66,88,110,132\}
italic_δ = { 22 , 44 , 66 , 88 , 110 , 132 }
. The resulting series of lead-lag matrices serves as the new input to the graph learning model. Subsequent steps follow the NMM-DTW process.
3.
NMM-DDTW
and
NMM-DDTW-E
:
•
NMM-DDTW uses the derivative dynamic time warping algorithm. Subsequent steps follow the NMM-DTW process.
•
NMM-DDTW-E employs the ensemble methods by constructing the lead-lag matrices with DDTW algorithm with lookback
δ
=
{
22
,
44
,
66
,
88
,
110
,
132
}
𝛿
22
44
66
88
110
132
\delta=\{22,44,66,88,110,132\}
italic_δ = { 22 , 44 , 66 , 88 , 110 , 132 }
. Subsequent steps follow the NMM-DTW-E process.
4.
NMM-SDTW
and
NMM-SDTW-E
:
•
NMM-SDTW applies the shape dynamic time warping algorithm, with the descriptor length
l
=
11
𝑙
11
l=11
italic_l = 11
, to construct the lead-lag matrix with a lookback window of
δ
=
132
𝛿
132
\delta=132
italic_δ = 132
. Subsequent steps follow the NMM-DTW process.
•
NMM-SDTW-E employs the ensemble methods with shape dynamic time warping with lookback windows
δ
=
{
22
,
44
,
66
,
88
,
110
,
132
}
𝛿
22
44
66
88
110
132
\delta=\{22,44,66,88,110,132\}
italic_δ = { 22 , 44 , 66 , 88 , 110 , 132 }
. Subsequent steps follow the NMM-DTW-E process.
5.
NMM-SDDTW
and
NMM-SDDTW-E
:
•
NMM-SDDTW applies the shape dynamic time warping derivative algorithms, with the descriptor length
l
=
11
𝑙
11
l=11
italic_l = 11
, to construct the lead-lag matrix with a lookback window of
δ
=
132
𝛿
132
\delta=132
italic_δ = 132
. Subsequent steps follow the NMM-DTW process.
•
NMM-SDTW-E employs the ensemble methods with lookback windows
δ
=
{
22
,
44
,
66
,
88
,
110
,
132
}
𝛿
22
44
66
88
110
132
\delta=\{22,44,66,88,110,132\}
italic_δ = { 22 , 44 , 66 , 88 , 110 , 132 }
. Subsequent steps follow the NMM-DTW-E process.
6.
NMM-LEVY
and
NMM-LEVY-E
:
•
NMM-LEVY applies the Lévy area algorithm to construct the lead-lag matrix with lookback window
δ
=
132
𝛿
132
\delta=132
italic_δ = 132
. Subsequent steps follow the NMM-DTW process.
•
NMM-LEVY-E employs the ensemble methods with Lévy area algrotim with lookback windows
δ
=
{
22
,
44
,
66
,
88
,
110
,
132
}
𝛿
22
44
66
88
110
132
\delta=\{22,44,66,88,110,132\}
italic_δ = { 22 , 44 , 66 , 88 , 110 , 132 }
. Subsequent steps follow the NMM-DTW-E process.
6
Performance Analysis
6.1
Portfolio Performance Analysis
In evaluating model performance, we consider the following three aspects, following the convention established in
[
32
]
:
1.
Profitability
: This includes annualised expected gross return, annualised expected net return, and hit rate – defined as the percentage of days with positive returns during the out-of-sample periods.
2.
Risk
: This includes volatility, downside deviation, and maximum drawdown to understand the risk exposure of our models. While comparing the volatility and downside deviation across models is unnecessary due to each model’s positions being scaled to a target volatility of 10% (Definition
5.2
), we include them in our summary for completeness, as they remain relevant for calculating the Sharpe ratio and Sortino ratio.
3.
Overall and Other Performance
: This includes transaction costs, skewness of monthly returns, Sharpe ratio (expected return / volatility), Sortino ratio (expected return / downside deviation), Calmar ratio (expected return / maximum drawdown), and the ratio of average profits to average losses
(
Avg. P
Avg. L
)
Avg. P
Avg. L
\left(\frac{\text{Avg. P}}{\text{Avg. L}}\right)
( divide start_ARG Avg. P end_ARG start_ARG Avg. L end_ARG )
.
We present the performance of the benchmark MACD model alongside the network momentum models. Our primary focus is on their average performance across
100
100
100
100
bootstrapped datasets. We investigate whether the network momentum models can achieve a statistically significant higher net Sharpe ratio compared to the benchmark MACD model. We also present the models’ performance on real-world price data from the out-of-sample period of 2005 to 2024 to illustrate their profitability in a historical context.
Table 1:
Performance Metrics for Various Signals
Gross
Transaction
Net
vol.
Sharpe
downside
MDD
Sortino
Calmar
Skewness
hit rate
Avg. P
Return
Return
deviation
Avg. L
Panel A: Average Performance on 100 Bootstrapped Price Data
MACD
0.057
0.027
0.030
0.107
0.277
0.058
0.239
0.515
0.039
0.395
0.516
1.158
NMM-DTW
0.064
0.029
0.034
0.108
0.315
0.058
0.261
0.592
0.041
0.441
0.515
1.200
NMM-DTW-E
0.063
0.023
0.039
0.109
0.353
0.058
0.259
0.669
0.046
0.457
0.516
1.226
NMM-DDTW
0.064
0.029
0.034
0.109
0.315
0.059
0.256
0.590
0.042
0.450
0.514
1.203
NMM-DDTW-E
0.063
0.023
0.039
0.110
0.357
0.058
0.250
0.684
0.048
0.486
0.514
1.243
NMM-SDTW
0.064
0.028
0.035
0.110
0.319
0.058
0.280
0.606
0.041
0.458
0.508
1.235
NMM-SDTW-E
0.062
0.022
0.039
0.109
0.355
0.058
0.255
0.677
0.047
0.473
0.512
1.250
NMM-SDDTW
0.062
0.029
0.032
0.109
0.296
0.057
0.269
0.568
0.039
0.507
0.504
1.234
NMM-SDDTW-E
0.062
0.023
0.038
0.110
0.350
0.057
0.257
0.679
0.046
0.509
0.510
1.255
NMM-LEVY
0.064
0.027
0.036
0.109
0.336
0.059
0.230
0.624
0.050
0.419
0.517
1.206
NMM-LEVY-E
0.060
0.024
0.035
0.109
0.323
0.058
0.240
0.610
0.045
0.454
0.516
1.202
Panel B: Performance on Real Price Data
MACD
0.051
0.026
0.024
0.104
0.233
0.053
0.227
0.454
0.031
0.645
0.526
1.080
NMM-DTW
0.054
0.028
0.026
0.106
0.243
0.056
0.274
0.457
0.027
0.630
0.513
1.147
NMM-DTW-E
0.062
0.023
0.039
0.106
0.364
0.056
0.203
0.694
0.055
0.683
0.509
1.285
NMM-DDTW
0.056
0.028
0.027
0.107
0.257
0.053
0.247
0.517
0.032
0.759
0.487
1.282
NMM-DDTW-E
0.055
0.022
0.032
0.107
0.298
0.055
0.244
0.577
0.038
0.719
0.513
1.198
NMM-SDTW
0.065
0.027
0.037
0.106
0.351
0.054
0.163
0.689
0.066
0.704
0.513
1.249
NMM-SDTW-E
0.055
0.022
0.033
0.107
0.307
0.055
0.222
0.600
0.043
0.691
0.513
1.205
NMM-SDDTW
0.049
0.028
0.020
0.108
0.189
0.058
0.289
0.354
0.020
0.589
0.470
1.303
NMM-SDDTW-E
0.057
0.022
0.035
0.106
0.328
0.054
0.208
0.643
0.048
0.720
0.509
1.249
NMM-LEVY
0.064
0.026
0.038
0.106
0.356
0.054
0.204
0.702
0.053
0.675
0.517
1.228
NMM-LEVY-E
0.055
0.023
0.032
0.106
0.300
0.054
0.208
0.586
0.044
0.673
0.513
1.198
a
Best performance is in bold.
b
No comparison for volatility and downside deviation since every portfolio is scaled to the same target annualised volatility in
5.2
for direct comparison of the net Sharpe.
In Panel A of Table
1
, we report the average performance of the portfolio constructed from various momentum models on bootstrapped price data. In Panel B, we report the performance of these models on real price data from 2005 to 2024.
Based on the metrics in Panel A, all network momentum models (NMM) exhibit higher expected gross returns than the benchmark MACD model, with the NMM-DDTW model achieving the highest at
0.064
0.064
0.064
0.064
, compared to MACD’s
0.057
0.057
0.057
0.057
. Typically, NMM models incur higher transaction costs than MACD, reflecting their sensitivity to market movements and increased daily turnover. However, ensemble methods reduce transaction costs, with DTW variations further decreasing them to
0.022
0.022
0.022
0.022
, approximately 19% lower than MACD. The NMM-LEVY model achieves an 11% reduction in costs. As a result, all NMM models demonstrate better performance over MACD in terms of expected net returns, net Sharpe ratios, and Sortino ratios. Notably, NMM-DDTW-E achieves a Sharpe ratio of
0.357
0.357
0.357
0.357
and a Sortino ratio of
0.684
0.684
0.684
0.684
, marking increases of 29% and 33%, respectively, over MACD.
The ability to effectively follow trends is crucial for trading strategies. NMM-SDDTW-E stands out with the highest
Avg. Profit
Avg. Loss
Avg. Profit
Avg. Loss
\frac{\text{Avg. Profit}}{\text{Avg. Loss}}
divide start_ARG Avg. Profit end_ARG start_ARG Avg. Loss end_ARG
ratio among all NMM models. It also exhibits the highest positive skewness, suggesting that although it may frequently incur small losses, the gains it captures are significant. Meanwhile, NMM-LEVY demonstrates the smallest MDD and highest hit rate, suggesting it is particularly effective at identifying trend reversals and capturing new opportunities for positive returns, although it achieves smaller gains per trade, as indicated by its slightly lower skewness and the ratio between average profit and average loss.
Panel B of Table
1
demonstrates that NMM models outperform the benchmark MACD model on real market data during the out-of-sample period from 2005 to 2024. NMM-DTW-E achieves the highest net Sharpe ratio at
0.364
0.364
0.364
0.364
, compared to the benchmark’s
0.233
0.233
0.233
0.233
, showing better risk-adjusted returns. NMM-DDTW exhibits the most positive skew in returns at
0.759
0.759
0.759
0.759
, surpassing the benchmark’s
0.645
0.645
0.645
0.645
. Although MACD has the highest hit rate, indicating more days with positive PnL, it suffers from the lowest
Avg. Profit
Avg. Loss
Avg. Profit
Avg. Loss
\frac{\text{Avg. Profit}}{\text{Avg. Loss}}
divide start_ARG Avg. Profit end_ARG start_ARG Avg. Loss end_ARG
ratio, suggesting that its losses are larger than those of NMM models. However, we reiterate that the portfolio included in Appendix
7.1
is somewhat random, and the models’ performance may not be reproducible for other portfolios. Therefore, we emphasise that our assessment of the models is primarily based on their performance on the bootstrapped data.
We examine the distribution of the net Sharpe ratios for all models. Figure
1
presents the distribution of Sharpe ratios on the bootstrapped price datasets along with their interquartile ranges. The net Sharpe ratios achieved by each model on the actual price data are marked by red crosses on the distribution plots. The box plots demonstrate that the median net Sharpe ratios for all network momentum models are higher than those for the MACD model, and the ensemble methods further enhance performance. The positioning of the red crosses, which for all models except NMM-DDTW-E and NMM-SDTW-D fall within the interquartile ranges, suggests that the bootstrapped price data provides a valid representation of the real price data and is suitable for inference.
Figure 1:
Distribution of net Sharpe Ratios for the Benchmark Model (MACD) and Network Momentum Models on bootstrapped datasets, with net Sharpe achieved on real price data indicated by red crosses
We have two primary objectives as follows:
1.
To determine whether the net Sharpe ratio achieved by the network momentum model is significantly higher than that achieved by the MACD model when both are used to construct portfolios from the same price data set. We employ a one-sided Wilcoxon signed-rank test
[
55
]
, a matched-pair test, to assess if the difference in net Sharpe ratios (network momentum model minus MACD model) is significantly greater than 0.
2.
To examine whether the distributions of the net Sharpe ratios from the MACD model and a network momentum model are statistically different without considering the matched-pair nature of the data. We use the one-sided Kolmogorov-Smirnov test
[
56
]
to determine if the cumulative distribution function of the MACD model’s net Sharpe ratios is stochastically greater than that of the network momentum model, it indicates that the MACD model generally yields lower Sharpe ratios than the network momentum model.
We report the p-values for the two tests in Table
2
. For the Wilcoxon signed-rank test, all network momentum models achieve significant p-values (
p
<
0.05
𝑝
0.05
p<0.05
italic_p < 0.05
). This demonstrates that, when applied to the same random set of market price data, the network momentum models significantly outperform the benchmark MACD model, which relies only on time-series momentum in terms of net Sharpe ratio. For the Kolmogorov-Smirnov test, aside from NMM-SDDTW, all other NMM models achieve significant p-values (
p
<
0.05
𝑝
0.05
p<0.05
italic_p < 0.05
). This indicates that the cumulative distribution function of the net Sharpe ratios for the network momentum models is stochastically smaller than that of the MACD model, suggesting that the network momentum models generally achieve higher Sharpe ratios than the MACD model. These two tests collectively underscore the enhanced performance capability of the network momentum feature.
Our results demonstrate the robustness and reliability of the network momentum spillover identified by the proposed algorithms. These findings suggest that under both uniform and varied market conditions, the NMM models consistently outperform the benchmark MACD model with statistically confidence.
Table 2:
P-Values for Sharpe Ratio Comparisons Against Benchmark
NMM-DTW
NMM-DTW-E
NMM-DDTW
NMM-DDTW-E
NMM-SDTW
NMM-SDTW-E
NMM-SDDTW
NMM-SDDTW-E
NMM-LEVY
NMM-LEVY-E
Wilcoxon signed-rank test
0
0
0
0
0
0
0.005
0
0
0
Kolmogorov–Smirnov test
0.018
0
0.012
0
0.005
0
0.077
0
0
0.002
6.2
Long/Short Performance Analysis
In this section, We focus on the model’s ability to identify and respond to upward and downward market trends by examining performance in both long and short trading positions. The returns from these positions are analysed separately, with the metrics for short and long positions detailed in Tables
3
and
4
, respectively.
Table 3:
Performance Metrics for Various Signals in Short Direction Only
Gross
Transaction
Net
vol.
Sharpe
downside
MDD
Sortino
Calmar
Skewness
hit rate
Avg. P
Return
Return
deviation
Avg. L
Panel A: Average Performance on 100 Bootstrapped Price Data
MACD
-0.011
0.014
-0.026
0.066
-0.396
0.040
0.635
-0.638
-0.012
0.804
0.367
1.245
NMM-DTW
-0.005
0.014
-0.020
0.062
-0.329
0.039
0.546
-0.513
-0.010
0.885
0.351
1.379
NMM-DTW-E
-0.006
0.012
-0.018
0.058
-0.317
0.037
0.519
-0.490
-0.010
1.007
0.342
1.427
NMM-DDTW
-0.006
0.014
-0.021
0.062
-0.340
0.038
0.553
-0.541
-0.011
1.042
0.349
1.385
NMM-DDTW-E
-0.005
0.011
-0.017
0.059
-0.300
0.037
0.508
-0.467
-0.010
1.155
0.341
1.449
NMM-SDTW
-0.007
0.014
-0.022
0.061
-0.364
0.038
0.562
-0.569
-0.011
0.996
0.348
1.358
NMM-SDTW-E
-0.006
0.011
-0.017
0.058
-0.310
0.037
0.500
-0.474
-0.010
1.035
0.342
1.439
NMM-SDDTW
-0.007
0.014
-0.021
0.061
-0.351
0.039
0.555
-0.544
-0.011
0.877
0.344
1.395
NMM-SDDTW-E
-0.007
0.011
-0.018
0.058
-0.317
0.037
0.510
-0.487
-0.010
1.130
0.333
1.468
NMM-LEVY
-0.008
0.014
-0.022
0.062
-0.363
0.039
0.573
-0.576
-0.011
0.814
0.357
1.327
NMM-LEVY-E
-0.010
0.012
-0.022
0.060
-0.374
0.038
0.575
-0.586
-0.011
0.861
0.341
1.387
Panel B: Performance on Real Price Data
MACD
-0.013
0.014
-0.028
0.070
-0.396
0.043
0.584
-0.645
-0.014
0.953
0.376
1.189
NMM-DTW
-0.007
0.014
-0.022
0.066
-0.327
0.040
0.525
-0.536
-0.012
1.218
0.342
1.430
NMM-DTW-E
-0.005
0.012
-0.016
0.064
-0.254
0.039
0.450
-0.410
-0.010
1.237
0.363
1.368
NMM-DDTW
-0.009
0.015
-0.023
0.066
-0.353
0.040
0.514
-0.578
-0.013
1.295
0.368
1.252
NMM-DDTW-E
-0.007
0.012
-0.019
0.063
-0.299
0.039
0.487
-0.484
-0.011
1.395
0.333
1.485
NMM-SDTW
-0.006
0.014
-0.020
0.067
-0.297
0.041
0.456
-0.483
-0.013
1.243
0.350
1.393
NMM-SDTW-E
-0.007
0.012
-0.018
0.062
-0.297
0.040
0.485
-0.465
-0.011
1.290
0.355
1.350
NMM-SDDTW
-0.012
0.014
-0.025
0.067
-0.381
0.041
0.553
-0.615
-0.013
1.164
0.312
1.575
NMM-SDDTW-E
-0.008
0.012
-0.019
0.062
-0.299
0.040
0.485
-0.469
-0.011
1.365
0.329
1.485
NMM-LEVY
-0.006
0.014
-0.020
0.067
-0.296
0.041
0.476
-0.487
-0.012
1.027
0.372
1.312
NMM-LEVY-E
-0.012
0.012
-0.024
0.065
-0.374
0.041
0.549
-0.595
-0.013
0.906
0.359
1.267
a
Best performance is in bold.
b
No comparison for volatility and downside deviation since every portfolio is scaled to the same target annualised volatility in
5.2
for direct comparison of the net Sharpe.
Based on the data in Panel A of Table
3
, the benchmark model MACD averages a loss in short positions on the bootstrapped dataset, with a net Sharpe of
−
0.396
0.396
-0.396
- 0.396
and the highest MDD across both bootstrapped and real price data. In contrast, NMM models improve performance in short positions by reducing losses. Specifically, NMM-DDTW-E enhances performance over MACD by reducing losses by 35% and increasing the net Sharpe ratio by 24% on bootstrapped data. It also achieves the highest Sortino and Calmar ratios, indicating effective downside risk and MDD control. Despite MACD’s higher hit rate in short positions, its skewness score of 0.804 is lower than that of NMM-DDTW-E, which scores 1.155, and other NMM models. This indicates that NMM models not only result in smaller losses but also achieve more substantial occasional gains.
In Panel B of Table
3
, NMM models continue to demonstrate effective loss control in short positions on the real price data. NMM-SDTW-E and NMM-DTW-E notably improve net Sharpe and reduce MDD to the greatest extent compared to the benchmark, respectively, with NMM-DDTW-E again achieving the most positively skewed performance, mirroring its success on bootstrapped datasets.
In the long direction, as detailed in Table
4
, MACD demonstrates strong profitability with a net Sharpe ratio of 0.559 with the highest hit rate at 0.554. Among the network momentum models, NMM-LEVY outperforms with a net Sharpe of 0.587, a 6.1% increase over the benchmark. It also reduces the MDD to 0.168, indicating superior loss control. Notably, while some network momentum models exhibit slightly lower net Sharpe ratios in long positions compared to the benchmark, all of them demonstrate more positively skewed returns, signifying smaller average losses and occasional larger gains. NMM-SDDTW achieves the most positively skewed returns, with a 76.6% increase over MACD’s skewness. This highlights the robust capability of network momentum models in long positions. Corresponding performance on the real price data in Panel B of Table
4
further supports this, showing that NMM-LEVY has a higher Sharpe and Sortino ratio compared to the benchmark, and NMM-SDDTW-E records the most skewed returns.
Table 4:
Performance Metrics for Various Signals in Long Direction Only
Gross
Transaction
Net
vol.
Sharpe
downside
MDD
Sortino
Calmar
Skewness
hit rate
Avg. P
Return
Return
deviation
Avg. L
Panel A: Average Performance on 100 Bootstrapped Price Data
MACD
0.068
0.012
0.055
0.099
0.559
0.057
0.191
0.983
0.091
0.367
0.554
1.243
NMM-DTW
0.069
0.015
0.054
0.100
0.540
0.054
0.186
0.998
0.093
0.553
0.519
1.412
NMM-DTW-E
0.069
0.012
0.057
0.099
0.572
0.054
0.172
1.053
0.103
0.574
0.519
1.451
NMM-DDTW
0.070
0.015
0.055
0.101
0.542
0.055
0.192
1.001
0.090
0.565
0.522
1.401
NMM-DDTW-E
0.068
0.012
0.056
0.099
0.568
0.054
0.173
1.053
0.101
0.594
0.518
1.459
NMM-SDTW
0.071
0.014
0.057
0.101
0.563
0.055
0.188
1.047
0.098
0.557
0.525
1.405
NMM-SDTW-E
0.068
0.011
0.056
0.099
0.569
0.054
0.171
1.056
0.102
0.602
0.518
1.460
NMM-SDDTW
0.069
0.015
0.053
0.100
0.529
0.054
0.193
0.997
0.088
0.648
0.517
1.417
NMM-SDDTW-E
0.068
0.012
0.056
0.099
0.569
0.053
0.172
1.066
0.102
0.648
0.517
1.468
NMM-LEVY
0.072
0.013
0.059
0.100
0.587
0.055
0.168
1.076
0.109
0.477
0.534
1.377
NMM-LEVY-E
0.069
0.012
0.057
0.098
0.582
0.054
0.161
1.076
0.110
0.542
0.527
1.415
Panel B: Performance on Real Price Data
MACD
0.064
0.012
0.052
0.094
0.557
0.049
0.199
1.065
0.076
0.623
0.547
1.276
NMM-DTW
0.062
0.014
0.047
0.096
0.494
0.048
0.216
0.995
0.063
0.901
0.500
1.479
NMM-DTW-E
0.067
0.011
0.055
0.095
0.581
0.047
0.158
1.170
0.100
0.918
0.491
1.649
NMM-DDTW
0.065
0.014
0.051
0.098
0.519
0.050
0.214
1.011
0.069
0.831
0.496
1.531
NMM-DDTW-E
0.062
0.011
0.051
0.095
0.535
0.048
0.193
1.052
0.076
0.868
0.517
1.418
NMM-SDTW
0.070
0.013
0.057
0.095
0.600
0.046
0.153
1.236
0.108
0.916
0.513
1.538
NMM-SDTW-E
0.062
0.011
0.051
0.094
0.543
0.047
0.170
1.088
0.087
0.904
0.500
1.545
NMM-SDDTW
0.061
0.014
0.046
0.098
0.470
0.047
0.220
0.972
0.060
0.850
0.500
1.441
NMM-SDDTW-E
0.065
0.011
0.053
0.095
0.565
0.047
0.168
1.139
0.092
0.935
0.504
1.550
NMM-LEVY
0.070
0.012
0.058
0.094
0.610
0.046
0.169
1.257
0.098
0.810
0.526
1.449
NMM-LEVY-E
0.068
0.011
0.056
0.094
0.597
0.047
0.155
1.187
0.105
0.813
0.517
1.495
a
Best performance is in bold.
b
No comparison for volatility and downside deviation since every portfolio is scaled to the same target annualised volatility in
5.2
for direct comparison of the net Sharpe.
6.3
Diversification Analysis
We analyse the correlation of their returns to assess whether the NMM models and MACD exhibit orthogonal trading signals. Figure
2(a)
presents the average correlation on bootstrapped datasets, while Figure
2(b)
the correlation on real price data covering the entire out-of-sample period from 2005 to 2024.
(a)
(b)
Figure 2:
A diversification analysis on the PnL pairwise correlation between models on the bootstrapped datasets (left) and real price dataset (right).
By analysing the returns between the NMM models and the benchmark MACD on the bootstrapped datasets, we notice that the average correlations range from 0.71 to 0.89 in Figure
2(a)
. NMM-SDDTW exhibits the lowest average correlation with MACD at 0.71, similar to NMM-DDTW’s correlation with MACD. NMM-LEVY and NMM-LEVY-E show slightly higher correlations with MACD at 0.87 and 0.89, respectively. Comparable results are observed in the PnL from the real price data between 2005 and 2024 in Figure
2(b)
, where NMM-DDTW and NMM-SDDTW display the lowest correlations with MACD, at 0.72 and 0.74, respectively. Although the PnL correlations are not completely orthogonal, these empirical findings support the existence of additional information captured in our NMM models.
Our empirical findings indicate that different DTW algorithms capture distinct lead-lag relationships, consequently influencing the network momentum identified. Specifically, NMM models employing multi-dimensional DTW algorithms, such as NMM-SDTW and NMM-SDDTW, exhibit lower correlation values, around 0.7, with models based on one-dimensional DTW algorithms like NMM-DTW and NMM-DDTW in Figure
2(a)
. This suggests that multi-dimensional DTW effectively captures different lead-lag relationships with one-dimensional approaches. Furthermore, NMM-LEVY demonstrates correlations ranging from 0.73 to 0.83 with NMM-DTW and its variations, indicating that using the Lévy area as a lead-lag detection method yields additional results from those obtained via dynamic time warping algorithms.
It is also noteworthy that correlations between each NMM model and its ensemble variant range from 0.80 to 0.92. This implies that while there is some dependency, the ensemble method still introduces different information on the lead-lag relationship. This is achieved by utilising six different lookback lengths, leading to variations in the network momentum model outcomes. The ensemble approach thus contributes uniquely to understanding and leveraging network momentum in trading strategies.
Next, we introduce a second metric for our diversification analysis: the sign agreement between two models. This metric is the percentage of days on which two models share the same trading direction—either opting to go long or short on the market on a trading day—across the entire portfolio. The average results on bootstrapped data is presented in Figure
3(a)
, with performance on real price data from the entire out-of-sample period from 2005 to 2024 in Figure
3(b)
. We also examine the average annualised expected PnL on days when the NMM models diverge in sign from the benchmark MACD model to assess whether differences in trading direction result in additional profits. The differences in average profits between the models (network momentum models minus MACD) for these days are detailed in Table
5
.
(a)
(b)
Figure 3:
A diversification analysis on the pairwise sign agreement between models on the bootstrapped datasets (left) and real price dataset (right)
Table 5:
Average PnL Gains Over Benchmark on Opposing Signal Days
NMM-DTW
NMM-DTW-E
NMM-DDTW
NMM-DDTW-E
NMM-SDTW
NMM-SDTW-E
NMM-SDDTW
NMM-SDDTW-E
NMM-LEVY
NMM-LEVY-E
Bootstrapped data
0.011
0.032
0.001
0.023
-0.002
0.021
-0.020
0.026
0.013
0.004
Real Price data
0.017
0.043
-0.017
0.015
0.057
-0.008
-0.063
0.039
0.051
0.031
It can be observed that NMM-DTW and NMM-DDTW, with the lowest sign agreement with the MACD at 85%, result in average additional returns of 0.011 and 0.032, respectively. Most other NMM models show a sign agreement ranging from 85% to 90% with MACD and generally yield higher returns than MACD on days with differing signs, except for NMM-SDTW and NMM-SDDTW, which achieve less profits than the benchmark on these days. These empirical results suggest that the additional network momentum captured by the NMM models is effective at following and adjusting to trends identified by the MACD, which focuses solely on time-series momentum. This indicates that our models are robust and effective in identifying network momentum within a portfolio.
On the real price data from 2005 to 2024, it is notable that the NMM-SDTW achieves the highest average returns gain over the benchmark model with an annualised expected difference at 0.057, with a sign agreement of 86%. However, NMM-DDTW, NMM-SDTW-E, and NMM-SDDTW realise lower profits compared to MACD, with respective losses of -0.017, -0.008 and -0.063, respectively, , and sign agreements of 85%, 90%, and 85%.
6.4
Skewness Analysis
In this final section on performance analysis, we examine the skewness of returns from NMM models across different time horizons and compare them with the benchmark MACD model.
As highlighted in
[
38
]
, effective trend-following strategies often exhibit a long-option-type payoff, attributed to positive skewness. This phenomenon can be conceptualised as the purchase of an option: regular small losses represent the premium paid, while correctly identifying and riding a trend may result in significant gains, analogous to an option’s payoff.
We present the skewness across various return horizons for four NMM models in Figure
4
for detailed analysis. The four representative network momentum models are: NMM-DTW-E (a), which performs the best on real price data and achieves the highest average PnL gain over the benchmark on days with opposing signals; NMM-DDTW-E (b), the top performer on bootstrapped data and in short positions; NMM-SDTW-E (c), which outperforms the other multi-dimensional dynamic time warping models; and NMM-LEVY (d), the top performer in long positions. Our empirical study finds that the skewness of the network momentum models exhibits a similar and consistent pattern; therefore, we only include four examples here. For completeness, we include plots for the other NMM models in Appendix
7.2
.
Our analysis indicates that the NMM models exhibit stronger positive skewness in returns across time horizons ranging from days to months compared to the benchmark MACD, with a notable peak at the two-month return horizon. This suggests that the NMM models are more effective at identifying and positioning for trends ahead of time to capitalise on these opportunities. Even in the case of daily returns, where all models typically exhibit a negative skew due to the option-like payoff characteristic of trend-following strategies, the NMM models show less negative skewness, indicating better risk control and the ability to identify short-term trends without enduring prolonged periods of losses.
However, it is important to note that the NMM models demonstrate a more pronounced decay in skewness over longer horizons compared to MACD. Particularly from half-year to one-year return horizons, although the NMM models still maintain positive skewness, it is less pronounced than that observed with MACD.
The pattern of skewness across different time horizons aligns with the findings reported in
[
38
]
, which we refer interested readers to for further details. Our empirical results suggest that NMM models not only uphold the desired characteristics of a trend-following strategy but also enhance them. They effectively capture network momentum spillover and identify both short-term and medium-term trends accurately, thereby enabling the models to anticipate market movements by considering momentum from interconnected markets within the portfolio.
(a)
(b)
(c)
(d)
Figure 4:
Skewness in the returns of the network momentum model over various periods, compared to those of the time series momentum model, using different lead-lag detection models: (a) NMM-DTW-E, (b) NMM-DDTW-E, (c) NMM-SDTW-E, and (d) NMM-LEVY.
7
Conclusion
We propose a methodology that transforms cross-sectional momentum spillover into network momentum across market industries. This process utilises two lead-lag detection models to identify non-linear relationships at fixed lags and between non-synchronised market returns. We then apply a graph learning model to quantify the intricate interconnectedness of market leadership and individual momentum, generating a novel trading signal. This signal is utilised to construct a portfolio for a systematic trend-following strategy, which we evaluate using 100 sets of bootstrapped price data from 28 futures contracts across metals, agriculture, energy, and equities. We backtest our strategy in a realistic trading environment that accounts for time delays in establishing positions.
Our framework enhances the performance of traditional trend-following strategies, consistently achieving a higher and statistically significant net Sharpe ratio compared to time series momentum strategies. Our model also robustly reduces transaction costs and enhances performance over time series momentum strategies in short positions, where the latter typically incurs losses. By employing various lead-lag detection techniques, our network momentum models generate low-correlated signals that more effectively identify market trends by establishing positions in the correct direction. The proposed framework also consistently yields more positively skewed returns, underscoring the efficiency and robustness of the network momentum identified for trend-following strategies.
Most importantly, the results indicate that the superior performance of converting cross-sectional momentum into network momentum is not confined to specific market combinations within the portfolio, nor is it dependent on historical market trends. Instead, the proposed network momentum model demonstrates remarkable generalisability across various industries and markets.
We propose several future research directions. Firstly, exploring non-linear ensemble methods on the lead-lag matrices computed by multiple models could be beneficial. Considering that the divergence analysis indicates dynamic time warping and Lévy area models capture different information, their combination in a non-linear manner may enhance the identification of lead-lag relationships. Secondly, investigating asymmetrical adjacency matrices with machine learning models like graph neural networks could shed light on potential non-symmetrical relationships between markets. Thirdly, while our current portfolio construction combines time series momentum features with equal weights and applies the same adjacency matrix to all of them, it may be worthwhile to explore fitting different lead-lag matrices and adjacency matrices to time series momentum features at varying speeds. Employing non-linear methods to combine these may more effectively capture the nonlinearity in momentum spillover.
Acknowledgments
I would like to thank Dr William Ferreira, Leonardo Marroni, Irene Perdomo and Lorenzo Reati for the opportunity to undertake this project.
Appendix
7.1
Dataset Details
In Table
6
, we summarise the Bloomberg tickers and names of all the futures contracts we used in our portfolio.
Bloomberg Ticker
Contract Name
Market Class
future_bo1_comdty
CBOT Soybean Oil Future
Ags
future_sm1_comdty
CBOT Soybean Meal Future
Ags
future_sb1_comdty
NYBOT CSC Number 11 World Sugar Future
Ags
future_rr1_comdty
Rough Rice Future
Ags
future_o_1_comdty
Oats Future
Ags
future_mw1_comdty
MGE Red Wheat Future
Ags
future_kw1_comdty
KCBT Hard Red Winter Wheat Future
Ags
future_kc1_comdty
NYBOT CSC C Coffee Future
Ags
future_jo1_comdty
Orange Juice (RTH) Future
Ags
future_w_1_comdty
CBOT Wheat Future
Ags
future_c_1_comdty
CBOT Corn Future
Ags
future_cc1_comdty
NYBOT CSC Cocoa Future
Ags
future_da1_comdty
Class III Milk Future
Ags
future_ct1_comdty
NYBOT CTN Number 2 Cotton Future
Ags
future_cl1_comdty
NYMEX Light Sweet Crude Oil Future
Energy
future_co1_comdty
ICE Brent Crude Oil Future
Energy
future_ng1_comdty
NYMEX Henry Hub Natural Gas Future
Energy
future_cf1_index
Euronext CAC 40 Index Future
Equity
future_nq1_index
CME E-Mini NASDAQ 100 Index Future
Equity
future_vg1_index
Eurex EURO STOXX 50 Future
Equity
future_hi1_index
HKG Hang Seng Index Future
Equity
future_gx1_index
Eurex DAX Index Future
Equity
future_es1_index
CME E-Mini Standard & Poor’s 500 Future
Equity
future_pa1_comdty
NYMEX Palladium Future
Metals
future_pl1_comdty
NYMEX Platinum Future
Metals
future_hg1_comdty
COMEX Copper Future
Metals
future_si1_comdty
COMEX Silver Future
Metals
future_gc1_comdty
COMEX Gold 100 Troy Ounces Future
Metals
Table 6:
Futures Contracts from Bloomberg
7.2
Supplementary Skewness Plots Across Time Horizons
In Figure
5
, we include skewness plots for additional network momentum models not presented in Section
6.4
.
(a)
(b)
(c)
(d)
(e)
(f)
Figure 5:
Supplementary plots of skewness in the returns of the network momentum model over various periods, compared to those of the time series momentum model, using different lead-lag detection models.
References
[1]
Brian Hurst, Yao Hua Ooi, and Lasse Heje Pedersen.
A century of evidence on trend-following investing.
Available at SSRN 2993026
, 2017.
[2]
Laura Xiaolei Liu and Lu Zhang.
Momentum Profits, Factor Pricing, and Macroeconomic Risk.
The Review of Financial Studies
, 21(6):2417–2448, 10 2008.
[3]
Harrison Hong and Jeremy C. Stein.
A unified theory of underreaction, momentum trading and overreaction in asset markets.
Journal of Finance
, LIV(6):2143–2184., 1999.
[4]
Kewei Hou.
Industry information diffusion and the lead-lag effect in stock returns.
The review of financial studies
, 20(4):1113–1138, 2007.
[5]
Nicholas Barberis, Andrei Shleifer, and Robert Vishny.
A model of investor sentiment.
Journal of financial economics
, 49(3):307–343, 1998.
[6]
Dimitri Vayanos and Paul Woolley.
An institutional theory of momentum and reversal.
The Review of Financial Studies
, 26(5):1087–1145, 2013.
[7]
Narasimhan Jegadeesh and Sheridan Titman.
Returns to buying winners and selling losers: Implications for stock market efficiency.
The Journal of finance
, 48(1):65–91, 1993.
[8]
K Geert Rouwenhorst.
International momentum strategies.
The journal of finance
, 53(1):267–284, 1998.
[9]
Andy Chui, Sheridan Titman, and KC John Wei.
Momentum, ownership structure, and financial crises: An analysis of asian stock markets.
wp University of Texas at Austin
, 2000.
[10]
Narasimhan Jegadeesh and Sheridan Titman.
Profitability of momentum strategies: An evaluation of alternative explanations.
The Journal of finance
, 56(2):699–720, 2001.
[11]
William R Gebhardt, Soeren Hvidkjaer, and Bhaskaran Swaminathan.
Stock and bond market interaction: Does momentum spill over?
Journal of Financial Economics
, 75(3):651–690, 2005.
[12]
Andrew W. Lo and A. Craig MacKinlay.
When are contrarian profits due to stock market overreaction?
The Review of Financial Studies
, 3(2):175–205, 1990.
[13]
S. G. Badrinath, Jayant R. Kale, and Thomas H. Noe.
Of shepherds, sheep, and the cross-autocorrelations in equity returns.
The Review of Financial Studies
, 8(2):401–430, 1995.
[14]
Michael J Brennan, Narasimhan Jegadeesh, and Bhaskaran Swaminathan.
Investment analysis and the adjustment of stock prices to common information.
The Review of Financial Studies
, 6(4):799–824, 1993.
[15]
Tobias J Moskowitz and Mark Grinblatt.
Do industries explain momentum?
The Journal of finance
, 54(4):1249–1290, 1999.
[16]
Klaus Grobys, Joni Ruotsalainen, and Janne Äijö.
Risk-managed industry momentum and momentum crashes.
Quantitative Finance
, 18(10):1715–1733, 2018.
[17]
John Y Campbell, Andrew W Lo, A Craig MacKinlay, and Robert F Whitelaw.
The econometrics of financial markets.
Macroeconomic Dynamics
, 2(4):559–562, 1998.
[18]
Di Wu, Yiping Ke, Jeffrey Xu Yu, Philip S Yu, and Lei Chen.
Detecting leaders from correlated time series.
In
Database Systems for Advanced Applications: 15th International Conference, DASFAA 2010, Tsukuba, Japan, April 1-4, 2010, Proceedings, Part I 15
, pages 352–367. Springer, 2010.
[19]
Stefanos Bennett, Mihai Cucuringu, and Gesine Reinert.
Lead-lag detection and network clustering for multivariate time series with an application to the us equity market, 2022.
[20]
Maurice G Kendall.
A new measure of rank correlation.
Biometrika
, 30(1-2):81–93, 1938.
[21]
Gábor J Székely, Maria L Rizzo, and Nail K Bakirov.
Measuring and testing dependence by correlation of distances.
2007.
[22]
Paweł Fiedor.
Information-theoretic approach to lead-lag effect on financial markets.
The European Physical Journal B
, 87:1–9, 2014.
[23]
Monica Billio, Mila Getmansky, Andrew W Lo, and Loriana Pelizzon.
Econometric measures of connectedness and systemic risk in the finance and insurance sectors.
Journal of financial economics
, 104(3):535–559, 2012.
[24]
Donghua Wang, Jingqing Tu, Xiaohui Chang, and Saiping Li.
The lead–lag relationship between the spot and futures markets in china.
Quantitative Finance
, 17(9):1447–1456, 2017.
[25]
Gautier Marti, Sébastien Andler, Frank Nielsen, and Philippe Donnat.
Exploring and measuring non-linear correlations: Copulas, lightspeed transportation and clustering.
In
NIPS 2016 Time Series Workshop
, pages 59–69. PMLR, 2017.
[26]
Álvaro Cartea, Mihai Cucuringu, and Qi Jin.
Detecting lead-lag relationships in stock returns and portfolio strategies.
Available at SSRN
, 2023.
[27]
Usman Ali and David Hirshleifer.
Shared analyst coverage: Unifying momentum spillover effects.
Journal of Financial Economics
, 136(3):649–675, 2020.
[28]
Yichi Zhang, Mihai Cucuringu, Alexander Y. Shestopaloff, and Stefan Zohren.
Dynamic time warping for lead-lag relationships in lagged multi-factor models, 2023.
[29]
Lasko Basnarkov, Viktor Stojkoski, Zoran Utkovski, and Ljupco Kocarev.
Lead–lag relationships in foreign exchange markets.
Physica A: Statistical Mechanics and its Applications
, 539:122986, 2020.
[30]
Wee Ling Tan, Stephen Roberts, and Stefan Zohren.
Spatio-temporal momentum: Jointly learning time-series and cross-sectional strategies.
arXiv preprint arXiv:2302.10175
, 2023.
[31]
Daniel Poh, Bryan Lim, Stefan Zohren, and Stephen Roberts.
Building cross-sectional systematic strategies by learning to rank.
arXiv preprint arXiv:2012.07149
, 2020.
[32]
Xingyue Pu, Stephen Roberts, Xiaowen Dong, and Stefan Zohren.
Network momentum across asset classes, 2023.
[33]
Daniel Haesen, Patrick Houweling, and Jeroen van Zundert.
Momentum spillover from stocks to corporate bonds.
Journal of Banking & Finance
, 79:28–41, 2017.
[34]
Philippe Declerck.
Trend-following and spillover effects.
Available at SSRN 3473657
, 2019.
[35]
Ehab Abdel-Tawab Yamani and Moustafa Abuelfadl.
Currency news and international bond markets.
North American Journal of Economics and Finance
, 2021.
[36]
Adrian Fernandez-Perez, Ivan Indriawan, Yiuman Tse, and Yahua Xu.
Cross-asset time-series momentum: Crude oil volatility and global stock markets.
Journal of Banking & Finance
, 154:106704, 2023.
[37]
Rei Yamamoto, Naoya Kawadai, and Hiroki Miyahara.
Momentum information propagation through global supply chain networks.
Journal of Portfolio Management
, 47(8):197–211, 2021.
[38]
Richard J. Martin.
Design and analysis of momentum trading strategies, 2023.
[39]
Taras K Vintsyuk.
Speech discrimination by dynamic programming.
Cybernetics
, 4(1):52–57, 1968.
[40]
A. Varfis, L. Corleto, J.M. Auger, D. Perrotta, and M. Alvarez.
Lead-lag estimation by means of the dynamic time warping technique.
Research in Official Statistics (European Communities)
, page 5, 2001.
[41]
Lajos Gergely Gyurkó, Terry Lyons, Mark Kontkowski, and Jonathan Field.
Extracting information from the signature of a financial data stream, 2014.
[42]
Ilya Chevyrev and Andrey Kormilitzin.
A primer on the signature method in machine learning, 2016.
[43]
Johannes Stübinger and Dominik Walter.
Using multi-dimensional dynamic time warping to identify time-varying lead-lag relationships.
Sensors
, 22(18), 2022.
[44]
Meinard Müller.
Dynamic time warping.
Information Retrieval for Music and Motion
, 2:69–84, 01 2007.
[45]
Eamonn J. Keogh and Michael J. Pazzani.
Derivative dynamic time warping.
In
Proceedings of the 2001 SIAM International Conference on Data Mining
, pages 1–11, Chicago, IL, USA, April 5–7 2001. Society for Industrial and Applied Mathematics.
[46]
Thanawin Rakthanmanon, Bilson Campana, Abdullah Mueen, Gustavo Batista, Brandon Westover, Qiang Zhu, Jesin Zakaria, and Eamonn Keogh.
Addressing big data time series: Mining trillions of time series subsequences under dynamic time warping.
ACM Transactions on Knowledge Discovery from Data (TKDD)
, 7(3):1–31, 2013.
[47]
Jiaping Zhao and Laurent Itti.
shapedtw: Shape dynamic time warping.
Pattern Recognition
, 74:171–184, 2018.
[48]
Mohammad Shokoohi-Yekta, Bing Hu, Hongxia Jin, Jun Wang, and Eamonn Keogh.
Generalizing dtw to the multi-dimensional case requires an adaptive approach.
Data mining and knowledge discovery
, 31:1–31, 2017.
[49]
Vassilis Kalofolias.
How to learn a graph from smooth signals.
In
Artificial intelligence and statistics
, pages 920–929. PMLR, 2016.
[50]
Steven Diamond and Stephen Boyd.
Cvxpy: A python-embedded modeling language for convex optimization, 2016.
[51]
Dimitris N. Politis and Joseph P. Romano.
The stationary bootstrap.
Journal of the American Statistical Association
, 89(428):1303–1313, 1994.
[52]
Radovan Vojtko and Matus Padysak.
Continuous futures contracts methodology for backtesting, 2019.
[53]
Jamil Baz, Nicolas Granger, Campbell Harvey, Nicolas Roux, and Sandy Rattray.
Dissecting investment strategies in the cross section and time series.
SSRN Electronic Journal
, 01 2015.
[54]
Bryan Lim, Stefan Zohren, and Stephen Roberts.
Enhancing time series momentum strategies using deep neural networks, 2020.
[55]
Frank Wilcoxon.
Individual comparisons by ranking methods.
In
Breakthroughs in statistics: Methodology and distribution
, pages 196–202. Springer, 1992.
[56]
Vance W Berger and YanYan Zhou.
Kolmogorov–smirnov test: Overview.
Wiley statsref: Statistics reference online
, 2014.