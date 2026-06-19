---
title: fixed income - What is drift in interest rate term structure model - Quantitative
  Finance Stack Exchange
id: fixed-income-what-is-drift-in-interest-rate-term-structure-model-quantitative-fi
tags:
- options-risk
created: '2026-06-17T20:12:08.310374Z'
updated: '2026-06-17T20:28:23.103700Z'
source: https://quant.stackexchange.com/questions/23215/risk-management-for-spread-strategies-vs-individual-legs
source_domain: quant.stackexchange.com
fetched_at: '2026-06-17T20:12:08.310194Z'
fetch_provider: builtin
status: review
type: note
tier: practitioner
content_type: forum
deprecated: false
summary: fixed income - What is drift in interest rate term structure model - Quantitative
  Finance Stack Exchange
---

fixed income - What is drift in interest rate term structure model - Quantitative Finance Stack Exchange
Stack Internal
Knowledge at work
Bring the best of human thought and AI automation together at your work.
Explore Stack Internal
What is drift in interest rate term structure model
Ask Question
Asked
10 years, 4 months ago
Modified
10 years, 4 months ago
Viewed
3k times
3
$\begingroup$
I was studying about the interest rate term structures and i came across term structure model with (and without) drift.
I am really unsure about what this drift is in this equation for term structure model. 
$$dr=\lambda dt + \sigma dw$$
From the equation above $\lambda$ is the drift factor and $\lambda dt$ is the drift. I have a very confusing explanation of drift which is along the lines of interest rates are moved in the future by some factor.
Can someone give me an explanation of drift. An example associated with it would be ideal. Thanks!
fixed-income
interest-rates
term-structure
Share
Improve this question
Follow
edited
Feb 10, 2016 at 20:04
Neeraj
2,278
15
15 silver badges
32
32 bronze badges
asked
Feb 10, 2016 at 14:53
stud91
137
3
3 silver badges
10
10 bronze badges
$\endgroup$
1
$\begingroup$
Let's face it, empirically single factor models $dr=\sigma dw$ do a poor job of modeling the actual term structure. I believe adding $\lambda$ is a "hack" intended to improve the fit at the cost of model realism (after all interest rates don't always go up). An alternative approach is to go to 2 factor and other models like CIR and HJM.
$\endgroup$
nbbo2
–
nbbo2
2016-02-10 17:35:35 +00:00
Commented
Feb 10, 2016 at 17:35
Add a comment
|
1 Answer
1
Sorted by:
Reset to default
Highest score (default)
Date modified (newest first)
Date created (oldest first)
2
$\begingroup$
Many term structure models-both single-factor and multifactor imply dynamics
for the short-term riskless rate $r$ that can be nested within the following
stochastic differential equation:
$dr = (\alpha + \beta r)dt + \sigma r^\gamma dZ. $
These dynamics imply that the conditional mean and variance of changes in
the short-term rate depend on the level of $r$.
On your case we have $\alpha = \lambda$ and $\beta=\gamma=0$, and the model simplifies to the one on Merton (1973). So $\lambda$ is just capturing the growth over time of the interest rate.  If there was no uncertainty, it would mean that interest rates would grow forever. Usually we don't see this in the data that's why most models haave a $\beta < 0$ which implies that interest rates are mean reverting.
Share
Improve this answer
Follow
answered
Feb 10, 2016 at 15:45
phdstudent
9,356
4
4 gold badges
30
30 silver badges
57
57 bronze badges
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
fixed-income
interest-rates
term-structure
See similar questions with these tags.
Featured on Meta
Native Ads Coming To Comments
Related
11
Is Vasicek risk neutral?
4
Derivation of the Nelson-Siegel model and proof of arbitrage
1
Factor immunization for bond portfolio
2
Deriving interest rate term structure in a short rate model
4
Contango and backwardation in VIX futures
2
Why should future short rates tend towards the current term structure of interest rates?
Hot Network Questions
Can "nach" be followed by "ein"?
Who is Prof. Y in Einstein–Born correspondence?
Two numbered equations in one line
Anti-AI license that forbids end-users' AI use
How to procedurally generate instance sets in Geometry to Instance
Looking for true 100 amp DC SSR solution for 60 VDC 2 kW inductive heating coil load
How to write a scene with sword training (2 people) supervised by a 3rd person?
Do Large creatures automatically have Reach?
How to fix force unslant error in XeTeX/LuaTeX?
Is there an error regarding pre-orders and isomorphisms in "A Gentle Introduction to Category Theory" (pp. 42-43)?
Hand positions on MTB handlebars
Why can’t my Aetheros Wi-Fi adapter on an Asus Vivobook keep a stable connection?
Golf all the logic gates with X inputs and Y outputs!
Why did the elves abandon their immortality to live among mortals?
Statistical inference and visualization with n = 3 biological replicates
Are Vishnu and Parvati twins?
The Great Escape
Animated TV show from the 2010s with a character who was a hot dog actor
Does Quine believe a phenomenalist conceptual scheme can be translated into a physicalist conceptual scheme?
Hat Game with 2 prisoners
What is the point of making a timeline for your world?
Would a tidally locked planet have a higher or lower amount of photosynthesis compared to a planet like Earth?
Creating sharp, high-definition logo engraving with proper topology?
отвести = to conduct for a whole period
more hot questions
Question feed
default