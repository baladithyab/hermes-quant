---
title: portfolio management - Analyzing the angle between vector of weights and vector
  of returns in mean-variance optimization - Quantitative Finance Stack Exchange
id: portfolio-management-analyzing-the-angle-between-vector-of-weights-and-vector-of
tags:
- minimum-observations statistics edge-detection
created: '2026-06-17T20:06:05.615918Z'
updated: '2026-06-17T20:28:22.927130Z'
source: https://quant.stackexchange.com/questions/7026/how-many-observations-needed-for-out-of-sample-test-to-trust-results
source_domain: quant.stackexchange.com
fetched_at: '2026-06-17T20:06:05.615735Z'
fetch_provider: builtin
status: review
type: note
tier: practitioner
content_type: forum
deprecated: false
summary: portfolio management - Analyzing the angle between vector of weights and
  vector of returns in mean-variance optimizat...
---

portfolio management - Analyzing the angle between vector of weights and vector of returns in mean-variance optimization - Quantitative Finance Stack Exchange
Stack Internal
Knowledge at work
Bring the best of human thought and AI automation together at your work.
Explore Stack Internal
Analyzing the angle between vector of weights and vector of returns in mean-variance optimization
Ask Question
Asked
13 years, 5 months ago
Modified
13 years, 5 months ago
Viewed
472 times
5
$\begingroup$
I am using the paper "A Sharper Angle on Optimization" by Golts and Jones (2009) as a basis for my (minor) masters thesis in mathematical finance. The paper focuses on the mean-variance analysis of Markowitz but instead turns attention to the vector geometry of the returns vector and vector of resultant portfolio weights.  As it is a working paper, most of the concepts are not elaborated on well enough to make sense or for one to implement by him/herself.  The paper may be accessed on this link:
http://ssrn.com/abstract=1483412
.
One of the ideas I am struggling with is the angle between the returns vector and vector of weights and how this angle can be related to the condition number of the covariance matrix.  The authors then employ robust optimization techniques to control this angle (i.e. minimize it) to obtain more intuitive investment portfolios.
The authors state that the angle between the returns and positions vector, call it $\omega$,  is bounded from below as:
$\cos(\omega)=\frac{\alpha^{T}\Sigma^{-1}\alpha}{\sqrt{\alpha^{T}\alpha}\sqrt{\alpha^{T}\Sigma^{-2}\alpha}} \geq \frac{\theta_{\max}\theta_{\min}}{(\theta_{\max}^{2}+\theta_{\min}^{2})/2}$
where $\alpha$ is the vector of returns and $\Sigma$ is the covariance matrix with spectral decomposition given by  $\Sigma=Q^{T}\mbox{diag}(\theta_{1}^{2},...,\theta_{n}^{2})Q$ where $\theta_{1}^{2} \geq \theta_{2}^{2} \geq ... \geq \theta_{n}^{2} > 0$ are the eigenvalues in decreasing order and where we let $\theta_{\max}^{2}=\theta_{1}^{2}$ and $\theta_{\min}^{2}=\theta_{n}^{2}$.
If anyone has any ideas on how the authors may have arrived at this, as well as what it means graphically, I would really appreciate it.
Many thanks in advance!
portfolio-management
optimization
covariance
mean-variance
eigenvalue
Share
Improve this question
Follow
edited
Jan 20, 2013 at 21:20
asked
Jan 18, 2013 at 21:08
Geraldine Bailey
165
5
5 bronze badges
$\endgroup$
4
1
$\begingroup$
how is this related to finance?
$\endgroup$
Matt Wolf
–
Matt Wolf
2013-01-19 02:34:38 +00:00
Commented
Jan 19, 2013 at 2:34
$\begingroup$
@Freddy:  We are dealing with the Markowitz mean-variance optimization setup. Sorry if that was not clear.
$\endgroup$
Geraldine Bailey
–
Geraldine Bailey
2013-01-19 10:49:11 +00:00
Commented
Jan 19, 2013 at 10:49
$\begingroup$
@Geraldine Bailey Hi, maybe
An algorithm for the orthogonal decomposition of ﬁnancial return data
helps. It seems to be written in a similar spirit.
$\endgroup$
Richi Wa
–
Richi Wa
2013-01-27 21:35:28 +00:00
Commented
Jan 27, 2013 at 21:35
$\begingroup$
@Richard:  Thank you! Will take a look at it.
$\endgroup$
Geraldine Bailey
–
Geraldine Bailey
2013-01-28 00:09:00 +00:00
Commented
Jan 28, 2013 at 0:09
Add a comment
|
0
Sorted by:
Reset to default
Highest score (default)
Date modified (newest first)
Date created (oldest first)
Know someone who can answer? Share a link to this
question
via
email
,
Twitter
, or
Facebook
.
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
portfolio-management
optimization
covariance
mean-variance
eigenvalue
See similar questions with these tags.
Featured on Meta
Native Ads Coming To Comments
Related
5
Robust-Bayesian optimization in Markowitz framework
6
Robust Bayesian portfolio optimization in matlab?
4
Monte Carlo based mean variance optimization
1
Solving a Markowitz problem with restrictions (lower and upper bound) to the weights vector
2
Linear Regression vs Mean Variance Optimization
0
Mean-variance optimization - objective function formation with factor models
Hot Network Questions
Anti-AI license that forbids end-users' AI use
Do Large creatures automatically have Reach?
Looking for true 100 amp DC SSR solution for 60 VDC 2 kW inductive heating coil load
Hat Game with 2 prisoners
Can I backup multiple Mac devices to a NAS on the LAN?
Golf all the logic gates with X inputs and Y outputs!
What are the names of the various parts of a SMTP `Received:` header?
Are Vishnu and Parvati twins?
Can we observe and have we observed 'time-refraction'?
отвести = to conduct for a whole period
Best practice for GND copper planes with basic PCB design
Applications of Egorov's theorem beyond the Bounded Convergence Theorem
Does 1 Cor. 13:8, 13 teach that faith, hope, & love will outlast miraculous gifts?
Short story - probably by Greg Egan - laborers sing "Dehumanize Yourself"
How to fix force unslant error in XeTeX/LuaTeX?
Wheeled cart tips over instead of climbing over a bump
Does the half-minotaur template truly add Str and Con (and remove Dex) via size changes?
What does 褃劲 mean?
Statistics of a distribution on unitary matrices
Electrical Current Traveling Down a Curve?
Does Quine believe a phenomenalist conceptual scheme can be translated into a physicalist conceptual scheme?
How to reduce the sweetness of a sauce?
What is the point of making a timeline for your world?
IPO stocks: does selling covered calls trigger the flipping rule?
more hot questions
Question feed
default