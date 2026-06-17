---
title: spot rate - Eikon Government Benchmark Yield Curve - Quantitative Finance Stack
  Exchange
id: spot-rate-eikon-government-benchmark-yield-curve-quantitative-finance-stack-exch
tags:
- edge-detection paper-trading minimum-n
created: '2026-06-17T20:13:35.505452Z'
updated: '2026-06-17T20:28:23.121603Z'
source: https://quant.stackexchange.com/questions/73945/when-can-i-trust-my-out-of-sample-paper-trading-results
source_domain: quant.stackexchange.com
fetched_at: '2026-06-17T20:13:35.505282Z'
fetch_provider: builtin
status: review
type: note
tier: practitioner
content_type: forum
deprecated: false
summary: spot rate - Eikon Government Benchmark Yield Curve - Quantitative Finance
  Stack Exchange
---

spot rate - Eikon Government Benchmark Yield Curve - Quantitative Finance Stack Exchange
Stack Internal
Knowledge at work
Bring the best of human thought and AI automation together at your work.
Explore Stack Internal
Eikon Government Benchmark Yield Curve
Ask Question
Asked
3 years, 6 months ago
Modified
3 years, 6 months ago
Viewed
453 times
1
$\begingroup$
I want to price gov bonds using Bid Yields (column 5) from the screen below, and quantlib.
I am not sure what those Bid Yield rates represent.
Do those Bid yields represent spot rates, or what?
yield-curve
spot-rate
Share
Improve this question
Follow
edited
Dec 3, 2022 at 23:26
asked
Dec 3, 2022 at 23:20
Skittles
119
1
1 silver badge
11
11 bronze badges
$\endgroup$
3
$\begingroup$
Three are bond yields. Are you looking for a yield to price for India government bonds?
$\endgroup$
Dimitri Vulis
–
Dimitri Vulis
2022-12-04 00:57:33 +00:00
Commented
Dec 4, 2022 at 0:57
$\begingroup$
@Dimitri Vulis I am just trying to construct the yield curve to price india gov bonds, having access to both Bloomberg and eikon terminals, and using quantlib python, I thought that was the best method.
$\endgroup$
Skittles
–
Skittles
2022-12-04 11:01:58 +00:00
Commented
Dec 4, 2022 at 11:01
$\begingroup$
@DimitriVulis I opened a separate topic for this question, would appreciate if you could have a look;
quant.stackexchange.com/questions/73966/…
thank you!
$\endgroup$
Skittles
–
Skittles
2022-12-06 08:50:29 +00:00
Commented
Dec 6, 2022 at 8:50
Add a comment
|
1 Answer
1
Sorted by:
Reset to default
Highest score (default)
Date modified (newest first)
Date created (oldest first)
1
$\begingroup$
These are not spot rates, but yield to maturities of actual bonds. If you right click on the table, click on "Related," then "Quote," then you can see the actual bonds for each tenor. For example, currently the "10Y" bond has a coupon of 7.26%, maturing on August 22, 2022.
Share
Improve this answer
Follow
answered
Dec 4, 2022 at 6:29
Helin
12k
1
1 gold badge
27
27 silver badges
45
45 bronze badges
$\endgroup$
5
$\begingroup$
thank you,how to get spot rates, or what is the best way to price those bonds,please?
$\endgroup$
Skittles
–
Skittles
2022-12-04 08:59:15 +00:00
Commented
Dec 4, 2022 at 8:59
$\begingroup$
maybe fit the bond curve using Nelson Siegel and quantlib would be way to go?, but it takes 3 parameters:maturity, coupon and price. There is no issue date parameter, don't see how it would work without it?
$\endgroup$
Skittles
–
Skittles
2022-12-04 09:36:47 +00:00
Commented
Dec 4, 2022 at 9:36
$\begingroup$
or thinking about it, the method I mentioned above, if I specify semi-annual coupons in the code, is it going to apply automatic coupon dates such as maturity-6M and so on, and bootstrap those rates? apologies for 3 comments
$\endgroup$
Skittles
–
Skittles
2022-12-04 11:03:23 +00:00
Commented
Dec 4, 2022 at 11:03
$\begingroup$
As recommended by @Skittles, you need to perform curve fitting to get zero rates. Search for curve fitting or spline in this stackexchange and there are already many answers.
$\endgroup$
Helin
–
Helin
2022-12-04 15:58:18 +00:00
Commented
Dec 4, 2022 at 15:58
$\begingroup$
I opened a separate topic for this question, would appreciate if you could have a look;
quant.stackexchange.com/questions/73966/…
thank you!
$\endgroup$
Skittles
–
Skittles
2022-12-06 08:51:18 +00:00
Commented
Dec 6, 2022 at 8:51
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
yield-curve
spot-rate
See similar questions with these tags.
Featured on Meta
Native Ads Coming To Comments
Related
7
Bootstrapping spot rates from treasury yield curve
2
Who determine Sport rate curve (Yield Curve)
2
Bootstrap yield curve with QLNet / Quantlib
1
Constructing yield curve directly from yield-to-maturity data
4
Duration: Parallel shift in yield curve assumption
1
Interpolation for discount curve building QuantLib for bonds
0
QuantLib Yield Curve Bootstrapping Fails with Bracketing Error
Hot Network Questions
Statistics of a distribution on unitary matrices
Hand positions on MTB handlebars
How to include section template defined heading prefix in TOC?
Should I change my PhD position if the project is very different from what was advertised?
Early-morning (pre-6 AM) accessible route from Caumartin to Gare du Nord with luggage
Can I backup multiple Mac devices to a NAS on the LAN?
Short story - probably by Greg Egan - laborers sing "Dehumanize Yourself"
Was Carl Sagan correct to say that meteor entry is "completely silent"?
Where is Rav Meir Simcha quoted in the Mishnah Berura?
Does the half-minotaur template truly add Str and Con (and remove Dex) via size changes?
Does the explanatory chain 'Relation → Distinction → Identity → Content' hold? (Nothingness as limiting case)
An Implementation of a Mathematical Expression Postfix Parser
Give me an OEIS member
Reference for regularization "in general"
How to minimize the number of keypresses in my edit -> save -> run cycle?
What kind of hexagonal antenna is this?
Do FreeBSD jails allow running older version of the base system?
Hagbah: Why roll to a seam?
Do Large creatures automatically have Reach?
Golf all the logic gates with X inputs and Y outputs!
Who is this bird creature in Helheim?
Why would Australian Border Force issue Do Not Board for an outbound flight?
Two papers, the same appendix: Is it plagiarism?
Is there any reason to add red skulls to an organ?
more hot questions
Question feed
default