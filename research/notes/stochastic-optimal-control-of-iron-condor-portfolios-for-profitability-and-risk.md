---
title: Stochastic Optimal Control of Iron Condor Portfolios for Profitability and
  Risk Management
id: stochastic-optimal-control-of-iron-condor-portfolios-for-profitability-and-risk
tags:
- exit-strategy
created: '2026-06-17T20:05:43.707077Z'
updated: '2026-06-17T20:28:22.893304Z'
source: https://arxiv.org/html/2501.12397
source_domain: arxiv.org
fetched_at: '2026-06-17T20:05:43.706882Z'
fetch_provider: builtin
status: review
type: note
tier: institutional
content_type: paper
deprecated: false
summary: Stochastic Optimal Control of Iron Condor Portfolios for Profitability and
  Risk Management
---

Stochastic Optimal Control of Iron Condor Portfolios for Profitability and Risk Management
Stochastic Optimal Control of Iron Condor Portfolios for Profitability and Risk Management
Hanyue Huang
Qiguo Sun
Xibei Yang
Abstract
Previous research on option strategies has primarily focused on their behavior near expiration, with limited attention to the transient value process of the portfolio. In this paper, we formulate Iron Condor portfolio optimization as a stochastic optimal control problem, examining the impact of the control process
u
⁢
(
k
i
,
τ
)
𝑢
subscript
𝑘
𝑖
𝜏
u(k_{i},\tau)
italic_u ( italic_k start_POSTSUBSCRIPT italic_i end_POSTSUBSCRIPT , italic_τ )
on the portfolio’s potential profitability and risk. By assuming the underlying price process as a bounded martingale within
[
K
1
,
K
2
]
subscript
𝐾
1
subscript
𝐾
2
[K_{1},K_{2}]
[ italic_K start_POSTSUBSCRIPT 1 end_POSTSUBSCRIPT , italic_K start_POSTSUBSCRIPT 2 end_POSTSUBSCRIPT ]
, we prove that the portfolio with a strike structure of
k
1
<
k
2
=
K
2
<
S
t
<
k
3
=
K
3
<
k
4
subscript
𝑘
1
subscript
𝑘
2
subscript
𝐾
2
subscript
𝑆
𝑡
subscript
𝑘
3
subscript
𝐾
3
subscript
𝑘
4
k_{1}<k_{2}=K_{2}<S_{t}<k_{3}=K_{3}<k_{4}
italic_k start_POSTSUBSCRIPT 1 end_POSTSUBSCRIPT < italic_k start_POSTSUBSCRIPT 2 end_POSTSUBSCRIPT = italic_K start_POSTSUBSCRIPT 2 end_POSTSUBSCRIPT < italic_S start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT < italic_k start_POSTSUBSCRIPT 3 end_POSTSUBSCRIPT = italic_K start_POSTSUBSCRIPT 3 end_POSTSUBSCRIPT < italic_k start_POSTSUBSCRIPT 4 end_POSTSUBSCRIPT
has a submartingale value process, which results in the optimal stopping time aligning with the expiration date
τ
=
T
𝜏
𝑇
\tau=T
italic_τ = italic_T
.
Moreover, we construct a data generator based on the Rough Heston model to investigate general scenarios through simulation. The results show that asymmetric, left-biased Iron Condor portfolios with
τ
=
T
𝜏
𝑇
\tau=T
italic_τ = italic_T
are optimal in SPX markets, balancing profitability and risk management. Deep out-of-the-money strategies improve profitability and success rates at the cost of introducing extreme losses, which can be alleviated by using an optimal stopping strategy. Except for the left-biased portfolios
τ
𝜏
\tau
italic_τ
generally falls within the range of [50%,75%] of total duration. In addition, we validate these findings through case studies on the actual SPX market, covering bullish, sideways, and bearish market conditions.
keywords:
Iron Condor , Fractional Brownian Motion , Rough Heston , Optimal Stopping Time
\affiliation
[label1]organization=School of Computer, Jiangsu University of Science and Technology,
city=Zhenjiang,
postcode=212003,
state=Jiangsu Province,
country=China
\affiliation
[label2]organization=Technical University of Munich,
city=Munich,
postcode=80333,
state=Bavaria,
country=Germany
1
Introduction
Option portfolio optimization can be formulated as a stochastic optimal control problem, where the portfolio value process indicated by
X
t
subscript
𝑋
𝑡
X_{t}
italic_X start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT
is a state process, which can be partly controlled by some control process
u
𝑢
u
italic_u
.
For a given initial point
x
0
∈
ℝ
n
subscript
𝑥
0
superscript
ℝ
𝑛
x_{0}\in\mathbb{R}^{n}
italic_x start_POSTSUBSCRIPT 0 end_POSTSUBSCRIPT ∈ blackboard_R start_POSTSUPERSCRIPT italic_n end_POSTSUPERSCRIPT
, we consider
X
t
subscript
𝑋
𝑡
X_{t}
italic_X start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT
as a controlled stochastic differential equations:
{
d
⁢
X
t
=
μ
⁢
(
t
,
X
t
,
u
t
)
⁢
d
⁢
t
+
σ
⁢
(
t
,
X
t
,
u
t
)
⁢
d
⁢
W
t
,
X
0
=
x
0
cases
𝑑
subscript
𝑋
𝑡
𝜇
𝑡
subscript
𝑋
𝑡
subscript
𝑢
𝑡
𝑑
𝑡
𝜎
𝑡
subscript
𝑋
𝑡
subscript
𝑢
𝑡
𝑑
subscript
𝑊
𝑡
otherwise
subscript
𝑋
0
subscript
𝑥
0
otherwise
\begin{cases}dX_{t}=\mu(t,X_{t},u_{t})dt+\sigma(t,X_{t},u_{t})dW_{t},\\
X_{0}=x_{0}\end{cases}
{ start_ROW start_CELL italic_d italic_X start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT = italic_μ ( italic_t , italic_X start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT , italic_u start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT ) italic_d italic_t + italic_σ ( italic_t , italic_X start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT , italic_u start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT ) italic_d italic_W start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT , end_CELL start_CELL end_CELL end_ROW start_ROW start_CELL italic_X start_POSTSUBSCRIPT 0 end_POSTSUBSCRIPT = italic_x start_POSTSUBSCRIPT 0 end_POSTSUBSCRIPT end_CELL start_CELL end_CELL end_ROW
(1)
where
μ
𝜇
\mu
italic_μ
and
σ
𝜎
\sigma
italic_σ
are some given functions with
{
μ
:
ℝ
+
×
ℝ
n
×
ℝ
k
→
ℝ
n
,
σ
:
ℝ
+
×
ℝ
n
×
ℝ
k
→
ℝ
n
×
d
cases
:
𝜇
→
superscript
ℝ
superscript
ℝ
𝑛
superscript
ℝ
𝑘
superscript
ℝ
𝑛
otherwise
:
𝜎
→
superscript
ℝ
superscript
ℝ
𝑛
superscript
ℝ
𝑘
superscript
ℝ
𝑛
𝑑
otherwise
\begin{cases}\mu:\mathbb{R}^{+}\times\mathbb{R}^{n}\times\mathbb{R}^{k}%
\rightarrow\mathbb{R}^{n},\\
\sigma:\mathbb{R}^{+}\times\mathbb{R}^{n}\times\mathbb{R}^{k}\rightarrow%
\mathbb{R}^{n\times d}\end{cases}
{ start_ROW start_CELL italic_μ : blackboard_R start_POSTSUPERSCRIPT + end_POSTSUPERSCRIPT × blackboard_R start_POSTSUPERSCRIPT italic_n end_POSTSUPERSCRIPT × blackboard_R start_POSTSUPERSCRIPT italic_k end_POSTSUPERSCRIPT → blackboard_R start_POSTSUPERSCRIPT italic_n end_POSTSUPERSCRIPT , end_CELL start_CELL end_CELL end_ROW start_ROW start_CELL italic_σ : blackboard_R start_POSTSUPERSCRIPT + end_POSTSUPERSCRIPT × blackboard_R start_POSTSUPERSCRIPT italic_n end_POSTSUPERSCRIPT × blackboard_R start_POSTSUPERSCRIPT italic_k end_POSTSUPERSCRIPT → blackboard_R start_POSTSUPERSCRIPT italic_n × italic_d end_POSTSUPERSCRIPT end_CELL start_CELL end_CELL end_ROW
(2)
where
W
𝑊
W
italic_W
is a d-dimensional Wiener process. The control process
u
𝑢
u
italic_u
is defined to be adapted to the
X
t
subscript
𝑋
𝑡
X_{t}
italic_X start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT
, denoted by
𝐮
⁢
(
t
,
X
t
)
𝐮
𝑡
subscript
𝑋
𝑡
\mathbf{u}(t,X_{t})
bold_u ( italic_t , italic_X start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT )
.
Moreover, it is also an admissible function in the admissible control law classes
U
𝑈
U
italic_U
for all
t
∈
ℝ
+
𝑡
superscript
ℝ
t\in\mathbb{R}^{+}
italic_t ∈ blackboard_R start_POSTSUPERSCRIPT + end_POSTSUPERSCRIPT
and
x
∈
ℝ
n
𝑥
superscript
ℝ
𝑛
x\in\mathbb{R}^{n}
italic_x ∈ blackboard_R start_POSTSUPERSCRIPT italic_n end_POSTSUPERSCRIPT
.
In real applications, the state process
X
t
subscript
𝑋
𝑡
X_{t}
italic_X start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT
must reside within a fixed domain. Consider a general situation where all options in the portfolio share the same expiration date,
T
𝑇
T
italic_T
. Moreover, let us assume a group of option strategies where the maximum potential loss and maximum potential gain are predetermined at the initial state
t
=
0
𝑡
0
t=0
italic_t = 0
. This setup naturally forms a domain for such a class of option portfolios, denoted as
D
⊆
[
0
,
T
]
×
ℝ
n
𝐷
0
𝑇
superscript
ℝ
𝑛
D\subseteq[0,T]\times\mathbb{R}^{n}
italic_D ⊆ [ 0 , italic_T ] × blackboard_R start_POSTSUPERSCRIPT italic_n end_POSTSUPERSCRIPT
.
When the state process reaches the boundary
∂
D
𝐷
\partial D
∂ italic_D
, the investment terminates. Thus, we define the stopping time as follows:
τ
=
inf
{
t
≥
0
∣
(
t
,
X
t
)
∈
∂
D
}
∧
T
,
𝜏
infimum
conditional-set
𝑡
0
𝑡
subscript
𝑋
𝑡
𝐷
𝑇
\tau=\inf\{t\geq 0\mid(t,X_{t})\in\partial D\}\wedge T,
italic_τ = roman_inf { italic_t ≥ 0 ∣ ( italic_t , italic_X start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT ) ∈ ∂ italic_D } ∧ italic_T ,
(3)
where
x
∧
y
=
min
⁡
(
x
,
y
)
𝑥
𝑦
𝑥
𝑦
x\wedge y=\min(x,y)
italic_x ∧ italic_y = roman_min ( italic_x , italic_y )
.
Furthermore, we define the instantaneous utility function as
F
⁢
(
t
,
x
,
u
)
𝐹
𝑡
𝑥
𝑢
F(t,x,u)
italic_F ( italic_t , italic_x , italic_u )
and the terminal utility (or bequest) as
Φ
⁢
(
τ
,
X
τ
)
Φ
𝜏
subscript
𝑋
𝜏
\Phi(\tau,X_{\tau})
roman_Φ ( italic_τ , italic_X start_POSTSUBSCRIPT italic_τ end_POSTSUBSCRIPT )
, where
Φ
:
∂
D
→
ℝ
:
Φ
→
𝐷
ℝ
\Phi:\partial D\to\mathbb{R}
roman_Φ : ∂ italic_D → blackboard_R
. The optimization problem is then to maximize the following expected value:
𝔼
⁢
[
∫
0
τ
F
⁢
(
s
,
X
s
,
u
s
)
⁢
𝑑
s
+
Φ
⁢
(
τ
,
X
τ
)
]
.
𝔼
delimited-[]
superscript
subscript
0
𝜏
𝐹
𝑠
subscript
𝑋
𝑠
subscript
𝑢
𝑠
differential-d
𝑠
Φ
𝜏
subscript
𝑋
𝜏
\mathbb{E}\left[\int_{0}^{\tau}F(s,X_{s},u_{s})\,ds+\Phi(\tau,X_{\tau})\right].
blackboard_E [ ∫ start_POSTSUBSCRIPT 0 end_POSTSUBSCRIPT start_POSTSUPERSCRIPT italic_τ end_POSTSUPERSCRIPT italic_F ( italic_s , italic_X start_POSTSUBSCRIPT italic_s end_POSTSUBSCRIPT , italic_u start_POSTSUBSCRIPT italic_s end_POSTSUBSCRIPT ) italic_d italic_s + roman_Φ ( italic_τ , italic_X start_POSTSUBSCRIPT italic_τ end_POSTSUBSCRIPT ) ] .
(4)
A well-known option strategy belonging to the above class is the Iron Condor strategy
Cohen (
2005
); Woodard (
2011
)
. Without loss of generality,
X
𝑋
X
italic_X
only contains a underlying process
S
t
subscript
𝑆
𝑡
S_{t}
italic_S start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT
and a bank account
B
t
subscript
𝐵
𝑡
B_{t}
italic_B start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT
process as follows:
{
d
⁢
S
t
=
μ
⁢
S
t
⁢
d
⁢
t
+
σ
⁢
S
t
⁢
d
⁢
W
t
,
d
⁢
B
t
=
r
⁢
B
t
⁢
d
⁢
t
cases
𝑑
subscript
𝑆
𝑡
𝜇
subscript
𝑆
𝑡
𝑑
𝑡
𝜎
subscript
𝑆
𝑡
𝑑
subscript
𝑊
𝑡
otherwise
𝑑
subscript
𝐵
𝑡
𝑟
subscript
𝐵
𝑡
𝑑
𝑡
otherwise
\begin{cases}dS_{t}=\mu S_{t}dt+\sigma S_{t}dW_{t},\\
dB_{t}=rB_{t}dt\end{cases}
{ start_ROW start_CELL italic_d italic_S start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT = italic_μ italic_S start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT italic_d italic_t + italic_σ italic_S start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT italic_d italic_W start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT , end_CELL start_CELL end_CELL end_ROW start_ROW start_CELL italic_d italic_B start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT = italic_r italic_B start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT italic_d italic_t end_CELL start_CELL end_CELL end_ROW
(5)
where
r
𝑟
r
italic_r
is the short rate. From general portfolio theory
X
𝑋
X
italic_X
process can be expressed as,
d
⁢
X
t
=
X
t
⁢
(
(
1
−
ω
)
⁢
d
⁢
B
t
B
t
+
ω
⁢
d
⁢
S
t
S
t
)
𝑑
subscript
𝑋
𝑡
subscript
𝑋
𝑡
1
𝜔
𝑑
subscript
𝐵
𝑡
subscript
𝐵
𝑡
𝜔
𝑑
subscript
𝑆
𝑡
subscript
𝑆
𝑡
dX_{t}=X_{t}((1-\omega)\frac{dB_{t}}{B_{t}}+\omega\frac{dS_{t}}{S_{t}})
italic_d italic_X start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT = italic_X start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT ( ( 1 - italic_ω ) divide start_ARG italic_d italic_B start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT end_ARG start_ARG italic_B start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT end_ARG + italic_ω divide start_ARG italic_d italic_S start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT end_ARG start_ARG italic_S start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT end_ARG )
(6)
where
ω
𝜔
\omega
italic_ω
is the weight of the underlying asset.
If we do not consider optimizing consumption and set
F
=
0
𝐹
0
F=0
italic_F = 0
, the utility function depends solely on the terminal wealth, which is the usual case for Iron Condor portfolio, then the control process
u
∈
U
𝑢
𝑈
u\in U
italic_u ∈ italic_U
, where
U
𝑈
U
italic_U
is the family of Iron Condor portfolios corresponding to some underlying, is determined by the structure of the four options and time t. Specifically, Iron Condor combines a bullish put spread and a bearish call spread with option strikes satisfying
k
1
<
k
2
≤
k
3
<
k
4
subscript
𝑘
1
subscript
𝑘
2
subscript
𝑘
3
subscript
𝑘
4
k_{1}<k_{2}\leq k_{3}<k_{4}
italic_k start_POSTSUBSCRIPT 1 end_POSTSUBSCRIPT < italic_k start_POSTSUBSCRIPT 2 end_POSTSUBSCRIPT ≤ italic_k start_POSTSUBSCRIPT 3 end_POSTSUBSCRIPT < italic_k start_POSTSUBSCRIPT 4 end_POSTSUBSCRIPT
. A concrete example of the terminal Profit and Loss (P&L) profile is illustrated in Figure
1
.
Most previous research on the Iron Condor focused on portfolio behavior at or near the expiration date
de Saint-Cyr (
2023
); Dziawgo (
2020
)
. For instance,
de Saint-Cyr (
2023
)
conducted a comparative study on the success rates of put spreads, call spreads, and Iron Condor portfolios near expiration, using the SPX dataset. Their findings indicated that put and call spreads generally exhibit higher success rates compared to Iron Condor portfolios. In addition, the success rates of all strategies decrease as the time to expiration increases.
In another study,
Dziawgo (
2020
)
analyzed the risk measures of Iron Condor portfolios through the lens of option Greeks. Their results revealed that all risk metrics fluctuate significantly over time.
In another study,
Dziawgo (
2020
)
analyzed the risk measures of Iron Condor portfolios based on option Greeks. Their results revealed that all risk metrics fluctuate significantly with the increase of time horizon. Despite these contributions, previous research did not dive into the transient behavior of the Iron Condor portfolio. A comprehensive investigation of the potential profits and risks in this aspect, either through theoretical proofs or simulation-based analyses, is required.
Figure 1:
Schematic diagram of
P
&
L
𝑃
𝐿
P\&L
italic_P & italic_L
of an Iron Condor portfolio at expiration
T
𝑇
T
italic_T
, where
S
T
subscript
𝑆
𝑇
S_{T}
italic_S start_POSTSUBSCRIPT italic_T end_POSTSUBSCRIPT
represents the current underlying price, and
k
1
subscript
𝑘
1
k_{1}
italic_k start_POSTSUBSCRIPT 1 end_POSTSUBSCRIPT
,
k
2
subscript
𝑘
2
k_{2}
italic_k start_POSTSUBSCRIPT 2 end_POSTSUBSCRIPT
,
k
3
subscript
𝑘
3
k_{3}
italic_k start_POSTSUBSCRIPT 3 end_POSTSUBSCRIPT
, and
k
4
subscript
𝑘
4
k_{4}
italic_k start_POSTSUBSCRIPT 4 end_POSTSUBSCRIPT
correspond to the strike prices of the long put, short put, long call, and short call options, respectively. The two green dashed lines indicate the breakeven prices of the put and call spreads. The control process
u
𝑢
u
italic_u
is paramtrized by
x
𝑥
x
italic_x
(moneyness),
x
^
^
𝑥
\hat{x}
over^ start_ARG italic_x end_ARG
(span) and
x
¯
¯
𝑥
\bar{x}
over¯ start_ARG italic_x end_ARG
(asymmetry degree).
Financial stochastic process simulation models have undergone extensive development, particularly in addressing volatility-related challenges.
The classical Black-Scholes model assumes a constant volatility surface, which is an unrealistic simplification. To address this limitation,
Dupire et al. (
1994
)
developed local volatility models that simulate volatility as a deterministic function of the underlying price and time, effectively capturing time-inhomogeneity. Meanwhile,
Heston (
1993
)
proposed the renowned Heston model, which describes volatility using stochastic differential equations driven by Brownian motion, incorporating additional dynamics such as mean reversion. This results in a semi-martingale process. However, these classical models often fail to capture the full complexity of observed option prices.
Gatheral and Jacquier (
2014
)
identified that realized log-volatility behaves like a fractional Brownian motion (fBm) with a Hurst exponent
H
𝐻
H
italic_H
around 0.1. This insight spurred the development of rough volatility models. The Rough Fractional Stochastic Volatility (RFSV) model, introduced by
Bayer et al. (
2016
)
, demonstrated remarkable consistency with observed SPX volatility surfaces. To integrate the rough volatility dynamics with the analytical tractability of the classical Heston model, the Rough Heston model was developed. This model replaces the standard Brownian motion with fBm characterized by
H
<
0.5
𝐻
0.5
H<0.5
italic_H < 0.5
.
Despite their successes, rough stochastic models are computationally intensive.
Bennedsen et al. (
2017
)
developed a hybrid scheme to enhance simulation efficiency, applying it to Monte Carlo pricing in the rough Bergomi model,
achieving faster and more accurate fits for implied volatility smiles. Markovian approximation techniques, such as those in
Abi Jaber (
2019
)
,
further speed up rough volatility models by approximating the power-law kernel using a system of exponential kernels.
Fadugba (
2020
)
use homotopy analysis method to price European call option with time-fractional BS equation.
Wang et al. (
2022
)
employ finite difference method to study the multi-dimensional fractional
Balck-Scholes model under three underlying assets.
Recently,
Wong and Bilokon (
2024
)
introduced a fast algorithm for simulating fBm-driven processes, achieving a tenfold speed increase compared to traditional rough Heston simulations.
In this work, we first provide a theoretical proof of the optimal stopping time for an Iron Condor portfolio with specific structures where the underlying prices follow a bounded martingale. Next, we utilize the Rough Heston model and its fast simulation algorithms to design a data generator to investigate the potential profits and risks associated with Iron Condor portfolios under more general conditions.
Specifically, let
(
ω
,
ℱ
,
P
,
𝔽
)
𝜔
ℱ
𝑃
𝔽
(\omega,\mathcal{F},P,\mathbb{F})
( italic_ω , caligraphic_F , italic_P , blackboard_F )
denote a filtered probability space, where the filtration
𝔽
𝔽
\mathbb{F}
blackboard_F
satisfies the usual conditions.
Our objective is to determine the optimal control process
u
⁢
(
k
i
,
τ
)
∈
𝔽
t
,
i
∈
{
1
,
2
,
3
,
4
}
formulae-sequence
𝑢
subscript
𝑘
𝑖
𝜏
subscript
𝔽
𝑡
𝑖
1
2
3
4
u(k_{i},\tau)\in\mathbb{F}_{t},i\in\{1,2,3,4\}
italic_u ( italic_k start_POSTSUBSCRIPT italic_i end_POSTSUBSCRIPT , italic_τ ) ∈ blackboard_F start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT , italic_i ∈ { 1 , 2 , 3 , 4 }
(or its parameterized form
u
(
x
,
x
^
,
x
¯
,
τ
)
)
u(x,\hat{x},\bar{x},\tau))
italic_u ( italic_x , over^ start_ARG italic_x end_ARG , over¯ start_ARG italic_x end_ARG , italic_τ ) )
for all
τ
<
t
𝜏
𝑡
\tau<t
italic_τ < italic_t
.
The parameterized study is based on simulation methods, where the three key portfolio structure parameters used in the simulations are moneyness (
x
𝑥
x
italic_x
), strike span (
x
^
^
𝑥
\hat{x}
over^ start_ARG italic_x end_ARG
), and asymmetry degree (
x
¯
¯
𝑥
\bar{x}
over¯ start_ARG italic_x end_ARG
).
The remainder of this paper is organized as follows: Section 2 details the methodology and metrics used in this study. Section 3 presents the theoretical proof of the optimal control strategy for a bounded martingale process. Section 4 focuses on the symmetric Iron Condor portfolio simulation and analysis. Section 5 investigates the asymmetric Iron Condor portfolio. Section 6 validates the findings from the simulation on actual SPX datasets across bullish, sideways, and bearish markets. Finally, Section 7 concludes the paper and suggests potential directions for future research.
2
Methodology
2.1
Data generator implementation
The data generator is utilized for simulation purposes, generating underlying asset paths and performing Monte Carlo-based option pricing for the simulation of Iron Condor portfolios.
To design the data generator, we must add more structural assumptions for
S
t
subscript
𝑆
𝑡
S_{t}
italic_S start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT
, so the univariate Rough Heston framework
El Euch et al. (
2019
)
is employed.
The volatility term in Rough Heston evolves as a correlated fractional Brownian motion (fBm) with Hurst parameter
H
<
0.5
𝐻
0.5
H<0.5
italic_H < 0.5
, reflecting its self-similar Gaussian process nature. The
S
t
subscript
𝑆
𝑡
S_{t}
italic_S start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT
process is defined as follows:
Definition 1
.
The underlying asset prices have P-dynamics defined as
d
⁢
S
t
=
μ
⁢
S
t
⁢
d
⁢
t
+
V
t
⁢
S
t
⁢
d
⁢
W
t
1
,
𝑑
subscript
𝑆
𝑡
𝜇
subscript
𝑆
𝑡
𝑑
𝑡
subscript
𝑉
𝑡
subscript
𝑆
𝑡
𝑑
superscript
subscript
𝑊
𝑡
1
dS_{t}=\mu S_{t}\,dt+\sqrt{V_{t}}S_{t}\,dW_{t}^{1},
italic_d italic_S start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT = italic_μ italic_S start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT italic_d italic_t + square-root start_ARG italic_V start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT end_ARG italic_S start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT italic_d italic_W start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT start_POSTSUPERSCRIPT 1 end_POSTSUPERSCRIPT ,
(7)
with
V
u
=
V
t
+
κ
Γ
⁢
(
H
+
1
2
)
⁢
∫
t
u
θ
t
⁢
(
s
)
−
V
s
(
u
−
s
)
1
2
−
H
⁢
𝑑
s
+
ν
Γ
⁢
(
H
+
1
2
)
⁢
∫
t
u
V
s
(
u
−
s
)
1
2
−
H
⁢
𝑑
W
s
2
,
subscript
𝑉
𝑢
subscript
𝑉
𝑡
𝜅
Γ
𝐻
1
2
superscript
subscript
𝑡
𝑢
subscript
𝜃
𝑡
𝑠
subscript
𝑉
𝑠
superscript
𝑢
𝑠
1
2
𝐻
differential-d
𝑠
𝜈
Γ
𝐻
1
2
superscript
subscript
𝑡
𝑢
subscript
𝑉
𝑠
superscript
𝑢
𝑠
1
2
𝐻
differential-d
superscript
subscript
𝑊
𝑠
2
V_{u}=V_{t}+\frac{\kappa}{\Gamma\left(H+\frac{1}{2}\right)}\int_{t}^{u}\frac{%
\theta_{t}(s)-V_{s}}{(u-s)^{\frac{1}{2}-H}}\,ds+\frac{\nu}{\Gamma\left(H+\frac%
{1}{2}\right)}\int_{t}^{u}\frac{\sqrt{V_{s}}}{(u-s)^{\frac{1}{2}-H}}\,dW_{s}^{%
2},
italic_V start_POSTSUBSCRIPT italic_u end_POSTSUBSCRIPT = italic_V start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT + divide start_ARG italic_κ end_ARG start_ARG roman_Γ ( italic_H + divide start_ARG 1 end_ARG start_ARG 2 end_ARG ) end_ARG ∫ start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT start_POSTSUPERSCRIPT italic_u end_POSTSUPERSCRIPT divide start_ARG italic_θ start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT ( italic_s ) - italic_V start_POSTSUBSCRIPT italic_s end_POSTSUBSCRIPT end_ARG start_ARG ( italic_u - italic_s ) start_POSTSUPERSCRIPT divide start_ARG 1 end_ARG start_ARG 2 end_ARG - italic_H end_POSTSUPERSCRIPT end_ARG italic_d italic_s + divide start_ARG italic_ν end_ARG start_ARG roman_Γ ( italic_H + divide start_ARG 1 end_ARG start_ARG 2 end_ARG ) end_ARG ∫ start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT start_POSTSUPERSCRIPT italic_u end_POSTSUPERSCRIPT divide start_ARG square-root start_ARG italic_V start_POSTSUBSCRIPT italic_s end_POSTSUBSCRIPT end_ARG end_ARG start_ARG ( italic_u - italic_s ) start_POSTSUPERSCRIPT divide start_ARG 1 end_ARG start_ARG 2 end_ARG - italic_H end_POSTSUPERSCRIPT end_ARG italic_d italic_W start_POSTSUBSCRIPT italic_s end_POSTSUBSCRIPT start_POSTSUPERSCRIPT 2 end_POSTSUPERSCRIPT ,
(8)
where:
μ
𝜇
\mu
italic_μ
is he drift term of the asset price, representing the expected return rate,
V
t
subscript
𝑉
𝑡
V_{t}
italic_V start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT
is the stochastic volatility of the underlying asset at time
t
𝑡
t
italic_t
,
W
t
1
superscript
subscript
𝑊
𝑡
1
W_{t}^{1}
italic_W start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT start_POSTSUPERSCRIPT 1 end_POSTSUPERSCRIPT
is a standard Brownian motion driving the asset price process,
W
s
2
superscript
subscript
𝑊
𝑠
2
W_{s}^{2}
italic_W start_POSTSUBSCRIPT italic_s end_POSTSUBSCRIPT start_POSTSUPERSCRIPT 2 end_POSTSUPERSCRIPT
is a standard Brownian motion independent of
W
t
1
superscript
subscript
𝑊
𝑡
1
W_{t}^{1}
italic_W start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT start_POSTSUPERSCRIPT 1 end_POSTSUPERSCRIPT
that driving the volatility process,
κ
𝜅
\kappa
italic_κ
is the mean reversion rate,
θ
t
⁢
(
s
)
subscript
𝜃
𝑡
𝑠
\theta_{t}(s)
italic_θ start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT ( italic_s )
is the long-term,
F
t
subscript
𝐹
𝑡
F_{t}
italic_F start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT
measurable function indicating the mean level of the variance process,
Γ
⁢
(
H
+
1
2
)
Γ
𝐻
1
2
\Gamma(H+\frac{1}{2})
roman_Γ ( italic_H + divide start_ARG 1 end_ARG start_ARG 2 end_ARG )
is the Gamma function evaluated at
H
+
1
2
𝐻
1
2
H+\frac{1}{2}
italic_H + divide start_ARG 1 end_ARG start_ARG 2 end_ARG
, scaling the rough volatility dynamics,
ν
𝜈
\nu
italic_ν
is the volatility of volatility parameter, influencing the intensity of the volatility process.
The stationary increments of fractional Gaussian noise (fGn) has an autocovariance function defined as,
ρ
H
⁢
(
k
)
=
1
2
⁢
(
|
k
+
1
|
2
⁢
H
+
|
k
−
1
|
2
⁢
H
−
2
⁢
|
k
|
2
⁢
H
)
,
k
∈
ℝ
+
.
formulae-sequence
subscript
𝜌
𝐻
𝑘
1
2
superscript
𝑘
1
2
𝐻
superscript
𝑘
1
2
𝐻
2
superscript
𝑘
2
𝐻
𝑘
superscript
ℝ
\rho_{H}(k)=\frac{1}{2}(|k+1|^{2H}+|k-1|^{2H}-2|k|^{2H}),\quad k\in\mathbb{R}^%
{+}.
italic_ρ start_POSTSUBSCRIPT italic_H end_POSTSUBSCRIPT ( italic_k ) = divide start_ARG 1 end_ARG start_ARG 2 end_ARG ( | italic_k + 1 | start_POSTSUPERSCRIPT 2 italic_H end_POSTSUPERSCRIPT + | italic_k - 1 | start_POSTSUPERSCRIPT 2 italic_H end_POSTSUPERSCRIPT - 2 | italic_k | start_POSTSUPERSCRIPT 2 italic_H end_POSTSUPERSCRIPT ) , italic_k ∈ blackboard_R start_POSTSUPERSCRIPT + end_POSTSUPERSCRIPT .
(9)
Since the subsequent experiments are conducted on the SPX option chain, the params of the Rough Heston model adopts
r
=
0
𝑟
0
r=0
italic_r = 0
,
V
0
=
0.0392
subscript
𝑉
0
0.0392
V_{0}=0.0392
italic_V start_POSTSUBSCRIPT 0 end_POSTSUBSCRIPT = 0.0392
,
κ
=
0.1
𝜅
0.1
\kappa=0.1
italic_κ = 0.1
,
θ
=
0.3156
𝜃
0.3156
\theta=0.3156
italic_θ = 0.3156
,
ν
=
0.0331
𝜈
0.0331
\nu=0.0331
italic_ν = 0.0331
, and
ρ
=
−
0.681
𝜌
0.681
\rho=-0.681
italic_ρ = - 0.681
, which is calibrated by
Ma and Wu (
2022
)
.
Moreover, the fast algorithm proposed by
Wong and Bilokon (
2024
)
is employed to perform Monte Carlo-based option pricing at each time step prior to maturity. The risk-neutral measure for the fractional Brownian motion (fBm) process is adopted using an fBm-specific version of Girsanov’s theorem
Hu and Øksendal (
2003
)
.
We consider relative strikes for call and put options within the range
k
∈
[
0.8
,
1.2
]
𝑘
0.8
1.2
k\in[0.8,1.2]
italic_k ∈ [ 0.8 , 1.2 ]
with increments of
0.2
0.2
0.2
0.2
, over the time horizon
t
∈
[
0
,
T
]
𝑡
0
𝑇
t\in[0,T]
italic_t ∈ [ 0 , italic_T ]
. The Monte Carlo simulation uses 10,000 trajectories, and each procedure is repeated 30 times to ensure robust results.
2.2
Iron Condor Portfolio
An Iron Condor strategy is a combination of a bullish put spread and a bearish call spread, the investors achieve maximum profit if
S
T
subscript
𝑆
𝑇
S_{T}
italic_S start_POSTSUBSCRIPT italic_T end_POSTSUBSCRIPT
remains between
k
2
subscript
𝑘
2
k_{2}
italic_k start_POSTSUBSCRIPT 2 end_POSTSUBSCRIPT
and
k
3
subscript
𝑘
3
k_{3}
italic_k start_POSTSUBSCRIPT 3 end_POSTSUBSCRIPT
. An Iron Condor portfolio adopt an adapt control process
u
⁢
(
k
i
,
τ
)
𝑢
subscript
𝑘
𝑖
𝜏
u(k_{i},\tau)
italic_u ( italic_k start_POSTSUBSCRIPT italic_i end_POSTSUBSCRIPT , italic_τ )
or the the parametrized form
u
(
x
,
x
^
,
(
¯
x
)
,
τ
)
u(x,\hat{x},\bar{(}x),\tau)
italic_u ( italic_x , over^ start_ARG italic_x end_ARG , over¯ start_ARG ( end_ARG italic_x ) , italic_τ )
, which is defined as follows (see also Figure 1 but use
S
0
subscript
𝑆
0
S_{0}
italic_S start_POSTSUBSCRIPT 0 end_POSTSUBSCRIPT
rather than
S
T
subscript
𝑆
𝑇
S_{T}
italic_S start_POSTSUBSCRIPT italic_T end_POSTSUBSCRIPT
):
x
𝑥
x
italic_x
measures the relative strike position to the current underlying price, defined as:
x
=
|
k
2
−
S
0
|
S
0
,
𝑥
subscript
𝑘
2
subscript
𝑆
0
subscript
𝑆
0
x=\frac{|k_{2}-S_{0}|}{S_{0}},
italic_x = divide start_ARG | italic_k start_POSTSUBSCRIPT 2 end_POSTSUBSCRIPT - italic_S start_POSTSUBSCRIPT 0 end_POSTSUBSCRIPT | end_ARG start_ARG italic_S start_POSTSUBSCRIPT 0 end_POSTSUBSCRIPT end_ARG ,
(10)
where
S
0
subscript
𝑆
0
S_{0}
italic_S start_POSTSUBSCRIPT 0 end_POSTSUBSCRIPT
represents the underlying price, and
k
2
subscript
𝑘
2
k_{2}
italic_k start_POSTSUBSCRIPT 2 end_POSTSUBSCRIPT
is the strike of the short put.
x
^
^
𝑥
\hat{x}
over^ start_ARG italic_x end_ARG
measures the distance between
k
1
subscript
𝑘
1
k_{1}
italic_k start_POSTSUBSCRIPT 1 end_POSTSUBSCRIPT
and
k
2
subscript
𝑘
2
k_{2}
italic_k start_POSTSUBSCRIPT 2 end_POSTSUBSCRIPT
(or
k
4
subscript
𝑘
4
k_{4}
italic_k start_POSTSUBSCRIPT 4 end_POSTSUBSCRIPT
and
k
3
subscript
𝑘
3
k_{3}
italic_k start_POSTSUBSCRIPT 3 end_POSTSUBSCRIPT
due to symmetry), defined as:
x
^
=
(
k
2
−
k
1
)
.
^
𝑥
subscript
𝑘
2
subscript
𝑘
1
\hat{x}=(k_{2}-k_{1}).
over^ start_ARG italic_x end_ARG = ( italic_k start_POSTSUBSCRIPT 2 end_POSTSUBSCRIPT - italic_k start_POSTSUBSCRIPT 1 end_POSTSUBSCRIPT ) .
(11)
The asymmetry,
x
¯
¯
𝑥
\bar{x}
over¯ start_ARG italic_x end_ARG
, measures the imbalance of moneyness between the bullish put spread and the bearish call spread, defined as:
x
¯
=
(
S
0
−
k
1
)
−
(
k
4
−
S
0
)
,
¯
𝑥
subscript
𝑆
0
subscript
𝑘
1
subscript
𝑘
4
subscript
𝑆
0
\bar{x}=(S_{0}-k_{1})-(k_{4}-S_{0}),
over¯ start_ARG italic_x end_ARG = ( italic_S start_POSTSUBSCRIPT 0 end_POSTSUBSCRIPT - italic_k start_POSTSUBSCRIPT 1 end_POSTSUBSCRIPT ) - ( italic_k start_POSTSUBSCRIPT 4 end_POSTSUBSCRIPT - italic_S start_POSTSUBSCRIPT 0 end_POSTSUBSCRIPT ) ,
(12)
where
k
1
subscript
𝑘
1
k_{1}
italic_k start_POSTSUBSCRIPT 1 end_POSTSUBSCRIPT
and
k
4
subscript
𝑘
4
k_{4}
italic_k start_POSTSUBSCRIPT 4 end_POSTSUBSCRIPT
are the strike prices of the first and fourth options, respectively
Definition 2
.
An
Iron Condor
portfolio
P
t
⁢
(
u
⁢
(
k
i
,
τ
)
)
subscript
𝑃
𝑡
𝑢
subscript
𝑘
𝑖
𝜏
P_{t}(u(k_{i},\tau))
italic_P start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT ( italic_u ( italic_k start_POSTSUBSCRIPT italic_i end_POSTSUBSCRIPT , italic_τ ) )
is a function of the control process
u
𝑢
u
italic_u
and stopping time
τ
𝜏
\tau
italic_τ
, such that
P
t
⁢
(
u
⁢
(
k
i
,
τ
)
)
subscript
𝑃
𝑡
𝑢
subscript
𝑘
𝑖
𝜏
P_{t}(u(k_{i},\tau))
italic_P start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT ( italic_u ( italic_k start_POSTSUBSCRIPT italic_i end_POSTSUBSCRIPT , italic_τ ) )
has the following dynamics:
{
P
u
,
τ
=
(
V
P
⁢
(
k
2
,
S
t
,
σ
,
t
)
−
V
P
⁢
(
k
1
,
S
t
,
σ
,
t
)
)
+
(
V
C
⁢
(
k
3
,
S
t
,
σ
,
t
)
−
V
C
⁢
(
k
4
,
S
t
,
σ
,
t
)
)
,
P
0
,
k
=
0
cases
subscript
𝑃
𝑢
𝜏
subscript
𝑉
𝑃
subscript
𝑘
2
subscript
𝑆
𝑡
𝜎
𝑡
subscript
𝑉
𝑃
subscript
𝑘
1
subscript
𝑆
𝑡
𝜎
𝑡
subscript
𝑉
𝐶
subscript
𝑘
3
subscript
𝑆
𝑡
𝜎
𝑡
subscript
𝑉
𝐶
subscript
𝑘
4
subscript
𝑆
𝑡
𝜎
𝑡
otherwise
subscript
𝑃
0
𝑘
0
otherwise
\begin{cases}P_{u,\tau}=(V_{P}(k_{2},S_{t},\sigma,t)-V_{P}(k_{1},S_{t},\sigma,%
t))+(V_{C}(k_{3},S_{t},\sigma,t)-V_{C}(k_{4},S_{t},\sigma,t)),\\
P_{0,k}=0\end{cases}
{ start_ROW start_CELL italic_P start_POSTSUBSCRIPT italic_u , italic_τ end_POSTSUBSCRIPT = ( italic_V start_POSTSUBSCRIPT italic_P end_POSTSUBSCRIPT ( italic_k start_POSTSUBSCRIPT 2 end_POSTSUBSCRIPT , italic_S start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT , italic_σ , italic_t ) - italic_V start_POSTSUBSCRIPT italic_P end_POSTSUBSCRIPT ( italic_k start_POSTSUBSCRIPT 1 end_POSTSUBSCRIPT , italic_S start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT , italic_σ , italic_t ) ) + ( italic_V start_POSTSUBSCRIPT italic_C end_POSTSUBSCRIPT ( italic_k start_POSTSUBSCRIPT 3 end_POSTSUBSCRIPT , italic_S start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT , italic_σ , italic_t ) - italic_V start_POSTSUBSCRIPT italic_C end_POSTSUBSCRIPT ( italic_k start_POSTSUBSCRIPT 4 end_POSTSUBSCRIPT , italic_S start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT , italic_σ , italic_t ) ) , end_CELL start_CELL end_CELL end_ROW start_ROW start_CELL italic_P start_POSTSUBSCRIPT 0 , italic_k end_POSTSUBSCRIPT = 0 end_CELL start_CELL end_CELL end_ROW
(13)
subject to the constraints
{
k
1
<
k
2
≤
k
3
<
k
4
;
τ
≤
t
cases
subscript
𝑘
1
subscript
𝑘
2
subscript
𝑘
3
subscript
𝑘
4
otherwise
𝜏
𝑡
otherwise
\begin{cases}k_{1}<k_{2}\leq k_{3}<k_{4};\\
\tau\leq t\end{cases}
{ start_ROW start_CELL italic_k start_POSTSUBSCRIPT 1 end_POSTSUBSCRIPT < italic_k start_POSTSUBSCRIPT 2 end_POSTSUBSCRIPT ≤ italic_k start_POSTSUBSCRIPT 3 end_POSTSUBSCRIPT < italic_k start_POSTSUBSCRIPT 4 end_POSTSUBSCRIPT ; end_CELL start_CELL end_CELL end_ROW start_ROW start_CELL italic_τ ≤ italic_t end_CELL start_CELL end_CELL end_ROW
(14)
Where
V
p
subscript
𝑉
𝑝
V_{p}
italic_V start_POSTSUBSCRIPT italic_p end_POSTSUBSCRIPT
and
V
c
subscript
𝑉
𝑐
V_{c}
italic_V start_POSTSUBSCRIPT italic_c end_POSTSUBSCRIPT
are Monte Carlo option pricing functions for put and call options.
2.3
Datasets Partition
Due to the complex nonlinear structure of the payoff of Iron Condor portfolios, deriving the optimal stopping strategy under general conditions is challenging, so we conduct simulations using the data generator.
The overall generated dataset is denoted by
D
n
,
t
,
f
∈
ℝ
N
×
T
×
F
subscript
𝐷
𝑛
𝑡
𝑓
superscript
ℝ
𝑁
𝑇
𝐹
D_{n,t,f}\in\mathbb{R}^{N\times T\times F}
italic_D start_POSTSUBSCRIPT italic_n , italic_t , italic_f end_POSTSUBSCRIPT ∈ blackboard_R start_POSTSUPERSCRIPT italic_N × italic_T × italic_F end_POSTSUPERSCRIPT
, where:
1.
N
𝑁
N
italic_N
is the number of underlying prices trajectories.
2.
T
𝑇
T
italic_T
is the total time steps to maturity.
3.
F
𝐹
F
italic_F
is the number of portfolios.
We note that
D
n
,
t
,
0
subscript
𝐷
𝑛
𝑡
0
D_{n,t,0}
italic_D start_POSTSUBSCRIPT italic_n , italic_t , 0 end_POSTSUBSCRIPT
are all the underlying price processes, and
D
n
,
t
,
i
,
i
∈
[
1
,
F
]
subscript
𝐷
𝑛
𝑡
𝑖
𝑖
1
𝐹
D_{n,t,i},i\in[1,F]
italic_D start_POSTSUBSCRIPT italic_n , italic_t , italic_i end_POSTSUBSCRIPT , italic_i ∈ [ 1 , italic_F ]
are all the normalized value processes of Iron Condor portfolios under different control
u
⁢
(
k
i
,
T
)
𝑢
subscript
𝑘
𝑖
𝑇
u(k_{i},T)
italic_u ( italic_k start_POSTSUBSCRIPT italic_i end_POSTSUBSCRIPT , italic_T )
for the n-th underlying price process.
Therefore, dataset partition is based on the 0-th dimension of F for N underlying price process, defined as follows:
Definition 3
.
The datasets of bullish market
D
r
subscript
𝐷
𝑟
D_{r}
italic_D start_POSTSUBSCRIPT italic_r end_POSTSUBSCRIPT
, sideway market
D
M
subscript
𝐷
𝑀
D_{M}
italic_D start_POSTSUBSCRIPT italic_M end_POSTSUBSCRIPT
, and bearish market
D
l
subscript
𝐷
𝑙
D_{l}
italic_D start_POSTSUBSCRIPT italic_l end_POSTSUBSCRIPT
are the partition of
D
𝐷
D
italic_D
with the following rule:
{
D
r
:=
D
n
,
t
,
f
|
D
n
,
T
,
0
D
n
,
0
,
0
∈
[
1.1
,
+
∞
]
,
D
M
:=
D
n
,
t
,
f
|
D
n
,
T
,
0
D
n
,
0
,
0
∈
[
0.9
,
1.1
]
,
D
l
:=
D
n
,
t
,
f
|
D
n
,
T
,
0
D
n
,
0
,
0
∈
[
−
∞
,
0.9
]
cases
assign
subscript
𝐷
𝑟
conditional
subscript
𝐷
𝑛
𝑡
𝑓
subscript
𝐷
𝑛
𝑇
0
subscript
𝐷
𝑛
0
0
1.1
otherwise
assign
subscript
𝐷
𝑀
conditional
subscript
𝐷
𝑛
𝑡
𝑓
subscript
𝐷
𝑛
𝑇
0
subscript
𝐷
𝑛
0
0
0.9
1.1
otherwise
assign
subscript
𝐷
𝑙
conditional
subscript
𝐷
𝑛
𝑡
𝑓
subscript
𝐷
𝑛
𝑇
0
subscript
𝐷
𝑛
0
0
0.9
otherwise
\begin{cases}D_{r}:=D_{n,t,f}|\frac{D_{n,T,0}}{D_{n,0,0}}\in[1.1,+\infty],\\
D_{M}:=D_{n,t,f}|\frac{D_{n,T,0}}{D_{n,0,0}}\in[0.9,1.1],\\
D_{l}:=D_{n,t,f}|\frac{D_{n,T,0}}{D_{n,0,0}}\in[-\infty,0.9]\end{cases}
{ start_ROW start_CELL italic_D start_POSTSUBSCRIPT italic_r end_POSTSUBSCRIPT := italic_D start_POSTSUBSCRIPT italic_n , italic_t , italic_f end_POSTSUBSCRIPT | divide start_ARG italic_D start_POSTSUBSCRIPT italic_n , italic_T , 0 end_POSTSUBSCRIPT end_ARG start_ARG italic_D start_POSTSUBSCRIPT italic_n , 0 , 0 end_POSTSUBSCRIPT end_ARG ∈ [ 1.1 , + ∞ ] , end_CELL start_CELL end_CELL end_ROW start_ROW start_CELL italic_D start_POSTSUBSCRIPT italic_M end_POSTSUBSCRIPT := italic_D start_POSTSUBSCRIPT italic_n , italic_t , italic_f end_POSTSUBSCRIPT | divide start_ARG italic_D start_POSTSUBSCRIPT italic_n , italic_T , 0 end_POSTSUBSCRIPT end_ARG start_ARG italic_D start_POSTSUBSCRIPT italic_n , 0 , 0 end_POSTSUBSCRIPT end_ARG ∈ [ 0.9 , 1.1 ] , end_CELL start_CELL end_CELL end_ROW start_ROW start_CELL italic_D start_POSTSUBSCRIPT italic_l end_POSTSUBSCRIPT := italic_D start_POSTSUBSCRIPT italic_n , italic_t , italic_f end_POSTSUBSCRIPT | divide start_ARG italic_D start_POSTSUBSCRIPT italic_n , italic_T , 0 end_POSTSUBSCRIPT end_ARG start_ARG italic_D start_POSTSUBSCRIPT italic_n , 0 , 0 end_POSTSUBSCRIPT end_ARG ∈ [ - ∞ , 0.9 ] end_CELL start_CELL end_CELL end_ROW
(15)
2.4
Variable and indicator design
Moreover, To simplify subsequent analysis, we only consider the options portfolio payoff process, and set weight of
B
t
subscript
𝐵
𝑡
B_{t}
italic_B start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT
process in Equation
6
to 0.
Furthermore, we normalize
P
t
⁢
(
u
⁢
(
k
i
,
τ
)
)
subscript
𝑃
𝑡
𝑢
subscript
𝑘
𝑖
𝜏
P_{t}(u(k_{i},\tau))
italic_P start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT ( italic_u ( italic_k start_POSTSUBSCRIPT italic_i end_POSTSUBSCRIPT , italic_τ ) )
to get the potential profit under control
u
𝑢
u
italic_u
denoted by
ϕ
t
,
u
subscript
italic-ϕ
𝑡
𝑢
\phi_{t,u}
italic_ϕ start_POSTSUBSCRIPT italic_t , italic_u end_POSTSUBSCRIPT
, defined as:
ϕ
t
,
u
=
𝔼
ω
⁢
[
P
0
⁢
(
ω
,
t
,
u
)
−
P
t
⁢
(
ω
,
t
,
u
)
P
0
⁢
(
ω
,
t
,
u
)
|
D
]
,
t
∈
[
0
,
T
]
,
u
∈
U
formulae-sequence
subscript
italic-ϕ
𝑡
𝑢
subscript
𝔼
𝜔
delimited-[]
conditional
subscript
𝑃
0
𝜔
𝑡
𝑢
subscript
𝑃
𝑡
𝜔
𝑡
𝑢
subscript
𝑃
0
𝜔
𝑡
𝑢
𝐷
formulae-sequence
𝑡
0
𝑇
𝑢
𝑈
\phi_{t,u}=\mathbb{E}_{\omega}[\frac{P_{0}(\omega,t,u)-P_{t}(\omega,t,u)}{P_{0%
}(\omega,t,u)}|D],\quad t\in[0,T],\quad u\in U
italic_ϕ start_POSTSUBSCRIPT italic_t , italic_u end_POSTSUBSCRIPT = blackboard_E start_POSTSUBSCRIPT italic_ω end_POSTSUBSCRIPT [ divide start_ARG italic_P start_POSTSUBSCRIPT 0 end_POSTSUBSCRIPT ( italic_ω , italic_t , italic_u ) - italic_P start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT ( italic_ω , italic_t , italic_u ) end_ARG start_ARG italic_P start_POSTSUBSCRIPT 0 end_POSTSUBSCRIPT ( italic_ω , italic_t , italic_u ) end_ARG | italic_D ] , italic_t ∈ [ 0 , italic_T ] , italic_u ∈ italic_U
(16)
Consequently, the potential profit under control
u
𝑢
u
italic_u
at the expiration date is denoted by
ϕ
T
,
u
subscript
italic-ϕ
𝑇
𝑢
\phi_{T,u}
italic_ϕ start_POSTSUBSCRIPT italic_T , italic_u end_POSTSUBSCRIPT
, and the potential profit at optimal stopping time is denoted by
ϕ
τ
,
u
subscript
italic-ϕ
𝜏
𝑢
\phi_{\tau,u}
italic_ϕ start_POSTSUBSCRIPT italic_τ , italic_u end_POSTSUBSCRIPT
. We note that the optimal stopping time is determined on dataset
D
𝐷
D
italic_D
and then used as a constant in calculating other metrics.
Formally,
{
ϕ
τ
,
u
=
𝐦𝐚𝐱
𝔼
⁢
[
ϕ
t
,
u
|
D
]
,
t
∈
[
0
,
T
]
,
u
∈
U
τ
⁢
(
u
)
=
𝐚𝐫𝐠𝐦𝐚𝐱
𝔼
⁢
[
ϕ
t
,
u
|
D
]
,
t
∈
[
0
,
T
]
,
u
∈
U
cases
formulae-sequence
subscript
italic-ϕ
𝜏
𝑢
𝐦𝐚𝐱
𝔼
delimited-[]
conditional
subscript
italic-ϕ
𝑡
𝑢
𝐷
formulae-sequence
𝑡
0
𝑇
𝑢
𝑈
otherwise
formulae-sequence
𝜏
𝑢
𝐚𝐫𝐠𝐦𝐚𝐱
𝔼
delimited-[]
conditional
subscript
italic-ϕ
𝑡
𝑢
𝐷
formulae-sequence
𝑡
0
𝑇
𝑢
𝑈
otherwise
\begin{cases}\phi_{\tau,u}=\mathbf{max}\quad\mathbb{E}[\phi_{t,u}|D],\quad t%
\in[0,T],\quad u\in U\\
\tau(u)=\mathbf{argmax}\quad\mathbb{E}[\phi_{t,u}|D],\quad t\in[0,T],\quad u%
\in U\\
\end{cases}
{ start_ROW start_CELL italic_ϕ start_POSTSUBSCRIPT italic_τ , italic_u end_POSTSUBSCRIPT = bold_max blackboard_E [ italic_ϕ start_POSTSUBSCRIPT italic_t , italic_u end_POSTSUBSCRIPT | italic_D ] , italic_t ∈ [ 0 , italic_T ] , italic_u ∈ italic_U end_CELL start_CELL end_CELL end_ROW start_ROW start_CELL italic_τ ( italic_u ) = bold_argmax blackboard_E [ italic_ϕ start_POSTSUBSCRIPT italic_t , italic_u end_POSTSUBSCRIPT | italic_D ] , italic_t ∈ [ 0 , italic_T ] , italic_u ∈ italic_U end_CELL start_CELL end_CELL end_ROW
(17)
Next, we define the success rates at time
T
𝑇
T
italic_T
and optimal stopping time
τ
𝜏
\tau
italic_τ
as:
{
θ
T
,
u
=
𝔼
ω
[
𝕀
ϕ
T
,
u
>
0
)
|
D
]
,
t
=
T
,
u
∈
U
θ
τ
,
u
=
𝔼
ω
[
𝕀
ϕ
τ
,
u
>
0
)
|
D
]
,
t
=
τ
,
u
∈
U
\begin{cases}\theta_{T,u}=\mathbb{E}_{\omega}[\mathbb{I}_{\phi_{T,u}>0})|D],%
\quad t=T,\quad u\in U\\
\theta_{\tau,u}=\mathbb{E}_{\omega}[\mathbb{I}_{\phi_{\tau,u}>0})|D],\quad t=%
\tau,\quad u\in U\\
\end{cases}
{ start_ROW start_CELL italic_θ start_POSTSUBSCRIPT italic_T , italic_u end_POSTSUBSCRIPT = blackboard_E start_POSTSUBSCRIPT italic_ω end_POSTSUBSCRIPT [ blackboard_I start_POSTSUBSCRIPT italic_ϕ start_POSTSUBSCRIPT italic_T , italic_u end_POSTSUBSCRIPT > 0 end_POSTSUBSCRIPT ) | italic_D ] , italic_t = italic_T , italic_u ∈ italic_U end_CELL start_CELL end_CELL end_ROW start_ROW start_CELL italic_θ start_POSTSUBSCRIPT italic_τ , italic_u end_POSTSUBSCRIPT = blackboard_E start_POSTSUBSCRIPT italic_ω end_POSTSUBSCRIPT [ blackboard_I start_POSTSUBSCRIPT italic_ϕ start_POSTSUBSCRIPT italic_τ , italic_u end_POSTSUBSCRIPT > 0 end_POSTSUBSCRIPT ) | italic_D ] , italic_t = italic_τ , italic_u ∈ italic_U end_CELL start_CELL end_CELL end_ROW
(18)
The two potential profit metrics for sideways market dataset
D
M
subscript
𝐷
𝑀
D_{M}
italic_D start_POSTSUBSCRIPT italic_M end_POSTSUBSCRIPT
are denoted by
ϕ
T
,
M
subscript
italic-ϕ
𝑇
𝑀
\phi_{T,M}
italic_ϕ start_POSTSUBSCRIPT italic_T , italic_M end_POSTSUBSCRIPT
and
ϕ
τ
,
M
subscript
italic-ϕ
𝜏
𝑀
\phi_{\tau,M}
italic_ϕ start_POSTSUBSCRIPT italic_τ , italic_M end_POSTSUBSCRIPT
, in which we use the same
τ
𝜏
\tau
italic_τ
values in
ϕ
τ
subscript
italic-ϕ
𝜏
\phi_{\tau}
italic_ϕ start_POSTSUBSCRIPT italic_τ end_POSTSUBSCRIPT
but condition on dataset
D
M
subscript
𝐷
𝑀
D_{M}
italic_D start_POSTSUBSCRIPT italic_M end_POSTSUBSCRIPT
.
Finally, the risk
η
u
subscript
𝜂
𝑢
\eta_{u}
italic_η start_POSTSUBSCRIPT italic_u end_POSTSUBSCRIPT
results from significant bullish or bearish markets are measured based on
D
r
subscript
𝐷
𝑟
D_{r}
italic_D start_POSTSUBSCRIPT italic_r end_POSTSUBSCRIPT
and
D
l
subscript
𝐷
𝑙
D_{l}
italic_D start_POSTSUBSCRIPT italic_l end_POSTSUBSCRIPT
data, which is defined as:
{
η
T
,
u
|
D
r
=
𝔼
ω
⁢
[
ϕ
T
,
u
|
D
r
]
,
t
=
T
,
u
∈
U
η
T
,
u
|
D
l
=
𝔼
ω
⁢
[
ϕ
T
,
u
|
D
l
]
,
t
=
T
,
u
∈
U
cases
formulae-sequence
conditional
subscript
𝜂
𝑇
𝑢
subscript
𝐷
𝑟
subscript
𝔼
𝜔
delimited-[]
conditional
subscript
italic-ϕ
𝑇
𝑢
subscript
𝐷
𝑟
formulae-sequence
𝑡
𝑇
𝑢
𝑈
otherwise
formulae-sequence
conditional
subscript
𝜂
𝑇
𝑢
subscript
𝐷
𝑙
subscript
𝔼
𝜔
delimited-[]
conditional
subscript
italic-ϕ
𝑇
𝑢
subscript
𝐷
𝑙
formulae-sequence
𝑡
𝑇
𝑢
𝑈
otherwise
\begin{cases}\eta_{T,u}|D_{r}=\mathbb{E}_{\omega}[\phi_{T,u}|D_{r}],\quad t=T,%
\quad u\in U\\
\eta_{T,u}|D_{l}=\mathbb{E}_{\omega}[\phi_{T,u}|D_{l}],\quad t=T,\quad u\in U%
\\
\end{cases}
{ start_ROW start_CELL italic_η start_POSTSUBSCRIPT italic_T , italic_u end_POSTSUBSCRIPT | italic_D start_POSTSUBSCRIPT italic_r end_POSTSUBSCRIPT = blackboard_E start_POSTSUBSCRIPT italic_ω end_POSTSUBSCRIPT [ italic_ϕ start_POSTSUBSCRIPT italic_T , italic_u end_POSTSUBSCRIPT | italic_D start_POSTSUBSCRIPT italic_r end_POSTSUBSCRIPT ] , italic_t = italic_T , italic_u ∈ italic_U end_CELL start_CELL end_CELL end_ROW start_ROW start_CELL italic_η start_POSTSUBSCRIPT italic_T , italic_u end_POSTSUBSCRIPT | italic_D start_POSTSUBSCRIPT italic_l end_POSTSUBSCRIPT = blackboard_E start_POSTSUBSCRIPT italic_ω end_POSTSUBSCRIPT [ italic_ϕ start_POSTSUBSCRIPT italic_T , italic_u end_POSTSUBSCRIPT | italic_D start_POSTSUBSCRIPT italic_l end_POSTSUBSCRIPT ] , italic_t = italic_T , italic_u ∈ italic_U end_CELL start_CELL end_CELL end_ROW
(19)
3
Optimal Control for a Bounded Martingale Process
This section analyzes a simplified case where
S
t
subscript
𝑆
𝑡
S_{t}
italic_S start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT
is a bounded martingale under the risk-neutral measure
ℚ
ℚ
\mathbb{Q}
blackboard_Q
, and
K
2
≤
S
t
≤
K
3
subscript
𝐾
2
subscript
𝑆
𝑡
subscript
𝐾
3
K_{2}\leq S_{t}\leq K_{3}
italic_K start_POSTSUBSCRIPT 2 end_POSTSUBSCRIPT ≤ italic_S start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT ≤ italic_K start_POSTSUBSCRIPT 3 end_POSTSUBSCRIPT
for all
t
∈
[
0
,
T
]
𝑡
0
𝑇
t\in[0,T]
italic_t ∈ [ 0 , italic_T ]
.
We begin with the following lemma:
Lemma 1
.
Let
S
t
subscript
𝑆
𝑡
S_{t}
italic_S start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT
be a martingale, and assume that
S
t
subscript
𝑆
𝑡
S_{t}
italic_S start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT
remains within the profitable region
[
K
2
,
K
3
]
subscript
𝐾
2
subscript
𝐾
3
[K_{2},K_{3}]
[ italic_K start_POSTSUBSCRIPT 2 end_POSTSUBSCRIPT , italic_K start_POSTSUBSCRIPT 3 end_POSTSUBSCRIPT ]
for all
t
∈
[
0
,
T
]
𝑡
0
𝑇
t\in[0,T]
italic_t ∈ [ 0 , italic_T ]
. Then the value process of Iron Condor portfolio
P
u
,
t
subscript
𝑃
𝑢
𝑡
P_{u,t}
italic_P start_POSTSUBSCRIPT italic_u , italic_t end_POSTSUBSCRIPT
is a submartingale.
Proof.
The rate of time decay (
Θ
Θ
\Theta
roman_Θ
) for short and long options is given by:
{
Θ
long
=
−
∂
V
C
∂
t
or
−
∂
V
P
∂
t
,
Θ
short
=
∂
V
C
∂
t
or
∂
V
P
∂
t
,
cases
subscript
Θ
long
subscript
𝑉
𝐶
𝑡
or
subscript
𝑉
𝑃
𝑡
otherwise
subscript
Θ
short
subscript
𝑉
𝐶
𝑡
or
subscript
𝑉
𝑃
𝑡
otherwise
\begin{cases}\Theta_{\text{long}}=-\frac{\partial V_{C}}{\partial t}\quad\text%
{or}\quad-\frac{\partial V_{P}}{\partial t},\\
\Theta_{\text{short}}=\frac{\partial V_{C}}{\partial t}\quad\text{or}\quad%
\frac{\partial V_{P}}{\partial t},\end{cases}
{ start_ROW start_CELL roman_Θ start_POSTSUBSCRIPT long end_POSTSUBSCRIPT = - divide start_ARG ∂ italic_V start_POSTSUBSCRIPT italic_C end_POSTSUBSCRIPT end_ARG start_ARG ∂ italic_t end_ARG or - divide start_ARG ∂ italic_V start_POSTSUBSCRIPT italic_P end_POSTSUBSCRIPT end_ARG start_ARG ∂ italic_t end_ARG , end_CELL start_CELL end_CELL end_ROW start_ROW start_CELL roman_Θ start_POSTSUBSCRIPT short end_POSTSUBSCRIPT = divide start_ARG ∂ italic_V start_POSTSUBSCRIPT italic_C end_POSTSUBSCRIPT end_ARG start_ARG ∂ italic_t end_ARG or divide start_ARG ∂ italic_V start_POSTSUBSCRIPT italic_P end_POSTSUBSCRIPT end_ARG start_ARG ∂ italic_t end_ARG , end_CELL start_CELL end_CELL end_ROW
(20)
where
V
C
subscript
𝑉
𝐶
V_{C}
italic_V start_POSTSUBSCRIPT italic_C end_POSTSUBSCRIPT
and
V
P
subscript
𝑉
𝑃
V_{P}
italic_V start_POSTSUBSCRIPT italic_P end_POSTSUBSCRIPT
denote the call and put option prices, respectively.
According to the no-arbitrage theory, given the relation
K
1
<
K
2
≤
S
t
≤
K
3
<
4
subscript
𝐾
1
subscript
𝐾
2
subscript
𝑆
𝑡
subscript
𝐾
3
4
K_{1}<K_{2}\leq S_{t}\leq K_{3}<4
italic_K start_POSTSUBSCRIPT 1 end_POSTSUBSCRIPT < italic_K start_POSTSUBSCRIPT 2 end_POSTSUBSCRIPT ≤ italic_S start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT ≤ italic_K start_POSTSUBSCRIPT 3 end_POSTSUBSCRIPT < 4
, we have
{
V
C
,
t
⁢
(
K
1
)
≤
V
C
,
t
⁢
(
K
2
)
,
V
P
,
t
⁢
(
K
4
)
≤
V
P
,
t
⁢
(
K
3
)
cases
subscript
𝑉
𝐶
𝑡
subscript
𝐾
1
subscript
𝑉
𝐶
𝑡
subscript
𝐾
2
otherwise
subscript
𝑉
𝑃
𝑡
subscript
𝐾
4
subscript
𝑉
𝑃
𝑡
subscript
𝐾
3
otherwise
\begin{cases}V_{C,t}(K_{1})\leq V_{C,t}(K_{2}),\\
V_{P,t}(K_{4})\leq V_{P,t}(K_{3})\end{cases}
{ start_ROW start_CELL italic_V start_POSTSUBSCRIPT italic_C , italic_t end_POSTSUBSCRIPT ( italic_K start_POSTSUBSCRIPT 1 end_POSTSUBSCRIPT ) ≤ italic_V start_POSTSUBSCRIPT italic_C , italic_t end_POSTSUBSCRIPT ( italic_K start_POSTSUBSCRIPT 2 end_POSTSUBSCRIPT ) , end_CELL start_CELL end_CELL end_ROW start_ROW start_CELL italic_V start_POSTSUBSCRIPT italic_P , italic_t end_POSTSUBSCRIPT ( italic_K start_POSTSUBSCRIPT 4 end_POSTSUBSCRIPT ) ≤ italic_V start_POSTSUBSCRIPT italic_P , italic_t end_POSTSUBSCRIPT ( italic_K start_POSTSUBSCRIPT 3 end_POSTSUBSCRIPT ) end_CELL start_CELL end_CELL end_ROW
(21)
Applying dynamic programming lead to the following relations,
{
|
Θ
⁢
(
K
1
,
t
)
|
≤
|
Θ
⁢
(
K
2
,
t
)
|
,
|
Θ
⁢
(
K
4
,
t
)
|
≤
|
Θ
⁢
(
K
3
,
t
)
|
cases
Θ
subscript
𝐾
1
𝑡
Θ
subscript
𝐾
2
𝑡
otherwise
Θ
subscript
𝐾
4
𝑡
Θ
subscript
𝐾
3
𝑡
otherwise
\begin{cases}|\Theta(K_{1},t)|\leq|\Theta(K_{2},t)|,\\
|\Theta(K_{4},t)|\leq|\Theta(K_{3},t)|\end{cases}
{ start_ROW start_CELL | roman_Θ ( italic_K start_POSTSUBSCRIPT 1 end_POSTSUBSCRIPT , italic_t ) | ≤ | roman_Θ ( italic_K start_POSTSUBSCRIPT 2 end_POSTSUBSCRIPT , italic_t ) | , end_CELL start_CELL end_CELL end_ROW start_ROW start_CELL | roman_Θ ( italic_K start_POSTSUBSCRIPT 4 end_POSTSUBSCRIPT , italic_t ) | ≤ | roman_Θ ( italic_K start_POSTSUBSCRIPT 3 end_POSTSUBSCRIPT , italic_t ) | end_CELL start_CELL end_CELL end_ROW
(22)
We have thus obtained the following inequality:
Θ
⁢
(
K
1
,
t
)
+
Θ
⁢
(
K
2
,
t
)
+
Θ
⁢
(
K
3
,
t
)
+
Θ
⁢
(
K
4
,
t
)
≥
0
,
∀
t
∈
[
0
,
T
]
formulae-sequence
Θ
subscript
𝐾
1
𝑡
Θ
subscript
𝐾
2
𝑡
Θ
subscript
𝐾
3
𝑡
Θ
subscript
𝐾
4
𝑡
0
for-all
𝑡
0
𝑇
\Theta(K_{1},t)+\Theta(K_{2},t)+\Theta(K_{3},t)+\Theta(K_{4},t)\geq 0,\forall t%
\in[0,T]
roman_Θ ( italic_K start_POSTSUBSCRIPT 1 end_POSTSUBSCRIPT , italic_t ) + roman_Θ ( italic_K start_POSTSUBSCRIPT 2 end_POSTSUBSCRIPT , italic_t ) + roman_Θ ( italic_K start_POSTSUBSCRIPT 3 end_POSTSUBSCRIPT , italic_t ) + roman_Θ ( italic_K start_POSTSUBSCRIPT 4 end_POSTSUBSCRIPT , italic_t ) ≥ 0 , ∀ italic_t ∈ [ 0 , italic_T ]
(23)
Since
S
t
subscript
𝑆
𝑡
S_{t}
italic_S start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT
is within the profitable region
[
K
2
,
K
3
]
subscript
𝐾
2
subscript
𝐾
3
[K_{2},K_{3}]
[ italic_K start_POSTSUBSCRIPT 2 end_POSTSUBSCRIPT , italic_K start_POSTSUBSCRIPT 3 end_POSTSUBSCRIPT ]
, the intrinsic values of all options remain
0
0
, and changes in portfolio value are driven by extrinsic time decay. So we obtain the inequality
𝔼
⁢
[
P
u
,
t
+
1
∣
ℱ
t
]
≥
P
u
,
t
,
∀
t
∈
[
0
,
T
]
.
formulae-sequence
𝔼
delimited-[]
conditional
subscript
𝑃
𝑢
𝑡
1
subscript
ℱ
𝑡
subscript
𝑃
𝑢
𝑡
for-all
𝑡
0
𝑇
\mathbb{E}[P_{u,t+1}\mid\mathcal{F}_{t}]\geq P_{u,t},\quad\forall t\in[0,T].
blackboard_E [ italic_P start_POSTSUBSCRIPT italic_u , italic_t + 1 end_POSTSUBSCRIPT ∣ caligraphic_F start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT ] ≥ italic_P start_POSTSUBSCRIPT italic_u , italic_t end_POSTSUBSCRIPT , ∀ italic_t ∈ [ 0 , italic_T ] .
Thus,
P
u
,
t
subscript
𝑃
𝑢
𝑡
P_{u,t}
italic_P start_POSTSUBSCRIPT italic_u , italic_t end_POSTSUBSCRIPT
is a submartingale. This concludes the proof of Lemma
1
.
∎
We now recall a foundational proposition in the theory of optimal stopping, which serves as a basis for analyzing American-style options.
Proposition 1
.
1.
If the discounted payoff process is a submartingale, then late stopping is optimal, i.e.,
τ
^
=
T
^
𝜏
𝑇
\hat{\tau}=T
over^ start_ARG italic_τ end_ARG = italic_T
.
2.
If the discounted payoff process is a supermartingale, then it is optimal to stop immediately, i.e.,
τ
^
=
0
^
𝜏
0
\hat{\tau}=0
over^ start_ARG italic_τ end_ARG = 0
.
3.
If the discounted payoff process is a martingale, then all stopping times
τ
𝜏
\tau
italic_τ
with
0
≤
τ
≤
T
0
𝜏
𝑇
0\leq\tau\leq T
0 ≤ italic_τ ≤ italic_T
are optimal.
Using Lemma
1
and Proposition
1
, we establish the following theorem:
Theorem 1
.
Let
S
t
subscript
𝑆
𝑡
S_{t}
italic_S start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT
be a martingale bounded by
K
2
subscript
𝐾
2
K_{2}
italic_K start_POSTSUBSCRIPT 2 end_POSTSUBSCRIPT
and
K
3
subscript
𝐾
3
K_{3}
italic_K start_POSTSUBSCRIPT 3 end_POSTSUBSCRIPT
for all
t
∈
[
0
,
T
]
𝑡
0
𝑇
t\in[0,T]
italic_t ∈ [ 0 , italic_T ]
. Then, for any Iron Condor portfolio with the strike structure
k
1
≤
k
2
=
K
2
<
k
3
=
K
3
<
k
4
subscript
𝑘
1
subscript
𝑘
2
subscript
𝐾
2
subscript
𝑘
3
subscript
𝐾
3
subscript
𝑘
4
k_{1}\leq k_{2}=K_{2}<k_{3}=K_{3}<k_{4}
italic_k start_POSTSUBSCRIPT 1 end_POSTSUBSCRIPT ≤ italic_k start_POSTSUBSCRIPT 2 end_POSTSUBSCRIPT = italic_K start_POSTSUBSCRIPT 2 end_POSTSUBSCRIPT < italic_k start_POSTSUBSCRIPT 3 end_POSTSUBSCRIPT = italic_K start_POSTSUBSCRIPT 3 end_POSTSUBSCRIPT < italic_k start_POSTSUBSCRIPT 4 end_POSTSUBSCRIPT
, the optimal stopping time is
τ
=
T
𝜏
𝑇
\tau=T
italic_τ = italic_T
.
Proof.
Define the Iron Condor portfolio values
P
u
,
T
subscript
𝑃
𝑢
𝑇
P_{u,T}
italic_P start_POSTSUBSCRIPT italic_u , italic_T end_POSTSUBSCRIPT
as the discounted payoff process adapted to
S
t
subscript
𝑆
𝑡
S_{t}
italic_S start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT
, where
𝔼
⁢
[
S
t
+
1
∣
ℱ
t
]
=
S
t
,
𝔼
delimited-[]
conditional
subscript
𝑆
𝑡
1
subscript
ℱ
𝑡
subscript
𝑆
𝑡
\mathbb{E}[S_{t+1}\mid\mathcal{F}_{t}]=S_{t},
blackboard_E [ italic_S start_POSTSUBSCRIPT italic_t + 1 end_POSTSUBSCRIPT ∣ caligraphic_F start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT ] = italic_S start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT ,
(24)
subject to
K
2
≤
S
t
≤
K
3
subscript
𝐾
2
subscript
𝑆
𝑡
subscript
𝐾
3
K_{2}\leq S_{t}\leq K_{3}
italic_K start_POSTSUBSCRIPT 2 end_POSTSUBSCRIPT ≤ italic_S start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT ≤ italic_K start_POSTSUBSCRIPT 3 end_POSTSUBSCRIPT
for all
t
∈
[
0
,
T
]
𝑡
0
𝑇
t\in[0,T]
italic_t ∈ [ 0 , italic_T ]
.
From Lemma
1
,
P
u
,
T
subscript
𝑃
𝑢
𝑇
P_{u,T}
italic_P start_POSTSUBSCRIPT italic_u , italic_T end_POSTSUBSCRIPT
is a submartingale. By Proposition
1
, the optimal stopping time is
τ
=
T
𝜏
𝑇
\tau=T
italic_τ = italic_T
. This concludes the proof of Theorem
1
.
∎
4
Simulation Research on Symmetric Iron Condor
4.1
Influence of Moneyness
In this section, Moneyness
x
𝑥
x
italic_x
is the sole variable determining the control process
u
𝑢
u
italic_u
. Figures
2
,
3
, and
4
depict the distributions of potential profits
ϕ
t
,
u
⁢
(
ω
)
subscript
italic-ϕ
𝑡
𝑢
𝜔
\phi_{t,u}(\omega)
italic_ϕ start_POSTSUBSCRIPT italic_t , italic_u end_POSTSUBSCRIPT ( italic_ω )
and risks
η
t
,
u
⁢
(
ω
)
subscript
𝜂
𝑡
𝑢
𝜔
\eta_{t,u}(\omega)
italic_η start_POSTSUBSCRIPT italic_t , italic_u end_POSTSUBSCRIPT ( italic_ω )
for
x
𝑥
x
italic_x
values of 0.12, 0.06, and 0.00, respectively.
The red line in each figure is the expectation process, denoted by
ϕ
t
,
u
subscript
italic-ϕ
𝑡
𝑢
\phi_{t,u}
italic_ϕ start_POSTSUBSCRIPT italic_t , italic_u end_POSTSUBSCRIPT
or
η
t
,
u
subscript
𝜂
𝑡
𝑢
\eta_{t,u}
italic_η start_POSTSUBSCRIPT italic_t , italic_u end_POSTSUBSCRIPT
.
The options in Figure
2
are deep Out-Of-The-Money (OTM), resulting in the
ϕ
t
,
u
⁢
(
ω
)
subscript
italic-ϕ
𝑡
𝑢
𝜔
\phi_{t,u}(\omega)
italic_ϕ start_POSTSUBSCRIPT italic_t , italic_u end_POSTSUBSCRIPT ( italic_ω )
distribution spanning a wide range of values from [-4,1] and
ϕ
t
,
u
≈
0
subscript
italic-ϕ
𝑡
𝑢
0
\phi_{t,u}\approx 0
italic_ϕ start_POSTSUBSCRIPT italic_t , italic_u end_POSTSUBSCRIPT ≈ 0
for all
t
∈
[
0
,
T
]
𝑡
0
𝑇
t\in[0,T]
italic_t ∈ [ 0 , italic_T ]
(Figure
2
(a)).
In a sideways market (Figure
2
(b)),
ϕ
t
,
u
subscript
italic-ϕ
𝑡
𝑢
\phi_{t,u}
italic_ϕ start_POSTSUBSCRIPT italic_t , italic_u end_POSTSUBSCRIPT
exhibits a convex increase. However, the risk associated with this portfolio is significant, as illustrated in Figures
2
(c) and
2
(d), where
η
t
,
u
subscript
𝜂
𝑡
𝑢
\eta_{t,u}
italic_η start_POSTSUBSCRIPT italic_t , italic_u end_POSTSUBSCRIPT
shows a concave decrease as time approaches the option expiration date.
Figure 2:
Distributions of (a)
ϕ
t
,
u
⁢
(
ω
)
|
D
conditional
subscript
italic-ϕ
𝑡
𝑢
𝜔
𝐷
\phi_{t,u}(\omega)|D
italic_ϕ start_POSTSUBSCRIPT italic_t , italic_u end_POSTSUBSCRIPT ( italic_ω ) | italic_D
; (b)
ϕ
t
,
u
⁢
(
ω
)
|
D
M
conditional
subscript
italic-ϕ
𝑡
𝑢
𝜔
subscript
𝐷
𝑀
\phi_{t,u}(\omega)|D_{M}
italic_ϕ start_POSTSUBSCRIPT italic_t , italic_u end_POSTSUBSCRIPT ( italic_ω ) | italic_D start_POSTSUBSCRIPT italic_M end_POSTSUBSCRIPT
; (c)
η
t
,
u
⁢
(
ω
)
|
D
r
conditional
subscript
𝜂
𝑡
𝑢
𝜔
subscript
𝐷
𝑟
\eta_{t,u}(\omega)|D_{r}
italic_η start_POSTSUBSCRIPT italic_t , italic_u end_POSTSUBSCRIPT ( italic_ω ) | italic_D start_POSTSUBSCRIPT italic_r end_POSTSUBSCRIPT
and (d)
η
t
,
u
⁢
(
ω
)
|
D
l
conditional
subscript
𝜂
𝑡
𝑢
𝜔
subscript
𝐷
𝑙
\eta_{t,u}(\omega)|D_{l}
italic_η start_POSTSUBSCRIPT italic_t , italic_u end_POSTSUBSCRIPT ( italic_ω ) | italic_D start_POSTSUBSCRIPT italic_l end_POSTSUBSCRIPT
for the deep OTM portfolio with
x
=
0.16
,
k
=
[
0.84
,
0.88
,
1.12
,
1.16
]
formulae-sequence
𝑥
0.16
𝑘
0.84
0.88
1.12
1.16
x=0.16,k=[0.84,0.88,1.12,1.16]
italic_x = 0.16 , italic_k = [ 0.84 , 0.88 , 1.12 , 1.16 ]
. The red lines represent the expectations.
In contrast, the options in Figure
3
are slightly OTM, meaning their strike prices are closer to the underlying price compared to the previous case. In Figure
3
(a), the distribution of
ϕ
t
,
u
⁢
(
ω
)
subscript
italic-ϕ
𝑡
𝑢
𝜔
\phi_{t,u}(\omega)
italic_ϕ start_POSTSUBSCRIPT italic_t , italic_u end_POSTSUBSCRIPT ( italic_ω )
becomes denser within the range of [-1.2,1]. In Figures
3
(c) and
3
(d), both
η
t
,
u
|
D
r
conditional
subscript
𝜂
𝑡
𝑢
subscript
𝐷
𝑟
\eta_{t,u}|D_{r}
italic_η start_POSTSUBSCRIPT italic_t , italic_u end_POSTSUBSCRIPT | italic_D start_POSTSUBSCRIPT italic_r end_POSTSUBSCRIPT
and
η
t
,
u
|
D
l
conditional
subscript
𝜂
𝑡
𝑢
subscript
𝐷
𝑙
\eta_{t,u}|D_{l}
italic_η start_POSTSUBSCRIPT italic_t , italic_u end_POSTSUBSCRIPT | italic_D start_POSTSUBSCRIPT italic_l end_POSTSUBSCRIPT
exhibit a concave decrease over time, but the maximum loss is significantly reduced compared to that shown in Figures
2
(c) and
2
(d).
Figure 3:
Distributions of (a)
ϕ
t
,
u
⁢
(
ω
)
|
D
conditional
subscript
italic-ϕ
𝑡
𝑢
𝜔
𝐷
\phi_{t,u}(\omega)|D
italic_ϕ start_POSTSUBSCRIPT italic_t , italic_u end_POSTSUBSCRIPT ( italic_ω ) | italic_D
; (b)
ϕ
t
,
u
⁢
(
ω
)
|
D
M
conditional
subscript
italic-ϕ
𝑡
𝑢
𝜔
subscript
𝐷
𝑀
\phi_{t,u}(\omega)|D_{M}
italic_ϕ start_POSTSUBSCRIPT italic_t , italic_u end_POSTSUBSCRIPT ( italic_ω ) | italic_D start_POSTSUBSCRIPT italic_M end_POSTSUBSCRIPT
; (c)
η
t
,
u
⁢
(
ω
)
|
D
r
conditional
subscript
𝜂
𝑡
𝑢
𝜔
subscript
𝐷
𝑟
\eta_{t,u}(\omega)|D_{r}
italic_η start_POSTSUBSCRIPT italic_t , italic_u end_POSTSUBSCRIPT ( italic_ω ) | italic_D start_POSTSUBSCRIPT italic_r end_POSTSUBSCRIPT
and (d)
η
t
,
u
⁢
(
ω
)
|
D
l
conditional
subscript
𝜂
𝑡
𝑢
𝜔
subscript
𝐷
𝑙
\eta_{t,u}(\omega)|D_{l}
italic_η start_POSTSUBSCRIPT italic_t , italic_u end_POSTSUBSCRIPT ( italic_ω ) | italic_D start_POSTSUBSCRIPT italic_l end_POSTSUBSCRIPT
for the slightly OTM portfolio with
x
=
0.10
,
k
=
[
0.9
,
0.94
,
1.06
,
1.1
]
formulae-sequence
𝑥
0.10
𝑘
0.9
0.94
1.06
1.1
x=0.10,k=[0.9,0.94,1.06,1.1]
italic_x = 0.10 , italic_k = [ 0.9 , 0.94 , 1.06 , 1.1 ]
. The red lines represent the expectations.
Figure
4
presents the at-the-money (ATM) case, where
x
=
0
𝑥
0
x=0
italic_x = 0
and
k
2
=
S
0
=
k
3
subscript
𝑘
2
subscript
𝑆
0
subscript
𝑘
3
k_{2}=S_{0}=k_{3}
italic_k start_POSTSUBSCRIPT 2 end_POSTSUBSCRIPT = italic_S start_POSTSUBSCRIPT 0 end_POSTSUBSCRIPT = italic_k start_POSTSUBSCRIPT 3 end_POSTSUBSCRIPT
, transforming the Iron Condor portfolio into an Iron Butterfly structure.
The distribution of
ϕ
t
,
u
⁢
(
ω
)
subscript
italic-ϕ
𝑡
𝑢
𝜔
\phi_{t,u}(\omega)
italic_ϕ start_POSTSUBSCRIPT italic_t , italic_u end_POSTSUBSCRIPT ( italic_ω )
in Figure
4
(a) is particularly appealing to investors, as it retains the maximum profit potential (albeit with a low probability) while capping the maximum loss at -0.2. However, Figure
4
shows that even under profitable market conditions, the
ϕ
t
,
u
|
D
M
conditional
subscript
italic-ϕ
𝑡
𝑢
subscript
𝐷
𝑀
\phi_{t,u}|D_{M}
italic_ϕ start_POSTSUBSCRIPT italic_t , italic_u end_POSTSUBSCRIPT | italic_D start_POSTSUBSCRIPT italic_M end_POSTSUBSCRIPT
is significantly lower compared to the previous cases where
x
=
0.12
𝑥
0.12
x=0.12
italic_x = 0.12
and
x
=
0.06
𝑥
0.06
x=0.06
italic_x = 0.06
.
Figure 4:
Distributions of (a)
ϕ
t
,
u
⁢
(
ω
)
|
D
conditional
subscript
italic-ϕ
𝑡
𝑢
𝜔
𝐷
\phi_{t,u}(\omega)|D
italic_ϕ start_POSTSUBSCRIPT italic_t , italic_u end_POSTSUBSCRIPT ( italic_ω ) | italic_D
; (b)
ϕ
t
,
u
⁢
(
ω
)
|
D
M
conditional
subscript
italic-ϕ
𝑡
𝑢
𝜔
subscript
𝐷
𝑀
\phi_{t,u}(\omega)|D_{M}
italic_ϕ start_POSTSUBSCRIPT italic_t , italic_u end_POSTSUBSCRIPT ( italic_ω ) | italic_D start_POSTSUBSCRIPT italic_M end_POSTSUBSCRIPT
; (c)
η
t
,
u
⁢
(
ω
)
|
D
r
conditional
subscript
𝜂
𝑡
𝑢
𝜔
subscript
𝐷
𝑟
\eta_{t,u}(\omega)|D_{r}
italic_η start_POSTSUBSCRIPT italic_t , italic_u end_POSTSUBSCRIPT ( italic_ω ) | italic_D start_POSTSUBSCRIPT italic_r end_POSTSUBSCRIPT
and (d)
η
t
,
u
⁢
(
ω
)
|
D
l
conditional
subscript
𝜂
𝑡
𝑢
𝜔
subscript
𝐷
𝑙
\eta_{t,u}(\omega)|D_{l}
italic_η start_POSTSUBSCRIPT italic_t , italic_u end_POSTSUBSCRIPT ( italic_ω ) | italic_D start_POSTSUBSCRIPT italic_l end_POSTSUBSCRIPT
for the At-The-Money (ATM) portfolio with
x
=
0.00
,
k
=
[
0.96
,
1.0
,
1.0
,
1.04
]
formulae-sequence
𝑥
0.00
𝑘
0.96
1.0
1.0
1.04
x=0.00,k=[0.96,1.0,1.0,1.04]
italic_x = 0.00 , italic_k = [ 0.96 , 1.0 , 1.0 , 1.04 ]
. The red lines represent the expectations.
Figure
5
summarizes the influence of
x
𝑥
x
italic_x
on portfolio performance.
In Figure
5
(a),
ϕ
τ
,
u
|
D
conditional
subscript
italic-ϕ
𝜏
𝑢
𝐷
\phi_{\tau,u}|D
italic_ϕ start_POSTSUBSCRIPT italic_τ , italic_u end_POSTSUBSCRIPT | italic_D
increases exponentially with
x
𝑥
x
italic_x
. The growing deviation between
ϕ
τ
,
u
|
D
conditional
subscript
italic-ϕ
𝜏
𝑢
𝐷
\phi_{\tau,u}|D
italic_ϕ start_POSTSUBSCRIPT italic_τ , italic_u end_POSTSUBSCRIPT | italic_D
and
ϕ
T
,
u
|
D
conditional
subscript
italic-ϕ
𝑇
𝑢
𝐷
\phi_{T,u}|D
italic_ϕ start_POSTSUBSCRIPT italic_T , italic_u end_POSTSUBSCRIPT | italic_D
as
x
𝑥
x
italic_x
increases highlights the importance of employing an optimal stopping strategy for deep OTM portfolios.
In Figure
5
(b),
ϕ
τ
,
u
|
D
M
conditional
subscript
italic-ϕ
𝜏
𝑢
subscript
𝐷
𝑀
\phi_{\tau,u}|D_{M}
italic_ϕ start_POSTSUBSCRIPT italic_τ , italic_u end_POSTSUBSCRIPT | italic_D start_POSTSUBSCRIPT italic_M end_POSTSUBSCRIPT
underperforms
ϕ
T
,
u
|
D
M
conditional
subscript
italic-ϕ
𝑇
𝑢
subscript
𝐷
𝑀
\phi_{T,u}|D_{M}
italic_ϕ start_POSTSUBSCRIPT italic_T , italic_u end_POSTSUBSCRIPT | italic_D start_POSTSUBSCRIPT italic_M end_POSTSUBSCRIPT
, suggesting that applying an optimal stopping strategy in sideways market conditions reduces profitability. This observation is consistent with the theoretical proof presented in Section 3.
In Figure
5
(c), both
θ
T
,
u
|
D
conditional
subscript
𝜃
𝑇
𝑢
𝐷
\theta_{T,u}|D
italic_θ start_POSTSUBSCRIPT italic_T , italic_u end_POSTSUBSCRIPT | italic_D
and
θ
τ
,
u
|
D
conditional
subscript
𝜃
𝜏
𝑢
𝐷
\theta_{\tau,u}|D
italic_θ start_POSTSUBSCRIPT italic_τ , italic_u end_POSTSUBSCRIPT | italic_D
increase with
x
𝑥
x
italic_x
. This is because increasing
x
𝑥
x
italic_x
widens the span between
k
2
subscript
𝑘
2
k_{2}
italic_k start_POSTSUBSCRIPT 2 end_POSTSUBSCRIPT
and
k
3
subscript
𝑘
3
k_{3}
italic_k start_POSTSUBSCRIPT 3 end_POSTSUBSCRIPT
, thereby improving the success ratio. However, an interesting crossover is observed between
θ
T
,
u
|
D
conditional
subscript
𝜃
𝑇
𝑢
𝐷
\theta_{T,u}|D
italic_θ start_POSTSUBSCRIPT italic_T , italic_u end_POSTSUBSCRIPT | italic_D
and
θ
τ
,
u
|
D
conditional
subscript
𝜃
𝜏
𝑢
𝐷
\theta_{\tau,u}|D
italic_θ start_POSTSUBSCRIPT italic_τ , italic_u end_POSTSUBSCRIPT | italic_D
, indicating that for deep OTM options, holding the portfolio until expiration outperforms early stopping at the optimal time
τ
𝜏
\tau
italic_τ
.
In Figure
5
(d),
θ
T
,
u
subscript
𝜃
𝑇
𝑢
\theta_{T,u}
italic_θ start_POSTSUBSCRIPT italic_T , italic_u end_POSTSUBSCRIPT
and
θ
τ
,
u
subscript
𝜃
𝜏
𝑢
\theta_{\tau,u}
italic_θ start_POSTSUBSCRIPT italic_τ , italic_u end_POSTSUBSCRIPT
exhibit similar performance under the
D
r
subscript
𝐷
𝑟
D_{r}
italic_D start_POSTSUBSCRIPT italic_r end_POSTSUBSCRIPT
and
D
l
subscript
𝐷
𝑙
D_{l}
italic_D start_POSTSUBSCRIPT italic_l end_POSTSUBSCRIPT
datasets. Notably,
θ
τ
,
u
subscript
𝜃
𝜏
𝑢
\theta_{\tau,u}
italic_θ start_POSTSUBSCRIPT italic_τ , italic_u end_POSTSUBSCRIPT
consistently outperforms
θ
T
,
u
subscript
𝜃
𝑇
𝑢
\theta_{T,u}
italic_θ start_POSTSUBSCRIPT italic_T , italic_u end_POSTSUBSCRIPT
, indicating that applying an optimal stopping strategy generally reduces risks in volatile market conditions. For deep OTM options with
x
>
0.1
𝑥
0.1
x>0.1
italic_x > 0.1
, employing an optimal stopping strategy can significantly truncate risk within the range of -1.0.
Figure 5:
Influence of
x
𝑥
x
italic_x
on (a) potential profits
ϕ
T
,
u
|
D
conditional
subscript
italic-ϕ
𝑇
𝑢
𝐷
\phi_{T,u}|D
italic_ϕ start_POSTSUBSCRIPT italic_T , italic_u end_POSTSUBSCRIPT | italic_D
and
ϕ
τ
,
u
|
D
conditional
subscript
italic-ϕ
𝜏
𝑢
𝐷
\phi_{\tau,u}|D
italic_ϕ start_POSTSUBSCRIPT italic_τ , italic_u end_POSTSUBSCRIPT | italic_D
; (b) potential profits
ϕ
T
,
u
|
D
M
conditional
subscript
italic-ϕ
𝑇
𝑢
subscript
𝐷
𝑀
\phi_{T,u}|D_{M}
italic_ϕ start_POSTSUBSCRIPT italic_T , italic_u end_POSTSUBSCRIPT | italic_D start_POSTSUBSCRIPT italic_M end_POSTSUBSCRIPT
and
ϕ
τ
,
u
|
D
M
conditional
subscript
italic-ϕ
𝜏
𝑢
subscript
𝐷
𝑀
\phi_{\tau,u}|D_{M}
italic_ϕ start_POSTSUBSCRIPT italic_τ , italic_u end_POSTSUBSCRIPT | italic_D start_POSTSUBSCRIPT italic_M end_POSTSUBSCRIPT
; (c) success rates
θ
T
,
u
|
D
conditional
subscript
𝜃
𝑇
𝑢
𝐷
\theta_{T,u}|D
italic_θ start_POSTSUBSCRIPT italic_T , italic_u end_POSTSUBSCRIPT | italic_D
and
θ
τ
,
u
|
D
conditional
subscript
𝜃
𝜏
𝑢
𝐷
\theta_{\tau,u}|D
italic_θ start_POSTSUBSCRIPT italic_τ , italic_u end_POSTSUBSCRIPT | italic_D
; and (d) risks
η
T
,
u
|
D
r
conditional
subscript
𝜂
𝑇
𝑢
subscript
𝐷
𝑟
\eta_{T,u}|D_{r}
italic_η start_POSTSUBSCRIPT italic_T , italic_u end_POSTSUBSCRIPT | italic_D start_POSTSUBSCRIPT italic_r end_POSTSUBSCRIPT
and
η
T
,
u
|
D
l
conditional
subscript
𝜂
𝑇
𝑢
subscript
𝐷
𝑙
\eta_{T,u}|D_{l}
italic_η start_POSTSUBSCRIPT italic_T , italic_u end_POSTSUBSCRIPT | italic_D start_POSTSUBSCRIPT italic_l end_POSTSUBSCRIPT
, respectively.
Table
1
provides comprehensive information about the performance of Iron Condor portfolios under various values of
x
𝑥
x
italic_x
. The metrics
ϕ
τ
,
u
|
D
conditional
subscript
italic-ϕ
𝜏
𝑢
𝐷
\phi_{\tau,u}|D
italic_ϕ start_POSTSUBSCRIPT italic_τ , italic_u end_POSTSUBSCRIPT | italic_D
,
θ
T
,
u
|
D
conditional
subscript
𝜃
𝑇
𝑢
𝐷
\theta_{T,u}|D
italic_θ start_POSTSUBSCRIPT italic_T , italic_u end_POSTSUBSCRIPT | italic_D
,
θ
τ
,
u
|
D
conditional
subscript
𝜃
𝜏
𝑢
𝐷
\theta_{\tau,u}|D
italic_θ start_POSTSUBSCRIPT italic_τ , italic_u end_POSTSUBSCRIPT | italic_D
,
ϕ
T
,
u
|
D
M
conditional
subscript
italic-ϕ
𝑇
𝑢
subscript
𝐷
𝑀
\phi_{T,u}|D_{M}
italic_ϕ start_POSTSUBSCRIPT italic_T , italic_u end_POSTSUBSCRIPT | italic_D start_POSTSUBSCRIPT italic_M end_POSTSUBSCRIPT
, and
ϕ
τ
,
u
|
D
M
conditional
subscript
italic-ϕ
𝜏
𝑢
subscript
𝐷
𝑀
\phi_{\tau,u}|D_{M}
italic_ϕ start_POSTSUBSCRIPT italic_τ , italic_u end_POSTSUBSCRIPT | italic_D start_POSTSUBSCRIPT italic_M end_POSTSUBSCRIPT
exhibit a positive relationship with
x
𝑥
x
italic_x
, while all risk metrics, i.e.,
η
T
,
u
|
D
r
conditional
subscript
𝜂
𝑇
𝑢
subscript
𝐷
𝑟
\eta_{T,u}|D_{r}
italic_η start_POSTSUBSCRIPT italic_T , italic_u end_POSTSUBSCRIPT | italic_D start_POSTSUBSCRIPT italic_r end_POSTSUBSCRIPT
,
η
τ
,
u
|
D
r
conditional
subscript
𝜂
𝜏
𝑢
subscript
𝐷
𝑟
\eta_{\tau,u}|D_{r}
italic_η start_POSTSUBSCRIPT italic_τ , italic_u end_POSTSUBSCRIPT | italic_D start_POSTSUBSCRIPT italic_r end_POSTSUBSCRIPT
,
η
T
,
u
|
D
l
conditional
subscript
𝜂
𝑇
𝑢
subscript
𝐷
𝑙
\eta_{T,u}|D_{l}
italic_η start_POSTSUBSCRIPT italic_T , italic_u end_POSTSUBSCRIPT | italic_D start_POSTSUBSCRIPT italic_l end_POSTSUBSCRIPT
, and
η
τ
,
u
|
D
l
conditional
subscript
𝜂
𝜏
𝑢
subscript
𝐷
𝑙
\eta_{\tau,u}|D_{l}
italic_η start_POSTSUBSCRIPT italic_τ , italic_u end_POSTSUBSCRIPT | italic_D start_POSTSUBSCRIPT italic_l end_POSTSUBSCRIPT
, show a negative relationship with
x
𝑥
x
italic_x
.
Notably, the optimal stopping time
τ
𝜏
\tau
italic_τ
ranges from 34 to 47 days out of the total 63-day period, corresponding to approximately 54% to 75% of the entire duration.
Interestingly, although employing the optimal stopping strategy reduces profitability in the sideways market (
D
M
subscript
𝐷
𝑀
D_{M}
italic_D start_POSTSUBSCRIPT italic_M end_POSTSUBSCRIPT
), it generally truncates risks and enhances overall profitability on
D
𝐷
D
italic_D
, as evidenced by the
θ
τ
,
u
|
D
conditional
subscript
𝜃
𝜏
𝑢
𝐷
\theta_{\tau,u}|D
italic_θ start_POSTSUBSCRIPT italic_τ , italic_u end_POSTSUBSCRIPT | italic_D
metric.
Table 1:
Portfolio performance metrics for different moneyness
x
𝑥
x
italic_x
ϕ
T
,
u
|
D
conditional
subscript
italic-ϕ
𝑇
𝑢
𝐷
\phi_{T,u}|D
italic_ϕ start_POSTSUBSCRIPT italic_T , italic_u end_POSTSUBSCRIPT | italic_D
ϕ
τ
,
u
|
D
conditional
subscript
italic-ϕ
𝜏
𝑢
𝐷
\phi_{\tau,u}|D
italic_ϕ start_POSTSUBSCRIPT italic_τ , italic_u end_POSTSUBSCRIPT | italic_D
τ
u
|
D
conditional
subscript
𝜏
𝑢
𝐷
\tau_{u}|D
italic_τ start_POSTSUBSCRIPT italic_u end_POSTSUBSCRIPT | italic_D
θ
T
,
u
|
D
conditional
subscript
𝜃
𝑇
𝑢
𝐷
\theta_{T,u}|D
italic_θ start_POSTSUBSCRIPT italic_T , italic_u end_POSTSUBSCRIPT | italic_D
θ
τ
,
u
|
D
conditional
subscript
𝜃
𝜏
𝑢
𝐷
\theta_{\tau,u}|D
italic_θ start_POSTSUBSCRIPT italic_τ , italic_u end_POSTSUBSCRIPT | italic_D
ϕ
T
,
u
|
D
M
conditional
subscript
italic-ϕ
𝑇
𝑢
subscript
𝐷
𝑀
\phi_{T,u}|D_{M}
italic_ϕ start_POSTSUBSCRIPT italic_T , italic_u end_POSTSUBSCRIPT | italic_D start_POSTSUBSCRIPT italic_M end_POSTSUBSCRIPT
ϕ
τ
,
u
|
D
M
conditional
subscript
italic-ϕ
𝜏
𝑢
subscript
𝐷
𝑀
\phi_{\tau,u}|D_{M}
italic_ϕ start_POSTSUBSCRIPT italic_τ , italic_u end_POSTSUBSCRIPT | italic_D start_POSTSUBSCRIPT italic_M end_POSTSUBSCRIPT
η
T
,
u
|
D
r
conditional
subscript
𝜂
𝑇
𝑢
subscript
𝐷
𝑟
\eta_{T,u}|D_{r}
italic_η start_POSTSUBSCRIPT italic_T , italic_u end_POSTSUBSCRIPT | italic_D start_POSTSUBSCRIPT italic_r end_POSTSUBSCRIPT
η
τ
,
u
|
D
r
conditional
subscript
𝜂
𝜏
𝑢
subscript
𝐷
𝑟
\eta_{\tau,u}|D_{r}
italic_η start_POSTSUBSCRIPT italic_τ , italic_u end_POSTSUBSCRIPT | italic_D start_POSTSUBSCRIPT italic_r end_POSTSUBSCRIPT
η
T
,
u
|
D
l
conditional
subscript
𝜂
𝑇
𝑢
subscript
𝐷
𝑙
\eta_{T,u}|D_{l}
italic_η start_POSTSUBSCRIPT italic_T , italic_u end_POSTSUBSCRIPT | italic_D start_POSTSUBSCRIPT italic_l end_POSTSUBSCRIPT
η
τ
,
u
|
D
l
conditional
subscript
𝜂
𝜏
𝑢
subscript
𝐷
𝑙
\eta_{\tau,u}|D_{l}
italic_η start_POSTSUBSCRIPT italic_τ , italic_u end_POSTSUBSCRIPT | italic_D start_POSTSUBSCRIPT italic_l end_POSTSUBSCRIPT
0.18
-0.02
0.13
43
0.83
0.73
1.00
0.59
-2.03
-0.71
-1.98
-0.79
0.16
0.02
0.10
43
0.78
0.70
1.00
0.50
-1.91
-0.70
-1.87
-0.66
0.14
0.01
0.07
44
0.70
0.66
1.00
0.43
-1.89
-0.66
-1.92
-0.58
0.12
0.02
0.05
34
0.63
0.66
0.89
0.21
-1.65
-0.28
-1.70
-0.23
0.10
0.02
0.04
34
0.54
0.64
0.64
0.16
-1.18
-0.22
-1.18
-0.16
0.08
0.00
0.03
47
0.45
0.55
0.38
0.21
-0.73
-0.34
-0.73
-0.30
0.06
-0.00
0.02
47
0.35
0.53
0.21
0.13
-0.41
-0.21
-0.41
-0.18
0.00
-0.00
0.01
47
0.24
0.51
0.09
0.06
-0.17
-0.10
-0.17
-0.08
4.2
Influence of Strikes Span
In this section, we focus on the strikes span
x
^
^
𝑥
\hat{x}
over^ start_ARG italic_x end_ARG
, which governs the slope of the call and put spreads in an Iron Condor strategy.
Figure
6
presents a narrow-span case with
x
^
=
0.02
^
𝑥
0.02
\hat{x}=0.02
over^ start_ARG italic_x end_ARG = 0.02
. The risks (
η
t
,
u
|
D
r
conditional
subscript
𝜂
𝑡
𝑢
subscript
𝐷
𝑟
\eta_{t,u}|D_{r}
italic_η start_POSTSUBSCRIPT italic_t , italic_u end_POSTSUBSCRIPT | italic_D start_POSTSUBSCRIPT italic_r end_POSTSUBSCRIPT
and
η
t
,
u
|
D
l
conditional
subscript
𝜂
𝑡
𝑢
subscript
𝐷
𝑙
\eta_{t,u}|D_{l}
italic_η start_POSTSUBSCRIPT italic_t , italic_u end_POSTSUBSCRIPT | italic_D start_POSTSUBSCRIPT italic_l end_POSTSUBSCRIPT
) are well controlled, as shown in Figures
6
(c) and (d). However,
ϕ
t
,
u
|
D
M
conditional
subscript
italic-ϕ
𝑡
𝑢
subscript
𝐷
𝑀
\phi_{t,u}|D_{M}
italic_ϕ start_POSTSUBSCRIPT italic_t , italic_u end_POSTSUBSCRIPT | italic_D start_POSTSUBSCRIPT italic_M end_POSTSUBSCRIPT
is approximately 0, indicating minimal profit potential.
Figure 6:
Distributions of (a)
ϕ
t
,
u
⁢
(
ω
)
|
D
conditional
subscript
italic-ϕ
𝑡
𝑢
𝜔
𝐷
\phi_{t,u}(\omega)|D
italic_ϕ start_POSTSUBSCRIPT italic_t , italic_u end_POSTSUBSCRIPT ( italic_ω ) | italic_D
; (b)
ϕ
t
,
u
⁢
(
ω
)
|
D
M
conditional
subscript
italic-ϕ
𝑡
𝑢
𝜔
subscript
𝐷
𝑀
\phi_{t,u}(\omega)|D_{M}
italic_ϕ start_POSTSUBSCRIPT italic_t , italic_u end_POSTSUBSCRIPT ( italic_ω ) | italic_D start_POSTSUBSCRIPT italic_M end_POSTSUBSCRIPT
; (c)
η
t
,
u
|
D
r
conditional
subscript
𝜂
𝑡
𝑢
subscript
𝐷
𝑟
\eta_{t,u}|D_{r}
italic_η start_POSTSUBSCRIPT italic_t , italic_u end_POSTSUBSCRIPT | italic_D start_POSTSUBSCRIPT italic_r end_POSTSUBSCRIPT
and (d)
η
t
,
u
|
D
l
conditional
subscript
𝜂
𝑡
𝑢
subscript
𝐷
𝑙
\eta_{t,u}|D_{l}
italic_η start_POSTSUBSCRIPT italic_t , italic_u end_POSTSUBSCRIPT | italic_D start_POSTSUBSCRIPT italic_l end_POSTSUBSCRIPT
for the narrow-span portfolio with
x
^
=
0.02
,
k
=
[
0.96
,
0.98
,
1.02
,
1.04
]
formulae-sequence
^
𝑥
0.02
𝑘
0.96
0.98
1.02
1.04
\hat{x}=0.02,k=[0.96,0.98,1.02,1.04]
over^ start_ARG italic_x end_ARG = 0.02 , italic_k = [ 0.96 , 0.98 , 1.02 , 1.04 ]
. The red lines represent the expectations.
In contrast, Figure
7
illustrates the distribution of
ϕ
t
,
u
⁢
(
ω
)
subscript
italic-ϕ
𝑡
𝑢
𝜔
\phi_{t,u}(\omega)
italic_ϕ start_POSTSUBSCRIPT italic_t , italic_u end_POSTSUBSCRIPT ( italic_ω )
in a wider-span case with
x
^
=
0.14
^
𝑥
0.14
\hat{x}=0.14
over^ start_ARG italic_x end_ARG = 0.14
. Here,
ϕ
t
,
u
|
D
M
conditional
subscript
italic-ϕ
𝑡
𝑢
subscript
𝐷
𝑀
\phi_{t,u}|D_{M}
italic_ϕ start_POSTSUBSCRIPT italic_t , italic_u end_POSTSUBSCRIPT | italic_D start_POSTSUBSCRIPT italic_M end_POSTSUBSCRIPT
exhibits a convex increase (Figure
7
(b)), but the risks are significantly elevated compared to the
x
^
=
0.02
^
𝑥
0.02
\hat{x}=0.02
over^ start_ARG italic_x end_ARG = 0.02
case, as demonstrated in Figures
7
(c) and (d).
Figure 7:
Distributions of (a)
ϕ
t
,
u
⁢
(
ω
)
|
D
conditional
subscript
italic-ϕ
𝑡
𝑢
𝜔
𝐷
\phi_{t,u}(\omega)|D
italic_ϕ start_POSTSUBSCRIPT italic_t , italic_u end_POSTSUBSCRIPT ( italic_ω ) | italic_D
; (b)
ϕ
t
,
u
⁢
(
ω
)
|
D
M
conditional
subscript
italic-ϕ
𝑡
𝑢
𝜔
subscript
𝐷
𝑀
\phi_{t,u}(\omega)|D_{M}
italic_ϕ start_POSTSUBSCRIPT italic_t , italic_u end_POSTSUBSCRIPT ( italic_ω ) | italic_D start_POSTSUBSCRIPT italic_M end_POSTSUBSCRIPT
; (c)
η
t
,
u
|
D
r
conditional
subscript
𝜂
𝑡
𝑢
subscript
𝐷
𝑟
\eta_{t,u}|D_{r}
italic_η start_POSTSUBSCRIPT italic_t , italic_u end_POSTSUBSCRIPT | italic_D start_POSTSUBSCRIPT italic_r end_POSTSUBSCRIPT
and (d)
η
t
,
u
|
D
l
conditional
subscript
𝜂
𝑡
𝑢
subscript
𝐷
𝑙
\eta_{t,u}|D_{l}
italic_η start_POSTSUBSCRIPT italic_t , italic_u end_POSTSUBSCRIPT | italic_D start_POSTSUBSCRIPT italic_l end_POSTSUBSCRIPT
for the wide-span portfolio with
x
^
=
0.14
,
k
=
[
0.84
,
0.98
,
1.02
,
1.16
]
formulae-sequence
^
𝑥
0.14
𝑘
0.84
0.98
1.02
1.16
\hat{x}=0.14,k=[0.84,0.98,1.02,1.16]
over^ start_ARG italic_x end_ARG = 0.14 , italic_k = [ 0.84 , 0.98 , 1.02 , 1.16 ]
. The red lines represent the expectations.
Figure
8
illustrates the impact of
x
^
^
𝑥
\hat{x}
over^ start_ARG italic_x end_ARG
on portfolio performance. In Figure
8
(a),
ϕ
τ
,
u
|
D
conditional
subscript
italic-ϕ
𝜏
𝑢
𝐷
\phi_{\tau,u}|D
italic_ϕ start_POSTSUBSCRIPT italic_τ , italic_u end_POSTSUBSCRIPT | italic_D
exhibits a linear increase with
x
^
^
𝑥
\hat{x}
over^ start_ARG italic_x end_ARG
, highlighting the significance of employing optimal stopping strategies for wide-span portfolios.
Under sideways market conditions, as shown in Figure
8
(b), all optimal stopping strategies determined on
D
𝐷
D
italic_D
underperform
ϕ
T
,
u
|
D
M
conditional
subscript
italic-ϕ
𝑇
𝑢
subscript
𝐷
𝑀
\phi_{T,u}|D_{M}
italic_ϕ start_POSTSUBSCRIPT italic_T , italic_u end_POSTSUBSCRIPT | italic_D start_POSTSUBSCRIPT italic_M end_POSTSUBSCRIPT
, consistent with the theoretical analysis presented in Section 3.
Figure
8
(c) demonstrates that the success ratio
θ
τ
,
u
|
D
conditional
subscript
𝜃
𝜏
𝑢
𝐷
\theta_{\tau,u}|D
italic_θ start_POSTSUBSCRIPT italic_τ , italic_u end_POSTSUBSCRIPT | italic_D
consistently outperforms
θ
T
,
u
|
D
conditional
subscript
𝜃
𝑇
𝑢
𝐷
\theta_{T,u}|D
italic_θ start_POSTSUBSCRIPT italic_T , italic_u end_POSTSUBSCRIPT | italic_D
across all control values of
x
^
^
𝑥
\hat{x}
over^ start_ARG italic_x end_ARG
, indicating that optimal stopping strategies enhance the likelihood of portfolio success.
Furthermore, Figure
8
(d) presents the risk metrics in volatile markets. It is evident that for both
D
r
subscript
𝐷
𝑟
D_{r}
italic_D start_POSTSUBSCRIPT italic_r end_POSTSUBSCRIPT
and
D
l
subscript
𝐷
𝑙
D_{l}
italic_D start_POSTSUBSCRIPT italic_l end_POSTSUBSCRIPT
, the optimal stopping strategies effectively reduce risks.
Figure 8:
Influence of
x
^
^
𝑥
\hat{x}
over^ start_ARG italic_x end_ARG
on (a) potential profits
ϕ
T
,
u
|
D
conditional
subscript
italic-ϕ
𝑇
𝑢
𝐷
\phi_{T,u}|D
italic_ϕ start_POSTSUBSCRIPT italic_T , italic_u end_POSTSUBSCRIPT | italic_D
and
ϕ
τ
,
u
|
D
conditional
subscript
italic-ϕ
𝜏
𝑢
𝐷
\phi_{\tau,u}|D
italic_ϕ start_POSTSUBSCRIPT italic_τ , italic_u end_POSTSUBSCRIPT | italic_D
; (b) potential profits
ϕ
T
,
u
|
D
M
conditional
subscript
italic-ϕ
𝑇
𝑢
subscript
𝐷
𝑀
\phi_{T,u}|D_{M}
italic_ϕ start_POSTSUBSCRIPT italic_T , italic_u end_POSTSUBSCRIPT | italic_D start_POSTSUBSCRIPT italic_M end_POSTSUBSCRIPT
and
ϕ
τ
,
u
|
D
M
conditional
subscript
italic-ϕ
𝜏
𝑢
subscript
𝐷
𝑀
\phi_{\tau,u}|D_{M}
italic_ϕ start_POSTSUBSCRIPT italic_τ , italic_u end_POSTSUBSCRIPT | italic_D start_POSTSUBSCRIPT italic_M end_POSTSUBSCRIPT
; (c) success rates
θ
T
,
u
|
D
conditional
subscript
𝜃
𝑇
𝑢
𝐷
\theta_{T,u}|D
italic_θ start_POSTSUBSCRIPT italic_T , italic_u end_POSTSUBSCRIPT | italic_D
and
θ
τ
,
u
|
D
conditional
subscript
𝜃
𝜏
𝑢
𝐷
\theta_{\tau,u}|D
italic_θ start_POSTSUBSCRIPT italic_τ , italic_u end_POSTSUBSCRIPT | italic_D
; and (d) risks
η
T
,
u
|
D
r
conditional
subscript
𝜂
𝑇
𝑢
subscript
𝐷
𝑟
\eta_{T,u}|D_{r}
italic_η start_POSTSUBSCRIPT italic_T , italic_u end_POSTSUBSCRIPT | italic_D start_POSTSUBSCRIPT italic_r end_POSTSUBSCRIPT
and
η
T
,
u
|
D
l
conditional
subscript
𝜂
𝑇
𝑢
subscript
𝐷
𝑙
\eta_{T,u}|D_{l}
italic_η start_POSTSUBSCRIPT italic_T , italic_u end_POSTSUBSCRIPT | italic_D start_POSTSUBSCRIPT italic_l end_POSTSUBSCRIPT
, respectively.
Table
2
summarizes the performance metrics of Iron Condor portfolios for various values of
x
^
^
𝑥
\hat{x}
over^ start_ARG italic_x end_ARG
.
Overall, it can be observed that
ϕ
τ
,
u
|
D
conditional
subscript
italic-ϕ
𝜏
𝑢
𝐷
\phi_{\tau,u}|D
italic_ϕ start_POSTSUBSCRIPT italic_τ , italic_u end_POSTSUBSCRIPT | italic_D
,
θ
T
,
u
|
D
conditional
subscript
𝜃
𝑇
𝑢
𝐷
\theta_{T,u}|D
italic_θ start_POSTSUBSCRIPT italic_T , italic_u end_POSTSUBSCRIPT | italic_D
,
θ
τ
,
u
|
D
conditional
subscript
𝜃
𝜏
𝑢
𝐷
\theta_{\tau,u}|D
italic_θ start_POSTSUBSCRIPT italic_τ , italic_u end_POSTSUBSCRIPT | italic_D
, and
ϕ
T
,
u
|
D
M
conditional
subscript
italic-ϕ
𝑇
𝑢
subscript
𝐷
𝑀
\phi_{T,u}|D_{M}
italic_ϕ start_POSTSUBSCRIPT italic_T , italic_u end_POSTSUBSCRIPT | italic_D start_POSTSUBSCRIPT italic_M end_POSTSUBSCRIPT
increase as
x
^
^
𝑥
\hat{x}
over^ start_ARG italic_x end_ARG
grows, indicating higher potential profits and success rates for wide-span cases. However,
η
T
,
u
|
D
r
conditional
subscript
𝜂
𝑇
𝑢
subscript
𝐷
𝑟
\eta_{T,u}|D_{r}
italic_η start_POSTSUBSCRIPT italic_T , italic_u end_POSTSUBSCRIPT | italic_D start_POSTSUBSCRIPT italic_r end_POSTSUBSCRIPT
and
η
T
,
u
|
D
l
conditional
subscript
𝜂
𝑇
𝑢
subscript
𝐷
𝑙
\eta_{T,u}|D_{l}
italic_η start_POSTSUBSCRIPT italic_T , italic_u end_POSTSUBSCRIPT | italic_D start_POSTSUBSCRIPT italic_l end_POSTSUBSCRIPT
also increase with
x
^
^
𝑥
\hat{x}
over^ start_ARG italic_x end_ARG
, reflecting elevated risks for wider spans.
The optimal stopping time
τ
𝜏
\tau
italic_τ
varies from 34 to 47 days out of the total 63-day period, representing approximately 54% to 75% of the entire duration. Implementing optimal stopping strategies slightly improves potential profits while significantly reducing risk magnitudes.
Table 2:
Portfolio performance metrics for different spans
x
^
^
𝑥
\hat{x}
over^ start_ARG italic_x end_ARG
ϕ
T
,
u
|
D
conditional
subscript
italic-ϕ
𝑇
𝑢
𝐷
\phi_{T,u}|D
italic_ϕ start_POSTSUBSCRIPT italic_T , italic_u end_POSTSUBSCRIPT | italic_D
ϕ
τ
,
u
|
D
conditional
subscript
italic-ϕ
𝜏
𝑢
𝐷
\phi_{\tau,u}|D
italic_ϕ start_POSTSUBSCRIPT italic_τ , italic_u end_POSTSUBSCRIPT | italic_D
τ
u
|
D
conditional
subscript
𝜏
𝑢
𝐷
\tau_{u}|D
italic_τ start_POSTSUBSCRIPT italic_u end_POSTSUBSCRIPT | italic_D
θ
T
,
u
|
D
conditional
subscript
𝜃
𝑇
𝑢
𝐷
\theta_{T,u}|D
italic_θ start_POSTSUBSCRIPT italic_T , italic_u end_POSTSUBSCRIPT | italic_D
θ
τ
,
u
|
D
conditional
subscript
𝜃
𝜏
𝑢
𝐷
\theta_{\tau,u}|D
italic_θ start_POSTSUBSCRIPT italic_τ , italic_u end_POSTSUBSCRIPT | italic_D
ϕ
T
,
u
|
D
M
conditional
subscript
italic-ϕ
𝑇
𝑢
subscript
𝐷
𝑀
\phi_{T,u}|D_{M}
italic_ϕ start_POSTSUBSCRIPT italic_T , italic_u end_POSTSUBSCRIPT | italic_D start_POSTSUBSCRIPT italic_M end_POSTSUBSCRIPT
ϕ
τ
,
u
|
D
M
conditional
subscript
italic-ϕ
𝜏
𝑢
subscript
𝐷
𝑀
\phi_{\tau,u}|D_{M}
italic_ϕ start_POSTSUBSCRIPT italic_τ , italic_u end_POSTSUBSCRIPT | italic_D start_POSTSUBSCRIPT italic_M end_POSTSUBSCRIPT
η
T
,
u
|
D
r
conditional
subscript
𝜂
𝑇
𝑢
subscript
𝐷
𝑟
\eta_{T,u}|D_{r}
italic_η start_POSTSUBSCRIPT italic_T , italic_u end_POSTSUBSCRIPT | italic_D start_POSTSUBSCRIPT italic_r end_POSTSUBSCRIPT
η
τ
,
u
|
D
r
conditional
subscript
𝜂
𝜏
𝑢
subscript
𝐷
𝑟
\eta_{\tau,u}|D_{r}
italic_η start_POSTSUBSCRIPT italic_τ , italic_u end_POSTSUBSCRIPT | italic_D start_POSTSUBSCRIPT italic_r end_POSTSUBSCRIPT
η
T
,
u
|
D
l
conditional
subscript
𝜂
𝑇
𝑢
subscript
𝐷
𝑙
\eta_{T,u}|D_{l}
italic_η start_POSTSUBSCRIPT italic_T , italic_u end_POSTSUBSCRIPT | italic_D start_POSTSUBSCRIPT italic_l end_POSTSUBSCRIPT
η
τ
,
u
|
D
l
conditional
subscript
𝜂
𝜏
𝑢
subscript
𝐷
𝑙
\eta_{\tau,u}|D_{l}
italic_η start_POSTSUBSCRIPT italic_τ , italic_u end_POSTSUBSCRIPT | italic_D start_POSTSUBSCRIPT italic_l end_POSTSUBSCRIPT
0.02
0.00
0.00
47
0.51
0.24
0.14
0.09
-0.28
-0.15
-0.28
-0.19
0.04
0.00
0.01
47
0.53
0.34
0.20
0.12
-0.40
-0.21
-0.40
-0.18
0.06
0.00
0.02
47
0.54
0.42
0.28
0.15
-0.54
-0.27
-0.54
-0.23
0.08
0.01
0.02
47
0.55
0.47
0.37
0.19
-0.71
-0.32
-0.71
-0.28
0.10
0.01
0.03
47
0.50
0.57
0.44
0.22
-0.84
-0.36
-0.85
-0.33
0.12
0.01
0.03
34
0.53
0.64
0.49
0.13
-0.92
-0.18
-0.93
-0.14
0.14
0.01
0.03
34
0.55
0.65
0.51
0.14
-0.97
-0.19
-0.98
-0.16
5
Simulation Research on Asymmetry Iron Condor
This section examines the dynamics of an intriguing Asymmetric Iron Condor portfolio, which exhibits non-martingale behavior based on our simulation results. We define the asymmetry of an Iron Condor portfolio as the unbalanced moneyness between the bull put spread and the bear call spread. When
x
¯
>
0
¯
𝑥
0
\bar{x}>0
over¯ start_ARG italic_x end_ARG > 0
, indicating that the put spread is deeper OTM, the portfolio is referred to as left-biased. Conversely, when
x
¯
<
0
¯
𝑥
0
\bar{x}<0
over¯ start_ARG italic_x end_ARG < 0
, indicating that the call spread is deeper OTM, the portfolio is called right-biased.
To clarify the analysis, we first present a balanced baseline in Figure
9
and then provide a comparative discussion of the left-biased and right-biased portfolios in Figures
10
and
11
Figure 9:
Distributions of (a)
ϕ
t
,
u
⁢
(
ω
)
|
D
conditional
subscript
italic-ϕ
𝑡
𝑢
𝜔
𝐷
\phi_{t,u}(\omega)|D
italic_ϕ start_POSTSUBSCRIPT italic_t , italic_u end_POSTSUBSCRIPT ( italic_ω ) | italic_D
; (b)
ϕ
t
,
u
⁢
(
ω
)
|
D
M
conditional
subscript
italic-ϕ
𝑡
𝑢
𝜔
subscript
𝐷
𝑀
\phi_{t,u}(\omega)|D_{M}
italic_ϕ start_POSTSUBSCRIPT italic_t , italic_u end_POSTSUBSCRIPT ( italic_ω ) | italic_D start_POSTSUBSCRIPT italic_M end_POSTSUBSCRIPT
; (c)
η
t
,
u
|
D
r
conditional
subscript
𝜂
𝑡
𝑢
subscript
𝐷
𝑟
\eta_{t,u}|D_{r}
italic_η start_POSTSUBSCRIPT italic_t , italic_u end_POSTSUBSCRIPT | italic_D start_POSTSUBSCRIPT italic_r end_POSTSUBSCRIPT
and (d)
η
t
,
u
|
D
l
conditional
subscript
𝜂
𝑡
𝑢
subscript
𝐷
𝑙
\eta_{t,u}|D_{l}
italic_η start_POSTSUBSCRIPT italic_t , italic_u end_POSTSUBSCRIPT | italic_D start_POSTSUBSCRIPT italic_l end_POSTSUBSCRIPT
for the symmetric portfolio with
x
¯
=
0
,
k
=
[
0.92
,
0.96
,
1.04
,
1.08
]
formulae-sequence
¯
𝑥
0
𝑘
0.92
0.96
1.04
1.08
\bar{x}=0,k=[0.92,0.96,1.04,1.08]
over¯ start_ARG italic_x end_ARG = 0 , italic_k = [ 0.92 , 0.96 , 1.04 , 1.08 ]
. The red lines represent the expectations.
As shown in Figure
10
(a),
ϕ
t
,
u
|
D
conditional
subscript
italic-ϕ
𝑡
𝑢
𝐷
\phi_{t,u}|D
italic_ϕ start_POSTSUBSCRIPT italic_t , italic_u end_POSTSUBSCRIPT | italic_D
exhibits a slightly concave shape, suggesting non-martingale properties that may indicate arbitrage opportunities. Furthermore,
η
t
,
u
|
D
r
conditional
subscript
𝜂
𝑡
𝑢
subscript
𝐷
𝑟
\eta_{t,u}|D_{r}
italic_η start_POSTSUBSCRIPT italic_t , italic_u end_POSTSUBSCRIPT | italic_D start_POSTSUBSCRIPT italic_r end_POSTSUBSCRIPT
and
η
t
,
u
|
D
l
conditional
subscript
𝜂
𝑡
𝑢
subscript
𝐷
𝑙
\eta_{t,u}|D_{l}
italic_η start_POSTSUBSCRIPT italic_t , italic_u end_POSTSUBSCRIPT | italic_D start_POSTSUBSCRIPT italic_l end_POSTSUBSCRIPT
become asymmetric: the risk of
η
t
,
u
|
D
r
conditional
subscript
𝜂
𝑡
𝑢
subscript
𝐷
𝑟
\eta_{t,u}|D_{r}
italic_η start_POSTSUBSCRIPT italic_t , italic_u end_POSTSUBSCRIPT | italic_D start_POSTSUBSCRIPT italic_r end_POSTSUBSCRIPT
increases significantly compared to the symmetric case shown in Figure
9
(c), while the values of
η
t
,
u
|
D
l
conditional
subscript
𝜂
𝑡
𝑢
subscript
𝐷
𝑙
\eta_{t,u}|D_{l}
italic_η start_POSTSUBSCRIPT italic_t , italic_u end_POSTSUBSCRIPT | italic_D start_POSTSUBSCRIPT italic_l end_POSTSUBSCRIPT
also increase compared to Figure
9
(d). Notably,
η
t
,
u
|
D
l
conditional
subscript
𝜂
𝑡
𝑢
subscript
𝐷
𝑙
\eta_{t,u}|D_{l}
italic_η start_POSTSUBSCRIPT italic_t , italic_u end_POSTSUBSCRIPT | italic_D start_POSTSUBSCRIPT italic_l end_POSTSUBSCRIPT
achieves positive values within
t
∈
[
0
,
45
]
𝑡
0
45
t\in[0,45]
italic_t ∈ [ 0 , 45 ]
, indicating reduced risk during this time interval.
Figure 10:
Distributions of (a)
ϕ
t
,
u
⁢
(
ω
)
|
D
conditional
subscript
italic-ϕ
𝑡
𝑢
𝜔
𝐷
\phi_{t,u}(\omega)|D
italic_ϕ start_POSTSUBSCRIPT italic_t , italic_u end_POSTSUBSCRIPT ( italic_ω ) | italic_D
; (b)
ϕ
t
,
u
⁢
(
ω
)
|
D
M
conditional
subscript
italic-ϕ
𝑡
𝑢
𝜔
subscript
𝐷
𝑀
\phi_{t,u}(\omega)|D_{M}
italic_ϕ start_POSTSUBSCRIPT italic_t , italic_u end_POSTSUBSCRIPT ( italic_ω ) | italic_D start_POSTSUBSCRIPT italic_M end_POSTSUBSCRIPT
; (c)
η
t
,
u
|
D
r
conditional
subscript
𝜂
𝑡
𝑢
subscript
𝐷
𝑟
\eta_{t,u}|D_{r}
italic_η start_POSTSUBSCRIPT italic_t , italic_u end_POSTSUBSCRIPT | italic_D start_POSTSUBSCRIPT italic_r end_POSTSUBSCRIPT
and (d)
η
t
,
u
|
D
l
conditional
subscript
𝜂
𝑡
𝑢
subscript
𝐷
𝑙
\eta_{t,u}|D_{l}
italic_η start_POSTSUBSCRIPT italic_t , italic_u end_POSTSUBSCRIPT | italic_D start_POSTSUBSCRIPT italic_l end_POSTSUBSCRIPT
for the left-based portfolio with
x
¯
=
0.10
,
k
=
[
0.82
,
0.86
,
1.04
,
1.08
]
formulae-sequence
¯
𝑥
0.10
𝑘
0.82
0.86
1.04
1.08
\bar{x}=0.10,k=[0.82,0.86,1.04,1.08]
over¯ start_ARG italic_x end_ARG = 0.10 , italic_k = [ 0.82 , 0.86 , 1.04 , 1.08 ]
. The red lines represent the expectations.
Figure
11
illustrates the right-biased case. Comparing Figure
11
(a) with Figure
9
(a), the Asymmetric Iron Condor introduces additional risk. Specifically,
ϕ
t
,
u
|
D
M
conditional
subscript
italic-ϕ
𝑡
𝑢
subscript
𝐷
𝑀
\phi_{t,u}|D_{M}
italic_ϕ start_POSTSUBSCRIPT italic_t , italic_u end_POSTSUBSCRIPT | italic_D start_POSTSUBSCRIPT italic_M end_POSTSUBSCRIPT
exhibits a convex increase. The risk metric
η
t
,
u
|
D
r
conditional
subscript
𝜂
𝑡
𝑢
subscript
𝐷
𝑟
\eta_{t,u}|D_{r}
italic_η start_POSTSUBSCRIPT italic_t , italic_u end_POSTSUBSCRIPT | italic_D start_POSTSUBSCRIPT italic_r end_POSTSUBSCRIPT
, shown in Figure
11
(c), displays a concave shape and achieves positive values before the 50th day, which is similar to the behavior of
η
t
,
u
|
D
l
conditional
subscript
𝜂
𝑡
𝑢
subscript
𝐷
𝑙
\eta_{t,u}|D_{l}
italic_η start_POSTSUBSCRIPT italic_t , italic_u end_POSTSUBSCRIPT | italic_D start_POSTSUBSCRIPT italic_l end_POSTSUBSCRIPT
observed in Figure
10
(d).
However, in Figure
11
(d),
η
t
,
u
|
D
l
conditional
subscript
𝜂
𝑡
𝑢
subscript
𝐷
𝑙
\eta_{t,u}|D_{l}
italic_η start_POSTSUBSCRIPT italic_t , italic_u end_POSTSUBSCRIPT | italic_D start_POSTSUBSCRIPT italic_l end_POSTSUBSCRIPT
expands compared to that in Figure
9
(d), indicating increased risk.
Figure 11:
Distributions of (a)
ϕ
t
,
u
⁢
(
ω
)
|
D
conditional
subscript
italic-ϕ
𝑡
𝑢
𝜔
𝐷
\phi_{t,u}(\omega)|D
italic_ϕ start_POSTSUBSCRIPT italic_t , italic_u end_POSTSUBSCRIPT ( italic_ω ) | italic_D
; (b)
ϕ
t
,
u
⁢
(
ω
)
|
D
M
conditional
subscript
italic-ϕ
𝑡
𝑢
𝜔
subscript
𝐷
𝑀
\phi_{t,u}(\omega)|D_{M}
italic_ϕ start_POSTSUBSCRIPT italic_t , italic_u end_POSTSUBSCRIPT ( italic_ω ) | italic_D start_POSTSUBSCRIPT italic_M end_POSTSUBSCRIPT
; (c)
η
t
,
u
|
D
r
conditional
subscript
𝜂
𝑡
𝑢
subscript
𝐷
𝑟
\eta_{t,u}|D_{r}
italic_η start_POSTSUBSCRIPT italic_t , italic_u end_POSTSUBSCRIPT | italic_D start_POSTSUBSCRIPT italic_r end_POSTSUBSCRIPT
and (d)
η
t
,
u
|
D
l
conditional
subscript
𝜂
𝑡
𝑢
subscript
𝐷
𝑙
\eta_{t,u}|D_{l}
italic_η start_POSTSUBSCRIPT italic_t , italic_u end_POSTSUBSCRIPT | italic_D start_POSTSUBSCRIPT italic_l end_POSTSUBSCRIPT
for the right-based portfolio with
x
¯
=
−
0.10
,
k
=
[
0.92
,
0.96
,
1.14
,
1.18
]
formulae-sequence
¯
𝑥
0.10
𝑘
0.92
0.96
1.14
1.18
\bar{x}=-0.10,k=[0.92,0.96,1.14,1.18]
over¯ start_ARG italic_x end_ARG = - 0.10 , italic_k = [ 0.92 , 0.96 , 1.14 , 1.18 ]
. The red lines represent the expectations.
Figure
12
illustrates the impact of asymmetry on portfolio performance. In Figure
12
(a), for left-biased portfolios,
ϕ
T
,
u
|
D
conditional
subscript
italic-ϕ
𝑇
𝑢
𝐷
\phi_{T,u}|D
italic_ϕ start_POSTSUBSCRIPT italic_T , italic_u end_POSTSUBSCRIPT | italic_D
and
ϕ
τ
,
u
|
D
conditional
subscript
italic-ϕ
𝜏
𝑢
𝐷
\phi_{\tau,u}|D
italic_ϕ start_POSTSUBSCRIPT italic_τ , italic_u end_POSTSUBSCRIPT | italic_D
overlap, indicating
τ
=
T
𝜏
𝑇
\tau=T
italic_τ = italic_T
. Moreover, the large values of
ϕ
T
,
u
|
D
conditional
subscript
italic-ϕ
𝑇
𝑢
𝐷
\phi_{T,u}|D
italic_ϕ start_POSTSUBSCRIPT italic_T , italic_u end_POSTSUBSCRIPT | italic_D
occurs in
x
<
0
𝑥
0
x<0
italic_x < 0
suggests that left-biased portfolios are more profitable.
In contrast, for right-biased portfolios,
ϕ
T
,
u
|
D
conditional
subscript
italic-ϕ
𝑇
𝑢
𝐷
\phi_{T,u}|D
italic_ϕ start_POSTSUBSCRIPT italic_T , italic_u end_POSTSUBSCRIPT | italic_D
underperforms the symmetric case, while
ϕ
τ
,
u
|
D
conditional
subscript
italic-ϕ
𝜏
𝑢
𝐷
\phi_{\tau,u}|D
italic_ϕ start_POSTSUBSCRIPT italic_τ , italic_u end_POSTSUBSCRIPT | italic_D
surpasses both the symmetric case and the left-biased portfolios. This highlights the importance of employing an optimal stopping strategy in right-biased portfolios.
Under sideways market conditions, as shown in Figure
12
(b), the left-biased portfolio significantly outperforms other scenarios. Furthermore,
θ
T
,
u
|
D
conditional
subscript
𝜃
𝑇
𝑢
𝐷
\theta_{T,u}|D
italic_θ start_POSTSUBSCRIPT italic_T , italic_u end_POSTSUBSCRIPT | italic_D
and
θ
τ
,
u
|
D
conditional
subscript
𝜃
𝜏
𝑢
𝐷
\theta_{\tau,u}|D
italic_θ start_POSTSUBSCRIPT italic_τ , italic_u end_POSTSUBSCRIPT | italic_D
achieve identical values, indicating that the optimal stopping time for the left-biased portfolio under sideways market conditions is
T
𝑇
T
italic_T
.
Despite the data generator using tuned parameters from the real SPX market, some imperfect symmetric patterns can still be observed. In Figure
12
(c), both
θ
T
,
u
|
D
conditional
subscript
𝜃
𝑇
𝑢
𝐷
\theta_{T,u}|D
italic_θ start_POSTSUBSCRIPT italic_T , italic_u end_POSTSUBSCRIPT | italic_D
and
θ
τ
,
u
|
D
conditional
subscript
𝜃
𝜏
𝑢
𝐷
\theta_{\tau,u}|D
italic_θ start_POSTSUBSCRIPT italic_τ , italic_u end_POSTSUBSCRIPT | italic_D
exhibit even symmetry around the symmetric portfolio. This indicates that asymmetric portfolios can achieve higher success rates compared to symmetric portfolios in the SPX market, although the latter case is more complex to analyze.
Moreover, in Figure
12
(d),
η
T
,
u
|
D
r
conditional
subscript
𝜂
𝑇
𝑢
subscript
𝐷
𝑟
\eta_{T,u}|D_{r}
italic_η start_POSTSUBSCRIPT italic_T , italic_u end_POSTSUBSCRIPT | italic_D start_POSTSUBSCRIPT italic_r end_POSTSUBSCRIPT
and
η
T
,
u
|
D
l
conditional
subscript
𝜂
𝑇
𝑢
subscript
𝐷
𝑙
\eta_{T,u}|D_{l}
italic_η start_POSTSUBSCRIPT italic_T , italic_u end_POSTSUBSCRIPT | italic_D start_POSTSUBSCRIPT italic_l end_POSTSUBSCRIPT
display odd symmetry about the symmetric portfolio. For the left-biased portfolio,
η
τ
,
u
|
D
l
conditional
subscript
𝜂
𝜏
𝑢
subscript
𝐷
𝑙
\eta_{\tau,u}|D_{l}
italic_η start_POSTSUBSCRIPT italic_τ , italic_u end_POSTSUBSCRIPT | italic_D start_POSTSUBSCRIPT italic_l end_POSTSUBSCRIPT
outperforms
η
T
,
u
|
D
l
conditional
subscript
𝜂
𝑇
𝑢
subscript
𝐷
𝑙
\eta_{T,u}|D_{l}
italic_η start_POSTSUBSCRIPT italic_T , italic_u end_POSTSUBSCRIPT | italic_D start_POSTSUBSCRIPT italic_l end_POSTSUBSCRIPT
, while
η
τ
,
u
|
D
r
conditional
subscript
𝜂
𝜏
𝑢
subscript
𝐷
𝑟
\eta_{\tau,u}|D_{r}
italic_η start_POSTSUBSCRIPT italic_τ , italic_u end_POSTSUBSCRIPT | italic_D start_POSTSUBSCRIPT italic_r end_POSTSUBSCRIPT
underperforms
η
T
,
u
|
D
r
conditional
subscript
𝜂
𝑇
𝑢
subscript
𝐷
𝑟
\eta_{T,u}|D_{r}
italic_η start_POSTSUBSCRIPT italic_T , italic_u end_POSTSUBSCRIPT | italic_D start_POSTSUBSCRIPT italic_r end_POSTSUBSCRIPT
. This implies that when trading a left-biased portfolio, one should adopt an optimal stopping strategy in bearish markets but refrain from using such a strategy in bullish markets. Conversely, for the right-biased portfolio, the strategy should be reversed.
Figure 12:
Influence of
x
¯
¯
𝑥
\bar{x}
over¯ start_ARG italic_x end_ARG
on (a) potential profits
ϕ
T
,
u
|
D
conditional
subscript
italic-ϕ
𝑇
𝑢
𝐷
\phi_{T,u}|D
italic_ϕ start_POSTSUBSCRIPT italic_T , italic_u end_POSTSUBSCRIPT | italic_D
and
ϕ
τ
,
u
|
D
conditional
subscript
italic-ϕ
𝜏
𝑢
𝐷
\phi_{\tau,u}|D
italic_ϕ start_POSTSUBSCRIPT italic_τ , italic_u end_POSTSUBSCRIPT | italic_D
; (b) potential profits
ϕ
T
,
u
|
D
M
conditional
subscript
italic-ϕ
𝑇
𝑢
subscript
𝐷
𝑀
\phi_{T,u}|D_{M}
italic_ϕ start_POSTSUBSCRIPT italic_T , italic_u end_POSTSUBSCRIPT | italic_D start_POSTSUBSCRIPT italic_M end_POSTSUBSCRIPT
and
ϕ
τ
,
u
|
D
M
conditional
subscript
italic-ϕ
𝜏
𝑢
subscript
𝐷
𝑀
\phi_{\tau,u}|D_{M}
italic_ϕ start_POSTSUBSCRIPT italic_τ , italic_u end_POSTSUBSCRIPT | italic_D start_POSTSUBSCRIPT italic_M end_POSTSUBSCRIPT
; (c) success rates
θ
T
,
u
|
D
conditional
subscript
𝜃
𝑇
𝑢
𝐷
\theta_{T,u}|D
italic_θ start_POSTSUBSCRIPT italic_T , italic_u end_POSTSUBSCRIPT | italic_D
and
θ
τ
,
u
|
D
conditional
subscript
𝜃
𝜏
𝑢
𝐷
\theta_{\tau,u}|D
italic_θ start_POSTSUBSCRIPT italic_τ , italic_u end_POSTSUBSCRIPT | italic_D
; (d) risks
η
T
,
u
|
D
r
conditional
subscript
𝜂
𝑇
𝑢
subscript
𝐷
𝑟
\eta_{T,u}|D_{r}
italic_η start_POSTSUBSCRIPT italic_T , italic_u end_POSTSUBSCRIPT | italic_D start_POSTSUBSCRIPT italic_r end_POSTSUBSCRIPT
,
η
τ
,
u
|
D
r
conditional
subscript
𝜂
𝜏
𝑢
subscript
𝐷
𝑟
\eta_{\tau,u}|D_{r}
italic_η start_POSTSUBSCRIPT italic_τ , italic_u end_POSTSUBSCRIPT | italic_D start_POSTSUBSCRIPT italic_r end_POSTSUBSCRIPT
,
η
T
,
u
|
D
l
conditional
subscript
𝜂
𝑇
𝑢
subscript
𝐷
𝑙
\eta_{T,u}|D_{l}
italic_η start_POSTSUBSCRIPT italic_T , italic_u end_POSTSUBSCRIPT | italic_D start_POSTSUBSCRIPT italic_l end_POSTSUBSCRIPT
(dash), and
η
τ
,
u
|
D
l
conditional
subscript
𝜂
𝜏
𝑢
subscript
𝐷
𝑙
\eta_{\tau,u}|D_{l}
italic_η start_POSTSUBSCRIPT italic_τ , italic_u end_POSTSUBSCRIPT | italic_D start_POSTSUBSCRIPT italic_l end_POSTSUBSCRIPT
(dash), respectively.
Table
3
summarizes the influence of
x
¯
¯
𝑥
\bar{x}
over¯ start_ARG italic_x end_ARG
on portfolio performance. Overall, the left-biased portfolio is observed to outperform the right-biased portfolio for the following reasons:
1.
ϕ
T
,
u
⁢
|
D
>
⁢
0
subscript
italic-ϕ
𝑇
𝑢
ket
𝐷
0
\phi_{T,u}|D>0
italic_ϕ start_POSTSUBSCRIPT italic_T , italic_u end_POSTSUBSCRIPT | italic_D > 0
for all
x
¯
<
0
¯
𝑥
0
\bar{x}<0
over¯ start_ARG italic_x end_ARG < 0
, whereas
ϕ
T
,
u
|
D
<
0
conditional
subscript
italic-ϕ
𝑇
𝑢
𝐷
0
\phi_{T,u}|D<0
italic_ϕ start_POSTSUBSCRIPT italic_T , italic_u end_POSTSUBSCRIPT | italic_D < 0
when
x
¯
>
0
¯
𝑥
0
\bar{x}>0
over¯ start_ARG italic_x end_ARG > 0
.
2.
ϕ
τ
,
u
|
D
M
conditional
subscript
italic-ϕ
𝜏
𝑢
subscript
𝐷
𝑀
\phi_{\tau,u}|D_{M}
italic_ϕ start_POSTSUBSCRIPT italic_τ , italic_u end_POSTSUBSCRIPT | italic_D start_POSTSUBSCRIPT italic_M end_POSTSUBSCRIPT
of the left-biased portfolio strictly dominates that of the right-biased portfolio.
3. Under the same absolute level of
|
x
¯
|
¯
𝑥
|\bar{x}|
| over¯ start_ARG italic_x end_ARG |
,
θ
T
,
u
|
D
conditional
subscript
𝜃
𝑇
𝑢
𝐷
\theta_{T,u}|D
italic_θ start_POSTSUBSCRIPT italic_T , italic_u end_POSTSUBSCRIPT | italic_D
of the left-biased portfolio conditionally dominates that of the right-biased portfolio.
4. The maximum risk of the left-biased portfolio is lower than that of the right-biased ones.
However, the right-biased Iron Condor heavily depends on precise optimal stopping time to truncate extreme losses before expiration, which may cause
ϕ
τ
,
u
⁢
|
D
,
x
>
⁢
0
subscript
italic-ϕ
𝜏
𝑢
ket
𝐷
𝑥
0
\phi_{\tau,u}|D,x>0
italic_ϕ start_POSTSUBSCRIPT italic_τ , italic_u end_POSTSUBSCRIPT | italic_D , italic_x > 0
to be dominated by
ϕ
T
,
u
|
D
,
x
≤
0
conditional
subscript
italic-ϕ
𝑇
𝑢
𝐷
𝑥
0
\phi_{T,u}|D,x\leq 0
italic_ϕ start_POSTSUBSCRIPT italic_T , italic_u end_POSTSUBSCRIPT | italic_D , italic_x ≤ 0
. Nevertheless, it leads to
η
τ
,
u
⁢
|
D
r
,
x
>
⁢
0
subscript
𝜂
𝜏
𝑢
ket
subscript
𝐷
𝑟
𝑥
0
\eta_{\tau,u}|D_{r},x>0
italic_η start_POSTSUBSCRIPT italic_τ , italic_u end_POSTSUBSCRIPT | italic_D start_POSTSUBSCRIPT italic_r end_POSTSUBSCRIPT , italic_x > 0
strictly dominating
η
τ
,
u
|
D
l
,
x
≤
0
conditional
subscript
𝜂
𝜏
𝑢
subscript
𝐷
𝑙
𝑥
0
\eta_{\tau,u}|D_{l},x\leq 0
italic_η start_POSTSUBSCRIPT italic_τ , italic_u end_POSTSUBSCRIPT | italic_D start_POSTSUBSCRIPT italic_l end_POSTSUBSCRIPT , italic_x ≤ 0
, and
η
τ
,
u
⁢
|
D
l
,
x
>
⁢
0
subscript
𝜂
𝜏
𝑢
ket
subscript
𝐷
𝑙
𝑥
0
\eta_{\tau,u}|D_{l},x>0
italic_η start_POSTSUBSCRIPT italic_τ , italic_u end_POSTSUBSCRIPT | italic_D start_POSTSUBSCRIPT italic_l end_POSTSUBSCRIPT , italic_x > 0
strictly dominating
η
τ
,
u
|
D
r
,
x
≤
0
conditional
subscript
𝜂
𝜏
𝑢
subscript
𝐷
𝑟
𝑥
0
\eta_{\tau,u}|D_{r},x\leq 0
italic_η start_POSTSUBSCRIPT italic_τ , italic_u end_POSTSUBSCRIPT | italic_D start_POSTSUBSCRIPT italic_r end_POSTSUBSCRIPT , italic_x ≤ 0
.
The results are intriguing, and we aim to analyze them further.
Since the S&P 500 index is predominantly bullish over time, establishing a left-biased portfolio provides greater tolerance for the upward movement of the underlying price while ensuring the terminal price remains within the profitable region. As a result, the left-biased Iron Condor is appealing for long-term holding and consistently outperforms the commonly used symmetric Iron Condor strategy.
From Table
3
, the optimal stopping time
τ
𝜏
\tau
italic_τ
for the right-biased portfolio varies between 34 and 41 days out of the total 63-day period, representing approximately 54% to 65% of the entire duration.
Table 3:
Portfolio performance metrics for different asymmetry degrees
x
¯
¯
𝑥
\bar{x}
over¯ start_ARG italic_x end_ARG
ϕ
T
,
u
|
D
conditional
subscript
italic-ϕ
𝑇
𝑢
𝐷
\phi_{T,u}|D
italic_ϕ start_POSTSUBSCRIPT italic_T , italic_u end_POSTSUBSCRIPT | italic_D
ϕ
τ
,
u
|
D
conditional
subscript
italic-ϕ
𝜏
𝑢
𝐷
\phi_{\tau,u}|D
italic_ϕ start_POSTSUBSCRIPT italic_τ , italic_u end_POSTSUBSCRIPT | italic_D
τ
u
|
D
conditional
subscript
𝜏
𝑢
𝐷
\tau_{u}|D
italic_τ start_POSTSUBSCRIPT italic_u end_POSTSUBSCRIPT | italic_D
θ
T
,
u
|
D
conditional
subscript
𝜃
𝑇
𝑢
𝐷
\theta_{T,u}|D
italic_θ start_POSTSUBSCRIPT italic_T , italic_u end_POSTSUBSCRIPT | italic_D
θ
τ
,
u
|
D
conditional
subscript
𝜃
𝜏
𝑢
𝐷
\theta_{\tau,u}|D
italic_θ start_POSTSUBSCRIPT italic_τ , italic_u end_POSTSUBSCRIPT | italic_D
ϕ
T
,
u
|
D
M
conditional
subscript
italic-ϕ
𝑇
𝑢
subscript
𝐷
𝑀
\phi_{T,u}|D_{M}
italic_ϕ start_POSTSUBSCRIPT italic_T , italic_u end_POSTSUBSCRIPT | italic_D start_POSTSUBSCRIPT italic_M end_POSTSUBSCRIPT
ϕ
τ
,
u
|
D
M
conditional
subscript
italic-ϕ
𝜏
𝑢
subscript
𝐷
𝑀
\phi_{\tau,u}|D_{M}
italic_ϕ start_POSTSUBSCRIPT italic_τ , italic_u end_POSTSUBSCRIPT | italic_D start_POSTSUBSCRIPT italic_M end_POSTSUBSCRIPT
η
T
,
u
|
D
r
conditional
subscript
𝜂
𝑇
𝑢
subscript
𝐷
𝑟
\eta_{T,u}|D_{r}
italic_η start_POSTSUBSCRIPT italic_T , italic_u end_POSTSUBSCRIPT | italic_D start_POSTSUBSCRIPT italic_r end_POSTSUBSCRIPT
η
τ
,
u
|
D
r
conditional
subscript
𝜂
𝜏
𝑢
subscript
𝐷
𝑟
\eta_{\tau,u}|D_{r}
italic_η start_POSTSUBSCRIPT italic_τ , italic_u end_POSTSUBSCRIPT | italic_D start_POSTSUBSCRIPT italic_r end_POSTSUBSCRIPT
η
T
,
u
|
D
l
conditional
subscript
𝜂
𝑇
𝑢
subscript
𝐷
𝑙
\eta_{T,u}|D_{l}
italic_η start_POSTSUBSCRIPT italic_T , italic_u end_POSTSUBSCRIPT | italic_D start_POSTSUBSCRIPT italic_l end_POSTSUBSCRIPT
η
τ
,
u
|
D
l
conditional
subscript
𝜂
𝜏
𝑢
subscript
𝐷
𝑙
\eta_{\tau,u}|D_{l}
italic_η start_POSTSUBSCRIPT italic_τ , italic_u end_POSTSUBSCRIPT | italic_D start_POSTSUBSCRIPT italic_l end_POSTSUBSCRIPT
0.10
-0.05
0.08
41
0.60
0.64
0.43
0.27
-1.88
0.29
-0.18
-0.91
0.08
-0.04
0.07
34
0.58
0.64
0.48
0.19
-1.66
0.19
-0.47
-0.54
0.06
-0.04
0.06
34
0.54
0.64
0.52
0.18
-1.42
0.06
-0.87
-0.42
0.00
0.00
0.03
47
0.45
0.55
0.38
0.21
-0.73
-0.34
-0.73
-0.3
-0.06
0.05
0.05
62
0.59
0.59
0.63
0.63
-0.78
-1.33
-1.33
-0.78
-0.08
0.05
0.05
62
0.61
0.61
0.59
0.59
-0.42
-1.53
-1.53
-0.42
-0.10
0.04
0.04
62
0.63
0.63
0.56
0.56
-0.13
-1.72
-1.72
-0.13
6
Validation on Actual SPX Market
This section examines the performance of Iron Condor portfolios in the actual SPX market to validate the prior findings. We replace
ϕ
t
,
u
subscript
italic-ϕ
𝑡
𝑢
\phi_{t,u}
italic_ϕ start_POSTSUBSCRIPT italic_t , italic_u end_POSTSUBSCRIPT
with (normalized) Profit & Loss (P&L) as the metric since only a single realization is available in real world. Specifically, we analyze three cases, with time frames spanning 63, 63, and 49 trading days prior to the options’ maturity dates of 2020-12-18, 2021-07-16, and 2022-10-21, respectively. The data used in this analysis is sourced from the OptionMetrics database.
Figure
13
presents the performance of portfolios with varying
x
𝑥
x
italic_x
,
x
^
^
𝑥
\hat{x}
over^ start_ARG italic_x end_ARG
, and
x
¯
¯
𝑥
\bar{x}
over¯ start_ARG italic_x end_ARG
during a bullish market (Figure
13
(a)), where
S
T
>
3700
subscript
𝑆
𝑇
3700
S_{T}>3700
italic_S start_POSTSUBSCRIPT italic_T end_POSTSUBSCRIPT > 3700
.
Figure
13
(b) shows the P&L of portfolios with different
x
𝑥
x
italic_x
values. Only the portfolio with the maximum
x
𝑥
x
italic_x
(purple line) achieves its maximum profit, while 3 out of 5 portfolios result in negative returns, indicating a low success rate in this scenario.
Figure
13
(c) shows the influence of
x
^
^
𝑥
\hat{x}
over^ start_ARG italic_x end_ARG
. Unfortunately, none of these portfolios achieve positive return.
Promising results emerge in Figure
13
(d), where a mirror symmetry of left-biased portfolios and right-biased portfolios about the symmetric portfolio is observed, which is very interesting. All left-biased portfolios achieve their maximum profit, whereas symmetric and right-biased portfolios all get negative return. This align with our simulation results that the left-biased Iron Condor can effectively handle risks resulting from bullish markets.
Figure 13:
(a) Trajectory of underlying prices
S
t
subscript
𝑆
𝑡
S_{t}
italic_S start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT
; and portfolios P&L influenced by (b) moneyness
x
𝑥
x
italic_x
; (c) strike span
x
^
^
𝑥
\hat{x}
over^ start_ARG italic_x end_ARG
; and (d) asymmtry degree
x
¯
¯
𝑥
\bar{x}
over¯ start_ARG italic_x end_ARG
values, during a bullish market observed over 63 trading days prior to the options’ maturity date of 2020-12-18.
Figure
14
illustrates the P&L performance of portfolios under a sideways market condition (Figure
14
(a)).
In Figure
14
(b), four out of five portfolios achieve their maximum profits, except the one with the smallest
x
𝑥
x
italic_x
. This result aligns with our simulation findings that increasing
x
𝑥
x
italic_x
improves the success rate.
In Figure
14
(c), for portfolios with a fixed
x
𝑥
x
italic_x
, varying
x
^
^
𝑥
\hat{x}
over^ start_ARG italic_x end_ARG
results in similar P&L patterns. Notably, three out of five portfolios yield positive returns.
In Figure
14
(d), all left-biased portfolios achieve their maximum profits, whereas the symmetric and right-biased portfolios exhibit poorer performance. This observation further supports the outcomes from our simulations.
Moreover, adopting an optimal stopping strategy within the time interval [40, 50] days can significantly reduce overall risks, even if the profits of some portfolios are slightly diminished.
Figure 14:
(a) Trajectory of underlying prices
S
t
subscript
𝑆
𝑡
S_{t}
italic_S start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT
; (b) portfolios P&L under varying
x
𝑥
x
italic_x
values; (c) portfolios P&L under varying
x
^
^
𝑥
\hat{x}
over^ start_ARG italic_x end_ARG
values; and (d) portfolios P&L under varying
x
¯
¯
𝑥
\bar{x}
over¯ start_ARG italic_x end_ARG
values, during a sideway market observed over 63 trading days prior to the options’ maturity date of 2020-07-16
Finally, Figure
15
(a) depicts an extreme bearish market. Under such conditions, changes in either
x
𝑥
x
italic_x
or
x
^
^
𝑥
\hat{x}
over^ start_ARG italic_x end_ARG
result in losses, as shown in Figures
15
(b) and
15
(c).
However, constructing an asymmetric right-biased Iron Condor portfolio leads to positive returns, aligning with our findings from simulations.
Figure 15:
(a) Trajectory of underlying prices
S
t
subscript
𝑆
𝑡
S_{t}
italic_S start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT
; (b) portfolios P&L under varying
x
𝑥
x
italic_x
values; (c) portfolios P&L under varying
x
^
^
𝑥
\hat{x}
over^ start_ARG italic_x end_ARG
values; and (d) portfolios P&L under varying
x
¯
¯
𝑥
\bar{x}
over¯ start_ARG italic_x end_ARG
values, during a bearish market observed over 49 trading days prior to the options’ maturity date of 2022-10-21
7
Conclusion
This paper provides an in-depth analysis of the influence of the control process
u
⁢
(
k
i
,
τ
)
𝑢
subscript
𝑘
𝑖
𝜏
u(k_{i},\tau)
italic_u ( italic_k start_POSTSUBSCRIPT italic_i end_POSTSUBSCRIPT , italic_τ )
(
u
⁢
(
x
,
x
^
,
x
¯
,
τ
)
𝑢
𝑥
^
𝑥
¯
𝑥
𝜏
u(x,\hat{x},\bar{x},\tau)
italic_u ( italic_x , over^ start_ARG italic_x end_ARG , over¯ start_ARG italic_x end_ARG , italic_τ )
) on Iron Condor portfolios.
Initially, we assume the underlying price process
S
t
subscript
𝑆
𝑡
S_{t}
italic_S start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT
is a bounded martingale within
[
K
2
,
K
3
]
subscript
𝐾
2
subscript
𝐾
3
[K_{2},K_{3}]
[ italic_K start_POSTSUBSCRIPT 2 end_POSTSUBSCRIPT , italic_K start_POSTSUBSCRIPT 3 end_POSTSUBSCRIPT ]
, and then prove a theorem that the optimal stopping time is
τ
=
T
𝜏
𝑇
\tau=T
italic_τ = italic_T
if the Iron Condor strike structure satisfies
k
1
<
k
2
=
K
2
<
S
t
<
k
3
=
K
3
<
k
4
subscript
𝑘
1
subscript
𝑘
2
subscript
𝐾
2
subscript
𝑆
𝑡
subscript
𝑘
3
subscript
𝐾
3
subscript
𝑘
4
k_{1}<k_{2}=K_{2}<S_{t}<k_{3}=K_{3}<k_{4}
italic_k start_POSTSUBSCRIPT 1 end_POSTSUBSCRIPT < italic_k start_POSTSUBSCRIPT 2 end_POSTSUBSCRIPT = italic_K start_POSTSUBSCRIPT 2 end_POSTSUBSCRIPT < italic_S start_POSTSUBSCRIPT italic_t end_POSTSUBSCRIPT < italic_k start_POSTSUBSCRIPT 3 end_POSTSUBSCRIPT = italic_K start_POSTSUBSCRIPT 3 end_POSTSUBSCRIPT < italic_k start_POSTSUBSCRIPT 4 end_POSTSUBSCRIPT
for all
t
∈
[
0
,
T
]
𝑡
0
𝑇
t\in[0,T]
italic_t ∈ [ 0 , italic_T ]
.
We extend our study to more general scenarios by employing a simulation method based on the data generator derived from the Rough Heston model. We parameterize
u
𝑢
u
italic_u
using four variables: moneyness (
x
𝑥
x
italic_x
), strike span (
x
^
^
𝑥
\hat{x}
over^ start_ARG italic_x end_ARG
), asymmetry degree (
x
¯
¯
𝑥
\bar{x}
over¯ start_ARG italic_x end_ARG
) and optimal stopping time
τ
𝜏
\tau
italic_τ
. The simulation results reveal the following key findings:
(1) Asymmetric, left-biased Iron Condor portfolios with
τ
=
T
𝜏
𝑇
\tau=T
italic_τ = italic_T
tend to be optimal in SPX markets, balancing profitability and risk management effectively;
(2) Deep OTM Iron Condor portfolios improve profitability and success rates, but they also introduce the risk of extreme losses. Adopting an optimal stopping strategy can mitigate these losses, although it slightly reduces potential profits;
(3)
τ
𝜏
\tau
italic_τ
generally falls between 50% and 75% of the total duration, except for left-biased portfolios.
Finally, we validate our findings on the actual SPX market through three case studies covering bullish, sideways, and bearish market conditions, and the results support the simulation findings.
There are two limitations to the current research:
1.
The optimal stopping strategies derived from simulation methods need to be quantified and further analyzed.
2.
The approach requires a market trend forecasting method for effective strategy design.
The first limitation may be addressed using entropy-based methods, while the second could be further investigated through machine learning techniques in future research.
Declarations
1.
Funding:
This work is supported by National Natural Fundation of China (No.42402239) and Jiangsu University of Science and Technology Scientific Research Start-up Fund (No.1132932306).
2.
Competing Interest declaration:
The author declare that they have no known competing financial interests or personal relationships that could have appeared to influence the work reported in this paper.
3.
Data Availability Statement:
The author do not have permission to share data.
4.
Author Contributions:
Qiguo Sun: Conceptualization, Investigation, Methodology, Analysis, Writing - Review& Editing.
Hanyue Huang: Conceptualization, Investigation, Methodology, Analysis, Writing - Review& Editing.
Xibei Yang: Methodology
5.
Ethical statement:
All data handling procedures adhered to ethical guidelines for research.
References
Abi Jaber (2019)
Eduardo Abi Jaber.
Lifting the heston model.
Quantitative finance
, 19(12):1995–2013, 2019.
Bayer et al. (2016)
Christian Bayer, Peter Friz, and Jim Gatheral.
Pricing under rough volatility.
Quantitative Finance
, 16(6):887–904, 2016.
Bennedsen et al. (2017)
Mikkel Bennedsen, Asger Lunde, and Mikko S Pakkanen.
Hybrid scheme for brownian semistationary processes.
Finance and Stochastics
, 21:931–965, 2017.
Cohen (2005)
Guy Cohen.
The bible of options strategies: the definitive guide for practical trading strategies
.
Pearson Education, 2005.
de Saint-Cyr (2023)
Alberic de Saint-Cyr.
A simple historical analysis of the performance of iron condors on the spx.
Available at SSRN 4643378
, 2023.
Dupire et al. (1994)
Bruno Dupire et al.
Pricing with a smile.
Risk
, 7(1):18–20, 1994.
Dziawgo (2020)
Ewa Dziawgo.
The iron condor strategy in financial risk management.
Prace Naukowe Uniwersytetu Ekonomicznego we Wrocławiu
, 64(2):33–44, 2020.
El Euch et al. (2019)
Omar El Euch, Jim Gatheral, and Mathieu Rosenbaum.
Roughening heston.
Risk
, pages 84–89, 2019.
Fadugba (2020)
Sunday Emmanuel Fadugba.
Homotopy analysis method and its applications in the valuation of european call options with time-fractional black-scholes equation.
Chaos, Solitons & Fractals
, 141:110351, 2020.
Gatheral and Jacquier (2014)
Jim Gatheral and Antoine Jacquier.
Arbitrage-free svi volatility surfaces.
Quantitative Finance
, 14(1):59–71, 2014.
Heston (1993)
Steven L Heston.
A closed-form solution for options with stochastic volatility with applications to bond and currency options.
The review of financial studies
, 6(2):327–343, 1993.
Hu and Øksendal (2003)
Yaozhong Hu and Bernt Øksendal.
Fractional white noise calculus and applications to finance.
Infinite dimensional analysis, quantum probability and related topics
, 6(01):1–32, 2003.
Ma and Wu (2022)
Jingtang Ma and Haofei Wu.
A fast algorithm for simulation of rough volatility models.
Quantitative Finance
, 22(3):447–462, 2022.
Wang et al. (2022)
Jian Wang, Shuai Wen, Mengdie Yang, and Wei Shao.
Practical finite difference method for solving multi-dimensional black-scholes model in fractal market.
Chaos, Solitons & Fractals
, 157:111895, 2022.
Wong and Bilokon (2024)
Yat Chun Chester Wong and Paul Bilokon.
Simulation of fractional brownian motion and related stochastic processes in practice: A straightforward approach.
Available at SSRN
, 2024.
Woodard (2011)
Jared Woodard.
Iron Condor Spread Strategies: Timing, Structuring, and Managing Profitable Options Trades
.
Pearson Education, 2011.